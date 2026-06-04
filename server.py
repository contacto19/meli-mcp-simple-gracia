"""
Simple Gracia — MCP de Mercado Libre (Chile / MLC)
==================================================
Servidor MCP "nivel Supermetrics" para Mercado Libre:
  - LECTURA: cuenta, items, ordenes, visitas/metricas, preguntas
  - DATA DE MERCADO: busqueda, tendencias, competencia de precios
  - MODIFICACION (con guardrail dry_run): precio, stock, estado, descripcion, responder preguntas

Diseñado para desplegarse en Render (transporte streamable-HTTP).

Variables de entorno requeridas:
  MELI_CLIENT_ID        -> APP_ID de tu app de MeLi
  MELI_CLIENT_SECRET    -> SECRET_KEY de tu app de MeLi
  MELI_REFRESH_TOKEN    -> refresh token inicial (se obtiene una vez via OAuth)
  MELI_SITE             -> sitio (default: MLC para Chile)
  TOKEN_STORE_PATH      -> ruta a archivo persistente para el refresh token rotativo
                           (en Render: monta un Disk y apunta aqui, ej. /var/data/meli_token.json)
  MCP_AUTH_TOKEN        -> (opcional) token para proteger el endpoint MCP
  PORT                  -> puerto (Render lo inyecta automaticamente)

IMPORTANTE sobre el refresh token:
  MeLi rota el refresh_token en CADA renovacion (es de un solo uso). Por eso este
  servidor lo persiste en TOKEN_STORE_PATH. En Render el filesystem es efimero:
  DEBES montar un Disk persistente, o cambiar _TokenStore por Postgres/Redis.
"""

import os
import json
import time
import threading
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------
API_BASE = "https://api.mercadolibre.com"
SITE = os.environ.get("MELI_SITE", "MLC")
CLIENT_ID = os.environ.get("MELI_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET", "")
INITIAL_REFRESH_TOKEN = os.environ.get("MELI_REFRESH_TOKEN", "")
TOKEN_STORE_PATH = os.environ.get("TOKEN_STORE_PATH", "./meli_token.json")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

# Desactiva la proteccion anti DNS-rebinding del SDK para que el endpoint
# sea accesible detras del proxy de Render (si no, devuelve "Invalid Host header").
try:
    from mcp.server.transport_security import TransportSecuritySettings
    _SECURITY = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
    )
    mcp = FastMCP("mercadolibre-simple-gracia", transport_security=_SECURITY)
except Exception:
    mcp = FastMCP("mercadolibre-simple-gracia")


# --------------------------------------------------------------------------
# Almacen persistente del token rotativo
# --------------------------------------------------------------------------
class _TokenStore:
    """Guarda access_token (con expiracion) y el refresh_token rotativo en disco."""

    def __init__(self, path: str, initial_refresh: str):
        self._path = path
        self._lock = threading.Lock()
        self._data = self._load()
        # Si no hay refresh guardado, usar el inicial del env.
        if not self._data.get("refresh_token") and initial_refresh:
            self._data["refresh_token"] = initial_refresh
            self._save()

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
        os.replace(tmp, self._path)

    @property
    def refresh_token(self) -> str:
        return self._data.get("refresh_token", "")

    @property
    def access_token(self) -> Optional[str]:
        if self._data.get("access_token") and time.time() < self._data.get("expires_at", 0):
            return self._data["access_token"]
        return None

    def update(self, access_token: str, refresh_token: str, expires_in: int) -> None:
        with self._lock:
            self._data["access_token"] = access_token
            self._data["refresh_token"] = refresh_token
            # margen de 5 min antes de la expiracion real (10800s)
            self._data["expires_at"] = time.time() + max(0, expires_in - 300)
            self._save()


_store = _TokenStore(TOKEN_STORE_PATH, INITIAL_REFRESH_TOKEN)


