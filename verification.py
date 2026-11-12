"""Identity verification and claim authorization service."""

import re


def normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def normalize_email(value):
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def email_variants(value):
    normalized = normalize_email(value)
    variants = {normalized} if normalized else set()
    if "@" in normalized:
        local, domain = normalized.split("@", 1)
        if domain in {"gmail.com", "googlemail.com"}:
            variants.add(local.replace(".", "") + "@gmail.com")
            variants.add(local.split("+", 1)[0].replace(".", "") + "@gmail.com")
    return variants


def normalize_name(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def normalize_dob(value):
    return re.sub(r"\D", "", str(value or ""))


def name_variants(value):
    raw = re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())
    tokens = [token for token in raw.split() if token]
    variants = {normalize_name(value)} if tokens else set()
    if tokens:
        variants.add(normalize_name(" ".join(reversed(tokens))))
        variants.add(normalize_name(" ".join(sorted(tokens))))
    return variants


def identity_field_matches(field, supplied, expected, record):
    if field == "phone":
        aliases = [*record.get("phone_aliases", []), *record.get("phones", [])]
        return normalize_phone(supplied) in {normalize_phone(item) for item in [expected, *aliases]}
    if field == "email":
        aliases = [*record.get("email_aliases", []), *record.get("emails", [])]
        expected_values = set()
        for item in [expected, *aliases]:
            expected_values.update(email_variants(item))
        return bool(email_variants(supplied) & expected_values)
    if field == "full_name":
        aliases = [*record.get("name_aliases", []), *record.get("full_name_aliases", [])]
        expected_values = set()
        for item in [expected, *aliases]:
            expected_values.update(name_variants(item))
        return bool(name_variants(supplied) & expected_values)
    if field == "dob":
        return normalize_dob(supplied) == normalize_dob(expected)
    return str(supplied or "").strip().lower() == str(expected or "").strip().lower()


def verify(state, extracted):
    import app
    state["identity"].update({k: v for k, v in extracted.items() if k != "case_hint"})
    if extracted.get("case_hint") and extracted["case_hint"] not in state["case_hints"]:
        state["case_hints"].append(extracted["case_hint"])
    fields = ("full_name", "dob", "phone", "email", "ssn_last4")
    candidates = []
    for person in app.POLICYHOLDERS:
        expected = {"full_name": person["name"], "dob": person["dob"], "phone": person["phone"],
                    "email": person["email"], "ssn_last4": person["id_last4"]}
        matches = sum(bool(state["identity"].get(field)) and identity_field_matches(
            field, state["identity"][field], expected[field], person) for field in fields)
        if matches >= 3:
            candidates.append(person)
    if len(candidates) != 1:
        return False, None
    candidate = candidates[0]
    state["verified_fields"] = [field for field in fields if state["identity"].get(field)]
    state["verified_party_id"] = candidate["party_id"]
    state["gates"]["VERIFY_ID"] = True
    state["stage_progress"]["VERIFY_ID"] = 1.0
    app.advance_phase(state, "RESOLVE_INTENT")
    return True, candidate


def authorize_case_access(state):
    import app
    import case_service
    if not state["gates"]["VERIFY_ID"] or not state.get("verified_party_id"):
        return False, "Before I can access claim information, please provide at least three matching identity details: your full name, date of birth, phone number, email, or SSN last four digits."
    case_id = state.get("matched_case_id")
    if not case_id:
        return False, "Before I can access claim information, please provide the case ID you want to access."
    claim = case_service.authorized_claim(state["verified_party_id"], case_id)
    if not claim:
        if case_service.find_claim(case_id):
            return False, "I could not match that case to the identity you verified. Please provide a case ID associated with that identity."
        return False, "I could not locate that case. Please provide a valid case ID, such as CL-2048."
    if claim.get("party_id") != state["verified_party_id"]:
        return False, "I could not match that case to the identity you verified. Please provide a case ID associated with that identity."
    state["case_access_authorized"] = True
    return True, claim


__all__ = ["verify", "authorize_case_access", "identity_field_matches", "normalize_phone", "normalize_email", "normalize_name", "normalize_dob"]
