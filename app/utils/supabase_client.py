from supabase import create_client, Client
from flask import current_app
import os

_client: Client | None = None
_admin_client: Client | None = None


def get_supabase() -> Client:
    """Cliente anon — usado nas rotas autenticadas (RLS ativo)."""
    global _client
    if _client is None:
        try:
            url = current_app.config.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
            key = current_app.config.get("SUPABASE_KEY") or os.getenv("SUPABASE_KEY")
            
            if not url or not key:
                raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
            
            _client = create_client(url, key)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize Supabase client: {str(e)}")
    return _client


def get_supabase_admin() -> Client:
    """Cliente service_role — bypassa RLS. Usar APENAS em operações admin
    como criar tenant, registrar usuário, etc."""
    global _admin_client
    if _admin_client is None:
        try:
            url = current_app.config.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
            service_key = current_app.config.get("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
            
            if not url or not service_key:
                raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
            
            _admin_client = create_client(url, service_key)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize Supabase admin client: {str(e)}")
    return _admin_client


class _LazySupabaseClient:
    """Lazy-loading proxy para o cliente Supabase anon.
    Permite usar supabase.table(), supabase.auth, etc sem inicialização manual."""
    
    def __getattr__(self, name):
        try:
            client = get_supabase()
            return getattr(client, name)
        except Exception as e:
            raise RuntimeError(f"Supabase client error: {str(e)}")


# Exporta um objeto que age como um cliente Supabase
supabase = _LazySupabaseClient()
