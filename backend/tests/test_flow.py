import base64
import os
import sys
from pathlib import Path

for key in (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "R2_ENDPOINT",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "SMTP_HOST",
    "SMTP_USER",
    "SMTP_PASSWORD",
):
    os.environ.pop(key, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(main.app)
AUTH = {"Authorization": "Bearer demo_token_user_1"}


def reset_state():
    main.memory_cases.clear()
    main.memory_documents.clear()
    main.memory_invoices.clear()
    main.memory_invoice_templates.clear()
    main.memory_evaluations.clear()


def create_case(name="Anna Schmidt", email="anna@example.com"):
    response = client.post(
        "/api/cases",
        headers=AUTH,
        json={
            "client_name": name,
            "client_email": email,
            "case_type": "Blue Card",
            "destination": "Germany",
            "notes": "Test case",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_case_creation_appears_in_case_list():
    reset_state()
    case = create_case()

    response = client.get("/api/cases", headers=AUTH)

    assert response.status_code == 200
    cases = response.json()
    assert any(item["id"] == case["id"] for item in cases)
    assert case["stage"] == "documents"


def test_case_upload_creates_document_and_delete_removes_it():
    reset_state()
    case = create_case()

    upload_response = client.post(
        f"/api/cases/{case['id']}/upload",
        headers=AUTH,
        files={"file": ("passport.pdf", b"fake pdf", "application/pdf")},
    )
    assert upload_response.status_code == 200
    document_id = upload_response.json()["document_id"]

    docs_response = client.get(f"/api/documents?case_id={case['id']}", headers=AUTH)
    assert docs_response.status_code == 200
    assert docs_response.json()[0]["id"] == document_id

    delete_response = client.delete(f"/api/cases/{case['id']}/documents/{document_id}", headers=AUTH)
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] is True

    docs_after = client.get(f"/api/documents?case_id={case['id']}", headers=AUTH)
    assert docs_after.status_code == 200
    assert docs_after.json() == []


def test_intake_matches_case_by_filename():
    reset_state()
    case = create_case()

    response = client.post(
        "/api/documents/intake",
        headers=AUTH,
        files={"file": ("anna-schmidt-passport.pdf", b"fake pdf", "application/pdf")},
    )

    assert response.status_code == 200
    document = response.json()
    assert document["status"] == "assigned"
    assert document["case_id"] == case["id"]


def test_intake_unrecognized_when_no_case_matches():
    reset_state()
    create_case()

    response = client.post(
        "/api/documents/intake",
        headers=AUTH,
        files={"file": ("unknown-client.pdf", b"fake pdf", "application/pdf")},
    )

    assert response.status_code == 200
    document = response.json()
    assert document["status"] == "unrecognized"
    assert document["case_id"] is None


def test_invoice_attachment_and_send_demo_mode():
    reset_state()
    case = create_case()
    invoice = {
        "id": "inv-test",
        "case_id": case["id"],
        "number": "INV-2026-001",
        "status": "draft",
        "client_name": case["client_name"],
        "client_email": case["client_email"],
        "issue_date": "2026-07-04",
        "due_date": "2026-07-11",
        "currency": "EUR",
        "notes": "Test invoice",
        "template_id": "tpl-default",
        "items": [{"description": "Legal service", "quantity": 1, "unit_price": 1190}],
        "attachments": [],
    }

    upsert_response = client.post("/api/invoices", headers=AUTH, json=invoice)
    assert upsert_response.status_code == 200
    assert upsert_response.json()["amount"] == 1190

    attachment_response = client.post(
        "/api/invoices/inv-test/attachments",
        headers=AUTH,
        files={"file": ("INV-2026-001.pdf", b"pdf bytes", "application/pdf")},
    )
    assert attachment_response.status_code == 200
    assert attachment_response.json()["name"] == "INV-2026-001.pdf"

    send_response = client.post("/api/invoices/inv-test/send", headers=AUTH, json={})
    assert send_response.status_code == 200
    payload = send_response.json()
    assert payload["to"] == case["client_email"]
    assert payload["sent"] is False
    assert payload["status"] == "queued_demo"


def test_email_webhook_routes_documents_by_sender():
    reset_state()
    case = create_case(email="client@example.com")
    encoded = base64.b64encode(b"pdf bytes").decode("utf-8")

    response = client.post(
        "/api/webhook/email",
        json={
            "from": "client@example.com",
            "subject": "Documents",
            "attachments": [
                {
                    "filename": "passport.pdf",
                    "content_base64": encoded,
                    "content_type": "application/pdf",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["matched_cases"] == [case["id"]]
    docs_response = client.get(f"/api/documents?case_id={case['id']}", headers=AUTH)
    assert docs_response.status_code == 200
    assert docs_response.json()[0]["source"] == "email"


def test_ocr_evaluation_extracts_fields_and_stores_score():
    reset_state()
    case = create_case()
    text = """
    Passport
    Name: Anna Schmidt
    Passport number: C12345678
    Date of birth: 01.02.1990
    Nationality: German
    Employer: Demo GmbH
    """

    response = client.post(
        "/api/ocr/evaluate",
        headers=AUTH,
        json={"text": text, "filename": "passport-anna-schmidt.pdf", "case_id": case["id"], "document_id": "doc-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["fields"]["document_type"] == "passport"
    assert payload["fields"]["full_name"] == "Anna Schmidt"
    assert payload["fields"]["passport_number"] == "C12345678"
    assert payload["fields"]["date_of_birth"] == "1990-02-01"
    assert payload["evaluation"]["score"] >= 0.65

    evaluations = client.get(f"/api/evaluations?case_id={case['id']}", headers=AUTH)
    assert evaluations.status_code == 200
    assert evaluations.json()[0]["document_id"] == "doc-1"


def test_gmail_attachment_extractor_builds_webhook_payload():
    reset_state()
    message = main.EmailMessage()
    message["From"] = "Client <client@example.com>"
    message["Subject"] = "Documents"
    message.set_content("Attached.")
    message.add_attachment(b"pdf bytes", maintype="application", subtype="pdf", filename="passport.pdf")

    attachments = main.extract_gmail_attachments(message)

    assert len(attachments) == 1
    assert attachments[0].filename == "passport.pdf"
    assert base64.b64decode(attachments[0].content_base64) == b"pdf bytes"
