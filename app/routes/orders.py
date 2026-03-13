"""
Pedidos / Comandas — gerenciamento completo de pedidos. v7
Fluxo restaurante:
  aberto → em_preparo → pronto → fechado
  (delivery): aberto → em_preparo → pronto → saindo → entregue → fechado

LÓGICA CORRIGIDA v7:
- Balcão: itens com destino=cozinha ainda vão para cozinha; destino=balcao notifica GARÇOM (não caixa)
- Garçom: confirma busca de item de bar/balcão separadamente
- Adicionar itens a pedidos em andamento (em_preparo)
- Cancelar item individual (com notificação se já estava em preparo)
- Histórico paginado com filtro de data
"""
from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error
from .notifications import criar_notif
from datetime import date, datetime, timezone

orders_bp = Blueprint("orders", __name__)

VALID_STATUS = ["aberto", "em_preparo", "pronto", "saindo", "entregue", "fechado", "cancelado"]


def _proximo_numero(sb, tenant_id):
    try:
        hoje = str(date.today())
        res = sb.table("orders").select("numero_pedido").eq("tenant_id", tenant_id) \
            .gte("created_at", hoje).order("numero_pedido", desc=True).limit(1).execute()
        ultimo = (res.data or [{}])[0].get("numero_pedido") or 0
        return (ultimo or 0) + 1
    except Exception:
        return 1


# ── LISTAR ─────────────────────────────────────────────────────────────────────

@orders_bp.get("/")
@require_auth
def list_orders():
    tid    = request.tenant_id
    status = request.args.get("status")
    entrega = request.args.get("delivery")
    limit  = min(int(request.args.get("limit", 100)), 200)
    offset = int(request.args.get("offset", 0))
    data_de = request.args.get("data_de")     # filtro de histórico
    data_ate = request.args.get("data_ate")

    query = get_supabase_admin().table("orders") \
        .select("*, tables(numero, capacidade), clients(nome, telefone)") \
        .eq("tenant_id", tid) \
        .order("created_at", desc=True) \
        .limit(limit) \
        .offset(offset)

    if status:
        # Suporta múltiplos status separados por vírgula
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            query = query.eq("status", statuses[0])
        else:
            query = query.in_("status", statuses)
    if entrega == "1":
        query = query.eq("is_delivery", True)
    if data_de:
        query = query.gte("created_at", data_de)
    if data_ate:
        query = query.lte("created_at", data_ate)

    return success(query.execute().data)


@orders_bp.get("/<order_id>")
@require_auth
def get_order(order_id):
    sb  = get_supabase_admin()
    tid = request.tenant_id

    order = sb.table("orders") \
        .select("*, tables(numero, capacidade), clients(nome, telefone, cpf_cnpj)") \
        .eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()

    if not order.data:
        return error("Pedido não encontrado", 404)

    items = sb.table("order_items") \
        .select("*, products(nome, codigo_barras)") \
        .eq("order_id", order_id).execute()

    result = order.data
    result["items"] = items.data
    return success(result)


# ── CRIAR PEDIDO ──────────────────────────────────────────────────────────────

@orders_bp.post("/")
@require_auth
def create_order():
    sb   = get_supabase_admin()
    body = request.get_json() or {}
    tid  = request.tenant_id

    body["tenant_id"]     = tid
    body["status"]        = body.get("status", "aberto")
    body["total"]         = 0
    body["numero_pedido"] = _proximo_numero(sb, tid)

    is_delivery = bool(body.get("is_delivery"))
    body["is_delivery"] = is_delivery

    for f in ["table_id", "client_id", "obs", "delivery_nome", "delivery_tel",
              "delivery_end", "delivery_compl", "delivery_bairro", "delivery_obs"]:
        if body.get(f) == "":
            body[f] = None

    resp  = sb.table("orders").insert(body).execute()
    order = resp.data[0]

    if order.get("table_id"):
        sb.table("tables").update({"status": "ocupada"}) \
            .eq("id", order["table_id"]).eq("tenant_id", tid).execute()

    if is_delivery:
        msg = f"Delivery #{order['numero_pedido']} — {body.get('delivery_nome', '')}"
        criar_notif(sb, tid, "caixa",   "novo_pedido", f"🛵 Novo Delivery #{order['numero_pedido']}", msg, order["id"])
        criar_notif(sb, tid, "gerente", "novo_pedido", f"🛵 Novo Delivery #{order['numero_pedido']}", msg, order["id"])
    else:
        mesa_num = ""
        if order.get("table_id"):
            t = sb.table("tables").select("numero").eq("id", order["table_id"]).maybe_single().execute()
            mesa_num = f"Mesa {t.data['numero']}" if t.data else ""
        msg = f"Comanda aberta {mesa_num} — pedido #{order['numero_pedido']}"
        criar_notif(sb, tid, "caixa", "novo_pedido", f"🍽️ {msg}", None, order["id"])

    return success(order, "Pedido criado", 201)


