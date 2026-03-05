"""
Mercado Libre Developer Docs Scraper
=======================================
Raspa TODA la documentacion de developers.mercadolibre.cl:
- Como funciona MeLi (guias de negocio)
- API completa (endpoints, parametros, respuestas)
- Guias de producto, envios, promociones, ads, pagos, reclamos, etc.

Sin login requerido — HTML estatico.

USO: python3.13 scraper_meli_docs.py
Luego: python3.13 update_meli_agent.py
"""

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BASE_URL   = "https://developers.mercadolibre.cl"
OUTPUT_DIR = Path("Agentes/meli-knowledge")
MAX_PAGES  = 400
CONCURRENCY = 8   # requests paralelos

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "es-AR,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─── Mapa completo de URLs a scrapear ─────────────────────────────────────────
ALL_URLS = [
    # ── Primeros pasos / Auth ─────────────────────────────────────────────
    "/es_ar/api-docs-es",
    "/es_ar/guia-para-producto",
    "/es_ar/crea-una-aplicacion-en-mercado-libre-es",
    "/es_ar/permisos-funcionales",
    "/es_ar/usuarios-y-aplicaciones",
    "/es_ar/recomendaciones-de-autorizacion-y-token",
    "/es_ar/autenticacion-y-autorizacion",
    "/es_ar/realiza-pruebas",
    "/es_ar/validador-de-publicaciones",
    "/es_ar/buenas-practicas",
    "/es_ar/consideraciones-de-diseno",
    "/es_ar/gestionar-ips-aplicacion",
    "/es_ar/error-403",

    # ── Usuarios ──────────────────────────────────────────────────────────
    "/es_ar/consulta-usuarios",
    "/es_ar/validar-datos-de-vendedores",
    "/es_ar/bloqueo-de-aplicaciones",
    "/es_ar/direcciones-del-usuario",
    "/es_ar/preguntas-frecuentes-validacion-datos",
    "/es_ar/favoritos",

    # ── Dominios / Categorias / Ubicacion ─────────────────────────────────
    "/es_ar/dominios-y-categorias",
    "/es_ar/ubicacion-y-monedas",
    "/es_ar/atributos",

    # ── Items y Busquedas ─────────────────────────────────────────────────
    "/es_ar/items-y-busquedas",
    "/es_ar/introduccion-producto",
    "/es_ar/tipos-de-publicacion",
    "/es_ar/categorizacion-de-productos",
    "/es_ar/publicar-productos",
    "/es_ar/user-products",
    "/es_ar/precio-por-variacion",
    "/es_ar/stock-distribuido",
    "/es_ar/stock-multi-origen",
    "/es_ar/descripcion-de-productos",
    "/es_ar/validaciones",
    "/es_ar/imagenes",
    "/es_ar/identificadores-de-productos",
    "/es_ar/variaciones",
    "/es_ar/republicar-items",
    "/es_ar/kits-virtuales",
    "/es_ar/sincroniza-y-modifica-publicaciones",
    "/es_ar/compatibilidades-entre-items-y-productos",
    "/es_ar/referencias-de-dominios-autopartes",
    "/es_ar/convivencia-full-flex",

    # ── Precios y Costos ──────────────────────────────────────────────────
    "/es_ar/precios-de-productos",
    "/es_ar/precio-por-cantidad",
    "/es_ar/costos-por-vender",
    "/es_ar/referencias-de-precios",
    "/es_ar/automatizaciones-de-precios",
    "/es_ar/precios-netos-por-cantidad",

    # ── Catalogo ──────────────────────────────────────────────────────────
    "/es_ar/que-es-catalogo",
    "/es_ar/elegibilidad-de-catalogo",
    "/es_ar/buscador-de-productos",
    "/es_ar/publicar-en-catalogo",
    "/es_ar/publicaciones-requeridas",
    "/es_ar/catalogo-reacondicionados",
    "/es_ar/competencia",

    # ── Moderaciones ──────────────────────────────────────────────────────
    "/es_ar/gestionar-moderaciones",
    "/es_ar/moderaciones-con-pausado",
    "/es_ar/diagnostico-de-imagenes",
    "/es_ar/moderaciones-de-imagenes",

    # ── Guias de talles ───────────────────────────────────────────────────
    "/es_ar/primeros-pasos-guia-de-talles",
    "/es_ar/gestionar-guia-de-talles",
    "/es_ar/validacion-guia-de-talles",
    "/es_ar/calidad-de-fotos-de-moda",

    # ── Envios ────────────────────────────────────────────────────────────
    "/es_ar/mercado-envios-1",
    "/es_ar/estados-de-ordenes-y-seguimiento",
    "/es_ar/flete-dinamico",
    "/es_ar/mercado-envios-2",
    "/es_ar/costos-de-envio",
    "/es_ar/envios-en-feriados-opcionales",
    "/es_ar/envios-colecta-y-places",
    "/es_ar/agrupacion-de-paquetes",
    "/es_ar/envios-flex",
    "/es_ar/envios-turbo",
    "/es_ar/envios-fulfillment",
    "/es_ar/envios-personalizados",

    # ── Promociones ───────────────────────────────────────────────────────
    "/es_ar/gestionar-promociones",
    "/es_ar/campanas-tradicionales",
    "/es_ar/campanas-co-fondeadas",
    "/es_ar/campanas-con-descuento-por-cantidad",
    "/es_ar/pre-acordado-por-item-y-liquidacion-stock",
    "/es_ar/descuento-individual",
    "/es_ar/ofertas-del-dia",
    "/es_ar/ofertas-relampago",
    "/es_ar/campanas-del-vendedor",
    "/es_ar/co-fondeada-automatizada-y-precios-competitivos",
    "/es_ar/cupones-del-vendedor",
    "/es_ar/campana-co-fondeada-para-pix",

    # ── Gestion de Ventas ─────────────────────────────────────────────────
    "/es_ar/ordenes",
    "/es_ar/packs",
    "/es_ar/envios",
    "/es_ar/notas-de-packs",
    "/es_ar/pagos",
    "/es_ar/feedback-de-una-venta",
    "/es_ar/notas-en-ordenes",
    "/es_ar/preguntas-y-respuestas",

    # ── Facturacion ───────────────────────────────────────────────────────
    "/es_ar/datos-de-facturacion",
    "/es_ar/cargar-factura",
    "/es_ar/obtener-documento-fiscal",
    "/es_ar/descarga-de-factura",
    "/es_ar/envio-de-datos-fiscales",
    "/es_ar/buenas-practicas-para-el-consumo-de-apis",
    "/es_ar/reportes-de-facturacion",
    "/es_ar/provisiones",
    "/es_ar/pagos-reporte",
    "/es_ar/descargas",
    "/es_ar/percepciones",

    # ── Mensajeria y Reclamos ─────────────────────────────────────────────
    "/es_ar/que-es-mensajeria",
    "/es_ar/motivos-para-comunicarse",
    "/es_ar/gestion-de-mensajes",
    "/es_ar/mensajes-pendientes",
    "/es_ar/mensajes-bloqueados",
    "/es_ar/gestionar-reclamos",
    "/es_ar/gestionar-mensajes-de-un-reclamo",
    "/es_ar/gestionar-resolucion-de-reclamos",
    "/es_ar/gestionar-evidencia-de-reclamos",
    "/es_ar/errores-reclamos",
    "/es_ar/devoluciones",
    "/es_ar/cambios",

    # ── Metricas y Tendencias ─────────────────────────────────────────────
    "/es_ar/programa-de-despegue",
    "/es_ar/reputacion-de-vendedores",
    "/es_ar/tendencias",
    "/es_ar/mas-vendidos-en-mercado-libre",
    "/es_ar/opiniones-de-productos",
    "/es_ar/calidad-de-publicaciones",
    "/es_ar/experiencia-de-compra",
    "/es_ar/visitas",
    "/es_ar/carga-de-atributos",

    # ── Notificaciones ────────────────────────────────────────────────────
    "/es_ar/notificaciones",
    "/es_ar/comunicaciones",

    # ── Brand Protection ──────────────────────────────────────────────────
    "/es_ar/que-es-brand-protection-program",
    "/es_ar/miembros-del-programa",
    "/es_ar/publicaciones-denunciadas",

    # ── MercadoLider y Tiendas Oficiales ──────────────────────────────────
    "/es_ar/tiendas-oficiales",

    # ── Mercado Ads ───────────────────────────────────────────────────────
    "/es_ar/introduccion-mercado-ads",
    "/es_ar/product-ads",
    "/es_ar/brand-ads",
    "/es_ar/display-ads",
    "/es_ar/product-ads-catalogo-user-products",
    "/es_ar/bonificaciones-para-product-ads",

    # ── Seguridad ─────────────────────────────────────────────────────────
    "/es_ar/developer-partner-program",
    "/es_ar/seguridad-desarrollo-seguro",

    # ── MCP ───────────────────────────────────────────────────────────────
    "/es_ar/mercado-libre-mcp-server",

    # ── Guia Inmuebles ────────────────────────────────────────────────────
    "/es_ar/introduccion-inmuebles",
    "/es_ar/primeros-pasos-inmuebles",
    "/es_ar/configuracion-requisitos-previos",
    "/es_ar/obtencion-access-token",
    "/es_ar/consulta-usuarios-inmuebles",
    "/es_ar/pasos-rapidos-para-publicar",
    "/es_ar/categorias-y-atributos-inmuebles",
    "/es_ar/localizar-inmuebles",
    "/es_ar/gestionar-paquetes",
    "/es_ar/contratacion-de-paquetes",
    "/es_ar/paquetes-y-permisos-proyectos",
    "/es_ar/publica-inmuebles",
    "/es_ar/publicaciones-tiendas-oficiales-inmuebles",
    "/es_ar/actualiza-publicaciones-inmuebles",
    "/es_ar/ciclo-de-vida-publicaciones",
    "/es_ar/variaciones-inmuebles",
    "/es_ar/actualizacion-variacion-inmuebles",
    "/es_ar/calidad-publicaciones-inmuebles",
    "/es_ar/desarrollos-inmobiliarios",
    "/es_ar/leads",
    "/es_ar/solicitud-de-visita",
    "/es_ar/estadisticas-de-interacciones",

    # ── Guia Vehiculos ────────────────────────────────────────────────────
    "/es_ar/introduccion-vehiculos",
    "/es_ar/consulta-usuarios-vehiculos",
    "/es_ar/categorias-y-atributos-vehiculos",
    "/es_ar/localiza-vehiculos",
    "/es_ar/gestiona-paquetes-vehiculos",
    "/es_ar/publica-vehiculos",
    "/es_ar/sincroniza-publicaciones-vehiculos",
    "/es_ar/gestiona-preguntas-contactos",
    "/es_ar/personas-interesadas",
    "/es_ar/creditos-pre-aprobados",
    "/es_ar/calidad-publicaciones-vehiculos",

    # ── Guia Servicios ────────────────────────────────────────────────────
    "/es_ar/introduccion-servicios",
    "/es_ar/consulta-usuarios-servicios",
    "/es_ar/elige-tipo-de-servicio",
    "/es_ar/administra-areas-de-cobertura",
    "/es_ar/publica-servicios",
    "/es_ar/sincroniza-publicaciones-servicios",
    "/es_ar/consultas-avanzadas",
]

