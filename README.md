# LexFlow — EU Immigration LegalTech Pilot

Micro-CRM and document automation pipeline for European immigration lawyers.

## Stack

- **Frontend**: Vanilla HTML/CSS/JS deployed on Cloudflare Pages
- **Backend**: Python FastAPI on Render (Frankfurt region)
- **Storage**: Cloudflare R2 (EU bucket)
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

## Deploy

- Frontend: Cloudflare Pages
- Backend: Render (auto-deploy from GitHub)

## Demo credentials

- Email: `demo@lexflow.eu`
- Password: `demo`

## Next steps

1. Connect real Supabase project (EU Central).
2. Add Cloudflare R2 credentials for file storage.
3. Integrate Mistral OCR for document parsing.
4. Add official PDF form mapping.