# ── ADICIONAR ITEM ─────────────────────────────────────────────────────────────

@orders_bp.post("/<order_id>/items")
@require_auth
def add_item(order_id):
    sb  = get_supabase_admin()
    tid = request.tenant_id

    order = sb.table("orders").select("id, total, status, table_id, numero_pedido, is_delivery, tables(numero)") \
        .eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()
    if not order.data:
        return error("Pedido não encontrado", 404)
    if order.data["status"] in ("fechado", "cancelado"):
        return error("Pedido já encerrado, não é possível adicionar itens")

    body = request.get_json() or {}
    if not body.get("nome") or body.get("preco_unit") is None or body.get("qtd") is None:
        return error("nome, preco_unit e qtd são obrigatórios")

    try:
        qtd   = float(body["qtd"])
        preco = float(body["preco_unit"])
    except (ValueError, TypeError):
        return error("qtd e preco_unit devem ser números")

    subtotal = round(qtd * preco, 2)
    destino  = body.get("destino") or "balcao"

    item_data = {
        "order_id":    order_id,
        "nome":        body["nome"],
        "qtd":         qtd,
        "preco_unit":  preco,
        "subtotal":    subtotal,
        "obs":         body.get("obs") or None,
        "product_id":  body.get("product_id") or None,
        "destino":     destino,
        "status_item": "pendente",
    }
    item_resp = sb.table("order_items").insert(item_data).execute()

    # Recalcula total
    all_items  = sb.table("order_items").select("subtotal").eq("order_id", order_id).execute()
    novo_total = round(sum(float(i["subtotal"] or 0) for i in all_items.data), 2)
    sb.table("orders").update({"total": novo_total}).eq("id", order_id).eq("tenant_id", tid).execute()

    # Se pedido já está em_preparo e item é de cozinha → notifica cozinha imediatamente (item adicional)
    od = order.data
    if od["status"] == "em_preparo" and destino == "cozinha":
        mesa = od.get("tables") or {}
        mesa_label = f"Mesa {mesa.get('numero')}" if mesa.get("numero") else f"Pedido #{od.get('numero_pedido', '?')}"
        criar_notif(sb, tid, "cozinheiro", "pedido_cozinha",
                    f"🍳 Item adicional — {mesa_label}",
                    f"{qtd}x {body['nome']}", order_id)

    # Se pedido em_preparo e item é de bar → notifica garçom que tem item de bar para buscar
    if od["status"] == "em_preparo" and destino == "bar":
        mesa = od.get("tables") or {}
        mesa_label = f"Mesa {mesa.get('numero')}" if mesa.get("numero") else f"Pedido #{od.get('numero_pedido', '?')}"
        criar_notif(sb, tid, "garcom", "pedido_bar",
                    f"🍺 Buscar no bar — {mesa_label}",
                    f"{qtd}x {body['nome']}", order_id)

    return success({**item_resp.data[0], "order_total": novo_total}, "Item adicionado", 201)


# ── CONFIRMAR ITEM BUSCADO (garçom confirma que buscou e entregou) ─────────────

@orders_bp.patch("/<order_id>/items/<item_id>/confirmar")
@require_auth
def confirmar_item_garcom(order_id, item_id):
    """Garçom confirma que buscou e entregou um item de bar/proprio/balcao."""
    sb  = get_supabase_admin()
    tid = request.tenant_id

    item = sb.table("order_items").select("id, destino, nome, status_item") \
        .eq("id", item_id).eq("order_id", order_id).maybe_single().execute()
    if not item.data:
        return error("Item não encontrado", 404)

    sb.table("order_items").update({
        "status_item": "pronto",
    }).eq("id", item_id).execute()

    return success(message=f"Item '{item.data['nome']}' confirmado como entregue.")


