-- ====================================================================================
-- MÓDULO DE HOTELARIA & HOSPEDAGEM (SaaS: RED Comercial)
-- ====================================================================================
-- Objetivo: Criar as tabelas base para gestão de quartos e reservas de hóspedes.
-- Autor: RED Comercial AI Agent
-- ====================================================================================

-- 1. Criação da Tabela de Acomodações (Quartos)
CREATE TABLE public.acomodacoes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    numero TEXT NOT NULL,
    tipo TEXT NOT NULL DEFAULT 'padrao', -- padrao, luxo, chale
    capacidade INTEGER NOT NULL DEFAULT 2,
    diaria_padrao NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    status TEXT NOT NULL DEFAULT 'livre', -- livre, ocupado, manutencao, limpeza
    descricao TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(tenant_id, numero)
);

-- 2. Criação da Tabela de Reservas (Check-ins/Check-outs)
CREATE TABLE public.reservas (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    client_id UUID NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE, -- Hóspede Titular
    acomodacao_id UUID NOT NULL REFERENCES public.acomodacoes(id) ON DELETE CASCADE,
    data_checkin TIMESTAMP WITH TIME ZONE NOT NULL,
    data_checkout TIMESTAMP WITH TIME ZONE,
    status TEXT NOT NULL DEFAULT 'agendada', -- agendada, em_curso, finalizada, cancelada
    valor_total NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    observacoes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ====================================================================================
-- POLÍTICAS DE SEGURANÇA (Row Level Security - RLS)
-- ====================================================================================

-- Habilitar RLS nas novas tabelas
ALTER TABLE public.acomodacoes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reservas ENABLE ROW LEVEL SECURITY;

-- Políticas para Acomodações
CREATE POLICY "Tenants podem gerenciar apenas suas próprias acomodações"
    ON public.acomodacoes
    FOR ALL
    USING (tenant_id::text = current_setting('request.jwt.claims', true)::json->>'tenant_id')
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claims', true)::json->>'tenant_id');

-- Políticas para Reservas
CREATE POLICY "Tenants podem gerenciar apenas suas próprias reservas"
    ON public.reservas
    FOR ALL
    USING (tenant_id::text = current_setting('request.jwt.claims', true)::json->>'tenant_id')
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claims', true)::json->>'tenant_id');

-- ====================================================================================
-- GATILHOS (Triggers) PARA UPDATED_AT
-- ====================================================================================

-- Função utilitária (Cria caso o banco master não tenha ela instanciada)
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_acomodacoes_updated_at
    BEFORE UPDATE ON public.acomodacoes
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_reservas_updated_at
    BEFORE UPDATE ON public.reservas
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
