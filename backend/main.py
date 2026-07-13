import asyncio
import base64
import contextlib
import email
import html
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
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
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
ZOHO_OAUTH_CLIENT_ID = os.environ.get("ZOHO_OAUTH_CLIENT_ID", "")
ZOHO_OAUTH_CLIENT_SECRET = os.environ.get("ZOHO_OAUTH_CLIENT_SECRET", "")
ZOHO_OAUTH_STATE_SECRET = os.environ.get("ZOHO_OAUTH_STATE_SECRET", SUPABASE_SERVICE_KEY or "dev-zoho-oauth-state-secret")
ZOHO_ACCOUNTS_BASE = os.environ.get("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.com").rstrip("/")
ZOHO_MAIL_API_BASE = os.environ.get("ZOHO_MAIL_API_BASE", "https://mail.zoho.com").rstrip("/")
ENABLE_TEST_AUTH = os.environ.get("LEXFLOW_TEST_AUTH", "") == "1"
EMAIL_AUTO_POLL_ENABLED = os.environ.get("EMAIL_AUTO_POLL_ENABLED", "1") == "1" and not ENABLE_TEST_AUTH
EMAIL_AUTO_POLL_INTERVAL_SECONDS = max(15, int(os.environ.get("EMAIL_AUTO_POLL_INTERVAL_SECONDS", "30") or "30"))
EMAIL_AUTO_POLL_IDLE_INTERVAL_SECONDS = max(
    EMAIL_AUTO_POLL_INTERVAL_SECONDS,
    int(os.environ.get("EMAIL_AUTO_POLL_IDLE_INTERVAL_SECONDS", "900") or "900"),
)

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
memory_notifications: dict[str, dict] = {}
EMAIL_INTEGRATIONS_R2_KEY = "_system/email_integrations.json"

app.state.email_poll_lock = None
app.state.email_poll_task = None
app.state.email_poll_debug = {}
app.state.email_integration_db_debug = {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_email_integrations_from_r2() -> list[dict]:
    if not USE_R2 or not r2_client:
        return []
    try:
        obj = r2_client.get_object(Bucket=R2_BUCKET_NAME, Key=EMAIL_INTEGRATIONS_R2_KEY)
        payload = obj["Body"].read().decode("utf-8")
        data = json.loads(payload or "[]")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict) and item.get("id")]
    except Exception as e:
        print(f"R2 email integrations load failed: {e}")
    return []


def save_email_integrations_to_r2(rows: list[dict]) -> None:
    if not USE_R2 or not r2_client:
        return
    try:
        r2_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=EMAIL_INTEGRATIONS_R2_KEY,
            Body=json.dumps(rows, ensure_ascii=True).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:
        print(f"R2 email integrations save failed: {e}")


