"""
admin.py — Painel administrativo centralizado.
Consolida: autenticação de admin, tenants, status do sistema e logs.
Todas as rotas exigem admin_token (JWT próprio, independente do Supabase).
"""
import os, time, jwt, bcrypt, requests
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify
from ..utils.supabase_client import get_supabase_admin
from ..utils.response import success, error

admin_bp = Blueprint("admin", __name__)

# ── Env vars ──────────────────────────────────────────────
JWT_SECRET      = os.getenv("JWT_SECRET", "red-admin-secret-change-in-prod")
JWT_EXPIRY_DAYS = int(os.getenv("JWT_EXPIRY_DAYS", 7))
PALAVRA_MESTRE  = os.getenv("ADMIN_PALAVRA_MESTRE", "redmaster2024")

# Infraestrutura monitorada
FLY_URL              = os.getenv("FLY_URL", "https://redbackend.fly.dev")
FLY_API_TOKEN        = os.getenv("FLY_API_TOKEN", "").strip()
FLY_APP_NAME         = os.getenv("FLY_APP_NAME", "redbackend").strip()
GITHUB_TOKEN         = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO          = os.getenv("GITHUB_REPO", "")
GITHUB_BACKEND_REPO  = os.getenv("GITHUB_BACKEND_REPO", "")
VERCEL_TOKEN         = os.getenv("VERCEL_TOKEN", "")
VERCEL_PROJECT_ID    = os.getenv("VERCEL_PROJECT_ID", "")


