from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error
import re

tenants_bp = Blueprint("tenants", __name__)
INTERNAL_DOMAIN = ".red.internal"

PAPEIS_VALIDOS = ("gerente", "vendedor", "caixa", "mecanico", "garcom", "cozinheiro", "entregador")


def to_auth_email(login: str) -> str:
    if "@" not in login:
        return login.lower().strip() + INTERNAL_DOMAIN
    return login.lower().strip()


def validate_username(u: str) -> str | None:
    """Retorna None se válido, mensagem de erro se inválido."""
    if len(u) < 3:
        return "Username deve ter ao menos 3 caracteres"
    if len(u) > 30:
        return "Username deve ter no máximo 30 caracteres"
    if re.search(r'[^a-z0-9._-]', u.lower()):
        return "Username só pode ter letras minúsculas, números, ponto, traço ou underline"
    return None


@tenants_bp.get("/me")
@require_auth
def get_my_tenant():
    resp = get_supabase_admin().table("tenants") \
        .select("*").eq("id", request.tenant_id).maybe_single().execute()
    if not resp.data:
        return error("Empresa não encontrada", 404)
    # Injeta papel do usuário na resposta para que o frontend possa usar
    tenant_data = dict(resp.data)
    tenant_data["papel"] = getattr(request, "papel", None)
    return success(tenant_data)


@tenants_bp.get("/my-units")
@require_auth
@require_papel("dono", "gerente")
def list_my_units():
    """Lista todos os estabelecimentos vinculados ao dono/gerente logado, para RBAC cruzado."""
    sb = get_supabase_admin()
    resp = sb.table("tenant_users") \
        .select("papel, tenants(id, nome, slug, tipo)") \
        .eq("user_id", request.user_id) \
        .execute()
    
    if not resp.data:
        return success([])
        
    units = []
    for item in resp.data:
        if item.get("tenants") and item.get("papel") in ("dono", "gerente"):
            units.append(item["tenants"])
            
    return success(units)


@tenants_bp.put("/me")
@require_auth
@require_papel("dono", "gerente")
def update_my_tenant():
    body = request.get_json() or {}

    # Campos permitidos na tabela tenants
    ALLOWED = {
        "nome", "descricao", "logo_url",
        "cnpj", "inscricao_estadual", "razao_social",
        "telefone", "email", "website",
        "endereco", "numero", "complemento", "bairro",
        "cidade", "estado", "cep",
        "moeda", "fuso_horario", "config",
        # PIX — só inclui se a coluna existir (adicionada via migration)
        "pix_chave", "pix_tipo", "pix_titular",
    }

    # Remove campos protegidos / inexistentes
    body = {k: v for k, v in body.items() if k in ALLOWED}

    # Converte strings vazias em NULL
    for k in list(body.keys()):
        if body[k] == "":
            body[k] = None

    if not body:
        return error("Nenhum campo válido para atualizar")

    sb = get_supabase_admin()

    # Tenta salvar com campos PIX; se der erro de schema, salva sem eles
    pix_fields = {"pix_chave", "pix_tipo", "pix_titular"}
    try:
        resp = sb.table("tenants").update(body).eq("id", request.tenant_id).execute()
    except Exception as e:
        if any(f in str(e) for f in pix_fields) and "schema" in str(e).lower():
            body_sem_pix = {k: v for k, v in body.items() if k not in pix_fields}
            resp = sb.table("tenants").update(body_sem_pix).eq("id", request.tenant_id).execute()
        else:
            return error(f"Erro ao atualizar: {str(e)}", 500)

    if not resp.data:
        return error("Empresa não encontrada", 404)
    return success(resp.data[0], "Dados atualizados")


@tenants_bp.get("/users")
@require_auth
@require_papel("dono", "gerente")
def list_users():
    sb   = get_supabase_admin()
    resp = sb.table("tenant_users") \
        .select("id, user_id, papel, ativo, created_at") \
        .eq("tenant_id", request.tenant_id) \
        .order("created_at") \
        .execute()

    if not resp.data:
        return success([])

    # Busca todos os usuários do Auth de uma vez (1 chamada em vez de N)
    try:
        all_auth_users = sb.auth.admin.list_users()
        auth_map = {u.id: u.email for u in all_auth_users if u and u.id}
    except Exception:
        auth_map = {}

    users = []
    for row in resp.data:
        email = auth_map.get(row["user_id"])
        if email and email.endswith(INTERNAL_DOMAIN):
            username = email.replace(INTERNAL_DOMAIN, "")
            row["email"] = None
            row["username"] = username
            row["display_login"] = username
        else:
            row["email"] = email or None
            row["username"] = None
            row["display_login"] = email or "—"
        users.append(row)
    return success(users)


