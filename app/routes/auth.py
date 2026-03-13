"""
auth.py — Autenticação RED SaaS
Suporte a login por USERNAME (sem @email) ou EMAIL.
Usernames são armazenados como username@red.internal no Supabase Auth.
"""
from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin, get_supabase
from ..utils.auth_middleware import require_auth, require_auth_token_only
from app.config.business_types import validate_business_type, get_all_business_types
from ..utils.response import success, error
import re

auth_bp = Blueprint("auth", __name__)

INTERNAL_DOMAIN = ".red.internal"


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text[:50]


def is_username(login: str) -> bool:
    """Retorna True se login é username (ex: antonio ou antonio@padaria)."""
    if "@" not in login:
        return True
    domain_part = login.split("@")[-1]
    if "." not in domain_part:
        return True
    return False


def to_auth_email(login: str) -> str:
    """Converte login para email usado no Supabase Auth."""
    if is_username(login):
        return login.lower().strip() + INTERNAL_DOMAIN
    return login.lower().strip()


def check_username_available(sb, username: str, tenant_id: str = None) -> bool:
    """Verifica se username está disponível (globalmente ou no tenant)."""
    auth_email = to_auth_email(username)
    # Tenta achar no auth — se não lança exceção, existe
    try:
        users = sb.auth.admin.list_users()
        for u in users:
            if u.email == auth_email:
                return False
        return True
    except Exception:
        return True


@auth_bp.post("/register")
def register():
    """Cria usuário + tenant + vínculo como dono. Rollback completo em caso de falha."""
    body     = request.get_json() or {}
    login    = body.get("email", "").strip()
    password = body.get("password", "")
    tenant   = body.get("tenant", {})

    if not login or not password:
        return error("Login e senha são obrigatórios")
    if not tenant.get("nome"):
        return error("Nome do negócio é obrigatório")
    tipo = (tenant.get("tipo") or "").strip()
    if not validate_business_type(tipo):
        allowed = ", ".join(get_all_business_types())
        return error(f"Tipo inválido. Use: {allowed}")

    auth_email = to_auth_email(login)
    sb = get_supabase_admin()

    # ── Verificação prévia: email já existe no Auth? ──────────────────────────
    # Evita o "usuário fantasma" — se já existe no Auth mas não tem tenant,
    # retorna erro claro em vez de tentar criar e falhar com 422.
    try:
        existing_users = sb.auth.admin.list_users()
        for u in existing_users:
            if u.email and u.email.lower() == auth_email.lower():
                # Verifica se tem tenant vinculado
                link = sb.table("tenant_users").select("id").eq("user_id", u.id).execute()
                if link.data:
                    return error("Este e-mail já está cadastrado", 409)
                else:
                    # Usuário fantasma: existe no Auth mas sem tenant → limpa e recria
                    try:
                        sb.auth.admin.delete_user(u.id)
                    except Exception:
                        pass
                    break
    except Exception:
        pass  # Se não conseguir listar, tenta criar e deixa o create_user tratar

    # ── Etapa 1: cria no Supabase Auth ───────────────────────────────────────
    user_id = None
    try:
        auth_resp = sb.auth.admin.create_user({
            "email":         auth_email,
            "password":      password,
            "email_confirm": True,
        })
        user_id = auth_resp.user.id
    except Exception as e:
        msg = str(e)
        if "already registered" in msg or "already exists" in msg:
            return error("Este e-mail já está cadastrado", 409)
        return error(f"Erro ao criar usuário: {msg}", 400)

    # ── Etapa 2: cria o tenant ────────────────────────────────────────────────
    tenant_id = None
    try:
        slug_base = slugify(tenant["nome"])
        slug = slug_base
        i = 1
        while True:
            existing = sb.table("tenants").select("id").eq("slug", slug).execute()
            if not existing.data:
                break
            slug = f"{slug_base}-{i}"
            i += 1

        tenant_resp = sb.table("tenants").insert({
            "nome":     tenant["nome"].strip(),
            "slug":     slug,
            "tipo":     tenant["tipo"],
            "cnpj":     tenant.get("cnpj") or None,
            "telefone": tenant.get("telefone") or None,
            "cidade":   tenant.get("cidade") or None,
            "estado":   tenant.get("estado") or None,
        }).execute()
        tenant_id = tenant_resp.data[0]["id"]
    except Exception as e:
        # Rollback: remove usuário do Auth
        try: sb.auth.admin.delete_user(user_id)
        except: pass
        return error(f"Erro ao criar negócio: {str(e)}", 500)

    # ── Etapa 3: vincula usuário ao tenant ────────────────────────────────────
    try:
        sb.table("tenant_users").insert({
            "tenant_id": tenant_id,
            "user_id":   user_id,
            "papel":     "dono",
        }).execute()
    except Exception as e:
        # Rollback completo: remove tenant E usuário
        try: sb.table("tenants").delete().eq("id", tenant_id).execute()
        except: pass
        try: sb.auth.admin.delete_user(user_id)
        except: pass
        return error(f"Erro ao vincular usuário: {str(e)}", 500)

    return success({"tenant_id": tenant_id, "slug": slug}, "Negócio criado com sucesso!", 201)


