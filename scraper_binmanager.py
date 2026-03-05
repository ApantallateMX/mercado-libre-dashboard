"""
BinManager Full Scraper + API Discovery
=========================================
1. Login automatico con credenciales MIT Technologies
2. Explora TODOS los menus y secciones del sistema
3. Intercepta llamadas de red para mapear endpoints internos
4. Captura formularios, parametros y estructuras de datos
5. Genera documentacion completa para el agente binmanager-specialist

USO: python3.13 scraper_binmanager.py
Luego: python3.13 update_bm_agent.py
"""

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

from playwright.async_api import async_playwright, Page, Route, Request

BASE_URL   = "https://binmanager.mitechnologiesinc.com"
OUTPUT_DIR = Path("Agentes/bm-knowledge")

CREDENTIALS = {
    "email":    "jovan.rodriguez@mitechnologiesinc.com",
    "password": "123456",
    "company":  1,
}

# Rutas conocidas a explorar
KNOWN_ROUTES = [
    "/User/Admin",
    "/User/Login",
    "/Home/Index",
    "/Home/Dashboard",
    "/InventoryReport",
    "/InventoryReport/Index",
    "/InventoryReport/InventoryReport",
    "/Product",
    "/Product/Index",
    "/Product/Products",
    "/Order",
    "/Order/Index",
    "/Order/Orders",
    "/Warehouse",
    "/Warehouse/Index",
    "/Location",
    "/Location/Index",
    "/Bin",
    "/Bin/Index",
    "/Supplier",
    "/Supplier/Index",
    "/Transfer",
    "/Transfer/Index",
    "/Receiving",
    "/Receiving/Index",
    "/Shipping",
    "/Shipping/Index",
    "/Report",
    "/Report/Index",
    "/Settings",
    "/Settings/Index",
    "/User",
    "/User/Index",
]

# Registro global
API_CALLS    = []   # todas las llamadas de red interceptadas
PAGES_DATA   = []   # contenido de paginas visitadas
VISITED_URLS = set()
FORMS_FOUND  = []   # formularios y sus campos


def clean_text(text: str) -> str:
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


async def setup_network_interceptor(page: Page):
    """Intercepta TODAS las llamadas de red para descubrir la API."""

    async def handle_request(request: Request):
        url = request.url
        method = request.method
        # Solo capturar llamadas interesantes (no assets)
        if any(ext in url for ext in ['.css', '.js', '.png', '.jpg', '.gif', '.woff', '.ico']):
            return
        if any(skip in url for skip in ['google', 'analytics', 'cdn', 'jquery']):
            return

        record = {
            'method': method,
            'url': url,
            'path': urlparse(url).path,
            'headers': dict(request.headers),
            'post_data': None,
            'timestamp': time.strftime('%H:%M:%S'),
        }

        # Capturar body de POST/PUT
        if method in ('POST', 'PUT', 'PATCH'):
            try:
                post_data = request.post_data
                if post_data:
                    try:
                        record['post_data'] = json.loads(post_data)
                    except Exception:
                        record['post_data'] = post_data[:500]
            except Exception:
                pass

        API_CALLS.append(record)

    page.on('request', handle_request)


async def login(page: Page) -> bool:
    """Login automatico en BinManager."""
    print("\n[LOGIN] Iniciando sesion en BinManager...")

    try:
        await page.goto(f"{BASE_URL}/User/Admin", wait_until='domcontentloaded', timeout=20000)
        await asyncio.sleep(1)

        # Buscar campos de login
        # Intentar con email
        email_selectors = [
            'input[name="Email"]',
            'input[name="email"]',
            'input[name="UserName"]',
            'input[name="username"]',
            'input[type="email"]',
            'input[type="text"]',
        ]
        password_selectors = [
            'input[name="Password"]',
            'input[name="password"]',
            'input[type="password"]',
        ]

        email_field = None
        for sel in email_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    email_field = el
                    break
            except Exception:
                continue

        password_field = None
        for sel in password_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    password_field = el
                    break
            except Exception:
                continue

        if not email_field or not password_field:
            print("  [!] No se encontraron campos de login")
            return False

        await email_field.fill(CREDENTIALS['email'])
        await password_field.fill(CREDENTIALS['password'])

        # Buscar boton de submit
        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Sign In")',
            'button:has-text("Login")',
            'button:has-text("Ingresar")',
            '.btn-primary',
        ]
        for sel in submit_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click()
                    break
            except Exception:
                continue

        await asyncio.sleep(3)
        await page.wait_for_load_state('networkidle')

        current = page.url
        if 'login' in current.lower() or 'admin' in current.lower():
            # Puede que redirigiera al dashboard
            title = await page.title()
            if 'login' in title.lower():
                print(f"  [!] Login fallido — aun en pagina de login")
                return False

        print(f"  [OK] Login exitoso — URL: {page.url}")
        return True

    except Exception as e:
        print(f"  [ERROR] Login: {e}")
        return False


