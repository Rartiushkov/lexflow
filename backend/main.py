import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="LexFlow Backend", version="0.1.0")

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8001")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:8001", "http://127.0.0.1:8001", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-memory demo store ─────────────────────────────────
users = {
    "demo@lexflow.eu": {
        "id": "user_1",
        "email": "demo@lexflow.eu",
        "name": "Demo Lawyer",
        "password": "demo",
    }
}

cases: dict[str, dict] = {}
invoices: dict[str, dict] = {}

def utc_now():
    return datetime.now(timezone.utc).isoformat()

# ─── Auth ───────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/api/auth/login")
def login(req: LoginRequest):
    user = users.get(req.email)
    if not user or user["password"] != req.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {
        "token": f"demo_token_{user['id']}",
        "user": {"id": user["id"], "email": user["email"], "name": user["name"]},
    }

# ─── Helpers ────────────────────────────────────────────
def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization[len("Bearer "):]
    if not token.startswith("demo_token_"):
        raise HTTPException(status_code=401, detail="Invalid token")
    return token.replace("demo_token_", "")

# ─── Cases ──────────────────────────────────────────────
class CreateCase(BaseModel):
    client_name: str
    client_email: str
    case_type: str
    destination: str
    notes: Optional[str] = ""

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "lexflow", "version": "0.1.0"}

@app.get("/api/cases")
def list_cases():
    return sorted(cases.values(), key=lambda c: c["created_at"], reverse=True)

@app.post("/api/cases")
def create_case(req: CreateCase):
    case_id = str(uuid.uuid4())[:8]
    case = {
        "id": case_id,
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
        "portal_url": f"{FRONTEND_URL}/client-upload.html?id={case_id}",
    }
    cases[case_id] = case
    return case

@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    case = cases.get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case

@app.get("/api/cases/{case_id}/public")
def get_case_public(case_id: str):
    case = cases.get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return {
        "id": case["id"],
        "client_name": case["client_name"],
        "case_type": case["case_type"],
        "destination": case["destination"],
        "invoice": case.get("invoice"),
        "invoice_paid": case["invoice_paid"],
    }

# ─── Invoices ───────────────────────────────────────────
class CreateInvoice(BaseModel):
    amount: float = 1000.0
    vat_rate: float = 0.19

@app.post("/api/cases/{case_id}/invoice")
def create_invoice(case_id: str):
    case = cases.get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    inv_id = str(uuid.uuid4())[:8]
    amount = 1000.0
    vat = round(amount * 0.19, 2)
    total = round(amount + vat, 2)
    invoice_number = f"INV-{datetime.now(timezone.utc).year}-{len(invoices)+1:03d}"
    invoice = {
        "id": inv_id,
        "number": invoice_number,
        "amount": total,
        "net": amount,
        "vat": vat,
        "vat_rate": 0.19,
        "currency": "EUR",
        "created_at": utc_now(),
    }
    invoices[inv_id] = invoice
    case["invoice"] = invoice
    case["updated_at"] = utc_now()
    return invoice

@app.post("/api/cases/{case_id}/pay")
def pay_invoice(case_id: str):
    case = cases.get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if not case.get("invoice"):
        raise HTTPException(status_code=400, detail="No invoice")
    case["invoice_paid"] = True
    case["stage"] = "processing"
    case["updated_at"] = utc_now()
    return {"status": "paid"}

@app.post("/api/cases/{case_id}/advance")
def advance_case(case_id: str):
    case = cases.get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    stages = ["documents", "payment", "processing", "review", "submitted"]
    idx = stages.index(case["stage"])
    if idx < len(stages) - 1:
        case["stage"] = stages[idx + 1]
    case["updated_at"] = utc_now()
    return case

# ─── Document upload via email ─────────────────────────
@app.post("/api/webhook/email")
def email_webhook(request: Request):
    """Receive forwarded email with attachments and match to a case."""
    data = request.json()
    if not data:
        raise HTTPException(status_code=400, detail="No JSON payload")
    from_email = data.get("from", "").lower().strip()
    subject = data.get("subject", "")
    attachments = data.get("attachments", [])
    matched = []
    for case in cases.values():
        if case["client_email"].lower() == from_email:
            for att in attachments:
                case["docs"].append({
                    "name": att.get("filename", "document.pdf"),
                    "status": "uploaded_via_email",
                    "uploaded_at": utc_now(),
                    "source": "email",
                    "subject": subject,
                })
            case["updated_at"] = utc_now()
            matched.append(case["id"])
    return {"matched_cases": matched, "attachments_processed": len(attachments)}

# ─── OCR / PDF pipeline ────────────────────────────────
@app.post("/api/cases/{case_id}/parse")
def parse_documents(case_id: str):
    case = cases.get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return {
        "case_id": case_id,
        "status": "parsed",
        "extracted": {
            "full_name": case["client_name"],
            "passport_number": "DEMO-P1234567",
            "date_of_birth": "1990-01-01",
            "nationality": "Demo",
            "employer": "Demo GmbH",
        },
        "form_url": f"{FRONTEND_URL}/api/cases/{case_id}/form",
    }

@app.get("/api/cases/{case_id}/form")
def download_form(case_id: str):
    # In a real implementation, generate filled PDF.
    return JSONResponse({
        "message": "PDF generation is a placeholder. Connect pypdf + official PDF form.",
        "case_id": case_id,
    })

# ─── Stripe webhook ─────────────────────────────────────
@app.post("/api/webhook/stripe")
def stripe_webhook(request: Request):
    payload = request.json()
    event_type = payload.get("type", "")
    if event_type == "checkout.session.completed":
        meta = payload.get("data", {}).get("object", {}).get("metadata", {})
        case_id = meta.get("case_id")
        if case_id and case_id in cases:
            cases[case_id]["invoice_paid"] = True
            cases[case_id]["stage"] = "processing"
    return {"received": True}