# ── CANCELAR ITEM INDIVIDUAL ───────────────────────────────────────────────────

@orders_bp.delete("/<order_id>/items/<item_id>")
@require_auth
def remove_item(order_id, item_id):
    sb  = get_supabase_admin()
    tid = request.tenant_id

    order = sb.table("orders").select("id, status, numero_pedido, tables(numero)") \
        .eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()
    if not order.data:
        return error("Pedido não encontrado", 404)
    if order.data["status"] in ("fechado", "cancelado"):
        return error("Pedido já encerrado")

    # Verifica se item estava em preparo (notifica cozinha)
    item = sb.table("order_items").select("id, nome, qtd, destino, status_item") \
        .eq("id", item_id).eq("order_id", order_id).maybe_single().execute()

    if item.data and item.data.get("status_item") == "em_preparo" and item.data.get("destino") == "cozinha":
        od    = order.data
        mesa  = od.get("tables") or {}
        mesa_label = f"Mesa {mesa.get('numero')}" if mesa.get("numero") else f"Pedido #{od.get('numero_pedido', '?')}"
        criar_notif(sb, tid, "cozinheiro", "pedido_cancelado",
                    f"❌ Item cancelado — {mesa_label}",
                    f"{item.data['qtd']}x {item.data['nome']} foi removido do pedido", order_id)

    sb.table("order_items").delete().eq("id", item_id).eq("order_id", order_id).execute()

    all_items  = sb.table("order_items").select("subtotal").eq("order_id", order_id).execute()
    novo_total = round(sum(float(i["subtotal"] or 0) for i in all_items.data), 2)
    sb.table("orders").update({"total": novo_total}).eq("id", order_id).eq("tenant_id", tid).execute()

    return success({"order_total": novo_total}, "Item removido")


# ── ATUALIZAR STATUS DE ITEM (cozinheiro) ─────────────────────────────────────

@orders_bp.patch("/<order_id>/items/<item_id>/status")
@require_auth
def update_item_status(order_id, item_id):
    sb    = get_supabase_admin()
    tid   = request.tenant_id
    body  = request.get_json() or {}
    st    = body.get("status_item")
    valid = ["pendente", "em_preparo", "pronto"]
    if st not in valid:
        return error(f"status_item inválido. Valores aceitos: {', '.join(valid)}")

    sb.table("order_items").update({"status_item": st}) \
        .eq("id", item_id).eq("order_id", order_id).execute()

    # Se marcou como pronto, verifica se todos itens da cozinha estão prontos
    if st == "pronto":
        all_items = sb.table("order_items").select("status_item, destino").eq("order_id", order_id).execute()
        kitchen_items = [i for i in (all_items.data or []) if i.get("destino") == "cozinha"]
        all_done = all(i.get("status_item") == "pronto" for i in kitchen_items) if kitchen_items else False

        if all_done:
            order = sb.table("orders") \
                .select("table_id, numero_pedido, is_delivery, tables(numero)") \
                .eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()
            d    = order.data or {}
            mesa = d.get("tables") or {}
            num  = d.get("numero_pedido", "?")
            mesa_label = f"Mesa {mesa.get('numero')}" if mesa.get("numero") else f"Pedido #{num}"
            msg  = f"{mesa_label} — todos os itens da cozinha prontos!"
            criar_notif(sb, tid, "garcom", "pedido_pronto", f"✅ {msg}", None, order_id)
            criar_notif(sb, tid, "caixa",  "pedido_pronto", f"✅ {msg}", None, order_id)
            if d.get("is_delivery"):
                criar_notif(sb, tid, "entregador", "pedido_pronto",
                            f"📦 Pronto p/ entrega #{num}", msg, order_id)

    return success(message=f"Item atualizado: {st}")


# ── ATUALIZAR STATUS DO PEDIDO ────────────────────────────────────────────────

