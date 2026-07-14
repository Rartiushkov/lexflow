-- LexFlow migration — missing columns observed in Render logs 2026-07-14
-- Run in Supabase SQL Editor

-- cases: missing columns
ALTER TABLE public.cases ADD COLUMN IF NOT EXISTS last_intake_decision JSONB DEFAULT '{}'::jsonb;
ALTER TABLE public.cases ADD COLUMN IF NOT EXISTS automation           JSONB DEFAULT '{}'::jsonb;
ALTER TABLE public.cases ADD COLUMN IF NOT EXISTS portal_url           TEXT;
ALTER TABLE public.cases ADD COLUMN IF NOT EXISTS firm_name            TEXT;

-- documents: missing columns (automation_decision added later)
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS automation_decision   JSONB DEFAULT '{}'::jsonb;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS match_reason          TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS match_score           NUMERIC DEFAULT 0;

-- Refresh PostgREST schema cache so new columns are visible immediately
NOTIFY pgrst, 'reload schema';
