# LexFlow — AI Memory

> **AI MUST READ THIS FILE AT THE START OF EVERY SESSION.**  
> **AI MUST UPDATE THIS FILE AFTER ANY SIGNIFICANT CHANGE.**  
> Source of truth for architecture, deployment, credentials, and recent history.

## Project identity

- **Name**: LexFlow
- **Purpose**: EU immigration legal-tech pilot / micro-CRM and document automation pipeline for European immigration lawyers.
- **Repo**: `Rartiushkov/lexflow` (GitHub)
- **Local root**: `C:\Users\r.artyshkov\Desktop\LEGAL_UPLOAD`
- **AI instruction file**: `.windsurf/instructions.md` (read first)

## URLs & deployment

| Environment | URL | Notes |
|-------------|-----|-------|
| Frontend production | https://lexflow-11l.pages.dev | Cloudflare Pages, project `lexflow` |
| Frontend preview | https://68656089.lexflow-11l.pages.dev | latest deployment hash |
| Backend API | https://lexflow-backend-y6m3.onrender.com | FastAPI, Render Oregon (free tier) |
| Render service | srv-d94itcho3t8c7394l100 | |
| GitHub repo | https://github.com/Rartiushkov/lexflow | |

### Cloudflare Pages details
- **Account ID**: `fc22c21f68493e5cb86b169b7aa57ea3`
- **Project name**: `lexflow`
- **Production deploy command**: `npx wrangler pages deploy frontend --project-name lexflow --branch production`
- **Auth**: `npx wrangler login` (already authenticated in current session)

### Backend deploy
- **Render**: auto-deploy on git push to `main`.
- **Manual**: use Render dashboard or CLI (`render deploy` if configured).
- **Config**: `render.yaml`

## Stack & architecture

- **Frontend**: Vanilla HTML / CSS / JS, no build step. Deployed to Cloudflare Pages.
- **Backend**: Python FastAPI on Render.
- **Storage**: In-memory fallback; Cloudflare R2 integration ready (needs credentials).
- **Auth**: Demo mode fallback; Supabase Auth integration ready (needs credentials).
- **OCR**: Demo extraction fallback; Mistral OCR integration ready (needs API key).
- **Payments**: Stripe test mode.
- **Database**: Supabase (schema in `supabase/schema.sql`).

## Project structure

```
LEGAL_UPLOAD/
├── frontend/            # Static HTML pages & styles
├── backend/             # FastAPI Python backend
├── supabase/            # SQL schema
├── docs/                # Documentation
├── render.yaml          # Render deployment config
├── README.md            # Human-readable overview
├── .windsurf/
│   ├── instructions.md  # AI behavior rules
│   └── memory.md        # This file — project source of truth
```

## Frontend pages

| Page | File | Purpose |
|------|------|---------|
| Landing | `index.html` | Marketing homepage (landing-v2 dark aurora) |
| Login | `login.html` | Lawyer/client login |
| Dashboard | `dashboard.html` | Kanban case board |
| Cases | `cases.html` | Case list |
| New case | `new-case.html` | Create case + generate portal link |
| Client portal | `client-upload.html` | Client document upload + notes + payment (portal-v2 light executive) |
| Documents | `documents.html` | Document management |
| Invoices | `invoices.html` | Invoice list |
| Invoice | `invoice.html` | Invoice editor + preview |
| Settings | `settings.html` | App settings |
| Styles | `styles.css` | Global styles (portal-v2, landing-v2, dashboard, etc.) |
| App logic | `app.js` | Shared API helpers, auth, formatting |

## Design systems

### Portal v2 (client-upload.html)
- **Theme**: Light executive premium.
- **Background**: `#f6f7f9`
- **Card**: `#ffffff`, border `#e4e7ec`, shadow stack.
- **Text**: `#11131a` (headings), `#344054` (body), `#667085` (muted), `#98a2b3` (dim).
- **Primary accent**: `#2563eb` (blue) — buttons, focus rings, done states.
- **Font**: Inter only.
- **Key classes**: `.portal-v2`, `.portal-v2-card`, `.portal-v2-input`, `.portal-v2-dropzone`, `.portal-v2-checklist`, `.portal-v2-submit`, `.portal-v2-payment`
- **Cache bust**: `styles.css?v=4`, `app.js?v=4`

### Landing v2 (index.html)
- **Theme**: Dark aurora premium.
- **Background**: `#05070a` with aurora gradients.
- **Accent**: `#46e0a1` (green)
- **Fonts**: Inter + Syne.
- **Key classes**: `.landing-v2*`

## Backend endpoints (key)

- `POST /api/login`
- `GET /api/cases/{id}/public`
- `POST /api/cases/{id}/public-submit`
- `POST /api/cases/{id}/upload`
- `DELETE /api/cases/{id}/documents/{ref}`
- `GET /api/webhook/email`
- See `backend/main.py` for full list.

## Demo credentials

- Email: `demo@lexflow.eu`
- Password: `demo`

## Required credentials for real integrations

Set these in **Render environment variables** (do not commit values):

| Variable | Purpose | Status |
|----------|---------|--------|
| `SUPABASE_URL` | Database & auth | needed |
| `SUPABASE_SERVICE_KEY` | Backend DB access | needed |
| `SUPABASE_ANON_KEY` | Frontend auth | needed |
| `MISTRAL_API_KEY` | OCR / document parsing | needed |
| `R2_ENDPOINT` | Cloudflare R2 endpoint | needed |
| `R2_ACCESS_KEY_ID` | R2 credentials | needed |
| `R2_SECRET_ACCESS_KEY` | R2 credentials | needed |
| `R2_BUCKET_NAME` | R2 bucket | needed |
| `R2_PUBLIC_URL` | Public file links | optional |

## Tested user flow

1. Lawyer logs in at `/login.html`.
2. Creates case at `/new-case.html` → system generates client portal link.
3. Client opens `/client-upload.html?id=...` and uploads docs.
4. Lawyer sees case in `/dashboard.html` kanban.
5. Lawyer clicks **Run AI parser** → demo extraction.
6. Lawyer clicks **Download PDF form** → PDF download.

## Recent changes (session history)

- Created `.windsurf/instructions.md` and `.windsurf/memory.md` for AI context.
- Redesigned `client-upload.html` to **portal-v2 light executive premium**.
- Redesigned `index.html` as `landing-v2` dark aurora premium.
- Updated `styles.css` with `.portal-v2*` and `.landing-v2*` classes.
- Added cache-busting (`?v=4`) to CSS/JS links.
- Deployed frontend to Cloudflare Pages production.

## Working conventions

- Read `.windsurf/instructions.md` and `.windsurf/memory.md` at session start.
- Update `.windsurf/memory.md` after every significant change.
- Prefer minimal edits; keep existing styles intact.
- Always add cache-busting query strings when redeploying redesigned assets.
- Frontend deploy command: `npx wrangler pages deploy frontend --project-name lexflow --branch production`.
- Backend deploy: git push triggers Render; manual via Render dashboard.
- Verify frontend deployment with `Invoke-WebRequest` to `https://lexflow-11l.pages.dev/`.

## Next steps / backlog

1. Enable Cloudflare R2 and create EU bucket.
2. Create Supabase project (EU Central) and run `supabase/schema.sql`.
3. Add real credentials to Render env vars.
4. Replace generated PDF form with official government PDF template.
5. Move backend to EU region for real documents.
6. Dovetail landing page style with portal-v2 (optional — user preference).

---

*Last updated: 2026-07-05 by Cascade.*