async def extract_page_info(page: Page, url: str) -> dict:
    """Extrae informacion completa de una pagina."""
    info = {
        'url': url,
        'title': '',
        'content': '',
        'menus': [],
        'forms': [],
        'tables': [],
        'buttons': [],
        'links': [],
    }

    try:
        title = await page.title()
        info['title'] = title.replace(' | BinManager | Mi Technologies Inc.', '').strip()

        # Menus de navegacion
        nav_links = await page.eval_on_selector_all(
            'nav a, .navbar a, .sidebar a, .menu a, [class*="nav"] a',
            'els => els.map(e => ({text: e.innerText.trim(), href: e.getAttribute("href")}))'
        )
        info['menus'] = [l for l in nav_links if l['text'] and l['href']]

        # Formularios y sus campos
        forms = await page.eval_on_selector_all('form', '''forms => forms.map(form => ({
            action: form.getAttribute("action"),
            method: form.getAttribute("method") || "GET",
            fields: Array.from(form.querySelectorAll("input, select, textarea")).map(f => ({
                name: f.getAttribute("name"),
                type: f.getAttribute("type") || f.tagName.toLowerCase(),
                id: f.getAttribute("id"),
                placeholder: f.getAttribute("placeholder"),
                required: f.hasAttribute("required"),
                options: f.tagName === "SELECT"
                    ? Array.from(f.options).map(o => ({value: o.value, text: o.text}))
                    : []
            })).filter(f => f.name || f.id)
        }))''')
        info['forms'] = forms
        FORMS_FOUND.extend(forms)

        # Tablas (estructura de datos)
        tables = await page.eval_on_selector_all('table', '''tables => tables.map(t => {
            const headers = Array.from(t.querySelectorAll("th")).map(h => h.innerText.trim());
            const rows = Array.from(t.querySelectorAll("tbody tr")).slice(0, 3).map(r =>
                Array.from(r.querySelectorAll("td")).map(c => c.innerText.trim().substring(0, 80))
            );
            return {headers, sample_rows: rows};
        })''')
        info['tables'] = [t for t in tables if t['headers']]

        # Botones con acciones
        buttons = await page.eval_on_selector_all(
            'button, a.btn, input[type="button"], input[type="submit"]',
            'els => els.map(e => ({text: e.innerText?.trim() || e.value, onclick: e.getAttribute("onclick"), href: e.getAttribute("href")}))'
        )
        info['buttons'] = [b for b in buttons if b['text'] and len(b['text']) < 60][:30]

        # Links internos
        links = await page.eval_on_selector_all(
            f'a[href*="{urlparse(BASE_URL).netloc}"], a[href^="/"]',
            'els => els.map(e => ({text: e.innerText.trim(), href: e.getAttribute("href")}))'
        )
        info['links'] = [l for l in links if l['href'] and l['text']]

        # Contenido de texto principal
        text_content = await page.inner_text('body')
        info['content'] = clean_text(text_content)[:8000]

    except Exception as e:
        info['error'] = str(e)

    return info


async def explore_section(page: Page, url: str, depth: int = 0) -> list:
    """Explora una seccion del sistema recursivamente."""
    if url in VISITED_URLS or depth > 2:
        return []
    VISITED_URLS.add(url)

    results = []
    try:
        print(f"  {'  '*depth}→ {url.replace(BASE_URL, '')}", end=' ')
        await page.goto(url, wait_until='domcontentloaded', timeout=15000)
        await asyncio.sleep(1.5)

        # Verificar que no redirigió a login
        if 'login' in page.url.lower() or 'User/Admin' in page.url:
            print("(requiere auth)")
            return []

        info = await extract_page_info(page, url)
        results.append(info)
        print(f"OK — '{info['title']}'")

        # Seguir links internos del mismo sistema
        if depth < 2:
            for link in info['links']:
                href = link['href']
                if href and href.startswith('/') and BASE_URL not in href:
                    full_url = urljoin(BASE_URL, href.split('?')[0])
                elif href and BASE_URL in href:
                    full_url = href.split('?')[0]
                else:
                    continue

                if full_url not in VISITED_URLS and BASE_URL in full_url:
                    sub = await explore_section(page, full_url, depth + 1)
                    results.extend(sub)
                    await asyncio.sleep(0.5)

    except Exception as e:
        print(f"ERROR: {e}")

    return results


