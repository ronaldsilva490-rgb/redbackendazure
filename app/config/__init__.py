import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    SECRET_KEY            = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    SUPABASE_URL          = os.getenv("SUPABASE_URL")
    SUPABASE_KEY          = os.getenv("SUPABASE_KEY")           # anon key
    SUPABASE_SERVICE_KEY  = os.getenv("SUPABASE_SERVICE_KEY")   # service_role key (admin)
    SUPABASE_JWT_SECRET   = os.getenv("SUPABASE_JWT_SECRET")
    OPENROUTER_KEY        = os.getenv("OPENROUTER_KEY")         # API Key para OpenRouter
    DEBUG                 = os.getenv("FLASK_DEBUG", "false").lower() == "true"
