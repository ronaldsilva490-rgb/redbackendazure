"""
superadmin.py — Super Admin com AI Agent integrado.
v4.0 — Migrado de HuggingFace para Fly.io + GitHub.
       Backend e frontend agora são dois repos no GitHub.
       AI Agent commita no GitHub → GitHub Action → fly deploy automático.
"""
import os, re, json, base64, subprocess, shutil, requests
from flask import Blueprint, request, jsonify, Response, stream_with_context
from ..utils.supabase_client import get_supabase_admin

superadmin_bp = Blueprint("superadmin", __name__)

SUPERADMIN_EMAIL  = os.getenv("SUPERADMIN_EMAIL", "")

# GitHub — frontend
GITHUB_TOKEN          = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO           = os.getenv("GITHUB_REPO", "")
GITHUB_REPO_ID        = os.getenv("GITHUB_REPO_ID", "")

# GitHub — backend (novo!)
GITHUB_BACKEND_REPO   = os.getenv("GITHUB_BACKEND_REPO", "")
GITHUB_BACKEND_REPO_ID= os.getenv("GITHUB_BACKEND_REPO_ID", "")

# Vercel
VERCEL_TOKEN      = os.getenv("VERCEL_TOKEN", "")
VERCEL_PROJECT_ID = os.getenv("VERCEL_PROJECT_ID", "")

# Fly.io
FLY_API_TOKEN = os.getenv("FLY_API_TOKEN", "")
FLY_APP_NAME  = os.getenv("FLY_APP_NAME", "redbackend")
FLY_URL       = os.getenv("FLY_URL", "https://redbackend.fly.dev")


# ─── Auth guard ───────────────────────────────────────────
def require_superadmin(f):
    from functools import wraps
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
            if SUPERADMIN_EMAIL and email != SUPERADMIN_EMAIL:
                return jsonify({"error": "Acesso negado"}), 403
        except Exception:
            return jsonify({"error": "Token invalido"}), 401
        return f(*args, **kwargs)
    return decorated


# ─── TENANTS ──────────────────────────────────────────────
@superadmin_bp.get("/tenants")
@require_superadmin
def list_tenants():
    sb   = get_supabase_admin()
    r    = sb.table("tenants").select("*, tenant_users(count)").execute()
    data = r.data or []
    for t in data:
        count = t.get("tenant_users", [])
        t["user_count"] = count[0]["count"] if count else 0
        t.pop("tenant_users", None)
    return jsonify({"data": data})


@superadmin_bp.patch("/tenants/<tenant_id>")
@require_superadmin
def update_tenant(tenant_id):
    body = request.get_json() or {}
    sb   = get_supabase_admin()
    sb.table("tenants").update(body).eq("id", tenant_id).execute()
    return jsonify({"ok": True})


# ─── SYSTEM STATUS ────────────────────────────────────────
@superadmin_bp.get("/status")
@require_superadmin
def system_status():
    import time
    results = {}

    # Backend — ping para health endpoint (local, rápido)
    try:
        t0 = time.perf_counter()
        r = requests.get(FLY_URL, timeout=8)
        results["backend"] = {"ok": r.status_code == 200, "label": "Backend Fly.io", "latency_ms": round((time.perf_counter()-t0)*1000)}
    except Exception as e:
        results["backend"] = {"ok": False, "label": "Backend Fly.io", "error": str(e)[:80]}

    # Supabase — ping simples
    try:
        t0 = time.perf_counter()
        sb = get_supabase_admin()
        sb.table("tenants").select("id").limit(1).execute()
        results["supabase"] = {"ok": True, "label": "Supabase DB", "latency_ms": round((time.perf_counter()-t0)*1000)}
    except Exception as e:
        results["supabase"] = {"ok": False, "label": "Supabase DB", "error": str(e)[:120]}

    # Vercel — ping direto na URL do frontend deployado
    try:
        t0 = time.perf_counter()
        # Tenta pingar a URL do frontend Vercel
        r = requests.head("https://redcomercialweb.vercel.app", timeout=8, allow_redirects=True)
        latency = round((time.perf_counter()-t0)*1000)
        results["vercel"] = {"ok": r.status_code < 400, "label": "Vercel Frontend", "latency_ms": latency}
    except Exception as e:
        results["vercel"] = {"ok": False, "label": "Vercel Frontend", "error": str(e)[:80]}

    # GitHub frontend — ping HEAD
    try:
        t0 = time.perf_counter()
        if GITHUB_TOKEN and GITHUB_REPO:
            r = requests.head(f"https://api.github.com/repos/{GITHUB_REPO}", headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}, timeout=8)
            results["github"] = {"ok": r.status_code in [200, 405], "label": "GitHub Frontend", "latency_ms": round((time.perf_counter()-t0)*1000)}
        else:
            results["github"] = {"ok": None, "label": "GitHub Frontend", "error": "GITHUB_REPO nao configurado"}
    except Exception as e:
        results["github"] = {"ok": False, "label": "GitHub Frontend", "error": str(e)[:80]}

    # GitHub backend — ping HEAD
    try:
        t0 = time.perf_counter()
        if GITHUB_TOKEN and GITHUB_BACKEND_REPO:
            r = requests.head(f"https://api.github.com/repos/{GITHUB_BACKEND_REPO}", headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}, timeout=8)
            results["github_backend"] = {"ok": r.status_code in [200, 405], "label": "GitHub Backend", "latency_ms": round((time.perf_counter()-t0)*1000)}
        else:
            results["github_backend"] = {"ok": None, "label": "GitHub Backend", "error": "GITHUB_BACKEND_REPO nao configurado"}
    except Exception as e:
        results["github_backend"] = {"ok": False, "label": "GitHub Backend", "error": str(e)[:80]}

    return jsonify({"data": results})


