"""
caixa.py — Controle de Sessão do Caixa (Abertura/Fechamento de Expediente)

Fluxo real:
  1. Gerente/Dono abre o caixa pela manhã → informa fundo de troco
  2. Durante o dia: vendas acontecem normalmente
  3. Ao fechar: sistema gera resumo por forma de pagamento, registra no histórico

Tabela necessária no Supabase (rodar uma vez):
  CREATE TABLE IF NOT EXISTS caixa_sessions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id),
    aberto_por      uuid,
    fechado_por     uuid,
    aberto_at       timestamptz NOT NULL DEFAULT now(),
    fechado_at      timestamptz,
    fundo_troco     numeric DEFAULT 0,
    total_vendas    numeric DEFAULT 0,
    total_dinheiro  numeric DEFAULT 0,
    total_pix       numeric DEFAULT 0,
    total_cartao    numeric DEFAULT 0,
    total_fiado     numeric DEFAULT 0,
    total_outros    numeric DEFAULT 0,
    obs_abertura    text,
    obs_fechamento  text,
    status          text DEFAULT 'aberto',
    created_at      timestamptz DEFAULT now()
  );
  CREATE INDEX IF NOT EXISTS idx_caixa_sessions_tenant ON caixa_sessions(tenant_id, status);
"""
from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth
from ..utils.response import success, error
from datetime import datetime, timezone, date

caixa_bp = Blueprint("caixa", __name__)


def _get_sessao_ativa(sb, tenant_id):
    """Retorna a sessão ativa do caixa ou None."""
    try:
        r = sb.table("caixa_sessions") \
            .select("*") \
            .eq("tenant_id", tenant_id) \
            .eq("status", "aberto") \
            .order("aberto_at", desc=True) \
            .limit(1) \
            .execute()
        return (r.data or [None])[0]
    except Exception:
        return None


def _calcular_totais(sb, tenant_id, desde):
    """Calcula totais de vendas desde uma data/hora."""
    try:
        orders = sb.table("orders") \
            .select("total, forma_pagamento") \
            .eq("tenant_id", tenant_id) \
            .eq("status", "fechado") \
            .gte("created_at", desde) \
            .execute()
        rows = orders.data or []

        total_vendas   = sum(float(r.get("total") or 0) for r in rows)
        total_dinheiro = sum(float(r.get("total") or 0) for r in rows if (r.get("forma_pagamento") or "").lower() == "dinheiro")
        total_pix      = sum(float(r.get("total") or 0) for r in rows if (r.get("forma_pagamento") or "").lower() == "pix")
        total_fiado    = sum(float(r.get("total") or 0) for r in rows if (r.get("forma_pagamento") or "").lower() == "fiado")
        total_cartao   = sum(
            float(r.get("total") or 0) for r in rows
            if any(k in (r.get("forma_pagamento") or "").lower() for k in ["cartão", "cartao", "débito", "debito", "crédito", "credito"])
        )
        total_outros = total_vendas - total_dinheiro - total_pix - total_fiado - total_cartao

        return {
            "total_vendas":   round(total_vendas, 2),
            "total_dinheiro": round(total_dinheiro, 2),
            "total_pix":      round(total_pix, 2),
            "total_cartao":   round(total_cartao, 2),
            "total_fiado":    round(total_fiado, 2),
            "total_outros":   round(max(total_outros, 0), 2),
            "num_vendas":     len(rows),
        }
    except Exception:
        return {}


# ── STATUS: retorna sessão ativa ──────────────────────────────────────────────

@caixa_bp.get("/sessao")
@require_auth
def get_sessao():
    sb  = get_supabase_admin()
    tid = request.tenant_id
    sessao = _get_sessao_ativa(sb, tid)

    if not sessao:
        # Sem sessão ativa — retorna status fechado
        try:
            ultima = sb.table("caixa_sessions") \
                .select("*") \
                .eq("tenant_id", tid) \
                .order("fechado_at", desc=True) \
                .limit(1) \
                .execute()
            return success({
                "aberto": False,
                "sessao": None,
                "ultima_sessao": (ultima.data or [None])[0],
            })
        except Exception:
            return success({"aberto": False, "sessao": None, "ultima_sessao": None})

    # Sessão ativa: calcula totais em tempo real
    totais = _calcular_totais(sb, tid, sessao["aberto_at"])
    return success({
        "aberto": True,
        "sessao": {**sessao, **totais},
    })


# ── ABRIR CAIXA ───────────────────────────────────────────────────────────────

@caixa_bp.post("/abrir")
@require_auth
def abrir_caixa():
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}
    uid  = request.user_id

    # Verifica se já tem sessão aberta
    ativa = _get_sessao_ativa(sb, tid)
    if ativa:
        return error("Já existe uma sessão de caixa aberta. Feche-a primeiro.", 400)

    try:
        nova = sb.table("caixa_sessions").insert({
            "tenant_id":    tid,
            "aberto_por":   uid,
            "fundo_troco":  float(body.get("fundo_troco") or 0),
            "obs_abertura": body.get("obs") or None,
            "status":       "aberto",
            "aberto_at":    datetime.now(timezone.utc).isoformat(),
        }).execute()
        return success(nova.data[0], "Caixa aberto! Bom expediente. ✅", 201)
    except Exception as e:
        return error(f"Erro ao abrir caixa: {str(e)}", 500)


# ── FECHAR CAIXA ──────────────────────────────────────────────────────────────

@caixa_bp.post("/fechar")
@require_auth
def fechar_caixa():
    sb   = get_supabase_admin()
    tid  = request.tenant_id
    body = request.get_json() or {}
    uid  = request.user_id

    sessao = _get_sessao_ativa(sb, tid)
    if not sessao:
        return error("Nenhuma sessão de caixa aberta.", 400)

    # Calcula totais finais
    totais = _calcular_totais(sb, tid, sessao["aberto_at"])

    try:
        fechado_at = datetime.now(timezone.utc).isoformat()
        sb.table("caixa_sessions").update({
            "status":          "fechado",
            "fechado_por":     uid,
            "fechado_at":      fechado_at,
            "obs_fechamento":  body.get("obs") or None,
            **{k: totais.get(k, 0) for k in [
                "total_vendas", "total_dinheiro", "total_pix",
                "total_cartao", "total_fiado", "total_outros"
            ]},
        }).eq("id", sessao["id"]).execute()

        resultado = {
            **sessao,
            **totais,
            "fechado_at": fechado_at,
            "fundo_troco": sessao.get("fundo_troco", 0),
        }
        return success(resultado, "Caixa fechado! Até amanhã. 👋")
    except Exception as e:
        return error(f"Erro ao fechar caixa: {str(e)}", 500)


# ── HISTÓRICO DE SESSÕES ──────────────────────────────────────────────────────

@caixa_bp.get("/historico")
@require_auth
def historico():
    sb  = get_supabase_admin()
    tid = request.tenant_id
    try:
        r = sb.table("caixa_sessions") \
            .select("*") \
            .eq("tenant_id", tid) \
            .order("aberto_at", desc=True) \
            .limit(30) \
            .execute()
        return success(r.data or [])
    except Exception as e:
        return error(str(e), 500)
