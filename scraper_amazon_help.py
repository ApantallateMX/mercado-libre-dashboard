"""
Amazon Knowledge Scraper — Completo
=====================================
Fuente 1: Amazon Seller Central MX Help (requiere login)
Fuente 2: SP-API Developer Docs (publico, sin login)

Guarda todo en Agentes/amazon-knowledge/ para alimentar al agente amazon-specialist.

USO:
    python3.13 scraper_amazon_help.py

Flujo:
  - Abre Chrome → tu haces login en Seller Central → ENTER → raspa automaticamente
  - Luego raspa SP-API docs sin intervencion
  - Al final: python3.13 update_agent_with_knowledge.py
"""

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

# ─── Configuracion ────────────────────────────────────────────────────────────
SC_BASE    = "https://sellercentral.amazon.com.mx"
SC_START   = f"{SC_BASE}/help/hub/reference/G2"
SPAPI_BASE = "https://developer-docs.amazon.com"
OUTPUT_DIR = Path("Agentes/amazon-knowledge")
MAX_SC_PAGES    = 200   # paginas Seller Central
MAX_SPAPI_PAGES = 300   # paginas SP-API docs

# ─── Seller Central: secciones prioritarias ───────────────────────────────────
SC_PRIORITY = [
    # Core / inicio
    "/help/hub/reference/G2",
    "/help/hub/reference/G200421970",
    "/help/hub/reference/G200336920",
    # Listings
    "/help/hub/reference/G200386250",
    "/help/hub/reference/G200390640",
    "/help/hub/reference/G200316110",
    "/help/hub/reference/G200291960",
    "/help/hub/reference/G200197950",
    "/help/hub/reference/G200270100",
    "/help/hub/reference/G200925890",
    "/help/hub/reference/G201140950",
    "/help/hub/reference/G200222450",
    # Pricing / Buy Box
    "/help/hub/reference/G200735930",
    "/help/hub/reference/G200383320",
    "/help/hub/reference/G201994750",
    "/help/hub/reference/G200336450",
    # FBA / Fulfillment
    "/help/hub/reference/G200547910",
    "/help/hub/reference/G200141510",
    "/help/hub/reference/G200612810",
    "/help/hub/reference/G201074400",
    "/help/hub/reference/G201411350",
    "/help/hub/reference/G200683750",
    "/help/hub/reference/G200298840",
    "/help/hub/reference/G200141500",
    "/help/hub/reference/G200178140",
    "/help/hub/reference/G200243180",
    # Ordenes
    "/help/hub/reference/G200198110",
    "/help/hub/reference/G200250370",
    "/help/hub/reference/G200709080",
    "/help/hub/reference/G200370560",
    "/help/hub/reference/G200893000",
    # Account Health
    "/help/hub/reference/G200205250",
    "/help/hub/reference/G200285960",
    "/help/hub/reference/G200761130",
    "/help/hub/reference/G200456110",
    "/help/hub/reference/G200370560",
    "/help/hub/reference/G201911360",
    "/help/hub/reference/G200414320",
    # Amazon Ads
    "/help/hub/reference/G200663330",
    "/help/hub/reference/G202172070",
    "/help/hub/reference/G200622870",
    "/help/hub/reference/G200212430",
    "/help/hub/reference/G202105490",
    "/help/hub/reference/G200390240",
    "/help/hub/reference/G200663340",
    "/help/hub/reference/G202145790",
    # Brand Registry
    "/help/hub/reference/G202130410",
    "/help/hub/reference/G201630350",
    "/help/hub/reference/G200164990",
    "/help/hub/reference/G201936720",
    # Reviews
    "/help/hub/reference/G201070100",
    "/help/hub/reference/G200145280",
    "/help/hub/reference/G200414320",
    # Tarifas / Pagos
    "/help/hub/reference/G200201830",
    "/help/hub/reference/G200336450",
    "/help/hub/reference/G200322510",
    "/help/hub/reference/G200168550",
    "/help/hub/reference/G200684750",
    # Politicas / Restricciones
    "/help/hub/reference/G200164990",
    "/help/hub/reference/G200270360",
    "/help/hub/reference/G200733160",
    "/help/hub/reference/G200477400",
    "/help/hub/reference/G201567350",
    # Reportes y analitica
    "/help/hub/reference/G200375890",
    "/help/hub/reference/G201612900",
    "/help/hub/reference/G200785170",
    "/help/hub/reference/G202173140",
    # Inventario / IPI
    "/help/hub/reference/G202061720",
    "/help/hub/reference/G201593890",
    "/help/hub/reference/G200413790",
    "/help/hub/reference/G200141500",
]

