import re
from datetime import datetime, timezone


DOCUMENT_RULES = {
    "passport": {
        "keywords": ("passport", "passport scan", "passport copy", "reisepass", "travel document", "document no", "identity document", "id page"),
        "required": ("full_name", "passport_number", "date_of_birth"),
    },
    "marriage_certificate": {
        "keywords": ("marriage certificate", "heiratsurkunde", "certificate of marriage", "spouse"),
        "required": ("full_name",),
    },
    "birth_certificate": {
        "keywords": ("birth certificate", "geburtsurkunde", "date of birth", "place of birth"),
        "required": ("full_name", "date_of_birth"),
    },
    "invoice": {
        "keywords": ("invoice", "rechnung", "iban", "vat", "amount due"),
        "required": ("invoice_number", "invoice_total"),
    },
    "residence_permit": {
        "keywords": (
            "residence permit", "tartózkodási", "engedély", "tartozkodasi", "fehér kártya", "feher kártya",
            "állampolgárság", "allampolgarsag", "születési", "szuletesi", "ervenyessege", "érvényessége",
            "aufenthaltstitel", "residence card", "id card", "id-card", "national id", "personalausweis",
            "permit", "blue card", "aufenthaltserlaubnis",
        ),
        "required": ("full_name", "date_of_birth"),
    },
    "employment": {
        "keywords": ("employment", "employment contract", "job offer", "offer letter", "employer", "arbeitsvertrag", "salary", "position"),
        "required": ("full_name", "employer"),
    },
    "qualification": {
        "keywords": ("diploma", "degree", "certificate", "zeugnis", "qualification", "transcript"),
        "required": ("full_name",),
    },
    "recognition_notice": {
        "keywords": ("recognition notice", "anerkennung", "statement of comparability", "partial recognition"),
        "required": (),
    },
    "health_insurance": {
        "keywords": ("health insurance", "krankenversicherung", "insurance certificate", "coverage", "insurance policy", "versicherung"),
        "required": (),
    },
    "financial_proof": {
        "keywords": ("blocked account", "bank statement", "declaration of commitment", "proof of funds", "payslip", "salary statement", "account statement"),
        "required": (),
    },
    "language_certificate": {
        "keywords": ("language certificate", "sprachzertifikat", "cefr", "goethe"),
        "required": ("full_name",),
    },
    "questionnaire": {
        "keywords": ("questionnaire", "intake form", "client questionnaire", "survey"),
        "required": (),
    },
    "power_of_attorney": {
        "keywords": ("power of attorney", "vollmacht", "authorization"),
        "required": ("full_name",),
    },
}


def normalize_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", (text or "").replace("\r", "\n")).strip()


def first_match(patterns: list[str], text: str, flags=re.IGNORECASE | re.MULTILINE) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return " ".join(match.group(1).split()).strip(" ,;")
    return ""


def normalize_date(value: str) -> str:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d %m %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return value


def classify_document(text: str, filename: str = "") -> dict:
    lookup = f"{filename}\n{text}".lower()
    scores = {}
    for doc_type, rule in DOCUMENT_RULES.items():
        scores[doc_type] = sum(1 for keyword in rule["keywords"] if keyword in lookup)
    doc_type = max(scores, key=scores.get) if scores else "unknown"
    if scores.get(doc_type, 0) == 0:
        doc_type = "unknown"
    confidence = min(0.95, 0.35 + scores.get(doc_type, 0) * 0.2) if doc_type != "unknown" else 0.2
    return {"document_type": doc_type, "classification_confidence": round(confidence, 2)}