# ── JWT helpers ───────────────────────────────────────────
def generate_admin_token(admin_id: str) -> str:
    payload = {
        "admin_id": admin_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_admin_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None


# ── Auth guard ────────────────────────────────────────────
def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        if not token:
            return jsonify({"error": "Token não fornecido"}), 401
        payload = verify_admin_token(token)
        if not payload:
            return jsonify({"error": "Token inválido ou expirado"}), 401
        request.admin_id = payload.get("admin_id")
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════

@admin_bp.post("/register")
def admin_register():
    """Cria novo admin. Requer palavra-mestre."""
    body          = request.get_json() or {}
    nome          = (body.get("nome") or "").strip()
    username      = (body.get("username") or "").strip().lower()
    email         = (body.get("email") or "").strip().lower()
    senha         = body.get("senha") or ""
    palavra_mestre = body.get("palavra_mestre") or ""

    if not all([nome, username, email, senha, palavra_mestre]):
        return error("Todos os campos são obrigatórios")

    if palavra_mestre != PALAVRA_MESTRE:
        return error("Palavra-mestre incorreta", 403)

    if len(username) < 3:
        return error("Username deve ter pelo menos 3 caracteres")

    if len(senha) < 6:
        return error("Senha deve ter pelo menos 6 caracteres")

    sb = get_supabase_admin()
    try:
        existing = sb.table("admin_users") \
            .select("id") \
            .or_(f"username.eq.{username},email.eq.{email}") \
            .execute()
        if existing.data:
            return error("Username ou e-mail já cadastrado")

        hashed = bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()
        resp   = sb.table("admin_users").insert({
            "nome":     nome,
            "username": username,
            "email":    email,
            "senha_hash": hashed,
            "ativo":    True,
        }).execute()

        admin = resp.data[0]
        return success({"admin": {"id": admin["id"], "nome": nome, "username": username}},
                       "Administrador criado com sucesso", 201)
    except Exception as e:
        msg = str(e)
        if "admin_users" in msg and "does not exist" in msg:
            return error("Tabela admin_users não encontrada. Execute o schema SQL no Supabase.", 503)
        return error(f"Erro ao criar administrador: {msg}", 500)


@admin_bp.post("/login")
def admin_login():
    """Login de admin com username ou e-mail."""
    body  = request.get_json() or {}
    login = (body.get("login") or "").strip()
    senha = body.get("senha") or ""

    if not login or not senha:
        return error("Login e senha são obrigatórios")

    sb = get_supabase_admin()
    try:
        query = sb.table("admin_users").select("*")
        if "@" in login:
            query = query.eq("email", login.lower())
        else:
            query = query.eq("username", login.lower())

        resp  = query.eq("ativo", True).limit(1).execute()
        if not resp.data:
            return error("Credenciais inválidas", 401)

        admin = resp.data[0]
        if not bcrypt.checkpw(senha.encode(), admin["senha_hash"].encode()):
            return error("Credenciais inválidas", 401)

        token = generate_admin_token(admin["id"])
        return success({
            "access_token": token,
            "admin": {
                "id":       admin["id"],
                "nome":     admin["nome"],
                "username": admin["username"],
                "email":    admin["email"],
            }
        })
    except Exception as e:
        msg = str(e)
        if "admin_users" in msg and "does not exist" in msg:
            return error("Tabela admin_users não encontrada. Execute o schema SQL no Supabase.", 503)
        return error(f"Erro ao fazer login: {msg}", 500)


@admin_bp.get("/verifica-token")
def admin_verifica_token():
    """Verifica validade do token de admin."""
    auth  = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    if not token:
        return error("Token não fornecido", 401)

    payload = verify_admin_token(token)
    if not payload:
        return error("Token inválido ou expirado", 401)

    sb = get_supabase_admin()
    try:
        resp = sb.table("admin_users") \
            .select("id, nome, username, email, ativo") \
            .eq("id", payload["admin_id"]) \
            .execute()
        if not resp.data or not resp.data[0]["ativo"]:
            return error("Admin não encontrado ou desativado", 401)
        return success({"admin": resp.data[0], "token_valido": True})
    except Exception:
        return error("Erro ao verificar token", 500)


@admin_bp.get("/list")
@require_admin
def list_admins():
    """Lista todos os administradores."""
    sb = get_supabase_admin()
    try:
        resp = sb.table("admin_users") \
            .select("id, nome, username, email, ativo, criado_em") \
            .order("criado_em", desc=True) \
            .execute()
        return success({"admins": resp.data or []})
    except Exception as e:
        msg = str(e)
        if "admin_users" in msg and "does not exist" in msg:
            return error("Tabela admin_users não encontrada. Execute o schema SQL.", 503)
        return error(f"Erro ao listar admins: {msg}", 500)


@admin_bp.post("/deactivate/<admin_id>")
@require_admin
def deactivate_admin(admin_id):
    sb = get_supabase_admin()
    sb.table("admin_users").update({"ativo": False}).eq("id", admin_id).execute()
    return success(message="Admin desativado")


@admin_bp.post("/activate/<admin_id>")
@require_admin
def activate_admin(admin_id):
    sb = get_supabase_admin()
    sb.table("admin_users").update({"ativo": True}).eq("id", admin_id).execute()
    return success(message="Admin ativado")


@admin_bp.delete("/<admin_id>")
@require_admin
def delete_admin(admin_id):
    if str(admin_id) == str(request.admin_id):
        return error("Você não pode deletar sua própria conta")
    sb = get_supabase_admin()
    try:
        # Verifica se o admin existe antes de deletar
        check = sb.table("admin_users").select("id").eq("id", admin_id).execute()
        if not check.data:
            return error("Administrador não encontrado", 404)
        sb.table("admin_users").delete().eq("id", admin_id).execute()
        return success(message="Admin removido")
    except Exception as e:
        return error(f"Erro ao remover administrador: {str(e)}", 500)


# ═══════════════════════════════════════════════════════════
# TENANTS
# ═══════════════════════════════════════════════════════════

@admin_bp.get("/tenants")
@require_admin
def list_tenants():
    """Lista todos os tenants (empresas) com contagem de usuários."""
    sb   = get_supabase_admin()
    resp = sb.table("tenants").select("*, tenant_users(count)").execute()
    data = resp.data or []
    for t in data:
        count = t.get("tenant_users", [])
        t["user_count"] = count[0]["count"] if count else 0
        t.pop("tenant_users", None)
    return success(data)


@admin_bp.patch("/tenants/<tenant_id>")
@require_admin
def update_tenant(tenant_id):
    body = request.get_json() or {}
    sb   = get_supabase_admin()
    sb.table("tenants").update(body).eq("id", tenant_id).execute()
    return success(message="Tenant atualizado")


# ═══════════════════════════════════════════════════════════
# SYSTEM STATUS
# ═══════════════════════════════════════════════════════════

@admin_bp.get("/status")
@require_admin
def system_status():
    """Health check de todos os serviços da infraestrutura."""
    results = {}

    # Backend (Fly.io)
    try:
        t0 = time.perf_counter()
        r  = requests.get(FLY_URL, timeout=8)
        results["backend"] = {
            "ok": r.status_code == 200,
            "label": "Backend Fly.io",
            "latency_ms": round((time.perf_counter() - t0) * 1000),
        }
    except Exception as e:
        results["backend"] = {"ok": False, "label": "Backend Fly.io", "error": str(e)[:80]}

    # Supabase DB
    try:
        t0 = time.perf_counter()
        sb = get_supabase_admin()
        sb.table("tenants").select("id").limit(1).execute()
        results["supabase"] = {
            "ok": True,
            "label": "Supabase DB",
            "latency_ms": round((time.perf_counter() - t0) * 1000),
        }
    except Exception as e:
        results["supabase"] = {"ok": False, "label": "Supabase DB", "error": str(e)[:120]}

    # Vercel Frontend
    try:
        t0 = time.perf_counter()
        r  = requests.head("https://redcomercialweb.vercel.app", timeout=8, allow_redirects=True)
        results["vercel"] = {
            "ok": r.status_code < 400,
            "label": "Vercel Frontend",
            "latency_ms": round((time.perf_counter() - t0) * 1000),
        }
    except Exception as e:
        results["vercel"] = {"ok": False, "label": "Vercel Frontend", "error": str(e)[:80]}

    # GitHub Frontend
    try:
        t0 = time.perf_counter()
        if GITHUB_TOKEN and GITHUB_REPO:
            r = requests.head(
                f"https://api.github.com/repos/{GITHUB_REPO}",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
                timeout=8,
            )
            results["github"] = {
                "ok": r.status_code in [200, 405],
                "label": "GitHub Frontend",
                "latency_ms": round((time.perf_counter() - t0) * 1000),
            }
        else:
            results["github"] = {"ok": None, "label": "GitHub Frontend", "error": "GITHUB_REPO não configurado"}
    except Exception as e:
        results["github"] = {"ok": False, "label": "GitHub Frontend", "error": str(e)[:80]}

    # GitHub Backend
    try:
        t0   = time.perf_counter()
        repo = GITHUB_BACKEND_REPO or GITHUB_REPO
        if GITHUB_TOKEN and repo:
            r = requests.head(
                f"https://api.github.com/repos/{repo}",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
                timeout=8,
            )
            results["github_backend"] = {
                "ok": r.status_code in [200, 405],
                "label": "GitHub Backend",
                "latency_ms": round((time.perf_counter() - t0) * 1000),
            }
        else:
            results["github_backend"] = {"ok": None, "label": "GitHub Backend", "error": "GITHUB_BACKEND_REPO não configurado"}
    except Exception as e:
        results["github_backend"] = {"ok": False, "label": "GitHub Backend", "error": str(e)[:80]}

    return success(results)


# ═══════════════════════════════════════════════════════════
# NETWORK INFO
# ═══════════════════════════════════════════════════════════

@admin_bp.get("/network-info")
@require_admin
def network_info():
    """Retorna IP WAN + geolocalização do backend, frontend e admin."""
    import socket

    def geolocate(ip: str) -> dict:
        """
        Geolocaliza um IP usando ip-api.com (gratuito, sem chave, 45 req/min).
        Fallback para ipwho.is se falhar.
        """
        try:
            r = requests.get(
                f"http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,regionName,city,lat,lon,timezone,org,isp",
                timeout=6
            )
            if r.status_code == 200 and r.text.strip():
                d = r.json()
                if d.get("status") == "success":
                    return {
                        "city":         d.get("city"),
                        "region":       d.get("regionName"),
                        "country":      d.get("country"),
                        "country_code": d.get("countryCode"),
                        "latitude":     d.get("lat"),
                        "longitude":    d.get("lon"),
                        "org":          d.get("org") or d.get("isp"),
                        "timezone":     d.get("timezone"),
                    }
        except Exception:
            pass

        # Fallback: ipwho.is
        try:
            r2 = requests.get(f"https://ipwho.is/{ip}", timeout=6)
            if r2.status_code == 200 and r2.text.strip():
                d = r2.json()
                if d.get("success"):
                    return {
                        "city":         d.get("city"),
                        "region":       d.get("region"),
                        "country":      d.get("country"),
                        "country_code": d.get("country_code"),
                        "latitude":     d.get("latitude"),
                        "longitude":    d.get("longitude"),
                        "org":          d.get("connection", {}).get("org"),
                        "timezone":     d.get("timezone", {}).get("id"),
                    }
        except Exception:
            pass

        return None

    result = {
        "backend":  {"ip": None, "geo": None, "error": None},
        "frontend": {"ip": None, "geo": None, "error": None},
        "client":   {"ip": None, "geo": None, "error": None},
    }

    # ── IP WAN do backend (Fly.io) ──
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=6)
        backend_ip = r.json().get("ip")
        result["backend"]["ip"] = backend_ip
        if backend_ip:
            result["backend"]["geo"] = geolocate(backend_ip)
    except Exception as e:
        result["backend"]["error"] = str(e)[:120]

    # ── IP do frontend (Vercel) via DNS ──
    try:
        vercel_url = os.getenv("VERCEL_FRONTEND_URL", "https://redcomercialweb.vercel.app")
        host = vercel_url.replace("https://", "").replace("http://", "").split("/")[0]
        frontend_ip = socket.gethostbyname(host)
        result["frontend"]["ip"] = frontend_ip
        if frontend_ip:
            result["frontend"]["geo"] = geolocate(frontend_ip)
    except Exception as e:
        result["frontend"]["error"] = str(e)[:120]

    # ── IP real do admin (extraído do request) ──
    try:
        forwarded = request.headers.get("X-Forwarded-For", "")
        client_ip = forwarded.split(",")[0].strip() if forwarded else request.remote_addr
        result["client"]["ip"] = client_ip

        if client_ip and client_ip not in ("127.0.0.1", "::1"):
            result["client"]["geo"] = geolocate(client_ip)
        else:
            result["client"]["geo"] = {"city": "Localhost", "country": "Local", "country_code": None}
    except Exception as e:
        result["client"]["error"] = str(e)[:120]

    return success(result)


