"""Vendas de Veículos — módulo completo. v10

v10 changes:
- Juros configuráveis: Price (composto) ou Simples, por % ao mês
- Campo tipo_venda: nova | antiga
- Geração de parcelas com juros corretos
- PDF: validação de campos obrigatórios + logo da empresa
- PDF: mensagem clara indicando o que falta preencher
"""
from flask import Blueprint, request, send_file, jsonify
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error
from datetime import date, timedelta
import io
import math

sales_bp = Blueprint("sales", __name__)


def _parcela_vencimento(data_base, mes):
    try:
        from dateutil.relativedelta import relativedelta
        return data_base + relativedelta(months=mes)
    except Exception:
        return data_base + timedelta(days=30 * mes)


def _calc_valor_parcela(saldo, n_parcelas, taxa_mensal, juros_tipo):
    """Retorna valor de cada parcela com juros.
    taxa_mensal: percentual (ex: 1.99 para 1,99%)
    juros_tipo: 'price' ou 'simples'
    """
    if saldo <= 0 or n_parcelas <= 0:
        return 0.0
    rate = taxa_mensal / 100.0
    if rate == 0 or n_parcelas == 1:
        return round(saldo / n_parcelas, 2)
    if juros_tipo == "price":
        # Tabela Price: PMT = PV * [r*(1+r)^n] / [(1+r)^n - 1]
        fator = math.pow(1 + rate, n_parcelas)
        return round(saldo * rate * fator / (fator - 1), 2)
    else:
        # Juros simples: PMT = PV*(1 + r*n) / n
        return round(saldo * (1 + rate * n_parcelas) / n_parcelas, 2)


def _gerar_parcelas(sb, tenant_id, sale_id, valor_financiado, n_parcelas, data_base,
                    desc_veh, client_name, taxa_mensal=0.0, juros_tipo="price"):
    if n_parcelas <= 0 or valor_financiado <= 0:
        return
    valor_parcela = _calc_valor_parcela(valor_financiado, n_parcelas, taxa_mensal, juros_tipo)
    total = valor_parcela * n_parcelas
    # Ajuste de arredondamento na última parcela
    ajuste = round(valor_financiado * (1 + (taxa_mensal / 100) * (1 if juros_tipo == "simples" else 0)) - valor_parcela * (n_parcelas - 1), 2) \
             if juros_tipo == "simples" else 0.0

    parcelas = []
    for i in range(1, n_parcelas + 1):
        v = valor_parcela
        if i == n_parcelas and juros_tipo == "simples":
            # ajusta última parcela para fechar exatamente o total
            v = round(total - valor_parcela * (n_parcelas - 1), 2)
        parcelas.append({
            "tenant_id": tenant_id, "tipo": "receita",
            "descricao": f"Parcela {i}/{n_parcelas} — {desc_veh} ({client_name})",
            "valor": v,
            "data_vencimento": str(_parcela_vencimento(data_base, i)),
            "pago": False, "categoria": "Venda Veículo",
            "referencia_id": sale_id, "referencia_tipo": "sale",
        })
    sb.table("transactions").insert(parcelas).execute()


# ─────────────────────────────────────────────────────────────────────────────
@sales_bp.get("/")
@require_auth
def list_sales():
    tid = request.tenant_id
    status = request.args.get("status")
    limit  = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    q = get_supabase_admin().table("sales") \
        .select("*, vehicles(marca, modelo, ano_fab, ano_mod, placa, cor, tipo), clients(nome, cpf_cnpj, telefone)") \
        .eq("tenant_id", tid).order("created_at", desc=True).limit(limit).offset(offset)
    if status:
        q = q.eq("status", status)
    return success(q.execute().data)


@sales_bp.get("/parcelas")
@require_auth
def list_parcelas():
    tid  = request.tenant_id
    sb   = get_supabase_admin()
    pago    = request.args.get("pago")
    vencido = request.args.get("vencido")
    limit   = min(int(request.args.get("limit", 100)), 200)
    q = sb.table("transactions").select("*").eq("tenant_id", tid) \
        .eq("referencia_tipo", "sale").order("data_vencimento")
    if pago == "true":  q = q.eq("pago", True)
    elif pago == "false": q = q.eq("pago", False)
    if vencido == "true": q = q.lt("data_vencimento", str(date.today())).eq("pago", False)
    return success(q.limit(limit).execute().data)


