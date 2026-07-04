-- LexFlow storage migration
-- Run in Supabase SQL Editor for an existing project.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'draft';
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS client_name TEXT;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS client_email TEXT;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS issue_date TEXT;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS due_date TEXT;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS template_id TEXT;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS items JSONB DEFAULT '[]'::jsonb;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS attachments JSONB DEFAULT '[]'::jsonb;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS last_sent_to TEXT;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE public.invoices ALTER COLUMN amount SET DEFAULT 0;
ALTER TABLE public.invoices ALTER COLUMN net SET DEFAULT 0;
ALTER TABLE public.invoices ALTER COLUMN vat SET DEFAULT 0;
ALTER TABLE public.invoices ALTER COLUMN vat_rate SET DEFAULT 0;

CREATE TABLE IF NOT EXISTS public.documents (
    id TEXT PRIMARY KEY,
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    case_id TEXT REFERENCES public.cases(id) ON DELETE SET NULL,
    case_name TEXT,
    invoice_id TEXT,
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    url TEXT,
    source TEXT DEFAULT 'intake',
    status TEXT DEFAULT 'unrecognized',
    content_hash TEXT,
    document_type TEXT,
    automation_status TEXT,
    automation_note TEXT,
    extracted JSONB DEFAULT '{}'::jsonb,
    content_type TEXT,
    size BIGINT DEFAULT 0,
    uploaded_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS document_type TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS automation_status TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS automation_note TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS extracted JSONB DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS public.invoice_templates (
    id TEXT PRIMARY KEY,
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.audit_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    case_id TEXT REFERENCES public.cases(id) ON DELETE SET NULL,
    document_id TEXT,
    invoice_id TEXT,
    action TEXT NOT NULL,
    payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.ml_evaluations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    case_id TEXT REFERENCES public.cases(id) ON DELETE SET NULL,
    document_id TEXT,
    model TEXT NOT NULL,
    score NUMERIC DEFAULT 0,
    passed BOOLEAN DEFAULT FALSE,
    suggestions JSONB DEFAULT '[]'::jsonb,
    payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoice_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ml_evaluations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view invoices for their cases" ON public.invoices;
CREATE POLICY "Users can view invoices for their cases"
    ON public.invoices
    FOR ALL
    USING (lawyer_id = auth.uid() OR EXISTS (
        SELECT 1 FROM public.cases WHERE cases.id = invoices.case_id AND cases.lawyer_id = auth.uid()
    ))
    WITH CHECK (lawyer_id = auth.uid());

DROP POLICY IF EXISTS "Users can manage their own documents" ON public.documents;
CREATE POLICY "Users can manage their own documents"
    ON public.documents
    FOR ALL
    USING (lawyer_id = auth.uid())
    WITH CHECK (lawyer_id = auth.uid());

DROP POLICY IF EXISTS "Users can manage their own invoice templates" ON public.invoice_templates;
CREATE POLICY "Users can manage their own invoice templates"
    ON public.invoice_templates
    FOR ALL
    USING (lawyer_id = auth.uid())
    WITH CHECK (lawyer_id = auth.uid());

DROP POLICY IF EXISTS "Users can view their own audit events" ON public.audit_events;
CREATE POLICY "Users can view their own audit events"
    ON public.audit_events
    FOR SELECT
    USING (lawyer_id = auth.uid());

DROP POLICY IF EXISTS "Users can view evaluations for their cases" ON public.ml_evaluations;
CREATE POLICY "Users can view evaluations for their cases"
    ON public.ml_evaluations
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM public.cases WHERE cases.id = ml_evaluations.case_id AND cases.lawyer_id = auth.uid()
    ));
