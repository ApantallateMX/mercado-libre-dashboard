# CLAUDE.md — mercado-libre-dashboard
# Este archivo se carga automáticamente en cada sesión de Claude Code.
# Contiene todas las reglas, contexto y memoria del proyecto.

## SYNC DE STACK MI2 — AL INICIO DE CADA SESIÓN

Fetchear `https://apps.mi2.com.mx/stack` y comparar `updated_at` contra la fecha
en `.claude/memory/reference_mi2_stack.md`. Si hay cambios, reescribir el archivo
con la información nueva antes de continuar. Fuente de verdad para reglas técnicas
de MI Technologies (conformance, HARD RULEs, infraestructura, modelos AI, agentes).

---

## SEÑAL DE RECONOCIMIENTO
Si el usuario escribe "1 2 3", responder EXACTAMENTE:
**diablos ya me encontraste corazon!**
Eso confirma que tengo toda la memoria y el contexto del proyecto cargado.

## IDENTIDAD DEL PROYECTO

Dashboard interno de Apantallate MX para gestión de ventas en Mercado Libre y Amazon MX.
- Usuario: Jovan Rodriguez (jovan.rodriguez@mitechnologiesinc.com)
- Stack: FastAPI + Python 3.12 + SQLite + HTMX + Tailwind
- Deploy principal: apantallatemx.up.railway.app (Railway)
- Deploy backup: ecomops.mi2.com.mx (Coolify — en pausa, exit 137 pendiente)
- Repos: ApantallateMX/mercado-libre-dashboard (Railway) · mi2-apps/ecomops (Coolify)

---

## REGLAS DE COLABORACIÓN — LEY ABSOLUTA, SIN EXCEPCIÓN

### 1. PLAN ANTES DE TOCAR CÓDIGO
Antes de cualquier cambio presentar:
1. Problema identificado
2. Archivos y líneas afectados
3. ⚠️ AFECTACIONES / RIESGOS (resaltado, obligatorio)
4. Solución propuesta
5. Esperar aprobación explícita del usuario

Aplica a TODO: bugs, features, refactors, env vars, Railway, credenciales, push.
Sin aprobación → no se toca nada. No hay excepción por "urgencia" o "fix obvio".

### 2. VERIFICAR ANTES DE PUSH
- Levantar servidor local: `py -m uvicorn app.main:app --port 8004`
- JWT local: `py make_jwt2.py` → llamar con `-H "Cookie: dash_session=TOKEN"`
- Solo hacer `git push` cuando curl devuelve 200 con JSON válido
- Esperar Railway deploy y confirmar "Deployment successful"

### 3. AL TERMINAR CUALQUIER FIX/FEAT
1. `git add [archivos]` → `git commit` → `git push origin main` → `git push mi2 main` (SIEMPRE ambos)
2. Actualizar DEVLOG.md y hacer commit/push
3. Confirmar al usuario: "subido a Railway y Coolify", "registrado en DEVLOG"

### 4. SCOPE DE CUENTA — NUNCA MEZCLAR
- Toda vista/función acotada a la cuenta seleccionada
- Excepción ÚNICA: tabs explícitamente "Global" o "Comparativa"
- Cada cuenta Amazon usa SUS PROPIAS credenciales (nunca mezclar client_id/secret/app)

### 5. FEATURES — TODAS LAS PLATAFORMAS
Cualquier feature pedido para ML o Amazon → implementar en TODAS las plataformas simultáneamente.

---

## REGLAS TÉCNICAS CRÍTICAS

### ML / Mercado Libre
- NUNCA pausar listings. Siempre `available_quantity: 0` (pausar penaliza el algoritmo)
- Sync automático que ESCRIBE en ML está prohibido — solo manual
- Gaps "Sin publicar" se calculan por cuenta (SKU en Autobot NO es gap para Lutema)
- SKUs combinados "SKU1 / SKU2": tomar el PRIMERO con exactamente 10 chars (formato BM)

### Amazon
- NUNCA pausar listings. Siempre `quantity: 0`
- Cada cuenta: AMAZONX_CLIENT_ID / AMAZONX_CLIENT_SECRET / AMAZONX_APP_SOLUTION_ID propios

