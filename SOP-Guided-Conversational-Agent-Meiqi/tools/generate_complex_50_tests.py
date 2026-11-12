"""Generate 50 varied, realistic complex customer conversations."""
import json
from pathlib import Path

ROOT = Path(__file__).parent
TARGET = ROOT / "tests" / "generated_complex_customers_50.jsonl"
IDENTITY = "Hi, this is Margaret Chen. My date of birth is 1985-03-15 and the last four digits of my SSN are 4472."

GROUPS = {
    "multi_question": [
        ("I uploaded the documents yesterday. Did you receive them, and how long will the review take?", "receipt_confirmation"),
        ("Why was the claim denied, and what is the appeal deadline?", "denial_question"),
        ("What documents are missing, and can I upload them from my phone?", "missing_documents"),
        ("How much will I receive, and when should the payment be issued?", "payment_question"),
        ("Can you summarize the denial and tell me what I should do next?", "case_summary"),
        ("What format should the pathology report have, and where do I send it?", "document_requirements"),
        ("Can I use a replacement report, and by what date should I send it?", "document_alternative"),
        ("Is the claim still being processed, or do you need something else from me?", "status_inquiry"),
        ("How do I appeal this, and how much time do I have?", "appeal_question"),
        ("What do I owe out of pocket, and what amount was allowed?", "payment_question"),
    ],
    "emotional": [
        ("I am honestly overwhelmed and angry. Please tell me why this claim was denied.", "denial_question"),
        ("I am worried sick about the delay—did my upload actually arrive?", "receipt_confirmation"),
        ("This is exhausting and I feel ignored. What documents are still missing?", "missing_documents"),
        ("I am scared I will miss the deadline. When exactly do I need to submit this?", "submission_deadline"),
        ("I am frustrated, but I need a straight answer: how much will I receive?", "payment_question"),
        ("I feel lost. Can you explain what the office note means?", "term_explanation"),
        ("I am panicking about the denial. What can I do next?", "next_steps"),
        ("I am really upset with how long this is taking. How long is the review?", "processing_time"),
        ("I cannot get the original report and I am stressed. What can replace it?", "document_alternative"),
        ("I am disappointed and confused. Could you summarize what happened?", "case_summary"),
    ],
    "unrelated": [
        ("Before we continue, can you recommend a restaurant?", "unclear"),
        ("What is the weather supposed to be like tomorrow?", "unclear"),
        ("Can you help me write an email to my landlord?", "unclear"),
        ("I just remembered—what is a good movie to watch tonight?", "unclear"),
        ("Can you translate a short paragraph for me?", "unclear"),
        ("What is the exchange rate today?", "unclear"),
        ("Do you know how to reset my Wi-Fi router?", "unclear"),
        ("Tell me something funny; I need a break from this conversation.", "unclear"),
        ("Can you plan a two-day trip for me?", "unclear"),
        ("What time does the store close?", "unclear"),
    ],
    "multiple_case": [
        ("For CL-2048, why was it denied? I also want to know whether my other claim is still open.", "denial_question"),
        ("Can you check CL-2048 and CL-3001? I need the status of both before I call my provider.", "status_inquiry"),
        ("The documents for CL-2048 were uploaded, but the other claim may still be missing records. Can you check both?", "status_inquiry"),
        ("I have two claim numbers in my notes. Which one needs the pathology report, and what is the deadline?", "missing_documents"),
        ("Please review CL-2048 first, then tell me whether my second case needs an appeal.", "status_inquiry"),
        ("For this denied claim, how much would I receive, and is the amount different on my other case?", "payment_question"),
        ("Can you summarize both claim outcomes in plain English?", "case_summary"),
        ("I sent documents for both cases. Did you receive the uploads for each one?", "receipt_confirmation"),
        ("Which claim should I appeal first, and how much time is left for each?", "appeal_question"),
        ("I am not sure which case we are discussing. Can you list the claim status before answering?", "status_inquiry"),
    ],
    "human_transfer": [
        ("I have explained this three times. Please transfer me to a human representative.", "human_transfer"),
        ("I do not want a scripted answer. Can a real claims agent call me?", "human_transfer"),
        ("Please escalate this to a supervisor; I need someone to review the denial with me.", "human_transfer"),
        ("Could you connect me with a person who can discuss the payment amount?", "human_transfer"),
        ("I am too frustrated to keep typing. I need a live representative now.", "human_transfer"),
        ("Please hand this over to a human after you record my question.", "human_transfer"),
        ("Can someone from claims speak with me about the missing documents?", "human_transfer"),
        ("I want a supervisor, not another automated explanation.", "human_transfer"),
        ("Would you please arrange a human callback about this appeal?", "human_transfer"),
        ("I need live help and would like to transfer this conversation.", "human_transfer"),
    ],
}


def main():
    rows = []
    number = 201
    for kind, cases in GROUPS.items():
        for question, intent in cases:
            rows.append({"id": number, "customer": "Margaret Chen", "kind": kind, "expected_intent": intent,
                         "turns": [IDENTITY, "My claim number is CL-2048.", question, "Yes, please continue.", "No, that is all."]})
            number += 1
    TARGET.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    print(f"Generated {len(rows)} complex tests: {TARGET}")


if __name__ == "__main__":
    main()