# ═══════════════════════════════════════════════════════════
# LOGS (DATABASE + FALLBACK FLY.IO)
# ═══════════════════════════════════════════════════════════

@admin_bp.get("/logs")
@require_admin
def get_logs():
    """Lista logs salvos no banco de dados Supabase."""
    nivel   = request.args.get("nivel")
    servico = request.args.get("servico")
    busca   = request.args.get("busca", "").strip()
    limit   = min(int(request.args.get("limit", 100)), 500)
    
    sb = get_supabase_admin()
    try:
        q = sb.table("system_logs").select("*").order("created_at", desc=True)
        if nivel:   q = q.eq("level", nivel)
        if servico: q = q.eq("service", servico)
        if busca:   q = q.ilike("message", f"%{busca}%")
        
        resp = q.limit(limit).execute()
        return success({"data": resp.data or [], "total": len(resp.data or [])})
    except Exception as e:
        return error(f"Erro ao carregar logs do banco: {str(e)}", 500)

@admin_bp.delete("/logs")
@require_admin
def clear_all_logs():
    """Wipe total de logs na DB e confirma limpeza visual."""
    sb = get_supabase_admin()
    try:
        # Remove TUDO da tabela de logs legada
        sb.table("system_logs").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
        return success(message="Cache de logs da base de dados limpo com sucesso.")
    except Exception as e:
        return error(f"Erro ao limpar banco de dados: {str(e)}", 500)


