-- UPGRADE CONCESSIONÁRIA - LEADS E CUSTOS

-- Tabela de Leads (CRM) se não existir (O schema master já tem uma básica, vamos turbinar)
CREATE TABLE IF NOT EXISTS public.leads (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
  nome text NOT NULL,
  telefone text,
  email text,
  origem text DEFAULT 'whatsapp', -- whatsapp, instagram, site, presencial
  status text DEFAULT 'novo', -- novo, em_atendimento, test_drive, proposta, fechado, perdido
  vehicle_id uuid REFERENCES public.vehicles(id) ON DELETE SET NULL,
  valor_oferta numeric,
  obs text,
  vendedor_id uuid REFERENCES public.tenant_users(id),
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  CONSTRAINT leads_pkey PRIMARY KEY (id)
);

-- Tabela de Custos de Preparação do Veículo
CREATE TABLE IF NOT EXISTS public.vehicle_costs (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
  vehicle_id uuid NOT NULL REFERENCES public.vehicles(id) ON DELETE CASCADE,
  descricao text NOT NULL,
  valor numeric NOT NULL DEFAULT 0,
  data date DEFAULT CURRENT_DATE,
  categoria text, -- mecanica, estetica, documentacao
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT vehicle_costs_pkey PRIMARY KEY (id)
);

-- Habilitar RLS
ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.vehicle_costs ENABLE ROW LEVEL SECURITY;

-- Políticas de Isolamento
DROP POLICY IF EXISTS "Tenant isolation for leads" ON public.leads;
DROP POLICY IF EXISTS "Tenant isolation for vehicle_costs" ON public.vehicle_costs;

CREATE POLICY "Tenant isolation for leads" ON public.leads FOR ALL USING (tenant_id = public.get_tenant_id());
CREATE POLICY "Tenant isolation for vehicle_costs" ON public.vehicle_costs FOR ALL USING (tenant_id = public.get_tenant_id());

-- Upgrade na tabela de vendas para suportar o fluxo de Concessionária (Baseado no sales.py v10)
DO $$ 
BEGIN 
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS valor_venda numeric DEFAULT 0;
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS valor_entrada numeric DEFAULT 0;
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS financiamento boolean DEFAULT false;
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS financiadora text;
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS parcelas integer DEFAULT 1;
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS juros_percentual numeric DEFAULT 0;
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS juros_tipo text DEFAULT 'price';
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS tipo_venda text DEFAULT 'nova';
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS vendedor_id uuid REFERENCES public.tenant_users(id);
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS data_venda date DEFAULT CURRENT_DATE;
    ALTER TABLE public.sales ADD COLUMN IF NOT EXISTS forma_entrada text DEFAULT 'Dinheiro';
EXCEPTION 
    WHEN others THEN NULL; 
END $$;
