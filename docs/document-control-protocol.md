# LexFlow Document Control Protocol

Date: 2026-07-05

## Goal

LexFlow should behave as a strict document-control system for immigration lawyers:

1. No file enters the system without a status.
2. No case moves stages without protocol checks.
3. No duplicate or low-quality file stays invisible.
4. Lawyers review exceptions, not raw intake chaos.

## Core states

### Document states

- `assigned`: attached to the correct case and acceptable for workflow.
- `needs_review`: likely useful, but a lawyer must confirm quality, translation, authenticity or replacement.
- `duplicate`: same hash as an existing file.
- `unrecognized`: the system could not safely attach the file to a case.
- `archived`: kept for audit, but excluded from active workflow.

### Requirement states

- `complete`: a valid document exists for the requirement.
- `missing`: no active file satisfies the requirement.
- `needs_review`: a candidate document exists, but it is blocked by manual review.

### Notification kinds

- `workflow`: stage changes or protocol milestones.
- `action_required`: the system needs lawyer or client follow-up.
- `risk_flag`: expiry, conflict, quality issue, authenticity issue or similar risk.

## Germany-first route protocols

### `DE_BLUE_CARD`

Required blockers:

- Passport
- Employment contract or job offer
- Degree or qualification proof
- Health insurance

Automation:

- If all blockers are complete and billing is incomplete, move case to `payment`.
- If all blockers are complete and billing is complete, move case to `processing`.

### `DE_SKILLED_WORKER`

Required blockers:

- Passport
- Employment contract or job offer
- Recognised qualification or recognition notice
- Health insurance

Automation:

- Same movement logic as Blue Card.

### `DE_FAMILY_REUNIFICATION_SPOUSE`

Required blockers:

- Passport
- Marriage certificate
- Housing or rental evidence
- Financial evidence / payslips

Manual review:

- Marriage certificate authenticity or apostille status
- Translation completeness

### `DE_RECOGNITION`

Required blockers:

- Passport
- Recognition notice
- Qualification evidence
- Language certificate
- Proof of funds
- Health insurance

Manual review:

- Borderline funds
- Partial recognition handling

## Intake and anti-trash rules

1. Compute hash on every upload.
2. OCR every file where possible.
3. Classify the document to a canonical family.
4. Try case matching by:
   - sender email
   - OCR full name
   - filename name match
5. If same hash exists, mark `duplicate`.
6. If same logical document family already exists in a case, mark `needs_review`.
7. If OCR confidence is low, mark `needs_review`.
8. If nothing matches safely, keep `unrecognized`.

## Lawyer-visible actions

The UI should surface messages like:

- `Request missing documents`
- `Send client questionnaire`
- `Follow up on invoice`
- `Resolve duplicate files`
- `Manual review required`
- `Attention required`

## Recommended browser push behavior

Push/in-app notifications should be created when:

- a case auto-moves stage
- a blocker is missing before an appointment
- a duplicate or conflict appears
- a passport or permit is expiring soon
- a client upload is too poor for auto-processing

Push text should be short and operational, for example:

- `Case moved to Payment: required documents are complete.`
- `Manual review: new passport conflicts with existing identity data.`
- `Client follow-up: questionnaire is still missing.`
- `Attention: marriage certificate needs authenticity review.`
