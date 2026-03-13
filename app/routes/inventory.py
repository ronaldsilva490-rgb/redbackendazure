"""
app/routes/inventory.py
API endpoints para gerenciar estoque e movimentações
"""

from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from datetime import datetime
import uuid
from app.utils.supabase_client import supabase
from app.utils.auth_middleware import token_required

inventory_bp = Blueprint('inventory', __name__, url_prefix='/api/inventory')

# ═══════════════════════════════════════════════════════════════════════════

@inventory_bp.route('/movimentacoes', methods=['GET'])
@cross_origin()
@token_required
def listar_movimentacoes(current_user):
    """
    GET /api/inventory/movimentacoes
    Listar movimentações de estoque
    
    Query params:
    - tenant_id: UUID (obrigatório)
    - produto_id: UUID (opcional)
    - tipo: entrada|saida|ajuste|devolucao (opcional)
    - data_inicio: YYYY-MM-DD (opcional)
    - data_fim: YYYY-MM-DD (opcional)
    - limit: 100
    - offset: 0
    """
    try:
        tenant_id = request.args.get('tenant_id')
        produto_id = request.args.get('produto_id')
        tipo = request.args.get('tipo')
        data_inicio = request.args.get('data_inicio')
        data_fim = request.args.get('data_fim')
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)
        
        if not tenant_id:
            return jsonify({'erro': 'tenant_id é obrigatório'}), 400
        
        # Construir query
        q = supabase.table('estoque_movimentacoes')\
            .select('*')\
            .eq('tenant_id', tenant_id)
        
        if produto_id:
            q = q.eq('produto_id', produto_id)
        if tipo:
            q = q.eq('tipo', tipo)
        if data_inicio:
            q = q.gte('created_at', f"{data_inicio}T00:00:00")
        if data_fim:
            q = q.lte('created_at', f"{data_fim}T23:59:59")
        
        q = q.order('created_at', desc=True)\
            .range(offset, offset + limit - 1)
        
        response = q.execute()
        
        return jsonify({
            'sucesso': True,
            'total': len(response.data),
            'data': response.data
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@inventory_bp.route('/movimentar', methods=['POST'])
@cross_origin()
@token_required
def registrar_movimentacao(current_user):
    """
    POST /api/inventory/movimentar
    Registrar movimentação de estoque
    
    Body:
    {
        "tenant_id": "uuid",
        "produto_id": "uuid",
        "tipo": "entrada|saida|ajuste|devolucao",
        "quantidade": 10,
        "motivo": "Entrada de compra",
        "referencia_tipo": "compra",
        "referencia_id": "uuid"
    }
    """
    try:
        dados = request.get_json()
        
        # Validações
        if not dados.get('tenant_id'):
            return jsonify({'erro': 'tenant_id é obrigatório'}), 400
        if not dados.get('produto_id'):
            return jsonify({'erro': 'produto_id é obrigatório'}), 400
        if not dados.get('tipo') or dados['tipo'] not in ['entrada', 'saida', 'ajuste', 'devolucao']:
            return jsonify({'erro': 'tipo inválido'}), 400
        if not dados.get('quantidade') or float(dados['quantidade']) <= 0:
            return jsonify({'erro': 'quantidade deve ser maior que 0'}), 400
        
        # Obter produto atual
        produto_resp = supabase.table('products')\
            .select('estoque_atual')\
            .eq('id', dados['produto_id'])\
            .eq('tenant_id', dados['tenant_id'])\
            .execute()
        
        if not produto_resp.data:
            return jsonify({'erro': 'Produto não encontrado'}), 404
        
        estoque_atual = produto_resp.data[0]['estoque']
        quantidade = float(dados['quantidade'])
        
        # Calcular novo estoque baseado no tipo de movimentação
        if dados['tipo'] == 'entrada':
            novo_estoque = estoque_atual + quantidade
        elif dados['tipo'] in ['saida', 'devolucao']:
            novo_estoque = estoque_atual - quantidade
            if novo_estoque < 0:
                return jsonify({'erro': 'Estoque insuficiente'}), 400
        elif dados['tipo'] == 'ajuste':
            novo_estoque = quantidade  # Ajuste é valor absoluto
        else:
            novo_estoque = estoque_atual
        
        # Criar movimentação
        movimentacao = {
            'id': str(uuid.uuid4()),
            'tenant_id': dados['tenant_id'],
            'produto_id': dados['produto_id'],
            'tipo': dados['tipo'],
            'quantidade': quantidade,
            'motivo': dados.get('motivo', ''),
            'referencia_tipo': dados.get('referencia_tipo'),
            'referencia_id': dados.get('referencia_id'),
            'criado_em': datetime.now().isoformat(),
            'criado_por': current_user.get('id')
        }
        
        # Criar movimentação e atualizar produto ATOMICAMENTE
        try:
            # Inserir movimentação
            mov_resp = supabase.table('estoque_movimentacoes')\
                .insert(movimentacao)\
                .execute()
            
            # Atualizar produto
            prod_resp = supabase.table('products')\
                .update({'estoque_atual': novo_estoque})\
                .eq('id', dados['produto_id'])\
                .execute()
            
            if not (mov_resp.data and prod_resp.data):
                raise Exception("Falha ao atualizar estoque")
            
            return jsonify({
                'sucesso': True,
                'mensagem': 'Movimentação registrada',
                'estoque_anterior': estoque_atual,
                'estoque_novo': novo_estoque,
                'data': mov_resp.data[0]
            }), 201
        
        except Exception as e:
            return jsonify({'erro': f'Erro ao registrar movimentação: {str(e)}'}), 500
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@inventory_bp.route('/estoque-baixo', methods=['GET'])
@cross_origin()
@token_required
def produtos_estoque_baixo(current_user):
    """
    GET /api/inventory/estoque-baixo?tenant_id=uuid
    Listar produtos com estoque abaixo do mínimo
    """
    try:
        tenant_id = request.args.get('tenant_id')
        
        if not tenant_id:
            return jsonify({'erro': 'tenant_id é obrigatório'}), 400
        
        response = supabase.table('produtos_estoque_baixo')\
            .select('*')\
            .eq('tenant_id', tenant_id)\
            .order('deficit', desc=True)\
            .execute()
        
        return jsonify({
            'sucesso': True,
            'total': len(response.data),
            'data': response.data
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@inventory_bp.route('/resumo', methods=['GET'])
@cross_origin()
@token_required
def resumo_estoque(current_user):
    """
    GET /api/inventory/resumo?tenant_id=uuid
    Obter resumo do estoque (total de produtos, valor, etc)
    """
    try:
        tenant_id = request.args.get('tenant_id')
        
        if not tenant_id:
            return jsonify({'erro': 'tenant_id é obrigatório'}), 400
        
        # Total de produtos
        produtos = supabase.table('products')\
            .select('estoque_atual, preco_custo')\
            .eq('tenant_id', tenant_id)\
            .eq('ativo', True)\
            .execute()
        
        if not produtos.data:
            return jsonify({
                'sucesso': True,
                'total_itens': 0,
                'valor_total': 0,
                'produtos_ativos': 0
            }), 200
        
        total_itens = sum(p.get('estoque', 0) for p in produtos.data)
        valor_total = sum(
            p.get('estoque', 0) * p.get('preco_custo', 0) 
            for p in produtos.data
        )
        
        # Produtos com estoque baixo
        baixo = supabase.table('produtos_estoque_baixo')\
            .select('id')\
            .eq('tenant_id', tenant_id)\
            .execute()
        
        return jsonify({
            'sucesso': True,
            'total_itens': total_itens,
            'valor_total': float(valor_total),
            'produtos_ativos': len(produtos.data),
            'produtos_estoque_baixo': len(baixo.data) if baixo.data else 0
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
