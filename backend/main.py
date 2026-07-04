import base64
import email
import imaplib
import io
import json
import os
import re
import smtplib
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage, Message
from email.utils import parseaddr
from typing import Optional
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ocr_parsers import evaluate_extraction, parse_document_text

app = FastAPI(title="LexFlow Backend", version="0.2.0")

# ─── Config ─────────────────────────────────────────────
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8001")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "lexflow-documents")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "billing@lexflow.eu")
GMAIL_IMAP_HOST = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
GMAIL_EMAIL = os.environ.get("GMAIL_EMAIL", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_MAILBOX = os.environ.get("GMAIL_MAILBOX", "INBOX")
GMAIL_POLL_LIMIT = int(os.environ.get("GMAIL_POLL_LIMIT", "10") or "10")

USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
USE_R2 = bool(R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY)
USE_MISTRAL = bool(MISTRAL_API_KEY)
USE_SMTP = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)
USE_GMAIL = bool(GMAIL_EMAIL and GMAIL_APP_PASSWORD)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:8001", "http://127.0.0.1:8001", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Optional clients ───────────────────────────────────
supabase_client = None
if USE_SUPABASE:
    try:
        from supabase import create_client
        supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    except Exception as e:
        print(f"Supabase init failed: {e}")

r2_client = None
if USE_R2:
    try:
        import boto3
        r2_client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
    except Exception as e:
        print(f"R2 init failed: {e}")

