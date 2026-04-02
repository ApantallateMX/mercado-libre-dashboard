"""
sku_utils.py — Utilidades canónicas para extracción de SKU de items ML.

Centraliza la lógica dispersa en main.py, stock_sync_multi.py y ml_listing_sync.py
para evitar inconsistencias entre las ~5 implementaciones inline.

Reglas de extracción (en orden de prioridad):
  1. Variaciones: seller_custom_field → attributes[SELLER_SKU]
  2. Item padre: seller_custom_field → attributes[SELLER_SKU]

base_sku() — normaliza SKUs compuestos (bundles):
  "SNTV001864 + SNPE000180"  → "SNTV001864"
  "SNTV001864 / SNWM000001"  → "SNTV001864"
  "SNFN000941-FLX01"         → "SNFN000941"
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


def base_sku(sku: str) -> str:
    """
    Normaliza un SKU a su base (sin sufijo de variante) y extrae el primer
    SKU válido de strings compuestos (bundles separados por +, / o espacio).

    Ejemplos:
      "SNFN000941-FLX01"          → "SNFN000941"
      "SNTV001864 + SNPE000180"   → "SNTV001864"
      "SNTV001864 / SNWM000001"   → "SNTV001864"
      "SNAC000029"                → "SNAC000029"
    """
    if not sku:
        return ""
    upper = sku.upper().strip()
    # Quitar sufijo de variante (e.g. -FLX01, -BLK)
    base = upper.split("-")[0].strip()
    # Si quedan separadores de bundle, extraer primer token válido
    if re.search(r'[\s+/]', base):
        m = _FIRST_SKU_RE.search(base)
        if m:
            return m.group(1)
    return base