### BinManager (BM) — REGLAS ABSOLUTAS
- **NUNCA llamar BM directamente** desde endpoints de lectura/display
- Fuente válida: `_bm_stock_cache` (dict en memoria, bulk pre-cargado)
- Si SKU no está en caché → valor es `None`. NO llamar BM para obtenerlo
- PROHIBIDO: httpx.AsyncClient() crudo, `_bm_stock()`, `get_available_qty()` en vivo
- Todo código BM DEBE usar `bm_post()` → `_BM_GLOBAL_SEM` (Semaphore 1)
- Stock vendible: `Get_GlobalStock_InventoryBySKU` CONCEPTID=1, LOC47+LOC62+LOC68+LOC45+LOC69+LOC43+LOC42 (implementado 2026-07-21, ver nota de LocationIDs abajo)
- `AvailableQTY = TotalQty - Reserve` (calculado por BM server-side)
- SIEMPRE mostrar AvailableQTY Y Reserve, incluso si Reserve=0
- ICB/ICC solo para SNTV* (TVs). Todos los demás: GRA,GRB,GRC,NEW únicamente
- `AvgCostQTY >= 9000` = valor centinela "sin costo". NUNCA usar como fallback

### BM — RetailPrice
- `RetailPrice` con SEARCH= puntual retorna 0. Usar `LastRetailPricePurchaseHistory` (con `NEEDRETAILPRICEPH: true`)
- Estructura fees Amazon MX: fee 18% + socio 7% = 25% total → neto vendedor 75%
- Precio sugerido: `retail_usd × 24` (recupera 100% del retail)

### BM — LocationIDs
- 47 = CDMX Autobot (Ebanistas)
- 62 = **Cuautitlán CDMX** (WH13, "CDMX-B2B") — corregido 2026-07-21: NO es Tijuana, es CDMX. Cuenta hacia el vendible.
- 63 = Tijuana BC real (WH14, "TJ-B2B") — sin stock propio (0 registros); el producto vendible de Tijuana vive en WH2 MITIJ
- 68 = Monterrey MAXX
- 66 = Guadalajara (NO incluida)
- 45, 69, 43, 42 = ubicaciones de WH2 "MITIJ" (Tijuana) clasificadas como vendible real tras auditoría SKU por SKU — **incluidas desde 2026-07-21**. El resto de MITIJ (tránsito "To Mexico", aduana, defectuoso, en proceso, y el bin de 340K que resultó ser 98.6% material de empaque) queda excluido a propósito.

Set final de stock vendible: `47,62,68,45,69,43,42`. Ver `.claude/memory/project_bm_locationid_62_63_swap.md` para el hallazgo completo y el detalle de qué se implementó en cada archivo.

### UI / Frontend
- Funciones onclick en scripts htmx: SIEMPRE `window.foo = function()`, nunca `function foo()`
- Display de dinero: MXN primario (grande), USD secundario (pequeño, gris, debajo)
- Toda tabla nueva: paginación de 10 filas/página (usar `_renderPaginated()`)
- Al corregir un bug: grep TODOS los lugares con el mismo patrón antes de cerrar el fix

---

## ARQUITECTURA DEL SISTEMA

### Cuentas ML activas
- APANTALLATEMX (user_id: 523916436)
- AUTOBOT (user_id: 292395685)
- BLOWTECHNOLOGIES (user_id: 391393176)
- LUTEMAMEXICO (user_id: 515061615)

### Cuentas Amazon activas
- VECKTOR IMPORTS (AMAZON1, Seller A20NFIUQNEYZ1E, MX)
- AUTOBOT AMZ MX (AMAZON2, Seller A252KSQ687FNRO, MX)
- ExclusiveBulbs (AMAZON3, Seller A22XNR713HGDVG, USA)

### Base de datos: tokens.db (SQLite)
- `ml_tokens` — tokens OAuth ML por cuenta
- `amazon_tokens` — tokens Amazon SP-API por cuenta
- `ml_listings` — catálogo de listings ML sincronizado
- `amazon_listings` — catálogo de listings Amazon sincronizado
- `bm_catalog` — caché de catálogo BinManager (metadata, precios, categorías)
- `order_history` — historial de órdenes ML + Amazon con snapshot costo/retail BM
- `higgsfield_assets` — assets generados con Higgsfield AI por SKU/listing