# ─── SP-API: todas las secciones de documentacion ─────────────────────────────
SPAPI_PAGES = [
    # Intro / overview
    "/sp-api/docs/welcome",
    "/sp-api/docs/sp-api-for-sellers",
    "/sp-api/docs/what-is-the-selling-partner-api",
    "/sp-api/docs/sp-api-endpoint",
    "/sp-api/docs/usage-plans-and-rate-limits-in-the-sp-api",
    "/sp-api/docs/sp-api-use-case-guides",
    "/sp-api/docs/connecting-to-the-selling-partner-api",
    # Auth
    "/sp-api/docs/authorizing-selling-partner-api-applications",
    "/sp-api/docs/self-authorization",
    "/sp-api/docs/the-selling-partner-api-and-login-with-amazon",
    "/sp-api/docs/seller-central-urls",
    "/sp-api/docs/tokens-api-use-case-guide",
    # Orders API
    "/sp-api/docs/orders-api-v0-reference",
    "/sp-api/docs/orders-api-v0-use-case-guide",
    # Listings API
    "/sp-api/docs/listings-items-api-v2021-08-01-reference",
    "/sp-api/docs/listings-items-api-v2021-08-01-use-case-guide",
    "/sp-api/docs/listing-restrictions-api-v2021-08-01-reference",
    "/sp-api/docs/product-types-definitions-api-v2020-09-01-reference",
    "/sp-api/docs/product-type-definitions-api-v2020-09-01-use-case-guide",
    # Catalog API
    "/sp-api/docs/catalog-items-api-v2022-04-01-reference",
    "/sp-api/docs/catalog-items-api-v2022-04-01-use-case-guide",
    # FBA Inventory
    "/sp-api/docs/fbainventory-api-v1-reference",
    "/sp-api/docs/fbainventory-api-v1-use-case-guide",
    # FBA Inbound
    "/sp-api/docs/fulfillment-inbound-api-v2024-03-20-reference",
    "/sp-api/docs/fulfillment-inbound-v2024-03-20-use-case-guide",
    "/sp-api/docs/fulfillment-inbound-api-v0-reference",
    # FBA Outbound
    "/sp-api/docs/fulfillment-outbound-api-v2020-07-01-reference",
    "/sp-api/docs/fulfillment-outbound-api-v2020-07-01-use-case-guide",
    # Pricing
    "/sp-api/docs/product-pricing-api-v0-reference",
    "/sp-api/docs/product-pricing-v0-use-case-guide",
    # Sales
    "/sp-api/docs/sales-api-v1-reference",
    "/sp-api/docs/sales-api-v1-use-case-guide",
    # Reports
    "/sp-api/docs/reports-api-v2021-06-30-reference",
    "/sp-api/docs/reports-api-v2021-06-30-use-case-guide",
    "/sp-api/docs/report-type-values",
    "/sp-api/docs/report-type-values-fba",
    "/sp-api/docs/report-type-values-order",
    "/sp-api/docs/report-type-values-inventory",
    "/sp-api/docs/report-type-values-analytics",
    # Feeds
    "/sp-api/docs/feeds-api-v2021-06-30-reference",
    "/sp-api/docs/feeds-api-v2021-06-30-use-case-guide",
    # Notifications
    "/sp-api/docs/notifications-api-v1-reference",
    "/sp-api/docs/notifications-api-v1-use-case-guide",
    "/sp-api/docs/notification-type-values",
    # Finances
    "/sp-api/docs/finances-api-reference",
    "/sp-api/docs/finances-api-use-case-guide",
    # Merchant Fulfillment (MFN)
    "/sp-api/docs/merchant-fulfillment-api-v0-reference",
    "/sp-api/docs/merchant-fulfillment-api-v0-use-case-guide",
    # Shipping
    "/sp-api/docs/shipping-api-v2-reference",
    "/sp-api/docs/shipping-api-v1-reference",
    # A+ Content
    "/sp-api/docs/aplus-content-api-v2020-11-01-reference",
    "/sp-api/docs/aplus-content-api-v2020-11-01-use-case-guide",
    # Advertising (Amazon Ads API)
    "/sp-api/docs/amazon-advertising-api",
    # Product Fees
    "/sp-api/docs/product-fees-api-v0-reference",
    "/sp-api/docs/product-fees-api-v0-use-case-guide",
    # Messaging
    "/sp-api/docs/messaging-api-v1-reference",
    "/sp-api/docs/messaging-api-v1-use-case-guide",
    # Solicitations (reviews request)
    "/sp-api/docs/solicitations-api-v1-reference",
    "/sp-api/docs/solicitations-api-v1-use-case-guide",
    # Seller API
    "/sp-api/docs/sellers-api-v1-reference",
    # Account
    "/sp-api/docs/account-api-v1-reference",
    # Brand Protection
    "/sp-api/docs/brand-protection-api-v1-reference",
    # Application Management
    "/sp-api/docs/application-management-api-v2023-11-30-reference",
    # Easy Ship
    "/sp-api/docs/easy-ship-api-v2022-03-23-reference",
    "/sp-api/docs/easy-ship-api-v2022-03-23-use-case-guide",
    # Data Kiosk (analytics)
    "/sp-api/docs/data-kiosk-api-v2023-11-15-reference",
    "/sp-api/docs/data-kiosk-api-v2023-11-15-use-case-guide",
    # Marketplace participation
    "/sp-api/docs/marketplace-participation-api",
    # Sandbox
    "/sp-api/docs/sandbox",
    "/sp-api/docs/sandbox-mode",
    # Best practices
    "/sp-api/docs/building-amazon-applications",
    "/sp-api/docs/high-throughput-use-cases",
    "/sp-api/docs/building-resilient-applications",
    "/sp-api/docs/frequently-used-sp-api-terminology",
]

