import json
from pathlib import Path
from .verification import identity_field_matches

class Repository:
    def __init__(self, email_tool=None, handoff_tool=None):
        self.email_tool = email_tool
        self.handoff_tool = handoff_tool
        root = Path(__file__).resolve().parents[1] / 'fixtures'
        self.people = json.loads((root / 'policyholders.json').read_text())
        self.claims = json.loads((root / 'claims.json').read_text())
        self.guidance = json.loads((root / 'required_document_guideline.json').read_text())

    def verify(self, identity):
        result = self.verify_detailed(identity)
        return result["person"]

    def verify_detailed(self, identity):
        fields = ('name', 'dob', 'phone', 'email', 'id_last4')
        if len(set(identity) & set(fields)) < 3:
            return {"person": None, "matched_fields": 0, "mismatches": [], "status": "pending"}
        matches = []
        best_mismatches = []
        best_count = 0
        for person in self.people:
            correct = 0
            mismatches = []
            for field in fields:
                if field not in identity:
                    continue
                record_field = 'full_name' if field == 'name' else field
                expected = person.get(field, '')
                record = dict(person)
                if identity_field_matches(record_field, identity[field], expected, record):
                    correct += 1
                else:
                    # A conversational name such as "Margaret" can be a
                    # partial form of a trusted full name ("Margaret Chen").
                    # It is not counted as identity evidence, but it is also
                    # not treated as contradictory when the other supplied
                    # fields independently satisfy the three-field gate.
                    if not (field == 'name' and self._is_trusted_partial_name(identity[field], expected, record)):
                        mismatches.append(field)
            if correct > best_count:
                best_count, best_mismatches = correct, mismatches
            if correct >= 3 and not mismatches:
                matches.append(person)
        if len(matches) == 1:
            return {"person": matches[0], "matched_fields": best_count, "mismatches": [], "status": "verified"}
        status = "failed" if best_count >= 3 and best_mismatches else "pending"
        return {"person": None, "matched_fields": best_count, "mismatches": best_mismatches, "status": status}

    @staticmethod
    def _is_trusted_partial_name(supplied, expected, record):
        import re
        supplied_tokens = re.findall(r"[a-z0-9]+", str(supplied or '').lower())
        if len(supplied_tokens) != 1:
            return False
        names = [expected, *record.get('name_aliases', []), *record.get('full_name_aliases', [])]
        for name in names:
            trusted_tokens = re.findall(r"[a-z0-9]+", str(name or '').lower())
            if len(trusted_tokens) > 1 and supplied_tokens[0] in trusted_tokens:
                return True
        return False

    def matched_identity_field_count(self, identity, person):
        """Count matching supplied fields without exposing their values."""
        fields = ('name', 'dob', 'phone', 'email', 'id_last4')

        count = 0
        for field in fields:
            if field not in identity:
                continue
            record_field = 'full_name' if field == 'name' else field
            if identity_field_matches(record_field, identity[field], person.get(field, ''), person):
                count += 1
        return count

    def claims_for(self, party_id, hint):
        found = [c for c in self.claims if c['party_id'] == party_id]
        if hint.case_id:
            return [c for c in found if c['case_id'] == hint.case_id]
        for field in ('case_type', 'status'):
            value = getattr(hint, field)
            if value:
                found = [c for c in found if c[field] == value]
        if getattr(hint, 'excluded_case_type', None):
            found = [c for c in found if c['case_type'] != hint.excluded_case_type]
        if hint.month:
            found = [c for c in found if int(c['created_at'][5:7]) == hint.month]
        if hint.year:
            found = [c for c in found if int(c['created_at'][:4]) == hint.year]
        return found

    def claims_for_party(self, party_id):
        return [c for c in self.claims if c['party_id'] == party_id]

    def claim(self, party_id, case_id):
        return next((c for c in self.claims if c['party_id'] == party_id and c['case_id'] == case_id), None)

    def authorized_claim(self, state):
        """Return claim data only after the authorization boundary passes."""
        if not getattr(state, 'verified', False):
            return None
        party_id = getattr(state, 'party_id', None)
        case_id = getattr(state, 'case_id', None)
        if not party_id or not case_id:
            return None
        return self.claim(party_id, case_id)

    def other_claim_ids(self, party_id, excluded_case_id=None):
        return [
            claim['case_id'] for claim in self.claims
            if claim.get('party_id') == party_id and claim.get('case_id') != excluded_case_id
        ]

    def select_case_id(self, text, options):
        import re
        ids = re.findall(r'\bCL[- ]?\d+\b', text, re.I)
        if ids:
            candidate = ids[-1].upper().replace(' ', '-')
            return candidate if candidate in options else None
        if re.fullmatch(r'\s*\d+\s*', text):
            index = int(text.strip()) - 1
            return options[index] if 0 <= index < len(options) else None
        return None

    def person(self, party_id):
        return next(p for p in self.people if p['party_id'] == party_id)
