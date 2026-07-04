# LexFlow — EU Immigration LegalTech Pilot

Micro-CRM and document automation pipeline for European immigration lawyers.

## Deployed URLs

- **Frontend**: https://lexflow-11l.pages.dev
- **Backend API**: https://lexflow-backend-y6m3.onrender.com
- **GitHub repo**: https://github.com/Rartiushkov/lexflow
- **Render service**: srv-d94itcho3t8c7394l100

## Stack

- **Frontend**: Vanilla HTML/CSS/JS deployed on Cloudflare Pages
- **Backend**: Python FastAPI on Render (Oregon — free tier; move to EU for real data)
- **Storage**: In-memory fallback; Cloudflare R2 integration ready (needs R2 enabled)
- **Auth**: Demo mode fallback; Supabase Auth integration ready (needs credentials)
- **OCR**: Demo extraction fallback; Mistral OCR integration ready (needs API key)
- **Payments**: Stripe test mode

## Project structure

```
frontend/          Static pages (landing, dashboard, client portal, etc.)
backend/           FastAPI backend
supabase/          SQL schema for Supabase
render.yaml        Render deployment config
```

## Local development

```bash
# Frontend
python -m http.server 8001 -d frontend

# Backend
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

## Demo credentials

- Email: `demo@lexflow.eu`
- Password: `demo`

## Tested flow

1. Lawyer logs in at `/login.html`.
2. Creates a case at `/new-case.html` → system generates a client portal link.
3. Client opens the link (`/client-upload.html?id=...`) and uploads documents.
4. Lawyer sees the case in the kanban dashboard at `/dashboard.html`.
5. Lawyer clicks **Run AI parser** → demo extraction runs.
6. Lawyer clicks **Download PDF form** → generated PDF downloads.
7. Backend exposes `/api/webhook/email` for matching emailed documents to a case.

## Required credentials to activate real integrations

Set these in Render environment variables for the backend:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `SUPABASE_ANON_KEY`
- `MISTRAL_API_KEY`
- `R2_ENDPOINT` (e.g. `https://<account>.eu.r2.cloudflarestorage.com`)
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME` (e.g. `lexflow-documents`)
- `R2_PUBLIC_URL` (optional, for public links)

## Important blockers

- **Cloudflare R2 must be enabled** in the dashboard before creating the bucket.
- **Supabase project** must be created manually in EU Central, then `supabase/schema.sql` run.
- **Mistral API key** requires a free account at https://mistral.ai.

## Next steps

1. Enable R2 in Cloudflare dashboard and create EU bucket.
2. Create Supabase project (EU Central) and run `supabase/schema.sql`.
3. Add all credentials to Render env vars.
4. Replace generated PDF form with an official government PDF template.
5. Move backend to EU region (paid Render / Hetzner / Scaleway) for real documents.
