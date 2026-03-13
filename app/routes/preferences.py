"""
app/routes/preferences.py
Endpoints para user preferences (localStorage → BD)
"""

from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from datetime import datetime
import uuid
from app.utils.supabase_client import get_supabase
from app.utils.auth_middleware import token_required

preferences_bp = Blueprint('preferences', __name__, url_prefix='/api/preferences')

@preferences_bp.route('/', methods=['GET'])
@cross_origin()
@token_required
def get_preferences(current_user):
    """GET /api/preferences - Obter preferências do usuário"""
    try:
        tenant_id = request.args.get('tenant_id')
        user_id = current_user.get('id')
        
        if not tenant_id:
            return jsonify({'erro': 'tenant_id obrigatório'}), 400
        
        response = get_supabase().table('user_preferences')\
            .select('*')\
            .eq('tenant_id', tenant_id)\
            .eq('user_id', user_id)\
            .execute()
        
        if not response.data:
            # Criar preferências padrão
            return create_default_preferences(tenant_id, user_id)
        
        return jsonify({
            'sucesso': True,
            'data': response.data[0]
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@preferences_bp.route('/', methods=['POST'])
@cross_origin()
@token_required
def save_preferences(current_user):
    """POST /api/preferences - Salvar preferências"""
    try:
        tenant_id = request.args.get('tenant_id')
        user_id = current_user.get('id')
        prefs = request.get_json()
        
        if not tenant_id:
            return jsonify({'erro': 'tenant_id obrigatório'}), 400
        
        # Verificar se existe
        exists = get_supabase().table('user_preferences')\
            .select('id')\
            .eq('tenant_id', tenant_id)\
            .eq('user_id', user_id)\
            .execute()
        
        prefs['atualizado_em'] = datetime.now().isoformat()
        
        if exists.data:
            # Update
            response = get_supabase().table('user_preferences')\
                .update(prefs)\
                .eq('tenant_id', tenant_id)\
                .eq('user_id', user_id)\
                .execute()
        else:
            # Insert
            prefs['tenant_id'] = tenant_id
            prefs['user_id'] = user_id
            prefs['id'] = str(uuid.uuid4())
            prefs['criado_em'] = datetime.now().isoformat()
            response = get_supabase().table('user_preferences').insert(prefs).execute()
        
        return jsonify({
            'sucesso': True,
            'mensagem': 'Preferências salvas',
            'data': response.data[0]
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@preferences_bp.route('/theme', methods=['POST'])
@cross_origin()
@token_required
def set_theme(current_user):
    """POST /api/preferences/theme - Mudar tema"""
    try:
        tenant_id = request.args.get('tenant_id')
        theme = request.json.get('theme', 'dark')
        
        if not tenant_id:
            return jsonify({'erro': 'tenant_id obrigatório'}), 400
        
        response = get_supabase().table('user_preferences')\
            .update({'theme': theme})\
            .eq('tenant_id', tenant_id)\
            .eq('user_id', current_user.get('id'))\
            .execute()
        
        return jsonify({'sucesso': True, 'theme': theme}), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@preferences_bp.route('/favorites/products', methods=['POST'])
@cross_origin()
@token_required
def add_favorite_product(current_user):
    """POST /api/preferences/favorites/products - Adicionar produto favorito"""
    try:
        tenant_id = request.args.get('tenant_id')
        product_id = request.json.get('product_id')
        
        response = get_supabase().table('user_preferences')\
            .select('favorite_products')\
            .eq('tenant_id', tenant_id)\
            .eq('user_id', current_user.get('id'))\
            .execute()
        
        favorites = response.data[0]['favorite_products'] or []
        if product_id not in favorites:
            favorites.append(product_id)
        
        get_supabase().table('user_preferences')\
            .update({'favorite_products': favorites})\
            .eq('tenant_id', tenant_id)\
            .eq('user_id', current_user.get('id'))\
            .execute()
        
        return jsonify({'sucesso': True}), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


def create_default_preferences(tenant_id, user_id):
    """Criar preferências padrão"""
    try:
        default = {
            'id': str(uuid.uuid4()),
            'tenant_id': tenant_id,
            'user_id': user_id,
            'theme': 'dark',
            'notify_updates': True,
            'notify_sales': True,
            'notify_stock': True,
            'criado_em': datetime.now().isoformat(),
            'atualizado_em': datetime.now().isoformat()
        }
        
        response = get_supabase().table('user_preferences').insert(default).execute()
        
        return jsonify({
            'sucesso': True,
            'data': response.data[0]
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500