VISITED = set()


def clean_html(soup: BeautifulSoup) -> str:
    """Extrae texto limpio de HTML, preservando estructura."""
    # Remover scripts, styles, nav, footer, header
    for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header',
                               'aside', 'meta', 'link', 'noscript']):
        tag.decompose()

    # Contenedores de contenido (por orden de preferencia)
    content_candidates = [
        soup.find('article'),
        soup.find('main'),
        soup.find(class_=re.compile(r'content|article|doc|post|body', re.I)),
        soup.find(id=re.compile(r'content|article|main|doc', re.I)),
        soup.find('div', class_=re.compile(r'markdown|prose', re.I)),
    ]

    target = None
    for c in content_candidates:
        if c:
            target = c
            break

    if not target:
        target = soup.find('body') or soup

    # Convertir a texto preservando estructura
    lines = []
    for el in target.descendants:
        if el.name in ('h1', 'h2', 'h3', 'h4', 'h5'):
            level = int(el.name[1])
            lines.append('\n' + '#' * level + ' ' + el.get_text(strip=True))
        elif el.name == 'p':
            text = el.get_text(strip=True)
            if text:
                lines.append('\n' + text)
        elif el.name == 'li':
            text = el.get_text(strip=True)
            if text:
                lines.append('- ' + text)
        elif el.name == 'code':
            text = el.get_text(strip=True)
            if text and len(text) < 300:
                lines.append(f'`{text}`')
        elif el.name == 'pre':
            text = el.get_text(strip=True)
            if text:
                lines.append(f'\n```\n{text}\n```')
        elif el.name == 'table':
            # Convertir tabla a markdown
            rows = el.find_all('tr')
            for i, row in enumerate(rows):
                cells = [td.get_text(strip=True) for td in row.find_all(['th', 'td'])]
                lines.append('| ' + ' | '.join(cells) + ' |')
                if i == 0:
                    lines.append('|' + '---|' * len(cells))

    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def extract_internal_links(soup: BeautifulSoup, current_url: str) -> list:
    """Extrae links internos de la documentacion."""
    links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        # Links relativos o del mismo dominio
        if href.startswith('/es_ar/') or href.startswith('./') or href.startswith('../'):
            full = urljoin(BASE_URL, href.split('?')[0].split('#')[0])
            if full not in VISITED and '/es_ar/' in full:
                links.append(full)
        elif BASE_URL in href and '/es_ar/' in href:
            full = href.split('?')[0].split('#')[0]
            if full not in VISITED:
                links.append(full)
    return list(set(links))


