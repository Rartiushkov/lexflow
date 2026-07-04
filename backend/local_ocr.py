import io
import shutil
from dataclasses import dataclass

from pypdf import PdfReader


try:
    from PIL import Image
except Exception:
    Image = None

try:
    import fitz
except Exception:
    fitz = None

try:
    import pytesseract
except Exception:
    pytesseract = None


@dataclass
class LocalOcrResult:
    raw_text: str
    provider: str
    pages: list[dict]
    confidence: float
    error: str = ""


def has_tesseract() -> bool:
    return bool(pytesseract and shutil.which("tesseract"))


def extract_pdf_text(content: bytes) -> LocalOcrResult:
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = []
        text_parts = []
        for index, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            pages.append({"page": index + 1, "chars": len(page_text), "method": "pypdf"})
            if page_text.strip():
                text_parts.append(page_text)
        text = "\n".join(text_parts).strip()
        lookup = text.lower()
        has_structured_fields = any(token in lookup for token in ("passport", "invoice", "date of birth", "name:", "iban"))
        confidence = 0.9 if len(text) > 80 or has_structured_fields else 0.45 if text else 0.0
        return LocalOcrResult(text, "pypdf", pages, confidence)
    except Exception as e:
        return LocalOcrResult("", "pypdf", [], 0.0, str(e))


def ocr_image_bytes(content: bytes, lang: str = "eng+deu") -> LocalOcrResult:
    if not has_tesseract() or Image is None:
        return LocalOcrResult("", "tesseract", [], 0.0, "Tesseract is not installed")
    try:
        image = Image.open(io.BytesIO(content))
        text = pytesseract.image_to_string(image, lang=lang).strip()
        confidence = 0.78 if len(text) > 80 else 0.45 if text else 0.0
        return LocalOcrResult(text, "tesseract", [{"page": 1, "method": "tesseract"}], confidence)
    except Exception as e:
        return LocalOcrResult("", "tesseract", [], 0.0, str(e))


def ocr_pdf_with_tesseract(content: bytes, lang: str = "eng+deu", max_pages: int = 5) -> LocalOcrResult:
    if not has_tesseract() or fitz is None:
        return LocalOcrResult("", "tesseract_pdf", [], 0.0, "Tesseract or PyMuPDF is not installed")
    try:
        doc = fitz.open(stream=content, filetype="pdf")
        text_parts = []
        pages = []
        for index, page in enumerate(doc[:max_pages]):
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            text = pytesseract.image_to_string(image, lang=lang).strip()
            pages.append({"page": index + 1, "chars": len(text), "method": "tesseract_pdf"})
            if text:
                text_parts.append(text)
        text = "\n".join(text_parts).strip()
        confidence = 0.76 if len(text) > 80 else 0.4 if text else 0.0
        return LocalOcrResult(text, "tesseract_pdf", pages, confidence)
    except Exception as e:
        return LocalOcrResult("", "tesseract_pdf", [], 0.0, str(e))


def run_local_ocr(content: bytes, filename: str, lang: str = "eng+deu") -> dict:
    name = (filename or "").lower()
    attempts = []
    if name.endswith(".pdf"):
        pdf_text = extract_pdf_text(content)
        attempts.append(pdf_text)
        if pdf_text.confidence >= 0.65:
            return {
                "raw_text": pdf_text.raw_text,
                "provider": pdf_text.provider,
                "pages": pdf_text.pages,
                "confidence": pdf_text.confidence,
                "attempts": [attempt.__dict__ for attempt in attempts],
            }
        scanned_pdf = ocr_pdf_with_tesseract(content, lang=lang)
        attempts.append(scanned_pdf)
        best = max(attempts, key=lambda item: item.confidence)
        return {
            "raw_text": best.raw_text,
            "provider": best.provider,
            "pages": best.pages,
            "confidence": best.confidence,
            "attempts": [attempt.__dict__ for attempt in attempts],
        }

    image_ocr = ocr_image_bytes(content, lang=lang)
    return {
        "raw_text": image_ocr.raw_text,
        "provider": image_ocr.provider,
        "pages": image_ocr.pages,
        "confidence": image_ocr.confidence,
        "attempts": [image_ocr.__dict__],
    }
