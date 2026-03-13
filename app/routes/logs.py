"""
logs.py — Endpoint de logs do sistema para superadmin.
Lê logs do Supabase (tabela system_logs) com filtros de nível, serviço e busca.
Recebe logs do frontend, backend e database.

Para criar a tabela no Supabase, execute este SQL:
  CREATE TABLE IF NOT EXISTS system_logs (
    id          BIGSERIAL PRIMARY KEY,
    level       TEXT NOT NULL DEFAULT 'info',   -- error | warning | info | debug
    service     TEXT NOT NULL DEFAULT 'backend',
    message     TEXT NOT NULL,
    details     JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
  );
  CREATE INDEX IF NOT EXISTS idx_system_logs_level   ON system_logs(level);
  CREATE INDEX IF NOT EXISTS idx_system_logs_service ON system_logs(service);
  CREATE INDEX IF NOT EXISTS idx_system_logs_created ON system_logs(created_at DESC);
"""
from flask import Blueprint, request, jsonify
from functools import wraps
import os
from ..utils.supabase_client import get_supabase_admin

logs_bp = Blueprint('logs', __name__)

# ─── Auth guard ───────────────────────────────────────────
def require_superadmin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token    = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        sa_token = os.getenv("SUPERADMIN_SECRET", "")
        if sa_token and token == sa_token:
            return f(*args, **kwargs)
        try:
            sb        = get_supabase_admin()
            user_resp = sb.auth.get_user(token)
            if not user_resp or not user_resp.user:
                return jsonify({"error": "Nao autorizado"}), 401
            email = user_resp.user.email or ""
            SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "")
            if SUPERADMIN_EMAIL and email != SUPERADMIN_EMAIL:
                return jsonify({"error": "Acesso negado"}), 403
            return f(*args, **kwargs)
        except Exception as e:
            return jsonify({"error": str(e)}), 401
    return decorated


@logs_bp.get('/logs')
@require_superadmin
def get_logs():
    """Lista logs com filtros opcionais de nível, serviço, busca e data."""
    level   = request.args.get('level')
    service = request.args.get('service')
    search  = request.args.get('search', '').strip()
    limit   = min(int(request.args.get('limit', 200)), 500)
    offset  = int(request.args.get('offset', 0))

    try:
        sb = get_supabase_admin()
        q  = sb.table('system_logs').select('*').order('created_at', desc=True)

        # Filtros
        if level and level != 'todos':
            q = q.eq('level', level)
        if service and service != 'todos':
            q = q.eq('service', service)
        if search:
            q = q.ilike('message', f'%{search}%')

        # Paginação
        q = q.limit(limit).offset(offset)
        r = q.execute()
        
        # Renomeia created_at → timestamp para o frontend
        logs = []
        for row in (r.data or []):
            logs.append({
                'id':        row.get('id'),
                'level':     row.get('level', 'info'),
                'service':   row.get('service', 'backend'),
                'message':   row.get('message', ''),
                'details':   row.get('details'),
                'timestamp': row.get('created_at'),
            })
        
        # Total de logs (sem paginação)
        total_r = sb.table('system_logs').select('id', count='exact').execute()
        total = total_r.count if hasattr(total_r, 'count') else len(logs)
        
        return jsonify({
            'data': logs,
            'pagination': {
                'total': total,
                'limit': limit,
                'offset': offset,
            }
        })
    except Exception as e:
        return jsonify({
            'data': [],
            'warning': f'Erro ao carregar logs: {str(e)[:120]}'
        })


@logs_bp.post('/logs')
@require_superadmin
def create_log():
    """
    Insere um log manualmente (útil para frontend enviar logs, ou testes).
    
    Body:
        level: 'error', 'warning', 'info', 'debug'
        service: 'frontend', 'backend', 'database', etc
        message: string obrigatório
        details: object opcional (stack trace, request_id, etc)
    """
    body    = request.get_json() or {}
    level   = body.get('level', 'info').lower()
    service = body.get('service', 'frontend')
    message = body.get('message', '')
    details = body.get('details')

    # Validações
    if not message:
        return jsonify({'error': 'message é obrigatório'}), 400
    
    if level not in ['error', 'warning', 'info', 'debug']:
        level = 'info'
    
    if service not in ['frontend', 'backend', 'database', 'auth', 'orders', 'products', 'sales', 'finance', 'workshop']:
        service = 'frontend'

    try:
        sb = get_supabase_admin()
        r  = sb.table('system_logs').insert({
            'level': level,
            'service': service,
            'message': message,
            'details': details or {},
        }).execute()
        
        return jsonify({'ok': True, 'data': r.data[0] if r.data else {}})
    except Exception as e:
        return jsonify({'error': f'Erro ao inserir log: {str(e)[:120]}'}), 500


