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
- **Storage**: In-memory for pilot; add Cloudflare R2 for production
- **Auth**: Demo mode for pilot; migrate to Supabase for production
- **Payments**: Stripe test mode

## Project structure

```
frontend/          Static pages (landing, dashboard, client portal, etc.)
backend/           FastAPI backend
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
5. Backend exposes `/api/webhook/email` for matching emailed documents to a case.

## Next steps

1. Connect real Supabase project (EU Central) for auth and database.
2. Add Cloudflare R2 credentials for file storage.
3. Integrate Mistral OCR for document parsing.
4. Add official PDF form mapping.
5. Move backend to EU region (paid Render / Hetzner / Scaleway) for real documents.
