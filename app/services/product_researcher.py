"""Servicio de investigacion automatica de productos para crear publicaciones.

Busca en DuckDuckGo + competidores MeLi para pre-llenar el wizard de publicacion
con datos optimizados (titulo SEO, categoria, atributos, rango de precios, descripcion).
Todas las busquedas y resultados se generan en espanol.
"""
import asyncio
import json
import re
import statistics
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup

from app.config import RESEARCH_TIMEOUT, RESEARCH_MAX_PAGES, RESEARCH_USER_AGENT


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProductProfile:
    """Result of research_product()."""
    title: str = ""
    description: str = ""
    category_id: str = ""
    category_name: str = ""
    category_path: str = ""
    attributes: list = field(default_factory=list)
    required_attributes: list = field(default_factory=list)
    recommended_attributes: list = field(default_factory=list)
    pictures: list = field(default_factory=list)
    suggested_price: dict = field(default_factory=dict)
    condition: str = "new"
    listing_type_id: str = "gold_special"
    competitors: list = field(default_factory=list)
    confidence: float = 0.0
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regex patterns for cleaning part numbers from titles
# ---------------------------------------------------------------------------

# Multi-dash part numbers: ABC-DEF-123, MP3FM-M95F-WH-005
_RE_MULTIDASH = re.compile(r'\b[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9-]+\b', re.IGNORECASE)
# Alphanumeric codes: MP3FM, M95Fi, ABC123, X5000
_RE_ALPHACODE = re.compile(r'\b[A-Z]{1,4}\d{2,}[A-Z0-9]*\b', re.IGNORECASE)
# Short dash codes with digits: BET-H, QN-65, BE-C, UE-55 (model series)
_RE_SHORT_DASH = re.compile(r'\b[A-Z]{1,4}\d*-[A-Z0-9]{1,4}\b', re.IGNORECASE)
# Resolution codes: 2160p, 1080p, 720p, 480p (redundant when 4K/FHD present)
_RE_RESOLUTION = re.compile(r'\b\d{3,4}p\b', re.IGNORECASE)
# Parenthetical codes: (2160p), (LH43BECHLGFXGO), (HDR10+)
_RE_PARENS = re.compile(r'\([^)]*\d+[^)]*\)')
# Numeric-dash codes: 824-16, 100-005
_RE_NUMDASH = re.compile(r'\b\d{3,}-\d+\b')
# Pure long numbers (likely UPC/EAN): 0123456789012
_RE_LONGNUM = re.compile(r'\b\d{8,}\b')
# SKU-like patterns: SNMC000498
_RE_SKU = re.compile(r'\b[A-Z]{2,6}\d{4,}\b', re.IGNORECASE)


def _clean_part_numbers(text: str) -> str:
    """Remove part numbers, model codes, SKUs from a product name."""
    cleaned = text
    for pattern in [_RE_PARENS, _RE_MULTIDASH, _RE_ALPHACODE, _RE_SHORT_DASH,
                    _RE_RESOLUTION, _RE_NUMDASH, _RE_LONGNUM, _RE_SKU]:
        cleaned = pattern.sub('', cleaned)
    # Remove filler/technical words
    filler = re.compile(
        r'\b(Class|Series|Edition|Ver|Rev|Version|Type)\b', re.IGNORECASE
    )
    cleaned = filler.sub('', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = re.sub(r'^[\s,\-]+|[\s,\-]+$', '', cleaned).strip()
    return cleaned


def _extract_product_words(text: str) -> list[str]:
    """Extract meaningful product words, removing codes and short tokens."""
    cleaned = _clean_part_numbers(text)
    # Remove very short words and common noise
    noise = {'the', 'for', 'and', 'with', 'pack', 'box', 'set', 'de', 'con', 'para', 'en', 'el', 'la', 'los', 'las', 'un', 'una'}
    words = [w for w in cleaned.split() if len(w) > 1 and w.lower() not in noise]
    return words


# ---------------------------------------------------------------------------
# DuckDuckGo HTML search (no API key needed)
# ---------------------------------------------------------------------------

async def search_duckduckgo(query: str, max_results: int = 8) -> list[dict]:
    """Busca en DuckDuckGo HTML y retorna lista de {url, title, snippet}."""
    results = []
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": RESEARCH_USER_AGENT},
            follow_redirects=True,
            timeout=RESEARCH_TIMEOUT,
        ) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query, "kl": "mx-es"},
            )
            if resp.status_code != 200:
                return results

            soup = BeautifulSoup(resp.text, "html.parser")
            for r in soup.select(".result"):
                link_el = r.select_one("a.result__a")
                snippet_el = r.select_one(".result__snippet")
                if not link_el:
                    continue

                raw_href = link_el.get("href", "")
                url = _extract_ddg_url(raw_href)
                if not url:
                    continue

                title = link_el.get_text(strip=True)
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                results.append({"url": url, "title": title, "snippet": snippet})
                if len(results) >= max_results:
                    break
    except Exception:
        pass
    return results