@orders_bp.patch("/<order_id>/status")
@require_auth
def update_status(order_id):
    body   = request.get_json() or {}
    status = body.get("status")
    if status not in VALID_STATUS:
        return error(f"Status inválido. Valores aceitos: {', '.join(VALID_STATUS)}")

    sb  = get_supabase_admin()
    tid = request.tenant_id

    order = sb.table("orders") \
        .select("id, table_id, total, status, numero_pedido, is_delivery, delivery_nome, delivery_pago, forma_pagamento, tables(numero)") \
        .eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()
    if not order.data:
        return error("Pedido não encontrado", 404)

    d    = order.data
    num  = d.get("numero_pedido", "?")
    mesa = d.get("tables") or {}
    prev = d.get("status")

    if prev in ("fechado", "cancelado"):
        return error(f"Pedido já {prev}. Não é possível alterar.")

    # Validação: fechar pedido só se cozinha está pronta
    if status == "fechado":
        all_items = sb.table("order_items").select("status_item, destino").eq("order_id", order_id).execute()
        items_data = all_items.data or []
        cozinha_items = [i for i in items_data if i.get("destino") == "cozinha"]
        nao_prontos = [i for i in cozinha_items if i.get("status_item") != "pronto"]
        if nao_prontos:
            return error(
                f"Não é possível fechar o pedido: {len(nao_prontos)} item(s) da cozinha ainda não foram marcados como prontos.",
                400
            )

        # Validação fiado: exige cliente cadastrado
        forma_pag = body.get("forma_pagamento") or d.get("forma_pagamento") or ""
        if forma_pag.lower() == "fiado":
            if not d.get("client_id"):
                return error("Fiado só pode ser registrado para clientes cadastrados. Associe um cliente ao pedido.", 400)

    update_data = {"status": status}
    if body.get("forma_pagamento"):
        update_data["forma_pagamento"] = body["forma_pagamento"]
    if body.get("delivery_pago") is not None:
        update_data["delivery_pago"] = bool(body["delivery_pago"])

    now_iso = datetime.now(timezone.utc).isoformat()
    if status == "saindo":
        update_data["saindo_at"] = now_iso
    elif status == "entregue":
        update_data["entregue_at"] = now_iso

    sb.table("orders").update(update_data).eq("id", order_id).eq("tenant_id", tid).execute()

    mesa_label = f"Mesa {mesa.get('numero')}" if mesa.get("numero") else f"Pedido #{num}"

    # ── Notificações por transição ────────────────────────────────────────────
    if status == "em_preparo" and prev == "aberto":
        items = sb.table("order_items").select("destino, nome, qtd").eq("order_id", order_id).execute()
        items_data = items.data or []

        has_cozinha = any(i.get("destino") == "cozinha" for i in items_data)
        has_bar     = any(i.get("destino") == "bar" for i in items_data)
        # destino=balcao e destino=proprio → garçom busca (notifica garçom, não caixa)
        has_garcom_busca = any(i.get("destino") in ("balcao", "proprio") for i in items_data)

        if has_cozinha:
            itens_str = ", ".join(
                f"{i['qtd']}x {i['nome']}" for i in items_data if i.get("destino") == "cozinha"
            )
            criar_notif(sb, tid, "cozinheiro", "pedido_cozinha",
                        f"🍳 Novo pedido — {mesa_label}", itens_str, order_id)

        if has_bar:
            itens_str = ", ".join(
                f"{i['qtd']}x {i['nome']}" for i in items_data if i.get("destino") == "bar"
            )
            # Notifica o GARÇOM para buscar no bar
            criar_notif(sb, tid, "garcom", "pedido_bar",
                        f"🍺 Buscar no bar — {mesa_label}", itens_str, order_id)

        if has_garcom_busca:
            itens_str = ", ".join(
                f"{i['qtd']}x {i['nome']}" for i in items_data if i.get("destino") in ("balcao", "proprio")
            )
            # Notifica o GARÇOM para buscar no balcão/estoque próprio
            criar_notif(sb, tid, "garcom", "pedido_balcao",
                        f"🪟 Buscar no balcão — {mesa_label}", itens_str, order_id)

        criar_notif(sb, tid, "caixa", "pedido_enviado",
                    f"📋 Pedido enviado — {mesa_label} #{num}", None, order_id)

    elif status == "pronto":
        msg = f"{mesa_label} #{num} — pronto para servir!"
        criar_notif(sb, tid, "garcom", "pedido_pronto", f"✅ {msg}", None, order_id)
        criar_notif(sb, tid, "caixa",  "pedido_pronto", f"✅ {msg}", None, order_id)
        if d.get("is_delivery"):
            criar_notif(sb, tid, "entregador", "pedido_pronto",
                        f"📦 Pronto p/ entrega #{num}", d.get("delivery_nome", ""), order_id)

    elif status == "saindo":
        criar_notif(sb, tid, "caixa", "pedido_saindo",
                    f"🛵 Delivery #{num} saiu para entrega", d.get("delivery_nome", ""), order_id)

    elif status == "entregue":
        msg = f"Delivery #{num} entregue — {d.get('delivery_nome', '')}"
        criar_notif(sb, tid, "caixa", "pedido_entregue", f"✅ {msg}", None, order_id)

    elif status == "cancelado":
        motivo = body.get("motivo") or "Pedido cancelado"
        msg_cancel = f"{mesa_label} #{num} — {motivo}"

        if d.get("table_id"):
            sb.table("tables").update({"status": "livre"}) \
                .eq("id", d["table_id"]).eq("tenant_id", tid).execute()

        for papel in ["garcom", "cozinheiro", "caixa", "gerente"]:
            criar_notif(sb, tid, papel, "pedido_cancelado",
                        f"❌ Pedido cancelado — {mesa_label} #{num}", msg_cancel, order_id)
        if d.get("is_delivery"):
            criar_notif(sb, tid, "entregador", "pedido_cancelado",
                        f"❌ Delivery cancelado #{num}", msg_cancel, order_id)

    elif status == "fechado":
        if d.get("table_id"):
            sb.table("tables").update({"status": "livre"}) \
                .eq("id", d["table_id"]).eq("tenant_id", tid).execute()

        # Registra transação financeira
        if float(d.get("total") or 0) > 0:
            try:
                forma = body.get("forma_pagamento") or d.get("forma_pagamento") or "Não informado"
                descricao = f"Venda #{num}"
                if mesa.get("numero"):
                    descricao += f" — Mesa {mesa.get('numero')}"
                elif d.get("is_delivery"):
                    descricao += f" — Delivery {d.get('delivery_nome', '')}"

                # FIADO: nunca é pago imediatamente — vai para contas a receber
                is_fiado = forma.lower() == "fiado"
                tx_pago = not is_fiado  # fiado = não pago, demais formas = pago

                sb.table("transactions").insert({
                    "tenant_id":       tid,
                    "tipo":            "receita",
                    "descricao":       descricao,
                    "valor":           float(d["total"]),
                    "data_vencimento": str(date.today()),
                    "data_pagamento":  str(date.today()) if tx_pago else None,
                    "pago":            tx_pago,
                    "categoria":       "venda",
                    "forma_pagamento": forma,
                    "referencia_id":   order_id,
                    "referencia_tipo": "order",
                }).execute()
            except Exception:
                pass

        forma_pag = body.get("forma_pagamento") or d.get("forma_pagamento") or ""
        criar_notif(sb, tid, "garcom", "pagamento_ok",
                    f"💰 Pago — {mesa_label} #{num}", forma_pag, order_id)
        criar_notif(sb, tid, "gerente", "pagamento_ok",
                    f"💰 Pago — {mesa_label} #{num}", forma_pag, order_id)

    result = sb.table("orders").select("*").eq("id", order_id).maybe_single().execute()
    return success(result.data, f"Pedido: {status}")


