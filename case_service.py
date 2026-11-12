"""Claim lookup, matching, and authorized case-processing boundary."""

import json
import os
import re
from pathlib import Path


def load_claims():
    """Load claims from the project-local fixture directory."""
    root = Path(__file__).parent
    fixture_dir = Path(os.getenv("FIXTURES_DIR", str(root / "fixtures")))
    with (fixture_dir / "claims.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def find_claim(case_id, claims=None):
    """Return a claim by ID without exposing it to an unverified caller."""
    records = load_claims() if claims is None else claims
    return next((claim for claim in records if claim.get("case_id") == case_id), None)


def authorized_claim(party_id, case_id, claims=None):
    """Return a claim only when it belongs to the already verified party."""
    if not party_id or not case_id:
        return None
    claim = find_claim(case_id, claims)
    return claim if claim and claim.get("party_id") == party_id else None


def other_claim_ids(party_id, excluded_case_id=None, claims=None):
    """Return only claim identifiers owned by a verified party."""
    return [
        claim["case_id"] for claim in (load_claims() if claims is None else claims)
        if claim.get("party_id") == party_id and claim.get("case_id") != excluded_case_id
    ]


def select_case_id(text, options):
    """Parse an explicit claim ID or numbered choice from a safe option list."""
    case_ids = re.findall(r"\bCL[- ]?\d+\b", text, re.I)
    if case_ids:
        candidate = case_ids[-1].upper().replace(" ", "-")
        return candidate if candidate in options else None
    selection = re.fullmatch(r"\s*(\d+)\s*", text)
    if selection:
        index = int(selection.group(1)) - 1
        if 0 <= index < len(options):
            return options[index]
    return None


def resolve_intent(state, text, extracted):
    import app
    return app.resolve_intent(state, text, extracted)


def process_case(state):
    import app
    return app.process_case(state)


__all__ = ["resolve_intent", "process_case", "load_claims", "find_claim", "authorized_claim", "other_claim_ids", "select_case_id"]
