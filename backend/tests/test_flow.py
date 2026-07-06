import base64
import asyncio
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
os.environ["LEXFLOW_TEST_AUTH"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from local_ocr import run_local_ocr  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(main.app)
AUTH = {"Authorization": "Bearer test_token_user_1"}


def auth_for(user_id: str):
    return {"Authorization": f"Bearer test_token_{user_id}"}


def reset_state():
    main.memory_cases.clear()
    main.memory_documents.clear()
    main.memory_invoices.clear()
    main.memory_invoice_templates.clear()
    main.memory_evaluations.clear()
    main.memory_profiles.clear()
    main.memory_firms.clear()
    main.memory_email_integrations.clear()
    main.memory_notifications.clear()
    main.app.state.email_poll_debug = {}


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
    assert case["priority"] == "medium"


def test_schema_compat_error_matches_legacy_supabase_messages():
    assert main.is_schema_compat_error("Could not find the 'firm_id' column of 'cases' in the schema cache")
    assert main.is_schema_compat_error("Could not find the table 'public.firms' in the schema cache")
    assert main.is_schema_compat_error("column invoices.updated_at does not exist")


def test_extract_missing_case_columns_parses_supabase_errors():
    assert main.extract_missing_case_columns("Could not find the 'notes' column of 'cases' in the schema cache") == {"notes"}
    assert main.extract_missing_case_columns('column cases.control_state does not exist') == {"control_state"}
    assert main.extract_missing_case_columns('column "route_code" of relation "cases" does not exist') == {"route_code"}


def test_db_update_case_retries_without_missing_columns(monkeypatch):
    reset_state()
    original_use_supabase = main.USE_SUPABASE
    original_client = getattr(main, "supabase_client", None)

    class FakeResult:
        def __init__(self, data):
            self.data = data

    class FakeCasesTable:
        def __init__(self):
            self.payloads = []
            self.current_payload = None

        def update(self, payload):
            self.current_payload = payload
            self.payloads.append(payload)
            return self

        def eq(self, *_args):
            return self

        def execute(self):
            if "notes" in self.current_payload:
                raise Exception("Could not find the 'notes' column of 'cases' in the schema cache")
            return FakeResult([{"id": "case_1", **self.current_payload}])

    class FakeSupabase:
        def __init__(self):
            self.cases = FakeCasesTable()

        def table(self, name):
            assert name == "cases"
            return self.cases

    fake = FakeSupabase()
    monkeypatch.setattr(main, "USE_SUPABASE", True)
    monkeypatch.setattr(main, "supabase_client", fake, raising=False)

    try:
        payload = {"stage": "payment", "notes": "legacy field", "updated_at": "2026-07-05T00:00:00Z"}
        updated = asyncio.run(main.db_update_case("case_1", payload))
    finally:
        monkeypatch.setattr(main, "USE_SUPABASE", original_use_supabase)
        monkeypatch.setattr(main, "supabase_client", original_client, raising=False)

    assert fake.cases.payloads[0]["notes"] == "legacy field"
    assert "notes" not in fake.cases.payloads[1]
    assert updated["stage"] == "payment"
    assert updated["notes"] == "legacy field"


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


def test_mistral_ocr_to_raw_text_uses_page_markdown():
    raw_text, confidence, pages = main.mistral_ocr_to_raw_text({
        "pages": [
            {
                "index": 0,
                "markdown": "Passport\nName: Anna Schmidt\nPassport number: C12345678",
                "confidence_scores": {"average_page_confidence_score": 0.91},
            },
            {
                "index": 1,
                "markdown": "Date of birth: 01.02.1990",
                "confidence_scores": {"average_page_confidence_score": 0.87},
            },
        ]
    })

    assert "Passport number: C12345678" in raw_text
    assert "Date of birth: 01.02.1990" in raw_text
    assert confidence > 0.88
    assert pages[0]["method"] == "mistral_markdown"


def test_case_upload_returns_extracted_fields_and_updated_case(monkeypatch):
    reset_state()
    case = create_case()

    async def fake_run_ocr(*_args, **_kwargs):
        return {"raw_text": "Passport\nName: Anna Schmidt\nPassport number: C12345678\nDate of birth: 01.02.1990", "provider": "mistral", "confidence": 0.93, "pages": []}

    monkeypatch.setattr(main, "run_ocr", fake_run_ocr)

    response = client.post(
        f"/api/cases/{case['id']}/upload",
        headers=AUTH,
        files={"file": ("passport.png", b"fake image", "image/png")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["extracted"]["passport_number"] == "C12345678"
    assert payload["case"]["extracted"]["passport_number"] == "C12345678"


def test_case_parse_merges_fields_from_multiple_documents(monkeypatch):
    reset_state()
    case = create_case(name="Roman", email="roman@example.com")

    main.memory_documents["doc-1"] = {
        "id": "doc-1",
        "case_id": case["id"],
        "name": "permit-front.png",
        "key": "case/permit-front.png",
        "status": "assigned",
        "source": "email",
    }
    main.memory_documents["doc-2"] = {
        "id": "doc-2",
        "case_id": case["id"],
        "name": "passport.png",
        "key": "case/passport.png",
        "status": "assigned",
        "source": "email",
    }

    class FakeBody:
        def __init__(self, value):
            self.value = value
        def read(self):
            return self.value

    class FakeR2:
        def get_object(self, Bucket=None, Key=None):
            return {"Body": FakeBody(Key.encode("utf-8"))}

    async def fake_run_ocr(content, filename):
        if filename == "permit-front.png":
            return {"raw_text": "Residence permit\nName: Roman Test\nDate of birth: 01.02.1990", "provider": "mistral", "confidence": 0.9}
        return {"raw_text": "Passport\nPassport number: C12345678\nNationality: Russian", "provider": "mistral", "confidence": 0.92}

    original_use_r2 = main.USE_R2
    original_r2 = main.r2_client
    monkeypatch.setattr(main, "run_ocr", fake_run_ocr)
    try:
        main.USE_R2 = True
        main.r2_client = FakeR2()
        response = client.post(f"/api/cases/{case['id']}/parse", headers=AUTH, json={})
    finally:
        main.USE_R2 = original_use_r2
        main.r2_client = original_r2

    assert response.status_code == 200
    payload = response.json()
    assert payload["extracted"]["full_name"] == "Roman Test"
    assert payload["extracted"]["passport_number"] == "C12345678"
    assert payload["parsed_documents"][0]["score"] >= payload["parsed_documents"][1]["score"]


def test_case_patch_succeeds_even_if_control_refresh_fails(monkeypatch):
    reset_state()
    case = create_case()

    async def broken_refresh(*args, **kwargs):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(main, "refresh_case_control", broken_refresh)

    response = client.patch(
        f"/api/cases/{case['id']}",
        headers=AUTH,
        json={"stage": "payment", "notes": "Updated from UI"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stage"] == "payment"
    assert payload["notes"] == "Updated from UI"


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


def test_portal_invite_send_demo_mode():
    reset_state()
    case = create_case()

    send_response = client.post(
        f"/api/cases/{case['id']}/send-portal-invite",
        headers=AUTH,
        json={"message": "Please upload the missing passport and contract."},
    )

    assert send_response.status_code == 200
    payload = send_response.json()
    assert payload["to"] == case["client_email"]
    assert payload["sent"] is False
    assert payload["status"] == "queued_demo"
    assert payload["portal_url"].endswith(f"/client-upload.html?id={case['id']}")
    assert "Open secure upload page" in payload["preview_html"]


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


def test_gmail_attachment_extractor_keeps_inline_image_without_filename():
    reset_state()
    message = main.EmailMessage()
    message["From"] = "Client <client@example.com>"
    message["Subject"] = "VNZH photo"
    message.set_content("Attached.")
    message.add_related(b"image-bytes", maintype="image", subtype="jpeg", cid="vnzh-photo")

    attachments = main.extract_gmail_attachments(message)

    assert len(attachments) == 1
    assert attachments[0].filename.endswith(".jpg")
    assert attachments[0].content_type == "image/jpeg"


def test_process_email_payload_ignores_non_document_assets(monkeypatch):
    reset_state()

    async def fake_route_incoming_document(**kwargs):
        return {
            "document": {"name": kwargs["filename"], "status": "assigned"},
            "case": {"id": "case-1"},
            "auto_created_case": False,
            "duplicate": False,
        }

    monkeypatch.setattr(main, "route_incoming_document", fake_route_incoming_document)

    payload = main.EmailWebhook(
        **{
            "from": "client@example.com",
            "subject": "Passport and documents",
            "attachments": [
                {"filename": "passport.pdf", "content_base64": base64.b64encode(b"pdf").decode("utf-8"), "content_type": "application/pdf"},
                {"filename": "logo.png", "content_base64": base64.b64encode(b"png").decode("utf-8"), "content_type": "image/png"},
            ],
        }
    )

    result = client.post("/api/webhook/email", json=payload.model_dump(by_alias=True))

    assert result.status_code == 200
    body = result.json()
    assert body["attachments_processed"] == 2
    assert body["attachments_accepted"] == 1
    assert body["attachments_ignored"] == 1
    assert body["ignored"][0]["filename"] == "logo.png"


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


def email_attachment(filename, content, content_type="application/pdf"):
    return {
        "filename": filename,
        "content_base64": base64.b64encode(content).decode("utf-8"),
        "content_type": content_type,
    }


def post_email_payload(sender, subject, attachments):
    return client.post(
        "/api/webhook/email",
        json={
            "from": sender,
            "subject": subject,
            "attachments": attachments,
        },
    )


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


def test_email_webhook_matches_follow_up_document_to_auto_created_case():
    reset_state()
    passport = make_pdf([
        "Passport",
        "Name: Mila Petrova",
        "Passport number: Y76543210",
        "Date of birth: 12.09.1993",
        "Nationality: Russian",
    ])
    contract = make_pdf([
        "Employment contract",
        "Name: Mila Petrova",
        "Passport number: Y76543210",
        "Date of birth: 12.09.1993",
        "Employer: Nordlicht GmbH",
    ])

    first = post_email_payload(
        "mila.petrova@example.com",
        "Initial VNZH package",
        [email_attachment("passport-mila-petrova.pdf", passport)],
    )
    assert first.status_code == 200
    created_case_id = first.json()["created_cases"][0]

    second = post_email_payload(
        "assistant@relocation-partner.com",
        "Follow-up contract for Mila Petrova",
        [email_attachment("employment-contract-mila-petrova.pdf", contract)],
    )

    assert second.status_code == 200
    payload = second.json()
    assert payload["created_cases"] == []
    assert payload["matched_cases"] == [created_case_id]
    assert payload["decisions"][0]["action"] == "attach_to_existing_case"
    assert payload["decisions"][0]["match_reason"] == "passport,dob,name"

    docs = client.get(f"/api/documents?case_id={created_case_id}", headers=AUTH)
    assert docs.status_code == 200
    names = {item["name"] for item in docs.json()}
    assert "passport-mila-petrova.pdf" in names
    assert "employment-contract-mila-petrova.pdf" in names


def test_email_webhook_matches_existing_case_by_identity_not_sender():
    reset_state()
    case = create_case(name="Ivan Kuznetsov", email="ivan.personal@example.com")

    passport_upload = client.post(
        f"/api/cases/{case['id']}/upload",
        headers=AUTH,
        files={
            "file": (
                "passport-ivan-kuznetsov.pdf",
                make_pdf([
                    "Passport",
                    "Name: Ivan Kuznetsov",
                    "Passport number: P99887766",
                    "Date of birth: 21.11.1988",
                ]),
                "application/pdf",
            )
        },
    )
    assert passport_upload.status_code == 200

    insurance = make_pdf([
        "Health insurance certificate",
        "Name: Ivan Kuznetsov",
        "Passport number: P99887766",
        "Date of birth: 21.11.1988",
        "Insurer: TK",
    ])

    response = post_email_payload(
        "case.worker@agency.example",
        "Insurance for Ivan Kuznetsov",
        [email_attachment("health-insurance-ivan-kuznetsov.pdf", insurance)],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["created_cases"] == []
    assert payload["matched_cases"] == [case["id"]]
    assert payload["decisions"][0]["match_score"] >= 0.9
    assert payload["documents"][0]["case_id"] == case["id"]
    assert payload["documents"][0]["status"] == "assigned"


def test_email_webhook_keeps_weak_unknown_document_for_review_without_case_creation():
    reset_state()
    weak_scan = make_pdf([
        "Scan copy",
        "Please see attached",
        "No clear identity data here",
    ])

    response = post_email_payload(
        "unknown.sender@example.com",
        "Document",
        [email_attachment("scan-001.pdf", weak_scan)],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["created_cases"] == []
    assert payload["matched_cases"] == []
    assert payload["decisions"][0]["action"] == "hold_for_manual_review"
    assert payload["documents"][0]["status"] == "unrecognized"

    cases = client.get("/api/cases", headers=AUTH)
    assert cases.status_code == 200
    assert cases.json() == []


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
    assert len(docs) == 1
    assert docs[0]["status"] != "duplicate"


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


def test_case_control_center_auto_moves_to_payment_when_documents_are_complete_but_invoice_is_open():
    reset_state()
    case = create_case()

    for filename, body in (
        ("passport-anna-schmidt.pdf", make_pdf(["Passport", "Name: Anna Schmidt", "Passport number: C12345678", "Date of birth: 01.02.1990"])),
        ("employment-contract.pdf", make_pdf(["Employment contract", "Name: Anna Schmidt", "Employer: Demo GmbH"])),
        ("degree-diploma.pdf", make_pdf(["Diploma", "Name: Anna Schmidt"])),
        ("health-insurance.pdf", make_pdf(["Health insurance certificate", "Anna Schmidt"])),
    ):
        response = client.post(
            f"/api/cases/{case['id']}/upload",
            headers=AUTH,
            files={"file": (filename, body, "application/pdf")},
        )
        assert response.status_code == 200

    client.post("/api/invoices", headers=AUTH, json={
        "id": "inv-open",
        "case_id": case["id"],
        "number": "INV-OPEN",
        "status": "unpaid",
        "client_name": case["client_name"],
        "client_email": case["client_email"],
        "issue_date": "2026-07-05",
        "due_date": "2026-07-12",
        "currency": "EUR",
        "items": [{"description": "Service", "quantity": 1, "unit_price": 100}],
        "attachments": [],
    })

    center = client.get(f"/api/cases/{case['id']}/control-center", headers=AUTH)
    assert center.status_code == 200
    payload = center.json()
    assert payload["control_state"]["route_code"] == "DE_BLUE_CARD"
    assert payload["control_state"]["blocking_missing_codes"] == []
    assert payload["control_state"]["auto_stage"] == "payment"
    assert payload["case"]["stage"] == "payment"


def test_case_control_center_moves_to_processing_when_invoice_is_signed():
    reset_state()
    case = create_case()

    for filename, body in (
        ("passport-anna-schmidt.pdf", make_pdf(["Passport", "Name: Anna Schmidt", "Passport number: C12345678", "Date of birth: 01.02.1990"])),
        ("employment-contract.pdf", make_pdf(["Employment contract", "Name: Anna Schmidt", "Employer: Demo GmbH"])),
        ("degree-diploma.pdf", make_pdf(["Diploma", "Name: Anna Schmidt"])),
        ("health-insurance.pdf", make_pdf(["Health insurance certificate", "Anna Schmidt"])),
    ):
        assert client.post(
            f"/api/cases/{case['id']}/upload",
            headers=AUTH,
            files={"file": (filename, body, "application/pdf")},
        ).status_code == 200

    assert client.post("/api/invoices", headers=AUTH, json={
        "id": "inv-signed",
        "case_id": case["id"],
        "number": "INV-SIGNED",
        "status": "signed",
        "client_name": case["client_name"],
        "client_email": case["client_email"],
        "issue_date": "2026-07-05",
        "due_date": "2026-07-12",
        "currency": "EUR",
        "items": [{"description": "Service", "quantity": 1, "unit_price": 100}],
        "attachments": [],
    }).status_code == 200

    center = client.get(f"/api/cases/{case['id']}/control-center", headers=AUTH)
    assert center.status_code == 200
    payload = center.json()
    assert payload["control_state"]["billing_complete"] is True
    assert payload["control_state"]["auto_priority"] == "low"
    assert payload["case"]["stage"] == "processing"
    assert payload["case"]["priority"] == "low"


def test_case_control_center_raises_priority_for_review_blocker():
    reset_state()
    case = create_case()

    upload = client.post(
        f"/api/cases/{case['id']}/upload",
        headers=AUTH,
        files={"file": ("passport-anna-schmidt.pdf", make_pdf(["Passport", "Name: Anna Schmidt", "Passport number: C12345678"]), "application/pdf")},
    )
    assert upload.status_code == 200
    document_id = upload.json()["document_id"]

    patched = client.patch(
        f"/api/documents/{document_id}",
        headers=AUTH,
        json={"manual_review_required": True},
    )
    assert patched.status_code == 200

    center = client.get(f"/api/cases/{case['id']}/control-center", headers=AUTH)
    assert center.status_code == 200
    payload = center.json()
    assert payload["control_state"]["auto_priority"] == "high"
    assert payload["case"]["priority"] == "high"


def test_notifications_endpoint_returns_protocol_alerts():
    reset_state()
    case = create_case()

    assert client.post(
        f"/api/cases/{case['id']}/upload",
        headers=AUTH,
        files={"file": ("passport-anna-schmidt.pdf", make_pdf(["Passport", "Name: Anna Schmidt", "Passport number: C12345678", "Date of birth: 01.02.1990"]), "application/pdf")},
    ).status_code == 200

    notifications = client.get("/api/notifications", headers=AUTH)
    assert notifications.status_code == 200
    assert any(item["kind"] == "action_required" for item in notifications.json())


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


def test_zoho_email_integration_start_requests_update_scope():
    reset_state()
    original_client_id = main.ZOHO_OAUTH_CLIENT_ID
    original_client_secret = main.ZOHO_OAUTH_CLIENT_SECRET
    try:
        main.ZOHO_OAUTH_CLIENT_ID = "zoho-client-id"
        main.ZOHO_OAUTH_CLIENT_SECRET = "zoho-client-secret"
        response = client.post("/api/email-integrations/zoho/start", headers=AUTH)
        assert response.status_code == 200
        auth_url = response.json()["auth_url"]
        assert "accounts.zoho.com" in auth_url
        assert "ZohoMail.messages.UPDATE" in auth_url
    finally:
        main.ZOHO_OAUTH_CLIENT_ID = original_client_id
        main.ZOHO_OAUTH_CLIENT_SECRET = original_client_secret


def test_is_zoho_message_unread_treats_status_one_as_unread():
    assert main.is_zoho_message_unread({"status": "1"}) is True
    assert main.is_zoho_message_unread({"status": "0"}) is False


def test_process_email_payload_ignores_system_zoho_messages():
    reset_state()
    payload = main.EmailWebhook(
        **{
            "from": "welcome@zoho.com",
            "subject": "Welcome aboard! Your new inbox is here",
            "attachments": [
                main.EmailAttachment(filename="welcome.png", content_base64=base64.b64encode(b"abc").decode("utf-8"), content_type="image/png")
            ],
        }
    )

    result = asyncio.run(main.process_email_payload(payload))

    assert result["attachments_accepted"] == 0
    assert result["attachments_ignored"] == 1
    assert result["ignored"][0]["reason"] == "ignored_system_sender"


def test_process_email_payload_deduplicates_same_attachment_in_one_message(monkeypatch):
    reset_state()

    async def fake_route(**kwargs):
        return {
            "case": None,
            "document": {"id": "doc-1", "name": kwargs["filename"]},
            "duplicate": False,
            "auto_created_case": False,
            "decision": {},
        }

    monkeypatch.setattr(main, "route_incoming_document", fake_route)
    encoded = base64.b64encode(b"same-file").decode("utf-8")
    payload = main.EmailWebhook(
        **{
            "from": "client@example.com",
            "subject": "Residence permit",
            "attachments": [
                main.EmailAttachment(filename="IMG_1203.png", content_base64=encoded, content_type="image/png"),
                main.EmailAttachment(filename="IMG_1203-copy.png", content_base64=encoded, content_type="image/png"),
            ],
        }
    )

    result = asyncio.run(main.process_email_payload(payload))

    assert result["attachments_processed"] == 2
    assert result["attachments_accepted"] == 1
    assert result["attachments_ignored"] == 1
    assert result["ignored"][0]["reason"] == "duplicate_attachment_in_same_message"


def test_route_incoming_document_returns_existing_duplicate_without_new_upload(monkeypatch):
    reset_state()
    existing = {
        "id": "doc-existing",
        "case_id": "case-1",
        "name": "IMG_1203.png",
        "document_type": "unknown",
        "extracted": {"document_type": "unknown", "confidence": 0.1},
        "status": "duplicate",
        "automation_note": "duplicate_of:doc-root",
    }
    root = {
        "id": "doc-root",
        "case_id": "case-1",
        "name": "IMG_1203.png",
        "document_type": "unknown",
        "extracted": {"document_type": "unknown", "confidence": 0.1},
        "status": "assigned",
    }

    async def fake_find(_content_hash):
        return existing

    async def fake_get_case(case_id):
        return {"id": case_id, "client_name": "Roman", "firm_id": "firm-1"}

    async def fake_get_document(document_id):
        return root if document_id == "doc-root" else None

    async def fail_upload(*_args, **_kwargs):
        raise AssertionError("upload_bytes should not be called for duplicates")

    monkeypatch.setattr(main, "db_find_document_by_hash", fake_find)
    monkeypatch.setattr(main, "db_get_case", fake_get_case)
    monkeypatch.setattr(main, "db_get_document", fake_get_document)
    monkeypatch.setattr(main, "upload_bytes", fail_upload)

    result = asyncio.run(
        main.route_incoming_document(
            filename="IMG_1203.png",
            content=b"same-file",
            content_type="image/png",
            source="email",
            sender_email="client@example.com",
            subject="Residence permit",
            user_id="user_1",
        )
    )

    assert result["duplicate"] is True
    assert result["document"]["id"] == "doc-root"
    assert result["document"]["status"] == "duplicate"


def test_route_incoming_document_replaces_stale_duplicate_with_new_canonical_doc(monkeypatch):
    reset_state()
    existing = {
        "id": "doc-duplicate",
        "case_id": "case-1",
        "name": "IMG_1203.png",
        "document_type": "unknown",
        "extracted": {"document_type": "unknown", "confidence": 0.1},
        "status": "duplicate",
        "automation_note": "duplicate_of:doc-root",
    }

    async def fake_find(_content_hash):
        return existing

    async def fake_get_doc(document_id):
        return None

    async def fake_get_cases():
        return []

    async def fake_upload(case_id, filename, content_type, content, source="email"):
        return {
            "name": filename,
            "key": f"{case_id}/file.png",
            "url": "https://example.com/file.png",
            "content_type": content_type,
            "size": len(content),
        }

    async def fake_create_document(document):
        return document

    async def fake_run_ocr(*_args, **_kwargs):
        return {"raw_text": "", "provider": "none", "confidence": 0}

    monkeypatch.setattr(main, "db_find_document_by_hash", fake_find)
    monkeypatch.setattr(main, "db_get_document", fake_get_doc)
    monkeypatch.setattr(main, "db_get_cases", fake_get_cases)
    monkeypatch.setattr(main, "upload_bytes", fake_upload)
    monkeypatch.setattr(main, "db_create_document", fake_create_document)
    monkeypatch.setattr(main, "run_ocr", fake_run_ocr)
    monkeypatch.setattr(main, "parse_document_text", lambda *_args, **_kwargs: {"document_type": "unknown", "confidence": 0.1, "missing_fields": []})

    result = asyncio.run(
        main.route_incoming_document(
            filename="IMG_1203.png",
            content=b"same-file",
            content_type="image/png",
            source="email",
            sender_email="new@example.com",
            subject="Residence permit",
            user_id="user_1",
        )
    )

    assert result["duplicate"] is False
    assert result["document"]["status"] == "unrecognized"


def test_db_get_email_integrations_falls_back_to_r2(monkeypatch):
    reset_state()
    original_use_r2 = main.USE_R2
    original_client = main.r2_client

    class FakeBody:
        def read(self):
            return b'[{"id":"r2-1","email":"firm@zohomail.com","provider":"zoho","auth_type":"oauth","active":true,"firm_id":"firm-1","created_at":"2026-07-07T00:00:00+00:00"}]'

    class FakeR2:
        def get_object(self, **_kwargs):
            return {"Body": FakeBody()}

    try:
        main.USE_R2 = True
        main.r2_client = FakeR2()
        rows = asyncio.run(main.db_get_email_integrations())
        assert len(rows) == 1
        assert rows[0]["id"] == "r2-1"
        assert main.memory_email_integrations["r2-1"]["email"] == "firm@zohomail.com"
    finally:
        main.USE_R2 = original_use_r2
        main.r2_client = original_client


def test_poll_gmail_uses_oauth_integration_even_without_active_flag(monkeypatch):
    reset_state()
    actor = asyncio.run(main.ensure_actor_context({"id": "user_1", "email": "user_1@example.com", "name": "User One"}))
    main.memory_email_integrations["oauth-1"] = {
        "id": "oauth-1",
        "provider": "gmail",
        "auth_type": "oauth",
        "email": "firm@gmail.com",
        "app_password": "",
        "access_token": "token",
        "refresh_token": "refresh",
        "token_expires_at": None,
        "imap_host": "imap.gmail.com",
        "mailbox": "INBOX",
        "poll_limit": 10,
        "active": False,
        "lawyer_id": "user_1",
        "firm_id": actor["firm"]["id"],
        "created_at": main.utc_now(),
        "updated_at": main.utc_now(),
        "last_polled_at": None,
        "last_processed_message_id": "",
    }

    async def fake_process(integration):
        return {"integration_id": integration["id"], "email": integration["email"], "processed": [], "count": 0}

    monkeypatch.setattr(main, "process_email_integration", fake_process)

    response = client.post("/api/gmail/poll", headers=AUTH)

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 0
    assert payload["runs"][0]["integration_id"] == "oauth-1"


def test_email_integrations_debug_reports_last_poll_error(monkeypatch):
    reset_state()
    actor = asyncio.run(main.ensure_actor_context({"id": "user_1", "email": "user_1@example.com", "name": "User One"}))
    main.memory_email_integrations["oauth-zoho"] = {
        "id": "oauth-zoho",
        "provider": "zoho",
        "auth_type": "oauth",
        "email": "firm@zohomail.com",
        "app_password": "",
        "access_token": "token",
        "refresh_token": "refresh",
        "account_id": "123",
        "token_expires_at": None,
        "imap_host": "imap.zoho.com",
        "mailbox": "INBOX",
        "poll_limit": 10,
        "active": True,
        "lawyer_id": "user_1",
        "firm_id": actor["firm"]["id"],
        "created_at": main.utc_now(),
        "updated_at": main.utc_now(),
        "last_polled_at": None,
        "last_processed_message_id": "",
    }

    async def broken_process(_integration):
        raise main.HTTPException(status_code=502, detail="Zoho Mail API request failed: HTTP 401 invalid token")

    monkeypatch.setattr(main, "process_email_integration", broken_process)

    response = client.post("/api/gmail/poll", headers=AUTH)
    assert response.status_code == 502

    debug = client.get("/api/email-integrations/debug", headers=AUTH)
    assert debug.status_code == 200
    payload = debug.json()
    assert payload["integrations"][0]["poll_debug"]["status"] == "error"
    assert "invalid token" in payload["integrations"][0]["poll_debug"]["last_error"]


def test_pick_runtime_email_integrations_prefers_oauth_per_email():
    rows = [
        {
            "id": "manual-1",
            "provider": "gmail",
            "auth_type": "app_password",
            "email": "firm@gmail.com",
            "app_password": "secret",
            "active": True,
            "updated_at": "2026-07-05T10:00:00Z",
        },
        {
            "id": "oauth-1",
            "provider": "gmail",
            "auth_type": "oauth",
            "email": "firm@gmail.com",
            "refresh_token": "refresh",
            "access_token": "",
            "active": True,
            "updated_at": "2026-07-05T09:00:00Z",
        },
    ]

    picked = main.pick_runtime_email_integrations(rows)

    assert len(picked) == 1
    assert picked[0]["id"] == "oauth-1"


def test_pick_runtime_email_integrations_by_workspace_keeps_one_per_firm():
    rows = [
        {
            "id": "firm-1-oauth",
            "provider": "zoho",
            "auth_type": "oauth",
            "email": "firm1@zohomail.com",
            "refresh_token": "refresh-1",
            "active": True,
            "firm_id": "firm-1",
            "updated_at": "2026-07-06T10:00:00Z",
        },
        {
            "id": "firm-1-manual",
            "provider": "gmail",
            "auth_type": "app_password",
            "email": "firm1@zohomail.com",
            "app_password": "secret",
            "active": True,
            "firm_id": "firm-1",
            "updated_at": "2026-07-06T09:00:00Z",
        },
        {
            "id": "firm-2-oauth",
            "provider": "gmail",
            "auth_type": "oauth",
            "email": "firm2@gmail.com",
            "refresh_token": "refresh-2",
            "active": True,
            "firm_id": "firm-2",
            "updated_at": "2026-07-06T08:00:00Z",
        },
    ]

    picked = main.pick_runtime_email_integrations_by_workspace(rows)

    assert len(picked) == 2
    assert {item["id"] for item in picked} == {"firm-1-oauth", "firm-2-oauth"}


def test_run_email_poll_for_integrations_collects_errors_when_requested(monkeypatch):
    rows = [
        {"id": "ok-1", "email": "ok@example.com"},
        {"id": "bad-1", "email": "bad@example.com"},
    ]

    async def fake_process(integration):
        if integration["id"] == "bad-1":
            raise RuntimeError("boom")
        return {"integration_id": integration["id"], "email": integration["email"], "processed": [], "count": 2}

    monkeypatch.setattr(main, "process_email_integration", fake_process)

    result = asyncio.run(main.run_email_poll_for_integrations(rows, continue_on_error=True))

    assert result["count"] == 2
    assert len(result["runs"]) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["integration_id"] == "bad-1"


def test_poll_all_active_email_integrations_once_reports_no_integrations():
    reset_state()

    result = asyncio.run(main.poll_all_active_email_integrations_once())

    assert result["count"] == 0
    assert result["has_integrations"] is False
