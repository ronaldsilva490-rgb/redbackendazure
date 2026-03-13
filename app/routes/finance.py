"""
finance.py — Financeiro completo: contas a pagar, a receber, DRE, fluxo de caixa.
v2.0 — arquitetura separada, sem redundância com Bills.
"""
from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error
from datetime import date, timedelta
import calendar

finance_bp = Blueprint("finance", __name__)

CATEGORIAS_RECEITA = [
    "Venda de Produto", "Prestação de Serviço", "Venda de Veículo",
    "Parcela Financiamento", "Aluguel Recebido", "Comissão",
    "Devolução / Reembolso", "Entrada de Pedido", "Outros Recebimentos",
]
CATEGORIAS_DESPESA = [
    "Fornecedor / Estoque", "Salário / Pró-labore", "Aluguel do Imóvel",
    "Energia Elétrica", "Água / Esgoto", "Internet / Telefone",
    "Impostos / Taxas", "Manutenção / Reparo", "Marketing / Publicidade",
    "Transporte / Frete", "Seguro", "Equipamentos / TI",
    "Alimentação / Benefícios", "Contabilidade / Jurídico", "Outros Pagamentos",
]
FORMAS_PAGAMENTO = [
    "Dinheiro", "PIX", "Cartão Débito", "Cartão Crédito",
    "Transferência", "Boleto", "Cheque", "Fiado", "Permuta",
]


def _sum_col(rows, col="valor"):
    return round(sum(float(r.get(col, 0) or 0) for r in rows), 2)


def _date_range(mes: str):
    y, m = int(mes[:4]), int(mes[5:7])
    last = calendar.monthrange(y, m)[1]
    return f"{mes}-01", f"{mes}-{last:02d}"


@finance_bp.get("/categorias")
@require_auth
def get_categorias():
    return success({
        "receita": CATEGORIAS_RECEITA,
        "despesa": CATEGORIAS_DESPESA,
        "formas":  FORMAS_PAGAMENTO,
    })


@finance_bp.get("/transactions")
@require_auth
def list_transactions():
    tid    = request.tenant_id
    tipo   = request.args.get("tipo")
    pago   = request.args.get("pago")
    mes    = request.args.get("mes")
    cat    = request.args.get("categoria")
    venc   = request.args.get("vencido")
    search = request.args.get("search", "").strip()
    limit  = min(int(request.args.get("limit", 100)), 500)
    offset = int(request.args.get("offset", 0))

    sb = get_supabase_admin()
    q  = sb.table("transactions").select("*") \
        .eq("tenant_id", tid).order("data_vencimento", desc=True)

    if tipo:
        q = q.eq("tipo", tipo)
    if pago is not None:
        q = q.eq("pago", pago.lower() == "true")
    if mes:
        d0, d1 = _date_range(mes)
        q = q.gte("data_vencimento", d0).lte("data_vencimento", d1)
    if cat:
        q = q.eq("categoria", cat)
    if venc and venc.lower() == "true":
        q = q.lt("data_vencimento", str(date.today())).eq("pago", False)
    if search:
        q = q.ilike("descricao", f"%{search}%")

    data = q.limit(limit).offset(offset).execute().data or []

    hoje = date.today()
    for row in data:
        if not row.get("pago") and row.get("data_vencimento"):
            diff = (hoje - date.fromisoformat(row["data_vencimento"])).days
            row["dias_atraso"] = max(0, diff)
        else:
            row["dias_atraso"] = 0

    return success(data)



@finance_bp.get("/contas-pagar")
@require_auth
def contas_pagar():
    """Alias de /transactions filtrado por tipo=despesa com suporte a status."""
    tid    = request.tenant_id
    status = request.args.get("status", "todos")   # pendente | vencido | pago | todos
    limit  = min(int(request.args.get("limit", 200)), 500)
    offset = int(request.args.get("offset", 0))

    sb   = get_supabase_admin()
    hoje = date.today()

    q = sb.table("transactions").select("*") \
        .eq("tenant_id", tid) \
        .eq("tipo", "despesa") \
        .order("data_vencimento", desc=False)

    if status == "pendente":
        q = q.eq("pago", False).gte("data_vencimento", str(hoje))
    elif status == "vencido":
        q = q.eq("pago", False).lt("data_vencimento", str(hoje))
    elif status == "pago":
        q = q.eq("pago", True)
    # "todos" → sem filtro extra

    data = q.limit(limit).offset(offset).execute().data or []

    for row in data:
        if not row.get("pago") and row.get("data_vencimento"):
            diff = (hoje - date.fromisoformat(row["data_vencimento"])).days
            row["dias_atraso"] = max(0, diff)
        else:
            row["dias_atraso"] = 0

    return success(data)