@logs_bp.post('/logs/batch')
@require_superadmin
def create_logs_batch():
    """
    Insere múltiplos logs de uma vez (batch).
    Útil para o frontend enviar vários logs simultaneamente.
    
    Body: array de logs
        [
            { level: 'error', service: 'frontend', message: '...', details: {...} },
            { level: 'info', service: 'frontend', message: '...', details: {...} },
        ]
    """
    logs_data = request.get_json() or []
    
    if not isinstance(logs_data, list) or len(logs_data) == 0:
        return jsonify({'error': 'Body deve ser um array de logs'}), 400
    
    if len(logs_data) > 100:
        return jsonify({'error': 'Máximo 100 logs por requisição'}), 400
    
    # Sanitiza e valida cada log
    sanitized = []
    for log in logs_data:
        level = log.get('level', 'info').lower()
        service = log.get('service', 'frontend')
        message = log.get('message', '')
        details = log.get('details')
        
        if not message:
            continue  # Pula logs sem mensagem
        
        if level not in ['error', 'warning', 'info', 'debug']:
            level = 'info'
        
        sanitized.append({
            'level': level,
            'service': service,
            'message': message,
            'details': details or {},
        })
    
    if not sanitized:
        return jsonify({'error': 'Nenhum log válido no batch'}), 400
    
    try:
        sb = get_supabase_admin()
        r = sb.table('system_logs').insert(sanitized).execute()
        return jsonify({'ok': True, 'inserted': len(r.data or [])})
    except Exception as e:
        return jsonify({'error': f'Erro ao inserir batch: {str(e)[:120]}'}), 500


@logs_bp.delete('/logs')
@require_superadmin
def clear_logs():
    """Limpa logs mais antigos que N dias (padrão 30)."""
    days = int(request.args.get('days', 30))
    
    if days < 1 or days > 365:
        return jsonify({'error': 'days deve estar entre 1 e 365'}), 400
    
    try:
        sb = get_supabase_admin()
        # Supabase não tem exec_sql direto, então usa delete com filtro
        r = sb.table('system_logs').delete().lt('created_at', f'now - {days} days').execute()
        return jsonify({'ok': True, 'message': f'Logs mais antigos que {days} dias removidos.'})
    except Exception as e:
        # Tenta alternativa
        try:
            sb = get_supabase_admin()
            sb.rpc('exec_sql', {
                'query': f"DELETE FROM system_logs WHERE created_at < NOW() - INTERVAL '{days} days'"
            }).execute()
            return jsonify({'ok': True, 'message': f'Logs mais antigos que {days} dias removidos.'})
        except Exception as e2:
            return jsonify({'error': f'Erro ao limpar logs: {str(e2)[:120]}'}), 500


@logs_bp.get('/logs/stats')
@require_superadmin
def logs_stats():
    """Retorna estatísticas dos logs (por nível e serviço)."""
    try:
        sb = get_supabase_admin()
        
        # Conta por nível
        levels = {}
        for lvl in ['error', 'warning', 'info', 'debug']:
            r = sb.table('system_logs').select('id', count='exact').eq('level', lvl).execute()
            levels[lvl] = r.count if hasattr(r, 'count') else 0
        
        # Conta por serviço
        services = {}
        r = sb.table('system_logs').select('service', count='exact').execute()
        # Agrupa por serviço
        for log in (r.data or []):
            svc = log.get('service', 'unknown')
            services[svc] = services.get(svc, 0) + 1
        
        # Total
        total_r = sb.table('system_logs').select('id', count='exact').execute()
        total = total_r.count if hasattr(total_r, 'count') else 0
        
        return jsonify({
            'total': total,
            'by_level': levels,
            'by_service': services,
        })
    except Exception as e:
        return jsonify({'error': str(e)[:120]}), 500
