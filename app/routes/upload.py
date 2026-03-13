"""
Upload de Arquivos — Supabase Storage. v9
Endpoints:
  POST /api/upload/image          → upload de imagem, retorna URL pública
  DELETE /api/upload/image        → remove imagem por URL

Supabase Storage Bucket: 'red-uploads'
Estrutura: {tenant_id}/{tipo}/{uuid}.{ext}
  tipo: vehicles | clients | products | logo

Requer: bucket 'red-uploads' público criado no Supabase Storage
"""
from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth
from ..utils.response import success, error
import uuid
import base64
import re

upload_bp = Blueprint("upload", __name__)

BUCKET      = "red-uploads"
MAX_SIZE_MB = 5
ALLOWED     = {"image/jpeg", "image/png", "image/webp", "image/gif"}
EXT_MAP     = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def _extract_path_from_url(url: str, bucket: str) -> str | None:
    """Extrai o path do arquivo a partir da URL pública do Supabase."""
    pattern = rf"/storage/v1/object/public/{bucket}/(.+)"
    m = re.search(pattern, url)
    return m.group(1) if m else None


@upload_bp.post("/image")
@require_auth
def upload_image():
    sb  = get_supabase_admin()
    tid = request.tenant_id

    # Aceita multipart/form-data OU JSON com base64
    content_type_req = request.content_type or ""

    if "multipart/form-data" in content_type_req:
        if "file" not in request.files:
            return error("Campo 'file' não encontrado")
        file     = request.files["file"]
        tipo     = request.form.get("tipo", "misc")
        mime     = file.content_type or "image/jpeg"
        if mime not in ALLOWED:
            return error(f"Tipo de arquivo não suportado. Use: {', '.join(ALLOWED)}")
        file_bytes = file.read()
        if len(file_bytes) > MAX_SIZE_MB * 1024 * 1024:
            return error(f"Arquivo muito grande. Máximo: {MAX_SIZE_MB}MB")
    else:
        # JSON com base64
        body = request.get_json() or {}
        tipo = body.get("tipo", "misc")
        b64  = body.get("base64", "")
        mime = body.get("mime", "image/jpeg")

        if mime not in ALLOWED:
            return error(f"Tipo de arquivo não suportado. Use: {', '.join(ALLOWED)}")

        # Remove prefixo data:image/...;base64,
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        try:
            file_bytes = base64.b64decode(b64)
        except Exception:
            return error("Base64 inválido")
        if len(file_bytes) > MAX_SIZE_MB * 1024 * 1024:
            return error(f"Arquivo muito grande. Máximo: {MAX_SIZE_MB}MB")

    ext  = EXT_MAP.get(mime, "jpg")
    path = f"{tid}/{tipo}/{uuid.uuid4()}.{ext}"

    try:
        sb.storage.from_(BUCKET).upload(
            path=path,
            file=file_bytes,
            file_options={"content-type": mime, "upsert": "true"},
        )
        # URL pública
        url_res = sb.storage.from_(BUCKET).get_public_url(path)
        # Supabase SDK pode retornar dict ou str
        if isinstance(url_res, dict):
            public_url = url_res.get("publicUrl") or url_res.get("publicURL", "")
        else:
            public_url = str(url_res)
        return success({"url": public_url, "path": path}, "Upload realizado!", 201)
    except Exception as e:
        return error(f"Erro no upload: {str(e)}", 500)


@upload_bp.delete("/image")
@require_auth
def delete_image():
    sb   = get_supabase_admin()
    body = request.get_json() or {}
    url  = body.get("url", "")

    if not url:
        return error("URL não informada")

    path = _extract_path_from_url(url, BUCKET)
    if not path:
        return error("URL inválida")

    # Segurança: só pode remover arquivos do próprio tenant
    tenant_id = request.tenant_id
    if not path.startswith(str(tenant_id) + "/"):
        return error("Sem permissão para remover este arquivo", 403)

    try:
        sb.storage.from_(BUCKET).remove([path])
        return success(message="Imagem removida")
    except Exception as e:
        return error(f"Erro ao remover: {str(e)}", 500)