# ─── In-memory fallback ─────────────────────────────────
users = {
    "demo@lexflow.eu": {
        "id": "user_1",
        "email": "demo@lexflow.eu",
        "name": "Demo Lawyer",
        "password": "demo",
    }
}
memory_cases: dict[str, dict] = {}
memory_invoices: dict[str, dict] = {}
memory_documents: dict[str, dict] = {}
memory_invoice_templates: dict[str, dict] = {}
memory_evaluations: dict[str, dict] = {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ─── Auth ───────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str


async def verify_token(token: str) -> Optional[dict]:
    if token.startswith("demo_token_"):
        return {"id": "user_1", "email": "demo@lexflow.eu", "role": "lawyer"}
    if not USE_SUPABASE:
        return None
    try:
        res = supabase_client.auth.get_user(token)
        return {"id": res.user.id, "email": res.user.email, "role": "lawyer"}
    except Exception as e:
        print(f"Token verify failed: {e}")
        return None


async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization[len("Bearer "):]
    user = await verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


# ─── Database helpers ───────────────────────────────────
async def db_create_case(data: dict) -> dict:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("cases").insert(data).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase insert failed: {e}")
    memory_cases[data["id"]] = data
    return data


async def db_get_cases() -> list:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("cases").select("*").order("created_at", desc=True).execute()
            return res.data
        except Exception as e:
            print(f"Supabase select failed: {e}")
    return sorted(memory_cases.values(), key=lambda c: c.get("created_at", ""), reverse=True)


async def db_get_case(case_id: str) -> Optional[dict]:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("cases").select("*").eq("id", case_id).single().execute()
            return res.data
        except Exception as e:
            print(f"Supabase get failed: {e}")
    return memory_cases.get(case_id)


async def db_update_case(case_id: str, patch: dict) -> Optional[dict]:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("cases").update(patch).eq("id", case_id).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase update failed: {e}")
    case = memory_cases.get(case_id)
    if case:
        case.update(patch)
        case["updated_at"] = utc_now()
    return case


async def db_create_document(data: dict) -> dict:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("documents").insert(data).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase document insert failed: {e}")
    memory_documents[data["id"]] = data
    return data


async def db_get_documents(status: Optional[str] = None, case_id: Optional[str] = None) -> list:
    if USE_SUPABASE:
        try:
            query = supabase_client.table("documents").select("*").order("uploaded_at", desc=True)
            if status:
                query = query.eq("status", status)
            if case_id:
                query = query.eq("case_id", case_id)
            res = query.execute()
            return res.data
        except Exception as e:
            print(f"Supabase document select failed: {e}")
    docs = list(memory_documents.values())
    if status:
        docs = [item for item in docs if item.get("status") == status]
    if case_id:
        docs = [item for item in docs if item.get("case_id") == case_id]
    return sorted(docs, key=lambda item: item.get("uploaded_at", ""), reverse=True)


async def db_update_document(document_id: str, patch: dict) -> Optional[dict]:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("documents").update(patch).eq("id", document_id).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Supabase document update failed: {e}")
    doc = memory_documents.get(document_id)
    if doc:
        doc.update(patch)
        doc["updated_at"] = utc_now()
    return doc


async def db_get_document(document_id: str) -> Optional[dict]:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("documents").select("*").eq("id", document_id).single().execute()
            return res.data
        except Exception as e:
            print(f"Supabase document get failed: {e}")
    return memory_documents.get(document_id)


async def db_delete_document(document_id: str) -> Optional[dict]:
    doc = await db_get_document(document_id)
    if not doc:
        return None
    if USE_SUPABASE:
        try:
            supabase_client.table("documents").delete().eq("id", document_id).execute()
        except Exception as e:
            print(f"Supabase document delete failed: {e}")
    memory_documents.pop(document_id, None)
    return doc


async def db_upsert_invoice(data: dict) -> dict:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("invoices").upsert(data).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase invoice upsert failed: {e}")
    memory_invoices[data["id"]] = data
    return data


async def db_get_invoices(case_id: Optional[str] = None) -> list:
    if USE_SUPABASE:
        try:
            query = supabase_client.table("invoices").select("*").order("updated_at", desc=True)
            if case_id:
                query = query.eq("case_id", case_id)
            res = query.execute()
            return res.data
        except Exception as e:
            print(f"Supabase invoice select failed: {e}")
    invoices = list(memory_invoices.values())
    if case_id:
        invoices = [item for item in invoices if item.get("case_id") == case_id]
    return sorted(invoices, key=lambda item: item.get("updated_at", item.get("created_at", "")), reverse=True)


async def db_get_invoice(invoice_id: str) -> Optional[dict]:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("invoices").select("*").eq("id", invoice_id).single().execute()
            return res.data
        except Exception as e:
            print(f"Supabase invoice get failed: {e}")
    return memory_invoices.get(invoice_id)


async def db_create_evaluation(data: dict) -> dict:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("ml_evaluations").insert(data).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase evaluation insert failed: {e}")
    memory_evaluations[data["id"]] = data
    return data


async def db_get_evaluations(case_id: Optional[str] = None) -> list:
    if USE_SUPABASE:
        try:
            query = supabase_client.table("ml_evaluations").select("*").order("created_at", desc=True)
            if case_id:
                query = query.eq("case_id", case_id)
            res = query.execute()
            return res.data
        except Exception as e:
            print(f"Supabase evaluation select failed: {e}")
    rows = list(memory_evaluations.values())
    if case_id:
        rows = [item for item in rows if item.get("case_id") == case_id]
    return sorted(rows, key=lambda item: item.get("created_at", ""), reverse=True)


# ─── File storage helpers ───────────────────────────────
async def upload_file(case_id: str, file: UploadFile, source: str = "portal") -> dict:
    ext = Path(file.filename or "document.pdf").suffix
    key = f"{case_id}/{uuid.uuid4().hex}{ext}"
    content = await file.read()

    if USE_R2:
        try:
            r2_client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=content, ContentType=file.content_type or "application/octet-stream")
            url = f"{R2_PUBLIC_URL}/{key}" if R2_PUBLIC_URL else f"{R2_ENDPOINT}/{R2_BUCKET_NAME}/{key}"
            return {"key": key, "name": file.filename, "url": url, "source": source, "status": "uploaded", "size": len(content), "content_type": file.content_type or "application/octet-stream"}
        except Exception as e:
            print(f"R2 upload failed: {e}")
    return {"key": key, "name": file.filename, "url": "", "source": source, "status": "uploaded", "size": len(content), "content_type": file.content_type or "application/octet-stream"}


def delete_r2_object(key: str):
    if USE_R2 and key:
        try:
            r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
        except Exception as e:
            print(f"R2 delete failed: {e}")


def send_email_message(to_email: str, subject: str, body: str) -> dict:
    if not USE_SMTP:
        return {
            "sent": False,
            "status": "queued_demo",
            "reason": "SMTP is not configured",
        }
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        return {"sent": True, "status": "sent"}
    except Exception as e:
        print(f"SMTP send failed: {e}")
        raise HTTPException(status_code=502, detail="Invoice email failed")


