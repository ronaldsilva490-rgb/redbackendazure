from functools import wraps
from flask import request, jsonify
from .supabase_client import get_supabase_admin
import inspect


def _inject_and_call(f, current_user, *args, **kwargs):
    """Injeta current_user como primeiro arg SOMENTE se a função o declara explicitamente."""
    params = inspect.signature(f).parameters
    if params:
        first_param_name = next(iter(params))
        # Só injeta se o primeiro parâmetro se chama 'current_user'
        if first_param_name == 'current_user':
            return f(current_user, *args, **kwargs)
    return f(*args, **kwargs)


def require_auth_token_only(f):
    """
    Valida JWT via Supabase e injeta request.user_id / request.user.
    NÃO exige que o usuário já tenha um tenant vinculado.
    Use em rotas que criam o primeiro tenant (register-tenant).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip()

        if not token:
            return jsonify({"error": "Token não fornecido"}), 401

        try:
            sb = get_supabase_admin()
            user_resp = sb.auth.get_user(token)
            if not user_resp or not user_resp.user:
                return jsonify({"error": "Token inválido"}), 401

            user_id = user_resp.user.id
            current_user = {"id": user_id, "email": user_resp.user.email}
            request.user     = {"sub": user_id, "email": user_resp.user.email}
            request.user_id  = user_id
            request.tenant_id = None
            request.papel     = None

        except Exception:
            return jsonify({"error": "Token inválido ou expirado"}), 401

        return _inject_and_call(f, current_user, *args, **kwargs)
    return decorated


def require_auth(f):
    """Valida JWT via Supabase e injeta request.user + request.tenant_id.
    Exige que o usuário já tenha um tenant ativo vinculado."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip()

        if not token:
            return jsonify({"error": "Token não fornecido"}), 401

        try:
            sb = get_supabase_admin()
            user_resp = sb.auth.get_user(token)
            if not user_resp or not user_resp.user:
                return jsonify({"error": "Token inválido"}), 401

            user_id = user_resp.user.id

            try:
                tenant_resp = sb.table("tenant_users") \
                    .select("id, tenant_id, papel") \
                    .eq("user_id", user_id) \
                    .eq("ativo", True) \
                    .limit(1) \
                    .execute()
            except Exception:
                # Fallback: coluna 'ativo' pode não existir ainda (migration pendente)
                tenant_resp = sb.table("tenant_users") \
                    .select("id, tenant_id, papel") \
                    .eq("user_id", user_id) \
                    .limit(1) \
                    .execute()

            if not tenant_resp.data:
                return jsonify({"error": "Usuário sem tenant vinculado"}), 403

            tenant_id = tenant_resp.data[0]["tenant_id"]
            papel     = tenant_resp.data[0]["papel"]
            tu_id     = tenant_resp.data[0].get("id")

            current_user = {"id": user_id, "email": user_resp.user.email}
            request.user      = {"sub": user_id, "email": user_resp.user.email}
            request.user_id   = user_id
            request.tenant_id = tenant_id
            request.papel     = papel
            request.tenant_user_id = tu_id

        except Exception:
            return jsonify({"error": "Token inválido ou expirado"}), 401

        return _inject_and_call(f, current_user, *args, **kwargs)
    return decorated


def require_papel(*papeis):
    """Restringe rota a papéis específicos. Usar após @require_auth."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not hasattr(request, "papel") or request.papel not in papeis:
                return jsonify({"error": "Sem permissão para esta ação"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


# Alias para compatibilidade com rotas que usam @token_required
token_required = require_auth