@sales_bp.patch("/parcelas/<parcela_id>/pagar")
@require_auth
def pagar_parcela(parcela_id):
    sb  = get_supabase_admin()
    tid = request.tenant_id
    body = request.get_json() or {}
    forma = body.get("forma_pagamento") or "Dinheiro"
    parc = sb.table("transactions").select("id, pago, tenant_id") \
        .eq("id", parcela_id).eq("tenant_id", tid).maybe_single().execute()
    if not parc.data:          return error("Parcela não encontrada", 404)
    if parc.data.get("pago"):  return error("Parcela já está paga")
    res = sb.table("transactions").update({
        "pago": True,
        "data_pagamento": str(date.today()),
        "forma_pagamento": forma,
    }).eq("id", parcela_id).execute()
    return success(res.data[0] if res.data else {}, "Parcela paga!")


@sales_bp.get("/<sale_id>")
@require_auth
def get_sale(sale_id):
    tid = request.tenant_id
    sb  = get_supabase_admin()
    sale = sb.table("sales").select("*, vehicles(*), clients(*)") \
        .eq("id", sale_id).eq("tenant_id", tid).maybe_single().execute()
    if not sale.data:
        return error("Venda não encontrada", 404)
    parcelas = sb.table("transactions").select("*") \
        .eq("referencia_id", sale_id).eq("referencia_tipo", "sale") \
        .order("data_vencimento").execute()
    result = dict(sale.data)
    result["parcelas"] = parcelas.data or []
    return success(result)


@sales_bp.post("/")
@require_auth
def create_sale():
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}

    if not body.get("vehicle_id"): return error("Veículo é obrigatório")
    if not body.get("client_id"):  return error("Cliente é obrigatório")
    if not body.get("valor_venda"): return error("Valor de venda é obrigatório")

    veh = sb.table("vehicles").select("*") \
        .eq("id", body["vehicle_id"]).eq("tenant_id", tid).maybe_single().execute()
    if not veh.data:                         return error("Veículo não encontrado")
    if veh.data.get("status") == "vendido":  return error("Veículo já foi vendido")

    cli = sb.table("clients").select("*") \
        .eq("id", body["client_id"]).eq("tenant_id", tid).maybe_single().execute()
    if not cli.data: return error("Cliente não encontrado")

    valor_venda    = float(body["valor_venda"])
    valor_entrada  = float(body.get("valor_entrada") or 0)
    n_parcelas     = int(body.get("parcelas") or 1)
    financiamento  = bool(body.get("financiamento", False))
    taxa_mensal    = float(body.get("juros_percentual") or 0)
    juros_tipo     = body.get("juros_tipo") or "price"
    tipo_venda     = body.get("tipo_venda") or "nova"
    data_venda     = body.get("data_venda") or str(date.today())

    sale_payload = {
        "tenant_id":        tid,
        "vehicle_id":       body["vehicle_id"],
        "client_id":        body["client_id"],
        "vendedor_id":      getattr(request, "tenant_user_id", None) or getattr(request, "user_id", None),
        "valor_venda":      valor_venda,
        "valor_entrada":    valor_entrada,
        "financiamento":    financiamento,
        "financiadora":     body.get("financiadora") or None,
        "parcelas":         n_parcelas,
        "juros_percentual": taxa_mensal,
        "juros_tipo":       juros_tipo,
        "tipo_venda":       tipo_venda,
        "obs":              body.get("obs") or None,
        "status":           "em_andamento",
        "data_venda":       data_venda,
        "forma_entrada":    body.get("forma_entrada") or "Dinheiro",
    }

    sale_res = sb.table("sales").insert(sale_payload).execute()
    if not sale_res.data:
        return error("Erro ao criar venda", 500)
    sale = sale_res.data[0]

    # Marca veículo como vendido
    sb.table("vehicles").update({"status": "vendido"}).eq("id", body["vehicle_id"]).execute()

    desc_veh = f"{veh.data['marca']} {veh.data['modelo']}"

    # Grava entrada como transação paga (aparece em contas a receber com saldo positivo)
    if valor_entrada > 0:
        sb.table("transactions").insert({
            "tenant_id":        tid,
            "tipo":             "receita",
            "descricao":        f"Entrada — {desc_veh} ({cli.data['nome']})",
            "valor":            valor_entrada,
            "data_vencimento":  data_venda,
            "data_pagamento":   data_venda,
            "pago":             True,
            "categoria":        "Venda Veículo",
            "forma_pagamento":  body.get("forma_entrada") or "Dinheiro",
            "referencia_id":    sale["id"],
            "referencia_tipo":  "sale_entrada",
        }).execute()

    # Gera parcelas com juros
    valor_financiado = valor_venda - valor_entrada
    if valor_financiado > 0 and n_parcelas > 0:
        try:
            from datetime import datetime
            data_base = datetime.strptime(data_venda, "%Y-%m-%d").date()
        except Exception:
            data_base = date.today()
        _gerar_parcelas(
            sb, tid, sale["id"], valor_financiado, n_parcelas,
            data_base, desc_veh, cli.data["nome"],
            taxa_mensal=taxa_mensal, juros_tipo=juros_tipo,
        )

    return success(sale, "Venda registrada com sucesso!", 201)