@admin_bp.get("/fly-logs")
@require_admin
def get_fly_logs():
    """Busca logs reais do Fly.io via GraphQL API."""
    if not FLY_API_TOKEN or not FLY_APP_NAME:
        return error("FLY_API_TOKEN ou FLY_APP_NAME não configurados no backend", 500)

    url = "https://api.fly.io/graphql"
    headers = {
        "Authorization": f"Bearer {FLY_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Query otimizada com limite explícito de 100
    query = """
    query($appName: String!) {
      app(name: $appName) {
        name
        status
        logs {
          nodes {
            timestamp
            message
            level
          }
        }
      }
    }
    """
    
    try:
        resp = requests.post(url, json={
            "query": query,
            "variables": {"appName": FLY_APP_NAME}
        }, headers=headers, timeout=12)
        
        if resp.status_code != 200:
            return error(f"Erro Fly API ({resp.status_code}): {resp.text[:200]}", 500)
            
        data = resp.json()
        app_data = data.get("data", {}).get("app")
        
        if not app_data:
            return error(f"Aplicativo '{FLY_APP_NAME}' não encontrado no Fly.io. Verifique o FLY_APP_NAME.", 404)
            
        logs = app_data.get("logs", {}).get("nodes", [])
        
        # Inverte para ordem de terminal (mais novos embaixo)
        logs.reverse() 
        
        return success(logs)
    except Exception as e:
        return error(f"Falha ao conectar na API do Fly: {str(e)}", 500)


# ═══════════════════════════════════════════════════════════
# INTEGRAÇÃO WHATSAPP
# ═══════════════════════════════════════════════════════════

@admin_bp.post("/whatsapp/send")
@require_admin
def whatsapp_send():
    """
    Dispara mensagens de teste utilizando a engine escolhida
    - 'oficial' (Meta Cloud API)
    - 'qrcode' (Microserviço Baileys em Node)
    """
    data = request.json or {}
    engine = data.get('engine', 'oficial')
    number = data.get('number', '').strip()
    message = data.get('message', '').strip()
    configs = data.get('configs', {})

    if not number or not message:
        return error("Número e mensagem são obrigatórios", 400)

    # Lógica Meta Oficial
    if engine == 'oficial':
        token = configs.get('token')
        phone_id = configs.get('phoneId')
        
        if not token or not phone_id:
            return error("Token e Phone ID são obrigatórios para a Cloud API Oficial", 400)
            
        url = f"https://graph.facebook.com/v17.0/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # Garante o formato do número
        safe_number = number if number.startswith('55') else f"55{number}"
        
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": safe_number,
            "type": "text",
            "text": {"preview_url": False, "body": message}
        }
        
        try:
            resp = requests.post(url, json=payload, headers=headers)
            if resp.status_code in [200, 201]:
                return success({"message_id": resp.json().get("messages", [{}])[0].get("id"), "status": "Enviado"})
            else:
                return error(f"Refusão Graph API: {resp.text}", resp.status_code)
        except Exception as e:
            return error(f"Falha de conexão com a Meta: {str(e)}", 500)
            
      # Lógica Microserviço (QR Code Baileys)
    elif engine == 'qrcode':
        import os
        node_url = os.environ.get('WHATSAPP_SERVICE_URL', 'http://localhost:3001')
        try:
            # Enviamos ao microserviço Node
            payload = {
                "number": number,
                "message": message
            }
            resp = requests.post(f"{node_url}/send", json=payload, timeout=5)
            if resp.status_code == 200:
                return success({"status": "Serviço QRCode completou o envio"})
            else:
                return error(f"Erro no serviço local WhatsApp: {resp.text}", 500)
        except requests.exceptions.ConnectionError:
            return error(f"O microserviço Node ({node_url}) parece estar offline ou não inicializado.", 500)
        except Exception as e:
             return error(f"Falha de gateway: {str(e)}", 500)
             
    return error("Engine desconhecida.", 400)