VISITED = set()


def clean_text(text: str) -> str:
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip('/')
    return re.sub(r'[^a-zA-Z0-9_-]', '_', path)[-80:]


async def extract_content(page, url: str, is_spapi: bool = False) -> dict | None:
    """Extrae contenido de una pagina. Maneja tanto SC como SP-API docs."""
    try:
        await page.goto(url, wait_until='networkidle', timeout=35000)
        await asyncio.sleep(1.2 if is_spapi else 1.8)

        current = page.url
        # Detectar redireccion a login en SC
        if not is_spapi and ('signin' in current or 'ap/signin' in current):
            print(f"  [AUTH] Sesion expirada — necesitas re-login")
            return None

        title = await page.title()
        for suffix in [
            ' - Centro de Ayuda de Amazon Seller Central',
            ' - Amazon Seller Central',
            ' | Amazon SP-API Documentation',
            ' | Selling Partner API',
        ]:
            title = title.replace(suffix, '').strip()

        # Selectores para SP-API docs
        spapi_selectors = [
            '.docs-content',
            '.prose',
            '[class*="content"]',
            'article',
            'main',
        ]
        # Selectores para Seller Central
        sc_selectors = [
            '#help-content',
            '.help-content',
            '[data-testid="help-content"]',
            'article',
            '#page-content',
            '.content-area',
            'main',
        ]

        selectors = spapi_selectors if is_spapi else sc_selectors
        content_text = ''

        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    content_text = await el.inner_text()
                    if len(content_text) > 300:
                        break
            except Exception:
                continue

        if len(content_text) < 300:
            content_text = await page.inner_text('body')

        # Extraer links internos para crawl
        internal_links = []
        if is_spapi:
            links = await page.eval_on_selector_all(
                'a[href*="/sp-api/"]',
                'els => els.map(e => e.getAttribute("href"))'
            )
            for link in links:
                if link and '/sp-api/' in link:
                    full = urljoin(SPAPI_BASE, link.split('?')[0].split('#')[0])
                    if full not in VISITED and 'reference' not in full.split('#')[0][-20:]:
                        internal_links.append(full)
        else:
            links = await page.eval_on_selector_all(
                'a[href*="/help/hub/reference/"]',
                'els => els.map(e => e.getAttribute("href"))'
            )
            for link in links:
                if link and '/help/hub/reference/' in link:
                    full = urljoin(SC_BASE, link.split('?')[0])
                    if full not in VISITED:
                        internal_links.append(full)

        return {
            'url': url,
            'title': title,
            'content': clean_text(content_text),
            'links': list(set(internal_links)),
            'source': 'spapi' if is_spapi else 'seller_central',
        }

    except Exception as e:
        print(f"  [ERROR] {url}: {type(e).__name__}: {str(e)[:80]}")
        return None


