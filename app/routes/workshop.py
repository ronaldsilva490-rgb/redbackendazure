from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error

workshop_bp = Blueprint("workshop", __name__)


@workshop_bp.get("/os")
@require_auth
def list_os():
    tid    = request.tenant_id
    status = request.args.get("status")
    query  = get_supabase_admin().table("os") \
        .select("*, clients(nome, telefone), vehicles(marca, modelo, placa, ano_mod)") \
        .eq("tenant_id", tid).order("created_at", desc=True)
    if status:
        query = query.eq("status", status)
    return success(query.execute().data)


@workshop_bp.get("/os/<os_id>")
@require_auth
def get_os(os_id):
    resp = get_supabase_admin().table("os") \
        .select("*, clients(nome, telefone, cpf_cnpj), vehicles(marca, modelo, placa, ano_mod, km)") \
        .eq("id", os_id).eq("tenant_id", request.tenant_id) \
        .maybe_single().execute()
    if not resp.data:
        return error("OS não encontrada", 404)
    return success(resp.data)


@workshop_bp.post("/os")
@require_auth
def create_os():
    body = request.get_json() or {}
    if not body.get("descricao"):
        return error("Descrição é obrigatória")
    body["tenant_id"] = request.tenant_id
    body.setdefault("status", "aberta")
    body.setdefault("pecas", [])
    body.setdefault("mao_obra", 0)
    body.setdefault("total", 0)
    resp = get_supabase_admin().table("os").insert(body).execute()
    return success(resp.data[0], "Ordem de Serviço criada", 201)


@workshop_bp.put("/os/<os_id>")
@require_auth
def update_os(os_id):
    body = request.get_json() or {}
    body.pop("id", None)
    body.pop("tenant_id", None)

    if "pecas" in body or "mao_obra" in body:
        pecas    = body.get("pecas", [])
        mao_obra = float(body.get("mao_obra", 0))
        total    = sum(float(p.get("valor", 0)) * int(p.get("qtd", 1)) for p in pecas)
        body["total"] = total + mao_obra

    resp = get_supabase_admin().table("os") \
        .update(body).eq("id", os_id).eq("tenant_id", request.tenant_id).execute()
    if not resp.data:
        return error("OS não encontrada", 404)
    return success(resp.data[0], "OS atualizada")


@workshop_bp.patch("/os/<os_id>/status")
@require_auth
def update_os_status(os_id):
    body   = request.get_json() or {}
    status = body.get("status")
    valid  = ["aberta", "em_andamento", "aguardando_peca", "concluida", "cancelada"]
    if status not in valid:
        return error(f"Status inválido. Use: {', '.join(valid)}")
    resp = get_supabase_admin().table("os") \
        .update({"status": status}).eq("id", os_id).eq("tenant_id", request.tenant_id).execute()
    return success(resp.data[0], f"OS: {status}")


@workshop_bp.delete("/os/<os_id>")
@require_auth
@require_papel("dono", "gerente")
def delete_os(os_id):
    get_supabase_admin().table("os") \
        .delete().eq("id", os_id).eq("tenant_id", request.tenant_id).execute()
    return success(message="OS removida")