async def fetch_page(client: httpx.AsyncClient, url: str) -> dict | None:
    """Descarga y parsea una pagina de documentacion."""
    try:
        resp = await client.get(url, timeout=20, follow_redirects=True)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Titulo
        title_tag = soup.find('h1') or soup.find('title')
        title = title_tag.get_text(strip=True) if title_tag else urlparse(url).path.split('/')[-1]
        title = title.replace(' - Mercado Libre Developers', '').strip()

        content = clean_html(soup)
        if len(content) < 200:
            return None

        links = extract_internal_links(soup, url)

        return {
            'url': url,
            'title': title,
            'content': content,
            'links': links,
        }

    except Exception as e:
        return None


async def scrape_all() -> list:
    """Scraper principal con concurrencia controlada."""
    # Construir cola inicial
    queue = []
    for path in ALL_URLS:
        url = urljoin(BASE_URL, path) if path.startswith('/') else path
        if url not in VISITED:
            queue.append(url)
    queue = list(dict.fromkeys(queue))  # dedup

    scraped = []
    semaphore = asyncio.Semaphore(CONCURRENCY)

    print(f"\n{'='*60}")
    print(f"  Mercado Libre Developer Docs Scraper")
    print(f"  {len(queue)} URLs iniciales | max {MAX_PAGES} paginas | {CONCURRENCY} paralelas")
    print(f"{'='*60}\n")

    async with httpx.AsyncClient(headers=HEADERS) as client:

        async def fetch_one(url: str):
            async with semaphore:
                if url in VISITED:
                    return None
                VISITED.add(url)
                result = await fetch_page(client, url)
                await asyncio.sleep(0.15)  # pausa respetuosa
                return result

        # Procesar en lotes para ir mostrando progreso
        batch_size = 20
        while queue and len(scraped) < MAX_PAGES:
            batch = []
            while queue and len(batch) < batch_size:
                url = queue.pop(0)
                if url not in VISITED:
                    batch.append(url)

            if not batch:
                break

            print(f"[Lote] Procesando {len(batch)} URLs... ({len(scraped)} guardadas hasta ahora)")
            tasks = [fetch_one(url) for url in batch]
            results = await asyncio.gather(*tasks)

            for result in results:
                if result and len(result['content']) > 200:
                    scraped.append(result)
                    # Agregar links nuevos a la cola
                    for link in result['links']:
                        if link not in VISITED and link not in queue and len(queue) < 500:
                            queue.append(link)
                    print(f"  [OK] '{result['title'][:55]}' ({len(result['content'])} chars)")

    print(f"\n  Total scrapeado: {len(scraped)} paginas")
    return scraped


