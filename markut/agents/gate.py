"""The evidence gate for the lean (run #28, TSM — 2026-10-11).

WHY: with no filings read and 0 of 5 key questions covered, the judge still
issued "Lean: bullish" — on market data alone, much of it mis-scaled. A stance
needs evidence under it. The gate is CODE, not a prompt request: the judge is
told before it writes, and its lean is overridden after, whatever it returns.

  no SEC filings read                 -> no lean (the reason is shown)
  fewer than 2 of 5 questions covered -> no lean
  exactly 2 covered                   -> a lean, confidence capped at low
"""
import re

LEAN_MIN_COVERED = 2        # questions with current-period evidence needed for any lean
LEAN_LOW_CAP_AT = 2         # at exactly this many, confidence is capped at "low"

_FILINGS_UNAVAILABLE_RE = re.compile(
    r"\[FILINGS\]\s*\n\s*\[(?:filings unavailable|SEC filings: unavailable)\s*(?::|—)\s*([^\]\n]*)\]", re.IGNORECASE)


def filings_unavailable(evidence: str) -> str:
    """PURE. The stated reason when the packet's [FILINGS] block says no
    filings could be read; '' when filings are present."""
    m = _FILINGS_UNAVAILABLE_RE.search(evidence or "")
    return (m.group(1).strip() or "no filings found") if m else ""


def lean_gate(state: dict) -> dict:
    """PURE. {"allowed": bool, "cap": None | "low", "reason": str}."""
    why = filings_unavailable((state or {}).get("evidence", ""))
    if why:
        return {"allowed": False, "cap": None,
                "reason": f"no SEC filings could be read ({why}), so the evidence is market data alone"}
    cov = (state or {}).get("coverage") or {}
    if cov:
        total = len(cov)
        covered = sum(1 for v in cov.values() if v)
        if covered < LEAN_MIN_COVERED:
            return {"allowed": False, "cap": None,
                    "reason": f"only {covered} of {total} key questions have current-period evidence in the filings"}
        if covered == LEAN_LOW_CAP_AT:
            return {"allowed": True, "cap": "low",
                    "reason": f"only {covered} of {total} key questions have current-period evidence in the filings"}
    return {"allowed": True, "cap": None, "reason": ""}


def gate_note(g: dict) -> str:
    """The line the judge reads before it writes (empty when nothing applies)."""
    if not g["allowed"]:
        return ("\n\nLEAN GATE: the lean is WITHHELD — " + g["reason"] + ". The code will not issue a lean whatever you return: "
                "set lean to 'neutral', lean_confidence to 'low', put the reason in lean_reason, and open the verdict by saying "
                "plainly what the evidence can and cannot show. Do not write or imply a bullish or bearish stance.")
    if g["cap"] == "low":
        return "\n\nLEAN GATE: confidence is capped at LOW — " + g["reason"] + "."
    return ""


def apply_gate(decision: dict, g: dict) -> dict:
    """The judge's decision with the gate applied: a withheld lean is None
    with the reason; a capped one keeps its side at low confidence."""
    out = dict(decision or {})
    if not g["allowed"]:
        out["lean"] = None
        out["lean_withheld"] = g["reason"]
    elif g["cap"] == "low" and out.get("lean") and out.get("lean_confidence") in ("medium", "high"):
        out["lean_confidence"] = "low"
        out["lean_capped"] = g["reason"]
    return out
