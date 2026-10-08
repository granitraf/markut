"""Strict-JSON parsers for model responses (moved verbatim). A parse failure
is a FEATURE: it triggers the caller's corrective retry / fail-closed path.
"""
import json
import re


def parse_judge_json(text: str) -> dict:
    # WHY: the model often wraps JSON in ```json ... ``` markdown fences even when
    # told not to. We strip those fences first so json.loads sees pure JSON,
    # instead of failing on the backticks and forcing an unnecessary retry.
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    data = json.loads(cleaned)  # raises json.JSONDecodeError if not valid JSON

    # WHY: these two keys drive graph control flow. If the model returns valid
    # JSON but omits them, silently defaulting could loop forever or crash the
    # router. Raising here forces judge_node's corrective retry / fail-closed path.
    for required in ("converged", "verdict"):
        if required not in data:
            raise ValueError(f"judge JSON missing required key: {required!r}")
    return data


def parse_review_json(text: str) -> dict:
    # Style-mirror of parse_judge_json (fence-strip) for the revision
    # response — defined HERE so the helpers cell stays untouched.
    # TOLERANT on shape, STRICT on substance: a dict keyed by claim carries
    # the same information as the canonical list, so it is CONVERTED, not
    # rejected — a live run failed twice over exactly that formatting trivia.
    # Missing DATA still raises (empty verdict, entry without a claim or a
    # valid resolution value, DERIVED with no anchors): each raise triggers
    # the corrective retry and, past that, the fail-closed annotation path.
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    data = json.loads(cleaned)
    for required in ("verdict", "resolutions"):
        if required not in data:
            raise ValueError(f"review JSON missing required key: {required!r}")
    if not str(data["verdict"]).strip():
        raise ValueError("review verdict must be a non-empty string")
    raw_resolutions = data["resolutions"]
    if isinstance(raw_resolutions, dict):
        if "resolution" in raw_resolutions:
            # a SINGLE resolution object passed bare -> one-element list
            raw_resolutions = [raw_resolutions]
        else:
            # {"20-50%": {...}, "30-40%": {...}} -> canonical list, the claim
            # key folded into each entry (an entry's own "claim" wins if set)
            raw_resolutions = [
                (dict(entry, claim=entry.get("claim") or claim_key)
                 if isinstance(entry, dict) else entry)
                for claim_key, entry in raw_resolutions.items()
            ]
    if not isinstance(raw_resolutions, list):
        raise ValueError("review resolutions must be a JSON array of objects "
                         f"(got {type(data['resolutions']).__name__})")
    allowed = {"DERIVED", "REVISED", "LABELED"}
    resolutions = []
    for res in raw_resolutions:
        if not isinstance(res, dict):
            raise ValueError("each resolution must be a JSON object "
                             f"(got {type(res).__name__})")
        res["resolution"] = str(res.get("resolution", "")).upper()
        if res["resolution"] not in allowed:
            raise ValueError("resolution value must be DERIVED, REVISED, or "
                             f"LABELED (got {res['resolution']!r})")
        res["claim"] = str(res.get("claim") or "").strip()
        if not res["claim"]:
            raise ValueError("a resolution entry is missing its claim")
        if not isinstance(res.get("anchors"), list):
            res["anchors"] = []
        if res["resolution"] == "DERIVED" and not res["anchors"]:
            raise ValueError(f"DERIVED resolution for {res['claim']!r} has no anchors")
        resolutions.append(res)
    data["resolutions"] = resolutions
    data["verdict"] = str(data["verdict"])
    return data


def salvage_judge_json(text: str) -> dict:
    """Best-effort recovery of a judge reply cut off mid-JSON (run #6: both
    replies ended inside a string at the output cap). Completed string fields
    are recovered whole; the field that was cut keeps its text with a
    [truncated] marker; `converged` is recovered only if it was written.
    Returns {} when nothing recognizable is present. Never raises."""
    raw = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```?$", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    out = {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    string_keys = ("bull_strongest", "bear_strongest", "reasoning", "verdict")
    for key in string_keys:
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key, raw)
        if m:
            try:
                out[key] = json.loads('"' + m.group(1) + '"')
            except Exception:
                out[key] = m.group(1)
    # the field the cap landed in: an opening quote with no closing one
    for key in string_keys:
        if key in out:
            continue
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)$' % key, raw, re.DOTALL)
        if m and m.group(1).strip():
            out[key] = m.group(1).rstrip() + " [truncated]"
    m = re.search(r'"unsupported_claims"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
    if m:
        try:
            out["unsupported_claims"] = json.loads("[" + m.group(1) + "]")
        except Exception:
            out["unsupported_claims"] = [c for c in re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))]
    m = re.search(r'"converged"\s*:\s*(true|false)', raw)
    if m:
        out["converged"] = (m.group(1) == "true")
    if out:
        out["truncated"] = True
    return out
