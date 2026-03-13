import os
import re

print("Mapeando relacionamentos Rotas x Tabelas...")
mapa = {}

rx = re.compile(r'sb\.table\([\'"](.*?)[\'"]\)\.(select|insert|update|upsert|delete)')

for dp, dn, files in os.walk('c:/Users/Ronyd/Desktop/REDCOMERCIAL/redbackend/app/routes'):
    for f in files:
        if f.endswith('.py'):
            filepath = os.path.join(dp, f)
            with open(filepath, encoding='utf-8', errors='ignore') as file:
                for line in file:
                    for m in rx.finditer(line):
                        tabela = m.group(1)
                        acao = m.group(2)
                        
                        if f not in mapa:
                            mapa[f] = {}
                        if tabela not in mapa[f]:
                            mapa[f][tabela] = set()
                        
                        mapa[f][tabela].add(acao)

# Imprimir formato relatorio
for rota, tabelas in mapa.items():
    print(f"\n[{rota}]")
    for tab, acoes in tabelas.items():
        print(f"  -> {tab}: {', '.join(acoes)}")
