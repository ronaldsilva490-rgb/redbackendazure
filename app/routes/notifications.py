"""
Notificações — sistema de alertas em tempo real por polling.
O frontend consulta a cada 5s. O backend insere automaticamente
quando pedidos mudam de status.
"""
from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error

notif_bp = Blueprint("notifications", __name__)


# ── Helper usado por outros módulos ──────────────────────────────────────────
def criar_notif(sb, tenant_id, para_papel, tipo, titulo, mensagem=None, order_id=None, para_user_id=None):
    """Cria notificação de forma segura (nunca quebra o fluxo principal)."""
    try:
        sb.table("notifications").insert({
            "tenant_id":    tenant_id,
            "para_papel":   para_papel,
            "para_user_id": para_user_id,
            "tipo":         tipo,
            "titulo":       titulo,
            "mensagem":     mensagem,
            "order_id":     order_id,
            "lida":         False,
        }).execute()
    except Exception:
        pass  # Notificação nunca pode quebrar o fluxo principal


# ── Endpoints ─────────────────────────────────────────────────────────────────

@notif_bp.get("/")
@require_auth
def list_notifs():
    """Retorna notificações do papel atual (últimas 60, não lidas primeiro)."""
    sb    = get_supabase_admin()
    tid   = request.tenant_id
    papel = request.papel

    res = sb.table("notifications") \
        .select("*, orders(id, status, numero_pedido, tables(numero), is_delivery)") \
        .eq("tenant_id", tid) \
        .eq("para_papel", papel) \
        .order("created_at", desc=True) \
        .limit(60) \
        .execute()

    items  = res.data or []
    unread = len([n for n in items if not n.get("lida")])
    return success({"items": items, "unread": unread})


@notif_bp.patch("/<notif_id>/read")
@require_auth
def mark_read(notif_id):
    """Marca uma notificação como lida."""
    sb = get_supabase_admin()
    sb.table("notifications").update({"lida": True}) \
        .eq("id", notif_id).eq("tenant_id", request.tenant_id).execute()
    return success(message="Marcada como lida")


@notif_bp.post("/read-all")
@require_auth
def read_all():
    """Marca todas as notificações do papel atual como lidas."""
    sb = get_supabase_admin()
    sb.table("notifications").update({"lida": True}) \
        .eq("tenant_id", request.tenant_id) \
        .eq("para_papel", request.papel).execute()
    return success(message="Todas marcadas como lidas")


@notif_bp.post("/chamar-garcom")
@require_auth
@require_papel("dono", "gerente", "caixa", "vendedor")
def chamar_garcom():
    """Caixa/gerente chama garçom — notificação urgente."""
    body = request.get_json() or {}
    sb   = get_supabase_admin()
    criar_notif(
        sb, request.tenant_id,
        para_papel="garcom",
        tipo="chamar_garcom",
        titulo="📢 Chamado no balcão",
        mensagem=body.get("mensagem") or "Dirija-se ao balcão.",
    )
    return success(message="Garçom chamado")


@notif_bp.post("/chamar-caixa")
@require_auth
@require_papel("dono", "gerente", "garcom")
def chamar_caixa():
    """Garçom solicita atenção do caixa."""
    body = request.get_json() or {}
    sb   = get_supabase_admin()
    criar_notif(
        sb, request.tenant_id,
        para_papel="caixa",
        tipo="chamar_caixa",
        titulo="🔔 Garçom solicitou atenção",
        mensagem=body.get("mensagem") or "Garçom precisa de atenção.",
    )
    return success(message="Caixa notificado")
