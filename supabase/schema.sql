-- LexFlow Supabase schema
-- Run this in the Supabase SQL Editor after creating the project.

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Lawyers / users table (managed by Supabase Auth, but we keep a profile)
CREATE TABLE IF NOT EXISTS public.firms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    vat_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
    email TEXT,
    full_name TEXT,
    firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL,
    firm_name TEXT,
    vat_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Cases table
CREATE TABLE IF NOT EXISTS public.cases (
    id TEXT PRIMARY KEY DEFAULT substring(uuid_generate_v4()::text, 1, 8),
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL,
    client_name TEXT NOT NULL,
    client_email TEXT NOT NULL,
    case_type TEXT NOT NULL,
    destination TEXT NOT NULL,
    notes TEXT,
    stage TEXT DEFAULT 'documents',
    invoice_paid BOOLEAN DEFAULT FALSE,
    route_code TEXT,
    control_state JSONB DEFAULT '{}'::jsonb,
    docs JSONB DEFAULT '[]'::jsonb,
    invoice JSONB,
    extracted JSONB DEFAULT '{}'::jsonb,
    public_notes TEXT,
    public_submission_completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Invoices table
CREATE TABLE IF NOT EXISTS public.invoices (
    id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES public.cases(id) ON DELETE CASCADE,
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL,
    number TEXT NOT NULL,
    status TEXT DEFAULT 'draft',
    client_name TEXT,
    client_email TEXT,
    issue_date TEXT,
    due_date TEXT,
    notes TEXT,
    template_id TEXT,
    items JSONB DEFAULT '[]'::jsonb,
    attachments JSONB DEFAULT '[]'::jsonb,
    sent_at TIMESTAMPTZ,
    last_sent_to TEXT,
    amount NUMERIC DEFAULT 0,
    net NUMERIC DEFAULT 0,
    vat NUMERIC DEFAULT 0,
    vat_rate NUMERIC DEFAULT 0,
    currency TEXT DEFAULT 'EUR',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Documents table
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
    document_family TEXT,
    automation_status TEXT,
    automation_note TEXT,
    manual_review_required BOOLEAN DEFAULT FALSE,
    quality_status TEXT,
    authenticity_status TEXT,
    translation_status TEXT,
    notes TEXT,
    extracted JSONB DEFAULT '{}'::jsonb,
    content_type TEXT,
    size BIGINT DEFAULT 0,
    uploaded_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Invoice templates table
CREATE TABLE IF NOT EXISTS public.invoice_templates (
    id TEXT PRIMARY KEY,
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    firm_id TEXT REFERENCES public.firms(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Audit events table
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

-- ML/OCR evaluation table
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

-- Row Level Security (RLS)
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.firms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoice_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ml_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.email_integrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notifications ENABLE ROW LEVEL SECURITY;

-- Policies for cases
CREATE POLICY "Users can manage their own cases"
    ON public.cases
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

-- Policies for invoices
CREATE POLICY "Users can view invoices for their cases"
    ON public.invoices
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()) OR EXISTS (
        SELECT 1 FROM public.cases WHERE cases.id = invoices.case_id AND (cases.lawyer_id = auth.uid() OR cases.firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    ))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

-- Policies for documents
CREATE POLICY "Users can manage their own documents"
    ON public.documents
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

-- Policies for invoice templates
CREATE POLICY "Users can manage their own invoice templates"
    ON public.invoice_templates
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

-- Policies for audit events
CREATE POLICY "Users can view their own audit events"
    ON public.audit_events
    FOR SELECT
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

-- Policies for ML/OCR evaluations
CREATE POLICY "Users can view evaluations for their cases"
    ON public.ml_evaluations
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM public.cases WHERE cases.id = ml_evaluations.case_id AND (cases.lawyer_id = auth.uid() OR cases.firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    ));

CREATE POLICY "Users can manage firms through their profile"
    ON public.firms
    FOR ALL
    USING (id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

CREATE POLICY "Users can manage email integrations for their firm"
    ON public.email_integrations
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));

CREATE POLICY "Users can manage notifications for their firm"
    ON public.notifications
    FOR ALL
    USING (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()))
    WITH CHECK (lawyer_id = auth.uid() OR firm_id IN (SELECT firm_id FROM public.profiles WHERE id = auth.uid()));
