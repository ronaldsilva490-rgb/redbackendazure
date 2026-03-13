"""
Estoque — Entrada de Mercadorias. v9
Endpoints:
  GET    /api/stock/movements          → histórico de movimentações
  POST   /api/stock/movements          → registrar entrada/saída manual
  GET    /api/stock/alerts             → produtos abaixo do mínimo
  POST   /api/stock/adjust/:product_id → ajuste direto de estoque
"""
from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error
from datetime import date

stock_bp = Blueprint("stock", __name__)


@stock_bp.get("/movements")
@require_auth
def list_movements():
    sb      = get_supabase_admin()
    tid     = request.tenant_id
    limit   = min(int(request.args.get("limit", 100)), 500)
    offset  = int(request.args.get("offset", 0))
    product = request.args.get("product_id")
    tipo    = request.args.get("tipo")  # entrada | saida

    q = sb.table("stock_movements") \
        .select("*, products(nome, unidade, categoria)") \
        .eq("tenant_id", tid) \
        .order("created_at", desc=True) \
        .limit(limit).offset(offset)

    if product:
        q = q.eq("product_id", product)
    if tipo:
        q = q.eq("tipo", tipo)

    return success(q.execute().data)


@stock_bp.post("/movements")
@require_auth
def add_movement():
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}

    product_id  = body.get("product_id")
    tipo        = body.get("tipo")          # entrada | saida | ajuste
    quantidade  = body.get("quantidade")
    motivo      = body.get("motivo", "")   # compra, devolução, perda, ajuste, etc.
    custo_unit  = body.get("custo_unit")   # custo unitário (para entrada)
    fornecedor  = body.get("fornecedor")
    nota_fiscal = body.get("nota_fiscal")
    obs         = body.get("obs")

    if not product_id:
        return error("Produto é obrigatório")
    if tipo not in ("entrada", "saida", "ajuste"):
        return error("Tipo deve ser: entrada, saida ou ajuste")
    if quantidade is None:
        return error("Quantidade é obrigatória")

    try:
        quantidade = float(quantidade)
    except (ValueError, TypeError):
        return error("Quantidade inválida")

    if quantidade <= 0:
        return error("Quantidade deve ser maior que zero")

    # Busca produto
    prod = sb.table("products").select("id, estoque_atual, nome, tenant_id") \
        .eq("id", product_id).eq("tenant_id", tid).maybe_single().execute()
    if not prod.data:
        return error("Produto não encontrado")

    estoque_antes = float(prod.data.get("estoque_atual") or 0)

    # Calcula novo estoque
    if tipo == "entrada":
        estoque_depois = estoque_antes + quantidade
    elif tipo == "saida":
        estoque_depois = max(0, estoque_antes - quantidade)
    else:  # ajuste
        estoque_depois = quantidade  # ajuste direto para o valor informado

    # Registra movimentação
    mov = {
        "tenant_id":    tid,
        "product_id":   product_id,
        "tipo":         tipo,
        "quantidade":   quantidade,
        "estoque_antes": estoque_antes,
        "estoque_depois": estoque_depois,
        "motivo":       motivo or None,
        "custo_unit":   float(custo_unit) if custo_unit else None,
        "fornecedor":   fornecedor or None,
        "nota_fiscal":  nota_fiscal or None,
        "obs":          obs or None,
        "data":         str(date.today()),
        "user_id":      getattr(request, "user_id", None),
    }
    mov_res = sb.table("stock_movements").insert(mov).execute()

    # Atualiza estoque do produto
    update_data = {"estoque_atual": estoque_depois}
    # Se entrada com custo, atualiza preco_custo
    if tipo == "entrada" and custo_unit:
        update_data["preco_custo"] = float(custo_unit)
    sb.table("products").update(update_data).eq("id", product_id).execute()

    return success(mov_res.data[0] if mov_res.data else {}, "Movimentação registrada!", 201)


@stock_bp.get("/alerts")
@require_auth
def stock_alerts():
    sb  = get_supabase_admin()
    tid = request.tenant_id

    # Produtos onde estoque_atual <= estoque_minimo E estoque_minimo > 0
    prods = sb.table("products").select("*") \
        .eq("tenant_id", tid).eq("ativo", True) \
        .gt("estoque_minimo", 0).execute()

    criticos = [p for p in (prods.data or [])
                if float(p.get("estoque_atual") or 0) <= float(p.get("estoque_minimo") or 0)]

    return success(criticos)


@stock_bp.post("/adjust/<product_id>")
@require_auth
@require_papel("dono", "gerente")
def adjust_stock(product_id):
    """Ajuste rápido de estoque — define valor absoluto."""
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}
    novo = body.get("estoque_atual")
    obs  = body.get("obs", "Ajuste manual")

    if novo is None:
        return error("Valor de estoque é obrigatório")
    try:
        novo = float(novo)
    except (ValueError, TypeError):
        return error("Valor inválido")

    prod = sb.table("products").select("id, estoque_atual, tenant_id") \
        .eq("id", product_id).eq("tenant_id", tid).maybe_single().execute()
    if not prod.data:
        return error("Produto não encontrado", 404)

    antes = float(prod.data.get("estoque_atual") or 0)

    sb.table("products").update({"estoque_atual": novo}).eq("id", product_id).execute()

    # Registra movimentação de ajuste
    sb.table("stock_movements").insert({
        "tenant_id":      tid,
        "product_id":     product_id,
        "tipo":           "ajuste",
        "quantidade":     abs(novo - antes),
        "estoque_antes":  antes,
        "estoque_depois": novo,
        "motivo":         "ajuste",
        "obs":            obs,
        "data":           str(date.today()),
        "user_id":        getattr(request, "user_id", None),
    }).execute()

    return success({"estoque_atual": novo}, "Estoque ajustado!")
