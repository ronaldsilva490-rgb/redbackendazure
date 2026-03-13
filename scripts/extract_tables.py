import os, re
tables = set()
for dp, dn, filenames in os.walk('c:/Users/Ronyd/Desktop/REDCOMERCIAL/redbackend/app'):
    for f in filenames:
        if f.endswith('.py'):
            filepath = os.path.join(dp, f)
            with open(filepath, encoding='utf-8', errors='ignore') as file:
                for line in file:
                    for m in re.finditer(r'\.table\([\'"](.*?)[\'"]\)', line):
                        tables.add(m.group(1))
print("\n".join(sorted(list(set(t.strip('\'"') for t in tables)))))
