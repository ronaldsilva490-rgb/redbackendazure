from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error

clients_bp = Blueprint("clients", __name__)


# ── CLIENTES ─────────────────────────────────────────────

@clients_bp.get("/")
@require_auth
def list_clients():
    tid    = request.tenant_id
    search = request.args.get("search", "").strip()
    query  = get_supabase_admin().table("clients").select("*") \
        .eq("tenant_id", tid).order("nome")
    if search:
        query = query.or_(
            f"nome.ilike.%{search}%,"
            f"cpf_cnpj.ilike.%{search}%,"
            f"telefone.ilike.%{search}%"
        )
    return success(query.execute().data)


@clients_bp.get("/<client_id>")
@require_auth
def get_client(client_id):
    resp = get_supabase_admin().table("clients").select("*") \
        .eq("id", client_id).eq("tenant_id", request.tenant_id) \
        .maybe_single().execute()
    if not resp.data:
        return error("Cliente não encontrado", 404)
    return success(resp.data)


@clients_bp.post("/")
@require_auth
def create_client():
    body = request.get_json() or {}
    if not body.get("nome", "").strip():
        return error("Nome do cliente é obrigatório")
    body["tenant_id"] = request.tenant_id
    resp = get_supabase_admin().table("clients").insert(body).execute()
    return success(resp.data[0], "Cliente cadastrado", 201)


@clients_bp.put("/<client_id>")
@require_auth
def update_client(client_id):
    body = request.get_json() or {}
    body.pop("id", None)
    body.pop("tenant_id", None)
    resp = get_supabase_admin().table("clients") \
        .update(body).eq("id", client_id).eq("tenant_id", request.tenant_id).execute()
    if not resp.data:
        return error("Cliente não encontrado", 404)
    return success(resp.data[0], "Cliente atualizado")


@clients_bp.delete("/<client_id>")
@require_auth
@require_papel("dono", "gerente")
def delete_client(client_id):
    get_supabase_admin().table("clients") \
        .delete().eq("id", client_id).eq("tenant_id", request.tenant_id).execute()
    return success(message="Cliente removido")


# ── LEADS / CRM ──────────────────────────────────────────

@clients_bp.get("/leads")
@require_auth
def list_leads():
    tid    = request.tenant_id
    status = request.args.get("status")
    query  = get_supabase_admin().table("leads") \
        .select("*, clients(nome, telefone), vehicles(marca, modelo, ano_mod)") \
        .eq("tenant_id", tid).order("created_at", desc=True)
    if status:
        query = query.eq("status", status)
    return success(query.execute().data)


@clients_bp.post("/leads")
@require_auth
def create_lead():
    body = request.get_json() or {}
    body["tenant_id"] = request.tenant_id
    body.setdefault("status", "novo")
    # Limpa campos opcionais
    for f in ["vehicle_id", "client_id", "obs"]:
        if body.get(f) == "":
            body[f] = None
    if body.get("valor_oferta") == "" or body.get("valor_oferta") is None:
        body["valor_oferta"] = None
    elif body.get("valor_oferta"):
        try:
            body["valor_oferta"] = float(body["valor_oferta"])
        except (ValueError, TypeError):
            body["valor_oferta"] = None
    resp = get_supabase_admin().table("leads").insert(body).execute()
    return success(resp.data[0], "Lead criado", 201)


@clients_bp.put("/leads/<lead_id>")
@require_auth
def update_lead(lead_id):
    body = request.get_json() or {}
    body.pop("id", None)
    body.pop("tenant_id", None)
    resp = get_supabase_admin().table("leads") \
        .update(body).eq("id", lead_id).eq("tenant_id", request.tenant_id).execute()
    return success(resp.data[0], "Lead atualizado")


@clients_bp.delete("/leads/<lead_id>")
@require_auth
def delete_lead(lead_id):
    get_supabase_admin().table("leads") \
        .delete().eq("id", lead_id).eq("tenant_id", request.tenant_id).execute()
    return success(message="Lead removido")
