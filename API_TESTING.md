# API Regression Testing

The project includes a 30-case API-only regression suite. Every turn calls OpenAI Structured NLU and the final-response generation path. The suite does not use local NLU or `RuleInterpreter`.

## Test composition

| Group | Cases | Main coverage |
|---|---:|---|
| Complicated business cases | C01-C15 | Verification gates, remembered claim hints, compound questions, claim switching, corrections, field-level answer tracking, grounded denial/status/document/payment answers, and deadline handling |
| Emotional cases | C16-C25 | Anxiety, frustration, repeated uncertainty, empathetic responses, de-escalation, SOP recovery, and human-handoff behavior |
| Edge and scope cases | C26-C30 | Weather, coding, translation, personal advice, sports, polite refusal, out-of-scope counting, and scope escalation |

## What each group validates

### Complicated business cases: C01-C15

- C01 checks that claim details remain protected before identity verification.
- C02 and C04 validate multi-field questions and follow-up field tracking.
- C03, C11, and C15 validate switching between claims without losing authorization boundaries.
- C05 and C06 validate document guidance and receipt status.
- C07 validates denial-reason grounding for a selected claim.
- C08 validates a grounded claim summary.
- C09 and C10 validate missing-document answers and remembered case hints.
- C12 validates deadline abbreviations and field normalization.
- C13 validates grounded term explanations.
- C14 validates customer corrections.

### Emotional cases: C16-C25

These cases check that the agent acknowledges frustration, anxiety, confusion, or refusal before continuing the workflow. Emotional language can change tone and recovery behavior, but it cannot bypass identity verification, claim ownership, email consent, or the phase order. The set also checks escalation after repeated unresolved or emotionally difficult turns.

### Edge and out-of-scope cases: C26-C30

These cases send unrelated questions after the customer context is established. The agent must politely redirect to insurance claim support, avoid claim selection for unrelated turns, increment `out_of_scope_count`, and offer a human representative after the configured repeated-attempt threshold.

## Run all 30 cases

From this project directory:

```bash
export OPENAI_API_KEY="your API key"
export NLU_MODE=openai
pip install -r requirements-api-regression.txt
python tools/run_complex_30_api.py --limit 30
```

The runner prints progress by case and turn. It writes:

- `tests/complex_30_api_conversion_results.txt` — customer conversations first, followed by diagnostics and assertions.
- `tests/complex_30_customer_conversions.txt` — the 30 legacy case definitions.

Run selected cases while debugging:

```bash
python tools/run_complex_30_api.py --ids C07 C16 C26 C27 C30
```

The runner records the phase, verification status, selected claim, requested fields, answered fields, emotional state, email consent, handoff state, and `out_of_scope_count` for every turn.

## API-only acceptance criteria

- No claim detail is disclosed before verification.
- The selected claim belongs to the verified customer.
- Compound requested fields are all answered or explicitly marked unavailable.
- Generated answers stay grounded in authorized fixtures and guidance.
- Emotional support does not bypass SOP gates.
- Out-of-scope turns are refused politely and counted in session state.
- Repeated out-of-scope turns offer human assistance.
- The final response is generated through the OpenAI response layer and checked by Python guards.