### Endpoints de diagnóstico (sin login)
```
GET /api/diag/sku?sku=SNWM000001&token=dk_b55c96a82a49f04908e0079bda6bee41ce2748be2c11f3b5
GET /api/diag/cache-health?token=dk_b55c96a82a49f04908e0079bda6bee41ce2748be2c11f3b5
```

---

## CUENTAS BM (BinManager)

- **Claude.Jovan@mitechnologiesinc.com** — cuenta de servicio ACTIVA (preferir)
- **jovan.rodriguez@mitechnologiesinc.com** — funcional (cuenta personal)
- **Carlos.Herrera@...** — IsFirstUse=true → retorna [] → NO usar
- **claudio.suarez@...** — HTTP 500 en todos los endpoints → NO usar

---

## AMAZON DEVELOPER CENTRAL

| App | App Solution ID | Usada por |
|-----|----------------|-----------|
| Claude Autobot Dashboard | 454ba70d-4aa1-4b27-a878-be5abaefdc7c | AUTOBOT AMZ MX (AMAZON2) |
| Claude Exclusive (Production) | 68ef1e09-d579-4f67-802a-8f6950c49261 | ExclusiveBulbs USA (AMAZON3) |
| VeKtorClaude | edc432e9-c674-4a48-a6f0-11891a51f840 | VECKTOR IMPORTS (AMAZON1) |

OAuth redirect URLs registrados:
- `https://apantallatemx.up.railway.app/auth/amazon/callback`
- `https://ecomops.mi2.com.mx/auth/amazon/callback`

---

## HIGGSFIELD AI

- **Estado: BLOQUEADO** — responde `not_enough_credits` vía API. No implementar hasta confirmar créditos activos en cloud.higgsfield.ai
- Plan acordado (5 fases): mejora listings, gaps, wizard ML, campañas temporada, studio contenido
- Variables a agregar en Railway cuando se reactive: `HIGGSFIELD_KEY_ID`, `HIGGSFIELD_SECRET`
- Leer `.claude/memory/project_higgsfield_plan.md` para plan completo

---

## RAILWAY — IDs y tokens

- **GitHub PAT:** en `.env.production` (no en git) y en `reference_railway_token.md`
- **Project ID:** 4d273fe8-14ec-456c-8177-f89d87124de0
- **Service ID:** 0775482a-8301-41a0-8106-394d060ecf26
- **Environment ID:** a4c39edf-55a9-47cc-a2e2-aaca3a2e9e11
- **API Token:** en `.env.production`

YO configuro Railway/env vars/APIs. El usuario NO debe hacer configuraciones técnicas.

---

## COOLIFY

- Repo: mi2-apps/ecomops
- URL: ecomops.mi2.com.mx
- **Estado actual: EN PAUSA** — exit 137 (OOM kill) pendiente fix de Amir
- `DISABLE_BM_MONITOR=true` → BM sync 1x/semana (viernes 9pm Monterrey)
- Status panel: status-dashboard.mi2.com.mx

---

## PENDIENTE / WIP (al inicio de cada sesión, leer también `.claude/memory/project_wip.md`)

- Probar ASIN search (buscar B0GWRX14QJ con ExclusiveBulbs)
- Verificar Returns Board ML en vista Global
- Verificar primera sync ExclusiveBulbs (156K+ listings vía Reports API)
- Verificar gap scan post-sync ExclusiveBulbs
- Probar Wizard Amazon v2 en browser
- VS REF% / Catalog sync RetailPH en Railway
- Coolify fix exit 137 (Amir pendiente)
- Higgsfield — verificar créditos activos antes de implementar

---

## MEMORIA COMPLETA

La carpeta `.claude/memory/` dentro de este proyecto contiene todos los archivos de memoria detallados.
En la nueva máquina, copiarlos a `~/.claude/projects/<ruta-del-proyecto>/memory/` para activar la memoria automática.

> Esta memoria fue generada el 2026-06-16 y cubre sesiones desde 2026-02-24 hasta hoy.