@tenants_bp.post("/check-username")
@require_auth
@require_papel("dono", "gerente")
def check_username_tenant():
    """Verifica disponibilidade de username para novo funcionário."""
    body     = request.get_json() or {}
    username = body.get("username", "").strip().lower()
    
    base_username = username.split("@")[0] if username.endswith(INTERNAL_DOMAIN) else username
    err = validate_username(base_username)
    if err:
        return error(err)

    auth_email = username if "@" in username else to_auth_email(username)
    sb = get_supabase_admin()
    try:
        users = sb.auth.admin.list_users()
        for u in users:
            if u.email == auth_email:
                return success({"available": False, "username": username})
        return success({"available": True, "username": username})
    except Exception:
        return success({"available": True, "username": username})


@tenants_bp.patch("/users/<user_id>")
@require_auth
@require_papel("dono", "gerente")
def update_user(user_id):
    body  = request.get_json() or {}
    papel = body.get("papel")
    ativo = body.get("ativo")
    update = {}
    if papel is not None:
        if papel not in PAPEIS_VALIDOS:
            return error(f"Papel inválido. Use: {', '.join(PAPEIS_VALIDOS)}")
        update["papel"] = papel
    if ativo is not None:
        update["ativo"] = bool(ativo)
    if not update:
        return error("Nenhum campo para atualizar")

    sb   = get_supabase_admin()
    try:
        resp = sb.table("tenant_users").update(update) \
            .eq("user_id", user_id).eq("tenant_id", request.tenant_id).execute()
    except Exception as e:
        msg = str(e)
        # Coluna 'ativo' pode não existir ainda — tenta sem ela
        if "ativo" in msg and "ativo" in update:
            update.pop("ativo")
            if not update:
                return error("Campo 'ativo' não disponível no banco. Execute a migration SQL.", 503)
            resp = sb.table("tenant_users").update(update) \
                .eq("user_id", user_id).eq("tenant_id", request.tenant_id).execute()
        else:
            return error(f"Erro ao atualizar funcionário: {msg}", 500)
    if not resp.data:
        return error("Funcionário não encontrado", 404)
    return success(resp.data[0], "Funcionário atualizado")


@tenants_bp.delete("/users/<user_id>")
@require_auth
@require_papel("dono")
def remove_user(user_id):
    sb  = get_supabase_admin()
    alvo = sb.table("tenant_users").select("papel") \
        .eq("user_id", user_id).eq("tenant_id", request.tenant_id) \
        .maybe_single().execute()
    if alvo.data and alvo.data.get("papel") == "dono":
        return error("Não é possível remover o dono da empresa")
    sb.table("tenant_users") \
        .delete().eq("user_id", user_id).eq("tenant_id", request.tenant_id).execute()
    return success(message="Funcionário removido")


@tenants_bp.post("/users/invite")
@require_auth
@require_papel("dono", "gerente")
def invite_user():
    """Cadastra novo funcionário com username ou email."""
    body     = request.get_json() or {}
    login    = body.get("login", "").strip()   # username ou email
    password = body.get("password", "")
    papel    = body.get("papel", "vendedor")

    if not login or not password:
        return error("Login e senha são obrigatórios")
    if len(password) < 6:
        return error("Senha deve ter no mínimo 6 caracteres")
    if papel not in PAPEIS_VALIDOS:
        return error(f"Papel inválido. Use: {', '.join(PAPEIS_VALIDOS)}")

    is_internal = login.endswith(INTERNAL_DOMAIN)
    is_username = "@" not in login or is_internal
    
    if is_username:
        base_login = login.split("@")[0] if is_internal else login
        err = validate_username(base_login)
        if err:
            return error(err)

    auth_email = login if is_internal else to_auth_email(login)
    sb = get_supabase_admin()

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
            return error("Este login já está cadastrado no sistema", 409)
        return error(f"Erro ao criar login: {msg}", 400)

    existente = sb.table("tenant_users").select("id") \
        .eq("user_id", user_id).eq("tenant_id", request.tenant_id).execute()
    if existente.data:
        # Rollback: usuário criado no Auth mas já vinculado → remove do Auth
        try: sb.auth.admin.delete_user(user_id)
        except: pass
        return error("Este login já é funcionário desta empresa principal", 409)

    raw_units = body.get("units", [])
    units = list(raw_units) if isinstance(raw_units, list) and len(raw_units) > 0 else [request.tenant_id]
        
    # Garante que request.tenant_id sempre está no bolo se veio vazio
    if request.tenant_id not in units:
        units.append(request.tenant_id)

    try:
        rows = []
        for t_id in units:
            row = {
                "tenant_id": t_id,
                "user_id":   user_id,
                "papel":     papel,
                "ativo":     True,
            }
            if is_username:
                row["username"] = base_login
            rows.append(row)

        try:
            sb.table("tenant_users").insert(rows).execute()
        except Exception as e:
            if "username" in str(e) and "schema cache" in str(e):
                clean_rows = [{k: v for k, v in r.items() if k != "username"} for r in rows]
                sb.table("tenant_users").insert(clean_rows).execute()
            else:
                raise
    except Exception as e:
        try: sb.auth.admin.delete_user(user_id)
        except: pass
        return error(f"Erro ao vincular funcionário em múltiplas unidades: {str(e)}", 500)

    return success({
        "user_id": user_id,
        "login":   login,
        "papel":   papel,
    }, "Funcionário cadastrado com sucesso!", 201)
