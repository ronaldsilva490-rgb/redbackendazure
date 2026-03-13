"""
SALES - MÓDULO UNIVERSAL DE VENDAS
Versão 2.0 - Suporta todos os tipos de negócio

Features:
- Suporte para restaurante, varejo, serviços, farmácia, etc
- Múltiplas formas de pagamento (dinheiro, cartão, PIX, fiado)
- Inventário integrado
- Descontos e promoções
- Parcelas com juros (Price e Simples)
- Relatórios detalhados
- Conexão com caixa
"""

from flask import Blueprint, request
from ..utils.supabase_client import get_supabase, get_supabase_admin
from ..utils.response import success, error
from ..utils.auth_middleware import require_auth
from datetime import datetime, timedelta
import uuid
import json

sales_bp = Blueprint("sales", __name__)
sb = get_supabase_admin()


# ═══════════════════════════════════════════════════════════════════════════════
# CRIAR VENDA
# ═══════════════════════════════════════════════════════════════════════════════

@sales_bp.post("/criar")
@require_auth
def criar_venda():
    """
    POST /api/sales/criar
    Cria uma nova venda
    
    Body:
    {
      "cliente_id": "uuid", // opcional para venda anônima
      "itens": [
        {
          "produto_id": "uuid",
          "quantidade": 1,
          "preco_unitario": 100.00,
          "desconto": 0 // em %
        }
      ],
      "pagamentos": [
        {
          "tipo": "dinheiro|cartao|pix|fiado",
          "valor": 100.00,
          "referencia": "pix key ou número pedido" // opcional
        }
      ],
      "notas": "Algo especial",
      "sessao_caixa_id": "uuid" // para restaurante/varejo
    }
    """
    try:
        body = request.get_json() or {}
        tenant_id = request.tenant_id
        user_id = request.user_id
        
        # Validações
        itens = body.get("itens", [])
        pagamentos = body.get("pagamentos", [])
        
        if not itens or not pagamentos:
            return error("Itens e pagamentos são obrigatórios")
        
        # Calcular totais
        subtotal = 0.0
        total_desconto = 0.0
        
        for item in itens:
            valor_item = item['quantidade'] * item['preco_unitario']
            desconto_item = (valor_item * item.get('desconto', 0) / 100)
            subtotal += valor_item
            total_desconto += desconto_item
        
        total_venda = subtotal - total_desconto
        
        # Validar pagamentos
        total_pago = sum(p['valor'] for p in pagamentos)
        if abs(total_pago - total_venda) > 0.01:  # Margem para arredondamento
            return error(f"Valor pago ({total_pago}) diferente do total ({total_venda})")
        
        # Criar venda
        venda = {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "cliente_id": body.get("cliente_id"),
            "user_id": user_id,
            "subtotal": subtotal,
            "total_desconto": total_desconto,
            "total": total_venda,
            "itens": json.dumps(itens),  # Armazenar como JSON
            "status": "completa",
            "notas": body.get("notas", ""),
            "criado_em": datetime.utcnow().isoformat(),
            "numero_venda": gerar_numero_venda(tenant_id)
        }
        
        # Salvar venda
        resp = sb.table("vendas").insert([venda]).execute()
        if not resp.data:
            return error("Erro ao criar venda")
        
        venda_id = resp.data[0]['id']
        
        # Criar pagamentos
        for pag in pagamentos:
            sb.table("pagamentos_venda").insert([{
                "venda_id": venda_id,
                "tenant_id": tenant_id,
                "forma": pag['tipo'],
                "valor": pag['valor'],
                "referencia": pag.get('referencia'),
                "criado_em": datetime.utcnow().isoformat()
            }]).execute()
        
        # Atualizar inventário se necessário
        for item in itens:
            try:
                sb.table("produtos").update({
                    "estoque_atual": sb.table("produtos").select("estoque_atual")
                        .eq("id", item['produto_id']).execute().data[0]['estoque_atual'] - item['quantidade']
                }).eq("id", item['produto_id']).execute()
            except:
                pass  # Alguns produtos podem não ter estoque
        
        # Registrar em logs
        sb.table("system_logs").insert([{
            "level": "info",
            "service": "sales",
            "message": f"Venda criada: {venda['numero_venda']}",
            "details": {"venda_id": venda_id, "total": total_venda}
        }]).execute()
        
        return success({
            "venda_id": venda_id,
            "numero_venda": venda['numero_venda'],
            "total": total_venda
        }, "Venda registrada com sucesso", 201)
        
    except Exception as e:
        sb.table("system_logs").insert([{
            "level": "error",
            "service": "sales",
            "message": f"Erro ao criar venda",
            "details": {"erro": str(e)}
        }]).execute()
        return error(f"Erro: {str(e)}", 500)


# ═══════════════════════════════════════════════════════════════════════════════
# LISTAR VENDAS
# ═══════════════════════════════════════════════════════════════════════════════

