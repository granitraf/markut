"""One structured-JSON call path for every agent (run #26 audit, P0 item 1).

WHY one helper: each structured call grew its own retry — the governor's
showed the model its validation error, its previous reply and the JSON
skeleton; the judge's showed only the reply; the planner's asked for a
shorter reply whatever the failure; the coverage check had none. A model
that just failed needs the same three things every time: WHAT failed, what
it SAID, and the exact SHAPE to produce. This module is that one path:

  call -> parse -> ok
       -> cut off at the output cap  -> one SHORTER retry (too long is not malformed)
       -> invalid (parse/validation) -> one CORRECTIVE retry: error + previous reply + skeleton
       -> still failing              -> value None (the caller fails closed)
"""
import json

from markut.agents import llm


def schema_skeleton(schema: dict) -> str:
    """PURE. A JSON example of a schema's shape with typed placeholders —
    what a corrective retry shows the model to mirror."""
    def walk(s):
        t = (s or {}).get("type")
        if "enum" in (s or {}):
            return "<one of: " + " | ".join(str(v) for v in s["enum"]) + ">"
        if t == "object":
            props = s.get("properties") or {}
            return {k: walk(v) for k, v in props.items()}
        if t == "array":
            return [walk(s.get("items") or {})]
        if t in ("integer", "number"):
            return f"<{t}>"
        if t == "boolean":
            return "<true | false>"
        return f"<{t or 'value'}>"
    return json.dumps(walk(schema), indent=1, ensure_ascii=False)


def corrective_message(error, previous_reply: str, skeleton: str) -> str:
    return ("\n\nYour previous response FAILED validation with this exact error:\n  " + str(error)
            + "\n\nYour previous response was:\n" + (previous_reply or "")
            + "\n\nFix exactly that problem. Respond again with ONLY this JSON structure and nothing else:\n" + skeleton)


def call_structured(system_prompt: str, user_content: str, schema: dict, parse, max_tokens: int, label: str,
                    cached_prefix: str = None, shorter: str = None, skeleton: str = None) -> dict:
    """-> {"value": parsed or None, "raws": [replies], "error": last error or None,
    "truncated": bool}. parse(raw) must raise on anything invalid; it is
    where substance (not just shape) is validated."""
    skeleton = skeleton or schema_skeleton(schema)
    kwargs = {"max_tokens": max_tokens, "schema": schema}
    if cached_prefix is not None:
        kwargs["cached_prefix"] = cached_prefix
    raws = []
    raw = llm.call_claude(system_prompt, user_content, **kwargs)
    raws.append(raw)
    truncated_first = llm.LAST_STOP_REASON == "max_tokens"
    try:
        if truncated_first:
            raise ValueError("reply cut off at the output cap")
        return {"value": parse(raw), "raws": raws, "error": None, "truncated": False}
    except Exception as first_error:
        error = first_error
        print(f"{label}: {'reply truncated' if truncated_first else f'reply invalid ({first_error})'}; "
              f"one {'shorter' if truncated_first else 'corrective'} retry.")
        print(f"{label} DEBUG raw reply: {(raw or '')[:400]}")
    if truncated_first:
        # the overflow itself is not shown back: it would invite the model to
        # continue it; the cap, the shape and a shorter target are what help
        retry = (user_content + "\n\nYour previous reply exceeded the output limit and was cut off before the JSON closed. "
                 + (shorter or "Reply again MUCH SHORTER.") + " Finish the JSON. Required structure:\n" + skeleton)
    else:
        retry = user_content + corrective_message(error, raw, skeleton)
    raw_retry = ""
    try:
        raw_retry = llm.call_claude(system_prompt, retry, **kwargs)
        raws.append(raw_retry)
        if llm.LAST_STOP_REASON == "max_tokens":
            raise ValueError("retry also cut off at the output cap")
        return {"value": parse(raw_retry), "raws": raws, "error": None, "truncated": truncated_first}
    except Exception as second_error:
        print(f"{label}: retry also failed ({second_error}); failing closed.")
        print(f"{label} DEBUG raw retry reply: {(raw_retry or '')[:400]}")
        return {"value": None, "raws": raws, "error": second_error, "truncated": truncated_first}