@auth_bp.post("/login")
def login():
    body     = request.get_json() or {}
    login    = body.get("email", "").strip()
    password = body.get("password", "")

    if not login or not password:
        return error("Login e senha são obrigatórios")

    auth_email = to_auth_email(login)

    try:
        resp = get_supabase().auth.sign_in_with_password({
            "email": auth_email, "password": password
        })

        import os
        SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "")

        # ── Superadmin: pula verificação de tenant ──────────
        if SUPERADMIN_EMAIL and resp.user.email == SUPERADMIN_EMAIL:
            return success({
                "access_token":  resp.session.access_token,
                "refresh_token": resp.session.refresh_token,
                "user": {
                    "id":       resp.user.id,
                    "email":    resp.user.email,
                    "username": "superadmin",
                },
                "tenant": None,
                "papel":  "superadmin",
            })

        # ── Login normal ────────────────────────────────────
        sb = get_supabase_admin()
        tenant_resp = sb.table("tenant_users") \
            .select("tenant_id, papel, tenants(*)") \
            .eq("user_id", resp.user.id) \
            .eq("ativo", True) \
            .limit(1) \
            .execute()

        tenant_data = tenant_resp.data[0] if tenant_resp.data else None
        if not tenant_data:
            return error("Usuário sem empresa vinculada. Contate o administrador.", 403)

        display_login = login

        return success({
            "access_token":  resp.session.access_token,
            "refresh_token": resp.session.refresh_token,
            "user": {
                "id":       resp.user.id,
                "email":    resp.user.email,
                "username": display_login,
            },
            "tenant": tenant_data["tenants"],
            "papel":  tenant_data["papel"],
        })
    except Exception as e:
        msg = str(e)
        if "Invalid login" in msg or "invalid_credentials" in msg:
            return error("Login ou senha incorretos", 401)
        return error("Erro ao fazer login", 401)

@auth_bp.post("/check-username")
def check_username():
    """Verifica se um username está disponível."""
    body     = request.get_json() or {}
    username = body.get("username", "").strip().lower()
    if not username or len(username) < 3:
        return error("Username deve ter ao menos 3 caracteres")
    if re.search(r'[^a-z0-9._-]', username):
        return error("Username só pode ter letras, números, ponto, traço e underline")

    sb = get_supabase_admin()
    available = check_username_available(sb, username)
    return success({"available": available, "username": username})


@auth_bp.post("/logout")
def logout():
    try:
        get_supabase().auth.sign_out()
    except: pass
    return success(message="Logout realizado")


@auth_bp.post("/refresh")
def refresh():
    body  = request.get_json() or {}
    token = body.get("refresh_token", "")
    if not token:
        return error("refresh_token obrigatório")
    try:
        resp = get_supabase().auth.refresh_session(token)
        return success({
            "access_token":  resp.session.access_token,
            "refresh_token": resp.session.refresh_token,
        })
    except Exception:
        return error("Refresh token inválido ou expirado", 401)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTENTICAÇÃO DE ADMINISTRADORES
# ═══════════════════════════════════════════════════════════════════════════════

from werkzeug.security import generate_password_hash, check_password_hash
import os
import jwt
from datetime import datetime, timedelta

ADMIN_MASTER_KEY = os.getenv("ADMIN_MASTER_KEY", "RED")  # Palavra-mestre padrão
JWT_SECRET = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
JWT_ALGORITHM = "HS256"