async def discover_api_via_interaction(page: Page):
    """
    Interactua con elementos clave para descubrir endpoints de API.
    Hace clic en menus, carga datos, ejecuta busquedas.
    """
    print("\n[API DISCOVERY] Interactuando con el sistema para descubrir endpoints...")

    # Acciones que tipicamente generan llamadas de API
    discovery_actions = [
        # Cargar inventario
        {
            'name': 'Inventory Report',
            'goto': f"{BASE_URL}/InventoryReport",
            'wait': 3,
        },
        {
            'name': 'Inventory by SKU (SNTV001763)',
            'goto': f"{BASE_URL}/InventoryReport",
            'script': '''
                // Buscar un SKU conocido
                const inputs = document.querySelectorAll("input");
                for (let inp of inputs) {
                    if (inp.type === "text" || inp.placeholder?.toLowerCase().includes("sku")) {
                        inp.value = "SNTV001763";
                        inp.dispatchEvent(new Event("change", {bubbles: true}));
                        inp.dispatchEvent(new Event("input", {bubbles: true}));
                        break;
                    }
                }
                // Buscar boton de busqueda
                const btns = document.querySelectorAll("button, input[type='submit']");
                for (let btn of btns) {
                    if (btn.innerText?.toLowerCase().includes("search") ||
                        btn.innerText?.toLowerCase().includes("buscar") ||
                        btn.innerText?.toLowerCase().includes("get")) {
                        btn.click();
                        break;
                    }
                }
            ''',
            'wait': 3,
        },
        # Abrir seccion de productos
        {
            'name': 'Products Section',
            'goto': f"{BASE_URL}/Product",
            'wait': 2,
        },
        # Ordenes
        {
            'name': 'Orders Section',
            'goto': f"{BASE_URL}/Order",
            'wait': 2,
        },
        # Warehouses
        {
            'name': 'Warehouse Section',
            'goto': f"{BASE_URL}/Warehouse",
            'wait': 2,
        },
        # Locations
        {
            'name': 'Locations Section',
            'goto': f"{BASE_URL}/Location",
            'wait': 2,
        },
        # Reportes
        {
            'name': 'Reports Section',
            'goto': f"{BASE_URL}/Report",
            'wait': 2,
        },
        # Settings
        {
            'name': 'Settings',
            'goto': f"{BASE_URL}/Settings",
            'wait': 2,
        },
    ]

    for action in discovery_actions:
        try:
            print(f"  Explorando: {action['name']}...")
            if 'goto' in action:
                await page.goto(action['goto'], wait_until='domcontentloaded', timeout=10000)

            if 'script' in action:
                await page.evaluate(action['script'])

            await asyncio.sleep(action.get('wait', 2))
            await page.wait_for_load_state('networkidle', timeout=5000)

        except Exception as e:
            print(f"    [skip] {e}")
            continue


def analyze_api_calls() -> dict:
    """Analiza y organiza las llamadas de API capturadas."""
    endpoints = {}

    for call in API_CALLS:
        path = call['path']
        method = call['method']

        # Filtrar paths no relevantes
        if not path or path in ('/', '/favicon.ico'):
            continue
        if any(ext in path for ext in ['.css', '.js', '.png', '.jpg', '.gif']):
            continue

        key = f"{method} {path}"
        if key not in endpoints:
            endpoints[key] = {
                'method': method,
                'path': path,
                'full_url': call['url'],
                'calls': 0,
                'post_bodies': [],
                'category': categorize_endpoint(path),
            }

        endpoints[key]['calls'] += 1
        if call.get('post_data') and call['post_data'] not in endpoints[key]['post_bodies']:
            endpoints[key]['post_bodies'].append(call['post_data'])

    # Ordenar por categoria
    by_category = {}
    for key, ep in endpoints.items():
        cat = ep['category']
        by_category.setdefault(cat, []).append(ep)

    return by_category


