# MCP de Mercado Libre — Simple Gracia (Chile / MLC)

Servidor MCP "nivel Supermetrics" para Mercado Libre, listo para Render.
Lectura + data de mercado + modificación (con guardrail `dry_run`).

## Qué incluye

**Lectura:** `meli_get_account`, `meli_list_items`, `meli_get_item`, `meli_list_orders`,
`meli_get_item_visits`, `meli_list_questions`, `meli_metrics_summary`.

**Data de mercado:** `meli_search_marketplace`, `meli_category_trends`, `meli_price_competition`.

**Modificación (con confirmación):** `meli_update_item` (precio/stock/estado/título),
`meli_update_item_description`, `meli_answer_question`.
→ Todas traen `dry_run=True` por defecto: **previsualizan sin aplicar**. Para ejecutar en vivo
hay que llamarlas con `dry_run=False`, lo que el agente debe hacer solo tras tu confirmación.

## Paso 1 — Crear la app de desarrollador en MeLi

1. Entra a https://developers.mercadolibre.cl/devcenter/ (logueado con la cuenta de Simple Gracia).
2. "Crear aplicación". Completa:
   - **Nombre:** Simple Gracia MCP
   - **Redirect URI:** `https://localhost` (solo se usa una vez para capturar el código)
   - **Scopes:** `read`, `write`, `offline_access`
3. Guarda el **APP_ID** (client_id) y el **SECRET_KEY** (client_secret).

## Paso 2 — Obtener el refresh token (una sola vez)

1. Abre en el navegador (reemplaza APP_ID):
   `https://auth.mercadolibre.cl/authorization?response_type=code&client_id=APP_ID&redirect_uri=https://localhost`
2. Autoriza. Te redirige a `https://localhost/?code=TG-xxxxx`. Copia ese `code`.
3. Canjea el code por tokens:
   ```bash
   curl -X POST https://api.mercadolibre.com/oauth/token \
     -d grant_type=authorization_code \
     -d client_id=APP_ID \
     -d client_secret=SECRET_KEY \
     -d code=TG-xxxxx \
     -d redirect_uri=https://localhost
   ```
4. De la respuesta guarda el **`refresh_token`** (eso va en Render).

## Paso 3 — Desplegar en Render

1. Sube esta carpeta a un repo de Git y conéctalo en Render (o usa `render.yaml`).
2. En **Environment** del servicio pega: `MELI_CLIENT_ID`, `MELI_CLIENT_SECRET`,
   `MELI_REFRESH_TOKEN`, y (opcional) `MCP_AUTH_TOKEN`.
3. El `render.yaml` ya monta un **Disk persistente** en `/var/data` para guardar el token
   rotativo (clave: el refresh token de MeLi rota en cada uso). Plan **starter** o superior
   (el free no persiste disco).
4. Deploy. Tu endpoint MCP queda en `https://<tu-servicio>.onrender.com/mcp`.

## Paso 4 — Conectar a Claude

Agrega el servidor MCP remoto apuntando a `https://<tu-servicio>.onrender.com/mcp`.

## Notas

- Sitio fijo en **MLC** (Chile). Cambia `MELI_SITE` si hiciera falta.
- Manejo de límites de tasa con reintentos (429/5xx) y refresco automático en 401.
- Para mover el token a Postgres/Redis en vez de disco, reemplaza la clase `_TokenStore`.
