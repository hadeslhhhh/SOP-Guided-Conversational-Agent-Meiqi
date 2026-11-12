import pytest

import app

pytestmark = pytest.mark.api


# These cases deliberately include near-misses: receiving vs submitting,
# missing vs required, denial vs payment, and deadline vs processing time.
CASES = [
    ("What is the current status of my claim?", "status_inquiry"),
    ("Where does my claim stand?", "status_inquiry"),
    ("Is my claim still being processed?", "status_inquiry"),
    ("Why was my claim denied?", "denial_question"),
    ("My reimbursement was turned down.", "denial_question"),
    ("The insurer did not approve the payment.", "denial_question"),
    ("What documents are still missing?", "missing_documents"),
    ("Did I miss any paperwork?", "missing_documents"),
    ("Which records do I still need?", "missing_documents"),
    ("How do I upload the missing documents?", "document_submission"),
    ("Where can I submit the report?", "document_submission"),
    ("Can I send the documents by fax?", "document_submission"),
    ("What documents are required?", "document_requirements"),
    ("What format should the files be in?", "document_requirements"),
    ("Does each page need a signature?", "document_requirements"),
    ("What can I submit instead of the pathology report?", "document_alternative"),
    ("I cannot get that record; is there an alternative?", "document_alternative"),
    ("What can replace the missing document?", "document_alternative"),
    ("When should I submit the documents?", "submission_deadline"),
    ("What is the deadline for sending them?", "submission_deadline"),
    ("How soon do I need to provide the report?", "submission_deadline"),
    ("How long will the review take?", "processing_time"),
    ("When will the claim be processed?", "processing_time"),
    ("How soon will I hear back after uploading?", "processing_time"),
    ("Did you receive the documents I uploaded?", "receipt_confirmation"),
    ("Have you received my files?", "receipt_confirmation"),
    ("Was my upload received?", "receipt_confirmation"),
    ("Can I appeal the denial?", "appeal_question"),
    ("How do I challenge this decision?", "appeal_question"),
    ("How much time do I have to appeal?", "appeal_question"),
    ("How much will I receive?", "payment_question"),
    ("What is my reimbursement amount?", "payment_question"),
    ("What do I owe?", "payment_question"),
    ("Can you summarize my case?", "case_summary"),
    ("Give me an overview of the claim.", "case_summary"),
    ("What happened in my case?", "case_summary"),
    ("What should I do next?", "next_steps"),
    ("What are the next steps?", "next_steps"),
    ("What do I do now?", "next_steps"),
    ("I need to speak with a human.", "human_transfer"),
    ("Please connect me with a representative.", "human_transfer"),
    ("Transfer me to an agent.", "human_transfer"),
    ("I am worried about my claim.", "emotional_support"),
    ("I feel overwhelmed by this situation.", "emotional_support"),
    ("I am anxious and scared.", "emotional_support"),
    ("What does pathology report mean?", "term_explanation"),
    ("Can you explain the office note?", "term_explanation"),
    ("What is a deductible?", "term_explanation"),
    ("I need help with my claim.", "unclear"),
    ("Can you help me?", "unclear"),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_intent_regression_50(text, expected):
    result = app.classify_intent(text, app.blank_state())
    assert result["intent"] == expected, (text, result)