@sales_bp.patch("/<sale_id>/status")
@require_auth
@require_papel("dono", "gerente")
def update_sale_status(sale_id):
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}
    status = body.get("status")
    if status not in ["em_andamento", "concluida", "cancelada"]:
        return error("Status inválido")
    sale = sb.table("sales").select("vehicle_id") \
        .eq("id", sale_id).eq("tenant_id", tid).maybe_single().execute()
    if not sale.data:
        return error("Venda não encontrada", 404)
    sb.table("sales").update({"status": status}).eq("id", sale_id).execute()
    if status == "cancelada" and sale.data.get("vehicle_id"):
        sb.table("vehicles").update({"status": "disponivel"}) \
            .eq("id", sale.data["vehicle_id"]).execute()
    return success(message=f"Venda {status}")


# ─────────────────────────────────────────────────────────────────────────────
# Contrato PDF
# ─────────────────────────────────────────────────────────────────────────────

def _check_pdf_fields(tenant):
    """Retorna lista de campos obrigatórios que estão faltando para gerar o contrato."""
    faltando = []
    if not (tenant.get("nome") or "").strip():
        faltando.append("Nome do negócio (Configurações > Meu Negócio)")
    if not (tenant.get("cnpj") or "").strip():
        faltando.append("CNPJ / CPF (Configurações > Meu Negócio)")
    if not (tenant.get("cidade") or "").strip():
        faltando.append("Cidade (Configurações > Meu Negócio)")
    return faltando


@sales_bp.get("/<sale_id>/contrato")
@require_auth
def gerar_contrato(sale_id):
    sb  = get_supabase_admin()
    tid = request.tenant_id

    sale = sb.table("sales").select("*, vehicles(*), clients(*)") \
        .eq("id", sale_id).eq("tenant_id", tid).maybe_single().execute()
    if not sale.data:
        return error("Venda não encontrada", 404)

    tenant = sb.table("tenants").select("*").eq("id", tid).maybe_single().execute()
    ten    = tenant.data or {}

    # Validação dos campos do estabelecimento
    faltando = _check_pdf_fields(ten)
    if faltando:
        msg = ("Não é possível gerar o contrato. Preencha os seguintes campos primeiro:\n• "
               + "\n• ".join(faltando))
        return jsonify({"success": False, "error": msg}), 400

    parcelas = sb.table("transactions").select("*") \
        .eq("referencia_id", sale_id).eq("referencia_tipo", "sale") \
        .order("data_vencimento").execute()

    try:
        pdf_bytes = _build_pdf(sale.data, ten, parcelas.data or [])
    except Exception as e:
        return error(f"Erro ao gerar PDF: {str(e)}", 500)

    buf = io.BytesIO(pdf_bytes)
    buf.seek(0)
    veh = sale.data.get("vehicles") or {}
    return send_file(
        buf, mimetype="application/pdf", as_attachment=True,
        download_name=f"contrato_{veh.get('placa', sale_id[:8])}.pdf",
    )