@admin_bp.post("/whatsapp/start")
@require_admin
def whatsapp_start():
    """Inicia o microserviço para a sessão 'admin'."""
    import os, time
    node_url = os.environ.get('WHATSAPP_SERVICE_URL', 'http://localhost:3001')
    
    # Retenta até 3 vezes caso o microserviço esteja em "cold start"
    for i in range(3):
        try:
            resp = requests.post(f"{node_url}/start/admin", timeout=10)
            try:
                data = resp.json()
            except:
                data = {"message": resp.text}
            return success(data, status=resp.status_code)
        except requests.exceptions.ConnectionError:
            if i < 2:
                time.sleep(2) # Espera o boot do Node.js
                continue
            return success({"status": "starting", "message": "Microserviço em boot. Aguarde o QR Code."})
        except Exception as e:
            return error(f"Falha na comunicação com microserviço: {str(e)}", 500)
    
    return error("Microserviço não respondeu após várias tentativas.", 504)

@admin_bp.post("/whatsapp/stop")
@require_admin
def whatsapp_stop():
    """Para o microserviço para a sessão 'admin'."""
    import os
    node_url = os.environ.get('WHATSAPP_SERVICE_URL', 'http://localhost:3001')
    try:
        resp = requests.post(f"{node_url}/stop/admin", timeout=10)
        try:
            data = resp.json()
        except:
            data = {"message": resp.text}
        return success(data, status=resp.status_code)
    except requests.exceptions.ConnectionError:
        return success({"message": "Serviço já estava offline ou em boot."})
    except Exception as e:
        return error(f"Falha na comunicação com microserviço: {str(e)}", 500)

