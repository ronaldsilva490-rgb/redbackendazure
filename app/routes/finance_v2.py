"""
app/routes/finance_v2.py
API endpoints para gerenciar finanças e transações (v2)
"""

from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from datetime import datetime, timedelta
from dateutil.parser import parse as parse_date
import uuid
from app.utils.supabase_client import supabase
from app.utils.auth_middleware import token_required

finance_v2_bp = Blueprint('finance_v2', __name__, url_prefix='/api/finance/v2')

# ═══════════════════════════════════════════════════════════════════════════

@finance_v2_bp.route('/transacoes', methods=['GET'])
@cross_origin()
@token_required
def listar_transacoes(current_user):
    """
    GET /api/finance/transacoes
    Listar transações financeiras
    
    Query params:
    - tenant_id: UUID (obrigatório)
    - tipo: receita|despesa|transferencia (opcional)
    - pago: true|false (opcional)
    - data_inicio: YYYY-MM-DD (opcional)
    - data_fim: YYYY-MM-DD (opcional)
    - categoria: nome da categoria (opcional)
    - limit: 100
    - offset: 0
    """
    try:
        tenant_id = request.args.get('tenant_id')
        tipo = request.args.get('tipo')
        pago = request.args.get('pago')
        data_inicio = request.args.get('data_inicio')
        data_fim = request.args.get('data_fim')
        categoria = request.args.get('categoria')
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)
        
        if not tenant_id:
            return jsonify({'erro': 'tenant_id é obrigatório'}), 400
        
        # Construir query
        q = supabase.table('transactions')\
            .select('*')\
            .eq('tenant_id', tenant_id)
        
        if tipo:
            q = q.eq('tipo', tipo)
        
        if pago is not None:
            q = q.eq('pago', pago.lower() == 'true')
        
        if data_inicio:
            q = q.gte('data_criacao', f"{data_inicio}T00:00:00")
        
        if data_fim:
            q = q.lte('data_criacao', f"{data_fim}T23:59:59")
        
        if categoria:
            q = q.eq('categoria', categoria)
        
        q = q.order('data_criacao', desc=True)\
            .range(offset, offset + limit - 1)
        
        response = q.execute()
        
        return jsonify({
            'sucesso': True,
            'total': len(response.data),
            'data': response.data
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@finance_v2_bp.route('/transacao', methods=['POST'])
@cross_origin()
@token_required
def criar_transacao(current_user):
    """
    POST /api/finance/transacao
    Criar nova transação
    
    Body:
    {
        "tenant_id": "uuid",
        "tipo": "receita|despesa|transferencia",
        "categoria": "venda",
        "descricao": "Venda PDV #123",
        "valor": 150.50,
        "data_vencimento": "2024-01-15",
        "pago": false,
        "referencia_tipo": "venda",
        "referencia_id": "uuid"
    }
    """
    try:
        dados = request.get_json()
        
        # Validações
        if not dados.get('tenant_id'):
            return jsonify({'erro': 'tenant_id é obrigatório'}), 400
        if not dados.get('tipo') or dados['tipo'] not in ['receita', 'despesa', 'transferencia']:
            return jsonify({'erro': 'tipo inválido'}), 400
        if not dados.get('descricao'):
            return jsonify({'erro': 'descricao é obrigatória'}), 400
        if not dados.get('valor') or float(dados['valor']) <= 0:
            return jsonify({'erro': 'valor deve ser maior que 0'}), 400
        
        transacao = {
            'id': str(uuid.uuid4()),
            'tenant_id': dados['tenant_id'],
            'tipo': dados['tipo'],
            'categoria': dados.get('categoria', 'Geral'),
            'descricao': dados['descricao'],
            'valor': float(dados['valor']),
            'data_criacao': datetime.now().isoformat(),
            'data_vencimento': dados.get('data_vencimento'),
            'data_pagamento': None,
            'pago': dados.get('pago', False),
            'referencia_tipo': dados.get('referencia_tipo'),
            'referencia_id': dados.get('referencia_id'),
            'criado_por': current_user.get('id'),
            'observacoes': dados.get('observacoes', '')
        }
        
        response = supabase.table('transactions').insert(transacao).execute()
        
        return jsonify({
            'sucesso': True,
            'mensagem': 'Transação criada',
            'data': response.data[0]
        }), 201
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@finance_v2_bp.route('/transacao/<transacao_id>', methods=['PUT'])
@cross_origin()
@token_required
def atualizar_transacao(current_user, transacao_id):
    """
    PUT /api/finance/transacao/{transacao_id}
    Atualizar transação
    """
    try:
        dados = request.get_json()
        
        response = supabase.table('transactions')\
            .update(dados)\
            .eq('id', transacao_id)\
            .execute()
        
        if not response.data:
            return jsonify({'erro': 'Transação não encontrada'}), 404
        
        return jsonify({
            'sucesso': True,
            'mensagem': 'Transação atualizada',
            'data': response.data[0]
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@finance_v2_bp.route('/transacao/<transacao_id>/pagar', methods=['POST'])
@cross_origin()
@token_required
def marcar_pago(current_user, transacao_id):
    """
    POST /api/finance/transacao/{transacao_id}/pagar
    Marcar transação como paga
    """
    try:
        response = supabase.table('transactions')\
            .update({
                'pago': True,
                'data_pagamento': datetime.now().isoformat(),
                'pago_por': current_user.get('id')
            })\
            .eq('id', transacao_id)\
            .execute()
        
        if not response.data:
            return jsonify({'erro': 'Transação não encontrada'}), 404
        
        return jsonify({
            'sucesso': True,
            'mensagem': 'Transação marcada como paga',
            'data': response.data[0]
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@finance_v2_bp.route('/relatorio', methods=['GET'])
@cross_origin()
@token_required
def relatorio_financeiro(current_user):
    """
    GET /api/finance/relatorio
    Obter relatório financeiro
    
    Query params:
    - tenant_id: UUID (obrigatório)
    - data_inicio: YYYY-MM-DD (default: hoje)
    - data_fim: YYYY-MM-DD (default: hoje)
    - agrupar_por: dia|semana|mes|ano
    """
    try:
        tenant_id = request.args.get('tenant_id')
        data_inicio = request.args.get('data_inicio')
        data_fim = request.args.get('data_fim')
        agrupar = request.args.get('agrupar_por', 'dia')
        
        if not tenant_id:
            return jsonify({'erro': 'tenant_id é obrigatório'}), 400
        
        # Defaults: hoje
        hoje = datetime.now().strftime('%Y-%m-%d')
        data_inicio = data_inicio or hoje
        data_fim = data_fim or hoje
        
        # Receitas
        receitas_resp = supabase.table('transactions')\
            .select('valor')\
            .eq('tenant_id', tenant_id)\
            .eq('tipo', 'receita')\
            .gte('data_criacao', f"{data_inicio}T00:00:00")\
            .lte('data_criacao', f"{data_fim}T23:59:59")\
            .execute()
        
        total_receitas = sum(t['valor'] for t in receitas_resp.data) if receitas_resp.data else 0
        
        # Despesas
        despesas_resp = supabase.table('transactions')\
            .select('valor')\
            .eq('tenant_id', tenant_id)\
            .eq('tipo', 'despesa')\
            .gte('data_criacao', f"{data_inicio}T00:00:00")\
            .lte('data_criacao', f"{data_fim}T23:59:59")\
            .execute()
        
        total_despesas = sum(t['valor'] for t in despesas_resp.data) if despesas_resp.data else 0
        
        # Pendentes
        pendentes_resp = supabase.table('transactions')\
            .select('valor')\
            .eq('tenant_id', tenant_id)\
            .eq('pago', False)\
            .lte('data_vencimento', data_fim)\
            .execute()
        
        total_pendente = sum(t['valor'] for t in pendentes_resp.data) if pendentes_resp.data else 0
        
        # Receitas por categoria
        categorias_resp = supabase.table('transactions')\
            .select('categoria, valor')\
            .eq('tenant_id', tenant_id)\
            .eq('tipo', 'receita')\
            .gte('data_criacao', f"{data_inicio}T00:00:00")\
            .lte('data_criacao', f"{data_fim}T23:59:59")\
            .execute()
        
        categorias = {}
        if categorias_resp.data:
            for t in categorias_resp.data:
                cat = t.get('categoria', 'Sem categoria')
                categorias[cat] = categorias.get(cat, 0) + t['valor']
        
        return jsonify({
            'sucesso': True,
            'periodo': {
                'inicio': data_inicio,
                'fim': data_fim
            },
            'resumo': {
                'receitas': float(total_receitas),
                'despesas': float(total_despesas),
                'liquido': float(total_receitas - total_despesas),
                'pendente': float(total_pendente)
            },
            'por_categoria': categorias
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@finance_v2_bp.route('/Dashboard', methods=['GET'])
@cross_origin()
@token_required
def dashboard(current_user):
    """
    GET /api/finance/dashboard?tenant_id=uuid
    Dashboard financeiro com métricas rápidas
    """
    try:
        tenant_id = request.args.get('tenant_id')
        
        if not tenant_id:
            return jsonify({'erro': 'tenant_id é obrigatório'}), 400
        
        hoje = datetime.now()
        inicio_mes = hoje.replace(day=1)
        
        # Receita hoje
        receita_hoje_resp = supabase.table('transactions')\
            .select('valor')\
            .eq('tenant_id', tenant_id)\
            .eq('tipo', 'receita')\
            .gte('data_criacao', hoje.strftime('%Y-%m-%dT00:00:00'))\
            .lte('data_criacao', hoje.strftime('%Y-%m-%dT23:59:59'))\
            .execute()
        
        receita_hoje = sum(t['valor'] for t in receita_hoje_resp.data) if receita_hoje_resp.data else 0
        
        # Receita este mês
        receita_mes_resp = supabase.table('transactions')\
            .select('valor')\
            .eq('tenant_id', tenant_id)\
            .eq('tipo', 'receita')\
            .gte('data_criacao', inicio_mes.isoformat())\
            .execute()
        
        receita_mes = sum(t['valor'] for t in receita_mes_resp.data) if receita_mes_resp.data else 0
        
        # Contas a vencer (próximos 7 dias)
        data_vencer = (hoje + timedelta(days=7)).strftime('%Y-%m-%d')
        contas_vencer_resp = supabase.table('transactions')\
            .select('valor')\
            .eq('tenant_id', tenant_id)\
            .eq('pago', False)\
            .lte('data_vencimento', data_vencer)\
            .execute()
        
        contas_vencer = sum(t['valor'] for t in contas_vencer_resp.data) if contas_vencer_resp.data else 0
        
        # Contas vencidas
        contas_vencidas_resp = supabase.table('transactions')\
            .select('valor')\
            .eq('tenant_id', tenant_id)\
            .eq('pago', False)\
            .lt('data_vencimento', hoje.strftime('%Y-%m-%d'))\
            .execute()
        
        contas_vencidas = sum(t['valor'] for t in contas_vencidas_resp.data) if contas_vencidas_resp.data else 0
        
        return jsonify({
            'sucesso': True,
            'metricas': {
                'receita_hoje': float(receita_hoje),
                'receita_mes': float(receita_mes),
                'contas_vencer': float(contas_vencer),
                'contas_vencidas': float(contas_vencidas)
            }
        }), 200
    
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
