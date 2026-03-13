import os
import re
import ast

def parse_sql_schema(filepath):
    tables = {}
    current_table = None
    
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # Encontrar CREATE TABLE
            m = re.match(r'CREATE TABLE \w+\.(\w+)\s*\(', line)
            if m:
                current_table = m.group(1)
                tables[current_table] = set()
                continue
            
            # Estamos dentro de uma tabela
            if current_table:
                if line == ');':
                    current_table = None
                    continue
                # Se for CONSTRAINT, pular
                if line.startswith('CONSTRAINT'):
                    continue
                # Extrair o nome da coluna -> primeira palavra antes de espaço
                col_match = re.match(r'^([a-zA-Z0-9_]+)\s+', line)
                if col_match:
                    tables[current_table].add(col_match.group(1))
                    
    # Hardcoded para as VIEWS que criamos:
    if 'products' in tables:
        tables['produtos'] = tables['products']
        tables['produtos_estoque_baixo'] = tables['products']
    if 'clients' in tables:
        tables['clientes_inadimplentes'] = tables['clients']
        
    return tables

def check_python_backend(folder, schema_tables):
    print("\n--- ANALISANDO BACKEND PYTHON ---")
    errors = 0
    
    for root, dirs, files in os.walk(folder):
        for file in files:
            if not file.endswith('.py'): continue
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                
            # Regex para encontrar: sb.table('tabela').insert([ { dict } ]) ou update({ dict })
            # É complexo fazer com AST pois precisaríamos parsear o código inteiro que pode estar solto.
            # Vamos buscar os dicionários literais na string.
            for match in re.finditer(r'(?:sb|supabase)\.table\([\'"](\w+)[\'"]\)\.(insert|update)\(\s*\[?\s*(\{.*?\})\s*\]?\s*\)', content, re.DOTALL):
                table_name = match.group(1)
                action = match.group(2)
                dict_str = match.group(3)
                
                if table_name not in schema_tables:
                    print(f"[ERRO] Backend ({file}): Operação {action} na tabela '{table_name}' que NÃO EXISTE no SQL.")
                    errors += 1
                    continue
                
                # Encontrar chaves do dicionario: "chave": ou 'chave':
                keys = re.findall(r'[\'"](\w+)[\'"]\s*:', dict_str)
                for key in keys:
                    if key not in schema_tables[table_name]:
                        print(f"[ERRO] Backend ({file}): Injeção na coluna '{key}' da tabela '{table_name}', mas a coluna não existe no SQL!")
                        errors += 1
                        
    if errors == 0:
        print("Backend OK! Nenhuma injeção em coluna/tabela inexistente encontrada.")
    return errors

def check_frontend(folder, schema_tables):
    print("\n--- ANALISANDO FRONTEND REACT/ZUSTAND ---")
    errors = 0
    
    for root, dirs, files in os.walk(folder):
        if 'node_modules' in root or 'dist' in root: continue
        for file in files:
            if not file.endswith(('.ts', '.tsx', '.js', '.jsx')): continue
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                
            # Regex para supabase.from('tabela').insert({ }) ou update({ })
            for match in re.finditer(r'(?:supabase|sb)\.from\([\'"](\w+)[\'"]\)\.(insert|update|upsert)\(\s*(\{.*?\})\s*\)', content, re.DOTALL):
                table_name = match.group(1)
                action = match.group(2)
                dict_str = match.group(3)
                
                if table_name not in schema_tables:
                    print(f"[ERRO] Frontend ({file}): Operação {action} na tabela '{table_name}' inexistente.")
                    errors += 1
                    continue
                
                # Encontrar chaves em JS (pode ser "chave": ou apenas chave:)
                keys = re.findall(r'([{,]\s*)([\w]+)\s*:', dict_str)
                for _, key in keys:
                    if key not in schema_tables[table_name]:
                        print(f"[ERRO] Frontend ({file}): Injeção na coluna '{key}' da tabela '{table_name}', mas ela não existe no DDL!")
                        errors += 1
                        
    if errors == 0:
        print("Frontend OK! Nenhuma injeção em coluna/tabela inexistente encontrada.")
    return errors

if __name__ == "__main__":
    sql_file = "c:/Users/Ronyd/Desktop/REDCOMERCIAL/supabase/reset_master_schema.sql"
    backend_folder = "c:/Users/Ronyd/Desktop/REDCOMERCIAL/redbackend/app"
    frontend_folder = "c:/Users/Ronyd/Desktop/REDCOMERCIAL/redcomercial/src"
    
    print("Iniciando Verificação de Integridade Transversal (SQL -> Backend -> Frontend)...\n")
    
    schema = parse_sql_schema(sql_file)
    print(f"[{len(schema)} tabelas/views parseadas do reset_master_schema.sql]")
    
    err_back = check_python_backend(backend_folder, schema)
    err_front = check_frontend(frontend_folder, schema)
    
    if err_back + err_front == 0:
        print("\n✅ SUCESSO! Banco de Dados e Aplicações parecem estar 100% alinhados.")
    else:
        print(f"\n❌ FALHA! {err_back + err_front} conflito(s) de infraestrutura detectado(s).")
