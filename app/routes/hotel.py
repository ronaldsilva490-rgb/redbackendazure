"""
MÓDULO DE HOTELARIA E HOSPEDAGEM (SaaS)
"""
from flask import Blueprint, request, jsonify
from datetime import datetime
from uuid import UUID

from ..utils.auth_middleware import require_auth
from ..utils.supabase_client import get_supabase_admin

hotel_bp = Blueprint("hotel", __name__)

@hotel_bp.get("/dashboard")
@require_auth
def get_hotel_dashboard():
    tenant_id = request.tenant_id
    sb = get_supabase_admin()

    try:
        hoje_texto = datetime.now().strftime("%Y-%m-%d")

        r_acomodacoes = sb.table("acomodacoes").select("id, status").eq("tenant_id", tenant_id).execute()
        acom_list = r_acomodacoes.data or []
        total_quartos = len(acom_list)
        ocupados = len([q for q in acom_list if q.get("status") == "ocupado"])
        
        ocupacao_pct = 0
        if total_quartos > 0:
            ocupacao_pct = int((ocupados / total_quartos) * 100)

        r_reservas = sb.table("reservas").select("id, status, valor_total, data_checkin, data_checkout").eq("tenant_id", tenant_id).execute()
        res_list = r_reservas.data or []

        checkins_hoje = 0
        checkouts_hoje = 0
        reservas_ativas = 0
        receita_estadia = 0.0

        for r in res_list:
            status = r.get("status")
            if status in ["em_curso", "agendada"]:
                reservas_ativas += 1
            if status == "em_curso":
                receita_estadia += float(r.get("valor_total", 0))
            data_in = r.get("data_checkin") or ""
            data_out = r.get("data_checkout") or ""
            if hoje_texto in data_in and status == "agendada":
                checkins_hoje += 1
            if hoje_texto in data_out and status == "em_curso":
                checkouts_hoje += 1

        return jsonify({
            "status": "success",
            "data": {
                "ocupacao_pct": ocupacao_pct,
                "checkins_hoje": checkins_hoje,
                "checkouts_hoje": checkouts_hoje,
                "reservas_ativas": reservas_ativas,
                "receita_hospedes": receita_estadia
            }
        }), 200
    except Exception as e:
        print(f"[HOTEL DASHBOARD ERROR] {e}")
        return jsonify({"error": str(e)}), 500

@hotel_bp.get("/acomodacoes")
@require_auth
def list_acomodacoes():
    tenant_id = request.tenant_id
    sb = get_supabase_admin()
    try:
        r = sb.table("acomodacoes").select("*").eq("tenant_id", tenant_id).execute()
        return jsonify({"status": "success", "data": r.data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@hotel_bp.post("/acomodacoes")
@require_auth
def create_acomodacao():
    tenant_id = request.tenant_id
    if request.papel not in ["dono", "gerente"]:
        return jsonify({"error": "Acesso negado."}), 403

    sb = get_supabase_admin()
    body = request.json or {}

    payload = {
        "tenant_id": tenant_id,
        "numero": str(body.get("numero", "")),
        "tipo": body.get("tipo", "padrao"),
        "capacidade": int(body.get("capacidade", 2)),
        "diaria_padrao": float(body.get("diaria_padrao", 0.0)),
        "status": body.get("status", "livre"),
        "descricao": body.get("descricao", "")
    }

    try:
        r = sb.table("acomodacoes").insert(payload).execute()
        return jsonify({"status": "success", "data": r.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@hotel_bp.get("/reservas")
@require_auth
def list_reservas():
    tenant_id = request.tenant_id
    sb = get_supabase_admin()
    try:
        r = sb.table("reservas").select("*, clients(nome, telefone), acomodacoes(numero, tipo)").eq("tenant_id", tenant_id).execute()
        return jsonify({"status": "success", "data": r.data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@hotel_bp.post("/reservas")
@require_auth
def create_reserva():
    tenant_id = request.tenant_id
    sb = get_supabase_admin()
    body = request.json or {}

    try:
        payload = {
            "tenant_id": tenant_id,
            "client_id": body.get("client_id"),
            "acomodacao_id": body.get("acomodacao_id"),
            "data_checkin": body.get("data_checkin"),
            "data_checkout": body.get("data_checkout") or None,
            "status": body.get("status", "agendada"),
            "valor_total": float(body.get("valor_total", 0.0)),
            "observacoes": body.get("observacoes", "")
        }

        r = sb.table("reservas").insert(payload).execute()
        nova_reserva = r.data[0]

        if payload["status"] == "em_curso":
            sb.table("acomodacoes").update({"status": "ocupado"}).eq("id", payload["acomodacao_id"]).execute()

        return jsonify({"status": "success", "data": nova_reserva}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400
