from flask import Blueprint, request, jsonify
import requests
import os
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error

tenant_ai_bp = Blueprint("tenant_ai", __name__)

WHATSAPP_SERVICE_URL = os.getenv("WHATSAPP_SERVICE_URL", "http://localhost:3001")


def _get_tenant_config_from_service(tenant_id: str) -> dict:
    """Busca config do tenant direto do microservico (JSON local)."""
    try:
        r = requests.get(f"{WHATSAPP_SERVICE_URL}/ai/config", timeout=5)
        if r.status_code == 200:
            all_configs = r.json()
            return all_configs.get(tenant_id) or all_configs.get("admin") or {}
    except Exception:
        pass
    return {}


@tenant_ai_bp.get("/config")
@require_auth
@require_papel("dono", "gerente")
def get_tenant_ai_config():
    """Busca as configuracoes de IA do tenant — lidas do JSON local."""
    cfg = _get_tenant_config_from_service(request.tenant_id)

    if not cfg:
        return success({
            "ai_enabled": False,
            "ai_provider": "groq",
            "api_key": "",
            "model": "",
            "system_prompt": "Voce e um assistente virtual prestativo e descontraido.",
            "ai_prefix": "",
            "red_instance_id": "",
            "red_proxy_url": "ws://automais.ddns.net:11434",
            "chat": {}, "stt": {}, "vision": {}, "tts": {}, "learning": {}, "proactive": {},
        })

    chat = cfg.get("chat") or {}
    return success({
        "ai_enabled":      cfg.get("ai_bot_enabled", True),
        "ai_provider":     chat.get("provider", "groq"),
        "api_key":         chat.get("api_key", ""),
        "model":           chat.get("model", ""),
        "system_prompt":   chat.get("system_prompt", ""),
        "ai_prefix":       cfg.get("ai_prefix", ""),
        "red_instance_id": cfg.get("red_instance_id", ""),
        "red_proxy_url":   cfg.get("red_proxy_url", ""),
        "chat":      cfg.get("chat", {}),
        "stt":       cfg.get("stt", {}),
        "vision":    cfg.get("vision", {}),
        "tts":       cfg.get("tts", {}),
        "learning":  cfg.get("learning", {}),
        "proactive": cfg.get("proactive", {}),
    })


@tenant_ai_bp.post("/config")
@require_auth
@require_papel("dono", "gerente")
def save_tenant_ai_config():
    """Salva config de IA do tenant direto no JSON local via microservico."""
    body = request.get_json() or {}
    tenant_id = request.tenant_id

    try:
        r = requests.put(
            f"{WHATSAPP_SERVICE_URL}/ai/config/{tenant_id}",
            json=body,
            timeout=10
        )
        if r.status_code != 200:
            return error(f"Erro ao salvar no microservico: {r.text}")
        return success(body, "Configuracoes salvas com sucesso")
    except requests.exceptions.ConnectionError:
        return error("Microservico WhatsApp offline. Suba o servico e tente novamente.", 503)
    except Exception as e:
        return error(f"Erro ao salvar configuracoes: {str(e)}")


@tenant_ai_bp.get("/status")
@require_auth
@require_papel("dono", "gerente")
def get_whatsapp_status():
    try:
        r = requests.get(f"{WHATSAPP_SERVICE_URL}/status/{request.tenant_id}", timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return error(f"Microservico Offline: {str(e)}", 503)


@tenant_ai_bp.post("/connect")
@require_auth
@require_papel("dono", "gerente")
def connect_whatsapp():
    try:
        r = requests.post(f"{WHATSAPP_SERVICE_URL}/start/{request.tenant_id}", timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return error(f"Erro ao iniciar conexao: {str(e)}", 503)


@tenant_ai_bp.post("/disconnect")
@require_auth
@require_papel("dono", "gerente")
def disconnect_whatsapp():
    try:
        r = requests.post(f"{WHATSAPP_SERVICE_URL}/stop/{request.tenant_id}", timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return error(f"Erro ao desconectar: {str(e)}", 503)


@tenant_ai_bp.post("/ai/list-models")
@require_auth
@require_papel("dono", "gerente")
def list_tenant_ai_models():
    body = request.get_json() or {}
    api_key = body.get("api_key")
    provider = body.get("provider", "gemini")

    if not api_key:
        return error("API Key e obrigatoria")

    try:
        r = requests.post(
            f"{WHATSAPP_SERVICE_URL}/ai/list-models",
            json={"api_key": api_key, "provider": provider},
            timeout=15
        )
        if r.status_code == 200:
            return success(r.json())
        return error(f"Erro ao listar modelos: {r.text}", r.status_code)
    except Exception as e:
        return error(f"Erro ao listar modelos: {str(e)}", 503)
