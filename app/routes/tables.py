from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error

tables_bp = Blueprint("tables", __name__)


@tables_bp.get("/")
@require_auth
def list_tables():
    tid    = request.tenant_id
    status = request.args.get("status")
    query  = get_supabase_admin().table("tables").select("*") \
        .eq("tenant_id", tid).order("numero")
    if status:
        query = query.eq("status", status)
    return success(query.execute().data)


@tables_bp.post("/")
@require_auth
@require_papel("dono", "gerente")
def create_table():
    body = request.get_json() or {}
    if not body.get("numero"):
        return error("Número da mesa é obrigatório")
    body["tenant_id"] = request.tenant_id
    body.setdefault("capacidade", 4)
    body.setdefault("status", "livre")

    try:
        resp = get_supabase_admin().table("tables").insert(body).execute()
        return success(resp.data[0], "Mesa cadastrada", 201)
    except Exception as e:
        return error(f"Mesa já existe ou erro: {str(e)}", 400)


@tables_bp.put("/<table_id>")
@require_auth
@require_papel("dono", "gerente")
def update_table(table_id):
    body = request.get_json() or {}
    body.pop("id", None)
    body.pop("tenant_id", None)
    resp = get_supabase_admin().table("tables") \
        .update(body).eq("id", table_id).eq("tenant_id", request.tenant_id).execute()
    if not resp.data:
        return error("Mesa não encontrada", 404)
    return success(resp.data[0], "Mesa atualizada")


@tables_bp.patch("/<table_id>/status")
@require_auth
def update_table_status(table_id):
    body   = request.get_json() or {}
    status = body.get("status")
    valid  = ["livre", "ocupada", "reservada"]
    if status not in valid:
        return error(f"Status inválido. Use: {', '.join(valid)}")
    resp = get_supabase_admin().table("tables") \
        .update({"status": status}).eq("id", table_id).eq("tenant_id", request.tenant_id).execute()
    if not resp.data:
        return error("Mesa não encontrada", 404)
    return success(resp.data[0], f"Mesa: {status}")


@tables_bp.delete("/<table_id>")
@require_auth
@require_papel("dono", "gerente")
def delete_table(table_id):
    get_supabase_admin().table("tables") \
        .delete().eq("id", table_id).eq("tenant_id", request.tenant_id).execute()
    return success(message="Mesa removida")