def categorize(item: dict) -> str:
    """Clasifica una pagina en su categoria tematica."""
    url = item['url'].lower()
    title = item['title'].lower()
    text = url + ' ' + title

    categories = [
        ('auth',         ['autenticacion', 'autorizacion', 'token', 'oauth', 'aplicacion', 'pkce', 'permisos']),
        ('items',        ['item', 'publicacion', 'listing', 'producto', 'catalogo', 'variacion', 'imagen', 'atributo', 'moderacion']),
        ('precios',      ['precio', 'costo', 'comision', 'tarifa', 'descuento', 'promocion', 'oferta', 'cupon']),
        ('envios',       ['envio', 'mercado-envio', 'fulfillment', 'flex', 'turbo', 'logistica', 'paquete', 'flete']),
        ('ventas',       ['orden', 'venta', 'pago', 'feedback', 'reclamo', 'devolucion', 'cambio', 'mensajeria', 'pack']),
        ('metricas',     ['reputacion', 'metrica', 'tendencia', 'visita', 'calidad', 'experiencia', 'estadistica']),
        ('facturacion',  ['factur', 'reporte', 'provision', 'percepcion', 'fiscal', 'descarga']),
        ('ads',          ['ads', 'publicidad', 'product-ad', 'brand-ad', 'display', 'bonificacion']),
        ('notificaciones',['notificacion', 'comunicacion', 'webhook']),
        ('usuarios',     ['usuario', 'vendedor', 'comprador', 'validar', 'direccion', 'favorito']),
        ('categorias',   ['categoria', 'dominio', 'ubicacion', 'moneda', 'busqueda']),
        ('brand',        ['brand', 'tienda-oficial', 'mercadolider', 'proteccion']),
        ('seguridad',    ['seguridad', 'partner', 'developer', 'mcp']),
        ('inmuebles',    ['inmueble', 'lead', 'visita', 'desarrollo-inmobiliario']),
        ('vehiculos',    ['vehiculo', 'auto', 'moto', 'credito-pre']),
        ('servicios',    ['servicio', 'cobertura']),
    ]

    for cat, keywords in categories:
        for kw in keywords:
            if kw in text:
                return cat
    return 'general'


