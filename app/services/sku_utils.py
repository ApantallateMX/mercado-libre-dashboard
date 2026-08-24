"""
sku_utils.py — Utilidades canónicas para extracción de SKU de items ML.

Centraliza la lógica dispersa en main.py, stock_sync_multi.py y ml_listing_sync.py
para evitar inconsistencias entre las ~5 implementaciones inline.

Reglas de extracción (en orden de prioridad):
  1. Variaciones: seller_custom_field → attributes[SELLER_SKU]
  2. Item padre: seller_custom_field → attributes[SELLER_SKU]

normalize_to_bm_sku() — convierte cualquier variante de SKU ML al SKU base BM:
  "SNTV001864 + SNPE000180"  → "SNTV001864"   (bundle)
  "SNTV001864 / SNWM000001"  → "SNTV001864"   (bundle)
  "SNFN000941-FLX01"         → "SNFN000941"   (sufijo condición)
  "SNPE000003(10)"           → "SNPE000003"   (pack con cantidad)
  "SNTV001764 (2)"           → "SNTV001764"   (pack con espacio)

base_sku() — alias ligero que maneja bundles y sufijos, sin límite de 10 chars.
"""
import re

_FIRST_SKU_RE = re.compile(r'([A-Z]{2,8}\d{3,10})', re.IGNORECASE)
_NONE_VALUES = {"", "none", "null", "n/a", "-"}


def extract_variation_sku(variation: dict) -> str:
    """Extrae SKU de una variación ML (seller_custom_field o atributo SELLER_SKU)."""
    raw = (variation.get("seller_custom_field") or "").strip()
    if raw.lower() not in _NONE_VALUES:
        return raw
    for attr in (variation.get("attributes") or []):
        if attr.get("id") == "SELLER_SKU":
            v = (attr.get("value_name") or "").strip()
            if v.lower() not in _NONE_VALUES:
                return v
    return ""


def extract_item_sku(item: dict) -> str:
    """
    Extrae SKU de un item ML completo.
    Prioriza variaciones sobre el campo del item padre, ya que
    seller_custom_field del padre puede ser incorrecto cuando hay variaciones.
    """
    # 1. Variaciones
    for var in (item.get("variations") or []):
        s = extract_variation_sku(var)
        if s:
            return s

    # 2. Padre: seller_custom_field
    raw = (item.get("seller_custom_field") or "").strip()
    if raw.lower() not in _NONE_VALUES:
        return raw

    # 3. Padre: atributos
    for attr in (item.get("attributes") or []):
        if attr.get("id") == "SELLER_SKU":
            v = (attr.get("value_name") or "").strip()
            if v.lower() not in _NONE_VALUES:
                return v

    return ""


def normalize_to_bm_sku(sku: str) -> str:
    """Normaliza cualquier variante de SKU de MeLi al SKU base de BinManager.

    Todos los SKUs de BM siguen el patrón: SN + 2 letras + 6 dígitos = 10 chars.

    4 pasos:
      1. Bundle: tomar primera parte antes de " / " o " + "
      2. Packs: quitar sufijos entre paréntesis — (2), (10), (cantidad:2), etc.
      3. Cortar en primer espacio o guión → elimina -GRA, -ICS, -NEW, etc.
      4. Primeros 10 caracteres en mayúsculas = SKU BM

    Casos verificados:
      SNTV007270-ICS       → SNTV007270
      SNTV007270 NEW       → SNTV007270
      SNTV007270 / SNAC000029  → SNTV007270
      SNTV001764 (2)       → SNTV001764
      SNPE000003(10)       → SNPE000003
      SNPE000214(10)       → SNPE000214
    """
    if not sku:
        return ""
    s = re.split(r'\s*[/+]\s*', sku)[0].strip()
    s = re.sub(r'\s*\([^)]*\)', '', s).strip()
    s = re.split(r'[\s\-]', s)[0].strip()
    return s[:10].upper()