# ─── DB EXPLORER ──────────────────────────────────────────
ALLOWED_TABLES = [
    "tenants", "tenant_users", "vehicles", "clients",
    "orders", "order_items", "products", "stock_movements",
    "sales", "sale_items", "workshop_orders", "transactions",
    "bills", "tables", "notifications",
]


@superadmin_bp.get("/db/tables")
@require_superadmin
def db_tables():
    return jsonify({"data": ALLOWED_TABLES})


@superadmin_bp.get("/db/table/<table_name>")
@require_superadmin
def db_table_data(table_name):
    if table_name not in ALLOWED_TABLES:
        return jsonify({"error": "Tabela nao permitida"}), 400
    limit  = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    sb = get_supabase_admin()
    r  = sb.table(table_name).select("*").limit(limit).offset(offset).execute()
    count_r = sb.table(table_name).select("id", count="exact").execute()
    total   = count_r.count if hasattr(count_r, "count") else len(r.data or [])
    return jsonify({"data": r.data or [], "total": total, "limit": limit, "offset": offset})


@superadmin_bp.post("/db/sql")
@require_superadmin
def db_sql():
    body  = request.get_json() or {}
    query = body.get("query", "").strip()
    if not query:
        return jsonify({"error": "Query vazia"}), 400
    try:
        _check_dangerous_sql(query)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        sb = get_supabase_admin()
        r  = sb.rpc("exec_sql", {"query": query}).execute()
        return jsonify({"data": r.data, "count": len(r.data) if isinstance(r.data, list) else 1})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─── DEPLOY ───────────────────────────────────────────────
@superadmin_bp.post("/deploy/vercel-status")
@require_superadmin
def vercel_status():
    if not VERCEL_TOKEN or not VERCEL_PROJECT_ID:
        return jsonify({"error": "VERCEL_TOKEN e VERCEL_PROJECT_ID nao configurados"}), 400
    r = requests.get(f"https://api.vercel.com/v6/deployments?projectId={VERCEL_PROJECT_ID}&limit=5",
                     headers={"Authorization": f"Bearer {VERCEL_TOKEN}"}, timeout=10)
    data = [{"created": d["created"], "state": d["state"], "uid": d["uid"], "url": d.get("url","")}
            for d in r.json().get("deployments", [])[:3]]
    return jsonify({"data": data})


@superadmin_bp.post("/deploy/vercel-deploy")
@require_superadmin
def vercel_deploy():
    if not VERCEL_TOKEN or not VERCEL_PROJECT_ID:
        return jsonify({"error": "VERCEL_TOKEN e VERCEL_PROJECT_ID nao configurados"}), 400
    payload = {"name": VERCEL_PROJECT_ID}
    if GITHUB_REPO_ID:
        payload["gitSource"] = {"type": "github", "repoId": str(GITHUB_REPO_ID), "ref": "main"}
    r = requests.post("https://api.vercel.com/v13/deployments",
                      headers={"Authorization": f"Bearer {VERCEL_TOKEN}", "Content-Type": "application/json"},
                      json=payload, timeout=15)
    return jsonify({"data": r.json()})


@superadmin_bp.post("/deploy/github-status")
@require_superadmin
def github_status():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return jsonify({"error": "GITHUB_TOKEN e GITHUB_REPO nao configurados"}), 400
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page=5",
                     headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}, timeout=10)
    commits = r.json()
    if not isinstance(commits, list):
        return jsonify({"error": commits.get("message", "Erro na API GitHub")}), 400
    return jsonify({"data": [{"sha": c["sha"][:7], "message": c["commit"]["message"][:80],
             "author": c["commit"]["author"]["name"], "date": c["commit"]["author"]["date"]} for c in commits]})


@superadmin_bp.post("/deploy/github-backend-status")
@require_superadmin
def github_backend_status():
    repo = GITHUB_BACKEND_REPO or GITHUB_REPO
    if not GITHUB_TOKEN or not repo:
        return jsonify({"error": "GITHUB_TOKEN e GITHUB_BACKEND_REPO nao configurados"}), 400
    r = requests.get(f"https://api.github.com/repos/{repo}/commits?per_page=5",
                     headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}, timeout=10)
    commits = r.json()
    if not isinstance(commits, list):
        return jsonify({"error": commits.get("message", "Erro na API GitHub")}), 400
    return jsonify({"data": [{"sha": c["sha"][:7], "message": c["commit"]["message"][:80],
             "author": c["commit"]["author"]["name"], "date": c["commit"]["author"]["date"]} for c in commits]})


