import os
import re

tables = set()
for dp, dn, files in os.walk('c:/Users/Ronyd/Desktop/REDCOMERCIAL/redbackend/app'):
    for f in files:
        if f.endswith('.py'):
            filepath = os.path.join(dp, f)
            with open(filepath, encoding='utf-8', errors='ignore') as file:
                for line in file:
                    for m in re.finditer(r'\.table\([\'"](.*?)[\'"]\)', line):
                        tables.add(m.group(1))

extracted_tables = ['admin_logs', 'admin_users', 'caixa_sessions', 'clients', 'cupons_desconto', 'devolucoes', 'estoque_movimentacoes', 'itens_venda', 'leads', 'meta_vendas', 'notifications', 'order_items', 'orders', 'os', 'pagamentos_venda', 'products', 'sales', 'service_health', 'stock_movements', 'superadmin_settings', 'system_config', 'system_logs', 'system_metrics', 'tables', 'tenant_users', 'tenants', 'transactions', 'user_preferences', 'vehicles', 'vendas']

required = set(t.strip('\'"') for t in tables)
missing = required - set(extracted_tables)

if missing:
    print('Tabelas Faltando no SQL supabase.sql:')
    for m in missing:
        print(f" - {m}")
else:
    print('Todas as tabelas usadas no Python estão presentes no supabase.sql!')
    
# Check for RLS
with open('c:/Users/Ronyd/Desktop/REDCOMERCIAL/supabase/supabase.sql', encoding='utf-8', errors='ignore') as f:
    sql = f.read()
    
# Count RLS polices
print(f"\nPolíticas RLS Encontradas: {sql.count('CREATE POLICY')}")
