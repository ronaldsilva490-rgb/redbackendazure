import os
import subprocess
import shutil

temp_dir = os.path.join(os.environ.get("TEMP", "C:/Windows/Temp"), "pgsql_bin")
pg_dump_exe = os.path.join(temp_dir, "pgsql", "bin", "pg_dump.exe")

def dump_schema():
    env = os.environ.copy()
    env["PGPASSWORD"] = "!!@@##Ron@ld2580!!@@##"

    regions = [
        "us-east-1", "sa-east-1", "us-east-2", "us-west-1", "us-west-2",
        "eu-west-1", "eu-west-2", "eu-central-1", "ap-southeast-1"
    ]
    
    success = False

    for rg in regions:
        host = f"aws-0-{rg}.pooler.supabase.com"
        print(f"Tentando extrair pelo host da região: {rg}...")
        
        cmd = [
            pg_dump_exe,
            "-h", host,
            "-p", "5432",
            "-U", "postgres.jezqzcenohqdtcfscsdk",
            "-d", "postgres",
            "--schema-only",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges"
        ]

        result = subprocess.run(cmd, env=env, capture_output=True)

        if result.returncode == 0:
            out_file = r"c:\Users\Ronyd\Desktop\REDCOMERCIAL\redbackend\COMPLETE_SCHEMA.sql"
            with open(out_file, "wb") as f:
                f.write(result.stdout)
            print("🎉 Esquema extraído e salvo com sucesso no COMPLETE_SCHEMA.sql!")
            success = True
            break
        else:
            err = result.stderr.decode("utf-8", errors="ignore")
            if "Tenant or user not found" in err:
                print(f"🚫 Região incorreta: {rg}")
            elif "password authentication failed" in err:
                print(f"🚫 Senha incorreta detectada na região: {rg}!")
                break
            else:
                print(f"Erro inesperado em {rg}: {err}")

    if not success:
        print("❌ Nenhuma região foi bem-sucedida.")

if __name__ == "__main__":
    dump_schema()
