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
    number TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    net NUMERIC NOT NULL,
    vat NUMERIC NOT NULL,
    vat_rate NUMERIC NOT NULL,
    currency TEXT DEFAULT 'EUR',
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Row Level Security (RLS)
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoices ENABLE ROW LEVEL SECURITY;

-- Policies for cases
CREATE POLICY "Users can manage their own cases"
    ON public.cases
    FOR ALL
    USING (lawyer_id = auth.uid())
    WITH CHECK (lawyer_id = auth.uid());

-- Policies for invoices
CREATE POLICY "Users can view invoices for their cases"
    ON public.invoices
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM public.cases WHERE cases.id = invoices.case_id AND cases.lawyer_id = auth.uid()
    ));