# ─── OCR helpers ────────────────────────────────────────
async def run_ocr(content: bytes, filename: str) -> dict:
    if not USE_MISTRAL:
        return {
            "raw_text": "DEMO OCR TEXT\nName: Anna Schmidt\nPassport: D1234567\nDOB: 1990-01-01\nEmployer: Demo GmbH",
            "pages": [],
        }
    try:
        encoded = base64.b64encode(content).decode("utf-8")
        mime = "application/pdf" if filename.lower().endswith(".pdf") else "image/jpeg"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.mistral.ai/v1/ocr",
                headers={"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "mistral-ocr-latest",
                    "document": {
                        "type": "document_base64",
                        "document_base64": encoded,
                        "document_name": filename,
                    },
                },
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        print(f"Mistral OCR failed: {e}")
        return {"raw_text": "", "pages": [], "error": str(e)}


async def store_extraction_evaluation(case_id: str, document_id: str, fields: dict) -> dict:
    evaluation = evaluate_extraction(fields)
    row = {
        "id": str(uuid.uuid4()),
        "case_id": case_id,
        "document_id": document_id,
        "model": "rules+mistral-ocr",
        "score": evaluation["score"],
        "passed": evaluation["passed"],
        "suggestions": evaluation["suggestions"],
        "payload": {"fields": fields, "evaluation": evaluation},
        "created_at": utc_now(),
    }
    await db_create_evaluation(row)
    return row