def _build_pdf(sale, tenant, parcelas):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm, mm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, HRFlowable, KeepTogether)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    import urllib.request

    W, H = A4  # 595 x 842 pts
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.8*cm, rightMargin=1.8*cm,
        topMargin=1.8*cm, bottomMargin=1.8*cm,
    )

    # ── Paleta ──────────────────────────────────────────────────────────────
    RED     = colors.HexColor("#C0162A")
    RED_LITE= colors.HexColor("#F5E6E8")
    DARK    = colors.HexColor("#1A1A1A")
    MID     = colors.HexColor("#444444")
    LGRAY   = colors.HexColor("#F7F7F7")
    MGRAY   = colors.HexColor("#DDDDDD")
    WHITE   = colors.white
    GREEN   = colors.HexColor("#166534")
    GREEN_BG= colors.HexColor("#DCFCE7")
    ORANGE  = colors.HexColor("#92400E")

    ss = getSampleStyleSheet()
    def sty(name, **kw):
        return ParagraphStyle(name, parent=ss["Normal"], **kw)

    # ── Helpers ──────────────────────────────────────────────────────────────
    def fmt_m(v):
        try:
            val = float(v or 0)
            return "R$ {:,.2f}".format(val).replace(",","X").replace(".",",").replace("X",".")
        except Exception:
            return "R$ 0,00"

    def fmt_d(x):
        if not x: return "—"
        try:
            from datetime import datetime
            return datetime.strptime(str(x)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            return str(x)[:10]

    def section_title(txt):
        return Paragraph(
            txt,
            sty("sec", fontSize=8, fontName="Helvetica-Bold", textColor=RED,
                spaceBefore=10, spaceAfter=4,
                borderPad=3, leading=12)
        )

    def tbl_style_header(color=RED):
        return TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), color),
            ("TEXTCOLOR",     (0,0), (-1,0), WHITE),
            ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",      (0,0), (-1,0), 8),
            ("FONTSIZE",      (0,1), (-1,-1), 8),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [LGRAY, WHITE]),
            ("GRID",          (0,0), (-1,-1), 0.3, MGRAY),
            ("PADDING",       (0,0), (-1,-1), 4),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ("LEADING",       (0,0), (-1,-1), 10),
        ])

    veh = sale.get("vehicles") or {}
    cli = sale.get("clients")  or {}
    ten = tenant

    ano_info = str(veh.get("ano_fab", ""))
    if veh.get("ano_mod") and str(veh["ano_mod"]) != str(veh.get("ano_fab","")):
        ano_info += f"/{veh['ano_mod']}"

    INNER_W = W - 1.8*cm*2  # usable width

    story = []

    # ══════════════════════════════════════════════════════════════
    # CABEÇALHO: Logo + Nome + Info
    # ══════════════════════════════════════════════════════════════
    logo_cell = ""
    logo_url  = ten.get("logo_url") or ""
    logo_img  = None
    if logo_url:
        try:
            with urllib.request.urlopen(logo_url, timeout=6) as resp:
                logo_bytes = resp.read()
            from reportlab.platypus import Image as RLImage
            from PIL import Image as PILImage
            pil_buf = io.BytesIO(logo_bytes)
            pil_img = PILImage.open(pil_buf)
            orig_w, orig_h = pil_img.size
            # Mantém proporção — cabe em 2.8cm x 2.2cm
            max_w, max_h = 2.8*cm, 2.2*cm
            ratio = min(max_w / orig_w, max_h / orig_h)
            logo_w = orig_w * ratio
            logo_h = orig_h * ratio
            logo_buf = io.BytesIO(logo_bytes)
            logo_img = RLImage(logo_buf, width=logo_w, height=logo_h)
        except Exception:
            logo_img = None

    nome_empresa = (ten.get("nome") or "Empresa").upper()
    info_parts = []
    if ten.get("cnpj"):     info_parts.append(f"CNPJ: {ten['cnpj']}")
    if ten.get("endereco"): info_parts.append(ten["endereco"])
    if ten.get("cidade"):
        loc = ten["cidade"]
        if ten.get("estado"): loc += f" - {ten['estado']}"
        info_parts.append(loc)
    if ten.get("telefone"): info_parts.append(f"Tel: {ten['telefone']}")

    header_content = [
        Paragraph(nome_empresa, sty("hd", fontSize=16, fontName="Helvetica-Bold",
                                    textColor=RED, leading=18, spaceAfter=2)),
        Paragraph(" | ".join(info_parts),
                  sty("hi", fontSize=7.5, textColor=MID, leading=10)),
    ]

    if logo_img:
        hdr_data = [[logo_img, header_content]]
        hdr_tbl  = Table(hdr_data, colWidths=[2.4*cm, INNER_W - 2.4*cm])
        hdr_tbl.setStyle(TableStyle([
            ("VALIGN",  (0,0), (-1,-1), "MIDDLE"),
            ("PADDING", (0,0), (-1,-1), 0),
            ("ALIGN",   (0,0), (0,0),   "LEFT"),
        ]))
    else:
        hdr_tbl = Table([[header_content]], colWidths=[INNER_W])
        hdr_tbl.setStyle(TableStyle([
            ("PADDING", (0,0), (-1,-1), 0),
        ]))

    story.append(hdr_tbl)
    story.append(HRFlowable(width="100%", thickness=2, color=RED, spaceAfter=6, spaceBefore=4))

    # Título do contrato
    story.append(Paragraph(
        "CONTRATO PARTICULAR DE COMPRA E VENDA DE VEÍCULO",
        sty("ct", fontSize=11, fontName="Helvetica-Bold", textColor=DARK,
            alignment=TA_CENTER, spaceAfter=2)
    ))
    story.append(Paragraph(
        f"Data: {fmt_d(sale.get('data_venda') or sale.get('created_at'))}",
        sty("dt", fontSize=8, textColor=MID, alignment=TA_CENTER, spaceAfter=8)
    ))

    # ══════════════════════════════════════════════════════════════
    # BLOCO DUPLO: Partes + Veículo lado a lado
    # ══════════════════════════════════════════════════════════════
    half = (INNER_W - 0.4*cm) / 2

    # --- Partes ---
    partes_data = [
        ["VENDEDOR", "COMPRADOR"],
        [ten.get("nome","—"),          cli.get("nome","—")],
        [f"CNPJ: {ten.get('cnpj','—')}", f"CPF/CNPJ: {cli.get('cpf_cnpj','—')}"],
        [f"{ten.get('cidade','')} {ten.get('estado','')}", f"Tel: {cli.get('telefone','—')}"],
    ]
    tbl_partes = Table(partes_data, colWidths=[half/2, half/2])
    tbl_partes.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), RED),
        ("TEXTCOLOR",     (0,0), (-1,0), WHITE),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("SPAN",          (0,0), (-1,0)),
        ("ALIGN",         (0,0), (-1,0), "CENTER"),
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [LGRAY, WHITE]),
        ("GRID",          (0,0), (-1,-1), 0.3, MGRAY),
        ("PADDING",       (0,0), (-1,-1), 4),
    ]))

    # --- Veículo ---
    veh_data = [
        ["DO VEÍCULO", ""],
        ["Marca/Modelo", f"{veh.get('marca','')} {veh.get('modelo','')}"],
        ["Ano",          ano_info],
        ["Placa",        veh.get("placa","—")],
        ["Cor",          veh.get("cor","—")],
        ["KM",           f"{int(veh.get('km') or 0):,}".replace(",",".")],
        ["Câmbio",       (veh.get("cambio","—") or "—").capitalize()],
    ]
    tbl_veh = Table(veh_data, colWidths=[half*0.42, half*0.58])
    tbl_veh.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), DARK),
        ("TEXTCOLOR",     (0,0), (-1,0), WHITE),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("SPAN",          (0,0), (-1,0)),
        ("ALIGN",         (0,0), (-1,0), "CENTER"),
        ("FONTNAME",      (0,1), (0,-1), "Helvetica-Bold"),
        ("TEXTCOLOR",     (0,1), (0,-1), MID),
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [LGRAY, WHITE]),
        ("GRID",          (0,0), (-1,-1), 0.3, MGRAY),
        ("PADDING",       (0,0), (-1,-1), 4),
    ]))

    # Lado a lado
    duplo = Table([[tbl_partes, tbl_veh]], colWidths=[half, half],
                  hAlign="LEFT")
    duplo.setStyle(TableStyle([
        ("PADDING",  (0,0), (-1,-1), 0),
        ("VALIGN",   (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING",  (1,0), (1,0), 6),
    ]))
    story.append(KeepTogether([
        section_title("1. PARTES   |   2. VEÍCULO"),
        duplo,
    ]))

    # ══════════════════════════════════════════════════════════════
    # PAGAMENTO
    # ══════════════════════════════════════════════════════════════
    vv  = float(sale.get("valor_venda")   or 0)
    ve  = float(sale.get("valor_entrada") or 0)
    np_ = int(sale.get("parcelas") or 1)
    tx  = float(sale.get("juros_percentual") or 0)
    jt  = sale.get("juros_tipo") or "price"
    vf  = vv - ve
    vp  = _calc_valor_parcela(vf, np_, tx, jt) if vf > 0 and np_ > 0 else 0
    total_juros  = max(0, vp * np_ - vf)
    total_efetivo= vp * np_ + ve

    pag_rows = [["Descrição", "Valor"]]
    pag_rows.append(["Valor total à vista",                      fmt_m(vv)])
    pag_rows.append([f"Entrada ({sale.get('forma_entrada','Dinheiro')})", fmt_m(ve)])
    pag_rows.append(["Saldo financiado",                         fmt_m(vf)])
    if np_ > 1 and vf > 0:
        pag_rows.append([f"{np_}x de (parcelas)",                fmt_m(vp)])
        if tx > 0:
            pag_rows.append([f"Juros {tx}% a.m. ({jt.capitalize()})", fmt_m(total_juros)])
            pag_rows.append(["Total a pagar (c/ juros)",         fmt_m(total_efetivo)])
    else:
        pag_rows.append(["Pagamento",                            sale.get("forma_entrada","À vista")])

    tbl_pag = Table(pag_rows, colWidths=[INNER_W*0.72, INNER_W*0.28])
    ts_pag  = tbl_style_header(RED)
    ts_pag.add("ALIGN",  (1,0), (1,-1), "RIGHT")
    ts_pag.add("FONTNAME", (0, len(pag_rows)-1), (-1, len(pag_rows)-1), "Helvetica-Bold")
    ts_pag.add("BACKGROUND", (0, len(pag_rows)-1), (-1, len(pag_rows)-1), RED_LITE)
    ts_pag.add("TEXTCOLOR", (0, len(pag_rows)-1), (-1, len(pag_rows)-1), RED)
    tbl_pag.setStyle(ts_pag)

    story.append(KeepTogether([
        section_title("3. DO PAGAMENTO"),
        tbl_pag,
    ]))

    # ══════════════════════════════════════════════════════════════
    # TABELA DE PARCELAS — 2 colunas lado a lado
    # ══════════════════════════════════════════════════════════════
    if parcelas:
        story.append(section_title(f"4. TABELA DE PARCELAS  ({len(parcelas)} parcelas)"))

        # Cabeçalho de coluna reutilizável
        col_hdr = ["#", "Vencimento", "Valor", "Situação"]
        COL_W   = [0.6*cm, 2.4*cm, 2.4*cm, 2.2*cm]  # total ~7.6cm por bloco

        # Divide em duas metades
        mid  = math.ceil(len(parcelas) / 2)
        left_rows  = parcelas[:mid]
        right_rows = parcelas[mid:]

        def build_col(rows):
            data = [col_hdr]
            for i, p in enumerate(rows):
                situacao = "PAGO" if p.get("pago") else "Pendente"
                data.append([
                    str(i + 1 + (0 if rows is left_rows else mid)),
                    fmt_d(p.get("data_vencimento")),
                    fmt_m(p.get("valor")),
                    situacao,
                ])
            t = Table(data, colWidths=COL_W)
            ts = TableStyle([
                ("BACKGROUND",    (0,0), (-1,0), DARK),
                ("TEXTCOLOR",     (0,0), (-1,0), WHITE),
                ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTSIZE",      (0,0), (-1,-1), 7),
                ("ROWBACKGROUNDS",(0,1), (-1,-1), [LGRAY, WHITE]),
                ("GRID",          (0,0), (-1,-1), 0.3, MGRAY),
                ("PADDING",       (0,0), (-1,-1), 3),
                ("ALIGN",         (0,0), (0,-1),  "CENTER"),
                ("ALIGN",         (2,0), (2,-1),  "RIGHT"),
                ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ])
            # Destaque "PAGO" em verde
            for ri, p in enumerate(rows, 1):
                if p.get("pago"):
                    ts.add("TEXTCOLOR",   (3,ri), (3,ri), GREEN)
                    ts.add("BACKGROUND",  (0,ri), (-1,ri), GREEN_BG)
                    ts.add("FONTNAME",    (3,ri), (3,ri), "Helvetica-Bold")
            t.setStyle(ts)
            return t

        # Padding entre as duas colunas
        GAP = 0.5*cm
        tbl_left  = build_col(left_rows)
        tbl_right = build_col(right_rows)

        outer = Table(
            [[tbl_left, tbl_right]],
            colWidths=[sum(COL_W) + GAP*0.2, sum(COL_W)],
        )
        outer.setStyle(TableStyle([
            ("PADDING",      (0,0), (-1,-1), 0),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("RIGHTPADDING", (0,0), (0,0), GAP),
        ]))
        story.append(outer)

    # ══════════════════════════════════════════════════════════════
    # CLÁUSULAS — compactas
    # ══════════════════════════════════════════════════════════════
    clausulas = [
        "O VENDEDOR declara que o veículo é de sua legítima propriedade, livre de ônus, dívidas ou alienação fiduciária, salvo indicação expressa.",
        "O COMPRADOR declara conhecer o estado do veículo, tendo vistoriado e aprovado antes da assinatura.",
        "A transferência da documentação ocorrerá após a quitação integral do valor acordado.",
        "O não pagamento nas datas estipuladas sujeitará o COMPRADOR a multa de 2% + juros de mora de 1% ao mês.",
        "Foro eleito: comarca de domicílio do VENDEDOR.",
    ]
    cl_data = [[f"{i}. {c}"] for i, c in enumerate(clausulas, 1)]
    tbl_cl  = Table(cl_data, colWidths=[INNER_W])
    tbl_cl.setStyle(TableStyle([
        ("FONTSIZE",  (0,0), (-1,-1), 7.5),
        ("LEADING",   (0,0), (-1,-1), 11),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [LGRAY, WHITE]),
        ("GRID",      (0,0), (-1,-1), 0.3, MGRAY),
        ("PADDING",   (0,0), (-1,-1), 4),
        ("TEXTCOLOR", (0,0), (-1,-1), MID),
    ]))
    story.append(KeepTogether([
        section_title("5. CLÁUSULAS"),
        tbl_cl,
    ]))

    # ══════════════════════════════════════════════════════════════
    # ASSINATURAS
    # ══════════════════════════════════════════════════════════════
    sig_data = [
        ["___________________________", "___________________________"],
        [ten.get("nome",""), cli.get("nome","—")],
        ["VENDEDOR", "COMPRADOR"],
        [f"CNPJ: {ten.get('cnpj','—')}", f"CPF/CNPJ: {cli.get('cpf_cnpj','—')}"],
    ]
    tbl_sig = Table(sig_data, colWidths=[INNER_W/2, INNER_W/2])
    tbl_sig.setStyle(TableStyle([
        ("FONTSIZE",   (0,0), (-1,-1), 8),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("FONTNAME",   (0,1), (-1,2),  "Helvetica-Bold"),
        ("TEXTCOLOR",  (0,2), (-1,2),  RED),
        ("TEXTCOLOR",  (0,3), (-1,3),  MID),
        ("TOPPADDING", (0,0), (-1,0),  18),
        ("PADDING",    (0,1), (-1,-1), 3),
    ]))
    story.append(KeepTogether([
        HRFlowable(width="100%", thickness=0.5, color=MGRAY, spaceBefore=14, spaceAfter=10),
        tbl_sig,
    ]))

    doc.build(story)
    return buf.getvalue()