def parse_document_text(text: str, filename: str = "") -> dict:
    cleaned = normalize_text(text)
    fields = {
        "full_name": first_match([
            r"(?:full name|name|surname and given names|vor- und nachname|name|vezetéknevek)[: \t]+([A-ZА-Я][A-Za-zА-Яа-я'\-]+(?:[ \t]+[A-ZА-Я][A-Za-zА-Яа-я'\-]+){1,3})",
            r"([A-Z][A-Z'\-]+,\s+[A-Z][A-Z'\-]+)",
            r"(?:vezetéknevek|surname)[: \t]*\n?\s*([A-Z][A-Za-z'\-]+)\s*\n?\s*([A-Z][A-Za-z'\-]+)",
            r"(?:utónevek|given name)[: \t]*\n?\s*([A-Z][A-Za-z'\-]+)\s*\n?\s*([A-Z][A-Za-z'\-]+)",
            r"\b([A-Z][A-Z'\-]+)\s*\n\s*([A-Z][A-Za-z'\-]+)\b",
        ], cleaned),
        "passport_no": first_match([
            r"(?:passport(?: no\.?| number)?|reisepass(?:nr\.?)?|document no\.?)[:\s#]+((?=[A-Z0-9]*\d)[A-Z0-9]{6,14})",
            r"\b([A-Z][0-9]{6,9})\b",
            r"\b([A-Z0-9]{6,14})\b(?=\s*passport|\s*reisepass|\s*document|\s*nationality)",
        ], cleaned),
        "permit_number": first_match([
            r"\b(\d{9})\b",
            r"(?:permit no|card no|kártya szám|kartyaszam)[:\s#]+(\d{6,12})",
        ], cleaned),
        "passport_number": first_match([
            r"(?:passport(?: no\.?| number)?|reisepass(?:nr\.?)?|document no\.?)[:\s#]+((?=[A-Z0-9]*\d)[A-Z0-9]{6,14})",
            r"\b([A-Z][0-9]{6,9})\b",
        ], cleaned),
        "date_of_birth": normalize_date(first_match([
            r"(?:date of birth|dob|geburtsdatum|geboren|születési idő|szuletesi ido)[:\s]+(\d{4}-\d{2}-\d{2}|\d{1,2}[.\s/-]\d{1,2}[.\s/-]\d{4})",
        ], cleaned)),
        "dob": normalize_date(first_match([
            r"(?:date of birth|dob|geburtsdatum|geboren|születési idő|szuletesi ido)[:\s]+(\d{4}-\d{2}-\d{2}|\d{1,2}[.\s/-]\d{1,2}[.\s/-]\d{4})",
        ], cleaned)),
        "passport_expiry": normalize_date(first_match([
            r"(?:expiry|expires|valid until|gultig bis|gültig bis|date of expiry|érvényessége|ervenyessege)[:\s]+(\d{4}-\d{2}-\d{2}|\d{1,2}[.\s/-]\d{1,2}[.\s/-]\d{4})",
        ], cleaned)),
        "expiry_date": normalize_date(first_match([
            r"(?:expiry|expires|valid until|gultig bis|gültig bis|date of expiry|érvényessége|ervenyessege)[:\s]+(\d{4}-\d{2}-\d{2}|\d{1,2}[.\s/-]\d{1,2}[.\s/-]\d{4})",
        ], cleaned)),
        "nationality": first_match([
            r"(?:nationality|staatsangehorigkeit|staatsangehörigkeit|citizenship|állampolgárság|allampolgarsag)[:\s]+([A-Z][A-Za-z ]{2,40})",
            r"(?:nationality|állampolgárság|allampolgarsag)[:\s]+\n?\s*\b([A-Z]{3})\b",
        ], cleaned),
        "email": first_match([
            r"\b([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})\b",
        ], cleaned),
        "phone": first_match([
            r"(?:phone|mobile|telefon|cell)[:\s]+(\+?[0-9][0-9 ()\-]{6,24})",
        ], cleaned),
        "address_home": first_match([
            r"(?:home address|home|address|anschrift|adresse)[:\s]+(.{8,120})",
        ], cleaned),
        "address_eu": first_match([
            r"(?:eu address|address in eu|address in germany|residence address|current address)[:\s]+(.{8,120})",
        ], cleaned),
        "address": first_match([
            r"(?:address|anschrift|adresse)[:\s]+(.{8,120})",
        ], cleaned),
        "employer": first_match([
            r"(?:employer|company|arbeitgeber)[:\s]+([A-Z0-9][A-Za-z0-9 &.,'\-]{2,80})",
        ], cleaned),
        "employer_name": first_match([
            r"(?:employer|company|arbeitgeber)[:\s]+([A-Z0-9][A-Za-z0-9 &.,'\-]{2,80})",
        ], cleaned),
        "employer_vat": first_match([
            r"(?:vat(?: number)?|tax id|steuer|ust-id|ust idnr)[:\s#]+([A-Z0-9]{6,20})",
        ], cleaned),
        "job_title": first_match([
            r"(?:position|job title|role|berufsbezeichnung|tätigkeit)[:\s]+([A-Z][A-Za-z0-9 &.,'\-]{2,60})",
        ], cleaned),
        "salary": first_match([
            r"(?:salary|gross salary|annual salary|jahreseinkommen|gehalt)[:\s]+(?:EUR|€)?\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
        ], cleaned),
        "salary_eur": first_match([
            r"(?:salary|gross salary|annual salary|jahreseinkommen|gehalt)[:\s]+(?:EUR|€)?\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
        ], cleaned),
        "marital_status": first_match([
            r"(?:marital status|family status|familienstand)[:\s]+(single|married|divorced|widowed|separated|ledig|verheiratet|geschieden|verwitwet)",
        ], cleaned),
        "dependants": first_match([
            r"(?:dependants|dependents|children|family members)[:\s]+(\d+)",
        ], cleaned),
        "invoice_number": first_match([
            r"(?:invoice(?: no\.?| number)?|rechnung(?:snummer)?)[:\s#]+([A-Z0-9\-\/]{3,30})",
        ], cleaned),
        "invoice_total": first_match([
            r"(?:total|amount due|gesamtbetrag|summe)[:\s]+(?:EUR|€)?\s*([0-9]+(?:[.,][0-9]{2})?)",
        ], cleaned),
        "iban": first_match([
            r"\b([A-Z]{2}[0-9]{2}[A-Z0-9]{11,30})\b",
        ], cleaned),
    }
    fields = {key: value for key, value in fields.items() if value}
    classification = classify_document(cleaned, filename, subject)
    doc_type = classification["document_type"]
    required = DOCUMENT_RULES.get(doc_type, {}).get("required", ())
    missing = [field for field in required if not fields.get(field)]
    score_base = 0.3 if doc_type != "unknown" else 0.1
    # If document was classified only by filename/subject (likely phone photo with empty OCR),
    # give a better base so it does not look like random noise
    if doc_type != "unknown" and (not cleaned or len(cleaned) < 80):
        score_base = max(score_base, 0.45)
    score_fields = min(0.6, len(fields) * 0.08)
    penalty = len(missing) * 0.12
    confidence = max(0.05, min(0.98, score_base + score_fields - penalty))
    return {
        **classification,
        **fields,
        "missing_fields": missing,
        "confidence": round(confidence, 2),
    }


def evaluate_extraction(fields: dict) -> dict:
    confidence = float(fields.get("confidence") or 0)
    missing = fields.get("missing_fields") or []
    suggestions = []
    if confidence < 0.65:
        suggestions.append("Review OCR quality or ask for a clearer scan.")
    if missing:
        suggestions.append(f"Missing required fields: {', '.join(missing)}.")
    if fields.get("document_type") == "unknown":
        suggestions.append("Add document-specific keywords or manual classification.")
    if not suggestions:
        suggestions.append("Extraction quality is acceptable.")
    return {
        "score": round(confidence, 2),
        "passed": confidence >= 0.65 and not missing,
        "suggestions": suggestions,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
