import base64
import os
import sys
from datetime import datetime, timedelta, timezone
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
from local_ocr import run_local_ocr  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(main.app)
AUTH = {"Authorization": "Bearer demo_token_user_1"}


def auth_for(user_id: str):
    return {"Authorization": f"Bearer demo_token_{user_id}"}


def reset_state():
    main.memory_cases.clear()
    main.memory_documents.clear()
    main.memory_invoices.clear()
    main.memory_invoice_templates.clear()
    main.memory_evaluations.clear()
    main.memory_profiles.clear()
    main.memory_firms.clear()
    main.memory_email_integrations.clear()


def create_case(name="Anna Schmidt", email="anna@example.com", auth=AUTH):
    response = client.post(
        "/api/cases",
        headers=auth,
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


def test_free_local_ocr_extracts_text_from_pdf():
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.drawString(72, 760, "Passport")
    pdf.drawString(72, 740, "Name: Anna Schmidt")
    pdf.drawString(72, 720, "Passport number: C12345678")
    pdf.drawString(72, 700, "Date of birth: 01.02.1990")
    pdf.save()
    content = buffer.getvalue()

    ocr = run_local_ocr(content, "passport.pdf")

    assert ocr["provider"] == "pypdf"
    assert "Anna Schmidt" in ocr["raw_text"]
    assert ocr["confidence"] >= 0.65


def make_pdf(lines):
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    y = 760
    for line in lines:
        pdf.drawString(72, y, line)
        y -= 20
    pdf.save()
    return buffer.getvalue()


def test_email_webhook_auto_creates_case_from_new_document():
    reset_state()
    content = make_pdf([
        "Passport",
        "Name: Nora Becker",
        "Passport number: X12345678",
        "Date of birth: 03.04.1991",
        "Germany",
    ])

    response = client.post(
        "/api/webhook/email",
        json={
            "from": "nora.becker@example.com",
            "subject": "Blue Card documents",
            "attachments": [
                {
                    "filename": "passport-nora-becker.pdf",
                    "content_base64": base64.b64encode(content).decode("utf-8"),
                    "content_type": "application/pdf",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["created_cases"]
    cases = client.get("/api/cases", headers=AUTH).json()
    created = next(item for item in cases if item["id"] == payload["created_cases"][0])
    assert created["client_name"] == "Nora Becker"
    assert created["client_email"] == "nora.becker@example.com"
    docs = client.get(f"/api/documents?case_id={created['id']}", headers=AUTH).json()
    assert docs[0]["document_type"] == "passport"
    assert docs[0]["status"] == "assigned"


def test_email_webhook_detects_duplicate_document_by_hash():
    reset_state()
    case = create_case(email="client@example.com")
    content = make_pdf([
        "Passport",
        "Name: Anna Schmidt",
        "Passport number: C12345678",
        "Date of birth: 01.02.1990",
    ])
    payload = {
        "from": "client@example.com",
        "subject": "Documents",
        "attachments": [
            {
                "filename": "passport.pdf",
                "content_base64": base64.b64encode(content).decode("utf-8"),
                "content_type": "application/pdf",
            }
        ],
    }

    first = client.post("/api/webhook/email", json=payload)
    second = client.post("/api/webhook/email", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicates"] == 1
    docs = client.get(f"/api/documents?case_id={case['id']}", headers=AUTH).json()
    assert any(doc["status"] == "duplicate" for doc in docs)


def test_workflow_summary_flags_overdue_and_due_soon_invoices():
    reset_state()
    case = create_case()
    today = datetime.now(timezone.utc).date()
    for invoice_id, due_date in (
        ("inv-overdue", today - timedelta(days=1)),
        ("inv-soon", today + timedelta(days=2)),
    ):
        invoice = {
            "id": invoice_id,
            "case_id": case["id"],
            "number": invoice_id.upper(),
            "status": "unpaid",
            "client_name": case["client_name"],
            "client_email": case["client_email"],
            "issue_date": str(today),
            "due_date": str(due_date),
            "currency": "EUR",
            "items": [{"description": "Service", "quantity": 1, "unit_price": 100}],
            "attachments": [],
        }
        assert client.post("/api/invoices", headers=AUTH, json=invoice).status_code == 200

    response = client.get("/api/workflow/summary", headers=AUTH)

    assert response.status_code == 200
    summary = response.json()
    assert len(summary["invoices"]["overdue"]) == 1
    assert len(summary["invoices"]["due_soon"]) == 1
    assert any(item["label"] == "Overdue invoices" for item in summary["actions"])


def test_public_portal_submit_and_upload_persist_to_case():
    reset_state()
    case = create_case()

    upload_response = client.post(
        f"/api/cases/{case['id']}/upload",
        files={"file": ("portal-passport.pdf", b"fake pdf", "application/pdf")},
    )
    assert upload_response.status_code == 200

    submit_response = client.post(
        f"/api/cases/{case['id']}/public-submit",
        json={
            "client_name": "Anna Schmidt",
            "client_email": "anna@example.com",
            "notes": "Client completed upload",
        },
    )
    assert submit_response.status_code == 200
    saved_case = client.get(f"/api/cases/{case['id']}", headers=AUTH).json()
    assert saved_case["public_notes"] == "Client completed upload"
    docs = client.get(f"/api/documents?case_id={case['id']}", headers=AUTH).json()
    assert len(docs) == 1
    assert docs[0]["source"] == "client"


def test_email_integrations_are_isolated_per_firm():
    reset_state()
    user2_auth = auth_for("user_2")

    assert client.post("/api/email-integrations", headers=AUTH, json={
        "provider": "gmail",
        "email": "first-firm@gmail.com",
        "app_password": "secret-1",
        "imap_host": "imap.gmail.com",
        "mailbox": "INBOX",
        "poll_limit": 10,
        "active": True,
    }).status_code == 200

    assert client.post("/api/email-integrations", headers=user2_auth, json={
        "provider": "gmail",
        "email": "second-firm@gmail.com",
        "app_password": "secret-2",
        "imap_host": "imap.gmail.com",
        "mailbox": "INBOX",
        "poll_limit": 10,
        "active": True,
    }).status_code == 200

    first = client.get("/api/email-integrations", headers=AUTH)
    second = client.get("/api/email-integrations", headers=user2_auth)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(first.json()) == 1
    assert len(second.json()) == 1
    assert first.json()[0]["email"] == "first-firm@gmail.com"
    assert second.json()[0]["email"] == "second-firm@gmail.com"


def test_cases_are_isolated_per_firm_in_memory_mode():
    reset_state()
    user2_auth = auth_for("user_2")
    case1 = create_case(name="Firm One Client", email="one@example.com", auth=AUTH)
    case2 = create_case(name="Firm Two Client", email="two@example.com", auth=user2_auth)

    first = client.get("/api/cases", headers=AUTH)
    second = client.get("/api/cases", headers=user2_auth)

    assert first.status_code == 200
    assert second.status_code == 200
    assert [item["id"] for item in first.json()] == [case1["id"]]
    assert [item["id"] for item in second.json()] == [case2["id"]]


def test_google_oauth_state_roundtrip():
    payload = {"user_id": "user_1", "firm_id": "firm_user_1", "exp": 4102444800}
    state = main.sign_google_state(payload)
    decoded = main.verify_google_state(state)
    assert decoded["user_id"] == "user_1"
    assert decoded["firm_id"] == "firm_user_1"


def test_google_email_integration_start_returns_auth_url():
    reset_state()
    original_client_id = main.GOOGLE_OAUTH_CLIENT_ID
    original_client_secret = main.GOOGLE_OAUTH_CLIENT_SECRET
    try:
        main.GOOGLE_OAUTH_CLIENT_ID = "client-id"
        main.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
        response = client.post("/api/email-integrations/google/start", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert "accounts.google.com" in data["auth_url"]
        assert "state=" in data["auth_url"]
    finally:
        main.GOOGLE_OAUTH_CLIENT_ID = original_client_id
        main.GOOGLE_OAUTH_CLIENT_SECRET = original_client_secret
