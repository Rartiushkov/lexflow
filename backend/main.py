import base64
import email
import hashlib
import hmac
import imaplib
import io
import json
import os
import re
import smtplib
import uuid
from datetime import date, datetime, timezone
from email.message import EmailMessage, Message
from email.utils import parseaddr
from urllib.parse import urlencode
from typing import Optional
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from local_ocr import has_tesseract, run_local_ocr
from ocr_parsers import evaluate_extraction, parse_document_text

app = FastAPI(title="LexFlow Backend", version="0.2.0")

# ─── Config ─────────────────────────────────────────────
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8001")
DEFAULT_LAWYER_ID = os.environ.get("DEFAULT_LAWYER_ID", "user_1")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "lexflow-documents")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
OCR_PROVIDER = os.environ.get("OCR_PROVIDER", "auto")
OCR_LANG = os.environ.get("OCR_LANG", "eng+deu")
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
BACKEND_PUBLIC_URL = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000")
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_STATE_SECRET = os.environ.get("GOOGLE_OAUTH_STATE_SECRET", SUPABASE_SERVICE_KEY or "dev-google-oauth-state-secret")
ENABLE_TEST_AUTH = os.environ.get("LEXFLOW_TEST_AUTH", "") == "1"

USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
USE_R2 = bool(R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY)
USE_MISTRAL = bool(MISTRAL_API_KEY)
USE_LOCAL_OCR = OCR_PROVIDER in {"auto", "local"}
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
memory_cases: dict[str, dict] = {}
memory_invoices: dict[str, dict] = {}
memory_documents: dict[str, dict] = {}
memory_invoice_templates: dict[str, dict] = {}
memory_evaluations: dict[str, dict] = {}
memory_profiles: dict[str, dict] = {}
memory_firms: dict[str, dict] = {}
memory_email_integrations: dict[str, dict] = {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_lookup(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def infer_case_type(document_type: str, subject: str = "") -> str:
    lookup = f"{document_type} {subject}".lower()
    if "blue card" in lookup:
        return "Blue Card"
    if "employment" in lookup or "passport" in lookup or "permit" in lookup:
        return "Work permit"
    if "invoice" in lookup:
        return "Billing review"
    return "Document intake"


def infer_destination(text: str = "") -> str:
    lookup = (text or "").lower()
    if "germany" in lookup or "deutschland" in lookup or "berlin" in lookup:
        return "Germany"
    if "netherlands" in lookup:
        return "Netherlands"
    if "france" in lookup:
        return "France"
    return "EU route"


def name_from_email(address: str) -> str:
    local = (address or "").split("@")[0]
    parts = [part for part in re.split(r"[._\-+]+", local) if part]
    return " ".join(part.capitalize() for part in parts[:3]) or "New client"


def parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def google_oauth_callback_url() -> str:
    return f"{BACKEND_PUBLIC_URL.rstrip('/')}/api/email-integrations/google/callback"


def sign_google_state(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    signature = hmac.new(GOOGLE_OAUTH_STATE_SECRET.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_google_state(state: str) -> dict:
    try:
        encoded, signature = state.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid OAuth state") from exc
    expected = hmac.new(GOOGLE_OAUTH_STATE_SECRET.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=400, detail="Invalid OAuth state signature")
    padded = encoded + "=" * (-len(encoded) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
    if payload.get("exp", 0) < int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(status_code=400, detail="OAuth state expired")
    return payload


def mask_integration(row: dict) -> dict:
    return {
        **row,
        "app_password": "********" if row.get("app_password") else "",
        "refresh_token": "********" if row.get("refresh_token") else "",
        "access_token": "",
    }


def strip_unsupported_case_fields(data: dict) -> dict:
    unsupported = {"firm_id", "portal_url", "public_notes", "public_submission_completed_at"}
    return {key: value for key, value in data.items() if key not in unsupported}


def is_schema_compat_error(message: str) -> bool:
    text = (message or "").lower()
    return any(
        marker in text
        for marker in (
            "does not exist",
            "schema cache",
            "could not find the table",
            "could not find the '",
            "pgrst204",
            "pgrst205",
        )
    )


def build_default_actor_context(user: dict) -> dict:
    firm_id = f"firm_{user['id']}"
    display_name = user.get("name") or user.get("email") or "LexFlow user"
    profile = {
        "id": user["id"],
        "email": user.get("email", ""),
        "full_name": display_name,
        "firm_id": firm_id,
        "firm_name": f"{display_name} Office",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    firm = {
        "id": firm_id,
        "name": profile["firm_name"],
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    return {"user": user, "profile": profile, "firm": firm}


async def ensure_actor_context(user: dict) -> dict:
    actor = build_default_actor_context(user)
    profile = await db_get_profile(user["id"])
    if profile:
        actor["profile"] = {**actor["profile"], **profile}
    actor["profile"]["firm_id"] = actor["profile"].get("firm_id") or actor["firm"]["id"]
    actor["profile"]["firm_name"] = actor["profile"].get("firm_name") or actor["firm"]["name"]
    firm = await db_get_firm(actor["profile"]["firm_id"])
    if firm:
        actor["firm"] = {**actor["firm"], **firm}
    else:
        actor["firm"]["id"] = actor["profile"]["firm_id"]
        actor["firm"]["name"] = actor["profile"].get("firm_name") or actor["firm"]["name"]
    memory_profiles[user["id"]] = actor["profile"]
    memory_firms[actor["firm"]["id"]] = actor["firm"]
    return actor


def record_belongs_to_actor(record: dict, actor: dict) -> bool:
    if not record:
        return False
    if record.get("lawyer_id") == actor["user"]["id"]:
        return True
    if record.get("firm_id") and record.get("firm_id") == actor["firm"]["id"]:
        return True
    return False


# ─── Auth ───────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str


class UpdateSettingsRequest(BaseModel):
    full_name: Optional[str] = ""
    firm_name: Optional[str] = ""
    vat_id: Optional[str] = ""


class EmailIntegrationRequest(BaseModel):
    id: Optional[str] = ""
    provider: str = "gmail"
    email: Optional[str] = ""
    app_password: Optional[str] = ""
    imap_host: Optional[str] = "imap.gmail.com"
    mailbox: Optional[str] = "INBOX"
    poll_limit: Optional[int] = 10
    active: Optional[bool] = True


async def verify_token(token: str) -> Optional[dict]:
    if ENABLE_TEST_AUTH and token.startswith("test_token_"):
        user_id = token[len("test_token_"):] or "user_1"
        return {"id": user_id, "email": f"{user_id}@test.lexflow", "name": f"Test {user_id}", "role": "lawyer"}
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
            return {**data, **res.data[0]}
        except Exception as e:
            print(f"Supabase insert failed: {e}")
            message = str(e)
            if is_schema_compat_error(message):
                legacy = strip_unsupported_case_fields(data)
                try:
                    res = supabase_client.table("cases").insert(legacy).execute()
                    return {**data, **(res.data[0] if res.data else legacy)}
                except Exception as inner:
                    print(f"Supabase legacy insert failed: {inner}")
            raise HTTPException(status_code=500, detail="Failed to save case")
    memory_cases[data["id"]] = data
    return data


async def db_get_cases() -> list:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("cases").select("*").order("created_at", desc=True).execute()
            return res.data
        except Exception as e:
            print(f"Supabase select failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to load cases")
    return sorted(memory_cases.values(), key=lambda c: c.get("created_at", ""), reverse=True)


async def db_get_case(case_id: str) -> Optional[dict]:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("cases").select("*").eq("id", case_id).limit(1).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Supabase get failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to load case")
    return memory_cases.get(case_id)


async def db_update_case(case_id: str, patch: dict) -> Optional[dict]:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("cases").update(patch).eq("id", case_id).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Supabase update failed: {e}")
            message = str(e)
            if is_schema_compat_error(message):
                legacy = strip_unsupported_case_fields(patch)
                try:
                    res = supabase_client.table("cases").update(legacy).eq("id", case_id).execute()
                    if res.data:
                        return {**res.data[0], **patch}
                except Exception as inner:
                    print(f"Supabase legacy update failed: {inner}")
            raise HTTPException(status_code=500, detail="Failed to update case")
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


async def db_find_document_by_hash(content_hash: str) -> Optional[dict]:
    if not content_hash:
        return None
    if USE_SUPABASE:
        try:
            res = supabase_client.table("documents").select("*").eq("content_hash", content_hash).limit(1).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Supabase document hash lookup failed: {e}")
    return next((item for item in memory_documents.values() if item.get("content_hash") == content_hash), None)


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


async def db_get_profile(user_id: str) -> Optional[dict]:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("profiles").select("*").eq("id", user_id).limit(1).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Supabase profile get failed: {e}")
    return memory_profiles.get(user_id)


async def db_upsert_profile(data: dict) -> dict:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("profiles").upsert(data).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase profile upsert failed: {e}")
    memory_profiles[data["id"]] = data
    return data


async def db_get_firm(firm_id: str) -> Optional[dict]:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("firms").select("*").eq("id", firm_id).limit(1).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Supabase firm get failed: {e}")
    return memory_firms.get(firm_id)


async def db_upsert_firm(data: dict) -> dict:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("firms").upsert(data).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase firm upsert failed: {e}")
    memory_firms[data["id"]] = data
    return data


async def db_upsert_email_integration(data: dict) -> dict:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("email_integrations").upsert(data).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase email integration upsert failed: {e}")
    memory_email_integrations[data["id"]] = data
    return data


async def db_delete_email_integration(integration_id: str) -> bool:
    if USE_SUPABASE:
        try:
            supabase_client.table("email_integrations").delete().eq("id", integration_id).execute()
            return True
        except Exception as e:
            print(f"Supabase email integration delete failed: {e}")
    memory_email_integrations.pop(integration_id, None)
    return True


async def db_get_email_integrations(*, lawyer_id: Optional[str] = None, firm_id: Optional[str] = None, active_only: bool = False) -> list:
    if USE_SUPABASE:
        try:
            query = supabase_client.table("email_integrations").select("*").order("created_at", desc=True)
            if active_only:
                query = query.eq("active", True)
            if lawyer_id:
                query = query.eq("lawyer_id", lawyer_id)
            if firm_id:
                query = query.eq("firm_id", firm_id)
            res = query.execute()
            return res.data
        except Exception as e:
            print(f"Supabase email integrations select failed: {e}")
    rows = list(memory_email_integrations.values())
    if active_only:
        rows = [item for item in rows if item.get("active")]
    if lawyer_id:
        rows = [item for item in rows if item.get("lawyer_id") == lawyer_id]
    if firm_id:
        rows = [item for item in rows if item.get("firm_id") == firm_id]
    return sorted(rows, key=lambda item: item.get("created_at", ""), reverse=True)


# ─── File storage helpers ───────────────────────────────
async def upload_file(case_id: str, file: UploadFile, source: str = "portal") -> dict:
    ext = Path(file.filename or "document.pdf").suffix
    key = f"{case_id}/{uuid.uuid4().hex}{ext}"
    content = await file.read()
    return await upload_bytes(case_id, file.filename or "document.pdf", file.content_type or "application/octet-stream", content, source, key=key)


async def upload_bytes(case_id: str, filename: str, content_type: str, content: bytes, source: str = "portal", key: Optional[str] = None) -> dict:
    ext = Path(filename or "document.pdf").suffix
    key = key or f"{case_id}/{uuid.uuid4().hex}{ext}"
    if USE_R2:
        try:
            r2_client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=content, ContentType=content_type or "application/octet-stream")
            url = f"{R2_PUBLIC_URL}/{key}" if R2_PUBLIC_URL else f"{R2_ENDPOINT}/{R2_BUCKET_NAME}/{key}"
            return {"key": key, "name": filename, "url": url, "source": source, "status": "uploaded", "size": len(content), "content_type": content_type or "application/octet-stream"}
        except Exception as e:
            print(f"R2 upload failed: {e}")
    return {"key": key, "name": filename, "url": "", "source": source, "status": "uploaded", "size": len(content), "content_type": content_type or "application/octet-stream"}


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
    local_result = {"raw_text": "", "provider": "disabled", "confidence": 0, "pages": []}
    if USE_LOCAL_OCR:
        local_result = run_local_ocr(content, filename, lang=OCR_LANG)
        if local_result.get("confidence", 0) >= 0.65 or OCR_PROVIDER == "local":
            return local_result
    if not USE_MISTRAL:
        return local_result if local_result.get("raw_text") else {
            "raw_text": "",
            "provider": "none",
            "confidence": 0,
            "pages": [],
            "attempts": local_result.get("attempts", []),
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
            result = r.json()
            result["provider"] = "mistral"
            result["local_attempt"] = local_result
            return result
    except Exception as e:
        print(f"Mistral OCR failed: {e}")
        return local_result if local_result.get("raw_text") else {"raw_text": "", "provider": "none", "pages": [], "error": str(e)}


async def store_extraction_evaluation(case_id: str, document_id: str, fields: dict) -> dict:
    evaluation = evaluate_extraction(fields)
    row = {
        "id": str(uuid.uuid4()),
        "case_id": case_id or None,
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


async def find_case_for_document(sender_email: str, filename: str, fields: dict, cases: list[dict]) -> tuple[Optional[dict], str, float]:
    sender = (sender_email or "").lower().strip()
    if sender:
        for case in cases:
            if case.get("client_email", "").lower().strip() == sender:
                return case, "email", 0.98

    full_name = fields.get("full_name", "")
    normalized_name = normalize_lookup(full_name)
    if normalized_name:
        for case in cases:
            if normalize_lookup(case.get("client_name", "")) == normalized_name:
                return case, "ocr_name", float(fields.get("confidence") or 0.7)

    filename_lookup = normalize_lookup(filename)
    for case in cases:
        parts = normalize_lookup(case.get("client_name", "")).split()
        if parts and all(part in filename_lookup for part in parts):
            return case, "filename", 0.75

    return None, "none", 0.0


async def create_case_from_document(sender_email: str, subject: str, fields: dict, user_id: str) -> dict:
    actor = await ensure_actor_context({"id": user_id, "email": sender_email or f"{user_id}@lexflow.local", "name": sender_email or user_id})
    client_name = fields.get("full_name") or name_from_email(sender_email)
    case_id = str(uuid.uuid4())[:8]
    case = {
        "id": case_id,
        "lawyer_id": user_id,
        "firm_id": actor["firm"]["id"],
        "client_name": client_name,
        "client_email": sender_email or fields.get("email") or "",
        "case_type": infer_case_type(fields.get("document_type", ""), subject),
        "destination": infer_destination(" ".join(str(value) for value in fields.values())),
        "notes": f"Auto-created from {fields.get('document_type', 'incoming document')}",
        "stage": "documents",
        "invoice_paid": False,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "docs": [],
        "invoice": None,
        "extracted": fields,
        "portal_url": f"{FRONTEND_URL}/client-upload.html?id={case_id}",
        "automation": {"created_by": "document_intake", "confidence": fields.get("confidence", 0)},
    }
    return await db_create_case(case)


async def case_has_document_type(case: dict, document_type: str) -> bool:
    if not case or not document_type or document_type == "unknown":
        return False
    docs = await db_get_documents(case_id=case["id"])
    return any(doc.get("document_type") == document_type and doc.get("status") != "duplicate" for doc in docs)


async def route_incoming_document(
    *,
    filename: str,
    content: bytes,
    content_type: str,
    source: str,
    sender_email: str = "",
    subject: str = "",
    user_id: str = "user_1",
) -> dict:
    actor = await ensure_actor_context({"id": user_id, "email": sender_email or f"{user_id}@lexflow.local", "name": sender_email or user_id})
    content_hash = hashlib.sha256(content).hexdigest()
    duplicate = await db_find_document_by_hash(content_hash)
    ocr = await run_ocr(content, filename)
    fields = parse_document_text(ocr.get("raw_text", ""), filename)
    fields["ocr_provider"] = ocr.get("provider", "none")
    fields["ocr_confidence"] = ocr.get("confidence", 0)

    cases = await db_get_cases()
    matched_case, match_reason, match_score = await find_case_for_document(sender_email, filename, fields, cases)
    auto_created = False
    if not matched_case and (fields.get("full_name") or sender_email) and float(fields.get("confidence") or 0) >= 0.45:
        matched_case = await create_case_from_document(sender_email, subject, fields, user_id)
        auto_created = True

    case_id = matched_case["id"] if matched_case else "unrecognized"
    uploaded = await upload_bytes(case_id, filename, content_type, content, source=source)
    document_type = fields.get("document_type", "unknown")
    type_exists = await case_has_document_type(matched_case, document_type) if matched_case else False
    status = "assigned" if matched_case else "unrecognized"
    automation_note = "routed"
    if duplicate:
        status = "duplicate"
        automation_note = f"duplicate_of:{duplicate.get('id')}"
    elif type_exists:
        status = "needs_review"
        automation_note = f"document_type_already_exists:{document_type}"

    document = {
        "id": str(uuid.uuid4()),
        "lawyer_id": user_id,
        "firm_id": matched_case.get("firm_id") if matched_case else actor["firm"]["id"],
        "case_id": matched_case["id"] if matched_case else None,
        "case_name": matched_case.get("client_name") if matched_case else "",
        "name": uploaded["name"],
        "key": uploaded["key"],
        "url": uploaded.get("url", ""),
        "source": source,
        "status": status,
        "content_type": uploaded.get("content_type", content_type or "application/octet-stream"),
        "size": uploaded.get("size", len(content)),
        "content_hash": content_hash,
        "document_type": document_type,
        "automation_status": "auto_created_case" if auto_created else match_reason,
        "automation_note": automation_note,
        "extracted": fields,
        "uploaded_at": utc_now(),
        "updated_at": utc_now(),
    }
    saved = await db_create_document(document)
    if matched_case and status != "duplicate":
        docs = matched_case.get("docs", []) + [{**uploaded, "document_id": saved["id"], "uploaded_at": saved["uploaded_at"], "document_type": document_type, "status": status}]
        await db_update_case(matched_case["id"], {"docs": docs, "extracted": {**matched_case.get("extracted", {}), **fields}, "updated_at": utc_now()})
    await store_extraction_evaluation(matched_case["id"] if matched_case else "", saved["id"], fields)
    return {
        "document": saved,
        "case": matched_case,
        "auto_created_case": auto_created,
        "match_reason": match_reason,
        "match_score": match_score,
        "duplicate": bool(duplicate),
    }


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
        "local_ocr": USE_LOCAL_OCR,
        "tesseract": has_tesseract(),
        "ocr_provider": OCR_PROVIDER,
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
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.get("/api/me")
async def me(user: dict = Depends(get_current_user)):
    return user


@app.get("/api/settings/profile")
async def get_settings_profile(user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    return {
        "profile": actor["profile"],
        "firm": actor["firm"],
    }


@app.post("/api/settings/profile")
async def update_settings_profile(req: UpdateSettingsRequest, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    firm = await db_upsert_firm({
        **actor["firm"],
        "name": req.firm_name or actor["firm"].get("name") or actor["firm"]["id"],
        "vat_id": req.vat_id or actor["firm"].get("vat_id", ""),
        "updated_at": utc_now(),
    })
    profile = await db_upsert_profile({
        **actor["profile"],
        "full_name": req.full_name or actor["profile"].get("full_name", ""),
        "firm_id": firm["id"],
        "firm_name": firm.get("name", ""),
        "vat_id": req.vat_id or actor["profile"].get("vat_id", ""),
        "updated_at": utc_now(),
    })
    return {"profile": profile, "firm": firm}


@app.get("/api/email-integrations")
async def list_email_integrations(user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    rows = await db_get_email_integrations(firm_id=actor["firm"]["id"])
    return [mask_integration(row) for row in rows]


@app.post("/api/email-integrations")
async def upsert_email_integration(req: EmailIntegrationRequest, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    if not req.app_password:
        raise HTTPException(status_code=400, detail="App password is required for manual IMAP mode")
    integration = {
        "id": req.id or str(uuid.uuid4()),
        "provider": req.provider,
        "email": (req.email or "").lower().strip(),
        "app_password": req.app_password,
        "imap_host": req.imap_host or "imap.gmail.com",
        "mailbox": req.mailbox or "INBOX",
        "poll_limit": max(1, min(int(req.poll_limit or 10), 100)),
        "active": bool(req.active),
        "lawyer_id": user["id"],
        "firm_id": actor["firm"]["id"],
        "auth_type": "app_password",
        "refresh_token": "",
        "access_token": "",
        "token_expires_at": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "last_polled_at": None,
        "last_processed_message_id": "",
    }
    saved = await db_upsert_email_integration(integration)
    return mask_integration(saved)


@app.delete("/api/email-integrations/{integration_id}")
async def delete_email_integration(integration_id: str, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    integrations = await db_get_email_integrations(firm_id=actor["firm"]["id"])
    match = next((i for i in integrations if i["id"] == integration_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Integration not found")
    await db_delete_email_integration(integration_id)
    return {"deleted": True, "id": integration_id}


@app.post("/api/email-integrations/google/start")
async def start_google_email_integration(user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET:
        raise HTTPException(status_code=400, detail="Google OAuth is not configured on the backend")
    state = sign_google_state({
        "user_id": user["id"],
        "firm_id": actor["firm"]["id"],
        "next": f"{FRONTEND_URL.rstrip('/')}/settings.html",
        "exp": int(datetime.now(timezone.utc).timestamp()) + 600,
    })
    query = urlencode({
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": google_oauth_callback_url(),
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": "openid email profile https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.modify",
        "state": state,
    })
    return {"auth_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"}


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


class PublicCaseSubmitRequest(BaseModel):
    client_name: Optional[str] = ""
    client_email: Optional[str] = ""
    notes: Optional[str] = ""


@app.get("/api/cases")
async def list_cases(user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    return [case for case in await db_get_cases() if record_belongs_to_actor(case, actor)]


@app.post("/api/cases")
async def create_case(req: CreateCase, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    case_id = str(uuid.uuid4())[:8]
    case = {
        "id": case_id,
        "lawyer_id": user["id"],
        "firm_id": actor["firm"]["id"],
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
    actor = await ensure_actor_context(user)
    case = await db_get_case(case_id)
    if not case or not record_belongs_to_actor(case, actor):
        raise HTTPException(status_code=404, detail="Case not found")
    return case


class UpdateCase(BaseModel):
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    case_type: Optional[str] = None
    destination: Optional[str] = None
    stage: Optional[str] = None
    notes: Optional[str] = None
    extracted: Optional[dict] = None
    invoice_paid: Optional[bool] = None
    public_notes: Optional[str] = None


@app.patch("/api/cases/{case_id}")
async def update_case(case_id: str, req: UpdateCase, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    case = await db_get_case(case_id)
    if not case or not record_belongs_to_actor(case, actor):
        raise HTTPException(status_code=404, detail="Case not found")
    patch_data = {k: v for k, v in req.model_dump().items() if v is not None}
    patch_data["updated_at"] = utc_now()
    updated = await db_update_case(case_id, patch_data)
    return updated or {**case, **patch_data}


@app.delete("/api/cases/{case_id}")
async def delete_case(case_id: str, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    case = await db_get_case(case_id)
    if not case or not record_belongs_to_actor(case, actor):
        raise HTTPException(status_code=404, detail="Case not found")
    # Delete associated documents from R2 + DB
    for doc in case.get("docs", []):
        doc_id = doc.get("document_id")
        key = doc.get("key", "")
        if key:
            delete_r2_object(key)
        if doc_id:
            await db_delete_document(doc_id)
    # Delete the case itself
    if USE_SUPABASE:
        try:
            supabase_client.table("cases").delete().eq("id", case_id).execute()
        except Exception as e:
            print(f"Supabase case delete failed: {e}")
    else:
        memory_cases.pop(case_id, None)
    return {"deleted": True, "id": case_id}


@app.get("/api/cases/{case_id}/public")
async def get_case_public(case_id: str):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return {
        "id": case["id"],
        "client_name": case["client_name"],
        "client_email": case.get("client_email", ""),
        "case_type": case["case_type"],
        "destination": case["destination"],
        "invoice": case.get("invoice"),
        "invoice_paid": case.get("invoice_paid", False),
        "public_notes": case.get("public_notes", ""),
    }


@app.post("/api/cases/{case_id}/public-submit")
async def submit_case_public(case_id: str, req: PublicCaseSubmitRequest):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    patch = {
        "client_name": req.client_name or case.get("client_name", ""),
        "client_email": req.client_email or case.get("client_email", ""),
        "public_notes": req.notes or case.get("public_notes", ""),
        "public_submission_completed_at": utc_now(),
        "updated_at": utc_now(),
    }
    updated = await db_update_case(case_id, patch)
    return {"submitted": True, "case": updated}


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
        "firm_id": case.get("firm_id"),
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
    actor = await ensure_actor_context(user)
    docs = await db_get_documents(status=status, case_id=case_id)
    return [doc for doc in docs if record_belongs_to_actor(doc, actor)]


@app.post("/api/documents/intake")
async def intake_document(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    content = await file.read()
    result = await route_incoming_document(
        filename=file.filename or "document.pdf",
        content=content,
        content_type=file.content_type or "application/octet-stream",
        source="intake",
        user_id=user["id"],
    )
    return result["document"]


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
    actor = await ensure_actor_context(user)
    invoices = await db_get_invoices(case_id=case_id)
    return [invoice for invoice in invoices if record_belongs_to_actor(invoice, actor)]


@app.get("/api/workflow/summary")
async def workflow_summary(user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    today = datetime.now(timezone.utc).date()
    invoices = [invoice for invoice in await db_get_invoices() if record_belongs_to_actor(invoice, actor)]
    documents = [doc for doc in await db_get_documents() if record_belongs_to_actor(doc, actor)]
    cases = [case for case in await db_get_cases() if record_belongs_to_actor(case, actor)]
    overdue = []
    due_soon = []
    for invoice in invoices:
        if invoice.get("status") == "paid":
            continue
        due = parse_date(invoice.get("due_date", ""))
        if not due:
            continue
        days_left = (due - today).days
        enriched = {**invoice, "days_left": days_left}
        if days_left < 0:
            overdue.append(enriched)
            if invoice.get("status") != "overdue":
                invoice["status"] = "overdue"
                invoice["updated_at"] = utc_now()
                await db_upsert_invoice(invoice)
        elif days_left <= 3:
            due_soon.append(enriched)

    doc_counts = {
        "unrecognized": len([doc for doc in documents if doc.get("status") == "unrecognized"]),
        "needs_review": len([doc for doc in documents if doc.get("status") == "needs_review"]),
        "duplicates": len([doc for doc in documents if doc.get("status") == "duplicate"]),
    }
    actions = []
    if overdue:
        actions.append({"priority": "high", "label": "Overdue invoices", "count": len(overdue), "action": "Send reminder or mark paid"})
    if due_soon:
        actions.append({"priority": "medium", "label": "Invoices due soon", "count": len(due_soon), "action": "Follow up before due date"})
    if doc_counts["unrecognized"]:
        actions.append({"priority": "medium", "label": "Unrecognized documents", "count": doc_counts["unrecognized"], "action": "Review and assign"})
    if doc_counts["needs_review"]:
        actions.append({"priority": "medium", "label": "Duplicate document type", "count": doc_counts["needs_review"], "action": "Confirm replacement or keep both"})

    return {
        "generated_at": utc_now(),
        "cases": {"total": len(cases), "by_stage": {stage: len([case for case in cases if case.get("stage") == stage]) for stage in ["documents", "payment", "processing", "review", "submitted"]}},
        "documents": doc_counts,
        "invoices": {"overdue": overdue, "due_soon": due_soon},
        "actions": actions,
    }


@app.get("/api/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, user: dict = Depends(get_current_user)):
    invoice = await db_get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@app.post("/api/invoices")
async def upsert_invoice(req: UpsertInvoiceRequest, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    total = sum(float(item.get("quantity", 0) or 0) * float(item.get("unit_price", 0) or 0) for item in req.items)
    invoice = req.model_dump()
    invoice.update({
        "lawyer_id": user["id"],
        "firm_id": actor["firm"]["id"],
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


async def process_email_payload(payload: EmailWebhook, *, owner_user_id: Optional[str] = None) -> dict:
    from_email = payload.from_.lower().strip()
    matched = []
    created = []
    documents = []
    duplicates = 0
    for att in payload.attachments:
        content = base64.b64decode(att.content_base64)
        result = await route_incoming_document(
            filename=att.filename,
            content=content,
            content_type=att.content_type or "application/pdf",
            source="email",
            sender_email=from_email,
            subject=payload.subject,
            user_id=owner_user_id or DEFAULT_LAWYER_ID,
        )
        if result["case"]:
            matched.append(result["case"]["id"])
        if result["auto_created_case"] and result["case"]:
            created.append(result["case"]["id"])
        if result["duplicate"]:
            duplicates += 1
        documents.append(result["document"])
    return {
        "matched_cases": sorted(set(matched)),
        "created_cases": sorted(set(created)),
        "attachments_processed": len(payload.attachments),
        "duplicates": duplicates,
        "documents": documents,
    }


@app.post("/api/webhook/email")
async def email_webhook(payload: EmailWebhook):
    return await process_email_payload(payload)


async def exchange_google_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": google_oauth_callback_url(),
            "grant_type": "authorization_code",
        })
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Failed to exchange Google OAuth code")
    return response.json()


async def refresh_google_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post("https://oauth2.googleapis.com/token", data={
            "refresh_token": refresh_token,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "grant_type": "refresh_token",
        })
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Failed to refresh Google access token")
    return response.json()


async def fetch_google_user_email(access_token: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Failed to fetch Google user profile")
    return (response.json().get("email") or "").lower().strip()


@app.get("/api/email-integrations/google/callback")
async def google_email_integration_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(f"{FRONTEND_URL.rstrip('/')}/settings.html?gmail_connected=0&reason={error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing Google OAuth callback parameters")
    payload = verify_google_state(state)
    token_data = await exchange_google_code(code)
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    if not access_token:
        raise HTTPException(status_code=502, detail="Google OAuth token response did not include an access token")
    email_address = await fetch_google_user_email(access_token)
    integration = {
        "id": str(uuid.uuid4()),
        "provider": "gmail",
        "auth_type": "oauth",
        "email": email_address,
        "app_password": "",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expires_at": (
            datetime.now(timezone.utc).timestamp() + int(token_data.get("expires_in") or 3600)
        ),
        "imap_host": "imap.gmail.com",
        "mailbox": "INBOX",
        "poll_limit": 10,
        "active": True,
        "lawyer_id": payload["user_id"],
        "firm_id": payload["firm_id"],
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "last_polled_at": None,
        "last_processed_message_id": "",
    }
    existing = await db_get_email_integrations(lawyer_id=payload["user_id"], firm_id=payload["firm_id"])
    matched = next((item for item in existing if item.get("provider") == "gmail" and item.get("email") == email_address), None)
    if matched:
        integration["id"] = matched["id"]
        integration["created_at"] = matched.get("created_at", utc_now())
        if not refresh_token:
            integration["refresh_token"] = matched.get("refresh_token", "")
    await db_upsert_email_integration(integration)
    return RedirectResponse(f"{payload.get('next', FRONTEND_URL.rstrip('/') + '/settings.html')}?gmail_connected=1")


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


def decode_gmail_base64(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


async def gmail_api_get_json(path: str, access_token: str, *, params: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/{path.lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Gmail API request failed")
    return response.json()


async def gmail_api_post_json(path: str, access_token: str, *, body: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"https://gmail.googleapis.com/gmail/v1/users/me/{path.lstrip('/')}",
            json=body or {},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Gmail API write request failed")
    return response.json() if response.text else {}


async def ensure_google_integration_access_token(integration: dict) -> dict:
    expires_at = float(integration.get("token_expires_at") or 0)
    if integration.get("access_token") and expires_at > datetime.now(timezone.utc).timestamp() + 60:
        return integration
    if not integration.get("refresh_token"):
        raise HTTPException(status_code=400, detail=f"Google OAuth refresh token missing for {integration.get('email')}")
    token_data = await refresh_google_access_token(integration["refresh_token"])
    updated = {
        **integration,
        "access_token": token_data.get("access_token", ""),
        "token_expires_at": datetime.now(timezone.utc).timestamp() + int(token_data.get("expires_in") or 3600),
        "updated_at": utc_now(),
    }
    if token_data.get("refresh_token"):
        updated["refresh_token"] = token_data["refresh_token"]
    await db_upsert_email_integration(updated)
    return updated


async def gmail_message_to_attachments(access_token: str, message_id: str) -> tuple[str, str, list[EmailAttachment]]:
    message = await gmail_api_get_json(f"messages/{message_id}", access_token, params={"format": "full"})
    headers = {item.get("name", "").lower(): item.get("value", "") for item in message.get("payload", {}).get("headers", [])}
    attachments: list[EmailAttachment] = []

    async def walk_parts(part: dict):
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        if filename:
            content = b""
            if body.get("data"):
                content = decode_gmail_base64(body["data"])
            elif body.get("attachmentId"):
                attachment = await gmail_api_get_json(
                    f"messages/{message_id}/attachments/{body['attachmentId']}",
                    access_token,
                )
                content = decode_gmail_base64(attachment.get("data", ""))
            attachments.append(EmailAttachment(
                filename=filename,
                content_base64=base64.b64encode(content).decode("utf-8"),
                content_type=part.get("mimeType") or "application/octet-stream",
            ))
        for child in part.get("parts", []) or []:
            await walk_parts(child)

    await walk_parts(message.get("payload", {}))
    return headers.get("from", ""), headers.get("subject", ""), attachments


async def process_email_integration(integration: dict) -> dict:
    processed = []
    if integration.get("provider") == "gmail" and integration.get("auth_type") == "oauth":
        ready = await ensure_google_integration_access_token(integration)
        access_token = ready["access_token"]
        listing = await gmail_api_get_json(
            "messages",
            access_token,
            params={
                "q": "is:unread has:attachment",
                "maxResults": int(ready.get("poll_limit") or 10),
            },
        )
        for item in listing.get("messages", []) or []:
            message_id = item.get("id")
            if not message_id:
                continue
            from_header, subject, attachments = await gmail_message_to_attachments(access_token, message_id)
            if not attachments:
                await gmail_api_post_json(f"messages/{message_id}/modify", access_token, body={"removeLabelIds": ["UNREAD"]})
                continue
            sender = parseaddr(from_header)[1]
            result = await process_email_payload(EmailWebhook(
                **{"from": sender, "subject": subject, "attachments": attachments}
            ), owner_user_id=ready.get("lawyer_id"))
            await gmail_api_post_json(f"messages/{message_id}/modify", access_token, body={"removeLabelIds": ["UNREAD"]})
            processed.append({
                "integration_id": ready["id"],
                "message_id": message_id,
                "from": sender,
                "subject": subject,
                **result,
            })
        await db_upsert_email_integration({
            **ready,
            "last_polled_at": utc_now(),
            "updated_at": utc_now(),
        })
        return {"integration_id": ready["id"], "email": ready["email"], "processed": processed, "count": len(processed)}

    with imaplib.IMAP4_SSL(integration.get("imap_host") or "imap.gmail.com") as mailbox:
        mailbox.login(integration["email"], integration["app_password"])
        mailbox.select(integration.get("mailbox") or "INBOX")
        status, data = mailbox.search(None, "UNSEEN")
        if status != "OK":
            raise HTTPException(status_code=502, detail=f"Gmail search failed for {integration['email']}")
        message_ids = data[0].split()[: int(integration.get("poll_limit") or 10)]
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
            ), owner_user_id=integration.get("lawyer_id"))
            mailbox.store(message_id, "+FLAGS", "\\Seen")
            processed.append({
                "integration_id": integration["id"],
                "message_id": message_id.decode("utf-8", errors="ignore"),
                "from": sender,
                "subject": subject,
                **result,
            })
    await db_upsert_email_integration({
        **integration,
        "last_polled_at": utc_now(),
        "updated_at": utc_now(),
    })
    return {"integration_id": integration["id"], "email": integration["email"], "processed": processed, "count": len(processed)}


@app.post("/api/gmail/poll")
async def poll_gmail(user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    integrations = await db_get_email_integrations(firm_id=actor["firm"]["id"], active_only=True)
    if not integrations and USE_GMAIL:
        integrations = [{
            "id": "env-fallback",
            "email": GMAIL_EMAIL,
            "app_password": GMAIL_APP_PASSWORD,
            "imap_host": GMAIL_IMAP_HOST,
            "mailbox": GMAIL_MAILBOX,
            "poll_limit": GMAIL_POLL_LIMIT,
            "lawyer_id": user["id"],
            "firm_id": actor["firm"]["id"],
            "active": True,
        }]
    if not integrations:
        raise HTTPException(status_code=400, detail="No active Gmail integrations configured")
    runs = []
    for integration in integrations:
        try:
            runs.append(await process_email_integration(integration))
        except HTTPException:
            raise
        except Exception as e:
            print(f"Gmail poll failed for {integration.get('email')}: {e}")
            raise HTTPException(status_code=502, detail=f"Gmail poll failed for {integration.get('email')}")
    return {"runs": runs, "count": sum(item["count"] for item in runs)}


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
        "google_email_oauth_enabled": bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET),
    }