async def scrape_seller_central(page) -> list:
    """Raspa el Centro de Ayuda de Seller Central (requiere sesion activa)."""
    queue = [SC_START] + [urljoin(SC_BASE, p) for p in SC_PRIORITY]
    queue = list(dict.fromkeys(queue))
    scraped = []

    print(f"\n{'='*60}")
    print(f"  FASE 1: Seller Central Help ({MAX_SC_PAGES} paginas max)")
    print(f"{'='*60}\n")

    while queue and len(scraped) < MAX_SC_PAGES:
        url = queue.pop(0)
        if url in VISITED:
            continue
        VISITED.add(url)

        print(f"[SC {len(scraped)+1:03d}] {url.split('/')[-1]}", end=' ... ')
        result = await extract_content(page, url, is_spapi=False)

        if result and len(result['content']) > 300:
            scraped.append(result)
            for link in result['links'][:8]:
                if link not in VISITED and len(queue) < 300:
                    queue.append(link)
            print(f"OK — '{result['title'][:45]}' ({len(result['content'])} chars)")
        else:
            if result is None:
                print("AUTH FAIL — deteniendo SC scraping")
                break
            print("SKIP")

        await asyncio.sleep(0.7)

    print(f"\n  [SC] Total: {len(scraped)} paginas extraidas de Seller Central")
    return scraped


async def scrape_spapi_docs(page) -> list:
    """Raspa la documentacion publica de SP-API (sin login)."""
    queue = [urljoin(SPAPI_BASE, p) for p in SPAPI_PAGES]
    queue = list(dict.fromkeys(queue))
    scraped = []

    print(f"\n{'='*60}")
    print(f"  FASE 2: SP-API Developer Docs ({MAX_SPAPI_PAGES} paginas max)")
    print(f"{'='*60}\n")

    while queue and len(scraped) < MAX_SPAPI_PAGES:
        url = queue.pop(0)
        if url in VISITED:
            continue
        VISITED.add(url)

        print(f"[API {len(scraped)+1:03d}] {url.split('/')[-1]}", end=' ... ')
        result = await extract_content(page, url, is_spapi=True)

        if result and len(result['content']) > 300:
            scraped.append(result)
            # Solo seguir links de docs y use-case-guides (no reference individuales)
            for link in result['links'][:5]:
                if link not in VISITED and len(queue) < 400:
                    queue.append(link)
            print(f"OK — '{result['title'][:45]}' ({len(result['content'])} chars)")
        else:
            print("SKIP")

        await asyncio.sleep(0.5)

    print(f"\n  [API] Total: {len(scraped)} paginas extraidas de SP-API Docs")
    return scraped