# ── CANCELAR PEDIDO (rota dedicada) ──────────────────────────────────────────

@orders_bp.post("/<order_id>/cancelar")
@require_auth
def cancelar_pedido(order_id):
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}
    motivo = body.get("motivo") or "Pedido cancelado pelo operador"

    order = sb.table("orders") \
        .select("id, table_id, total, status, numero_pedido, is_delivery, delivery_nome, tables(numero)") \
        .eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()
    if not order.data:
        return error("Pedido não encontrado", 404)

    d    = order.data
    prev = d.get("status")
    if prev in ("fechado", "cancelado"):
        return error(f"Pedido já {prev}. Não é possível cancelar.")

    num  = d.get("numero_pedido", "?")
    mesa = d.get("tables") or {}
    mesa_label = f"Mesa {mesa.get('numero')}" if mesa.get("numero") else f"Pedido #{num}"

    sb.table("orders").update({"status": "cancelado"}).eq("id", order_id).eq("tenant_id", tid).execute()

    if d.get("table_id"):
        sb.table("tables").update({"status": "livre"}) \
            .eq("id", d["table_id"]).eq("tenant_id", tid).execute()

    msg_cancel = f"{mesa_label} #{num} — {motivo}"
    for papel in ["garcom", "cozinheiro", "caixa", "gerente"]:
        criar_notif(sb, tid, papel, "pedido_cancelado",
                    f"❌ Pedido cancelado — {mesa_label} #{num}", msg_cancel, order_id)
    if d.get("is_delivery"):
        criar_notif(sb, tid, "entregador", "pedido_cancelado",
                    f"❌ Delivery cancelado #{num}", msg_cancel, order_id)

    return success(message=f"Pedido #{num} cancelado. Todos notificados.")