@superadmin_bp.post("/deploy/fly-status")
@require_superadmin
def fly_status():
    if not FLY_API_TOKEN:
        return jsonify({"error": "FLY_API_TOKEN nao configurado"}), 400
    try:
        r = requests.get(f"https://api.machines.dev/v1/apps/{FLY_APP_NAME}/machines",
                         headers={"Authorization": f"Bearer {FLY_API_TOKEN}"}, timeout=10)
        if r.status_code != 200:
            return jsonify({"error": f"Fly API: {r.status_code}"}), 400
        machines = r.json()
        return jsonify({"data": [{"id": m.get("id",""), "state": m.get("state",""),
                "region": m.get("region",""), "image": m.get("config",{}).get("image","")[:60]} for m in machines]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@superadmin_bp.post("/deploy/github-pull")
@require_superadmin
def github_pull():
    return jsonify({"data": (
        "No Fly.io nao e possivel fazer git pull diretamente. "
        "O deploy acontece automaticamente quando voce commita na branch main do GitHub. "
        "O arquivo .github/workflows/fly-deploy.yml cuida do deploy automatico."
    )})


# ─── AI AGENT ─────────────────────────────────────────────
@superadmin_bp.get("/ai/test-backend")
@require_superadmin
def test_backend():
    results = {}
    repo  = GITHUB_BACKEND_REPO
    token = GITHUB_TOKEN
    if not token:
        return jsonify({"ok": False, "error": "GITHUB_TOKEN nao configurado"}), 400
    if not repo:
        return jsonify({"ok": False, "error": "GITHUB_BACKEND_REPO nao configurado"}), 400
    results["repo"]         = repo
    results["token_prefix"] = token[:8] + "..." if len(token) > 8 else "(curto)"
    try:
        read = _github_read_file_from_repo(repo, "main.py")
        results["read_main_py"] = "ok" if read.get("ok") else read.get("error","erro")
    except Exception as e:
        results["read_main_py"] = str(e)
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}", headers=_gh_headers(), timeout=10)
        results["repo_api"]      = f"HTTP {r.status_code}"
        if r.status_code == 200:
            results["default_branch"] = r.json().get("default_branch","?")
    except Exception as e:
        results["repo_api"] = str(e)
    return jsonify({"ok": results.get("read_main_py") == "ok", "diagnostics": results})


# Alias de compatibilidade para frontend antigo
@superadmin_bp.get("/ai/test-hf")
@require_superadmin
def test_hf_compat():
    return test_backend()


@superadmin_bp.post("/ai/models")
@require_superadmin
def ai_models():
    body     = request.get_json() or {}
    provider = body.get("provider", "openrouter")
    api_key  = body.get("api_key", "")
    if not api_key:
        return jsonify({"error": "API Key necessaria"}), 400

    ENDPOINTS = {
        "openrouter": "https://openrouter.ai/api/v1/models",
        "openai":     "https://api.openai.com/v1/models",
        "github":     "https://models.github.ai/models",
        "mistral":    "https://api.mistral.ai/v1/models",
        "groq":       "https://api.groq.com/openai/v1/models",
        "together":   "https://api.together.xyz/v1/models",
        "xai":        "https://api.x.ai/v1/language-models",
    }

    STATIC_MODELS = {
        "anthropic": [
            {"id": "claude-opus-4-6",           "name": "Claude Opus 4.6"},
            {"id": "claude-sonnet-4-6",          "name": "Claude Sonnet 4.6 ⭐"},
            {"id": "claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet"},
            {"id": "claude-3-5-haiku-20241022",  "name": "Claude 3.5 Haiku"},
        ],
        "gemini": [
            {"id": "gemini-2.5-flash-preview-05-20", "name": "Gemini 2.5 Flash ⭐"},
            {"id": "gemini-2.0-flash",               "name": "Gemini 2.0 Flash"},
            {"id": "gemini-1.5-flash",               "name": "Gemini 1.5 Flash"},
            {"id": "gemini-2.5-pro-preview-06-05",   "name": "Gemini 2.5 Pro"},
        ],
        "cerebras": [
            {"id": "llama-4-scout-17b-16e-instruct", "name": "Llama 4 Scout 17B ⭐"},
            {"id": "llama3.3-70b",                   "name": "Llama 3.3 70B"},
            {"id": "qwen-3-235b",                    "name": "Qwen 3 235B"},
            {"id": "llama3.1-8b",                    "name": "Llama 3.1 8B"},
        ],
        "deepseek": [
            {"id": "deepseek-chat",     "name": "DeepSeek V3 ⭐"},
            {"id": "deepseek-reasoner", "name": "DeepSeek R1"},
        ],
        "cohere": [
            {"id": "command-r-plus", "name": "Command R+ ⭐"},
            {"id": "command-r",      "name": "Command R"},
            {"id": "command",        "name": "Command"},
        ],
    }

    if provider in STATIC_MODELS:
        return jsonify({"models": STATIC_MODELS[provider]})

    if provider == "ollama_local":
        base_url = api_key.rstrip("/")
        try:
            r      = requests.get(f"{base_url}/api/tags", timeout=10)
            models = [{"id": m["name"], "name": m["name"]} for m in r.json().get("models", [])]
            return jsonify({"models": models})
        except Exception as e:
            return jsonify({"error": f"Nao foi possivel conectar ao Ollama: {e}"}), 500

    url = ENDPOINTS.get(provider)
    if not url:
        return jsonify({"error": f"Provider '{provider}' nao suportado"}), 400

    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "openrouter":
        headers["HTTP-Referer"] = os.getenv("FRONTEND_URL", "https://redcomercialweb.vercel.app")
    try:
        r          = requests.get(url, headers=headers, timeout=10)
        models_raw = r.json()
        raw    = models_raw.get("data",[]) or models_raw.get("models",[]) or models_raw.get("items",[])
        models = [{"id": m.get("id") or m.get("name",""), "name": m.get("name") or m.get("id","")} for m in raw if m.get("id") or m.get("name")]
        return jsonify({"models": sorted(models, key=lambda x: x["id"])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@superadmin_bp.post("/ai/stream")
@require_superadmin
def ai_stream():
    body     = request.get_json() or {}
    provider = body.get("provider","openrouter")
    api_key  = body.get("api_key","")
    model    = body.get("model","")
    messages = body.get("messages",[])
    if not api_key:
        return jsonify({"error": "API Key necessaria"}), 400

    if not messages:
        history     = body.get("history",[])
        message     = body.get("message","")
        attachments = body.get("attachments",[])
        user_content = message
        if attachments:
            parts = [message] if message else []
            for att in attachments:
                if att.get("isText") and att.get("textContent"):
                    parts.append(f"\n\n[Arquivo: {att['name']}]\n```\n{att['textContent']}\n```")
                elif att.get("isImage") and att.get("data"):
                    parts.append(f"\n\n[Imagem: {att['name']}]")
                elif att.get("isPdf"):
                    parts.append(f"\n\n[PDF: {att['name']}]")
            user_content = "".join(parts)
        messages = history + ([{"role":"user","content":user_content}] if user_content else [])

    backend_repo  = GITHUB_BACKEND_REPO or "GITHUB_BACKEND_REPO nao configurado"
    frontend_repo = GITHUB_REPO or "GITHUB_REPO nao configurado"

    system = (
        "Voce e RED AI. Dois repositorios no GitHub:\n"
        f"BACKEND  (Fly.io) → repo '{backend_repo}': prefixo backend/ (ex: backend/app/routes/auth.py)\n"
        f"FRONTEND (Vercel) → repo '{frontend_repo}': sem prefixo (ex: src/App.jsx)\n"
        "NUNCA invente paths — use list_files() para ver a estrutura real primeiro.\n"
        "Fluxo OBRIGATORIO para editar: 1) list_files() → ver estrutura, "
        "2) read_file(path_exato) → ler, "
        "3) patch_file(path, old_str_literal, new_str) → editar.\n"
        "Commits no backend/ disparam GitHub Action → fly deploy em ~60s.\n"
        "Para conversa normal responda sem usar tools."
    )

    write_queue = []

    def _sse(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def generate():
        try:
            for token in _stream_provider(provider, api_key, model, system, messages, write_queue):
                if isinstance(token, dict):
                    if token.get("result") is None:
                        yield _sse({"type":"tool_start","tool":token.get("name",""),"args":token.get("args",{})})
                    else:
                        yield _sse({"type":"tool_done","tool":token.get("name",""),"result":token.get("result")})
                else:
                    yield _sse({"type":"token","text":token})
            yield _sse({"type":"done","pending_ops":write_queue})
        except Exception as e:
            yield _sse({"type":"error","text":str(e)})

    return Response(stream_with_context(generate()),
                    content_type="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@superadmin_bp.post("/ai/exec-ops")
@require_superadmin
def exec_ops():
    body = request.get_json() or {}
    ops  = body.get("ops",[])
    if not ops:
        return jsonify({"results":[],"commit":None})
    results = []
    for op in ops:
        result = _execute_tool_with_retry(op.get("name",""), op.get("args",{}), write_queue=None, max_attempts=3)
        results.append({"tool":op.get("name",""),"result":result})
    return jsonify({"results":results})


# Alias compatibilidade
@superadmin_bp.post("/ai/exec-hf-ops")
@require_superadmin
def exec_hf_ops_compat():
    return exec_ops()


# ─── Tools dispatch ───────────────────────────────────────
def _dispatch_tool(name, args, write_queue=None):
    path       = args.get("path","")
    is_backend = path.startswith("backend/")
    rel_path   = path[len("backend/"):] if is_backend else path
    repo       = GITHUB_BACKEND_REPO if is_backend else GITHUB_REPO

    def _read():
        return _github_read_file_from_repo(repo, rel_path)

    def _list():
        return _github_list_files_from_repo(repo, rel_path)

    def _write():
        content = args.get("content")
        message = args.get("message","chore: update via RED AI Agent")
        if is_backend and write_queue is not None:
            write_queue.append({"name":"write_file","args":args})
            return {"ok":True,"queued":True,"note":f"'{path}' enfileirado."}
        return _github_write_file_to_repo(repo, rel_path, content, message)

    def _patch():
        old_str = args.get("old_str")
        new_str = args.get("new_str")
        message = args.get("message","chore: patch via RED AI Agent")
        if not old_str:
            read_result = _github_read_file_from_repo(repo, rel_path)
            if "error" in read_result:
                return read_result
            return {"error":"old_str vazio.","action_required":"Leia o arquivo abaixo e chame patch_file com old_str preenchido.",
                    "file_content":read_result.get("content",""),"path":path}
        if is_backend and write_queue is not None:
            write_queue.append({"name":"patch_file","args":args})
            return {"ok":True,"queued":True,"note":f"'{path}' enfileirado."}
        return _github_patch_file_in_repo(repo, rel_path, old_str, new_str, message)

    dispatch = {
        "read_file":    _read,
        "list_files":   _list,
        "write_file":   _write,
        "patch_file":   _patch,
        "run_sql":      lambda: _run_sql(args.get("query","")),
        "list_tenants": lambda: _list_tenants_tool(),
    }
    fn = dispatch.get(name)
    if not fn:
        return {"error":f"Tool '{name}' nao reconhecida"}
    try:
        return fn()
    except Exception as e:
        return {"error":str(e)}


# ─── GitHub helpers ───────────────────────────────────────
def _gh_headers():
    return {"Authorization":f"Bearer {GITHUB_TOKEN}",
            "Accept":"application/vnd.github+json",
            "X-GitHub-Api-Version":"2022-11-28"}


def _github_read_file_from_repo(repo, path):
    if not GITHUB_TOKEN or not repo:
        return {"error":"GITHUB_TOKEN ou repo nao configurado"}
    r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}",
                     headers=_gh_headers(), timeout=15)
    if r.status_code == 404:
        parent  = "/".join(path.split("/")[:-1]) if "/" in path else ""
        listing = _github_list_files_from_repo(repo, parent)
        files   = [f["path"] for f in listing.get("files",[])]
        prefix  = "backend/" if repo == GITHUB_BACKEND_REPO else ""
        return {"error":f"Arquivo nao encontrado: {prefix}{path}",
                "hint":f"Arquivos em '{prefix}{parent or 'raiz'}': {files}",
                "action_required":"Use um dos paths listados. Nunca invente paths."}
    d = r.json()
    if isinstance(d,dict) and d.get("content"):
        prefix = "backend/" if repo == GITHUB_BACKEND_REPO else ""
        return {"ok":True,"path":f"{prefix}{path}",
                "content":base64.b64decode(d["content"]).decode("utf-8",errors="replace"),
                "sha":d.get("sha","")}
    return {"error":"Nao foi possivel ler o arquivo"}


def _github_list_files_from_repo(repo, path):
    if not GITHUB_TOKEN or not repo:
        return {"error":"GITHUB_TOKEN ou repo nao configurado"}
    url = (f"https://api.github.com/repos/{repo}/contents/{path}" if path
           else f"https://api.github.com/repos/{repo}/contents")
    r     = requests.get(url, headers=_gh_headers(), timeout=15)
    items = r.json()
    if not isinstance(items, list):
        return {"error":items.get("message","Erro")}
    prefix = "backend/" if repo == GITHUB_BACKEND_REPO else ""
    return {"ok":True,"files":[{"name":i["name"],"path":f"{prefix}{i['path']}","type":i["type"]} for i in items]}


def _github_write_file_to_repo(repo, path, content, message, sha=None):
    if not path or content is None:
        return {"error":"path e content sao obrigatorios"}
    if not GITHUB_TOKEN or not repo:
        return {"error":"GITHUB_TOKEN ou repo nao configurado"}
    if not sha:
        rg = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}",
                          headers=_gh_headers(), timeout=15)
        if rg.status_code == 200:
            sha = rg.json().get("sha","")
    encoded = base64.b64encode(content.encode("utf-8")).decode()
    payload = {"message":message,"content":encoded}
    if sha:
        payload["sha"] = sha
    r = requests.put(f"https://api.github.com/repos/{repo}/contents/{path}",
                     headers=_gh_headers(), json=payload, timeout=30)
    if r.status_code in (200,201):
        commit_sha = r.json().get("commit",{}).get("sha","")[:7]
        prefix = "backend/" if repo == GITHUB_BACKEND_REPO else ""
        note   = "GitHub Action vai rodar fly deploy em ~60s" if repo == GITHUB_BACKEND_REPO else "Vercel redeploya automaticamente"
        return {"ok":True,"path":f"{prefix}{path}","commit":commit_sha,"note":note}
    return {"error":r.json().get("message","Erro ao escrever arquivo")}


def _github_patch_file_in_repo(repo, path, old_str, new_str, message):
    if not old_str or new_str is None:
        return {"error":"old_str e new_str sao obrigatorios"}
    read = _github_read_file_from_repo(repo, path)
    if "error" in read:
        return read
    updated, match_type = _fuzzy_replace(read["content"], old_str, new_str)
    if updated is None:
        prefix = "backend/" if repo == GITHUB_BACKEND_REPO else ""
        return {"error":f"Patch falhou em {prefix}{path}. Use write_file para reescrever o arquivo inteiro."}
    result = _github_write_file_to_repo(repo, path, updated, message, sha=read["sha"])
    if "ok" in result:
        result["match_type"] = match_type
    return result


# ─── Fuzzy replace ────────────────────────────────────────
def _fuzzy_replace(content, old_str, new_str):
    import re as _re

    if old_str in content:
        return content.replace(old_str, new_str, 1), "exact"

    content_lines = content.splitlines(keepends=True)

    def deep_norm(s):
        return _re.sub(r'\s+', ' ', s.strip()).rstrip(';').strip()

    old_lines_raw  = old_str.splitlines()
    old_lines_norm = [deep_norm(l) for l in old_lines_raw if l.strip()]
    content_norm   = [deep_norm(l) for l in content.splitlines()]

    if old_lines_norm:
        for start in range(len(content_norm) - len(old_lines_norm) + 1):
            if content_norm[start:start+len(old_lines_norm)] == old_lines_norm:
                orig_block  = "".join(content_lines[start:start+len(old_lines_norm)])
                orig_indent = len(content_lines[start]) - len(content_lines[start].lstrip())
                indent = " " * orig_indent
                new_ind = "\n".join(indent+l.strip() if l.strip() else l for l in new_str.splitlines())
                if orig_block.endswith("\n") and not new_ind.endswith("\n"):
                    new_ind += "\n"
                return content.replace(orig_block, new_ind, 1), "deep_normalized"

    def strip_c(s):
        s = _re.sub(r'/\*.*?\*/', '', s)
        s = _re.sub(r'<-.*$','',s)
        s = _re.sub(r'//.*$','',s)
        return s.strip()

    old_stripped = [deep_norm(strip_c(l)) for l in old_lines_raw if strip_c(l).strip()]
    cont_stripped = [deep_norm(strip_c(l)) for l in content.splitlines()]

    if old_stripped:
        for start in range(len(cont_stripped)-len(old_stripped)+1):
            if cont_stripped[start:start+len(old_stripped)] == old_stripped:
                orig_block  = "".join(content_lines[start:start+len(old_stripped)])
                orig_indent = len(content_lines[start]) - len(content_lines[start].lstrip())
                indent = " " * orig_indent
                new_ind = "\n".join(indent+l.strip() if l.strip() else l for l in new_str.splitlines())
                if orig_block.endswith("\n") and not new_ind.endswith("\n"):
                    new_ind += "\n"
                return content.replace(orig_block, new_ind, 1), "comment_stripped"

    key = max(old_stripped, key=len) if old_stripped else ""
    if key and len(key) > 6:
        for i, nl in enumerate(cont_stripped):
            if nl == key:
                orig_line   = content_lines[i]
                orig_indent = len(orig_line) - len(orig_line.lstrip())
                indent = " " * orig_indent
                new_lines = [deep_norm(l) for l in new_str.splitlines() if l.strip()]
                new_key = max(new_lines, key=len) if new_lines else deep_norm(new_str)
                sc  = ";" if orig_line.rstrip("\n").rstrip().endswith(";") and not new_key.endswith(";") else ""
                eol = "\n" if orig_line.endswith("\n") else ""
                return content.replace(orig_line, f"{indent}{new_key}{sc}{eol}", 1), "key_line"

    css_props = _re.findall(r'(--[\w-]+)\s*:', old_str)
    for prop in css_props:
        for i, line in enumerate(content_lines):
            if _re.search(rf'\b{_re.escape(prop)}\s*:', line):
                orig_line   = content_lines[i]
                orig_indent = len(orig_line) - len(orig_line.lstrip())
                indent = " " * orig_indent
                mv = _re.search(rf'{_re.escape(prop)}\s*:\s*([^;]+)', new_str)
                if mv:
                    val  = mv.group(1).strip()
                    eol  = "\n" if orig_line.endswith("\n") else ""
                    nln  = f"{indent}{prop}: {val};\n" if eol else f"{indent}{prop}: {val};"
                    return content.replace(orig_line, nln, 1), "css_prop_key"

    return None, None


# ─── SQL / tenant helpers ─────────────────────────────────
def _run_sql(query):
    if not query:
        return {"error":"Query vazia"}
    try:
        _check_dangerous_sql(query)
    except ValueError as e:
        return {"error":str(e)}
    try:
        sb = get_supabase_admin()
        r  = sb.rpc("exec_sql", {"query":query}).execute()
        return {"ok":True,"data":r.data}
    except Exception as e:
        return {"error":str(e)}


def _list_tenants_tool():
    try:
        sb   = get_supabase_admin()
        data = sb.table("tenants").select("id, nome, tipo, plano, ativo").execute().data or []
        return {"ok":True,"tenants":data}
    except Exception as e:
        return {"error":str(e)}


# ─── Provider streaming ───────────────────────────────────
def _stream_provider(provider, api_key, model, system, messages, write_queue=None):
    if write_queue is None:
        write_queue = []
    if provider == "ollama_local":
        base_url = api_key.rstrip("/")
        yield from _stream_ollama_tools(f"{base_url}/v1/chat/completions", model, system, messages, write_queue)
        return
    yield f"[Provider '{provider}' nao suportado]"


def _extract_json_objects(text):
    objects = []
    i = 0
    while i < len(text):
        if text[i] == '{':
            depth=0; start=i; in_str=False; escape=False
            for j in range(i, len(text)):
                c = text[j]
                if escape: escape=False; continue
                if c=='\\' and in_str: escape=True; continue
                if c=='"': in_str=not in_str; continue
                if in_str: continue
                if c=='{': depth+=1
                elif c=='}':
                    depth-=1
                    if depth==0:
                        try:
                            obj = json.loads(text[start:j+1])
                            if isinstance(obj,dict): objects.append(obj)
                        except Exception: pass
                        i=j; break
        i+=1
    return objects


def _extract_tool_calls_from_text(text):
    import re as _re
    if not text or not text.strip(): return {}
    TOOL_NAMES = {"read_file","list_files","write_file","patch_file","run_sql","list_tenants"}

    for pattern in [r'<tool_call>\s*(.*?)\s*</tool_call>', r'```(?:json)?\s*(.*?)\s*```']:
        for m in _re.compile(pattern, _re.DOTALL).finditer(text):
            for obj in _extract_json_objects(m.group(1)):
                name = obj.get("name") or obj.get("tool","")
                args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
                if name in TOOL_NAMES:
                    return {0:{"id":"fallback_0","name":name,"args_str":json.dumps(args,ensure_ascii=False)}}

    for obj in reversed(_extract_json_objects(text)):
        name = obj.get("name") or obj.get("tool","")
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
        if name in TOOL_NAMES:
            return {0:{"id":"fallback_0","name":name,"args_str":json.dumps(args,ensure_ascii=False)}}
    return {}


def _stream_ollama_tools(url, model, system, messages, write_queue):
    tools   = _get_tools_schema()
    headers = {"Content-Type":"application/json"}

    def _sanitize(msgs):
        out = []
        for m in msgs:
            role    = m.get("role","")
            content = m.get("content")
            if role=="assistant" and content=="":
                content = None
            if role=="tool" and isinstance(content,str) and len(content)>6000:
                try:
                    d = json.loads(content)
                    if isinstance(d,dict) and "content" in d and len(d["content"])>5000:
                        d["content"] = d["content"][:5000]+"\n[...TRUNCADO...]"
                        content = json.dumps(d,ensure_ascii=False)
                except Exception:
                    content = content[:6000]+"\n[...TRUNCADO...]"
            out.append({**m,"content":content})
        return out

    chat    = ([{"role":"system","content":system}] if system else []) + _sanitize(messages)
    MAX_ITER = 15

    _ACTION_KEYWORDS = {
        "altere","mude","troque","adicione","crie","cria","remove","remova",
        "delete","edite","edita","faca","execute","rode","liste","mostra","mostre",
        "busca","busque","verifica","corrija","implemente","adiciona","insere","insira",
        "atualiza","atualize","patch","commit","deploy","sql","select","insert","update",
        "criar","fazer","adicionar","mudar","alterar","trocar","background","cor",
        "componente","pagina","rota","endpoint","tabela","coluna","campo","botao","menu","aba","sidebar",
    }

    def _is_conv(msgs):
        um = [m for m in msgs if m.get("role")=="user"]
        if not um: return False
        last = (um[-1].get("content") or "").strip().lower()
        if len(last)>=60: return False
        return not bool(set(last.replace("?","").replace("!","").replace(",","").split()) & _ACTION_KEYWORDS)

    _force_no_tools = _is_conv(messages)
    _recent_calls   = []

    for iteration in range(MAX_ITER):
        payload = {"model":model,"messages":chat,"stream":True,"temperature":0.1,"options":{"num_ctx":4096}}
        if _force_no_tools:
            payload["tool_choice"] = "none"
        else:
            payload["tools"]       = tools
            payload["tool_choice"] = "auto"

        full_text = ""
        tc_acc    = {}

        try:
            with requests.post(url, headers=headers, json=payload, stream=True, timeout=300) as resp:
                if resp.status_code != 200:
                    yield f"\n Ollama HTTP {resp.status_code}: {resp.text[:300]}"
                    return
                for raw in resp.iter_lines():
                    if not raw: continue
                    line = raw.decode("utf-8",errors="replace")
                    if line.startswith("data: "): line=line[6:]
                    if line=="[DONE]": break
                    try:
                        chunk = json.loads(line)
                        delta = chunk["choices"][0].get("delta",{})
                        txt   = delta.get("content") or ""
                        if txt: full_text += txt
                        for tc in delta.get("tool_calls",[]):
                            idx = tc.get("index",0)
                            if idx not in tc_acc:
                                tc_acc[idx] = {"id":tc.get("id",f"call_{idx}"),"name":"","args_str":""}
                            fn = tc.get("function",{})
                            if fn.get("name"): tc_acc[idx]["name"] = fn["name"]
                            tc_acc[idx]["args_str"] += fn.get("arguments","")
                    except Exception: pass
        except Exception as e:
            yield f"\n Erro de conexao com Ollama: {e}"
            return

        if not tc_acc and full_text.strip() and not _force_no_tools:
            tc_acc = _extract_tool_calls_from_text(full_text)
            if tc_acc: full_text = ""

        if not tc_acc or _force_no_tools:
            import re as _re
            clean = full_text
            def _is_tool_json(s):
                TOOL_NAMES={"read_file","list_files","write_file","patch_file","run_sql","list_tenants"}
                for o in _extract_json_objects(s):
                    if (o.get("name") or o.get("tool","")) in TOOL_NAMES: return True
                return False
            clean = _re.sub(r'```(?:json)?\s*.*?\s*```', lambda m: "" if _is_tool_json(m.group(0)) else m.group(0), clean, flags=_re.DOTALL)
            clean = _re.sub(r'<tool_call>.*?</tool_call>', '', clean, flags=_re.DOTALL)
            if _is_tool_json(clean.strip()): clean=""
            text = clean.strip()
            for i in range(0, len(text), 8):
                yield text[i:i+8]
            break

        asst = {"role":"assistant","content":full_text or None,"tool_calls":[]}
        res_msgs = []

        for idx in sorted(tc_acc.keys()):
            tc        = tc_acc[idx]
            tool_name = tc["name"]
            call_id   = tc["id"]
            try:
                args = json.loads(tc["args_str"]) if tc["args_str"].strip() else {}
            except Exception:
                args = {}

            asst["tool_calls"].append({"id":call_id,"type":"function",
                "function":{"name":tool_name,"arguments":tc["args_str"]}})

            yield {"name":tool_name,"args":args}
            result = _execute_tool_with_retry(tool_name, args, write_queue=write_queue)
            yield {"name":tool_name,"args":args,"result":result}

            res_msgs.append({"role":"tool","tool_call_id":call_id,
                             "content":json.dumps(result,ensure_ascii=False)})

        chat.append(asst)
        chat.extend(res_msgs)

        sig = "|".join(f"{tc_acc[i]['name']}:{tc_acc[i]['args_str']}" for i in sorted(tc_acc.keys()))
        _recent_calls.append(sig)
        if len(_recent_calls)>=3 and len(set(_recent_calls[-3:]))==1:
            yield "\n\nEstou em loop. Tente ser mais especifico."
            return
    else:
        yield f"\n\n Limite de {MAX_ITER} iteracoes atingido."


def _execute_tool_with_retry(tool_name, args, write_queue=None, max_attempts=3):
    import time as _t
    last_error = None
    for attempt in range(1, max_attempts+1):
        try:
            result = _dispatch_tool(tool_name, args, write_queue=write_queue)
            if isinstance(result,dict) and "error" in result:
                err = str(result["error"]).lower()
                if any(x in err for x in ("timeout","connection","network","502","503","504")) and attempt<max_attempts:
                    last_error = result["error"]
                    _t.sleep(2*attempt); continue
            return result
        except Exception as e:
            last_error = str(e)
            if attempt<max_attempts: _t.sleep(2*attempt)
    return {"error":f"Falhou apos {max_attempts} tentativas: {last_error}"}


def _get_tools_schema():
    return [
        {"type":"function","function":{"name":"read_file",
            "description":"Le arquivo. Prefixo 'backend/' = repo backend (Fly.io). Sem prefixo = frontend (Vercel).",
            "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
        {"type":"function","function":{"name":"list_files",
            "description":"Lista arquivos. 'backend/' para o repo do backend. Sem prefixo para o frontend.",
            "parameters":{"type":"object","properties":{"path":{"type":"string","default":""}},"required":[]}}},
        {"type":"function","function":{"name":"write_file",
            "description":"Cria ou substitui arquivo inteiro.",
            "parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"message":{"type":"string"}},"required":["path","content"]}}},
        {"type":"function","function":{"name":"patch_file",
            "description":"Substitui trecho especifico (old_str -> new_str). Preferir sobre write_file.",
            "parameters":{"type":"object","properties":{"path":{"type":"string"},"old_str":{"type":"string"},"new_str":{"type":"string"},"message":{"type":"string"}},"required":["path","old_str","new_str"]}}},
        {"type":"function","function":{"name":"run_sql",
            "description":"Executa SQL no Supabase. SELECT livre; DROP/DELETE/TRUNCATE/UPDATE sem WHERE bloqueados.",
            "parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}},
        {"type":"function","function":{"name":"list_tenants",
            "description":"Lista todos os tenants.",
            "parameters":{"type":"object","properties":{},"required":[]}}},
    ]


def _check_dangerous_sql(query):
    ql = query.strip().lower()
    ql = " ".join(line.split("--")[0] for line in ql.splitlines())
    for pattern in ["drop table","drop database","drop schema","truncate","delete from","alter table"]:
        if pattern in ql:
            raise ValueError(f"Query perigosa bloqueada: '{pattern}'")
    if "update " in ql and " set " in ql and " where " not in ql:
        raise ValueError("UPDATE sem WHERE bloqueado por seguranca")