def categorize_endpoint(path: str) -> str:
    path = path.lower()
    if 'inventory' in path:   return 'inventory'
    if 'product' in path:     return 'products'
    if 'order' in path:       return 'orders'
    if 'warehouse' in path:   return 'warehouse'
    if 'location' in path:    return 'location'
    if 'supplier' in path:    return 'supplier'
    if 'transfer' in path:    return 'transfer'
    if 'receiv' in path:      return 'receiving'
    if 'ship' in path:        return 'shipping'
    if 'report' in path:      return 'reports'
    if 'user' in path:        return 'users'
    if 'setting' in path:     return 'settings'
    if 'bin' in path:         return 'bins'
    return 'other'


def generate_markdown(pages_data: list, api_by_cat: dict, forms: list) -> str:
    """Genera documentacion completa en markdown."""
    lines = [
        "# BinManager — Documentacion Completa del Sistema",
        "",
        f"Generado: {time.strftime('%Y-%m-%d %H:%M')}",
        f"Sistema: {BASE_URL}",
        f"Paginas exploradas: {len(pages_data)}",
        f"Endpoints descubiertos: {sum(len(v) for v in api_by_cat.values())}",
        "",
        "---",
        "",
    ]

    # ── Seccion 1: Navegacion y estructura del sistema ────────────────────
    lines += [
        "## 1. ESTRUCTURA DEL SISTEMA",
        "",
        "### Paginas y Secciones Disponibles",
        "",
    ]
    for page_info in pages_data:
        if not page_info.get('title'):
            continue
        lines += [
            f"#### {page_info['title']}",
            f"**URL:** {page_info['url']}",
            "",
        ]
        if page_info.get('buttons'):
            btns = [b['text'] for b in page_info['buttons'] if b['text']][:15]
            if btns:
                lines.append(f"**Acciones disponibles:** {', '.join(btns)}")
                lines.append("")
        if page_info.get('tables'):
            for t in page_info['tables']:
                if t['headers']:
                    lines.append(f"**Tabla:** {' | '.join(t['headers'])}")
                    if t['sample_rows']:
                        lines.append(f"**Ejemplo:** {' | '.join(t['sample_rows'][0])}")
                    lines.append("")
        lines.append("")

    # ── Seccion 2: API Endpoints descubiertos ─────────────────────────────
    lines += [
        "---",
        "",
        "## 2. ENDPOINTS DE API DESCUBIERTOS",
        "",
        "> Capturados interceptando las llamadas de red del navegador.",
        "> Estos son los endpoints reales que usa la interfaz de BinManager.",
        "",
    ]

    cat_labels = {
        'inventory':  'Inventario',
        'products':   'Productos',
        'orders':     'Ordenes',
        'warehouse':  'Almacenes',
        'location':   'Ubicaciones',
        'supplier':   'Proveedores',
        'transfer':   'Transferencias',
        'receiving':  'Recepcion',
        'shipping':   'Envios',
        'reports':    'Reportes',
        'users':      'Usuarios',
        'settings':   'Configuracion',
        'bins':       'Bins',
        'other':      'Otros',
    }

    for cat, label in cat_labels.items():
        endpoints = api_by_cat.get(cat, [])
        if not endpoints:
            continue

        lines += [f"### {label}", ""]
        for ep in endpoints:
            lines.append(f"**`{ep['method']} {ep['path']}`**")
            lines.append(f"- URL completa: `{ep['full_url']}`")
            lines.append(f"- Llamadas capturadas: {ep['calls']}")
            if ep['post_bodies']:
                lines.append(f"- Payload ejemplo:")
                for body in ep['post_bodies'][:2]:
                    if isinstance(body, dict):
                        lines.append(f"  ```json")
                        lines.append(f"  {json.dumps(body, ensure_ascii=False, indent=2)[:500]}")
                        lines.append(f"  ```")
                    else:
                        lines.append(f"  `{str(body)[:200]}`")
            lines.append("")

    # ── Seccion 3: Formularios ─────────────────────────────────────────────
    if forms:
        lines += [
            "---",
            "",
            "## 3. FORMULARIOS Y CAMPOS",
            "",
        ]
        seen_forms = set()
        for form in forms:
            if not form.get('fields'):
                continue
            form_key = form.get('action', 'unknown')
            if form_key in seen_forms:
                continue
            seen_forms.add(form_key)

            lines.append(f"### Formulario: `{form.get('method', 'GET').upper()} {form.get('action', 'N/A')}`")
            lines.append("")
            lines.append("| Campo | Tipo | Requerido | Placeholder |")
            lines.append("|-------|------|-----------|-------------|")
            for field in form['fields']:
                req = "Si" if field.get('required') else "No"
                ph = field.get('placeholder', '')
                lines.append(f"| {field.get('name', '')} | {field.get('type', '')} | {req} | {ph} |")
                if field.get('options'):
                    opts = [f"{o['value']}={o['text']}" for o in field['options'][:10]]
                    lines.append(f"| *(opciones)* | | | {', '.join(opts)} |")
            lines.append("")

    # ── Seccion 4: Endpoints conocidos (pre-documentados) ─────────────────
    lines += [
        "---",
        "",
        "## 4. ENDPOINTS DOCUMENTADOS (VERIFICADOS)",
        "",
        "Estos endpoints han sido verificados y documentados previamente:",
        "",
        "### Autenticacion",
        "```",
        "POST /User/LoginUserByEmail",
        'Body: {"Email": "jovan.rodriguez@mitechnologiesinc.com", "Password": "123456", "COMPANYID": 1}',
        "Retorna: datos de usuario + cookie ASP.NET_SessionId",
        "```",
        "",
        "### Stock por Almacen (totales fisicos)",
        "```",
        "POST /InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse",
        'Body: {',
        '  "COMPANYID": 1,',
        '  "SKU": "<base_sku>",',
        '  "WarehouseID": null,',
        '  "LocationID": "47,62,68",',
        '  "BINID": null,',
        '  "Condition": "GRA,GRB,GRC,NEW",',
        '  "ForInventory": 0,',
        '  "SUPPLIERS": null',
        '}',
        "Retorna: filas con WarehouseName y QtyTotal",
        "```",
        "",
        "### Stock Disponible Real (excluye reservados)",
        "```",
        "POST /InventoryReport/InventoryBySKUAndCondicion_Quantity",
        'Body: {',
        '  "COMPANYID": 1,',
        '  "TYPEINVENTORY": 0,',
        '  "WAREHOUSEID": null,',
        '  "LOCATIONID": "47,62,68",',
        '  "BINID": null,',
        '  "PRODUCTSKU": "<base_sku>",',
        '  "CONDITION": "GRA,GRB,GRC,NEW",',
        '  "SUPPLIERS": null,',
        '  "LCN": null,',
        '  "SEARCH": "<base_sku>"',
        '}',
        "Retorna: Available (libre), Required (reservado), TotalQty",
        "```",
        "",
        "### Metadata de Producto",
        "```",
        "POST /InventoryReport/InventoryReport",
        'Body: {"COMPANYID": 1, "SKU": "<base_sku>", ...}',
        "Retorna: Brand, Model, AvgCostQTY, RetailPrice",
        "NUNCA usar AvailableQTY de este endpoint para stock",
        "```",
        "",
        "### Historial Precio Retail (Purchase History)",
        "```",
        "POST /InventoryReport/InventoryReport/GetRetailPriceHistoryBySku",
        'Body: {"SKU": "<base_sku>", "COMPANYID": 1, "Condition": null}',
        "Requiere sesion autenticada (cookie)",
        "Retorna: PurchaseRetail, LoadDate, Supplier, MITLoadID",
        "```",
        "",
        "### LocationIDs Fijos",
        "| ID | Almacen | Codigo | Vendible |",
        "|----|---------|--------|----------|",
        "| 47 | CDMX ALMACEN 2 — Autobot/Ebanistas | CDMX | SI |",
        "| 62 | Tijuana | TJ | NO (solo informativo) |",
        "| 68 | MTY-02 MAXX — Monterrey MAXX | MTY | SI |",
        "",
        "### Condiciones de Inventario",
        "| Condicion | Descripcion | Usar en listing regular | Usar en listing IC |",
        "|-----------|-------------|------------------------|-------------------|",
        "| GRA | Grado A | SI | SI |",
        "| GRB | Grado B | SI | SI |",
        "| GRC | Grado C | SI | SI |",
        "| ICB | Incompleto B | NO | SI |",
        "| ICC | Incompleto C/Danado | NO | SI |",
        "| NEW | Nuevo | SI | SI |",
        "",
    ]

    # ── Seccion 5: Contenido de paginas ───────────────────────────────────
    lines += [
        "---",
        "",
        "## 5. CONTENIDO DE PANTALLAS",
        "",
    ]
    for page_info in pages_data:
        if page_info.get('content') and len(page_info['content']) > 200:
            lines += [
                f"### {page_info.get('title', 'Sin titulo')}",
                f"> URL: {page_info['url']}",
                "",
                page_info['content'][:3000],
                "",
            ]

    return '\n'.join(lines)


