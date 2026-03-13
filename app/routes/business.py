"""
BUSINESS TYPES ROUTES
Gerenciamento de tipos de negócio
"""

from flask import Blueprint, request
from ..config.business_types import (
    BUSINESS_TYPES,
    get_business_type,
    get_all_business_types,
    get_business_modules,
    validate_business_type
)
from ..utils.supabase_client import get_supabase_admin
from ..utils.response import success, error
from ..utils.auth_middleware import require_auth

business_bp = Blueprint("business", __name__)

@business_bp.get("/tipos")
def listar_tipos():
    """GET /api/business/tipos - Lista todos os tipos de negócio disponíveis"""
    tipos = []
    for chave, config in BUSINESS_TYPES.items():
        tipos.append({
            "id": chave,
            "nome": config['nome'],
            "icone": config['icone'],
            "descricao": config['descricao'],
            "modulos_count": len(config['modulos']),
            "funcionalidades_count": len(config['funcionalidades'])
        })
    return success(tipos)

@business_bp.get("/tipos/<tipo_id>")
def detalhe_tipo(tipo_id):
    """GET /api/business/tipos/{tipo_id} - Detalhes de um tipo de negócio"""
    config = get_business_type(tipo_id)
    if not config:
        return error(f"Tipo de negócio '{tipo_id}' não encontrado", 404)
    return success(config)

@business_bp.get("/tipos/<tipo_id>/modulos")
def listar_modulos(tipo_id):
    """GET /api/business/tipos/{tipo_id}/modulos - Módulos disponíveis"""
    modulos = get_business_modules(tipo_id)
    if not modulos:
        return error(f"Tipo de negócio '{tipo_id}' não encontrado", 404)
    return success({"modulos": modulos})

@business_bp.get("/tipos/<tipo_id>/funcionalidades")
def listar_funcionalidades(tipo_id):
    """GET /api/business/tipos/{tipo_id}/funcionalidades - Funcionalidades"""
    config = get_business_type(tipo_id)
    if not config:
        return error(f"Tipo de negócio '{tipo_id}' não encontrado", 404)
    return success({"funcionalidades": config.get('funcionalidades', [])})

@business_bp.post("/validar-tipo")
def validar_tipo():
    """POST /api/business/validar-tipo - Valida um tipo de negócio"""
    body = request.get_json() or {}
    tipo = body.get("tipo", "").strip()
    
    if not tipo:
        return error("Campo 'tipo' obrigatório")
    
    valido = validate_business_type(tipo)
    return success({
        "tipo": tipo,
        "valido": valido,
        "config": get_business_type(tipo) if valido else None
    })