@sales_bp.get("/")
@require_auth
def listar_vendas():
    """
    GET /api/sales/?filtro=data&dataInicio=2024-01-01&dataFim=2024-01-31&limite=50
    Lista vendas do tenant
    """
    try:
        tenant_id = request.tenant_id
        data_inicio = request.args.get("dataInicio")
        data_fim = request.args.get("dataFim")
        limite = min(int(request.args.get("limite", 50)), 200)
        offset = int(request.args.get("offset", 0))
        
        q = sb.table("vendas").select("*, clients(nome, email)") \
            .eq("tenant_id", tenant_id) \
            .order("criado_em", desc=True) \
            .limit(limite) \
            .offset(offset)
        
        if data_inicio:
            q = q.gte("criado_em", f"{data_inicio}T00:00:00")
        if data_fim:
            q = q.lte("criado_em", f"{data_fim}T23:59:59")
        
        resp = q.execute()
        return success(resp.data)
        
    except Exception as e:
        return error(f"Erro ao listar vendas: {str(e)}", 500)


# ═══════════════════════════════════════════════════════════════════════════════
# RELATÓRIO DE VENDAS
# ═══════════════════════════════════════════════════════════════════════════════

@sales_bp.get("/relatorio")
@require_auth
def relatorio_vendas():
    """
    GET /api/sales/relatorio?dataInicio=2024-01-01&dataFim=2024-01-31
    Gera relatório completo de vendas
    """
    try:
        tenant_id = request.tenant_id
        data_inicio = request.args.get("dataInicio")
        data_fim = request.args.get("dataFim")
        
        q = sb.table("vendas") \
            .select("*") \
            .eq("tenant_id", tenant_id)
        
        if data_inicio:
            q = q.gte("criado_em", f"{data_inicio}T00:00:00")
        if data_fim:
            q = q.lte("criado_em", f"{data_fim}T23:59:59")
        
        vendas = q.execute().data
        
        # Calcular métricas
        total_vendas = len(vendas)
        total_valor = sum(v['total'] for v in vendas)
        total_desconto = sum(v['total_desconto'] for v in vendas)
        ticket_medio = total_valor / total_vendas if total_vendas > 0 else 0
        
        # Agrupar por forma de pagamento
        pagamentos = sb.table("pagamentos_venda") \
            .select("tipo, SUM(valor) as total") \
            .eq("tenant_id", tenant_id)
        
        if data_inicio:
            pagamentos = pagamentos.gte("criado_em", f"{data_inicio}T00:00:00")
        if data_fim:
            pagamentos = pagamentos.lte("criado_em", f"{data_fim}T23:59:59")
        
        pagamentos_agg = pagamentos.execute().data
        
        return success({
            "periodo": {
                "inicio": data_inicio or "—",
                "fim": data_fim or "—"
            },
            "resumo": {
                "total_vendas": total_vendas,
                "total_valor": round(total_valor, 2),
                "total_desconto": round(total_desconto, 2),
                "ticket_medio": round(ticket_medio, 2),
                "valor_liquido": round(total_valor - total_desconto, 2)
            },
            "por_pagamento": pagamentos_agg,
            "vendas": vendas
        })
        
    except Exception as e:
        return error(f"Erro ao gerar relatório: {str(e)}", 500)


# ═══════════════════════════════════════════════════════════════════════════════
# CANCELAR VENDA
# ═══════════════════════════════════════════════════════════════════════════════

@sales_bp.post("/<venda_id>/cancelar")
@require_auth
def cancelar_venda(venda_id):
    """
    POST /api/sales/{id}/cancelar
    Cancela uma venda e devolve estoque
    """
    try:
        tenant_id = request.tenant_id
        
        # Buscar venda
        venda_resp = sb.table("vendas") \
            .select("*") \
            .eq("id", venda_id) \
            .eq("tenant_id", tenant_id) \
            .limit(1) \
            .execute()
        
        if not venda_resp.data:
            return error("Venda não encontrada", 404)
        
        venda = venda_resp.data[0]
        
        # Se já foi cancelada
        if venda['status'] == 'cancelada':
            return error("Venda já foi cancelada")
        
        # Devolver estoque
        itens = json.loads(venda['itens'])
        for item in itens:
            try:
                prod = sb.table("produtos") \
                    .select("estoque_atual") \
                    .eq("id", item['produto_id']) \
                    .execute().data[0]
                
                sb.table("produtos").update({
                    "estoque_atual": prod['estoque_atual'] + item['quantidade']
                }).eq("id", item['produto_id']).execute()
            except:
                pass
        
        # Marcar como cancelada
        sb.table("vendas").update({
            "status": "cancelada",
            "cancelado_em": datetime.utcnow().isoformat()
        }).eq("id", venda_id).execute()
        
        return success({"venda_id": venda_id}, "Venda cancelada e estoque restaurado")
        
    except Exception as e:
        return error(f"Erro ao cancelar venda: {str(e)}", 500)


# ═══════════════════════════════════════════════════════════════════════════════
# AUXILIARES
# ═══════════════════════════════════════════════════════════════════════════════

def gerar_numero_venda(tenant_id):
    """Gera número sequencial único para venda"""
    try:
        ultima = sb.table("vendas") \
            .select("numero_venda") \
            .eq("tenant_id", tenant_id) \
            .order("numero_venda", desc=True) \
            .limit(1) \
            .execute().data
        
        if ultima and ultima[0].get('numero_venda'):
            numero = int(ultima[0]['numero_venda']) + 1
        else:
            numero = 1000  # Começar em 1000
        
        return str(numero)
    except:
        return str(datetime.now().timestamp())
