-- LexFlow Supabase schema
-- Run this in the Supabase SQL Editor after creating the project.

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Lawyers / users table (managed by Supabase Auth, but we keep a profile)
CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
    full_name TEXT,
    firm_name TEXT,
    vat_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Cases table
CREATE TABLE IF NOT EXISTS public.cases (
    id TEXT PRIMARY KEY DEFAULT substring(uuid_generate_v4()::text, 1, 8),
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    client_name TEXT NOT NULL,
    client_email TEXT NOT NULL,
    case_type TEXT NOT NULL,
    destination TEXT NOT NULL,
    notes TEXT,
    stage TEXT DEFAULT 'documents',
    invoice_paid BOOLEAN DEFAULT FALSE,
    docs JSONB DEFAULT '[]'::jsonb,
    invoice JSONB,
    extracted JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Invoices table
CREATE TABLE IF NOT EXISTS public.invoices (
    id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES public.cases(id) ON DELETE CASCADE,
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
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
    case_id TEXT REFERENCES public.cases(id) ON DELETE SET NULL,
    case_name TEXT,
    invoice_id TEXT,
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    url TEXT,
    source TEXT DEFAULT 'intake',
    status TEXT DEFAULT 'unrecognized',
    content_type TEXT,
    size BIGINT DEFAULT 0,
    uploaded_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Invoice templates table
CREATE TABLE IF NOT EXISTS public.invoice_templates (
    id TEXT PRIMARY KEY,
    lawyer_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Audit events table
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

-- Row Level Security (RLS)
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoice_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;

-- Policies for cases
CREATE POLICY "Users can manage their own cases"
    ON public.cases
    FOR ALL
    USING (lawyer_id = auth.uid())
    WITH CHECK (lawyer_id = auth.uid());

-- Policies for invoices
CREATE POLICY "Users can view invoices for their cases"
    ON public.invoices
    FOR ALL
    USING (lawyer_id = auth.uid() OR EXISTS (
        SELECT 1 FROM public.cases WHERE cases.id = invoices.case_id AND cases.lawyer_id = auth.uid()
    ))
    WITH CHECK (lawyer_id = auth.uid());

-- Policies for documents
CREATE POLICY "Users can manage their own documents"
    ON public.documents
    FOR ALL
    USING (lawyer_id = auth.uid())
    WITH CHECK (lawyer_id = auth.uid());

-- Policies for invoice templates
CREATE POLICY "Users can manage their own invoice templates"
    ON public.invoice_templates
    FOR ALL
    USING (lawyer_id = auth.uid())
    WITH CHECK (lawyer_id = auth.uid());

-- Policies for audit events
CREATE POLICY "Users can view their own audit events"
    ON public.audit_events
    FOR SELECT
    USING (lawyer_id = auth.uid());
