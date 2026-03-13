from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth
from ..utils.response import success, error
from datetime import datetime, timezone

leads_bp = Blueprint("leads", __name__)

@leads_bp.get("/")
@require_auth
def list_leads():
    tid = request.tenant_id
    status = request.args.get("status")
    
    query = get_supabase_admin().table("leads").select("*, vehicles(marca, modelo, placa)").eq("tenant_id", tid).order("created_at", desc=True)
    
    if status:
        query = query.eq("status", status)
        
    return success(query.execute().data)

@leads_bp.post("/")
@require_auth
def create_lead():
    body = request.get_json() or {}
    body["tenant_id"] = request.tenant_id
    body["created_at"] = datetime.now(timezone.utc).isoformat()
    
    if not body.get("nome"):
        return error("Nome do lead é obrigatório")
        
    try:
        resp = get_supabase_admin().table("leads").insert(body).execute()
        return success(resp.data[0], "Lead registrado", 201)
    except Exception as e:
        return error(f"Erro ao registrar lead: {str(e)}")

@leads_bp.put("/<lead_id>")
@require_auth
def update_lead(lead_id):
    body = request.get_json() or {}
    body.pop("id", None)
    body.pop("tenant_id", None)
    body["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    try:
        resp = get_supabase_admin().table("leads")\
            .update(body).eq("id", lead_id).eq("tenant_id", request.tenant_id).execute()
        if not resp.data:
            return error("Lead não encontrado", 404)
        return success(resp.data[0], "Lead atualizado")
    except Exception as e:
        return error(f"Erro ao atualizar lead: {str(e)}")

@leads_bp.delete("/<lead_id>")
@require_auth
def delete_lead(lead_id):
    try:
        get_supabase_admin().table("leads")\
            .delete().eq("id", lead_id).eq("tenant_id", request.tenant_id).execute()
        return success(message="Lead removido")
    except Exception as e:
        return error(f"Erro ao remover lead: {str(e)}")

@leads_bp.get("/stats")
@require_auth
def lead_stats():
    tid = request.tenant_id
    rows = get_supabase_admin().table("leads").select("status").eq("tenant_id", tid).execute().data
    
    stats = {
        "total": len(rows),
        "novo": sum(1 for r in rows if r["status"] == "novo"),
        "em_atendimento": sum(1 for r in rows if r["status"] == "em_atendimento"),
        "fechado": sum(1 for r in rows if r["status"] == "fechado"),
        "perdido": sum(1 for r in rows if r["status"] == "perdido"),
    }
    return success(stats)
