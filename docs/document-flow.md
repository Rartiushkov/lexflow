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

## Current implementation status

| Flow | Status |
| --- | --- |
| Manual case creation | Backend path exists; local fallback now saves the case locally too |
| Add docs inside case | Backend upload exists; metadata is now written as document rows |
| Drag-drop intake | Backend intake upload exists with filename matching; local fallback exists |
| Unrecognized documents | Exists in Intake with `status = unrecognized` |
| Client portal upload | Backend path exists; local fallback now attaches files to the local case |
| Gmail/email ingestion | Webhook exists at `/api/webhook/email`; needs Gmail adapter/forwarder |
| OCR/Mistral | Backend has `MISTRAL_API_KEY` and `/api/cases/{case_id}/parse`; currently simple field parser after OCR |
| Delete files | Implemented for intake, case docs, client portal list, and invoice attachments |
| Invoice email | Endpoint exists at `/api/invoices/{invoice_id}/send`; sends via SMTP when configured and returns `queued_demo` during local/test mode |

## Gmail test setup

For a test Gmail flow, use one of these:

1. Gmail API poller: a small scheduled worker reads unread messages with attachments, posts them to `/api/webhook/email`, then marks them processed.
2. Gmail forwarding: Gmail forwards to an ingestion mailbox/provider, and that provider posts webhook payloads to `/api/webhook/email`.

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
3. Later: run OCR with Mistral and match by name, passport number, email, or case reference.
4. If confidence is low, keep in Unrecognized.