def _extract_ddg_url(href: str) -> str | None:
    """Extrae URL real del redirect de DDG."""
    try:
        if "uddg=" in href:
            parsed = parse_qs(urlparse(href).query)
            urls = parsed.get("uddg", [])
            if urls:
                return unquote(urls[0])
        if href.startswith("http"):
            return href
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Web scraping - extract structured product data from pages
# ---------------------------------------------------------------------------

async def scrape_product_page(url: str) -> dict:
    """Extrae datos estructurados de una pagina web de producto.

    Intenta JSON-LD, Open Graph y meta tags.
    Retorna dict con: name, brand, model, description, images, price, specs.
    """
    data: dict = {}
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": RESEARCH_USER_AGENT},
            follow_redirects=True,
            timeout=RESEARCH_TIMEOUT,
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return data
            # Skip non-HTML responses
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                return data

            soup = BeautifulSoup(resp.text, "html.parser")

            # 1) JSON-LD
            for script in soup.select('script[type="application/ld+json"]'):
                try:
                    ld = json.loads(script.string or "")
                    items = ld if isinstance(ld, list) else [ld]
                    for item in items:
                        if item.get("@type") == "Product":
                            data["name"] = item.get("name", "")
                            brand = item.get("brand")
                            if isinstance(brand, dict):
                                data["brand"] = brand.get("name", "")
                            elif isinstance(brand, str):
                                data["brand"] = brand
                            data["model"] = item.get("model", "")
                            data["description"] = item.get("description", "")
                            data["gtin"] = item.get("gtin13") or item.get("gtin12") or item.get("gtin", "")
                            data["mpn"] = item.get("mpn", "")
                            img = item.get("image")
                            if isinstance(img, list):
                                data["images"] = img[:6]
                            elif isinstance(img, str):
                                data["images"] = [img]
                            offers = item.get("offers")
                            if isinstance(offers, dict):
                                data["price"] = offers.get("price")
                            elif isinstance(offers, list) and offers:
                                data["price"] = offers[0].get("price")
                            specs = item.get("additionalProperty", [])
                            if specs:
                                data["specs"] = {
                                    s.get("name"): s.get("value")
                                    for s in specs if s.get("name")
                                }
                            break
                except Exception:
                    continue

            # 2) Open Graph tags
            if not data.get("name"):
                og_title = soup.select_one('meta[property="og:title"]')
                if og_title:
                    data["name"] = og_title.get("content", "")
            if not data.get("description"):
                og_desc = soup.select_one('meta[property="og:description"]')
                if og_desc:
                    data["description"] = og_desc.get("content", "")
            if not data.get("images"):
                og_img = soup.select_one('meta[property="og:image"]')
                if og_img and og_img.get("content"):
                    data["images"] = [og_img["content"]]

            # 3) Standard meta / title
            if not data.get("name"):
                title_el = soup.select_one("title")
                if title_el:
                    data["name"] = title_el.get_text(strip=True)
            if not data.get("description"):
                meta_desc = soup.select_one('meta[name="description"]')
                if meta_desc:
                    data["description"] = meta_desc.get("content", "")

    except Exception:
        pass
    return data


# ---------------------------------------------------------------------------
# MeLi competitor analysis
# ---------------------------------------------------------------------------