def save_results(pages_data: list, api_by_cat: dict, forms: list):
    """Guarda toda la documentacion generada."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Documentacion principal
    md_content = generate_markdown(pages_data, api_by_cat, forms)
    main_path = OUTPUT_DIR / "binmanager-knowledge.md"
    main_path.write_text(md_content, encoding='utf-8')

    # Endpoints en JSON (para referencia tecnica)
    api_path = OUTPUT_DIR / "binmanager-api-endpoints.json"
    api_path.write_text(
        json.dumps(api_by_cat, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    # Raw API calls log
    calls_path = OUTPUT_DIR / "binmanager-api-calls-raw.json"
    calls_path.write_text(
        json.dumps(API_CALLS, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    # Indice
    index = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M'),
        'pages_explored': len(pages_data),
        'endpoints_discovered': sum(len(v) for v in api_by_cat.values()),
        'forms_found': len(forms),
        'api_categories': {cat: len(eps) for cat, eps in api_by_cat.items()},
    }
    (OUTPUT_DIR / "bm-index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    print(f"\n{'='*60}")
    print(f"  GUARDADO COMPLETO en {OUTPUT_DIR}/")
    print(f"  Paginas exploradas : {len(pages_data)}")
    print(f"  Endpoints API      : {sum(len(v) for v in api_by_cat.values())}")
    print(f"  Formularios        : {len(forms)}")
    print(f"  Doc principal      : {main_path}")
    print(f"{'='*60}")
    return main_path


async def main():
    print("=" * 60)
    print("  BinManager Full Scraper + API Discovery")
    print("  MIT Technologies Inc.")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=['--start-maximized']
        )
        context = await browser.new_context(
            viewport={'width': 1440, 'height': 900},
            locale='es-MX',
        )
        page = await context.new_page()

        # Activar interceptor de red ANTES de navegar
        await setup_network_interceptor(page)

        # ── LOGIN ─────────────────────────────────────────────────────────
        logged_in = await login(page)
        if not logged_in:
            print("\n[!] Login automatico fallido.")
            print("    Intentando login manual...")
            print("    Por favor inicia sesion en el navegador y presiona ENTER aqui.")
            input("    >>> ENTER cuando hayas iniciado sesion: ")

        # ── EXPLORACION DE RUTAS CONOCIDAS ────────────────────────────────
        print(f"\n[EXPLORE] Explorando {len(KNOWN_ROUTES)} rutas conocidas...")
        pages_data = []
        for route in KNOWN_ROUTES:
            url = urljoin(BASE_URL, route)
            if url not in VISITED_URLS:
                results = await explore_section(page, url, depth=0)
                pages_data.extend(results)
                await asyncio.sleep(0.5)

        # ── DESCUBRIMIENTO DE API VIA INTERACCION ─────────────────────────
        await discover_api_via_interaction(page)

        # ── ANALISIS DE LLAMADAS CAPTURADAS ───────────────────────────────
        print(f"\n[ANALYSIS] Analizando {len(API_CALLS)} llamadas de red capturadas...")
        api_by_cat = analyze_api_calls()
        total_endpoints = sum(len(v) for v in api_by_cat.values())
        print(f"  Endpoints unicos descubiertos: {total_endpoints}")
        for cat, eps in sorted(api_by_cat.items()):
            print(f"  {cat:15s}: {len(eps)} endpoints")

        # ── GUARDAR ───────────────────────────────────────────────────────
        print(f"\n[SAVE] Guardando documentacion...")
        save_results(pages_data, api_by_cat, FORMS_FOUND)

        await browser.close()

        print(f"\n  SIGUIENTE PASO:")
        print(f"  python3.13 update_bm_agent.py")


if __name__ == '__main__':
    asyncio.run(main())