@finance_bp.post("/transactions")
@require_auth
def create_transaction():
    body = request.get_json() or {}
    for f in ["tipo", "descricao", "valor", "data_vencimento"]:
        if not body.get(f) and body.get(f) != 0:
            return error(f"Campo obrigatório: {f}")
    if body["tipo"] not in ("receita", "despesa"):
        return error("tipo deve ser: receita ou despesa")

    try:
        body["valor"] = round(float(body["valor"]), 2)
        if body["valor"] <= 0:
            return error("Valor deve ser maior que zero")
    except (ValueError, TypeError):
        return error("Valor inválido")

    body["tenant_id"] = request.tenant_id
    body.setdefault("pago", False)
    body.setdefault("recorrente", False)

    for f in ["categoria", "forma_pagamento", "obs", "beneficiario",
              "referencia_id", "referencia_tipo", "centro_custo"]:
        if body.get(f) == "":
            body[f] = None

    if body.get("pago") and not body.get("data_pagamento"):
        body["data_pagamento"] = str(date.today())

    try:
        resp = get_supabase_admin().table("transactions").insert(body).execute()
        tx = resp.data[0]
        if body.get("recorrente") and not body.get("referencia_tipo"):
            _gerar_recorrencia(tx)
        return success(tx, "Lançamento criado", 201)
    except Exception as e:
        return error(f"Erro ao criar lançamento: {str(e)}", 500)


@finance_bp.put("/transactions/<tx_id>")
@require_auth
def update_transaction(tx_id):
    body = request.get_json() or {}
    for f in ["id", "tenant_id", "created_at"]:
        body.pop(f, None)

    if "valor" in body:
        try:
            body["valor"] = round(float(body["valor"]), 2)
            if body["valor"] <= 0:
                return error("Valor deve ser maior que zero")
        except Exception:
            return error("Valor inválido")

    for f in ["categoria", "forma_pagamento", "obs", "beneficiario", "centro_custo"]:
        if body.get(f) == "":
            body[f] = None

    resp = get_supabase_admin().table("transactions") \
        .update(body).eq("id", tx_id).eq("tenant_id", request.tenant_id).execute()
    if not resp.data:
        return error("Lançamento não encontrado", 404)
    return success(resp.data[0], "Lançamento atualizado")


@finance_bp.patch("/transactions/<tx_id>/pagar")
@require_auth
def marcar_pago(tx_id):
    body   = request.get_json() or {}
    update = {
        "pago":           True,
        "data_pagamento": body.get("data_pagamento") or str(date.today()),
    }
    if body.get("forma_pagamento"):
        update["forma_pagamento"] = body["forma_pagamento"]
    if body.get("valor_pago"):
        try:
            update["valor_pago"] = round(float(body["valor_pago"]), 2)
        except Exception:
            pass

    resp = get_supabase_admin().table("transactions").update(update) \
        .eq("id", tx_id).eq("tenant_id", request.tenant_id).execute()
    if not resp.data:
        return error("Lançamento não encontrado", 404)
    return success(resp.data[0], "Marcado como pago")


@finance_bp.patch("/transactions/<tx_id>/estornar")
@require_auth
@require_papel("dono", "gerente")
def estornar(tx_id):
    update = {"pago": False, "data_pagamento": None, "valor_pago": None}
    resp = get_supabase_admin().table("transactions").update(update) \
        .eq("id", tx_id).eq("tenant_id", request.tenant_id).execute()
    if not resp.data:
        return error("Lançamento não encontrado", 404)
    return success(resp.data[0], "Pagamento estornado")


@finance_bp.delete("/transactions/<tx_id>")
@require_auth
@require_papel("dono", "gerente")
def delete_transaction(tx_id):
    get_supabase_admin().table("transactions") \
        .delete().eq("id", tx_id).eq("tenant_id", request.tenant_id).execute()
    return success(message="Lançamento removido")