def clean_bm_title(title: str, brand: str = "", model: str = "") -> str:
    """Limpia el patrón de duplicación de título que viene sucio desde el
    feed de un proveedor dropship dentro de BinManager (columna `Title`
    cruda -- no la tocamos ni concatenamos nada de nuestro lado, ver
    _bm_catalog_sync_via_confcolumns en main.py). BinManager mismo entrega
    el dato así para varios SKUs (prefijo SH.., feed tipo Home Depot).

    Patrón real confirmado por Jovan (2026-08-18):
      "{Marca} {Modelo} {Marca}{Modelo}{resto del título real, sin espacio}"
    Ejemplos reales:
      "Hampton Bay HDP99180BRN Hampton BayHDP99180BRNKelford 18 in. 2-Light
       Brown Vanity Bath Light - 1008480255"
      → "Hampton Bay HDP99180BRN Kelford 18 in. 2-Light Brown Vanity Bath
         Light - 1008480255"
      "Toshiba WK0813CWRU ToshibaWK0813CWRU8000 BTU 115-Volt Smart Wi-Fi
       Touch Control Window Air Conditioner"
      → "Toshiba WK0813CWRU 8000 BTU 115-Volt Smart Wi-Fi Touch Control
         Window Air Conditioner"

    Diseño anti-falsos-positivos: en vez de adivinar la marca/modelo desde
    el propio texto del título (riesgo de cortar texto real que no está
    duplicado), usa los campos `brand`/`model` YA CONOCIDOS del mismo
    renglón de bm_sku_master/BM bulk -- son la fuente de verdad, no una
    heurística. Solo colapsa el título si:
      1. El título empieza EXACTAMENTE con "{brand} {model}" (case-insensitive).
      2. Justo después (con 0+ espacios) aparece OTRA COPIA de "{brand}{model}"
         (con o sin espacio entre marca y modelo en esa 2a copia).
    Si brand o model no están disponibles, o el patrón no calza al 100%,
    el título se devuelve TAL CUAL -- la inmensa mayoría de SKUs (sin este
    problema) no se toca en absoluto.
    """
    t = (title or "").strip()
    b = (brand or "").strip()
    m = (model or "").strip()
    if not t or not b or not m:
        return t

    first = f"{b} {m}"
    if len(t) <= len(first) or t[:len(first)].casefold() != first.casefold():
        return t

    rest = t[len(first):].lstrip(" ")
    for glue in (f"{b}{m}", f"{b} {m}"):
        if rest.casefold().startswith(glue.casefold()):
            real_rest = rest[len(glue):].lstrip(" ")
            return (first + (" " + real_rest if real_rest else "")).strip()

    # BUG REAL 2026-08-24 (Jovan reportó "Título" vacío en Alertas de Stock
    # tiempo real): faltaba este return. El título SÍ empieza con "{brand}
    # {model}" (línea 131 no lo descartó), pero el patrón de duplicado exacto
    # no aparece después -- ej. "Samsung UN55U8000FBXZA 55" Class..." (un
    # título normal, no el feed sucio de dropship). Sin este return, Python
    # regresaba None implícito -- silenciosamente vaciaba el título en TODOS
    # los llamadores de clean_bm_title() sin fallback propio (confirmado con
    # los 4/7 SKUs reales de la captura de Jovan). El propio docstring de esta
    # función (arriba) ya documentaba que este caso debía devolver `t` tal
    # cual -- nunca se implementó.
    return t


def target_coverage_days_for_sku(sku: str) -> int:
    """Días de cobertura objetivo (lead time real de reabasto) para un SKU.

    Canónica -- antes vivía solo en main.py como `_target_coverage_days_for_sku`
    (usada por `_rec_qty`), movida aquí 2026-08-22 para reusarla también en
    Amazon (amazon_products.py) sin import circular con main.py.

    14 días por default asume reabasto rápido (accesorios, SKUs locales). Pero
    el lead time real de importación de electrónicos (TVs, aduanas/pedimento,
    ver CLAUDE.md) es de 20-45 días — con 14 días fijos para TODO el catálogo,
    SNTV* sistemáticamente se recomendaba comprar corto. Solo se sube SNTV*
    (categoría de importación confirmada y ya tratada distinto en todo el
    código) — no se adivinan otros prefijos sin confirmar su taxonomía real,
    para no des-calibrar compras/alertas de SKUs que sí reabastecen rápido.
    """
    if (sku or "").upper().startswith("SNTV"):
        return 30
    return 14

    return t


def base_sku(sku: str) -> str:
    """
    Normaliza un SKU a su base (sin sufijo de variante) y extrae el primer
    SKU válido de strings compuestos (bundles separados por +, / o espacio).

    Ejemplos:
      "SNFN000941-FLX01"          → "SNFN000941"
      "SNTV001864 + SNPE000180"   → "SNTV001864"
      "SNTV001864 / SNWM000001"   → "SNTV001864"
      "SNAC000029"                → "SNAC000029"
      "SNPE000003(10)"            → "SNPE000003"   (pack con cantidad)
      "SNTV001764 (2)"            → "SNTV001764"   (pack con espacio)
    """
    if not sku:
        return ""
    upper = sku.upper().strip()
    # Quitar cantidad entre paréntesis: (2), (10), (cantidad:2), etc.
    upper = re.sub(r'\s*\([^)]*\)', '', upper).strip()
    # Quitar sufijo de variante (e.g. -FLX01, -BLK, -GRA, -NEW)
    base = upper.split("-")[0].strip()
    # Si quedan separadores de bundle, extraer primer token válido
    if re.search(r'[\s+/]', base):
        m = _FIRST_SKU_RE.search(base)
        if m:
            return m.group(1)
    return base
