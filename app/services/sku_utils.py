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
