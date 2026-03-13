"""
logger.py — Sistema centralizado de logging para backend, frontend e DB.
Registra automaticamente: requisições HTTP, erros, operações DB, eventos.
"""
import os, json, time, traceback
from datetime import datetime
from functools import wraps
from flask import request, g
from .supabase_client import get_supabase_admin

# Cache de conexão para evitar múltiplas instâncias
_sb_instance = None


def get_sb():
    global _sb_instance
    if not _sb_instance:
        _sb_instance = get_supabase_admin()
    return _sb_instance


def log_to_db(level='info', service='backend', message='', details=None):
    """
    Insere log diretamente no Supabase.
    
    Args:
        level: 'error', 'warning', 'info', 'debug'
        service: 'backend', 'frontend', 'database', 'auth', 'orders', etc
        message: mensagem do log
        details: dict com informações adicionais (request_id, stack, etc)
    """
    if not message:
        return
    
    try:
        sb = get_sb()
        sb.table('system_logs').insert({
            'level': level,
            'service': service,
            'message': message,
            'details': details or {},
        }).execute()
    except Exception as e:
        # Se falhar a inserção, printa no console mas não quebra
        print(f"[LOG_DB_ERROR] {level.upper()} | {service} | {message} | {str(e)[:100]}")


def log_request_middleware(app):
    """
    Middleware Flask que registra TODAS as requisições HTTP.
    Registra: método, rota, status, duração, IP, erros.
    """
    @app.before_request
    def before_req():
        g.start_time = time.perf_counter()
        g.request_id = f"{datetime.now().timestamp()}"
    
    @app.after_request
    def after_req(response):
        try:
            if not hasattr(g, 'start_time'):
                return response
            
            method = request.method
            path = request.path
            
            # ⚠️ SUPER IMPORTANTE: Pular logging de rotas de logs para evitar recursão infinita!
            # Se log for registrado aqui, vai criar uma requisição POST para /api/admin/logs
            # que vai criar outro log, etc. Então BLOQUEAMOS tudo que é /api/admin/logs
            if path.startswith('/api/admin/logs'):
                return response
            
            # Pula assets estáticos
            skip_static = ['/static/', '.js', '.css', '.png', '.jpg', '.svg', '.ico', '.woff']
            if any(skip in path for skip in skip_static):
                return response
            
            duration_ms = round((time.perf_counter() - g.start_time) * 1000)
            status = response.status_code
            ip = request.remote_addr or 'unknown'
            
            # Determina nível de log baseado no status
            if status >= 500:
                level = 'error'
            elif status >= 400:
                level = 'warning'
            else:
                level = 'info'
            
            # Filtra algumas rotas chatas (health checks)
            if path in ['/', '/health']:
                level = 'debug'
            
            # Monta mensagem
            message = f"{method} {path} → {status}"
            
            # Prepara detalhes
            details = {
                'method': method,
                'path': path,
                'status': status,
                'duration_ms': duration_ms,
                'ip': ip,
                'user_agent': request.headers.get('User-Agent', '')[:100],
            }
            
            # Se houver body na requisição
            if method in ['POST', 'PUT', 'PATCH']:
                try:
                    body = request.get_json() or {}
                    # Remove dados sensíveis
                    safe_body = {k: v if k not in ['password', 'token', 'secret'] else '***' 
                                 for k, v in body.items()}
                    details['body_keys'] = list(safe_body.keys())
                except:
                    pass
            
            # Log assíncrono (não bloqueia response)
            if level != 'debug':  # Não loga debug chamadas
                log_to_db(level=level, service='backend', message=message, details=details)
        
        except Exception as e:
            print(f"[ERROR] log_request_middleware falhou: {str(e)}")
        
        return response


def log_function(service='backend', level='info'):
    """
    Decorator para logar execução de funções.
    
    Uso:
        @log_function(service='orders')
        def criar_pedido():
            pass
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            func_name = f.__name__
            try:
                start = time.perf_counter()
                result = f(*args, **kwargs)
                duration = round((time.perf_counter() - start) * 1000)
                
                message = f"✓ {func_name} executado em {duration}ms"
                log_to_db(level='debug', service=service, message=message)
                
                return result
            except Exception as e:
                duration = round((time.perf_counter() - start) * 1000)
                message = f"✗ {func_name} falhou após {duration}ms"
                details = {
                    'error': str(e),
                    'traceback': traceback.format_exc()[:500],
                    'args': str(args)[:100],
                }
                log_to_db(level='error', service=service, message=message, details=details)
                raise
        
        return wrapper
    return decorator


def log_error(service='backend', message='', exc=None):
    """Log rápido de erros."""
    if exc:
        message = message or str(exc)
        details = {'error': str(exc), 'traceback': traceback.format_exc()[:500]}
    else:
        details = None
    
    log_to_db(level='error', service=service, message=message, details=details)


def log_info(service='backend', message='', details=None):
    """Log rápido de info."""
    log_to_db(level='info', service=service, message=message, details=details)


def log_warning(service='backend', message='', details=None):
    """Log rápido de warning."""
    log_to_db(level='warning', service=service, message=message, details=details)


def log_debug(service='backend', message='', details=None):
    """Log rápido de debug."""
    log_to_db(level='debug', service=service, message=message, details=details)