@finance_bp.get("/summary")
@require_auth
def summary():
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    mes  = request.args.get("mes") or str(date.today())[:7]
    hoje = str(date.today())
    d0, d1 = _date_range(mes)

    def qtotal(tipo, pago, d_ini=None, d_fim=None):
        q = sb.table("transactions").select("valor") \
            .eq("tenant_id", tid).eq("tipo", tipo).eq("pago", pago)
        if d_ini:
            q = q.gte("data_vencimento", d_ini).lte("data_vencimento", d_fim)
        return _sum_col(q.execute().data or [])

    rec_pagas  = qtotal("receita", True,  d0, d1)
    desp_pagas = qtotal("despesa", True,  d0, d1)
    rec_pend   = qtotal("receita", False, d0, d1)
    desp_pend  = qtotal("despesa", False, d0, d1)

    q_venc = sb.table("transactions").select("valor, tipo") \
        .eq("tenant_id", tid).eq("pago", False).lt("data_vencimento", hoje)
    venc_rows    = q_venc.execute().data or []
    venc_receber = _sum_col([r for r in venc_rows if r["tipo"] == "receita"])
    venc_pagar   = _sum_col([r for r in venc_rows if r["tipo"] == "despesa"])

    d7    = str(date.today() + timedelta(days=7))
    q_prx = sb.table("transactions").select("valor, tipo") \
        .eq("tenant_id", tid).eq("pago", False) \
        .gte("data_vencimento", hoje).lte("data_vencimento", d7)
    prox_rows    = q_prx.execute().data or []
    prox_receber = _sum_col([r for r in prox_rows if r["tipo"] == "receita"])
    prox_pagar   = _sum_col([r for r in prox_rows if r["tipo"] == "despesa"])

    return success({
        "mes":              mes,
        "receitas":         rec_pagas,
        "despesas":         desp_pagas,
        "saldo_realizado":  round(rec_pagas - desp_pagas, 2),
        "a_receber_mes":    rec_pend,
        "a_pagar_mes":      desp_pend,
        "resultado_previsto": round((rec_pagas + rec_pend) - (desp_pagas + desp_pend), 2),
        "vencido_receber":  venc_receber,
        "vencido_pagar":    venc_pagar,
        "prox_receber":     prox_receber,
        "prox_pagar":       prox_pagar,
        "a_receber":        qtotal("receita", False),
        "a_pagar":          qtotal("despesa", False),
    })


@finance_bp.get("/fluxo-caixa")
@require_auth
def fluxo_caixa():
    sb    = get_supabase_admin()
    tid   = request.tenant_id
    meses = int(request.args.get("meses", 6))
    hoje  = date.today()
    resultado = []

    for i in range(meses - 1, -1, -1):
        year  = hoje.year + (hoje.month - 1 - i) // 12
        month = (hoje.month - 1 - i) % 12 + 1
        mes_str = f"{year}-{month:02d}"
        d0, d1  = _date_range(mes_str)
        rows = sb.table("transactions").select("tipo, valor, pago") \
            .eq("tenant_id", tid) \
            .gte("data_vencimento", d0).lte("data_vencimento", d1) \
            .execute().data or []
        rec  = _sum_col([r for r in rows if r["tipo"] == "receita" and r["pago"]])
        desp = _sum_col([r for r in rows if r["tipo"] == "despesa" and r["pago"]])
        resultado.append({
            "mes": mes_str, "receitas": rec,
            "despesas": desp, "saldo": round(rec - desp, 2),
        })
    return success(resultado)


@finance_bp.get("/dre")
@require_auth
def dre():
    sb  = get_supabase_admin()
    tid = request.tenant_id
    mes = request.args.get("mes") or str(date.today())[:7]
    d0, d1 = _date_range(mes)

    rows = sb.table("transactions").select("tipo, categoria, valor, pago") \
        .eq("tenant_id", tid).eq("pago", True) \
        .gte("data_vencimento", d0).lte("data_vencimento", d1) \
        .execute().data or []

    receitas_cat = {}
    despesas_cat = {}
    for r in rows:
        cat = r.get("categoria") or "Outros"
        val = float(r.get("valor", 0))
        if r["tipo"] == "receita":
            receitas_cat[cat] = round(receitas_cat.get(cat, 0) + val, 2)
        else:
            despesas_cat[cat] = round(despesas_cat.get(cat, 0) + val, 2)

    total_rec  = round(sum(receitas_cat.values()), 2)
    total_desp = round(sum(despesas_cat.values()), 2)
    lucro      = round(total_rec - total_desp, 2)
    margem     = round((lucro / total_rec * 100), 1) if total_rec else 0

    return success({
        "mes": mes,
        "receitas":    {"total": total_rec,  "por_categoria": receitas_cat},
        "despesas":    {"total": total_desp, "por_categoria": despesas_cat},
        "lucro_bruto": lucro,
        "margem_pct":  margem,
    })


def _gerar_recorrencia(tx: dict):
    try:
        from dateutil.relativedelta import relativedelta
        venc = date.fromisoformat(tx["data_vencimento"])
        prox = venc + relativedelta(months=1)
    except Exception:
        venc = date.fromisoformat(tx["data_vencimento"])
        prox = venc + timedelta(days=30)

    nova = {k: v for k, v in tx.items()
            if k not in ("id", "created_at", "updated_at", "pago",
                         "data_pagamento", "valor_pago")}
    nova["data_vencimento"] = str(prox)
    nova["pago"]            = False
    nova["data_pagamento"]  = None
    nova["referencia_id"]   = tx["id"]
    nova["referencia_tipo"] = "recorrencia"
    try:
        get_supabase_admin().table("transactions").insert(nova).execute()
    except Exception:
        pass
