"""Proof that every notebook prompt is preserved byte-identical as the
*_NOTEBOOK constant in markut.agents.prompts, and that the LIVE prompts
extend (never silently rewrite) that text. Reads the untouched markut.ipynb,
evaluates each original system_prompt literal, and compares — an automated
diff, not an eyeball."""
import json
import os

import pytest

from markut.agents import prompts

NB = os.path.join(os.path.dirname(__file__), "..", "markut.ipynb")


def _cell(nb, i):
    return "".join(nb["cells"][i]["source"])


def _grab(src, assign, indent="    "):
    pat = indent + assign + " = ("
    i = src.index(pat)
    close = "\n" + indent + ")"
    j = src.index(close, i) + len(close)
    return src[i + len(indent + assign + " = "):j]


@pytest.fixture(scope="module")
def nodes_cell():
    nb = json.load(open(NB))
    for i, c in enumerate(nb["cells"]):
        s = "".join(c["source"])
        if c["cell_type"] == "code" and s.startswith("def research_node"):
            return s
    raise AssertionError("nodes cell not found")


@pytest.mark.parametrize("const, anchor", [
    ("BULL_SYSTEM_PROMPT", "def bull_node"),
    ("BEAR_SYSTEM_PROMPT", "def bear_node"),
    ("JUDGE_SYSTEM_PROMPT", "def judge_node"),
    ("NEWS_VERIFY_SYSTEM_PROMPT", "def news_verify_node"),
])
def test_agent_prompt_verbatim(nodes_cell, const, anchor):
    literal = _grab(nodes_cell[nodes_cell.index(anchor):], "system_prompt")
    notebook_text = eval(literal)
    assert notebook_text == getattr(prompts, const + "_NOTEBOOK")
    # the live prompt starts with the notebook's exact text and only ADDS to it
    assert getattr(prompts, const).startswith(notebook_text)


def test_review_skeleton_and_prompt_verbatim(nodes_cell):
    seg = nodes_cell[nodes_cell.index("def review_node"):]
    skeleton = eval(_grab(seg, "json_skeleton"))
    assert skeleton == prompts.REVIEW_JSON_SKELETON
    rendered = eval(_grab(seg, "system_prompt"), {"json_skeleton": skeleton})
    assert rendered == prompts.REVIEW_SYSTEM_PROMPT


def test_baseline_prompt_verbatim():
    nb = json.load(open(NB))
    harness_cell = next("".join(c["source"]) for c in nb["cells"]
                        if c["cell_type"] == "code"
                        and "_eval_baseline_system = (" in "".join(c["source"]))
    literal = _grab(harness_cell, "_eval_baseline_system", indent="        ")
    assert eval(literal) == prompts.BASELINE_SYSTEM_PROMPT
