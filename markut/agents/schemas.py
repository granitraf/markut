"""JSON schemas for the three structured replies (judge, claim review,
governor revision). AUDIT FIX (run #2): the claim review returned non-JSON
for every claim and the judge needed corrective retries — free-text "respond
with ONLY JSON" is a request, output_config.format is a guarantee. The
parsers in guardrails.parsing still validate SUBSTANCE (non-empty verdict,
allowed resolution values, DERIVED needs anchors); the schema settles SHAPE."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "bull_strongest": {"type": "string"},
        "bear_strongest": {"type": "string"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
        "verdict": {"type": "string"},
        "converged": {"type": "boolean"},
    },
    "required": ["bull_strongest", "bear_strongest", "unsupported_claims", "reasoning", "verdict", "converged"],
    "additionalProperties": False,
}

CLAIM_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "claim_reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "status": {"type": "string", "enum": ["supported", "contradicted", "unresolved"]},
                    "evidence_summary": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["claim", "status", "evidence_summary", "sources"],
                "additionalProperties": False,
            },
        },
        "reasoning": {"type": "string"},
        "verdict_changed": {"type": "boolean"},
        "revised_verdict": {"type": "string"},
    },
    "required": ["claim_reviews", "reasoning", "verdict_changed", "revised_verdict"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string"},
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "resolution": {"type": "string", "enum": ["DERIVED", "REVISED", "LABELED"]},
                    "anchors": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["claim", "resolution", "anchors"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "resolutions"],
    "additionalProperties": False,
}