# --------------------------------------------------------------------------
# Cliente HTTP con auth + reintentos
# --------------------------------------------------------------------------
def _refresh_access_token() -> str:
    if not (CLIENT_ID and CLIENT_SECRET and _store.refresh_token):
        raise RuntimeError(
            "Faltan credenciales. Configura MELI_CLIENT_ID, MELI_CLIENT_SECRET y "
            "MELI_REFRESH_TOKEN en las variables de entorno."
        )
    resp = httpx.post(
        f"{API_BASE}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": _store.refresh_token,
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    tok = resp.json()
    _store.update(tok["access_token"], tok.get("refresh_token", _store.refresh_token), tok.get("expires_in", 10800))
    return tok["access_token"]


def _token() -> str:
    return _store.access_token or _refresh_access_token()


def _request(method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> Any:
    """Llama a la API de MeLi con reintentos (429/5xx) y refresco de token en 401."""
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    last_err = None
    for attempt in range(4):
        headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
        try:
            r = httpx.request(method, url, params=params, json=json_body, headers=headers, timeout=40)
            if r.status_code == 401:  # token vencido/invalido -> refrescar 1 vez
                _refresh_access_token()
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}", "detail": r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:500]}
            return r.json()
        except httpx.HTTPError as e:
            last_err = str(e)
            time.sleep(1.5 * (attempt + 1))
    return {"error": "request_failed", "detail": last_err}


def _seller_id() -> str:
    me = _request("GET", "/users/me")
    return str(me.get("id", ""))


# ==========================================================================
# LECTURA
# ==========================================================================
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_get_account() -> dict:
    """Datos de la cuenta conectada (id de vendedor, nickname, reputacion, pais)."""
    return _request("GET", "/users/me")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_list_items(status: str = "active", limit: int = 50, offset: int = 0) -> dict:
    """Lista los IDs de publicaciones del vendedor. status: active|paused|closed|under_review."""
    sid = _seller_id()
    return _request("GET", f"/users/{sid}/items/search",
                    params={"status": status, "limit": limit, "offset": offset})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_get_item(item_id: str) -> dict:
    """Detalle completo de una publicacion (titulo, precio, stock, estado, atributos)."""
    return _request("GET", f"/items/{item_id}")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_list_orders(limit: int = 30, offset: int = 0, sort: str = "date_desc") -> dict:
    """Ordenes del vendedor (ventas). Incluye comprador, items, montos y estado."""
    sid = _seller_id()
    return _request("GET", "/orders/search",
                    params={"seller": sid, "sort": sort, "limit": limit, "offset": offset})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_get_item_visits(item_id: str, last: int = 30, unit: str = "day") -> dict:
    """Visitas de una publicacion en una ventana de tiempo (hasta 150 dias)."""
    return _request("GET", f"/items/{item_id}/visits/time_window",
                    params={"last": last, "unit": unit})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_list_questions(status: str = "UNANSWERED", limit: int = 50) -> dict:
    """Preguntas recibidas en las publicaciones. status: UNANSWERED|ANSWERED."""
    sid = _seller_id()
    return _request("GET", "/questions/search",
                    params={"seller_id": sid, "status": status, "limit": limit, "api_version": 4})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_metrics_summary() -> dict:
    """Resumen tipo dashboard: cantidad de items activos/pausados, ordenes recientes y preguntas sin responder."""
    sid = _seller_id()
    active = _request("GET", f"/users/{sid}/items/search", params={"status": "active", "limit": 1})
    paused = _request("GET", f"/users/{sid}/items/search", params={"status": "paused", "limit": 1})
    orders = _request("GET", "/orders/search", params={"seller": sid, "sort": "date_desc", "limit": 1})
    q = _request("GET", "/questions/search", params={"seller_id": sid, "status": "UNANSWERED", "limit": 1, "api_version": 4})
    return {
        "items_activos": active.get("paging", {}).get("total"),
        "items_pausados": paused.get("paging", {}).get("total"),
        "ordenes_totales": orders.get("paging", {}).get("total"),
        "preguntas_sin_responder": q.get("total"),
    }


# ==========================================================================
# DATA DE MERCADO
# ==========================================================================
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_search_marketplace(query: str, limit: int = 20) -> dict:
    """Busca productos en el marketplace (competencia): titulos, precios, vendedores, mas vendidos."""
    return _request("GET", f"/sites/{SITE}/search", params={"q": query, "limit": limit})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_category_trends(category_id: str = "") -> dict:
    """Tendencias de busqueda por pais o por categoria (que esta buscando la gente)."""
    path = f"/trends/{SITE}" + (f"/{category_id}" if category_id else "")
    return _request("GET", path)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_price_competition(query: str, limit: int = 50) -> dict:
    """Estadistica de precios de la competencia para un termino (min, max, promedio, mediana)."""
    res = _request("GET", f"/sites/{SITE}/search", params={"q": query, "limit": limit})
    prices = [r["price"] for r in res.get("results", []) if isinstance(r.get("price"), (int, float))]
    if not prices:
        return {"query": query, "muestras": 0, "detalle": "sin resultados"}
    prices.sort()
    n = len(prices)
    return {
        "query": query,
        "muestras": n,
        "precio_min": prices[0],
        "precio_max": prices[-1],
        "precio_promedio": round(sum(prices) / n),
        "precio_mediana": prices[n // 2],
    }


# ==========================================================================
# MODIFICACION  (guardrail: dry_run=True por defecto -> NO aplica cambios)
# ==========================================================================
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
def meli_update_item(item_id: str, price: float | None = None, available_quantity: int | None = None,
                     status: str | None = None, title: str | None = None, dry_run: bool = True) -> dict:
    """Edita una publicacion (precio, stock, estado active/paused, titulo).
    GUARDRAIL: dry_run=True por defecto solo previsualiza. Para aplicar el cambio
    en MeLi en vivo, llamar con dry_run=False (requiere confirmacion explicita del usuario)."""
    body: dict = {}
    if price is not None:
        body["price"] = price
    if available_quantity is not None:
        body["available_quantity"] = available_quantity
    if status is not None:
        body["status"] = status
    if title is not None:
        body["title"] = title
    if not body:
        return {"error": "nada_que_cambiar"}
    if dry_run:
        return {"dry_run": True, "item_id": item_id, "cambios_propuestos": body,
                "nota": "No se aplico nada. Confirma con el usuario y vuelve a llamar con dry_run=False."}
    return _request("PUT", f"/items/{item_id}", json_body=body)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
def meli_update_item_description(item_id: str, plain_text: str, dry_run: bool = True) -> dict:
    """Actualiza la descripcion de una publicacion. GUARDRAIL: dry_run=True previsualiza."""
    if dry_run:
        return {"dry_run": True, "item_id": item_id, "nueva_descripcion": plain_text[:500],
                "nota": "No se aplico. Confirma y vuelve a llamar con dry_run=False."}
    return _request("PUT", f"/items/{item_id}/description", json_body={"plain_text": plain_text})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
def meli_answer_question(question_id: str, text: str, dry_run: bool = True) -> dict:
    """Responde una pregunta de un cliente. GUARDRAIL: dry_run=True previsualiza.
    OJO: es contenido de cara al cliente, confirmar siempre antes de dry_run=False."""
    if dry_run:
        return {"dry_run": True, "question_id": question_id, "respuesta": text,
                "nota": "No se envio. Confirma con el usuario y vuelve a llamar con dry_run=False."}
    return _request("POST", "/answers", json_body={"question_id": question_id, "text": text})


# ==========================================================================
# PUBLICACION (alta de items) — la info de producto/stock/fotos viene de Shopify
# ==========================================================================
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_category_predict(title: str) -> Any:
    """Predice la categoria de ML (category_id) a partir de un titulo de producto.
    Usalo ANTES de crear una publicacion, pasando el titulo del producto Shopify."""
    return _request("GET", f"/sites/{SITE}/domain_discovery/search", params={"q": title, "limit": 5})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_category_attributes(category_id: str) -> Any:
    """Atributos (obligatorios y opcionales) de una categoria. Necesario para armar el payload de creacion."""
    return _request("GET", f"/categories/{category_id}/attributes")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
def meli_create_item(title: str, category_id: str, price: float, available_quantity: int,
                     pictures: list[str], description: str = "", listing_type_id: str = "gold_special",
                     condition: str = "new", attributes: list | None = None, dry_run: bool = True) -> Any:
    """Crea una publicacion nueva en Mercado Libre tomando la info desde Shopify.
    - title, price, available_quantity: del producto Shopify (precio en CLP, stock real).
    - pictures: lista de URLs de imagen publicas (ej. el CDN de Shopify del producto).
    - description: texto plano de la descripcion (del producto Shopify).
    - category_id: usar meli_category_predict primero. attributes: usar meli_category_attributes si la categoria exige.
    - listing_type_id: 'gold_special' (clasica) o 'gold_pro' (premium).
    GUARDRAIL: dry_run=True solo previsualiza el payload. Para publicar en vivo, dry_run=False (confirmar con el usuario)."""
    body: dict = {
        "title": title,
        "category_id": category_id,
        "price": price,
        "currency_id": "CLP",
        "available_quantity": available_quantity,
        "buying_mode": "buy_it_now",
        "listing_type_id": listing_type_id,
        "condition": condition,
        "pictures": [{"source": u} for u in (pictures or [])],
    }
    if attributes:
        body["attributes"] = attributes
    if dry_run:
        return {"dry_run": True, "accion": "POST /items", "payload": body, "descripcion": description[:300],
                "nota": "No se publico nada. Confirma con el usuario y vuelve a llamar con dry_run=False."}
    res = _request("POST", "/items", json_body=body)
    # Si se creo, intenta cargar la descripcion en texto plano.
    if isinstance(res, dict) and res.get("id") and description:
        _request("POST", f"/items/{res['id']}/description", json_body={"plain_text": description})
    return res


# ==========================================================================
# PUBLICIDAD (Product Ads)
# OJO: requiere el scope de Publicidad habilitado en la app de MeLi. Hoy esta en
# 'Sin acceso' -> hay que editar permisos y re-autorizar (re-hacer el OAuth) para usarlo.
# ==========================================================================
def _ads_request(method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> Any:
    """Como _request pero agrega el header Api-Version requerido por la API de Publicidad."""
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    import time
    for attempt in range(3):
        headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json", "Api-Version": "1"}
        try:
            r = httpx.request(method, url, params=params, json=json_body, headers=headers, timeout=40)
            if r.status_code == 401:
                _refresh_access_token()
                continue
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}", "detail": r.text[:500],
                        "hint": "Si es 403/forbidden, falta el scope de Publicidad: edita permisos de la app y re-autoriza."}
            return r.json()
        except httpx.HTTPError as e:
            time.sleep(1.5 * (attempt + 1))
    return {"error": "request_failed"}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_ads_advertisers() -> Any:
    """Lista los advertisers de Product Ads de la cuenta (para obtener advertiser_id)."""
    return _ads_request("GET", "/advertising/advertisers", params={"product_id": "PADS"})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def meli_ads_campaigns(advertiser_id: str) -> Any:
    """Lista las campañas de Product Ads de un advertiser (estado, presupuesto, ACOS, metricas)."""
    return _ads_request("GET", f"/advertising/advertisers/{advertiser_id}/product_ads/campaigns")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
def meli_ads_request(method: str, path: str, body: dict | None = None, dry_run: bool = True) -> Any:
    """Generico para la API de Publicidad: crear/editar campañas, activar/pausar ads, fijar presupuesto y ACOS objetivo.
    method: GET|POST|PUT. path: ej. '/advertising/advertisers/{id}/product_ads/campaigns'.
    GUARDRAIL: en escrituras (POST/PUT) dry_run=True solo previsualiza. dry_run=False ejecuta (confirmar con el usuario).
    Requiere scope de Publicidad habilitado en la app (si da 403, re-autorizar la app con ese permiso)."""
    if method.upper() != "GET" and dry_run:
        return {"dry_run": True, "accion": f"{method.upper()} {path}", "body": body,
                "nota": "No se ejecuto. Confirma con el usuario y vuelve con dry_run=False."}
    return _ads_request(method.upper(), path, json_body=body)


# --------------------------------------------------------------------------
# Arranque
# --------------------------------------------------------------------------
if __name__ == "__main__":
    # Transporte streamable-HTTP para Render. El puerto lo inyecta Render via $PORT.
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="streamable-http")
