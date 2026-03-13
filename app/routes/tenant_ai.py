from flask import Blueprint, request, jsonify
import requests
import os
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error

tenant_ai_bp = Blueprint("tenant_ai", __name__)

# URL do Microserviço WhatsApp (Node.js)
WHATSAPP_SERVICE_URL = os.getenv("WHATSAPP_SERVICE_URL", "http://localhost:3001")

@tenant_ai_bp.get("/config")
@require_auth
@require_papel("dono", "gerente")
def get_tenant_ai_config():
    """Busca as configurações de IA do tenant logado."""
    sb = get_supabase_admin()
    resp = sb.table("whatsapp_tenant_configs").select("*").eq("tenant_id", request.tenant_id).maybe_single().execute()
    
    if not resp or not resp.data:
        # Retorna valores padrão se não existir
        return success({
            "ai_enabled": False,
            "ai_provider": "gemini",
            "api_key": "",
            "model": "",
            "system_prompt": "Você é um assistente virtual prestativo e descontraído.",
            "ai_prefix": ""
        })
    
    return success(resp.data)

@tenant_ai_bp.post("/config")
@require_auth
@require_papel("dono", "gerente")
def save_tenant_ai_config():
    """Salva ou atualiza as configurações de IA do tenant."""
    body = request.get_json() or {}
    tenant_id = request.tenant_id
    
    # Campos permitidos
    payload = {
        "tenant_id": tenant_id,
        "ai_enabled": body.get("ai_enabled", False),
        "ai_provider": body.get("ai_provider", "gemini"),
        "api_key": body.get("api_key"),
        "model": body.get("model"),
        "system_prompt": body.get("system_prompt"),
        "ai_prefix": body.get("ai_prefix"),
        "updated_at": "now()"
    }
    
    sb = get_supabase_admin()
    resp = sb.table("whatsapp_tenant_configs").upsert(payload).execute()
    
    if not resp or not resp.data:
        return error("Erro ao salvar configurações de IA")
    
    # Notifica o microserviço para recarregar as configs
    try:
        requests.post(f"{WHATSAPP_SERVICE_URL}/ai/reload/{tenant_id}")
    except:
        pass
        
    return success(resp.data[0], "Configurações salvas com sucesso")

@tenant_ai_bp.get("/status")
@require_auth
@require_papel("dono", "gerente")
def get_whatsapp_status():
    """Proxy para buscar o status do WhatsApp no microserviço."""
    try:
        r = requests.get(f"{WHATSAPP_SERVICE_URL}/status/{request.tenant_id}", timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return error(f"Microserviço Offline: {str(e)}", 503)

@tenant_ai_bp.post("/connect")
@require_auth
@require_papel("dono", "gerente")
def connect_whatsapp():
    """Inicia o processo de conexão no microserviço."""
    try:
        r = requests.post(f"{WHATSAPP_SERVICE_URL}/start/{request.tenant_id}", timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return error(f"Erro ao iniciar conexão: {str(e)}", 503)

@tenant_ai_bp.post("/disconnect")
@require_auth
@require_papel("dono", "gerente")
def disconnect_whatsapp():
    """Encerra a conexão no microserviço."""
    try:
        r = requests.post(f"{WHATSAPP_SERVICE_URL}/stop/{request.tenant_id}", timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return error(f"Erro ao desconectar: {str(e)}", 503)
@tenant_ai_bp.post("/ai/list-models")
@require_auth
@require_papel("dono", "gerente")
def list_tenant_ai_models():
    """Proxy para buscar modelos disponíveis para o tenant."""
    body = request.get_json() or {}
    api_key = body.get("api_key")
    provider = body.get("provider", "gemini")
    
    if not api_key:
        return error("API Key é obrigatória")

    try:
        r = requests.post(f"{WHATSAPP_SERVICE_URL}/ai/list-models", json={"api_key": api_key, "provider": provider}, timeout=15)
        if r.status_code == 200:
            return success(r.json())
        return error(f"Erro ao listar modelos: {r.text}", r.status_code)
    except Exception as e:
        return error(f"Erro ao listar modelos: {str(e)}", 503)