def save_results(scraped: list):
    """Guarda resultados organizados por categoria."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Categorizar paginas
    by_category = {}
    for item in scraped:
        cat = categorize(item)
        by_category.setdefault(cat, []).append(item)

    # Guardar un archivo MD por categoria
    cat_files = {}
    for cat, items in sorted(by_category.items()):
        cat_path = OUTPUT_DIR / f"meli-{cat}.md"
        with open(cat_path, 'w', encoding='utf-8') as f:
            f.write(f"# Mercado Libre API — {cat.upper()}\n\n")
            f.write(f"Paginas: {len(items)} | Generado: {time.strftime('%Y-%m-%d %H:%M')}\n\n---\n\n")
            for item in items:
                f.write(f"## {item['title']}\n\n")
                f.write(f"> {item['url']}\n\n")
                f.write(item['content'])
                f.write("\n\n---\n\n")
        cat_files[cat] = str(cat_path)
        print(f"  {cat:15s} → {len(items):3d} paginas → {cat_path.name}")

    # Archivo consolidado principal
    main_path = OUTPUT_DIR / "meli-knowledge-complete.md"
    with open(main_path, 'w', encoding='utf-8') as f:
        f.write("# Mercado Libre — Base de Conocimiento Completa\n\n")
        f.write(f"Scrapeado: {time.strftime('%Y-%m-%d %H:%M')} | Total: {len(scraped)} paginas\n\n")

        # Resumen de categorias
        f.write("## Indice\n\n")
        for cat, items in sorted(by_category.items()):
            f.write(f"- **{cat.upper()}**: {len(items)} paginas\n")
        f.write("\n---\n\n")

        # Orden logico para el agente (de mas a menos importante para ventas)
        priority_order = [
            'auth', 'items', 'precios', 'ventas', 'envios',
            'ads', 'metricas', 'facturacion', 'notificaciones',
            'usuarios', 'categorias', 'brand', 'seguridad',
            'general', 'inmuebles', 'vehiculos', 'servicios'
        ]
        for cat in priority_order:
            if cat in by_category:
                for item in by_category[cat]:
                    f.write(f"## [{cat.upper()}] {item['title']}\n\n")
                    f.write(f"> {item['url']}\n\n")
                    f.write(item['content'])
                    f.write("\n\n---\n\n")

    # Indice JSON
    index = {
        'scraped_at': time.strftime('%Y-%m-%d %H:%M'),
        'total': len(scraped),
        'by_category': {
            cat: [{'title': x['title'], 'url': x['url'], 'chars': len(x['content'])}
                  for x in items]
            for cat, items in by_category.items()
        }
    }
    with open(OUTPUT_DIR / "meli-index.json", 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"  GUARDADO COMPLETO")
    print(f"  {len(scraped)} paginas en {len(by_category)} categorias")
    print(f"  Archivo principal: {main_path}")
    print(f"{'='*60}")
    return main_path


async def main():
    start = time.time()
    scraped = await scrape_all()

    print(f"\n[Guardando resultados...]")
    save_results(scraped)

    elapsed = time.time() - start
    print(f"\n  Tiempo total: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"\n  SIGUIENTE PASO:")
    print(f"  python3.13 update_meli_agent.py")


if __name__ == '__main__':
    asyncio.run(main())