def save_results(sc_pages: list, api_pages: list):
    """Guarda todo organizado por fuente."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Directorios por fuente
    (OUTPUT_DIR / "seller-central").mkdir(exist_ok=True)
    (OUTPUT_DIR / "sp-api").mkdir(exist_ok=True)

    all_pages = sc_pages + api_pages

    # Guardar paginas individuales
    for item in all_pages:
        subdir = "seller-central" if item['source'] == 'seller_central' else "sp-api"
        filename = slug_from_url(item['url']) + '.md'
        filepath = OUTPUT_DIR / subdir / filename
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"# {item['title']}\n\n")
            f.write(f"**Fuente:** {item['url']}\n\n---\n\n")
            f.write(item['content'])

    # ── Archivo consolidado Seller Central ─────────────────────────────────
    sc_path = OUTPUT_DIR / "amazon-seller-central-knowledge.md"
    with open(sc_path, 'w', encoding='utf-8') as f:
        f.write("# Amazon Seller Central MX — Base de Conocimiento\n\n")
        f.write(f"Scrapeado: {time.strftime('%Y-%m-%d %H:%M')} | Paginas: {len(sc_pages)}\n\n---\n\n")
        for item in sc_pages:
            f.write(f"## {item['title']}\n\n> {item['url']}\n\n{item['content']}\n\n---\n\n")

    # ── Archivo consolidado SP-API ──────────────────────────────────────────
    api_path = OUTPUT_DIR / "amazon-spapi-knowledge.md"
    with open(api_path, 'w', encoding='utf-8') as f:
        f.write("# Amazon SP-API — Documentacion Completa\n\n")
        f.write(f"Scrapeado: {time.strftime('%Y-%m-%d %H:%M')} | Paginas: {len(api_pages)}\n\n---\n\n")
        for item in api_pages:
            f.write(f"## {item['title']}\n\n> {item['url']}\n\n{item['content']}\n\n---\n\n")

    # ── Indice JSON ─────────────────────────────────────────────────────────
    index = {
        'scraped_at': time.strftime('%Y-%m-%d %H:%M'),
        'seller_central': [
            {'title': x['title'], 'url': x['url'], 'chars': len(x['content'])}
            for x in sc_pages
        ],
        'sp_api': [
            {'title': x['title'], 'url': x['url'], 'chars': len(x['content'])}
            for x in api_pages
        ],
    }
    with open(OUTPUT_DIR / "index.json", 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    total = len(all_pages)
    print(f"\n{'='*60}")
    print(f"  GUARDADO COMPLETO")
    print(f"  Seller Central : {len(sc_pages)} paginas → {sc_path}")
    print(f"  SP-API Docs    : {len(api_pages)} paginas → {api_path}")
    print(f"  Total          : {total} paginas de conocimiento")
    print(f"{'='*60}")
    return sc_path, api_path


async def main():
    print("=" * 60)
    print("  Amazon Knowledge Scraper — Completo")
    print("  Seller Central + SP-API Docs")
    print("  Apantallate / MIT Technologies")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=['--start-maximized']
        )
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 900},
            locale='es-MX',
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        )
        page = await context.new_page()

        # ── FASE 1: Login en Seller Central ────────────────────────────────
        print("\n[1] Abriendo Seller Central MX para login...")
        await page.goto(SC_START, wait_until='domcontentloaded')

        print("\n" + "=" * 60)
        print("  ACCION REQUERIDA — SELLER CENTRAL:")
        print("  1. Inicia sesion en la ventana Chrome que se abrio")
        print("  2. Espera a ver el articulo de ayuda cargado")
        print("  3. Vuelve aqui y presiona ENTER")
        print("=" * 60)
        input("\n  >>> ENTER cuando hayas iniciado sesion: ")

        sc_pages = await scrape_seller_central(page)

        # ── FASE 2: SP-API Docs (sin login) ────────────────────────────────
        print("\n[2] Iniciando scraping de SP-API Docs (sin login)...")
        api_pages = await scrape_spapi_docs(page)

        # ── GUARDAR ────────────────────────────────────────────────────────
        print("\n[3] Guardando todo el conocimiento...")
        save_results(sc_pages, api_pages)

        await browser.close()

        print(f"\n  SIGUIENTE PASO:")
        print(f"  python3.13 update_agent_with_knowledge.py")
        print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