# ─── PDF mapping helpers ────────────────────────────────
def fill_pdf_form(fields: dict) -> io.BytesIO:
    """Fill a simple PDF form. In production, use the official government PDF."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, 800, "EU Immigration Application Form")
    c.setFont("Helvetica", 12)
    y = 750
    for key, value in fields.items():
        c.drawString(50, y, f"{key}: {value}")
        y -= 25
    c.save()
    buf.seek(0)
    return buf


# ─── Endpoints ──────────────────────────────────────────
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "lexflow",
        "version": "0.2.0",
        "supabase": USE_SUPABASE,
        "r2": USE_R2,
        "mistral": USE_MISTRAL,
        "smtp": USE_SMTP,
        "gmail": USE_GMAIL,
    }


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    if USE_SUPABASE:
        try:
            res = supabase_client.auth.sign_in_with_password({"email": req.email, "password": req.password})
            return {
                "token": res.session.access_token,
                "user": {"id": res.user.id, "email": res.user.email, "name": res.user.email},
            }
        except Exception as e:
            print(f"Supabase login failed: {e}")
    user = users.get(req.email)
    if not user or user["password"] != req.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {
        "token": f"demo_token_{user['id']}",
        "user": {"id": user["id"], "email": user["email"], "name": user["name"]},
    }


@app.get("/api/me")
async def me(user: dict = Depends(get_current_user)):
    return user


# ─── Cases ─────────────────────────────────────────────
class CreateCase(BaseModel):
    client_name: str
    client_email: str
    case_type: str
    destination: str
    notes: Optional[str] = ""


class AssignDocumentRequest(BaseModel):
    case_id: str


class UpsertInvoiceRequest(BaseModel):
    id: str
    case_id: Optional[str] = ""
    number: str
    status: Optional[str] = "draft"
    client_name: Optional[str] = ""
    client_email: Optional[str] = ""
    issue_date: Optional[str] = ""
    due_date: Optional[str] = ""
    currency: Optional[str] = "EUR"
    notes: Optional[str] = ""
    template_id: Optional[str] = ""
    items: list[dict] = Field(default_factory=list)
    attachments: list[dict] = Field(default_factory=list)


@app.get("/api/cases")
async def list_cases(user: dict = Depends(get_current_user)):
    return await db_get_cases()


@app.post("/api/cases")
async def create_case(req: CreateCase, user: dict = Depends(get_current_user)):
    case_id = str(uuid.uuid4())[:8]
    case = {
        "id": case_id,
        "lawyer_id": user["id"],
        "client_name": req.client_name,
        "client_email": req.client_email,
        "case_type": req.case_type,
        "destination": req.destination,
        "notes": req.notes,
        "stage": "documents",
        "invoice_paid": False,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "docs": [],
        "invoice": None,
        "extracted": {},
        "portal_url": f"{FRONTEND_URL}/client-upload.html?id={case_id}",
    }
    await db_create_case(case)
    return case


@app.get("/api/cases/{case_id}")
async def get_case(case_id: str, user: dict = Depends(get_current_user)):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.get("/api/cases/{case_id}/public")
async def get_case_public(case_id: str):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return {
        "id": case["id"],
        "client_name": case["client_name"],
        "case_type": case["case_type"],
        "destination": case["destination"],
        "invoice": case.get("invoice"),
        "invoice_paid": case.get("invoice_paid", False),
    }


async def get_current_user_optional(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return await verify_token(authorization[len("Bearer "):])


# ─── File upload ────────────────────────────────────────
@app.post("/api/cases/{case_id}/upload")
async def upload_document(case_id: str, file: UploadFile = File(...), user: Optional[dict] = Depends(get_current_user_optional)):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    upload = await upload_file(case_id, file, source="lawyer" if user else "client")
    document_id = str(uuid.uuid4())
    document = {
        "id": document_id,
        "lawyer_id": user["id"] if user else case.get("lawyer_id"),
        "case_id": case_id,
        "case_name": case.get("client_name", ""),
        "name": upload["name"],
        "key": upload["key"],
        "url": upload.get("url", ""),
        "source": "lawyer" if user else "client",
        "status": "assigned",
        "content_type": upload.get("content_type", file.content_type or "application/octet-stream"),
        "size": upload.get("size", 0),
        "uploaded_at": utc_now(),
        "updated_at": utc_now(),
    }
    saved_doc = await db_create_document(document)
    docs = case.get("docs", []) + [{**upload, "document_id": saved_doc["id"], "uploaded_at": saved_doc["uploaded_at"]}]
    await db_update_case(case_id, {"docs": docs, "updated_at": utc_now()})
    return {**upload, "document_id": saved_doc["id"]}


@app.get("/api/documents")
async def list_documents(status: Optional[str] = None, case_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    return await db_get_documents(status=status, case_id=case_id)


@app.post("/api/documents/intake")
async def intake_document(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    cases = await db_get_cases()
    filename_lookup = re.sub(r"[^a-z0-9]+", " ", (file.filename or "").lower()).strip()
    matched_case = None
    for case in cases:
        parts = re.sub(r"[^a-z0-9]+", " ", case.get("client_name", "").lower()).split()
        if parts and all(part in filename_lookup for part in parts):
            matched_case = case
            break

    case_id = matched_case["id"] if matched_case else "unrecognized"
    uploaded = await upload_file(case_id, file, source="intake")
    document = {
        "id": str(uuid.uuid4()),
        "lawyer_id": user["id"],
        "case_id": matched_case["id"] if matched_case else None,
        "case_name": matched_case.get("client_name") if matched_case else "",
        "name": uploaded["name"],
        "key": uploaded["key"],
        "url": uploaded.get("url", ""),
        "source": "intake",
        "status": "assigned" if matched_case else "unrecognized",
        "content_type": uploaded.get("content_type", file.content_type or "application/octet-stream"),
        "size": uploaded.get("size", 0),
        "uploaded_at": utc_now(),
        "updated_at": utc_now(),
    }
    saved = await db_create_document(document)
    if matched_case:
        docs = matched_case.get("docs", []) + [{**uploaded, "document_id": saved["id"], "uploaded_at": saved["uploaded_at"]}]
        await db_update_case(matched_case["id"], {"docs": docs, "updated_at": utc_now()})
    return saved


@app.post("/api/documents/{document_id}/assign")
async def assign_document(document_id: str, req: AssignDocumentRequest, user: dict = Depends(get_current_user)):
    case = await db_get_case(req.case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    doc = await db_update_document(document_id, {
        "case_id": case["id"],
        "case_name": case["client_name"],
        "status": "assigned",
        "updated_at": utc_now(),
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    docs = case.get("docs", []) + [{
        "document_id": doc["id"],
        "key": doc.get("key", ""),
        "name": doc.get("name", ""),
        "url": doc.get("url", ""),
        "source": doc.get("source", "intake"),
        "status": "uploaded",
        "uploaded_at": doc.get("uploaded_at", utc_now()),
    }]
    await db_update_case(case["id"], {"docs": docs, "updated_at": utc_now()})
    return doc


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str, user: dict = Depends(get_current_user)):
    doc = await db_delete_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    delete_r2_object(doc.get("key", ""))
    case_id = doc.get("case_id")
    if case_id:
        case = await db_get_case(case_id)
        if case:
            docs = [
                item for item in case.get("docs", [])
                if item.get("document_id") != document_id and item.get("key") != doc.get("key")
            ]
            await db_update_case(case_id, {"docs": docs, "updated_at": utc_now()})
    return {"deleted": True, "id": document_id}


@app.delete("/api/cases/{case_id}/documents/{document_ref:path}")
async def delete_case_document(case_id: str, document_ref: str, user: Optional[dict] = Depends(get_current_user_optional)):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    removed = None
    kept = []
    for item in case.get("docs", []):
        if item.get("document_id") == document_ref or item.get("key") == document_ref:
            removed = item
        else:
            kept.append(item)
    if not removed:
        raise HTTPException(status_code=404, detail="Document not found")
    if removed.get("document_id"):
        await db_delete_document(removed["document_id"])
    delete_r2_object(removed.get("key", ""))
    await db_update_case(case_id, {"docs": kept, "updated_at": utc_now()})
    return {"deleted": True, "id": document_ref}


# ─── Invoices ───────────────────────────────────────────
@app.get("/api/invoices")
async def list_invoices(case_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    return await db_get_invoices(case_id=case_id)


@app.get("/api/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, user: dict = Depends(get_current_user)):
    invoice = await db_get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@app.post("/api/invoices")
async def upsert_invoice(req: UpsertInvoiceRequest, user: dict = Depends(get_current_user)):
    total = sum(float(item.get("quantity", 0) or 0) * float(item.get("unit_price", 0) or 0) for item in req.items)
    invoice = req.model_dump()
    invoice.update({
        "lawyer_id": user["id"],
        "amount": total,
        "net": total,
        "vat": 0,
        "vat_rate": 0,
        "updated_at": utc_now(),
        "created_at": invoice.get("created_at") or utc_now(),
    })
    return await db_upsert_invoice(invoice)


@app.post("/api/invoices/{invoice_id}/attachments")
async def upload_invoice_attachment(invoice_id: str, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    invoice = await db_get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    case_id = invoice.get("case_id") or "invoices"
    uploaded = await upload_file(f"{case_id}/invoices/{invoice_id}", file, source="invoice")
    attachment = {
        "id": str(uuid.uuid4()),
        "name": uploaded["name"],
        "key": uploaded["key"],
        "url": uploaded.get("url", ""),
        "type": uploaded.get("content_type", file.content_type or "application/octet-stream"),
        "size": uploaded.get("size", 0),
        "uploaded_at": utc_now(),
    }
    attachments = invoice.get("attachments", []) + [attachment]
    invoice["attachments"] = attachments
    invoice["updated_at"] = utc_now()
    await db_upsert_invoice(invoice)
    return attachment


@app.delete("/api/invoices/{invoice_id}/attachments/{attachment_id}")
async def delete_invoice_attachment(invoice_id: str, attachment_id: str, user: dict = Depends(get_current_user)):
    invoice = await db_get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    removed = None
    next_attachments = []
    for attachment in invoice.get("attachments", []):
        if attachment.get("id") == attachment_id:
            removed = attachment
        else:
            next_attachments.append(attachment)
    if not removed:
        raise HTTPException(status_code=404, detail="Attachment not found")
    delete_r2_object(removed.get("key", ""))
    invoice["attachments"] = next_attachments
    invoice["updated_at"] = utc_now()
    await db_upsert_invoice(invoice)
    return {"deleted": True, "id": attachment_id}


@app.post("/api/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: str, user: dict = Depends(get_current_user)):
    invoice = await db_get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    case = await db_get_case(invoice.get("case_id", "")) if invoice.get("case_id") else None
    to_email = invoice.get("client_email") or (case or {}).get("client_email")
    if not to_email:
        raise HTTPException(status_code=400, detail="Client email is missing")

    number = invoice.get("number") or invoice_id
    amount = invoice.get("amount", 0)
    currency = invoice.get("currency", "EUR")
    subject = f"Invoice {number}"
    body = (
        f"Hello {invoice.get('client_name') or (case or {}).get('client_name') or ''},\n\n"
        f"Your invoice {number} is ready.\n"
        f"Amount: {amount} {currency}\n"
        f"Due date: {invoice.get('due_date') or 'not specified'}\n\n"
        "Best regards,\nLexFlow"
    )
    result = send_email_message(to_email, subject, body)
    invoice["last_sent_to"] = to_email
    invoice["sent_at"] = utc_now()
    if invoice.get("status") == "draft":
        invoice["status"] = "unpaid"
    await db_upsert_invoice(invoice)
    return {
        **result,
        "invoice_id": invoice_id,
        "to": to_email,
        "subject": subject,
    }


@app.post("/api/cases/{case_id}/invoice")
async def create_invoice(case_id: str, user: dict = Depends(get_current_user)):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    amount = 1000.0
    vat = round(amount * 0.19, 2)
    total = round(amount + vat, 2)
    invoice_number = f"INV-{datetime.now(timezone.utc).year}-{len(memory_invoices)+1:03d}"
    invoice = {
        "id": str(uuid.uuid4())[:8],
        "number": invoice_number,
        "amount": total,
        "net": amount,
        "vat": vat,
        "vat_rate": 0.19,
        "currency": "EUR",
        "created_at": utc_now(),
    }
    await db_upsert_invoice({**invoice, "case_id": case_id, "lawyer_id": user["id"], "status": "draft", "updated_at": utc_now()})
    await db_update_case(case_id, {"invoice": invoice, "updated_at": utc_now()})
    return invoice


@app.post("/api/cases/{case_id}/pay")
async def pay_invoice(case_id: str):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if not case.get("invoice"):
        raise HTTPException(status_code=400, detail="No invoice")
    await db_update_case(case_id, {"invoice_paid": True, "stage": "processing", "updated_at": utc_now()})
    return {"status": "paid"}


@app.post("/api/cases/{case_id}/advance")
async def advance_case(case_id: str, user: dict = Depends(get_current_user)):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    stages = ["documents", "payment", "processing", "review", "submitted"]
    idx = stages.index(case.get("stage", "documents"))
    if idx < len(stages) - 1:
        await db_update_case(case_id, {"stage": stages[idx + 1], "updated_at": utc_now()})
    return await db_get_case(case_id)


# ─── Email webhook ─────────────────────────────────────
class EmailAttachment(BaseModel):
    filename: str
    content_base64: str
    content_type: Optional[str] = "application/pdf"


class EmailWebhook(BaseModel):
    from_: str = Field(..., alias="from")
    subject: str
    attachments: list[EmailAttachment]


async def process_email_payload(payload: EmailWebhook) -> dict:
    from_email = payload.from_.lower().strip()
    matched = []
    cases = await db_get_cases()
    for case in cases:
        if case.get("client_email", "").lower() == from_email:
            docs = case.get("docs", [])
            for att in payload.attachments:
                document_id = str(uuid.uuid4())
                key = f"{case['id']}/email/{document_id}/{att.filename}"
                document = {
                    "id": document_id,
                    "lawyer_id": case.get("lawyer_id"),
                    "case_id": case["id"],
                    "case_name": case.get("client_name", ""),
                    "name": att.filename,
                    "key": key,
                    "url": "",
                    "source": "email",
                    "status": "uploaded_via_email",
                    "content_type": att.content_type or "application/pdf",
                    "size": 0,
                    "uploaded_at": utc_now(),
                    "updated_at": utc_now(),
                }
                await db_create_document(document)
                docs.append({**document, "subject": payload.subject})
            await db_update_case(case["id"], {"docs": docs, "updated_at": utc_now()})
            matched.append(case["id"])
    return {"matched_cases": matched, "attachments_processed": len(payload.attachments)}


@app.post("/api/webhook/email")
async def email_webhook(payload: EmailWebhook):
    return await process_email_payload(payload)


def extract_gmail_attachments(message: Message) -> list[EmailAttachment]:
    attachments = []
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        content = part.get_payload(decode=True) or b""
        attachments.append(EmailAttachment(
            filename=filename,
            content_base64=base64.b64encode(content).decode("utf-8"),
            content_type=part.get_content_type() or "application/octet-stream",
        ))
    return attachments


@app.post("/api/gmail/poll")
async def poll_gmail(user: dict = Depends(get_current_user)):
    if not USE_GMAIL:
        raise HTTPException(status_code=400, detail="Gmail IMAP is not configured")
    processed = []
    try:
        with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST) as mailbox:
            mailbox.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
            mailbox.select(GMAIL_MAILBOX)
            status, data = mailbox.search(None, "UNSEEN")
            if status != "OK":
                raise HTTPException(status_code=502, detail="Gmail search failed")
            message_ids = data[0].split()[:GMAIL_POLL_LIMIT]
            for message_id in message_ids:
                fetch_status, fetch_data = mailbox.fetch(message_id, "(RFC822)")
                if fetch_status != "OK" or not fetch_data:
                    continue
                raw = fetch_data[0][1]
                message = email.message_from_bytes(raw)
                attachments = extract_gmail_attachments(message)
                if not attachments:
                    mailbox.store(message_id, "+FLAGS", "\\Seen")
                    continue
                sender = parseaddr(message.get("From", ""))[1]
                subject = message.get("Subject", "")
                result = await process_email_payload(EmailWebhook(
                    **{"from": sender, "subject": subject, "attachments": attachments}
                ))
                mailbox.store(message_id, "+FLAGS", "\\Seen")
                processed.append({
                    "message_id": message_id.decode("utf-8", errors="ignore"),
                    "from": sender,
                    "subject": subject,
                    **result,
                })
    except HTTPException:
        raise
    except Exception as e:
        print(f"Gmail poll failed: {e}")
        raise HTTPException(status_code=502, detail="Gmail poll failed")
    return {"processed": processed, "count": len(processed)}


# ─── OCR pipeline ───────────────────────────────────────
@app.post("/api/cases/{case_id}/parse")
async def parse_documents(case_id: str, user: dict = Depends(get_current_user)):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    docs = case.get("docs", [])
    if not docs:
        raise HTTPException(status_code=400, detail="No documents")
    # For demo, parse the first document name as source of fake text
    first_doc = docs[0]
    text = f"DEMO OCR TEXT\nName: {case['client_name']}\nPassport: D1234567\nDOB: 1990-01-01\nEmployer: Demo GmbH"
    if USE_R2 and first_doc.get("key"):
        try:
            obj = r2_client.get_object(Bucket=R2_BUCKET_NAME, Key=first_doc["key"])
            content = obj["Body"].read()
            ocr = await run_ocr(content, first_doc.get("name", "document.pdf"))
            text = ocr.get("raw_text", "")
        except Exception as e:
            print(f"R2 read for OCR failed: {e}")
    extracted = parse_document_text(text, first_doc.get("name", "document.pdf"))
    await store_extraction_evaluation(case_id, first_doc.get("document_id", ""), extracted)
    await db_update_case(case_id, {"extracted": extracted, "stage": "review", "updated_at": utc_now()})
    return {"case_id": case_id, "extracted": extracted, "stage": "review"}


class OcrEvaluationRequest(BaseModel):
    text: str
    filename: Optional[str] = ""
    case_id: Optional[str] = ""
    document_id: Optional[str] = ""


@app.post("/api/ocr/evaluate")
async def evaluate_ocr(req: OcrEvaluationRequest, user: dict = Depends(get_current_user)):
    fields = parse_document_text(req.text, req.filename or "")
    evaluation = await store_extraction_evaluation(req.case_id or "", req.document_id or "", fields)
    return {"fields": fields, "evaluation": evaluation}


@app.get("/api/evaluations")
async def list_evaluations(case_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    return await db_get_evaluations(case_id=case_id)


# ─── PDF form generation ───────────────────────────────
@app.get("/api/cases/{case_id}/form")
async def download_form(case_id: str, user: dict = Depends(get_current_user)):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    fields = {
        "full_name": case.get("client_name", ""),
        "case_type": case.get("case_type", ""),
        "destination": case.get("destination", ""),
    }
    fields.update(case.get("extracted", {}))
    buf = fill_pdf_form(fields)
    return StreamingResponse(buf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=case_{case_id}.pdf"})


# ─── Stripe webhook ─────────────────────────────────────
@app.post("/api/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.json()
    event_type = payload.get("type", "")
    if event_type == "checkout.session.completed":
        meta = payload.get("data", {}).get("object", {}).get("metadata", {})
        case_id = meta.get("case_id")
        if case_id:
            await db_update_case(case_id, {"invoice_paid": True, "stage": "processing", "updated_at": utc_now()})
    return {"received": True}


# ─── Config endpoint for frontend ───────────────────────
@app.get("/api/config")
async def public_config():
    return {
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
    }