@admin_bp.get("/whatsapp/status")
@require_admin
def whatsapp_status():
    """Consome o microserviço Node.js para relatar Status / Base64-QR"""
    import os
    node_url = os.environ.get('WHATSAPP_SERVICE_URL', 'http://localhost:3001')
    try:
        # Chamada direta para o endpoint de admin para evitar 302
        resp = requests.get(f"{node_url}/status/admin", timeout=10)
        if resp.status_code == 200:
            return success(resp.json())
        return error(f"Falha do serviço Node: {resp.status_code}", 500)
    except requests.exceptions.ConnectionError:
         return success({"status": "offline", "qr": None, "msg": "Microserviço Offline"})
    except Exception as e:
         return error(f"Falha ao conectar no microserviço Node: {str(e)}", 500)

@admin_bp.get("/whatsapp/groups")
@require_admin
def whatsapp_groups():
    """Consome o microserviço Node.js para relatar a listagem de Grupos do WhatsApp"""
    import os
    node_url = os.environ.get('WHATSAPP_SERVICE_URL', 'http://localhost:3001')
    try:
        # Chamada direta para admin
        resp = requests.get(f"{node_url}/groups/admin", timeout=15)
        if resp.status_code == 200:
            return success(resp.json())
        return error(f"Falha do serviço Node: {resp.text}", 500)
    except requests.exceptions.ConnectionError:
         return error("O microserviço Node está offline.", 503)
    except Exception as e:
         return error(f"Falha ao conectar no microserviço Node: {str(e)}", 500)


# ═══════════════════════════════════════════════════════════
# CONFIGURAÇÕES DE IA (GEMINI)
# ═══════════════════════════════════════════════════════════

@admin_bp.get("/ai/configs")
@require_admin
def get_ai_configs():
    """Retorna as configurações de IA da tabela ai_configs."""
    sb = get_supabase_admin()
    try:
        resp = sb.table("ai_configs").select("*").execute()
        # Converte lista de objetos {key, value} em um dicionário plano
        configs = {item['key']: item['value'] for item in resp.data}
        return success(configs)
    except Exception as e:
        return error(f"Erro ao buscar configs de IA: {str(e)}", 500)

@admin_bp.post("/ai/configs")
@require_admin
def update_ai_configs():
    """Atualiza as configurações de IA e notifica o microserviço WhatsApp."""
    body = request.get_json() or {}
    sb = get_supabase_admin()
    try:
        # Atualiza cada chave enviada
        for key, value in body.items():
            sb.table("ai_configs").upsert({
                "key": key,
                "value": str(value),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).execute()
        
        # Notifica o microserviço WhatsApp para recarregar as configs em memória
        import os
        node_url = os.environ.get('WHATSAPP_SERVICE_URL', 'http://localhost:3001')
        try:
            requests.post(f"{node_url}/ai/reload", json=body, timeout=5)
        except:
            pass # Se o node estiver off, ele carregará da DB ao iniciar
            
        return success("Configurações atualizadas com sucesso")
    except Exception as e:
        return error(f"Erro ao atualizar configs de IA: {str(e)}", 500)

@admin_bp.post("/ai/list-models")
@require_admin
def list_ai_models():
    """Solicita ao microserviço WhatsApp a listagem de modelos disponíveis para uma API Key."""
    body = request.get_json() or {}
    api_key = body.get("api_key")
    provider = body.get("provider", "gemini")
    
    if not api_key:
        return error("API Key é obrigatória para listar modelos")

    import os
    node_url = os.environ.get('WHATSAPP_SERVICE_URL', 'http://localhost:3001')
    try:
        resp = requests.post(f"{node_url}/ai/list-models", json={"api_key": api_key, "provider": provider}, timeout=10)
        if resp.status_code == 200:
            return success(resp.json())
        return error(f"Erro ao listar modelos: {resp.text}", 500)
    except Exception as e:
        return error(f"Falha ao conectar no microserviço de IA: {str(e)}", 500)
