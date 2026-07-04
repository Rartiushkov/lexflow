# LexFlow document flow

Date: 2026-07-04

## Target flow

1. Lawyer creates a case manually.
2. The case is saved immediately with client name, email, route, country, stage, and portal link.
3. Lawyer can add documents directly inside the case.
4. Lawyer can also drop files into Intake without choosing a case.
5. Intake reads the file name first, then OCR/extraction later, and tries to match it to an existing case.
6. If no case is matched, the file stays in Unrecognized.
7. Lawyer can delete, preview, download, or later reassign the file.
8. Lawyer sends the client portal link to the client.
9. Client uploads documents through the portal.
10. Client uploads are attached to the same case.
11. Email ingestion receives attachments from Gmail and routes them the same way as Intake.
12. Mistral OCR runs on stored files and writes extracted fields back to the case.
13. Lawyer sends generated invoices to the case client email from the invoice page.
14. Incoming documents are deduplicated by file hash and checked by document type inside the case.
15. If email/OCR confidently identifies a new person and no case exists, the backend auto-creates a case.
16. Workflow summary highlights overdue invoices, due-soon invoices, unrecognized documents, duplicate document types, and duplicates.

## Current implementation status

| Flow | Status |
| --- | --- |
| Manual case creation | Backend path exists; local fallback now saves the case locally too |
| Add docs inside case | Backend upload exists; metadata is now written as document rows |
| Drag-drop intake | Backend intake upload exists with filename matching; local fallback exists |
| Unrecognized documents | Exists in Intake with `status = unrecognized` |
| Client portal upload | Backend path exists; local fallback now attaches files to the local case |
| Gmail/email ingestion | Webhook exists at `/api/webhook/email`; Gmail IMAP adapter exists at `/api/gmail/poll` |
| OCR | Backend tries free local OCR first (`pypdf` for text PDFs, Tesseract for scans/images), then can use Mistral only as fallback |
| Delete files | Implemented for intake, case docs, client portal list, and invoice attachments |
| Invoice email | Endpoint exists at `/api/invoices/{invoice_id}/send`; sends via SMTP when configured and returns `queued_demo` during local/test mode |
| ML/evaluation | OCR extraction scores are stored in `ml_evaluations` and available through `/api/evaluations` |
| Workflow summary | Endpoint exists at `/api/workflow/summary`; dashboard uses it for automation alerts |

## Gmail test setup

For a test Gmail flow, use one of these:

1. Gmail IMAP adapter: configure `GMAIL_EMAIL` and `GMAIL_APP_PASSWORD`, then call `POST /api/gmail/poll`; unread messages with attachments are converted into the same payload used by `/api/webhook/email`.
2. Gmail forwarding: Gmail forwards to an ingestion mailbox/provider, and that provider posts webhook payloads to `/api/webhook/email`.

Render environment variables for the IMAP adapter:

```text
GMAIL_EMAIL=your-ingestion@gmail.com
GMAIL_APP_PASSWORD=google-app-password
GMAIL_IMAP_HOST=imap.gmail.com
GMAIL_MAILBOX=INBOX
GMAIL_POLL_LIMIT=10
DEFAULT_LAWYER_ID=supabase-user-uuid-for-automation
```

The webhook payload shape already expected by backend:

```json
{
  "from": "client@example.com",
  "subject": "Documents",
  "attachments": [
    {
      "filename": "passport.pdf",
      "content_base64": "...",
      "content_type": "application/pdf"
    }
  ]
}
```

## Matching order

1. Match by client email for email ingestion.
2. Match by full client name in filename for Intake.
3. Run OCR and match by name, passport number, email, or case reference.
4. If confidence is low, keep in Unrecognized.

## Autonomous intake rules

1. Exact email match wins and attaches the document to the existing case.
2. OCR full-name match attaches the document to an existing case.
3. Filename full-name match is used when OCR is weak.
4. If there is no case but OCR extracts a confident full name, or the sender email is known and extraction confidence is acceptable, the system auto-creates a case.
5. If the same file hash already exists, the new document is marked `duplicate`.
6. If the same document type already exists in the case, the new document is marked `needs_review` so a human can decide whether it replaces the old file.
7. If no safe match is found, the document stays `unrecognized`.

## OCR evaluation

The parser extracts document type, full name, passport number, date of birth, expiry date, nationality, email, phone, address, employer, invoice number, invoice total, and IBAN where present.

OCR order:

1. Text PDF extraction through `pypdf`. This is free and best for digitally generated PDFs.
2. Scan/image OCR through local Tesseract. This is free, runs on Render when `Aptfile` packages are installed, and supports English/German by default.
3. Mistral OCR only if `MISTRAL_API_KEY` exists and local OCR confidence is too low.

Render/system packages:

```text
tesseract-ocr
tesseract-ocr-eng
tesseract-ocr-deu
```

OCR environment variables:

```text
OCR_PROVIDER=auto
OCR_LANG=eng+deu
MISTRAL_API_KEY=optional fallback only
```

Every OCR/evaluation run stores:

```text
case_id
document_id
model
score
passed
suggestions
payload
created_at
```
