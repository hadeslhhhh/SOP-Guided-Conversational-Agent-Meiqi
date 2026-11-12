"""Independent authorization guard for protected claim data."""

IDENTITY_FIELDS = ("name", "dob", "phone", "email", "id_last4")


class IdentityVerificationGuard:
    def __init__(self, minimum_fields=3):
        self.minimum_fields = minimum_fields

    def check(self, state):
        supplied = {field: value for field, value in state.identity.items()
                    if field in IDENTITY_FIELDS and str(value or "").strip()}
        if not state.verified or not state.party_id:
            return {"passed": False, "reason": "identity_not_verified", "field_count": len(supplied)}
        if len(supplied) < self.minimum_fields:
            return {"passed": False, "reason": "insufficient_verified_fields", "field_count": len(supplied)}
        return {"passed": True, "reason": "verified", "field_count": len(supplied)}
