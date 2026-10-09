"""JSON schemas for the three structured replies (judge, claim review,
governor revision). AUDIT FIX (run #2): the claim review returned non-JSON
for every claim and the judge needed corrective retries — free-text "respond
with ONLY JSON" is a request, output_config.format is a guarantee. The
parsers in guardrails.parsing still validate SUBSTANCE (non-empty verdict,
allowed resolution values, DERIVED needs anchors); the schema settles SHAPE."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            # one ruling per planner question, before the overall verdict
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "answer": {"type": "string"},
                    "stronger_side": {"type": "string", "enum": ["bull", "bear", "neither"]},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "unsupported_claims": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "answer", "stronger_side", "confidence", "unsupported_claims"],
                "additionalProperties": False,
            },
        },
        "bull_strongest": {"type": "string"},
        "bear_strongest": {"type": "string"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
        "verdict": {"type": "string"},
        "converged": {"type": "boolean"},
        "planner_coverage": {"type": "string"},
    },
    "required": ["questions", "bull_strongest", "bear_strongest", "unsupported_claims", "reasoning", "verdict",
                 "converged", "planner_coverage"],
    "additionalProperties": False,
}

ARCHETYPES = ["semis_hardware", "software", "consumer_retail_restaurant", "bank", "insurer", "reit",
              "energy_commodity", "pharma_biotech", "industrial", "utility", "other"]
ACCOUNTING_FLAGS = ["lease_heavy", "acquisition_amortization", "large_sbc", "unprofitable", "cyclical",
                    "regulated", "financial_balance_sheet"]

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "business": {"type": "string"},
        "archetype": {"type": "string", "enum": ARCHETYPES},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "latest_revenue": {"type": "string"},
                    "latest_yoy": {"type": "string"},
                    "period": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["name", "latest_revenue", "latest_yoy", "period", "source"],
                "additionalProperties": False,
            },
        },
        "company_kpis": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "definition": {"type": "string"},
                    "unit": {"type": "string"},
                    "segment": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["name", "definition", "unit", "segment", "source"],
                "additionalProperties": False,
            },
        },
        "accounting_flags": {"type": "array", "items": {"type": "string", "enum": ACCOUNTING_FLAGS}},
    },
    "required": ["business", "archetype", "segments", "company_kpis", "accounting_flags"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "key_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "question": {"type": "string"},
                    "why": {"type": "string"},
                    "kpis": {"type": "array", "items": {"type": "string"}},
                    "segments": {"type": "array", "items": {"type": "string"}},
                    # filing-style sentences research embeds to find the evidence (no model call in research)
                    "search_queries": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "question", "why", "kpis", "segments", "search_queries"],
                "additionalProperties": False,
            },
        },
        "what_would_mislead": {"type": "array", "items": {"type": "string"}},
        "peer_tickers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["key_questions", "what_would_mislead", "peer_tickers"],
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
