-- ====================================================================================
-- MÓDULOS DE SAÚDE/ESTÉTICA & ACADEMIA (SaaS: RED Comercial)
-- ====================================================================================
-- Objetivo: Criar tabelas para agendas de clínicas e recorrência de alunos.
-- Autor: RED Comercial AI Agent
-- ====================================================================================

-- Função utilitária (Cria caso o banco master não tenha ela instanciada)
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ====================================================================================
-- 1. SAÚDE & ESTÉTICA: Tabela de Agendamentos
-- ====================================================================================
CREATE TABLE public.agendamentos (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    client_id UUID NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    prestador_nome TEXT, -- Nome do Médico, Dentista ou Barbeiro
    servico TEXT NOT NULL,
    data_hora TIMESTAMP WITH TIME ZONE NOT NULL,
    status TEXT NOT NULL DEFAULT 'agendado', -- agendado, em_atendimento, finalizado, cancelado
    valor NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    observacoes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ====================================================================================
-- 2. ACADEMIA & RECORRÊNCIA: Tabela de Assinaturas e Catraca
-- ====================================================================================
CREATE TABLE public.assinaturas (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    client_id UUID NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    plano_nome TEXT NOT NULL,
    valor_mensal NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    data_vencimento DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'ativo', -- ativo, inadimplente, congelado, cancelado
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(tenant_id, client_id) -- Um aluno por plano ativo na academia (simplificação)
);

CREATE TABLE public.checkins_catraca (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    client_id UUID NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    data_hora TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ====================================================================================
-- POLÍTICAS DE SEGURANÇA (Row Level Security - RLS)
-- ====================================================================================

ALTER TABLE public.agendamentos ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.assinaturas ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checkins_catraca ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Tenants gerenciam seus agendamentos" ON public.agendamentos
    FOR ALL USING (tenant_id::text = current_setting('request.jwt.claims', true)::json->>'tenant_id');

CREATE POLICY "Tenants gerenciam suas assinaturas" ON public.assinaturas
    FOR ALL USING (tenant_id::text = current_setting('request.jwt.claims', true)::json->>'tenant_id');

CREATE POLICY "Tenants gerenciam seus checkins de catraca" ON public.checkins_catraca
    FOR ALL USING (tenant_id::text = current_setting('request.jwt.claims', true)::json->>'tenant_id');

-- ====================================================================================
-- GATILHOS (Triggers) PARA UPDATED_AT
-- ====================================================================================
CREATE TRIGGER update_agendamentos_updated_at
    BEFORE UPDATE ON public.agendamentos
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_assinaturas_updated_at
    BEFORE UPDATE ON public.assinaturas
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
