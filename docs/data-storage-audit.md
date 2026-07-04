# LexFlow data and storage audit

Date: 2026-07-04

## Current storage map

| Area | Current state | Production target |
| --- | --- | --- |
| Users and auth | Demo token in browser plus optional Supabase Auth in backend | Supabase Auth only, no demo token in production |
| Lawyer profile | Supabase `profiles` table exists | Keep profile in Supabase with RLS |
| Cases | Backend writes to Supabase when configured, otherwise in-memory fallback | Supabase `cases` table, one row per case, scoped by `lawyer_id` |
| Case documents from case page / client portal | Backend uploads to R2 when configured and stores doc metadata in `cases.docs` JSONB | R2 object per file, Supabase `documents` table for metadata |
| Intake documents page | Browser `localStorage` with base64 `data_url` | R2 object per file, Supabase `documents` row with status `assigned` or `unrecognized` |
| Unrecognized documents | Browser `localStorage` only | Supabase `documents` row with `case_id = null`, `status = unrecognized` |
| Invoice drafts | Browser `localStorage` only | Supabase `invoices` table with full JSON payload and normalized totals |
| Invoice templates | Browser `localStorage` only | Supabase `invoice_templates` table scoped to lawyer or firm |
| Generated invoice PDF | Browser `localStorage` attachment as base64 | R2 object, linked to `invoices.generated_pdf_document_id` or `documents.invoice_id` |
| Uploaded invoice attachments | Browser `localStorage` attachment as base64 | R2 object plus Supabase metadata |
| OCR/extracted fields | Backend stores extracted JSON in `cases.extracted` | Supabase `cases.extracted`, optionally separate extraction logs |
| Audit events | Not stored | Supabase `audit_events` table |

## Gaps found

1. Intake upload does not call the backend yet. Files are stored as base64 in the browser, so they do not exist in R2 and cannot be shared across devices.
2. Generated invoice PDFs are attached only in `localStorage`. They are not uploaded to R2 and are not visible to backend workflows.
3. Invoice templates are local only. A lawyer opening the app from another browser loses templates.
4. The Supabase `invoices` table is too small for the current builder. It stores totals but not line items, template id, due date, status, attachments, notes, or generated PDF metadata.
5. Documents are stored inside `cases.docs` JSONB. This works for demo, but production needs a separate `documents` table for querying, reassignment, status, source, OCR, and audit.
6. There is no durable table for unrecognized documents.
7. There is no audit log for uploads, reassignment, invoice generation, payment, OCR, or case status changes.
8. Backend still has in-memory fallback. That is useful for demo, but production must fail loudly if Supabase or R2 is missing.

## Recommended production schema additions

| Table | Purpose |
| --- | --- |
| `documents` | Every uploaded/generated file, including unrecognized intake files |
| `invoice_templates` | Reusable seller/tax/branding templates |
| `invoice_attachments` | Link files to invoice records, or store this as typed rows in `documents` |
| `audit_events` | Immutable event trail for legal operations |

## Recommended R2 object layout

```text
cases/{case_id}/documents/{document_id}/{safe_filename}
cases/{case_id}/invoices/{invoice_id}/{invoice_number}.pdf
intake/unrecognized/{document_id}/{safe_filename}
templates/{template_id}/assets/{asset_id}/{safe_filename}
```

## Priority order

1. Move intake upload to backend: upload to R2, create Supabase `documents` row.
2. Add `documents.status`: `assigned`, `unrecognized`, `archived`.
3. Add manual reassignment from Unrecognized to Case.
4. Move invoice builder data from `localStorage` to backend.
5. Upload generated invoice PDFs to R2 and attach them to invoice rows.
6. Add audit events for upload, preview, download, reassignment, generation, and deletion.