async def analyze_meli_competitors(query: str, meli_client) -> dict:
    """Busca competidores en MeLi para el producto.

    Retorna: category_id, price_range, attributes, competitors, images.
    """
    result = {
        "category_id": "",
        "category_name": "",
        "price_range": {},
        "attributes": [],
        "competitors": [],
        "images": [],
        "titles": [],
    }
    try:
        search_data = await meli_client.search_items(query, limit=10)
        items = search_data.get("results", [])
        if not items:
            return result

        cat_counts: dict[str, int] = {}
        cat_names: dict[str, str] = {}
        prices = []
        attr_map: dict[str, dict] = {}

        for item in items:
            cid = item.get("category_id", "")
            if cid:
                cat_counts[cid] = cat_counts.get(cid, 0) + 1
                path = item.get("category_path", "")
                if path:
                    cat_names[cid] = path

            price = item.get("price")
            if price and price > 0:
                prices.append(price)

            for attr in item.get("attributes", []):
                aid = attr.get("id")
                if not aid:
                    continue
                vid = attr.get("value_id")
                vname = attr.get("value_name", "")
                if aid not in attr_map:
                    attr_map[aid] = {"id": aid, "name": attr.get("name", aid), "values": {}}
                key = vid or vname
                if key:
                    attr_map[aid]["values"][key] = attr_map[aid]["values"].get(key, 0) + 1

            result["competitors"].append({
                "title": item.get("title", ""),
                "price": item.get("price", 0),
                "sold_quantity": item.get("sold_quantity", 0),
                "permalink": item.get("permalink", ""),
                "thumbnail": item.get("thumbnail", ""),
            })
            result["titles"].append(item.get("title", ""))

            if len(result["images"]) < 6:
                thumb = item.get("thumbnail", "")
                if thumb:
                    result["images"].append({"url": thumb, "source": "competitor"})

        if cat_counts:
            best_cat = max(cat_counts, key=cat_counts.get)
            result["category_id"] = best_cat
            result["category_name"] = cat_names.get(best_cat, "")

        if prices:
            result["price_range"] = {
                "min": round(min(prices), 2),
                "max": round(max(prices), 2),
                "median": round(statistics.median(prices), 2),
            }

        for aid, info in attr_map.items():
            if not info["values"]:
                continue
            best_val = max(info["values"], key=info["values"].get)
            result["attributes"].append({
                "id": aid,
                "name": info["name"],
                "value_id": best_val if str(best_val).isdigit() or str(best_val).startswith("-") else None,
                "value_name": best_val if not (str(best_val).isdigit() or str(best_val).startswith("-")) else "",
                "frequency": info["values"][best_val],
            })

    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# USD/MXN exchange rate
# ---------------------------------------------------------------------------

