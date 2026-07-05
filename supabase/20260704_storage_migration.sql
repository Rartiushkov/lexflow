-- LexFlow storage migration
-- Run in Supabase SQL Editor for an existing project.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS public.firms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    vat_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE public.cases ADD COLUMN IF NOT EXISTS firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL;
ALTER TABLE public.cases ADD COLUMN IF NOT EXISTS public_notes TEXT;
ALTER TABLE public.cases ADD COLUMN IF NOT EXISTS public_submission_completed_at TIMESTAMPTZ;
ALTER TABLE public.cases ADD COLUMN IF NOT EXISTS route_code TEXT;
ALTER TABLE public.cases ADD COLUMN IF NOT EXISTS control_state JSONB DEFAULT '{}'::jsonb;

ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL;
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
    firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL,
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
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS document_type TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS document_family TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS automation_status TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS automation_note TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS manual_review_required BOOLEAN DEFAULT FALSE;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS quality_status TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS authenticity_status TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS translation_status TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS extracted JSONB DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS public.invoice_templates (
    id TEXT PRIMARY KEY,
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.audit_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL,
    case_id TEXT REFERENCES public.cases(id) ON DELETE SET NULL,
    document_id TEXT,
    invoice_id TEXT,
    action TEXT NOT NULL,
    payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.ml_evaluations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL,
    case_id TEXT REFERENCES public.cases(id) ON DELETE SET NULL,
    document_id TEXT,
    model TEXT NOT NULL,
    score NUMERIC DEFAULT 0,
    passed BOOLEAN DEFAULT FALSE,
    suggestions JSONB DEFAULT '[]'::jsonb,
    payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.email_integrations (
    id TEXT PRIMARY KEY,
    firm_id TEXT REFERENCES public.firms(id) ON DELETE CASCADE,
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'gmail',
    auth_type TEXT NOT NULL DEFAULT 'app_password',
    email TEXT NOT NULL,
    app_password TEXT,
    access_token TEXT,
    refresh_token TEXT,
    token_expires_at DOUBLE PRECISION,
    imap_host TEXT DEFAULT 'imap.gmail.com',
    mailbox TEXT DEFAULT 'INBOX',
    poll_limit INTEGER DEFAULT 10,
    active BOOLEAN DEFAULT TRUE,
    last_polled_at TIMESTAMPTZ,
    last_processed_message_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.notifications (
    id TEXT PRIMARY KEY,
    firm_id TEXT REFERENCES public.firms(id) ON DELETE CASCADE,
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    case_id TEXT REFERENCES public.cases(id) ON DELETE CASCADE,
    severity TEXT NOT NULL DEFAULT 'info',
    kind TEXT NOT NULL DEFAULT 'action_required',
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    payload JSONB DEFAULT '{}'::jsonb,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.email_integrations ADD COLUMN IF NOT EXISTS access_token TEXT;
ALTER TABLE public.email_integrations ADD COLUMN IF NOT EXISTS refresh_token TEXT;
ALTER TABLE public.email_integrations ADD COLUMN IF NOT EXISTS token_expires_at DOUBLE PRECISION;
ALTER TABLE public.email_integrations ALTER COLUMN app_password DROP NOT NULL;

ALTER TABLE public.firms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoice_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ml_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.email_integrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notifications ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can manage their own cases" ON public.cases;
CREATE POLICY "Users can manage their own cases"
    ON public.cases
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

DROP POLICY IF EXISTS "Users can view invoices for their cases" ON public.invoices;
CREATE POLICY "Users can view invoices for their cases"
    ON public.invoices
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()) OR EXISTS (
        SELECT 1 FROM public.cases WHERE cases.id = invoices.case_id AND (cases.lawyer_id = auth.uid() OR cases.firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    ))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

DROP POLICY IF EXISTS "Users can manage their own documents" ON public.documents;
CREATE POLICY "Users can manage their own documents"
    ON public.documents
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

DROP POLICY IF EXISTS "Users can manage their own invoice templates" ON public.invoice_templates;
CREATE POLICY "Users can manage their own invoice templates"
    ON public.invoice_templates
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

DROP POLICY IF EXISTS "Users can view their own audit events" ON public.audit_events;
CREATE POLICY "Users can view their own audit events"
    ON public.audit_events
    FOR SELECT
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

DROP POLICY IF EXISTS "Users can view evaluations for their cases" ON public.ml_evaluations;
CREATE POLICY "Users can view evaluations for their cases"
    ON public.ml_evaluations
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM public.cases WHERE cases.id = ml_evaluations.case_id AND (cases.lawyer_id = auth.uid() OR cases.firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    ));

DROP POLICY IF EXISTS "Users can manage firms through their profile" ON public.firms;
CREATE POLICY "Users can manage firms through their profile"
    ON public.firms
    FOR ALL
    USING (id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

DROP POLICY IF EXISTS "Users can manage email integrations for their firm" ON public.email_integrations;
CREATE POLICY "Users can manage email integrations for their firm"
    ON public.email_integrations
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

DROP POLICY IF EXISTS "Users can manage notifications for their firm" ON public.notifications;
CREATE POLICY "Users can manage notifications for their firm"
    ON public.notifications
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));