# ── SOLICITAR PAGAMENTO ────────────────────────────────────────────────────────

@orders_bp.post("/<order_id>/solicitar-pagamento")
@require_auth
def solicitar_pagamento(order_id):
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}

    order = sb.table("orders") \
        .select("id, total, numero_pedido, tables(numero)") \
        .eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()
    if not order.data:
        return error("Pedido não encontrado", 404)

    d     = order.data
    mesa  = d.get("tables") or {}
    num   = d.get("numero_pedido", "?")
    forma = body.get("forma_pagamento", "")
    total = float(d.get("total") or 0)

    msg = f"Mesa {mesa.get('numero', '?')} — Total: R$ {total:.2f}"
    if forma:
        msg += f" — Forma: {forma}"

    criar_notif(sb, tid, "caixa", "pagamento_solicitado",
                f"💳 Pagamento solicitado — Mesa {mesa.get('numero', '?')} #{num}", msg, order_id)

    return success(message="Caixa notificado!")


# ── EDITAR INFORMAÇÕES DO DELIVERY ────────────────────────────────────────────

@orders_bp.patch("/<order_id>/delivery-info")
@require_auth
def edit_delivery_info(order_id):
    """Permite editar dados do delivery após criação (endereço, cliente, obs)."""
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}

    order = sb.table("orders").select("id, status, is_delivery") \
        .eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()
    if not order.data:
        return error("Pedido não encontrado", 404)
    if not order.data.get("is_delivery"):
        return error("Este pedido não é um delivery")
    if order.data["status"] in ("fechado", "cancelado", "entregue"):
        return error("Não é possível editar pedido já finalizado")

    allowed = ["delivery_nome", "delivery_tel", "delivery_end", "delivery_compl",
               "delivery_bairro", "delivery_obs", "delivery_pago", "forma_pagamento"]
    update_data = {k: v for k, v in body.items() if k in allowed}

    if not update_data:
        return error("Nenhum campo válido para atualizar")

    sb.table("orders").update(update_data).eq("id", order_id).eq("tenant_id", tid).execute()
    return success(message="Dados do delivery atualizados")


@orders_bp.patch("/<order_id>/assign-client")
@require_auth
def assign_client(order_id):
    """Associa um cliente cadastrado a um pedido ativo (necessário para fiado)."""
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}
    client_id = body.get("client_id")

    if not client_id:
        return error("client_id é obrigatório", 400)

    order = sb.table("orders").select("id, status") \
        .eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()
    if not order.data:
        return error("Pedido não encontrado", 404)
    if order.data["status"] in ("fechado", "cancelado"):
        return error("Não é possível editar pedido já finalizado", 400)

    # Verifica se o cliente pertence ao mesmo tenant
    client = sb.table("clients").select("id, nome") \
        .eq("id", client_id).eq("tenant_id", tid).maybe_single().execute()
    if not client.data:
        return error("Cliente não encontrado", 404)

    sb.table("orders").update({"client_id": client_id}) \
        .eq("id", order_id).eq("tenant_id", tid).execute()

    return success({"client": client.data}, "Cliente associado ao pedido")


# ── EXCLUIR PEDIDO ────────────────────────────────────────────────────────────

@orders_bp.delete("/<order_id>")
@require_auth
@require_papel("dono", "gerente")
def delete_order(order_id):
    sb  = get_supabase_admin()
    tid = request.tenant_id
    order = sb.table("orders").select("table_id").eq("id", order_id).eq("tenant_id", tid).maybe_single().execute()
    if order.data and order.data.get("table_id"):
        sb.table("tables").update({"status": "livre"}) \
            .eq("id", order.data["table_id"]).eq("tenant_id", tid).execute()
    sb.table("orders").delete().eq("id", order_id).eq("tenant_id", tid).execute()
    return success(message="Pedido removido")