async def fetch_usd_mxn_rate() -> float:
    """Obtiene tipo de cambio USD/MXN actual."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("https://api.mercadolibre.com/currency_conversions/search?from=USD&to=MXN")
            if resp.status_code == 200:
                data = resp.json()
                rate = data.get("ratio", 0)
                if rate and rate > 0:
                    return float(rate)
    except Exception:
        pass
    # Fallback: approximate rate
    return 20.0


# ---------------------------------------------------------------------------
# Title generation (SEO optimized, Spanish)
# ---------------------------------------------------------------------------

def build_seo_title(brand: str, model: str, product_name: str,
                    category_name: str = "", competitor_titles: list = None) -> str:
    """Genera titulo SEO optimizado para MeLi en espanol.

    Formato: Marca + Tipo de Producto + Tamaño/Specs + Descriptores
    Max 60 chars, sin numeros de parte, sin caracteres especiales.
    Prioriza palabras que un comprador buscaria.
    """
    competitor_titles = competitor_titles or []

    # Step 1: Extract size (inches) from original product name BEFORE cleaning
    size_str = ""
    size_match = re.search(r'(\d{2,3})\s*["\u201c\u201d]|\b(\d{2,3})\s*(?:inch|pulgadas?|in)\b',
                           product_name or "", re.IGNORECASE)
    if size_match:
        inches = size_match.group(1) or size_match.group(2)
        size_str = f'{inches} Pulgadas'

    # Step 2: Extract meaningful words from product name (remove all codes)
    product_words = _extract_product_words(product_name) if product_name else []

    # Step 3: Get product type from category
    cat_type = ""
    if category_name:
        parts = category_name.split(" > ")
        cat_type = parts[-1].strip() if parts else ""

    # Step 4: Analyze competitor titles for common keywords
    common_words = _extract_common_keywords(competitor_titles) if competitor_titles else []

    # Step 5: Detect key feature words from product name (4K, UHD, LED, Smart, etc.)
    feature_words = []
    feature_patterns = [
        r'\b(4K|8K|UHD|QLED|OLED|LED|LCD|Crystal|Smart|HDR|Bluetooth|WiFi|Wi-Fi)\b',
        r'\b(Inalambrico|Portatil|Digital|Pro|Premium|Ultra|Mini|Max)\b',
    ]
    source = product_name or ""
    for pat in feature_patterns:
        for m in re.finditer(pat, source, re.IGNORECASE):
            fw = m.group(1)
            if fw.lower() not in [f.lower() for f in feature_words]:
                feature_words.append(fw)

    # Step 6: Build title: Brand + Category Type + Size + Features + Descriptors
    title_parts = []

    if brand:
        title_parts.append(brand.strip())

    if cat_type:
        if brand and brand.lower() in cat_type.lower():
            title_parts = [cat_type.strip()]
        else:
            title_parts.append(cat_type.strip())

    if size_str:
        title_parts.append(size_str)

    # Add detected feature words
    current_lower = " ".join(title_parts).lower()
    for fw in feature_words:
        if fw.lower() not in current_lower:
            title_parts.append(fw)
            current_lower = " ".join(title_parts).lower()

    # Add meaningful product words not yet in title
    brand_lower = (brand or "").lower()
    cat_lower = (cat_type or "").lower()
    size_num = (size_match.group(1) or size_match.group(2)) if size_match else ""
    for word in product_words:
        wl = word.lower()
        if wl == brand_lower or wl in cat_lower or wl in current_lower:
            continue
        # Skip very generic words already covered
        if wl in ('tv', 'television', 'televisor') and ('tv' in cat_lower or 'television' in cat_lower):
            continue
        # Skip bare size number if we already added "XX Pulgadas"
        if size_num and wl.strip('"\'') == size_num:
            continue
        # Translate common English words to Spanish for MeLi
        translations = {'commercial': 'Comercial', 'portable': 'Portatil',
                       'wireless': 'Inalambrico', 'inch': '', 'inches': ''}
        if wl in translations:
            word = translations[wl]
            if not word:
                continue
        title_parts.append(word)
        current_lower = " ".join(title_parts).lower()

    # Add competitor keywords if title is short
    current = " ".join(title_parts)
    if len(current) < 40 and common_words:
        for kw in common_words[:3]:
            if kw.lower() not in current.lower() and len(current) + len(kw) + 1 <= 58:
                title_parts.append(kw)
                current = " ".join(title_parts)

    title = " ".join(title_parts)
    title = re.sub(r'[!¡¿?#$%&*()]+', '', title)
    title = re.sub(r'\s+', ' ', title).strip()

    # Truncate at 60 chars on word boundary
    if len(title) > 60:
        title = title[:60].rsplit(" ", 1)[0]

    # If title is too short, try best competitor title
    if len(title) < 15 and competitor_titles:
        good = [t for t in competitor_titles if 20 <= len(t) <= 60]
        if good:
            title = good[0]

    # Final fallback
    if len(title) < 5:
        if product_name:
            title = _clean_part_numbers(product_name)[:60]
        else:
            title = brand or ""

    return title


def _extract_common_keywords(titles: list[str]) -> list[str]:
    """Extract commonly used descriptive words from competitor titles."""
    if not titles:
        return []

    word_freq: dict[str, int] = {}
    # Words to skip
    skip = {'de', 'con', 'para', 'en', 'el', 'la', 'los', 'las', 'un', 'una',
            'por', 'del', 'al', 'y', 'o', 'x', 'the', 'for', 'and', 'with',
            'pcs', 'pz', 'pieza', 'piezas', 'pack', 'set', 'kit', 'caja',
            'unidad', 'unidades', 'nuevo', 'original', 'envio', 'gratis',
            'free', 'shipping'}

    for title in titles:
        words = re.findall(r'[A-Za-zÀ-ÿ]+', title)
        seen = set()
        for w in words:
            wl = w.lower()
            if wl not in skip and len(w) > 2 and wl not in seen:
                seen.add(wl)
                word_freq[w] = word_freq.get(w, 0) + 1

    # Sort by frequency, return top keywords that appear in 2+ titles
    sorted_words = sorted(word_freq.items(), key=lambda x: -x[1])
    return [w for w, count in sorted_words if count >= 2][:10]


# ---------------------------------------------------------------------------
# Description generation (Spanish, professional)
# ---------------------------------------------------------------------------

def build_detailed_description(product_data: dict, bm_info: dict = None,
                                meli_competitors: dict = None,
                                seo_title: str = "") -> str:
    """Genera descripcion detallada y profesional en espanol para MeLi.

    Estructura:
    1. Encabezado con nombre del producto
    2. Descripcion general (de web o generada)
    3. Caracteristicas principales (bullets)
    4. Especificaciones tecnicas (si hay)
    5. Informacion de compra (garantia, envio)
    6. Contenido del paquete
    """
    parts = []
    bm_info = bm_info or {}
    meli_competitors = meli_competitors or {}

    bm_title = bm_info.get("title", "")
    bm_brand = bm_info.get("brand", "")
    bm_model = bm_info.get("model", "")

    name = seo_title or bm_title or product_data.get("name", "")
    brand = bm_brand or product_data.get("brand", "")
    model = bm_model or product_data.get("model", "")
    web_desc = product_data.get("description", "")

    # Clean the display name - remove part numbers for readability
    display_name = _clean_part_numbers(name) if name else ""
    if brand and display_name and brand.lower() not in display_name.lower():
        display_name = f"{brand} {display_name}"

    # ========== ENCABEZADO ==========
    if display_name:
        parts.append(display_name.upper())
        parts.append("")

    # ========== DESCRIPCION GENERAL ==========
    # Use web description if available and meaningful
    if web_desc and len(web_desc) > 30:
        desc = web_desc.strip()
        desc = re.sub(r'<[^>]+>', '', desc)  # strip any HTML tags
        desc = re.sub(r'\s+', ' ', desc)
        if len(desc) > 1000:
            desc = desc[:1000].rsplit(" ", 1)[0] + "..."
        parts.append(desc)
        parts.append("")
    else:
        # Generate a basic intro
        if display_name and brand:
            parts.append(
                f"Presentamos el {display_name}, un producto de alta calidad "
                f"de la marca {brand}. Disenado para ofrecer un rendimiento "
                f"confiable y duradero."
            )
            parts.append("")

    # ========== CARACTERISTICAS PRINCIPALES ==========
    features = []
    if brand:
        features.append(f"Marca: {brand}")
    if model:
        features.append(f"Modelo: {model}")
    upc = bm_info.get("upc", "")
    if upc:
        features.append(f"Codigo universal (UPC): {upc}")
    features.append("Condicion: Nuevo")

    # Add meaningful competitor attributes
    comp_attrs = meli_competitors.get("attributes", []) if meli_competitors else []
    skip_ids = {"BRAND", "MODEL", "MPN", "GTIN", "SELLER_SKU", "ITEM_CONDITION",
                "LISTING_TYPE_ID", "ALPHANUMERIC_MODEL", "IS_GTIN_VALID",
                "PACKAGE_LENGTH", "PACKAGE_WIDTH", "PACKAGE_HEIGHT", "PACKAGE_WEIGHT"}
    for ca in comp_attrs:
        aid = ca.get("id", "")
        aname = ca.get("name", "")
        vname = ca.get("value_name", "")
        if aid not in skip_ids and vname and aname and len(features) < 15:
            features.append(f"{aname}: {vname}")
            skip_ids.add(aid)

    if features:
        parts.append("CARACTERISTICAS PRINCIPALES")
        parts.append("")
        for f in features:
            parts.append(f"- {f}")
        parts.append("")

    # ========== ESPECIFICACIONES TECNICAS ==========
    specs = product_data.get("specs", {})
    if specs:
        parts.append("ESPECIFICACIONES TECNICAS")
        parts.append("")
        for k, v in list(specs.items())[:15]:
            parts.append(f"- {k}: {v}")
        parts.append("")

    # ========== POR QUE ELEGIRNOS ==========
    parts.append("POR QUE COMPRAR CON NOSOTROS")
    parts.append("")
    parts.append(f"- Producto 100% original{' ' + brand if brand else ''}")
    parts.append("- Envio rapido a todo Mexico")
    parts.append("- Atencion al cliente personalizada")
    parts.append("- Garantia de satisfaccion")
    parts.append("")

    # ========== CONTENIDO DEL PAQUETE ==========
    parts.append("CONTENIDO DEL PAQUETE")
    parts.append("")
    parts.append(f"- 1x {display_name or 'Producto'}")
    parts.append("")

    # ========== NOTA ==========
    parts.append("NOTA: Las imagenes son de referencia. El producto puede variar ligeramente en presentacion.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Family name generation
# ---------------------------------------------------------------------------

def generate_family_name(brand: str, product_name: str, category_name: str = "") -> str:
    """Genera family_name para MeLi catalog integration.

    family_name = short descriptive name for the product family.
    Example: "Cubrebocas Desechable" or "Funda iPhone"
    """
    # Use category type as base
    cat_type = ""
    if category_name:
        parts = category_name.split(" > ")
        cat_type = parts[-1].strip() if parts else ""

    # Clean product name
    clean_name = _clean_part_numbers(product_name) if product_name else ""

    # Remove brand from clean name to avoid redundancy
    if brand and clean_name:
        clean_name = re.sub(re.escape(brand), '', clean_name, flags=re.IGNORECASE).strip()

    # Build family name: prefer category type + key descriptive words
    if cat_type:
        family = cat_type
        # Add a couple descriptive words from product if they add value
        if clean_name:
            extra_words = [w for w in clean_name.split()[:3]
                          if w.lower() not in cat_type.lower() and len(w) > 2]
            if extra_words:
                family += " " + " ".join(extra_words[:2])
    elif clean_name:
        family = clean_name[:60]
    elif brand:
        family = brand
    else:
        family = "Producto"

    return family[:60].strip()


# ---------------------------------------------------------------------------
# Price calculation
# ---------------------------------------------------------------------------

def calculate_suggested_price(bm_info: dict, meli_competitors: dict,
                               usd_mxn_rate: float = 20.0) -> dict:
    """Calcula precio sugerido.

    Prioridad:
    1. BinManager RetailPrice * tipo_cambio * 1.16 (si RetailPrice > 0)
    2. BinManager AvgCostQTY * tipo_cambio * 2 * 1.16 (si AvgCostQTY razonable)
    3. Mediana de competidores MeLi
    """
    result = {}
    bm_info = bm_info or {}
    meli_competitors = meli_competitors or {}

    # Competitor price range (always include if available)
    comp_range = meli_competitors.get("price_range", {})
    if comp_range:
        result["market_min"] = comp_range.get("min", 0)
        result["market_max"] = comp_range.get("max", 0)
        result["market_median"] = comp_range.get("median", 0)

    # Try BM RetailPrice
    retail_price = float(bm_info.get("retail_price", 0) or 0)
    if retail_price > 0:
        calculated = round(retail_price * usd_mxn_rate * 1.16, 2)
        result["calculated"] = calculated
        result["source"] = "retail_price"
        result["formula"] = f"${retail_price:.2f} USD x {usd_mxn_rate:.2f} x 1.16"
        result["suggested"] = calculated
        return result

    # Try BM AvgCostQTY (use 2x markup + IVA)
    avg_cost = float(bm_info.get("avg_cost", 0) or 0)
    if 0 < avg_cost < 5000:  # sanity check
        calculated = round(avg_cost * usd_mxn_rate * 2.0 * 1.16, 2)
        result["calculated"] = calculated
        result["source"] = "avg_cost"
        result["formula"] = f"${avg_cost:.2f} USD x {usd_mxn_rate:.2f} x 2.0 x 1.16"
        result["suggested"] = calculated
        return result

    # Fallback: competitor median
    if comp_range and comp_range.get("median"):
        result["suggested"] = comp_range["median"]
        result["source"] = "market"
        return result

    return result


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def research_product(sku: str, meli_client, bm_info: dict = None) -> dict:
    """Orquestador principal de investigacion de producto.

    1. Usa info de BinManager (Marca, Modelo, Titulo) si esta disponible
    2. Busca en paralelo: MeLi + DuckDuckGo (por titulo, modelo, SKU, UPC)
    3. Scrapea paginas web con datos del producto
    4. Predice categoria + obtiene atributos
    5. Genera titulo SEO y descripcion detallada en espanol
    6. Calcula precio sugerido (BM RetailPrice * TC * 1.16 o mercado)
    7. Genera family_name para catalog integration
    8. Retorna todo pre-llenado con sugerencias de atributos
    """
    warnings = []
    web_data: dict = {}
    all_scraped: list[dict] = []
    bm_info = bm_info or {}

    bm_brand = bm_info.get("brand", "")
    bm_model = bm_info.get("model", "")
    bm_title = bm_info.get("title", "")
    bm_upc = bm_info.get("upc", "")

    # ===== Construir queries de busqueda inteligentes (en espanol) =====
    if bm_title:
        search_query = bm_title
        meli_query = bm_title
    elif bm_brand and bm_model:
        search_query = f"{bm_brand} {bm_model}"
        meli_query = f"{bm_brand} {bm_model}"
    else:
        search_query = f"{sku} producto"
        meli_query = sku

    # ===== Fase 1: Busquedas en paralelo (MeLi + DDG + exchange rate) =====
    meli_task = asyncio.create_task(analyze_meli_competitors(meli_query, meli_client))
    rate_task = asyncio.create_task(fetch_usd_mxn_rate())

    # DDG search 1: Title + ficha tecnica
    ddg_task1 = asyncio.create_task(
        search_duckduckgo(f"{search_query} ficha tecnica caracteristicas")
    )
    # DDG search 2: Brand + model specs
    ddg_task2 = None
    if bm_model:
        ddg_task2 = asyncio.create_task(
            search_duckduckgo(f"{bm_brand} {bm_model} especificaciones producto")
        )
    # DDG search 3: SKU
    ddg_task3 = None
    if sku and sku != bm_model and not bm_title:
        ddg_task3 = asyncio.create_task(
            search_duckduckgo(f"{sku} producto especificaciones")
        )
    # DDG search 4: UPC
    ddg_task4 = None
    if bm_upc:
        ddg_task4 = asyncio.create_task(
            search_duckduckgo(f"{bm_upc} producto")
        )

    tasks = [meli_task, rate_task, ddg_task1]
    if ddg_task2:
        tasks.append(ddg_task2)
    if ddg_task3:
        tasks.append(ddg_task3)
    if ddg_task4:
        tasks.append(ddg_task4)

    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    meli_result = gathered[0]
    if isinstance(meli_result, Exception):
        meli_result = {}
        warnings.append("No se pudo analizar competencia en MeLi")

    usd_mxn_rate = gathered[1]
    if isinstance(usd_mxn_rate, Exception):
        usd_mxn_rate = 20.0

    # Collect all DDG results
    all_ddg_results = []
    seen_urls = set()
    for i in range(2, len(gathered)):
        ddg_res = gathered[i]
        if isinstance(ddg_res, Exception) or not ddg_res:
            continue
        for r in ddg_res:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_ddg_results.append(r)

    if not all_ddg_results:
        warnings.append("Busqueda web no encontro resultados")

    # ===== Fase 2: Scrapear mejores paginas =====
    scrape_tasks = []
    urls_to_scrape = []
    skip_domains = {"mercadolibre", "mercadoshops", "youtube", "facebook",
                    "twitter", "instagram", "tiktok"}
    for r in all_ddg_results:
        if len(urls_to_scrape) >= RESEARCH_MAX_PAGES + 2:
            break
        url = r.get("url", "")
        parsed = urlparse(url)
        if any(d in parsed.netloc for d in skip_domains):
            continue
        if url.lower().endswith(".pdf"):
            continue
        urls_to_scrape.append(url)
        scrape_tasks.append(asyncio.create_task(scrape_product_page(url)))

    if scrape_tasks:
        results = await asyncio.gather(*scrape_tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, dict) and res:
                all_scraped.append(res)

    # ===== Fase 3: Fusionar datos web =====
    if all_scraped:
        def page_score(p):
            s = len([v for v in p.values() if v])
            if p.get("specs"):
                s += 5
            if p.get("brand"):
                s += 3
            if p.get("description") and len(p.get("description", "")) > 50:
                s += 3
            return s
        best_page = max(all_scraped, key=page_score)
        web_data = best_page

        # Merge specs from all pages
        merged_specs = dict(web_data.get("specs", {}))
        for page in all_scraped:
            for k, v in page.get("specs", {}).items():
                if k not in merged_specs:
                    merged_specs[k] = v
        web_data["specs"] = merged_specs

    # Priority: BinManager > web > MeLi competitors
    brand = bm_brand or web_data.get("brand", "")
    model = bm_model or web_data.get("model", "")
    product_name = bm_title or web_data.get("name", "")

    if not brand and meli_result and meli_result.get("attributes"):
        for attr in meli_result["attributes"]:
            if attr["id"] == "BRAND" and (attr.get("value_name") or attr.get("value_id")):
                brand = attr.get("value_name") or ""
                break

    # ===== Fase 4: Prediccion de categoria =====
    # Clean part numbers for better category prediction
    clean_search = _clean_part_numbers(product_name or sku)
    if brand and brand.lower() not in clean_search.lower():
        clean_search = f"{brand} {clean_search}"
    if len(clean_search) < 5:
        clean_search = product_name or sku

    category_id = ""
    category_name = ""
    category_path = ""
    cat_attributes_raw = []

    try:
        prediction = await meli_client.predict_category(clean_search)
        if prediction and prediction.get("id"):
            category_id = prediction["id"]
            category_name = prediction.get("name", "")
            category_path = " > ".join(
                p.get("name", "") for p in prediction.get("path_from_root", [])
            )
    except Exception:
        pass

    # Fallback: most common category from competitors
    if not category_id and meli_result and meli_result.get("category_id"):
        category_id = meli_result["category_id"]
        category_name = meli_result.get("category_name", "")

    if not category_id:
        warnings.append("No se pudo predecir la categoria - seleccionala manualmente")

    # ===== Fase 5: Atributos de categoria + match =====
    required_attrs = []
    recommended_attrs = []
    matched_attributes = []

    if category_id:
        try:
            cat_attributes_raw = await meli_client.get_category_attributes(category_id)
        except Exception:
            pass

        for attr in cat_attributes_raw:
            attr_id = attr.get("id", "")
            tags = attr.get("tags", {})
            values = attr.get("values", [])

            attr_info = {
                "id": attr_id,
                "name": attr.get("name", attr_id),
                "type": attr.get("value_type", "string"),
                "values": [{"id": v.get("id"), "name": v.get("name")} for v in values[:50]],
                "required": tags.get("required", False),
                "catalog_required": tags.get("catalog_required", False),
                "allow_custom": attr.get("attribute_group_id") != "OTHERS",
            }

            if tags.get("required"):
                required_attrs.append(attr_info)
            elif tags.get("catalog_required") or attr_id in ["BRAND", "MODEL", "MPN", "GTIN"]:
                recommended_attrs.append(attr_info)

            matched = _match_attribute(attr_id, attr_info, meli_result, web_data, values, bm_info)
            if matched:
                matched_attributes.append(matched)

    # ===== Fase 6: Titulo SEO =====
    competitor_titles = meli_result.get("titles", []) if meli_result else []
    title = build_seo_title(brand, model, product_name, category_path or category_name, competitor_titles)
    if not title or len(title) < 10:
        title = _clean_part_numbers(product_name)[:60] if product_name else sku

    # ===== Fase 7: Descripcion =====
    desc_data = dict(web_data)
    desc_data.setdefault("brand", brand)
    desc_data.setdefault("model", model)
    description = build_detailed_description(
        desc_data, bm_info=bm_info,
        meli_competitors=meli_result,
        seo_title=title,
    )

    # ===== Fase 8: Imagenes (with verification warnings) =====
    pictures = []
    brand_lower = brand.lower() if brand else ""
    for img_url in web_data.get("images", [])[:4]:
        if img_url and isinstance(img_url, str) and img_url.startswith("http"):
            pictures.append({"url": img_url, "source": "web", "verified": False})
    if meli_result:
        for img in meli_result.get("images", [])[:4]:
            if img.get("url") not in {p.get("url") for p in pictures}:
                img["verified"] = False
                pictures.append(img)

    if not pictures:
        warnings.append("No se encontraron imagenes - DEBES subir tus propias fotos")
    else:
        warnings.append("IMPORTANTE: Las imagenes son de referencia y pueden NO coincidir con tu producto. Verifica cada imagen antes de usarla o sube tus propias fotos.")

    # ===== Fase 9: Precio =====
    price_info = calculate_suggested_price(bm_info, meli_result, usd_mxn_rate)

    # ===== Fase 10: Family name =====
    family_name = generate_family_name(brand, product_name, category_path or category_name)

    # ===== Fase 11: Confianza =====
    score = 0.0
    if title and len(title) > 15:
        score += 0.2
    if category_id:
        score += 0.25
    if matched_attributes:
        score += min(0.2, len(matched_attributes) * 0.03)
    if price_info.get("suggested"):
        score += 0.15
    if description and len(description) > 100:
        score += 0.1
    if brand:
        score += 0.1
    score = min(1.0, round(score, 2))

    return {
        "title": title,
        "description": description,
        "category_id": category_id,
        "category_name": category_name,
        "category_path": category_path,
        "attributes": matched_attributes,
        "required_attributes": required_attrs,
        "recommended_attributes": recommended_attrs,
        "pictures": pictures,
        "suggested_price": price_info,
        "condition": "new",
        "listing_type_id": "gold_special",
        "competitors": (meli_result.get("competitors", []) if meli_result else [])[:5],
        "confidence": score,
        "warnings": warnings,
        "family_name": family_name,
        "usd_mxn_rate": usd_mxn_rate,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _match_attribute(attr_id: str, attr_info: dict, meli_result: dict,
                     web_data: dict, catalog_values: list, bm_info: dict = None) -> dict | None:
    """Intenta encontrar un valor para un atributo desde las fuentes recopiladas.

    Prioridad: BinManager > web scraping specs > MeLi competidores.
    """
    bm_info = bm_info or {}

    # 1. BinManager + web data for common attributes
    web_map = {
        "BRAND": bm_info.get("brand") or web_data.get("brand"),
        "MODEL": bm_info.get("model") or web_data.get("model"),
        "GTIN": bm_info.get("upc") or web_data.get("gtin") or web_data.get("upc"),
        "MPN": bm_info.get("model") or web_data.get("mpn") or web_data.get("model"),
    }
    if attr_id in web_map and web_map[attr_id]:
        value = web_map[attr_id]
        matched_vid = None
        for cv in catalog_values:
            if cv.get("name", "").lower() == value.lower():
                matched_vid = cv.get("id")
                break
        bm_key = {"BRAND": "brand", "MODEL": "model", "GTIN": "upc", "MPN": "model"}.get(attr_id, "")
        return {
            "id": attr_id,
            "name": attr_info.get("name", attr_id),
            "value_id": matched_vid,
            "value_name": value,
            "source": "binmanager" if bm_info.get(bm_key) else "web",
        }

    # 2. Web-scraped specs
    specs = web_data.get("specs", {})
    if specs:
        attr_name_lower = attr_info.get("name", "").lower()
        for spec_name, spec_val in specs.items():
            if not spec_val:
                continue
            sn = spec_name.lower()
            if (sn == attr_name_lower
                    or sn.replace(" ", "_") == attr_id.lower()
                    or sn.replace(" ", "") == attr_id.lower().replace("_", "")):
                matched_vid = None
                for cv in catalog_values:
                    if cv.get("name", "").lower() == str(spec_val).lower():
                        matched_vid = cv.get("id")
                        break
                return {
                    "id": attr_id,
                    "name": attr_info.get("name", attr_id),
                    "value_id": matched_vid,
                    "value_name": str(spec_val),
                    "source": "web",
                }

    # 3. MeLi competitors
    if meli_result and meli_result.get("attributes"):
        for comp_attr in meli_result["attributes"]:
            if comp_attr["id"] == attr_id:
                vid = comp_attr.get("value_id")
                vname = comp_attr.get("value_name", "")
                if vid or vname:
                    return {
                        "id": attr_id,
                        "name": attr_info.get("name", attr_id),
                        "value_id": str(vid) if vid else None,
                        "value_name": vname,
                        "source": "competitor",
                    }

    return None
