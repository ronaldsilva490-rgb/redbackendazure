from flask import Blueprint, request
from ..utils.supabase_client import get_supabase_admin
from ..utils.auth_middleware import require_auth, require_papel
from ..utils.response import success, error

products_bp = Blueprint("products", __name__)




def _to_frontend(p):
    """Mapeia campos do banco para nomes esperados pelo frontend.
    Banco → Frontend: preco → preco_venda, imagem_url → foto_url
    """
    if not p:
        return p
    p = dict(p)
    if "preco" in p:
        p["preco_venda"] = p["preco"]
    if "imagem_url" in p:
        p["foto_url"] = p["imagem_url"]
    return p

def _clean(body):
    """Normaliza campos do frontend para nomes/tipos corretos do banco.
    Colunas reais: id, tenant_id, nome, descricao, categoria, preco, preco_custo,
                   estoque_atual(int), estoque_minimo(int), unidade, codigo_barras,
                   imagem_url, ativo, created_at, updated_at
    """
    # preco_venda (frontend) → preco (banco)
    if "preco_venda" in body:
        body["preco"] = body.pop("preco_venda")

    # foto_url (frontend) → imagem_url (banco)
    if "foto_url" in body:
        body["imagem_url"] = body.pop("foto_url") or None

    # Remove colunas que não existem no banco
    for f in ["sku", "categoria_id", "destino", "imagens", "tags", "peso",
              "dimensoes", "localizacao", "precisa_receita", "controlado",
              "margem_percentual", "preco_promocial"]:
        body.pop(f, None)

    # Numéricos: preco/preco_custo como float, estoques como int
    for f in ["preco", "preco_custo"]:
        val = body.get(f)
        body[f] = float(val) if val not in ("", None) else None
    for f in ["estoque_atual", "estoque_minimo"]:
        val = body.get(f)
        try: body[f] = int(float(val)) if val not in ("", None) else 0
        except: body[f] = 0

    # Strings opcionais → None se vazio
    for f in ["descricao", "categoria", "codigo_barras", "imagem_url", "unidade"]:
        if body.get(f) == "":
            body[f] = None

    return body


@products_bp.get("/")
@require_auth
def list_products():
    tid       = request.tenant_id
    search    = request.args.get("search", "").strip()
    categoria = request.args.get("categoria")
    ativo     = request.args.get("ativo")

    query = get_supabase_admin().table("products").select("*") \
        .eq("tenant_id", tid).order("nome")

    if search:
        query = query.or_(f"nome.ilike.%{search}%,codigo_barras.ilike.%{search}%")
    if categoria:
        query = query.eq("categoria", categoria)
    if ativo is not None:
        query = query.eq("ativo", ativo.lower() == "true")

    data = query.execute().data
    return success([_to_frontend(p) for p in data])


@products_bp.get("/<product_id>")
@require_auth
def get_product(product_id):
    resp = get_supabase_admin().table("products").select("*") \
        .eq("id", product_id).eq("tenant_id", request.tenant_id) \
        .maybe_single().execute()
    if not resp.data:
        return error("Produto não encontrado", 404)
    return success(_to_frontend(resp.data))


@products_bp.post("/")
@require_auth
def create_product():
    body = request.get_json() or {}
    body = _clean(body)

    if not body.get("nome"):
        return error("Nome é obrigatório")
    if body.get("preco") is None:
        return error("Preço de venda é obrigatório")

    body["tenant_id"] = request.tenant_id
    body.setdefault("estoque_atual",  0)
    body.setdefault("estoque_minimo", 0)
    body.setdefault("unidade", "un")
    body.setdefault("ativo",   True)

    # Remove campos protegidos/gerados
    for f in ["id", "created_at", "updated_at"]:
        body.pop(f, None)

    try:
        resp = get_supabase_admin().table("products").insert(body).execute()
        return success(_to_frontend(resp.data[0]), "Produto cadastrado", 201)
    except Exception as e:
        return error(f"Erro ao cadastrar: {str(e)}", 500)


@products_bp.put("/<product_id>")
@require_auth
def update_product(product_id):
    body = request.get_json() or {}
    body = _clean(body)
    for f in ["id", "tenant_id", "created_at", "updated_at"]:
        body.pop(f, None)

    try:
        resp = get_supabase_admin().table("products") \
            .update(body).eq("id", product_id).eq("tenant_id", request.tenant_id).execute()
        if not resp.data:
            return error("Produto não encontrado", 404)
        return success(_to_frontend(resp.data[0]), "Produto atualizado")
    except Exception as e:
        return error(f"Erro ao atualizar: {str(e)}", 500)


@products_bp.delete("/<product_id>")
@require_auth
@require_papel("dono", "gerente")
def delete_product(product_id):
    get_supabase_admin().table("products") \
        .delete().eq("id", product_id).eq("tenant_id", request.tenant_id).execute()
    return success(message="Produto removido")


@products_bp.patch("/<product_id>/estoque")
@require_auth
def update_estoque(product_id):
    body      = request.get_json() or {}
    quantidade = body.get("quantidade")
    operacao   = body.get("operacao", "adicionar")

    if quantidade is None:
        return error("quantidade é obrigatório")

    sb   = get_supabase_admin()
    prod = sb.table("products").select("estoque_atual") \
        .eq("id", product_id).eq("tenant_id", request.tenant_id) \
        .maybe_single().execute()
    if not prod.data:
        return error("Produto não encontrado", 404)

    atual = float(prod.data["estoque_atual"] or 0)
    qtd   = float(quantidade)

    if operacao == "adicionar":
        novo = atual + qtd
    elif operacao == "subtrair":
        novo = max(0, atual - qtd)
    else:
        novo = qtd

    resp = sb.table("products").update({"estoque_atual": novo}) \
        .eq("id", product_id).eq("tenant_id", request.tenant_id).execute()
    return success(resp.data[0], f"Estoque: {novo}")


@products_bp.get("/categorias/lista")
@require_auth
def list_categorias():
    rows = get_supabase_admin().table("products").select("categoria") \
        .eq("tenant_id", request.tenant_id).not_.is_("categoria", "null").execute().data
    cats = sorted(set(r["categoria"] for r in rows if r.get("categoria")))
    return success(cats)
