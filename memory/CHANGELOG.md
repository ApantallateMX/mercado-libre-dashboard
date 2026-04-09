# Changelog - Mercado Libre Dashboard

> Este archivo cubre features grandes hasta 2026-03-09. Para bugs/fixes/decisiones ver DEVLOG.md.

---

## 2026-03-09 — feat: Scheduler sync stock + alertas proactivas de sobreventa

- Tablas SQLite: `sync_alerts`, `sync_status`
- `_run_stock_sync_for_user`: detecta `meli_stock > 0 AND bm_avail = 0`
- `_stock_sync_loop`: ejecuta cada 4h para todas las cuentas
- Endpoints: `GET /api/sync/alerts`, `POST /api/sync/trigger`, `GET /api/sync/status`
- Banner rojo en `/items` cuando hay items en riesgo de sobreventa
- Badge rojo en tab Stock con conteo de items en riesgo

## 2026-03-09 — feat: Infraestructura core de sistema de agentes IA

- `app/services/agents/base.py` — `BaseAgent` ABC con tool-use loop (máx 10 iteraciones, Anthropic API via httpx)
- `app/services/memory_manager.py` — tablas `agent_memory`, `agent_conversations`, `agent_alerts`
- `app/services/scheduler_service.py` — wrapper de APScheduler + tabla `agent_jobs`

## 2026-03-05 — fix: Separar detección FULL vs variaciones en update_item_stock

- `_FULL_KEYWORDS` y `_VAR_ERROR_KEYWORDS` separados en `meli_client.py`
- FULL items ya no intentan actualizar variaciones
- Handler para `error: "full_item"` en `items.py`

## 2026-03-05 — feat: Fulfillment Management Universal Amazon + Agentes IA

- Botón universal en tab Inventario Amazon para todas las filas
- Modal con 4 acciones: Pausar, Cambiar a Merchant, Actualizar qty FBM, Reactivar FBA
- `update_listing_fulfillment(sku, action, quantity)` en `amazon_client.py`
- Agentes creados: `amazon-specialist.md`, `binmanager-specialist.md`, `financial-analyst.md`

## 2026-02-20 — feat: 4 cuentas MeLi persistidas en .env.production

- APANTALLATEMX (523916436), AUTOBOT MEXICO (292395685), BLOWTECHNOLOGIES (391393176), LUTEMAMEXICO (515061615)
- Multi-cuenta dinámico con `ContextVar _active_user_id` + `AccountMiddleware`
- Commit: `5eadb5b` / `6d36c7b`

## 2026-02-19 — feat: Stock Reservado vs Disponible BinManager

- `_bm_avail` = Available (excluye reservados). `_bm_total` = físico bruto.
- Dashboard muestra columna "BM Disp./Total"
- Botones Sync usan `_bm_avail` en lugar de `_bm_total`

## 2026-02-19 — feat: Ads API v2 (migración completa)

- Endpoints deprecados por MeLi migrados a `/marketplace/advertising/MLM/...`
- BLOQUEANTE: `certification_status: not_certified` → writes de Product Ads imposibles vía API
- Solución: gestionar en ads.mercadolibre.com.mx directamente

## 2026-02-18 — fix: Excluir stock IC en listings sin sufijo IC

- SKUs simples: `Condition="GRA,GRB,GRC,NEW"` (excluye ICB/ICC)
- SKUs con sufijo `-ICB`/`-ICC`: `Condition="GRA,GRB,GRC,ICB,ICC,NEW"`
- 7 puntos de consulta actualizados: `main.py`, `items.py`, `sku_inventory.py`

## 2026-02-18 — fix crítico: Reemplazar FullFillment API por Warehouse endpoint

- `FullFillment API` colapsaba condiciones GRA/GRB/GRC al mismo ProductSKU → stock incorrecto
- Nuevo endpoint correcto: `Get_GlobalStock_InventoryBySKU_Warehouse` con `LocationID="47,62,68"`
- `InventoryReport.AvailableQTY` NUNCA usar para stock (contador histórico, no físico)
