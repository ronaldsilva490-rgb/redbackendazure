from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error

vehicles_bp = Blueprint("vehicles", __name__)


def _clean(body):
    # Campos numéricos float (preços)
    for f in ["preco", "preco_custo"]:
        val = body.get(f)
        if val == "" or val is None:
            body[f] = None if f == "preco_custo" else 0
        else:
            try:
                body[f] = float(val)
            except (ValueError, TypeError):
                body[f] = None
    # km e portas devem ser INT — converter via float intermediário para aceitar "30000.0"
    for f in ["km", "portas"]:
        val = body.get(f)
        if val == "" or val is None:
            body[f] = 0 if f == "km" else None
        else:
            try:
                body[f] = int(float(val))
            except (ValueError, TypeError):
                body[f] = 0 if f == "km" else None
    # Ano inteiro
    for f in ["ano", "ano_mod"]:
        val = body.get(f)
        if val == "" or val is None:
            body[f] = None
        else:
            try:
                body[f] = int(float(val))
            except (ValueError, TypeError):
                body[f] = None
    # Mapeia 'ano' → 'ano_fab' para compatibilidade com o schema do banco
    if "ano" in body:
        body["ano_fab"] = body.pop("ano")
    for f in ["descricao", "cor", "placa", "mecanico", "obs", "versao", "chassi", "renavam", "combustivel", "cambio"]:
        if body.get(f) == "":
            body[f] = None
    return body


@vehicles_bp.get("/")
@require_auth
def list_vehicles():
    tid    = request.tenant_id
    search = request.args.get("search", "").strip()
    tipo   = request.args.get("tipo")
    status = request.args.get("status")

    query = get_supabase_admin().table("vehicles").select("*") \
        .eq("tenant_id", tid).order("created_at", desc=True)

    if tipo:
        query = query.eq("tipo", tipo)
    if status:
        query = query.eq("status", status)
    if search:
        query = query.or_(f"marca.ilike.%{search}%,modelo.ilike.%{search}%,placa.ilike.%{search}%")

    return success(query.execute().data)


@vehicles_bp.get("/stats")
@require_auth
def stats():
    rows = get_supabase_admin().table("vehicles").select("status, preco") \
        .eq("tenant_id", request.tenant_id).execute().data
    return success({
        "total":       len(rows),
        "disponiveis": sum(1 for r in rows if r["status"] == "disponivel"),
        "reservados":  sum(1 for r in rows if r["status"] == "reservado"),
        "vendidos":    sum(1 for r in rows if r["status"] == "vendido"),
        "valor_total": round(sum(float(r["preco"] or 0) for r in rows if r["status"] == "disponivel"), 2),
    })


@vehicles_bp.get("/<vehicle_id>")
@require_auth
def get_vehicle(vehicle_id):
    resp = get_supabase_admin().table("vehicles").select("*") \
        .eq("id", vehicle_id).eq("tenant_id", request.tenant_id).maybe_single().execute()
    if not resp.data:
        return error("Veículo não encontrado", 404)
    return success(resp.data)


@vehicles_bp.post("/")
@require_auth
def create_vehicle():
    body = request.get_json() or {}
    body = _clean(body)

    if not body.get("marca") or not body.get("modelo"):
        return error("Marca e modelo são obrigatórios")
    if not body.get("preco"):
        return error("Preço é obrigatório")

    body["tenant_id"] = request.tenant_id
    body.setdefault("tipo", "carro")
    body.setdefault("status", "disponivel")
    body.setdefault("combustivel", "flex")
    body.setdefault("cambio", "manual")
    body.setdefault("km", 0)
    for f in ["id", "created_at", "updated_at"]:
        body.pop(f, None)

    try:
        resp = get_supabase_admin().table("vehicles").insert(body).execute()
        return success(resp.data[0], "Veículo cadastrado", 201)
    except Exception as e:
        return error(f"Erro: {str(e)}", 500)


@vehicles_bp.put("/<vehicle_id>")
@require_auth
def update_vehicle(vehicle_id):
    body = request.get_json() or {}
    body = _clean(body)
    for f in ["id", "tenant_id", "created_at", "updated_at"]:
        body.pop(f, None)
    try:
        resp = get_supabase_admin().table("vehicles") \
            .update(body).eq("id", vehicle_id).eq("tenant_id", request.tenant_id).execute()
        if not resp.data:
            return error("Veículo não encontrado", 404)
        return success(resp.data[0], "Veículo atualizado")
    except Exception as e:
        return error(f"Erro: {str(e)}", 500)


@vehicles_bp.delete("/<vehicle_id>")
@require_auth
@require_papel("dono", "gerente", "vendedor")
def delete_vehicle(vehicle_id):
    get_supabase_admin().table("vehicles") \
        .delete().eq("id", vehicle_id).eq("tenant_id", request.tenant_id).execute()
    return success(message="Veículo removido")


@vehicles_bp.patch("/<vehicle_id>/status")
@require_auth
def update_status(vehicle_id):
    body   = request.get_json() or {}
    status = body.get("status")
    valid  = ["disponivel", "reservado", "vendido"]
    if status not in valid:
        return error(f"Status inválido. Use: {', '.join(valid)}")
    resp = get_supabase_admin().table("vehicles") \
        .update({"status": status}).eq("id", vehicle_id).eq("tenant_id", request.tenant_id).execute()
    if not resp.data:
        return error("Veículo não encontrado", 404)
    return success(resp.data[0], f"Status: {status}")
@vehicles_bp.get("/<vehicle_id>/costs")
@require_auth
def get_vehicle_costs(vehicle_id):
    resp = get_supabase_admin().table("vehicle_costs")\
        .select("*").eq("vehicle_id", vehicle_id).eq("tenant_id", request.tenant_id).execute()
    return success(resp.data)

@vehicles_bp.post("/<vehicle_id>/costs")
@require_auth
def add_vehicle_cost(vehicle_id):
    body = request.get_json() or {}
    body["vehicle_id"] = vehicle_id
    body["tenant_id"] = request.tenant_id
    
    if not body.get("descricao") or not body.get("valor"):
        return error("Descrição e valor são obrigatórios")
        
    try:
        resp = get_supabase_admin().table("vehicle_costs").insert(body).execute()
        return success(resp.data[0], "Custo registrado", 201)
    except Exception as e:
        return error(f"Erro: {str(e)}", 500)