def normalize_lookup(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def parse_duplicate_origin_id(value: str) -> str:
    note = (value or "").strip()
    if note.startswith("duplicate_of:"):
        return note.split(":", 1)[1].strip()
    return ""


def mistral_ocr_to_raw_text(payload: dict) -> tuple[str, float, list[dict]]:
    pages = payload.get("pages") or []
    markdown_parts = []
    normalized_pages = []
    confidences = []
    for index, page in enumerate(pages):
        markdown = (page.get("markdown") or "").strip()
        if markdown:
            markdown_parts.append(markdown)
        normalized_pages.append({
            "page": page.get("index", index) + 1 if page.get("index") is not None else index + 1,
            "chars": len(markdown),
            "method": "mistral_markdown",
        })
        confidence_scores = page.get("confidence_scores") or {}
        average_confidence = confidence_scores.get("average_page_confidence_score")
        if average_confidence is not None:
            try:
                confidences.append(float(average_confidence))
            except Exception:
                pass
    raw_text = "\n\n".join(part for part in markdown_parts if part).strip()
    confidence = sum(confidences) / len(confidences) if confidences else (0.82 if raw_text else 0.0)
    return raw_text, confidence, normalized_pages


def score_extracted_fields(fields: dict) -> float:
    if not fields:
        return 0.0
    important = (
        "full_name",
        "passport_number",
        "date_of_birth",
        "expiry_date",
        "nationality",
        "employer",
        "email",
        "phone",
        "address",
    )
    present = sum(1 for key in important if fields.get(key))
    confidence = float(fields.get("confidence") or 0)
    return round(confidence + present * 0.08, 3)


def merge_extracted_fields(base: dict, incoming: dict) -> dict:
    merged = dict(base or {})
    current_score = score_extracted_fields(merged)
    incoming_score = score_extracted_fields(incoming)
    for key, value in (incoming or {}).items():
        if value in (None, "", [], {}):
            continue
        if key in {"missing_fields", "document_type", "classification_confidence", "confidence", "ocr_provider", "ocr_confidence"}:
            continue
        if not merged.get(key) or incoming_score >= current_score:
            merged[key] = value
    merged["missing_fields"] = incoming.get("missing_fields") or merged.get("missing_fields") or []
    merged["document_type"] = incoming.get("document_type") if incoming.get("document_type") and incoming.get("document_type") != "unknown" else merged.get("document_type", "unknown")
    merged["classification_confidence"] = max(float(merged.get("classification_confidence") or 0), float(incoming.get("classification_confidence") or 0))
    merged["confidence"] = max(float(merged.get("confidence") or 0), float(incoming.get("confidence") or 0))
    if incoming.get("ocr_provider"):
        merged["ocr_provider"] = incoming.get("ocr_provider")
    if incoming.get("ocr_confidence") is not None:
        merged["ocr_confidence"] = incoming.get("ocr_confidence")
    return merged


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


def normalize_identity_text(value: str) -> str:
    return normalize_lookup(value or "")


def normalize_identity_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


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


def zoho_oauth_callback_url() -> str:
    return f"{BACKEND_PUBLIC_URL.rstrip('/')}/api/email-integrations/zoho/callback"


def sign_oauth_state(payload: dict, secret: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_oauth_state(state: str, secret: str) -> dict:
    try:
        encoded, signature = state.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid OAuth state") from exc
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=400, detail="Invalid OAuth state signature")
    padded = encoded + "=" * (-len(encoded) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
    if payload.get("exp", 0) < int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(status_code=400, detail="OAuth state expired")
    return payload


def sign_google_state(payload: dict) -> str:
    return sign_oauth_state(payload, GOOGLE_OAUTH_STATE_SECRET)


def verify_google_state(state: str) -> dict:
    return verify_oauth_state(state, GOOGLE_OAUTH_STATE_SECRET)


def sign_zoho_state(payload: dict) -> str:
    return sign_oauth_state(payload, ZOHO_OAUTH_STATE_SECRET)


def verify_zoho_state(state: str) -> dict:
    return verify_oauth_state(state, ZOHO_OAUTH_STATE_SECRET)


def mask_integration(row: dict) -> dict:
    return {
        **row,
        "app_password": "********" if row.get("app_password") else "",
        "refresh_token": "********" if row.get("refresh_token") else "",
        "access_token": "",
    }


def trim_poll_debug_text(value: str, limit: int = 280) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def record_email_poll_debug(integration: dict, **patch) -> dict:
    bucket = getattr(app.state, "email_poll_debug", None)
    if bucket is None:
        bucket = {}
        app.state.email_poll_debug = bucket
    integration_id = integration.get("id") or "unknown"
    current = bucket.get(integration_id, {})
    next_state = {
        **current,
        **patch,
        "integration_id": integration_id,
        "email": integration.get("email", current.get("email", "")),
        "provider": integration.get("provider", current.get("provider", "")),
        "auth_type": integration.get("auth_type", current.get("auth_type", "")),
        "updated_at": utc_now(),
    }
    if "last_error" in next_state:
        next_state["last_error"] = trim_poll_debug_text(next_state.get("last_error", ""))
    bucket[integration_id] = next_state
    return next_state


def is_usable_email_integration(row: dict) -> bool:
    if not row or row.get("provider") not in {"gmail", "zoho"}:
        return False
    auth_type = row.get("auth_type") or "app_password"
    if auth_type == "oauth":
        return bool(row.get("email")) and bool(row.get("refresh_token") or row.get("access_token"))
    return bool(row.get("email")) and bool(row.get("app_password"))


def pick_runtime_email_integrations(rows: list[dict]) -> list[dict]:
    usable = [row for row in (rows or []) if is_usable_email_integration(row)]
    if not usable:
        return []

    def sort_key(row: dict) -> tuple:
        auth_type = row.get("auth_type") or "app_password"
        is_oauth = auth_type == "oauth"
        is_active = row.get("active") is not False
        has_refresh = bool(row.get("refresh_token"))
        has_access = bool(row.get("access_token"))
        return (
            1 if is_active else 0,
            1 if is_oauth else 0,
            1 if has_refresh else 0,
            1 if has_access else 0,
            row.get("updated_at") or row.get("created_at") or "",
        )

    preferred_by_email: dict[str, dict] = {}
    for row in sorted(usable, key=sort_key, reverse=True):
        email_key = (row.get("email") or "").lower().strip() or row.get("id") or str(uuid.uuid4())
        preferred_by_email.setdefault(email_key, row)
    return list(preferred_by_email.values())


def workspace_key_for_integration(row: dict) -> str:
    firm_id = (row or {}).get("firm_id")
    if firm_id:
        return f"firm:{firm_id}"
    lawyer_id = (row or {}).get("lawyer_id")
    if lawyer_id:
        return f"lawyer:{lawyer_id}"
    email_value = (row or {}).get("email")
    if email_value:
        return f"email:{email_value.lower().strip()}"
    return f"integration:{(row or {}).get('id', 'unknown')}"


def pick_runtime_email_integrations_by_workspace(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows or []:
        grouped.setdefault(workspace_key_for_integration(row), []).append(row)

    picked: list[dict] = []
    for group_rows in grouped.values():
        picked.extend(pick_runtime_email_integrations(group_rows))
    return picked


def strip_unsupported_case_fields(data: dict) -> dict:
    unsupported = {
        "firm_id",
        "portal_url",
        "public_notes",
        "public_submission_completed_at",
        "route_code",
        "control_state",
    }
    return {key: value for key, value in data.items() if key not in unsupported}


def extract_missing_case_columns(message: str) -> set[str]:
    if not message:
        return set()
    columns = set()
    patterns = (
        r"Could not find the '([^']+)' column of 'cases'",
        r"column\s+cases\.([a-zA-Z0-9_]+)\s+does not exist",
        r"column\s+\"?([a-zA-Z0-9_]+)\"?\s+of relation\s+\"?cases\"?\s+does not exist",
    )
    for pattern in patterns:
        columns.update(re.findall(pattern, message, flags=re.IGNORECASE))
    return {column for column in columns if column}


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


def prune_case_payload_for_legacy_schema(data: dict, message: str) -> dict:
    unsupported = set(strip_unsupported_case_fields(data).keys()) ^ set(data.keys())
    missing_columns = extract_missing_case_columns(message)
    blocked = unsupported | missing_columns
    return {key: value for key, value in data.items() if key not in blocked}


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
                legacy = prune_case_payload_for_legacy_schema(data, message)
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
                legacy = prune_case_payload_for_legacy_schema(patch, message)
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
    result = None
    if USE_SUPABASE:
        try:
            res = supabase_client.table("email_integrations").upsert(data).execute()
            app.state.email_integration_db_debug = {
                "last_upsert_error": "",
                "last_select_error": (getattr(app.state, "email_integration_db_debug", {}) or {}).get("last_select_error", ""),
                "last_upsert_at": utc_now(),
                "last_select_at": (getattr(app.state, "email_integration_db_debug", {}) or {}).get("last_select_at", ""),
            }
            result = res.data[0]
        except Exception as e:
            print(f"Supabase email integration upsert failed: {e}")
            app.state.email_integration_db_debug = {
                "last_upsert_error": trim_poll_debug_text(str(e)),
                "last_select_error": (getattr(app.state, "email_integration_db_debug", {}) or {}).get("last_select_error", ""),
                "last_upsert_at": utc_now(),
                "last_select_at": (getattr(app.state, "email_integration_db_debug", {}) or {}).get("last_select_at", ""),
            }
    payload = result or data
    memory_email_integrations[payload["id"]] = payload
    rows = [item for item in load_email_integrations_from_r2() if item.get("id") != payload["id"]]
    rows.append(payload)
    save_email_integrations_to_r2(sorted(rows, key=lambda item: item.get("created_at", ""), reverse=True))
    return payload


async def db_delete_email_integration(integration_id: str) -> bool:
    if USE_SUPABASE:
        try:
            supabase_client.table("email_integrations").delete().eq("id", integration_id).execute()
        except Exception as e:
            print(f"Supabase email integration delete failed: {e}")
    memory_email_integrations.pop(integration_id, None)
    rows = [item for item in load_email_integrations_from_r2() if item.get("id") != integration_id]
    save_email_integrations_to_r2(rows)
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
            app.state.email_integration_db_debug = {
                "last_upsert_error": (getattr(app.state, "email_integration_db_debug", {}) or {}).get("last_upsert_error", ""),
                "last_select_error": "",
                "last_upsert_at": (getattr(app.state, "email_integration_db_debug", {}) or {}).get("last_upsert_at", ""),
                "last_select_at": utc_now(),
            }
            return res.data
        except Exception as e:
            print(f"Supabase email integrations select failed: {e}")
            app.state.email_integration_db_debug = {
                "last_upsert_error": (getattr(app.state, "email_integration_db_debug", {}) or {}).get("last_upsert_error", ""),
                "last_select_error": trim_poll_debug_text(str(e)),
                "last_upsert_at": (getattr(app.state, "email_integration_db_debug", {}) or {}).get("last_upsert_at", ""),
                "last_select_at": utc_now(),
            }
    rows = list(memory_email_integrations.values())
    if not rows:
        rows = load_email_integrations_from_r2()
        if rows:
            memory_email_integrations.clear()
            memory_email_integrations.update({item["id"]: item for item in rows if item.get("id")})
    if active_only:
        rows = [item for item in rows if item.get("active")]
    if lawyer_id:
        rows = [item for item in rows if item.get("lawyer_id") == lawyer_id]
    if firm_id:
        rows = [item for item in rows if item.get("firm_id") == firm_id]
    return sorted(rows, key=lambda item: item.get("created_at", ""), reverse=True)


async def db_create_audit_event(data: dict) -> dict:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("audit_events").insert(data).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase audit event insert failed: {e}")
    return data


async def db_upsert_notification(data: dict) -> dict:
    if USE_SUPABASE:
        try:
            res = supabase_client.table("notifications").upsert(data).execute()
            return res.data[0]
        except Exception as e:
            print(f"Supabase notification upsert failed: {e}")
    memory_notifications[data["id"]] = data
    return data


async def db_get_notifications(*, firm_id: Optional[str] = None, lawyer_id: Optional[str] = None, case_id: Optional[str] = None, unread_only: bool = False) -> list:
    if USE_SUPABASE:
        try:
            query = supabase_client.table("notifications").select("*").order("created_at", desc=True)
            if firm_id:
                query = query.eq("firm_id", firm_id)
            if lawyer_id:
                query = query.eq("lawyer_id", lawyer_id)
            if case_id:
                query = query.eq("case_id", case_id)
            if unread_only:
                query = query.eq("read_at", None)
            res = query.execute()
            return res.data
        except Exception as e:
            print(f"Supabase notification select failed: {e}")
    rows = list(memory_notifications.values())
    if firm_id:
        rows = [item for item in rows if item.get("firm_id") == firm_id]
    if lawyer_id:
        rows = [item for item in rows if item.get("lawyer_id") == lawyer_id]
    if case_id:
        rows = [item for item in rows if item.get("case_id") == case_id]
    if unread_only:
        rows = [item for item in rows if not item.get("read_at")]
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


def send_email_message(to_email: str, subject: str, body: str, html_body: Optional[str] = None) -> dict:
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
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        return {"sent": True, "status": "sent"}
    except Exception as e:
        print(f"SMTP send failed: {e}")
        raise HTTPException(status_code=502, detail="Email delivery failed")


def build_portal_invite_email(case_item: dict, portal_url: str, custom_message: str = "") -> tuple[str, str]:
    client_name = case_item.get("client_name") or "Client"
    case_type = case_item.get("case_type") or "Immigration case"
    destination = case_item.get("destination") or "Germany"
    escaped_name = html.escape(client_name)
    escaped_case = html.escape(case_type)
    escaped_destination = html.escape(destination)
    escaped_url = html.escape(portal_url, quote=True)
    message_text = (custom_message or "").strip()
    escaped_message = html.escape(message_text).replace("\n", "<br/>")

    text_body = (
        f"Hello {client_name},\n\n"
        f"Please use your secure LexFlow portal to upload documents for your {case_type} case"
        f" ({destination}).\n\n"
        f"Open your portal:\n{portal_url}\n\n"
        "What you can do there:\n"
        "- upload missing files\n"
        "- review invoice status\n"
        "- leave notes for the legal team\n\n"
    )
    if message_text:
        text_body += f"Message from your legal team:\n{message_text}\n\n"
    text_body += "Best regards,\nLexFlow"

    message_block = ""
    if message_text:
        message_block = f"""
          <div style="margin:0 0 24px;padding:16px 18px;border:1px solid #dbe5f1;border-radius:16px;background:#f7fafc;">
            <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#5f6f85;margin:0 0 8px;">Message from your legal team</div>
            <div style="font-size:14px;line-height:1.7;color:#334155;">{escaped_message}</div>
          </div>
        """

    html_body = f"""\
<!DOCTYPE html>
<html lang="en">
  <body style="margin:0;padding:32px 16px;background:#f3f7fb;font-family:Inter,Segoe UI,Arial,sans-serif;color:#14213d;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;border-collapse:collapse;">
            <tr>
              <td style="padding:0 0 16px;">
                <div style="display:inline-flex;align-items:center;gap:10px;">
                  <span style="display:inline-flex;width:36px;height:36px;border-radius:12px;background:#2563eb;color:#ffffff;font-size:15px;font-weight:800;align-items:center;justify-content:center;">Lf</span>
                  <span style="font-size:18px;font-weight:700;color:#0f172a;">LexFlow</span>
                </div>
              </td>
            </tr>
            <tr>
              <td style="background:#ffffff;border:1px solid #dbe5f1;border-radius:28px;padding:32px;box-shadow:0 18px 60px rgba(15,23,42,0.08);">
                <div style="display:inline-block;padding:8px 12px;border-radius:999px;background:#eff6ff;color:#1d4ed8;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">Secure client portal</div>
                <h1 style="margin:18px 0 10px;font-size:30px;line-height:1.2;color:#0f172a;">Upload documents for your case</h1>
                <p style="margin:0 0 24px;font-size:15px;line-height:1.7;color:#475569;">
                  Hello {escaped_name}, your legal team prepared a secure LexFlow link for your
                  <strong>{escaped_case}</strong> case in <strong>{escaped_destination}</strong>.
                </p>

                <div style="margin:0 0 24px;padding:18px 20px;border:1px solid #dbe5f1;border-radius:18px;background:linear-gradient(180deg,#ffffff 0%,#f8fbff 100%);">
                  <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#5f6f85;margin:0 0 10px;">What you can do</div>
                  <div style="font-size:14px;line-height:1.7;color:#334155;">Upload requested documents, check invoice status, and leave a note for the legal team in one place.</div>
                </div>

                {message_block}

                <div style="margin:0 0 24px;text-align:center;">
                  <a href="{escaped_url}" style="display:inline-block;padding:15px 28px;border-radius:16px;background:#2563eb;color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;">Open secure upload page</a>
                </div>

                <div style="margin:0 0 24px;padding:16px 18px;border:1px solid #dbe5f1;border-radius:16px;background:#f8fafc;">
                  <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#5f6f85;margin:0 0 8px;">Direct link</div>
                  <div style="font-size:13px;line-height:1.7;color:#2563eb;word-break:break-all;">{escaped_url}</div>
                </div>

                <p style="margin:0;font-size:13px;line-height:1.7;color:#64748b;">
                  If something is unclear, reply to this email and your legal team will guide you.
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""
    return text_body, html_body


# ─── OCR helpers ────────────────────────────────────────
async def run_ocr(content: bytes, filename: str, content_type: str = "") -> dict:
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
    encoded = base64.b64encode(content).decode("utf-8")
    last_error = ""
    async with httpx.AsyncClient(timeout=90) as client:
        # Try document_base64 first (works for PDFs and images)
        try:
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
            raw_text, confidence, pages = mistral_ocr_to_raw_text(result)
            result["provider"] = "mistral"
            result["raw_text"] = raw_text
            result["confidence"] = confidence
            result["pages"] = pages
            result["local_attempt"] = local_result
            return result
        except Exception as e:
            last_error = str(e)
            print(f"Mistral OCR document_base64 failed: {e}")

        # Fallback for images: use image_url with data URL
        if (filename or "").lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            try:
                content_type = (content_type or "image/jpeg").split(";")[0].strip()
                if content_type not in ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"):
                    content_type = "image/jpeg"
                data_url = f"data:{content_type};base64,{encoded}"
                r = await client.post(
                    "https://api.mistral.ai/v1/ocr",
                    headers={"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": "mistral-ocr-latest",
                        "document": {
                            "type": "image_url",
                            "image_url": data_url,
                        },
                    },
                )
                r.raise_for_status()
                result = r.json()
                raw_text, confidence, pages = mistral_ocr_to_raw_text(result)
                result["provider"] = "mistral"
                result["raw_text"] = raw_text
                result["confidence"] = confidence
                result["pages"] = pages
                result["local_attempt"] = local_result
                return result
            except Exception as e:
                last_error = f"{last_error}; image_url fallback: {e}"
                print(f"Mistral OCR image_url fallback failed: {e}")

    return local_result if local_result.get("raw_text") else {"raw_text": "", "provider": "none", "pages": [], "error": last_error}


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


DOCUMENT_TYPE_ALIASES = {
    "passport": "passport",
    "residence_permit": "residence_permit",
    "employment": "employment_contract",
    "qualification": "qualification",
    "recognition_notice": "recognition_notice",
    "health_insurance": "health_insurance",
    "financial_proof": "financial_proof",
    "marriage_certificate": "marriage_certificate",
    "birth_certificate": "birth_certificate",
    "language_certificate": "language_certificate",
    "questionnaire": "questionnaire",
    "power_of_attorney": "power_of_attorney",
    "invoice": "invoice",
}


GERMANY_ROUTE_REQUIREMENTS = {
    "DE_BLUE_CARD": [
        {"code": "passport", "label": "Passport", "doc_types": ["passport"], "blocker": True},
        {"code": "employment", "label": "Employment contract or job offer", "doc_types": ["employment_contract"], "blocker": True},
        {"code": "qualification", "label": "Degree or qualification proof", "doc_types": ["qualification"], "blocker": True},
        {"code": "health", "label": "Health insurance", "doc_types": ["health_insurance"], "blocker": True},
    ],
    "DE_SKILLED_WORKER": [
        {"code": "passport", "label": "Passport", "doc_types": ["passport"], "blocker": True},
        {"code": "employment", "label": "Employment contract or job offer", "doc_types": ["employment_contract"], "blocker": True},
        {"code": "qualification", "label": "Recognised qualification", "doc_types": ["qualification", "recognition_notice"], "blocker": True},
        {"code": "health", "label": "Health insurance", "doc_types": ["health_insurance"], "blocker": True},
    ],
    "DE_FAMILY_REUNIFICATION_SPOUSE": [
        {"code": "passport", "label": "Passport", "doc_types": ["passport"], "blocker": True},
        {"code": "marriage", "label": "Marriage certificate", "doc_types": ["marriage_certificate"], "blocker": True},
        {"code": "housing", "label": "Housing or rental evidence", "doc_types": ["financial_proof"], "blocker": True},
        {"code": "income", "label": "Payslips or financial evidence", "doc_types": ["financial_proof"], "blocker": True},
    ],
    "DE_RECOGNITION": [
        {"code": "passport", "label": "Passport", "doc_types": ["passport"], "blocker": True},
        {"code": "recognition_notice", "label": "Recognition notice", "doc_types": ["recognition_notice"], "blocker": True},
        {"code": "qualification", "label": "Qualification evidence", "doc_types": ["qualification"], "blocker": True},
        {"code": "language", "label": "Language certificate", "doc_types": ["language_certificate"], "blocker": True},
        {"code": "funds", "label": "Proof of funds", "doc_types": ["financial_proof"], "blocker": True},
        {"code": "health", "label": "Health insurance", "doc_types": ["health_insurance"], "blocker": True},
    ],
    "EU_GENERAL": [
        {"code": "passport", "label": "Passport", "doc_types": ["passport"], "blocker": True},
        {"code": "questionnaire", "label": "Client questionnaire", "doc_types": ["questionnaire"], "blocker": False},
    ],
}


def canonical_document_type(raw_type: str, filename: str = "") -> str:
    normalized = DOCUMENT_TYPE_ALIASES.get(raw_type or "", raw_type or "unknown")
    if normalized != "unknown":
        return normalized
    lookup = normalize_lookup(filename)
    keyword_map = {
        "marriage": "marriage_certificate",
        "heirat": "marriage_certificate",
        "birth": "birth_certificate",
        "geburt": "birth_certificate",
        "insurance": "health_insurance",
        "kranken": "health_insurance",
        "diploma": "qualification",
        "degree": "qualification",
        "recognition": "recognition_notice",
        "anerkennung": "recognition_notice",
        "salary": "financial_proof",
        "payslip": "financial_proof",
        "bank": "financial_proof",
        "questionnaire": "questionnaire",
        "vollmacht": "power_of_attorney",
        "power attorney": "power_of_attorney",
        "contract": "employment_contract",
        "job offer": "employment_contract",
        "id card": "residence_permit",
        "id-card": "residence_permit",
        "national id": "residence_permit",
        "personalausweis": "residence_permit",
        "residence card": "residence_permit",
        "aufenthaltstitel": "residence_permit",
    }
    for keyword, doc_type in keyword_map.items():
        if keyword in lookup:
            return doc_type
    return "unknown"


def infer_route_code(case: dict) -> str:
    case_type = (case.get("case_type") or "").lower()
    destination = (case.get("destination") or "").lower()
    if "germany" in destination or "deutschland" in destination:
        if "blue card" in case_type:
            return "DE_BLUE_CARD"
        if "family" in case_type or "spouse" in case_type or "reunion" in case_type:
            return "DE_FAMILY_REUNIFICATION_SPOUSE"
        if "recognition" in case_type:
            return "DE_RECOGNITION"
        return "DE_SKILLED_WORKER"
    return "EU_GENERAL"


def describe_match_reason(reason: str) -> str:
    mapping = {
        "email": "Matched by sender email.",
        "passport": "Matched by passport number.",
        "dob": "Matched by date of birth.",
        "name": "Matched by extracted full name.",
        "employer": "Matched by employer information.",
        "filename": "Matched by client name in the filename.",
        "none": "No confident case match was found.",
    }
    parts = [mapping.get(part, f"Matched by {part}.") for part in (reason or "none").split(",") if part]
    return " ".join(dict.fromkeys(parts)) if parts else mapping["none"]


def document_is_active(doc: dict) -> bool:
    return doc.get("status") not in {"duplicate", "archived"}


def document_is_review_blocked(doc: dict) -> bool:
    return doc.get("status") in {"needs_review"} or bool(doc.get("manual_review_required"))


def make_notification_id(unique_key: str) -> str:
    return hashlib.sha256(unique_key.encode("utf-8")).hexdigest()[:24]


async def create_notification(*, actor: dict, case: dict, severity: str, title: str, message: str, kind: str, unique_key: str, payload: Optional[dict] = None):
    notification = {
        "id": make_notification_id(unique_key),
        "lawyer_id": case.get("lawyer_id") or actor["user"]["id"],
        "firm_id": case.get("firm_id") or actor["firm"]["id"],
        "case_id": case.get("id"),
        "severity": severity,
        "kind": kind,
        "title": title,
        "message": message,
        "payload": payload or {},
        "read_at": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    await db_upsert_notification(notification)
    return notification


async def log_case_event(case: dict, action: str, payload: Optional[dict] = None):
    await db_create_audit_event({
        "lawyer_id": case.get("lawyer_id"),
        "firm_id": case.get("firm_id"),
        "case_id": case.get("id"),
        "action": action,
        "payload": payload or {},
        "created_at": utc_now(),
    })


def build_case_control_state(case: dict, documents: list[dict], invoices: list[dict]) -> dict:
    route_code = infer_route_code(case)
    requirements = GERMANY_ROUTE_REQUIREMENTS.get(route_code, GERMANY_ROUTE_REQUIREMENTS["EU_GENERAL"])
    typed_docs: dict[str, list[dict]] = {}
    active_document_count = 0
    for doc in documents:
        doc_type = canonical_document_type(doc.get("document_type", ""), doc.get("name", ""))
        typed_docs.setdefault(doc_type, []).append(doc)
        if document_is_active(doc):
            active_document_count += 1
    requirement_states = []
    missing_codes = []
    missing_labels = []
    blocking_missing = []
    blocking_missing_labels = []
    completed_codes = []
    completed_labels = []
    review_codes = []
    review_labels = []
    open_reviews = 0
    risk_flags = []
    actions = []
    passport_numbers = {
        item.get("extracted", {}).get("passport_number")
        for item in typed_docs.get("passport", [])
        if item.get("extracted", {}).get("passport_number")
    }
    if len(passport_numbers) > 1:
        risk_flags.append({"code": "identity_conflict", "severity": "high", "message": "Multiple passport numbers detected across uploaded passport files."})
    for requirement in requirements:
        matched_docs = [
            doc for doc_type in requirement["doc_types"]
            for doc in typed_docs.get(doc_type, [])
            if document_is_active(doc)
        ]
        review_docs = [doc for doc in matched_docs if document_is_review_blocked(doc)]
        if matched_docs and not review_docs:
            state = "complete"
            completed_codes.append(requirement["code"])
            completed_labels.append(requirement["label"])
        elif matched_docs:
            state = "needs_review"
            open_reviews += 1
            review_codes.append(requirement["code"])
            review_labels.append(requirement["label"])
        else:
            state = "missing"
            missing_codes.append(requirement["code"])
            missing_labels.append(requirement["label"])
            if requirement["blocker"]:
                blocking_missing.append(requirement["code"])
                blocking_missing_labels.append(requirement["label"])
        requirement_states.append({
            "code": requirement["code"],
            "label": requirement["label"],
            "state": state,
            "blocker": requirement["blocker"],
            "document_ids": [doc.get("id") for doc in matched_docs],
        })
    for doc in documents:
        extracted = doc.get("extracted", {}) or {}
        confidence = float(extracted.get("confidence") or extracted.get("ocr_confidence") or 0)
        if confidence and confidence < 0.55 and document_is_active(doc):
            risk_flags.append({"code": f"quality:{doc['id']}", "severity": "medium", "message": f"{doc.get('name')} is low-quality and needs manual review."})
        expiry = parse_date(extracted.get("expiry_date", ""))
        if expiry:
            days_left = (expiry - datetime.now(timezone.utc).date()).days
            if days_left <= 0:
                risk_flags.append({"code": f"expired:{doc['id']}", "severity": "high", "message": f"{doc.get('name')} appears expired."})
            elif days_left <= 180:
                risk_flags.append({"code": f"expiring:{doc['id']}", "severity": "medium", "message": f"{doc.get('name')} expires in {days_left} days."})
    unrecognized_count = len([doc for doc in documents if doc.get("status") == "unrecognized"])
    duplicate_count = len([doc for doc in documents if doc.get("status") == "duplicate"])
    if duplicate_count:
        actions.append({"priority": "medium", "label": "Resolve duplicate files", "message": f"{duplicate_count} duplicate file(s) detected."})
    if open_reviews:
        actions.append({"priority": "high", "label": "Manual review required", "message": f"{open_reviews} requirement(s) are blocked by review items."})
    if blocking_missing:
        actions.append({"priority": "high", "label": "Request missing documents", "message": f"Missing blockers: {', '.join(blocking_missing)}."})
    if "questionnaire" in missing_codes:
        actions.append({"priority": "medium", "label": "Send client questionnaire", "message": "The client questionnaire is still missing."})
    latest_invoice = invoices[0] if invoices else None
    billing_complete = bool(case.get("invoice_paid")) or (latest_invoice and latest_invoice.get("status") in {"signed", "paid"})
    if latest_invoice and not billing_complete:
        actions.append({"priority": "medium", "label": "Follow up on invoice", "message": f"Invoice {latest_invoice.get('number', latest_invoice.get('id', 'draft'))} still needs client action."})
    auto_stage = case.get("stage", "documents")
    if not blocking_missing and open_reviews == 0:
        if latest_invoice and not billing_complete:
            auto_stage = "payment"
        elif billing_complete:
            auto_stage = "processing"
        else:
            auto_stage = "review"
    high_risk_count = len([flag for flag in risk_flags if flag.get("severity") == "high"])
    medium_risk_count = len([flag for flag in risk_flags if flag.get("severity") == "medium"])
    priority_reasons = []
    auto_priority = "medium"
    if high_risk_count:
        auto_priority = "high"
        priority_reasons.append("Critical risk flags detected in case documents.")
    elif open_reviews:
        auto_priority = "high"
        priority_reasons.append("Manual review items are blocking protocol completion.")
    elif unrecognized_count:
        auto_priority = "high"
        priority_reasons.append("Unrecognized intake files need assignment before progress continues.")
    elif (case.get("stage") in {"processing", "review", "submitted"} or auto_stage in {"processing", "review", "submitted"}) and not blocking_missing and open_reviews == 0 and billing_complete:
        auto_priority = "low"
        priority_reasons.append("Case is structurally clean and moving without blockers.")
    elif blocking_missing and active_document_count > 0:
        auto_priority = "medium"
        priority_reasons.append("Required blocker documents are still missing.")
    elif latest_invoice and not billing_complete:
        auto_priority = "medium"
        priority_reasons.append("Client action is pending on the invoice.")
    elif duplicate_count:
        auto_priority = "medium"
        priority_reasons.append("Duplicate files should be resolved to keep the case clean.")
    elif medium_risk_count:
        auto_priority = "medium"
        priority_reasons.append("Medium protocol risks should be checked by the team.")
    elif blocking_missing:
        auto_priority = "medium"
        priority_reasons.append("Initial document package is still incomplete.")
    else:
        priority_reasons.append("Case is in regular active intake.")
    next_step = actions[0]["label"] if actions else "Ready for legal review"
    request_line = ""
    if blocking_missing_labels:
        request_line = f"Please upload: {', '.join(blocking_missing_labels)}."
    elif missing_labels:
        request_line = f"Recommended next uploads: {', '.join(missing_labels)}."
    return {
        "route_code": route_code,
        "requirements": requirement_states,
        "missing_codes": missing_codes,
        "missing_labels": missing_labels,
        "blocking_missing_codes": blocking_missing,
        "blocking_missing_labels": blocking_missing_labels,
        "completed_codes": completed_codes,
        "completed_labels": completed_labels,
        "review_codes": review_codes,
        "review_labels": review_labels,
        "open_review_count": open_reviews,
        "unrecognized_count": unrecognized_count,
        "duplicate_count": duplicate_count,
        "risk_flags": risk_flags,
        "actions": actions,
        "latest_invoice_id": latest_invoice.get("id") if latest_invoice else None,
        "billing_complete": bool(billing_complete),
        "auto_stage": auto_stage,
        "auto_priority": auto_priority,
        "priority_reasons": priority_reasons,
        "next_step": next_step,
        "document_plan": {
            "required_documents": [item["label"] for item in requirement_states],
            "completed_documents": completed_labels,
            "missing_documents": missing_labels,
            "review_documents": review_labels,
            "recommended_request": request_line,
        },
        "updated_at": utc_now(),
    }


def build_intake_decision(*, matched_case: Optional[dict], match_reason: str, match_score: float, status: str, document_type: str, auto_created: bool, duplicate: bool, fields: dict) -> dict:
    confidence = float(fields.get("confidence") or 0)
    if duplicate:
        action = "ignore_as_duplicate"
    elif auto_created:
        action = "create_case_and_attach"
    elif matched_case and status == "assigned":
        action = "attach_to_existing_case"
    elif matched_case and status == "needs_review":
        action = "attach_with_review"
    else:
        action = "hold_for_manual_review"

    reasons = []
    if match_reason and match_reason != "none":
        reasons.append(describe_match_reason(match_reason))
    if document_type != "unknown":
        reasons.append(f"Document classified as {document_type.replace('_', ' ')}.")
    if confidence:
        reasons.append(f"Extraction confidence {round(confidence, 2)}.")
    if status == "needs_review":
        reasons.append("The document needs human review before it can fully unblock the case.")
    if status == "unrecognized":
        reasons.append("The system could not confidently assign this document to a case.")

    return {
        "action": action,
        "matched_case_id": matched_case.get("id") if matched_case else None,
        "match_reason": match_reason,
        "match_score": round(match_score, 3),
        "document_type": document_type,
        "extraction_confidence": round(confidence, 2),
        "status": status,
        "auto_created_case": auto_created,
        "explanation": " ".join(reasons).strip(),
    }


async def refresh_case_control(case_id: str, *, trigger: str = "system") -> Optional[dict]:
    case = await db_get_case(case_id)
    if not case:
        return None
    docs = await db_get_documents(case_id=case_id)
    invoices = await db_get_invoices(case_id=case_id)
    control_state = build_case_control_state(case, docs, invoices)
    patch = {
        "updated_at": utc_now(),
        "control_state": control_state,
        "route_code": control_state["route_code"],
        "priority": control_state["auto_priority"],
    }
    previous_stage = case.get("stage", "documents")
    previous_priority = case.get("priority", "medium")
    if control_state["auto_stage"] != previous_stage:
        patch["stage"] = control_state["auto_stage"]
    updated_case = await db_update_case(case_id, patch) or {**case, **patch}
    actor = await ensure_actor_context({
        "id": case.get("lawyer_id") or DEFAULT_LAWYER_ID,
        "email": case.get("client_email") or f"{case.get('lawyer_id') or DEFAULT_LAWYER_ID}@lexflow.local",
        "name": case.get("client_name") or "LexFlow case owner",
    })
    if patch.get("stage") and patch["stage"] != previous_stage:
        await log_case_event(updated_case, "case.stage.auto_advanced", {
            "from": previous_stage,
            "to": patch["stage"],
            "trigger": trigger,
            "route_code": control_state["route_code"],
        })
        await create_notification(
            actor=actor,
            case=updated_case,
            severity="info",
            kind="workflow",
            title="Case moved automatically",
            message=f"Case moved from {previous_stage} to {patch['stage']} after protocol checks passed.",
            unique_key=f"stage:{case_id}:{patch['stage']}",
            payload={"from": previous_stage, "to": patch["stage"], "trigger": trigger},
        )
    if patch.get("priority") and patch["priority"] != previous_priority:
        await log_case_event(updated_case, "case.priority.auto_updated", {
            "from": previous_priority,
            "to": patch["priority"],
            "trigger": trigger,
            "route_code": control_state["route_code"],
        })
        await create_notification(
            actor=actor,
            case=updated_case,
            severity="info",
            kind="priority_update",
            title="Case priority updated",
            message=f"Case priority changed from {previous_priority} to {patch['priority']} based on protocol signals.",
            unique_key=f"priority:{case_id}:{patch['priority']}",
            payload={"from": previous_priority, "to": patch["priority"], "trigger": trigger},
        )
    for action in control_state["actions"]:
        await create_notification(
            actor=actor,
            case=updated_case,
            severity=action["priority"],
            kind="action_required",
            title=action["label"],
            message=action["message"],
            unique_key=f"action:{case_id}:{action['label']}",
            payload={"route_code": control_state["route_code"], "trigger": trigger},
        )
    for flag in control_state["risk_flags"]:
        await create_notification(
            actor=actor,
            case=updated_case,
            severity=flag["severity"],
            kind="risk_flag",
            title="Attention required",
            message=flag["message"],
            unique_key=f"risk:{case_id}:{flag['code']}",
            payload={"route_code": control_state["route_code"], "trigger": trigger},
        )
    return updated_case


async def find_case_for_document(sender_email: str, filename: str, fields: dict, cases: list[dict]) -> tuple[Optional[dict], str, float]:
    sender = (sender_email or "").lower().strip()
    if sender:
        for case in cases:
            if case.get("client_email", "").lower().strip() == sender:
                return case, "email", 0.98

    documents = await db_get_documents()
    docs_by_case: dict[str, list[dict]] = {}
    for document in documents:
        case_id = document.get("case_id")
        if not case_id:
            continue
        docs_by_case.setdefault(case_id, []).append(document)

    incoming_name = normalize_identity_text(fields.get("full_name", ""))
    incoming_passport = normalize_identity_code(fields.get("passport_no", fields.get("passport_number", "")))
    incoming_dob = fields.get("dob") or fields.get("date_of_birth", "")
    incoming_employer = normalize_identity_text(fields.get("employer_name", fields.get("employer", "")))

    def collect_case_identities(case: dict) -> dict:
        extracted_sources = [case.get("extracted", {}) or {}]
        for item in docs_by_case.get(case.get("id", ""), []):
            extracted_sources.append(item.get("extracted", {}) or {})

        names = {
            normalize_identity_text(value)
            for value in [case.get("client_name", "")] + [src.get("full_name", "") for src in extracted_sources]
            if normalize_identity_text(value)
        }
        emails = {
            (value or "").lower().strip()
            for value in [case.get("client_email", "")] + [src.get("email", "") for src in extracted_sources]
            if (value or "").strip()
        }
        passports = {
            normalize_identity_code(src.get("passport_no", src.get("passport_number", "")))
            for src in extracted_sources
            if normalize_identity_code(src.get("passport_no", src.get("passport_number", "")))
        }
        dobs = {
            (src.get("dob") or src.get("date_of_birth", ""))
            for src in extracted_sources
            if (src.get("dob") or src.get("date_of_birth", ""))
        }
        employers = {
            normalize_identity_text(src.get("employer_name", src.get("employer", "")))
            for src in extracted_sources
            if normalize_identity_text(src.get("employer_name", src.get("employer", "")))
        }
        return {
            "names": names,
            "emails": emails,
            "passports": passports,
            "dobs": dobs,
            "employers": employers,
        }

    scored_matches = []
    for case in cases:
        identities = collect_case_identities(case)
        score = 0.0
        reasons: list[str] = []

        if sender and sender in identities["emails"]:
            score += 0.99
            reasons.append("email")
        if incoming_passport and incoming_passport in identities["passports"]:
            score += 0.97
            reasons.append("passport")
        if incoming_dob and incoming_dob in identities["dobs"]:
            score += 0.55
            reasons.append("dob")
        if incoming_name and incoming_name in identities["names"]:
            score += max(0.4, float(fields.get("confidence") or 0.7))
            reasons.append("name")
        if incoming_employer and incoming_employer in identities["employers"]:
            score += 0.28
            reasons.append("employer")

        if "passport" in reasons and "dob" in reasons:
            score += 0.2
        if "name" in reasons and "dob" in reasons:
            score += 0.14
        if "email" in reasons and "name" in reasons:
            score += 0.08

        if score > 0:
            scored_matches.append((case, ",".join(reasons), min(score, 0.995)))

    if scored_matches:
        best_case, best_reason, best_score = sorted(scored_matches, key=lambda item: item[2], reverse=True)[0]
        if best_score >= 0.78:
            return best_case, best_reason, round(best_score, 3)

    filename_lookup = normalize_lookup(filename)
    for case in cases:
        parts = normalize_lookup(case.get("client_name", "")).split()
        if parts and all(part in filename_lookup for part in parts):
            return case, "filename", 0.75

    return None, "none", 0.0


def should_auto_create_case(fields: dict, sender_email: str) -> bool:
    confidence = float(fields.get("confidence") or 0)
    has_email = bool((sender_email or fields.get("email") or "").strip())
    has_name = bool(normalize_identity_text(fields.get("full_name", "")))
    has_passport = bool(normalize_identity_code(fields.get("passport_no", fields.get("passport_number", ""))))
    has_dob = bool(fields.get("dob") or fields.get("date_of_birth", ""))
    has_employer = bool(normalize_identity_text(fields.get("employer_name", fields.get("employer", ""))))
    strong_identity = has_passport or (has_name and has_dob) or (has_name and has_email) or (has_name and has_employer)
    return confidence >= 0.45 and strong_identity


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
        "priority": "medium",
        "invoice_paid": False,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "docs": [],
        "invoice": None,
        "extracted": fields,
        "portal_url": f"{FRONTEND_URL}/client-upload.html?id={case_id}",
        "route_code": infer_route_code({"case_type": infer_case_type(fields.get("document_type", ""), subject), "destination": infer_destination(" ".join(str(value) for value in fields.values()))}),
        "control_state": {},
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
    if duplicate and duplicate.get("status") == "duplicate":
        origin_id = parse_duplicate_origin_id(duplicate.get("automation_note", ""))
        origin_doc = await db_get_document(origin_id) if origin_id and origin_id != duplicate.get("id") else None
        duplicate = origin_doc
    if duplicate:
        duplicate_case = await db_get_case(duplicate.get("case_id")) if duplicate.get("case_id") else None
        duplicate_fields = duplicate.get("extracted", {}) or {}
        duplicate_document_type = canonical_document_type(duplicate.get("document_type", "unknown"), duplicate.get("name", filename))
        intake_decision = build_intake_decision(
            matched_case=duplicate_case,
            match_reason="email" if sender_email and duplicate_case else "none",
            match_score=0.98 if sender_email and duplicate_case else 0.0,
            status="duplicate",
            document_type=duplicate_document_type,
            auto_created=False,
            duplicate=True,
            fields=duplicate_fields,
        )
        return {
            "case": duplicate_case,
            "document": {
                **duplicate,
                "status": "duplicate",
                "automation_note": f"duplicate_of:{duplicate.get('id')}",
                "automation_decision": intake_decision,
            },
            "duplicate": True,
            "auto_created_case": False,
            "decision": intake_decision,
        }
    ocr = await run_ocr(content, filename, content_type)
    raw_text = ocr.get("raw_text", "")
    fields = parse_document_text(raw_text, filename, subject)
    fields["ocr_provider"] = ocr.get("provider", "none")
    fields["ocr_confidence"] = ocr.get("confidence", 0)
    fields["ocr_raw_text"] = raw_text
    fields["ocr_error"] = ocr.get("error", "")
    document_type = canonical_document_type(fields.get("document_type", "unknown"), filename)
    fields["document_type"] = document_type

    cases = await db_get_cases()
    matched_case, match_reason, match_score = await find_case_for_document(sender_email, filename, fields, cases)
    auto_created = False
    if not matched_case and should_auto_create_case(fields, sender_email):
        matched_case = await create_case_from_document(sender_email, subject, fields, user_id)
        auto_created = True

    case_id = matched_case["id"] if matched_case else "unrecognized"
    type_exists = await case_has_document_type(matched_case, document_type) if matched_case else False
    status = "assigned" if matched_case else "unrecognized"
    automation_note = "routed"
    manual_review_required = False
    if type_exists:
        status = "needs_review"
        automation_note = f"document_type_already_exists:{document_type}"
        manual_review_required = True
    elif matched_case and document_type == "unknown" and float(fields.get("confidence") or 0) < 0.55:
        status = "needs_review"
        automation_note = "low_confidence_scan"
        manual_review_required = True
    uploaded = await upload_bytes(case_id, filename, content_type, content, source=source)

    intake_decision = build_intake_decision(
        matched_case=matched_case,
        match_reason=match_reason,
        match_score=match_score,
        status=status,
        document_type=document_type,
        auto_created=auto_created,
        duplicate=False,
        fields=fields,
    )

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
        "document_family": document_type,
        "automation_status": "auto_created_case" if auto_created else match_reason,
        "automation_note": automation_note,
        "automation_decision": intake_decision,
        "match_reason": match_reason,
        "match_score": round(match_score, 3),
        "manual_review_required": manual_review_required,
        "quality_status": "poor" if document_type == "unknown" and float(fields.get("confidence") or 0) < 0.55 else "acceptable",
        "authenticity_status": "pending" if document_type in {"marriage_certificate", "birth_certificate"} else "not_applicable",
        "translation_status": "pending_review" if document_type in {"marriage_certificate", "birth_certificate", "qualification", "recognition_notice"} else "not_applicable",
        "extracted": fields,
        "uploaded_at": utc_now(),
        "updated_at": utc_now(),
    }
    saved = await db_create_document(document)
    updated_case = matched_case
    if matched_case and status != "duplicate":
        docs = matched_case.get("docs", []) + [{**uploaded, "document_id": saved["id"], "uploaded_at": saved["uploaded_at"], "document_type": document_type, "status": status}]
        updated_case = await db_update_case(matched_case["id"], {
            "docs": docs,
            "extracted": {**matched_case.get("extracted", {}), **fields},
            "last_intake_decision": intake_decision,
            "updated_at": utc_now(),
        }) or {**matched_case, "docs": docs, "extracted": {**matched_case.get("extracted", {}), **fields}, "last_intake_decision": intake_decision}
        updated_case = await refresh_case_control(matched_case["id"], trigger=f"document_routed:{source}") or updated_case
        await log_case_event(updated_case, "document_intake_routed", {
            "document_id": saved["id"],
            "document_type": document_type,
            "status": status,
            "decision": intake_decision,
        })
    await store_extraction_evaluation(matched_case["id"] if matched_case else "", saved["id"], fields)
    return {
        "document": saved,
        "case": updated_case,
        "auto_created_case": auto_created,
        "match_reason": match_reason,
        "match_score": match_score,
        "duplicate": bool(duplicate),
        "decision": intake_decision,
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
    normalized = []
    for row in rows:
        current = row
        if row.get("provider") == "zoho" and row.get("auth_type") == "oauth":
            try:
                ready = await ensure_zoho_integration_access_token(row)
                profile = await fetch_zoho_account_profile(ready["access_token"])
                if profile["email"] != ready.get("email") or profile["account_id"] != str(ready.get("account_id") or ""):
                    current = {
                        **ready,
                        "email": profile["email"],
                        "account_id": profile["account_id"],
                        "updated_at": utc_now(),
                    }
                    current = await db_upsert_email_integration(current)
                else:
                    current = ready
            except Exception:
                current = row
        normalized.append(mask_integration(current))
    return normalized


@app.get("/api/email-integrations/debug")
async def email_integrations_debug(user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    rows = await db_get_email_integrations(firm_id=actor["firm"]["id"])
    runtime_ids = {
        item.get("id")
        for item in pick_runtime_email_integrations(rows)
    }
    bucket = getattr(app.state, "email_poll_debug", {}) or {}
    return {
        "generated_at": utc_now(),
        "auto_poll_enabled": EMAIL_AUTO_POLL_ENABLED,
        "auto_poll_interval_seconds": EMAIL_AUTO_POLL_INTERVAL_SECONDS,
        "auto_poll_idle_interval_seconds": EMAIL_AUTO_POLL_IDLE_INTERVAL_SECONDS,
        "integrations": [
            {
                **mask_integration(row),
                "runtime_selected": row.get("id") in runtime_ids,
                "poll_debug": bucket.get(row.get("id"), {}),
            }
            for row in rows
        ],
    }


@app.get("/api/public/email-integrations/debug")
async def public_email_integrations_debug(email: str):
    target = (email or "").lower().strip()
    if not target:
        raise HTTPException(status_code=400, detail="Email is required")
    all_rows = await db_get_email_integrations()
    rows = [row for row in all_rows if (row.get("email") or "").lower().strip() == target]
    match = rows[0] if rows else None
    if not match:
        return {
            "generated_at": utc_now(),
            "found": False,
            "email": target,
        }
    runtime_ids = {
        item.get("id")
        for item in pick_runtime_email_integrations(all_rows)
    }
    bucket = getattr(app.state, "email_poll_debug", {}) or {}
    debug = bucket.get(match.get("id"), {})
    return {
        "generated_at": utc_now(),
        "found": True,
        "email": target,
        "provider": match.get("provider"),
        "auth_type": match.get("auth_type"),
        "active": match.get("active"),
        "runtime_selected": match.get("id") in runtime_ids,
        "last_polled_at": match.get("last_polled_at"),
        "last_processed_message_id": match.get("last_processed_message_id") or "",
        "matching_rows": [
            {
                "id": row.get("id"),
                "provider": row.get("provider"),
                "auth_type": row.get("auth_type"),
                "active": row.get("active"),
                "updated_at": row.get("updated_at"),
            }
            for row in rows[:10]
        ],
        "poll_debug": {
            "status": debug.get("status", ""),
            "messages_seen": debug.get("messages_seen", 0),
            "messages_with_attachments": debug.get("messages_with_attachments", 0),
            "messages_without_attachments": debug.get("messages_without_attachments", 0),
            "attachments_total": debug.get("attachments_total", 0),
            "processed_count": debug.get("processed_count", 0),
            "last_error": debug.get("last_error", ""),
            "last_polled_at": debug.get("last_polled_at", ""),
            "last_processed_message_id": debug.get("last_processed_message_id", ""),
            "mark_read_failures": debug.get("mark_read_failures", []),
        },
    }


@app.post("/api/public/email-integrations/debug/poll")
async def public_email_integrations_debug_poll(email: str):
    target = (email or "").lower().strip()
    if not target:
        raise HTTPException(status_code=400, detail="Email is required")
    rows = await db_get_email_integrations()
    match = next((row for row in rows if (row.get("email") or "").lower().strip() == target), None)
    if not match:
        raise HTTPException(status_code=404, detail="Integration not found")
    result = await run_email_poll_for_integrations([match], continue_on_error=True)
    return {
        "generated_at": utc_now(),
        "email": target,
        **result,
    }


@app.get("/api/public/email-integrations/debug/messages")
async def public_email_integrations_debug_messages(mailbox_email: str = Query(..., alias="email"), limit: int = 5):
    target = (mailbox_email or "").lower().strip()
    if not target:
        raise HTTPException(status_code=400, detail="Email is required")
    rows = await db_get_email_integrations()
    match = next((row for row in rows if (row.get("email") or "").lower().strip() == target), None)
    if not match:
        raise HTTPException(status_code=404, detail="Integration not found")
    limit = max(1, min(int(limit or 5), 10))

    if match.get("provider") == "zoho" and match.get("auth_type") == "oauth":
        ready = await ensure_zoho_integration_access_token(match)
        account_id = ready.get("account_id") or (await fetch_zoho_account_profile(ready["access_token"]))["account_id"]
        folder_id = await zoho_inbox_folder_id(ready["access_token"], account_id)
        response = await zoho_api_get(
            f"/api/accounts/{account_id}/messages/view",
            ready["access_token"],
            params={
                "folderId": folder_id,
                "limit": limit,
                "sortorder": "false",
            },
        )
        items = response.json().get("data") or []
        inspected = []
        for item in items[:limit]:
            message_id = str(item.get("messageId") or "")
            raw_response = await zoho_api_get(
                f"/api/accounts/{account_id}/messages/{message_id}/originalmessage",
                ready["access_token"],
            ) if message_id else None
            mime_payload = (((raw_response.json() or {}).get("data") or {}).get("content") or "") if raw_response else ""
            message = email.message_from_string(mime_payload) if mime_payload else None
            attachments = extract_gmail_attachments(message) if message else []
            inspected.append({
                "message_id": message_id,
                "thread_id": str(item.get("threadId") or ""),
                "api_subject": item.get("subject") or "",
                "mime_subject": message.get("Subject", "") if message else "",
                "api_from": item.get("senderAddress") or item.get("fromAddress") or "",
                "mime_from": parseaddr(message.get("From", ""))[1] if message else "",
                "is_unread_inferred": is_zoho_message_unread(item),
                "raw_flags": {
                    "isUnread": item.get("isUnread"),
                    "unread": item.get("unread"),
                    "read": item.get("read"),
                    "status": item.get("status"),
                    "messageStatus": item.get("messageStatus"),
                },
                "attachment_count": len(attachments),
                "attachment_names": [att.filename for att in attachments[:10]],
            })
        return {
            "generated_at": utc_now(),
            "email": target,
            "provider": "zoho",
            "messages": inspected,
        }

    raise HTTPException(status_code=400, detail="Message inspection is only enabled for Zoho OAuth debug right now")


@app.get("/api/public/debug-email-integrations-all")
async def public_debug_email_integrations_all(limit: int = 20):
    rows = await db_get_email_integrations()
    limit = max(1, min(int(limit or 20), 100))
    return {
        "generated_at": utc_now(),
        "count": len(rows),
        "db_debug": getattr(app.state, "email_integration_db_debug", {}) or {},
        "items": [
            {
                "id": row.get("id"),
                "email": row.get("email"),
                "provider": row.get("provider"),
                "auth_type": row.get("auth_type"),
                "active": row.get("active"),
                "lawyer_id": row.get("lawyer_id"),
                "firm_id": row.get("firm_id"),
                "updated_at": row.get("updated_at"),
                "last_polled_at": row.get("last_polled_at"),
            }
            for row in rows[:limit]
        ],
    }


def is_probable_email_junk_document(doc: dict) -> bool:
    if (doc.get("source") or "") != "email":
        return False
    if (doc.get("status") or "") == "duplicate":
        return True
    if doc.get("case_id"):
        return False
    if (doc.get("status") or "") != "unrecognized":
        return False
    name = (doc.get("name") or "").lower().strip()
    if re.fullmatch(r"\d{10,}_\d+\.(png|jpg|jpeg|webp)", name):
        return True
    if name.startswith(("email-attachment-", "image00", "logo", "banner")):
        return True
    return False


@app.post("/api/public/intake/cleanup-junk")
async def public_cleanup_intake_junk():
    docs = await db_get_documents()
    targets = [doc for doc in docs if is_probable_email_junk_document(doc)]
    deleted = []
    for doc in targets:
        removed = await remove_document_everywhere(doc["id"])
        if removed:
            deleted.append({
                "id": removed["id"],
                "name": removed.get("name", ""),
                "status": removed.get("status", ""),
                "case_id": removed.get("case_id"),
            })
    return {
        "generated_at": utc_now(),
        "count": len(deleted),
        "deleted": deleted,
    }


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


@app.post("/api/email-integrations/zoho/start")
async def start_zoho_email_integration(user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    if not ZOHO_OAUTH_CLIENT_ID or not ZOHO_OAUTH_CLIENT_SECRET:
        raise HTTPException(status_code=400, detail="Zoho OAuth is not configured on the backend")
    state = sign_zoho_state({
        "user_id": user["id"],
        "firm_id": actor["firm"]["id"],
        "next": f"{FRONTEND_URL.rstrip('/')}/settings.html",
        "exp": int(datetime.now(timezone.utc).timestamp()) + 600,
    })
    query = urlencode({
        "client_id": ZOHO_OAUTH_CLIENT_ID,
        "redirect_uri": zoho_oauth_callback_url(),
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": "ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.READ,ZohoMail.messages.UPDATE",
        "state": state,
    })
    return {"auth_url": f"{ZOHO_ACCOUNTS_BASE}/oauth/v2/auth?{query}"}


# ─── Cases ─────────────────────────────────────────────
class CreateCase(BaseModel):
    client_name: str
    client_email: str
    case_type: str
    destination: str
    notes: Optional[str] = ""


class AssignDocumentRequest(BaseModel):
    case_id: str


class UpdateDocumentRequest(BaseModel):
    status: Optional[str] = None
    document_type: Optional[str] = None
    manual_review_required: Optional[bool] = None
    quality_status: Optional[str] = None
    authenticity_status: Optional[str] = None
    translation_status: Optional[str] = None
    notes: Optional[str] = None


class PortalInviteRequest(BaseModel):
    to: Optional[str] = None
    subject: Optional[str] = None
    message: Optional[str] = None


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
        "priority": "medium",
        "invoice_paid": False,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "docs": [],
        "invoice": None,
        "extracted": {},
        "portal_url": f"{FRONTEND_URL}/client-upload.html?id={case_id}",
        "route_code": infer_route_code({"case_type": req.case_type, "destination": req.destination}),
        "control_state": {},
    }
    await db_create_case(case)
    return await refresh_case_control(case_id, trigger="case_created") or case


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
    priority: Optional[str] = None
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
    if any(key in patch_data for key in ("case_type", "destination", "stage", "priority", "invoice_paid", "extracted", "public_notes")):
        try:
            refreshed = await refresh_case_control(case_id, trigger="case_patch")
            if refreshed:
                updated = refreshed
        except Exception as e:
            print(f"Case control refresh failed after patch {case_id}: {e}")
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
    await refresh_case_control(case_id, trigger="public_submit")
    return {"submitted": True, "case": updated}


@app.post("/api/cases/{case_id}/send-portal-invite")
async def send_portal_invite(case_id: str, req: PortalInviteRequest, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    case = await db_get_case(case_id)
    if not case or not record_belongs_to_actor(case, actor):
        raise HTTPException(status_code=404, detail="Case not found")

    to_email = (req.to or case.get("client_email") or "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="Client email is missing")

    portal_url = case.get("portal_url") or f"{FRONTEND_URL}/client-upload.html?id={case_id}"
    subject = (req.subject or f"Secure upload link for your {case.get('case_type') or 'LexFlow'} case").strip()
    text_body, html_body = build_portal_invite_email(case, portal_url, req.message or "")
    result = send_email_message(to_email, subject, text_body, html_body=html_body)

    event = {
        "id": str(uuid.uuid4()),
        "case_id": case_id,
        "type": "portal_invite_sent",
        "title": "Client portal invite sent",
        "message": f"Portal invite sent to {to_email}",
        "created_at": utc_now(),
        "meta": {
            "to": to_email,
            "subject": subject,
            "portal_url": portal_url,
        },
    }
    await db_create_audit_event(event)
    await db_update_case(case_id, {"updated_at": utc_now()})

    return {
        **result,
        "case_id": case_id,
        "to": to_email,
        "subject": subject,
        "portal_url": portal_url,
        "preview_html": html_body,
        "preview_text": text_body,
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
    content = await file.read()
    content_hash = hashlib.sha256(content).hexdigest()
    duplicate = await db_find_document_by_hash(content_hash)
    if duplicate and duplicate.get("status") == "duplicate":
        origin_id = parse_duplicate_origin_id(duplicate.get("automation_note", ""))
        origin_doc = await db_get_document(origin_id) if origin_id and origin_id != duplicate.get("id") else None
        duplicate = origin_doc or duplicate
    if duplicate:
        updated_case = await db_get_case(case_id)
        return {
            "name": duplicate.get("name", file.filename or "document.pdf"),
            "key": duplicate.get("key", ""),
            "url": duplicate.get("url", ""),
            "source": duplicate.get("source", "lawyer" if user else "client"),
            "status": "duplicate",
            "size": duplicate.get("size", len(content)),
            "content_type": duplicate.get("content_type", file.content_type or "application/octet-stream"),
            "document_id": duplicate.get("id", ""),
            "document_status": "duplicate",
            "document_type": duplicate.get("document_type", "unknown"),
            "extracted": duplicate.get("extracted", {}) or {},
            "case": updated_case or case,
        }
    upload = await upload_bytes(case_id, file.filename or "document.pdf", file.content_type or "application/octet-stream", content, source="lawyer" if user else "client")
    ocr = await run_ocr(content, file.filename or "document.pdf", file.content_type or "application/octet-stream")
    extracted = parse_document_text(ocr.get("raw_text", ""), file.filename or "document.pdf")
    extracted["ocr_provider"] = ocr.get("provider", "none")
    extracted["ocr_confidence"] = ocr.get("confidence", 0)
    document_type = canonical_document_type(extracted.get("document_type", "unknown"), file.filename or "document.pdf")
    extracted["document_type"] = document_type
    document_id = str(uuid.uuid4())
    type_exists = await case_has_document_type(case, document_type)
    status = "assigned"
    manual_review_required = False
    automation_note = "case_upload"
    if type_exists:
        status = "needs_review"
        automation_note = f"document_type_already_exists:{document_type}"
        manual_review_required = True
    elif document_type == "unknown" and float(extracted.get("confidence") or 0) < 0.55:
        status = "needs_review"
        automation_note = "low_confidence_scan"
        manual_review_required = True
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
        "status": status,
        "content_type": upload.get("content_type", file.content_type or "application/octet-stream"),
        "size": upload.get("size", 0),
        "content_hash": content_hash,
        "document_type": document_type,
        "document_family": document_type,
        "automation_status": "case_upload",
        "automation_note": automation_note,
        "manual_review_required": manual_review_required,
        "quality_status": "poor" if document_type == "unknown" and float(extracted.get("confidence") or 0) < 0.55 else "acceptable",
        "authenticity_status": "pending" if document_type in {"marriage_certificate", "birth_certificate"} else "not_applicable",
        "translation_status": "pending_review" if document_type in {"marriage_certificate", "birth_certificate", "qualification", "recognition_notice"} else "not_applicable",
        "extracted": extracted,
        "uploaded_at": utc_now(),
        "updated_at": utc_now(),
    }
    saved_doc = await db_create_document(document)
    docs = case.get("docs", []) + [{**upload, "document_id": saved_doc["id"], "uploaded_at": saved_doc["uploaded_at"], "document_type": document_type, "status": status}]
    await db_update_case(case_id, {"docs": docs, "extracted": {**case.get("extracted", {}), **extracted}, "updated_at": utc_now()})
    await store_extraction_evaluation(case_id, saved_doc["id"], extracted)
    await refresh_case_control(case_id, trigger=f"case_upload:{document_type}")
    updated_case = await db_get_case(case_id)
    return {
        **upload,
        "document_id": saved_doc["id"],
        "document_status": status,
        "document_type": document_type,
        "extracted": extracted,
        "case": updated_case or case,
    }


@app.get("/api/documents")
async def list_documents(status: Optional[str] = None, case_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    docs = await db_get_documents(status=status, case_id=case_id)
    return [doc for doc in docs if record_belongs_to_actor(doc, actor)]


@app.get("/api/cases/{case_id}/control-center")
async def case_control_center(case_id: str, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    case = await db_get_case(case_id)
    if not case or not record_belongs_to_actor(case, actor):
        raise HTTPException(status_code=404, detail="Case not found")
    updated_case = await refresh_case_control(case_id, trigger="control_center_open") or case
    notifications = await db_get_notifications(firm_id=actor["firm"]["id"], case_id=case_id)
    return {
        "case": updated_case,
        "control_state": updated_case.get("control_state", {}),
        "notifications": notifications[:20],
    }


@app.get("/api/notifications")
async def list_notifications(case_id: Optional[str] = None, unread_only: bool = False, user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    return await db_get_notifications(firm_id=actor["firm"]["id"], case_id=case_id, unread_only=unread_only)


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
        "manual_review_required": False,
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
    await refresh_case_control(case["id"], trigger="manual_assign")
    return doc


@app.patch("/api/documents/{document_id}")
async def update_document(document_id: str, req: UpdateDocumentRequest, user: dict = Depends(get_current_user)):
    doc = await db_get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if "document_type" in patch:
        patch["document_type"] = canonical_document_type(patch["document_type"], doc.get("name", ""))
        patch["document_family"] = patch["document_type"]
    patch["updated_at"] = utc_now()
    updated = await db_update_document(document_id, patch)
    if doc.get("case_id"):
        await refresh_case_control(doc["case_id"], trigger="document_patch")
    return updated or {**doc, **patch}


async def remove_document_everywhere(document_id: str) -> Optional[dict]:
    doc = await db_delete_document(document_id)
    if not doc:
        return None
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
            await refresh_case_control(case_id, trigger="document_delete")
    return doc


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str, user: dict = Depends(get_current_user)):
    doc = await remove_document_everywhere(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted": True, "id": document_id}


async def _stream_r2_key(key: str, name: str, content_type: str = "application/octet-stream"):
    if not USE_R2 or not key:
        raise HTTPException(status_code=404, detail="File not available")
    try:
        obj = r2_client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        content = obj["Body"].read()
        ct = content_type or obj.get("ContentType") or "application/octet-stream"
        return StreamingResponse(
            io.BytesIO(content),
            media_type=ct,
            headers={"Content-Disposition": f'inline; filename="{name}"'},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"R2 fetch failed: {e}")


@app.get("/api/documents/{document_id}/diagnostics")
async def document_diagnostics(document_id: str, user: dict = Depends(get_current_user)):
    # Render deploy trigger: diagnostics endpoint for unrecognized documents
    doc = await db_get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    extracted = doc.get("extracted", {}) or {}
    # If OCR failed or never ran, retry from stored R2 content
    if not extracted.get("ocr_raw_text") and (doc.get("key") or doc.get("r2_key")) and USE_R2:
        key = doc.get("key") or doc.get("r2_key")
        try:
            obj = r2_client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
            content = obj["Body"].read()
            ocr = await run_ocr(content, doc.get("name", "document.pdf"), doc.get("content_type", "application/octet-stream"))
            new_extracted = parse_document_text(ocr.get("raw_text", ""), doc.get("name", "document.pdf"), doc.get("subject", ""))
            new_extracted["ocr_provider"] = ocr.get("provider", "none")
            new_extracted["ocr_confidence"] = ocr.get("confidence", 0)
            new_extracted["ocr_raw_text"] = ocr.get("raw_text", "")
            new_extracted["ocr_error"] = ocr.get("error", "")
            extracted = {**extracted, **new_extracted}
            doc["extracted"] = extracted
            await db_update_document(document_id, {"extracted": extracted})
        except Exception as e:
            extracted["ocr_error"] = f"diagnostics retry failed: {e}"
    return {
        "document_id": doc.get("id"),
        "name": doc.get("name"),
        "document_type": doc.get("document_type"),
        "status": doc.get("status"),
        "ocr_provider": extracted.get("ocr_provider", "none"),
        "ocr_confidence": extracted.get("ocr_confidence", 0),
        "ocr_error": extracted.get("ocr_error", ""),
        "confidence": extracted.get("confidence", 0),
        "classification": {
            "document_type": extracted.get("document_type", "unknown"),
            "classification_confidence": extracted.get("classification_confidence", 0),
        },
        "raw_text": extracted.get("ocr_raw_text", ""),
        "parsed_fields": extracted,
    }


@app.get("/api/documents/{document_id}/download")
async def download_document(document_id: str, user: dict = Depends(get_current_user)):
    doc = await db_get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return await _stream_r2_key(doc.get("key", ""), doc.get("name", "document"), doc.get("content_type", ""))


@app.get("/api/cases/{case_id}/stream/{doc_key:path}")
async def stream_case_document(case_id: str, doc_key: str, user: dict = Depends(get_current_user)):
    case = await db_get_case(case_id)
    if not case or not record_belongs_to_actor(case, await ensure_actor_context(user)):
        raise HTTPException(status_code=404, detail="Case not found")
    doc_entry = next((d for d in case.get("docs", []) if d.get("key") == doc_key), None)
    if not doc_entry:
        raise HTTPException(status_code=404, detail="Document not found in case")
    name = doc_entry.get("name", doc_key.split("/")[-1])
    ct = doc_entry.get("content_type", "application/octet-stream")
    return await _stream_r2_key(doc_key, name, ct)


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
    await refresh_case_control(case_id, trigger="case_document_delete")
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
    notifications = await db_get_notifications(firm_id=actor["firm"]["id"], unread_only=True)
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
    if notifications:
        actions.append({"priority": "medium", "label": "Unread protocol alerts", "count": len(notifications), "action": "Review control-center notifications"})

    return {
        "generated_at": utc_now(),
        "cases": {"total": len(cases), "by_stage": {stage: len([case for case in cases if case.get("stage") == stage]) for stage in ["documents", "payment", "processing", "review", "submitted"]}},
        "documents": doc_counts,
        "invoices": {"overdue": overdue, "due_soon": due_soon},
        "notifications": notifications[:20],
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
    saved = await db_upsert_invoice(invoice)
    if saved.get("case_id"):
        case = await db_get_case(saved["case_id"])
        if case:
            await db_update_case(saved["case_id"], {
                "invoice": {"id": saved["id"], "number": saved["number"], "amount": saved["amount"], "status": saved.get("status", "draft")},
                "invoice_paid": saved.get("status") == "paid",
                "updated_at": utc_now(),
            })
            await refresh_case_control(saved["case_id"], trigger=f"invoice_upsert:{saved.get('status', 'draft')}")
    return saved


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
    if invoice.get("case_id"):
        await refresh_case_control(invoice["case_id"], trigger="invoice_attachment")
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
    if invoice.get("case_id"):
        await refresh_case_control(invoice["case_id"], trigger="invoice_attachment_delete")
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
    if invoice.get("case_id"):
        await refresh_case_control(invoice["case_id"], trigger="invoice_sent")
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
    await refresh_case_control(case_id, trigger="invoice_created")
    return invoice


@app.post("/api/cases/{case_id}/pay")
async def pay_invoice(case_id: str):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if not case.get("invoice"):
        raise HTTPException(status_code=400, detail="No invoice")
    await db_update_case(case_id, {"invoice_paid": True, "stage": "processing", "updated_at": utc_now()})
    await refresh_case_control(case_id, trigger="invoice_paid")
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
    await refresh_case_control(case_id, trigger="manual_advance")
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


IMPORTANT_EMAIL_ATTACHMENT_HINTS = (
    "passport",
    "reisepass",
    "idcard",
    "id card",
    "identity",
    "visa",
    "permit",
    "aufenthalt",
    "contract",
    "arbeitsvertrag",
    "employment",
    "job_offer",
    "job-offer",
    "insurance",
    "kranken",
    "diploma",
    "degree",
    "qualification",
    "recognition",
    "anerkennung",
    "marriage",
    "heirat",
    "birth",
    "geburt",
    "questionnaire",
    "vollmacht",
    "power_of_attorney",
    "bank_statement",
    "bank-statement",
    "payslip",
    "salary",
    "photo",
    "scan",
    "document",
    "doc",
    "upload",
    "pdf",
    "image",
)

IGNORED_EMAIL_ATTACHMENT_HINTS = (
    "logo",
    "signature",
    "image001",
    "image002",
    "smime",
    "winmail",
    "facebook",
    "instagram",
    "linkedin",
    "whatsapp",
    "telegram",
    "banner",
    "icon",
)

SUPPORTED_INTAKE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".tif", ".tiff"}
IGNORED_EMAIL_SENDERS = {
    "welcome@zoho.com",
    "no-reply@zoho.com",
    "noreply@zoho.com",
}
IGNORED_EMAIL_SUBJECT_HINTS = (
    "welcome aboard",
    "your new inbox is here",
)


def is_relevant_email_attachment(filename: str, content_type: str = "", subject: str = "") -> tuple[bool, str]:
    lower_name = (filename or "").lower().strip()
    lower_type = (content_type or "").lower().strip()
    lower_subject = (subject or "").lower().strip()
    subject_has_hint = any(hint in lower_subject for hint in IMPORTANT_EMAIL_ATTACHMENT_HINTS)

    if not lower_name:
        if subject_has_hint and (lower_type.startswith("image/") or lower_type == "application/pdf"):
            return True, "subject_hint_no_name"
        return False, "missing_filename"

    if any(hint in lower_name for hint in IGNORED_EMAIL_ATTACHMENT_HINTS):
        if subject_has_hint and (lower_type.startswith("image/") or lower_type == "application/pdf"):
            return True, "subject_hint_overrides_inline"
        return False, "ignored_inline_asset"

    ext = os.path.splitext(lower_name)[1]
    if ext and ext not in SUPPORTED_INTAKE_EXTENSIONS:
        if subject_has_hint or any(hint in lower_name for hint in IMPORTANT_EMAIL_ATTACHMENT_HINTS):
            return True, "important_keyword"
        return False, f"unsupported_extension:{ext}"

    if lower_type.startswith("image/") or lower_type == "application/pdf":
        if subject_has_hint or any(hint in lower_name for hint in IMPORTANT_EMAIL_ATTACHMENT_HINTS):
            return True, "important_keyword"
        if lower_name.startswith(("scan", "document", "attachment", "file", "image", "img", "photo")):
            return True, "generic_scan"
        return True, "supported_document"

    return False, "unsupported_content_type"


def should_ignore_email_message(sender_email: str = "", subject: str = "") -> tuple[bool, str]:
    normalized_sender = (sender_email or "").lower().strip()
    normalized_subject = (subject or "").lower().strip()
    if normalized_sender in IGNORED_EMAIL_SENDERS:
        return True, "ignored_system_sender"
    if any(hint in normalized_subject for hint in IGNORED_EMAIL_SUBJECT_HINTS):
        return True, "ignored_system_subject"
    return False, ""


async def process_email_payload(payload: EmailWebhook, *, owner_user_id: Optional[str] = None) -> dict:
    from_email = payload.from_.lower().strip()
    ignore_message, ignore_reason = should_ignore_email_message(from_email, payload.subject)
    if ignore_message:
        return {
            "matched_cases": [],
            "created_cases": [],
            "attachments_processed": len(payload.attachments),
            "attachments_accepted": 0,
            "attachments_ignored": len(payload.attachments),
            "ignored": [{"filename": att.filename, "reason": ignore_reason} for att in payload.attachments],
            "duplicates": 0,
            "documents": [],
            "decisions": [],
            "case_summaries": [],
        }
    matched = []
    created = []
    documents = []
    decisions = []
    duplicates = 0
    ignored = []
    seen_attachment_hashes: set[str] = set()
    for att in payload.attachments:
        allowed, reason = is_relevant_email_attachment(att.filename, att.content_type or "", payload.subject)
        if not allowed:
            ignored.append({"filename": att.filename, "reason": reason})
            continue
        content = base64.b64decode(att.content_base64)
        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash in seen_attachment_hashes:
            ignored.append({"filename": att.filename, "reason": "duplicate_attachment_in_same_message"})
            continue
        seen_attachment_hashes.add(content_hash)
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
        decisions.append(result.get("decision", {}))

    case_summaries = []
    seen_case_ids = []
    for case_id in matched:
        if case_id in seen_case_ids:
            continue
        seen_case_ids.append(case_id)
        case = await db_get_case(case_id)
        if not case:
            continue
        control_state = case.get("control_state", {}) or {}
        case_summaries.append({
            "case_id": case_id,
            "client_name": case.get("client_name", ""),
            "next_step": control_state.get("next_step", ""),
            "missing_documents": (control_state.get("document_plan") or {}).get("missing_documents", []),
            "recommended_request": (control_state.get("document_plan") or {}).get("recommended_request", ""),
        })
    return {
        "matched_cases": sorted(set(matched)),
        "created_cases": sorted(set(created)),
        "attachments_processed": len(payload.attachments),
        "attachments_accepted": len(documents),
        "attachments_ignored": len(ignored),
        "ignored": ignored,
        "duplicates": duplicates,
        "documents": documents,
        "decisions": decisions,
        "case_summaries": case_summaries,
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


async def exchange_zoho_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{ZOHO_ACCOUNTS_BASE}/oauth/v2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": ZOHO_OAUTH_CLIENT_ID,
                "client_secret": ZOHO_OAUTH_CLIENT_SECRET,
                "redirect_uri": zoho_oauth_callback_url(),
                "code": code,
            },
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Zoho OAuth token exchange failed: HTTP {response.status_code}")
    return response.json()


async def refresh_zoho_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{ZOHO_ACCOUNTS_BASE}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": ZOHO_OAUTH_CLIENT_ID,
                "client_secret": ZOHO_OAUTH_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Zoho OAuth token refresh failed: HTTP {response.status_code}")
    return response.json()


async def zoho_api_get(path: str, access_token: str, *, params: Optional[dict] = None, accept: str = "application/json"):
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            f"{ZOHO_MAIL_API_BASE}{path}",
            params=params,
            headers={
                "Authorization": f"Zoho-oauthtoken {access_token}",
                "Accept": accept,
                "Content-Type": "application/json",
            },
        )
    if response.status_code >= 400:
        detail = response.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        raise HTTPException(status_code=502, detail=f"Zoho Mail API request failed: HTTP {response.status_code} {detail}")
    return response


async def zoho_api_put_json(path: str, access_token: str, *, body: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.put(
            f"{ZOHO_MAIL_API_BASE}{path}",
            json=body or {},
            headers={
                "Authorization": f"Zoho-oauthtoken {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
    if response.status_code >= 400:
        detail = response.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        if response.status_code == 401 and "INVALID_OAUTHSCOPE" in detail:
            raise HTTPException(
                status_code=502,
                detail="Zoho OAuth token is missing message update scope. Reconnect Zoho work email to refresh permissions."
            )
        raise HTTPException(status_code=502, detail=f"Zoho Mail API write request failed: HTTP {response.status_code} {detail}")
    return response.json() if response.text else {}


async def try_mark_zoho_message_read(access_token: str, account_id: str, message_id: str, thread_id: str = "") -> Optional[str]:
    body = {
        "mode": "markAsRead",
        "messageId": [message_id],
    }
    if thread_id:
        body["threadId"] = [thread_id]
    try:
        await zoho_api_put_json(
            f"/api/accounts/{account_id}/updatemessage",
            access_token,
            body=body,
        )
        return None
    except HTTPException as exc:
        return str(exc.detail)


def parse_zoho_primary_email(account: dict) -> str:
    # Zoho account identity can differ from the actual mailbox address
    # when the user signed up via Google or uses alternate login aliases.
    mailbox_address = (account.get("mailboxAddress") or "").lower().strip()
    if mailbox_address:
        return mailbox_address

    addresses = account.get("emailAddress") or []
    for item in addresses:
        if item.get("isPrimary") and item.get("mailId"):
            return (item.get("mailId") or "").lower().strip()
    for item in addresses:
        if item.get("mailId"):
            return (item.get("mailId") or "").lower().strip()

    return (
        account.get("incomingUserName")
        or account.get("primaryEmailAddress")
        or ""
    ).lower().strip()


async def fetch_zoho_account_profile(access_token: str) -> dict:
    response = await zoho_api_get("/api/accounts", access_token)
    payload = response.json()
    accounts = payload.get("data") or []
    if not accounts:
        raise HTTPException(status_code=502, detail="Zoho Mail API did not return any accessible account")
    primary = next((item for item in accounts if item.get("type") == "ZOHO_ACCOUNT" and item.get("enabled")), None) or accounts[0]
    email_address = parse_zoho_primary_email(primary)
    account_id = str(primary.get("accountId") or "")
    if not email_address or not account_id:
        raise HTTPException(status_code=502, detail="Zoho Mail API account profile is missing email or accountId")
    return {"email": email_address, "account_id": account_id}


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


@app.get("/api/email-integrations/zoho/callback")
async def zoho_email_integration_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(f"{FRONTEND_URL.rstrip('/')}/settings.html?zoho_connected=0&reason={error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing Zoho OAuth callback parameters")
    payload = verify_zoho_state(state)
    token_data = await exchange_zoho_code(code)
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    if not access_token:
        raise HTTPException(status_code=502, detail="Zoho OAuth token response did not include an access token")
    profile = await fetch_zoho_account_profile(access_token)
    integration = {
        "id": str(uuid.uuid4()),
        "provider": "zoho",
        "auth_type": "oauth",
        "email": profile["email"],
        "app_password": "",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expires_at": (
            datetime.now(timezone.utc).timestamp() + int(token_data.get("expires_in") or 3600)
        ),
        "account_id": profile["account_id"],
        "imap_host": "imap.zoho.com",
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
    matched = next(
        (
            item for item in existing
            if item.get("provider") == "zoho" and item.get("auth_type") == "oauth"
        ),
        None,
    ) or next(
        (item for item in existing if item.get("provider") == "zoho" and item.get("email") == profile["email"]),
        None,
    )
    if matched:
        integration["id"] = matched["id"]
        integration["created_at"] = matched.get("created_at", utc_now())
        if not refresh_token:
            integration["refresh_token"] = matched.get("refresh_token", "")
    await db_upsert_email_integration(integration)
    return RedirectResponse(f"{payload.get('next', FRONTEND_URL.rstrip('/') + '/settings.html')}?zoho_connected=1")


def extract_gmail_attachments(message: Message) -> list[EmailAttachment]:
    attachments = []
    synthetic_index = 0
    extension_by_type = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/tiff": ".tiff",
    }
    for part in message.walk():
        filename = part.get_filename()
        content_type = part.get_content_type() or "application/octet-stream"
        disposition = (part.get_content_disposition() or "").lower().strip()
        if not filename and (disposition in {"attachment", "inline"} or content_type.startswith("image/") or content_type == "application/pdf"):
            synthetic_index += 1
            filename = f"email-attachment-{synthetic_index}{extension_by_type.get(content_type, '')}"
        if not filename:
            continue
        content = part.get_payload(decode=True) or b""
        attachments.append(EmailAttachment(
            filename=filename,
            content_base64=base64.b64encode(content).decode("utf-8"),
            content_type=content_type,
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
        detail = response.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        raise HTTPException(status_code=502, detail=f"Gmail API request failed: HTTP {response.status_code} {detail}")
    return response.json()


async def gmail_api_post_json(path: str, access_token: str, *, body: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"https://gmail.googleapis.com/gmail/v1/users/me/{path.lstrip('/')}",
            json=body or {},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        detail = response.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        raise HTTPException(status_code=502, detail=f"Gmail API write request failed: HTTP {response.status_code} {detail}")
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


async def ensure_zoho_integration_access_token(integration: dict) -> dict:
    expires_at = float(integration.get("token_expires_at") or 0)
    if integration.get("access_token") and expires_at > datetime.now(timezone.utc).timestamp() + 60:
        return integration
    if not integration.get("refresh_token"):
        raise HTTPException(status_code=400, detail=f"Zoho OAuth refresh token missing for {integration.get('email')}")
    token_data = await refresh_zoho_access_token(integration["refresh_token"])
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


async def zoho_inbox_folder_id(access_token: str, account_id: str) -> str:
    response = await zoho_api_get(f"/api/accounts/{account_id}/folders", access_token)
    folders = response.json().get("data") or []
    inbox = next(
        (
            item for item in folders
            if (item.get("folderType") or "").lower() == "inbox"
            or (item.get("folderName") or "").lower() == "inbox"
            or (item.get("path") or "").lower() == "/inbox"
        ),
        None,
    )
    if not inbox or not inbox.get("folderId"):
        raise HTTPException(status_code=502, detail="Zoho Mail API did not return an Inbox folder")
    return str(inbox["folderId"])


def is_zoho_message_unread(item: dict) -> bool:
    unread_candidates = (
        item.get("isUnread"),
        item.get("unread"),
        item.get("read"),
        item.get("status"),
        item.get("messageStatus"),
    )
    for value in unread_candidates:
        if isinstance(value, bool):
            if value is True:
                return True
            continue
        normalized = str(value or "").strip().lower()
        if normalized in {"unread", "not_read", "false"}:
            return True
        if normalized in {"read", "true"}:
            return False
        if normalized == "1":
            return True
        if normalized == "0":
            return False
    return False


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
    record_email_poll_debug(
        integration,
        status="running",
        last_error="",
        messages_seen=0,
        messages_with_attachments=0,
        messages_without_attachments=0,
        attachments_total=0,
        processed_count=0,
    )
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
        messages_seen = len(listing.get("messages", []) or [])
        attachments_total = 0
        messages_with_attachments = 0
        messages_without_attachments = 0
        for item in listing.get("messages", []) or []:
            message_id = item.get("id")
            if not message_id:
                continue
            from_header, subject, attachments = await gmail_message_to_attachments(access_token, message_id)
            if not attachments:
                messages_without_attachments += 1
                await gmail_api_post_json(f"messages/{message_id}/modify", access_token, body={"removeLabelIds": ["UNREAD"]})
                continue
            messages_with_attachments += 1
            attachments_total += len(attachments)
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
        record_email_poll_debug(
            ready,
            status="ok",
            messages_seen=messages_seen,
            messages_with_attachments=messages_with_attachments,
            messages_without_attachments=messages_without_attachments,
            attachments_total=attachments_total,
            processed_count=len(processed),
            last_error="",
            last_polled_at=utc_now(),
            last_processed_message_id=processed[-1]["message_id"] if processed else "",
        )
        await db_upsert_email_integration({
            **ready,
            "last_polled_at": utc_now(),
            "last_processed_message_id": processed[-1]["message_id"] if processed else ready.get("last_processed_message_id", ""),
            "updated_at": utc_now(),
        })
        return {"integration_id": ready["id"], "email": ready["email"], "processed": processed, "count": len(processed)}

    if integration.get("provider") == "zoho" and integration.get("auth_type") == "oauth":
        ready = await ensure_zoho_integration_access_token(integration)
        account_id = ready.get("account_id") or (await fetch_zoho_account_profile(ready["access_token"]))["account_id"]
        folder_id = await zoho_inbox_folder_id(ready["access_token"], account_id)
        listing_response = await zoho_api_get(
            f"/api/accounts/{account_id}/messages/view",
            ready["access_token"],
            params={
                "folderId": folder_id,
                "status": "unread",
                "limit": int(ready.get("poll_limit") or 10),
                "sortorder": "false",
            },
        )
        messages = listing_response.json().get("data") or []
        if not messages:
            fallback_response = await zoho_api_get(
                f"/api/accounts/{account_id}/messages/view",
                ready["access_token"],
                params={
                    "folderId": folder_id,
                    "limit": int(ready.get("poll_limit") or 10),
                    "sortorder": "false",
                },
            )
            fallback_messages = fallback_response.json().get("data") or []
            messages = [item for item in fallback_messages if is_zoho_message_unread(item)]
        attachments_total = 0
        messages_with_attachments = 0
        messages_without_attachments = 0
        mark_read_failures = []
        for item in messages:
            message_id = str(item.get("messageId") or "")
            if not message_id:
                continue
            raw_response = await zoho_api_get(
                f"/api/accounts/{account_id}/messages/{message_id}/originalmessage",
                ready["access_token"],
            )
            mime_payload = (((raw_response.json() or {}).get("data") or {}).get("content") or "")
            message = email.message_from_string(mime_payload)
            attachments = extract_gmail_attachments(message)
            thread_id = str(item.get("threadId") or "")
            if not attachments:
                messages_without_attachments += 1
                mark_error = await try_mark_zoho_message_read(ready["access_token"], account_id, message_id, thread_id)
                if mark_error:
                    mark_read_failures.append({
                        "message_id": message_id,
                        "reason": trim_poll_debug_text(mark_error, 120),
                    })
                continue
            messages_with_attachments += 1
            attachments_total += len(attachments)
            sender = parseaddr(message.get("From", ""))[1]
            subject = message.get("Subject", "")
            result = await process_email_payload(
                EmailWebhook(**{"from": sender, "subject": subject, "attachments": attachments}),
                owner_user_id=ready.get("lawyer_id"),
            )
            mark_error = await try_mark_zoho_message_read(ready["access_token"], account_id, message_id, thread_id)
            if mark_error:
                mark_read_failures.append({
                    "message_id": message_id,
                    "reason": trim_poll_debug_text(mark_error, 120),
                })
            processed.append({
                "integration_id": ready["id"],
                "message_id": message_id,
                "from": sender,
                "subject": subject,
                **result,
            })
        record_email_poll_debug(
            ready,
            status="ok",
            messages_seen=len(messages),
            messages_with_attachments=messages_with_attachments,
            messages_without_attachments=messages_without_attachments,
            attachments_total=attachments_total,
            processed_count=len(processed),
            last_error="",
            last_polled_at=utc_now(),
            account_id=account_id,
            last_processed_message_id=processed[-1]["message_id"] if processed else "",
            mark_read_failures=mark_read_failures[:5],
        )
        await db_upsert_email_integration({
            **ready,
            "account_id": account_id,
            "last_polled_at": utc_now(),
            "last_processed_message_id": processed[-1]["message_id"] if processed else ready.get("last_processed_message_id", ""),
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
        attachments_total = 0
        messages_with_attachments = 0
        messages_without_attachments = 0
        for message_id in message_ids:
            fetch_status, fetch_data = mailbox.fetch(message_id, "(RFC822)")
            if fetch_status != "OK" or not fetch_data:
                continue
            raw = fetch_data[0][1]
            message = email.message_from_bytes(raw)
            attachments = extract_gmail_attachments(message)
            if not attachments:
                messages_without_attachments += 1
                mailbox.store(message_id, "+FLAGS", "\\Seen")
                continue
            messages_with_attachments += 1
            attachments_total += len(attachments)
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
    record_email_poll_debug(
        integration,
        status="ok",
        messages_seen=len(message_ids),
        messages_with_attachments=messages_with_attachments,
        messages_without_attachments=messages_without_attachments,
        attachments_total=attachments_total,
        processed_count=len(processed),
        last_error="",
        last_polled_at=utc_now(),
        last_processed_message_id=processed[-1]["message_id"] if processed else "",
    )
    await db_upsert_email_integration({
        **integration,
        "last_polled_at": utc_now(),
        "last_processed_message_id": processed[-1]["message_id"] if processed else integration.get("last_processed_message_id", ""),
        "updated_at": utc_now(),
    })
    return {"integration_id": integration["id"], "email": integration["email"], "processed": processed, "count": len(processed)}


async def run_email_poll_for_integrations(integrations: list[dict], *, continue_on_error: bool = False) -> dict:
    runs = []
    errors = []
    for integration in integrations:
        try:
            runs.append(await process_email_integration(integration))
        except HTTPException as exc:
            message = str(exc.detail)
            record_email_poll_debug(
                integration,
                status="error",
                last_error=message,
                last_polled_at=utc_now(),
            )
            if not continue_on_error:
                raise HTTPException(status_code=exc.status_code, detail=message)
            errors.append({
                "integration_id": integration.get("id"),
                "email": integration.get("email"),
                "message": message,
            })
        except Exception as exc:
            message = f"Work email poll failed for {integration.get('email')}: {exc}"
            print(message)
            record_email_poll_debug(
                integration,
                status="error",
                last_error=message,
                last_polled_at=utc_now(),
            )
            if not continue_on_error:
                raise HTTPException(status_code=502, detail=message)
            errors.append({
                "integration_id": integration.get("id"),
                "email": integration.get("email"),
                "message": message,
            })
    return {
        "runs": runs,
        "count": sum(item["count"] for item in runs),
        "errors": errors,
    }


async def poll_all_active_email_integrations_once() -> dict:
    integrations = pick_runtime_email_integrations_by_workspace(
        await db_get_email_integrations(active_only=True)
    )
    if not integrations:
        return {"runs": [], "count": 0, "errors": [], "has_integrations": False}

    lock = getattr(app.state, "email_poll_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.email_poll_lock = lock

    if lock.locked():
        return {"runs": [], "count": 0, "errors": [], "skipped": True, "has_integrations": True}

    async with lock:
        result = await run_email_poll_for_integrations(integrations, continue_on_error=True)
        return {
            **result,
            "has_integrations": True,
        }


async def auto_email_poll_loop():
    await asyncio.sleep(8)
    while True:
        sleep_seconds = EMAIL_AUTO_POLL_INTERVAL_SECONDS
        try:
            result = await poll_all_active_email_integrations_once()
            if not result.get("has_integrations", True):
                sleep_seconds = EMAIL_AUTO_POLL_IDLE_INTERVAL_SECONDS
            if result.get("count") or result.get("errors"):
                print(
                    "Auto email poll completed",
                    json.dumps({
                        "count": result.get("count", 0),
                        "errors": len(result.get("errors") or []),
                        "skipped": bool(result.get("skipped")),
                    }),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Auto email poll loop failed: {exc}")
        await asyncio.sleep(sleep_seconds)


@app.on_event("startup")
async def start_auto_email_poll_loop():
    if not EMAIL_AUTO_POLL_ENABLED:
        print("Auto email poll is disabled")
        return
    if getattr(app.state, "email_poll_lock", None) is None:
        app.state.email_poll_lock = asyncio.Lock()
    if getattr(app.state, "email_poll_task", None) is None:
        app.state.email_poll_task = asyncio.create_task(auto_email_poll_loop())
        print(f"Auto email poll started with {EMAIL_AUTO_POLL_INTERVAL_SECONDS}s interval")


@app.on_event("shutdown")
async def stop_auto_email_poll_loop():
    task = getattr(app.state, "email_poll_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    app.state.email_poll_task = None


@app.post("/api/gmail/poll")
async def poll_gmail(user: dict = Depends(get_current_user)):
    actor = await ensure_actor_context(user)
    integrations = await db_get_email_integrations(firm_id=actor["firm"]["id"], active_only=True)
    if not integrations:
        all_integrations = await db_get_email_integrations(firm_id=actor["firm"]["id"])
        integrations = pick_runtime_email_integrations(all_integrations)
    else:
        integrations = pick_runtime_email_integrations(integrations)
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
        raise HTTPException(
            status_code=400,
            detail="No usable work email integration found for this workspace. Reconnect Google or Zoho work email, or save manual IMAP settings."
        )
    return await run_email_poll_for_integrations(integrations)


# ─── OCR pipeline ───────────────────────────────────────
@app.post("/api/cases/{case_id}/parse")
async def parse_documents(case_id: str, user: dict = Depends(get_current_user)):
    case = await db_get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    docs = await db_get_documents(case_id=case_id)
    docs = [
        doc for doc in docs
        if (doc.get("status") != "duplicate")
        and (doc.get("source") in {"email", "lawyer", "client", "intake"})
        and not (doc.get("name") or "").lower().startswith("application form")
    ]
    if not docs:
        raise HTTPException(status_code=400, detail="No documents")
    extracted = dict(case.get("extracted", {}) or {})
    parsed_documents = []
    for doc in docs:
        key = doc.get("key") or ""
        if not (USE_R2 and key):
            continue
        try:
            obj = r2_client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
            content = obj["Body"].read()
            ocr = await run_ocr(content, doc.get("name", "document.pdf"), doc.get("content_type", "application/octet-stream"))
            text = ocr.get("raw_text", "") or ""
            fields = parse_document_text(text, doc.get("name", "document.pdf"))
            fields["ocr_provider"] = ocr.get("provider", "none")
            fields["ocr_confidence"] = ocr.get("confidence", 0)
            fields["document_type"] = canonical_document_type(fields.get("document_type", "unknown"), doc.get("name", "document.pdf"))
            parsed_documents.append({
                "document_id": doc.get("id", ""),
                "name": doc.get("name", ""),
                "score": score_extracted_fields(fields),
                "extracted": fields,
            })
            extracted = merge_extracted_fields(extracted, fields)
            await store_extraction_evaluation(case_id, doc.get("id", ""), fields)
        except Exception as e:
            print(f"R2 read for OCR failed on {doc.get('name', '')}: {e}")
    if not parsed_documents:
        raise HTTPException(status_code=400, detail="No parsable documents")
    updated = await db_update_case(case_id, {"extracted": extracted, "stage": "review", "updated_at": utc_now()})
    return {
        "case_id": case_id,
        "extracted": extracted,
        "stage": "review",
        "parsed_documents": sorted(parsed_documents, key=lambda item: item.get("score", 0), reverse=True),
        "case": updated or {**case, "extracted": extracted, "stage": "review"},
    }


class OcrEvaluationRequest(BaseModel):
    text: str
    filename: Optional[str] = ""
    case_id: Optional[str] = ""
    document_id: Optional[str] = ""


class OcrDebugRequest(BaseModel):
    filename: str
    content_base64: str
    content_type: Optional[str] = "application/pdf"
    subject: Optional[str] = ""


@app.post("/api/ocr/debug")
async def debug_ocr(req: OcrDebugRequest, user: dict = Depends(get_current_user)):
    content = base64.b64decode(req.content_base64)
    ocr = await run_ocr(content, req.filename, req.content_type or "application/octet-stream")
    fields = parse_document_text(ocr.get("raw_text", ""), req.filename, req.subject or "")
    return {
        "raw_text": ocr.get("raw_text", ""),
        "ocr_provider": ocr.get("provider", "none"),
        "ocr_confidence": ocr.get("confidence", 0),
        "parsed_fields": fields,
    }


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
        "zoho_email_oauth_enabled": bool(ZOHO_OAUTH_CLIENT_ID and ZOHO_OAUTH_CLIENT_SECRET),
        "email_auto_poll_enabled": EMAIL_AUTO_POLL_ENABLED,
        "email_auto_poll_interval_seconds": EMAIL_AUTO_POLL_INTERVAL_SECONDS,
        "email_auto_poll_idle_interval_seconds": EMAIL_AUTO_POLL_IDLE_INTERVAL_SECONDS,
    }
