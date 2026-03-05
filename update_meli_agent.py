"""
Update MercadoLibre Agent with Scraped Knowledge
==================================================
Inyecta el conocimiento scrapeado de developers.mercadolibre.cl
en el agente mercadolibre-strategist.

USO: python3.13 update_meli_agent.py
"""

import json
import re
import time
from pathlib import Path

KNOWLEDGE_DIR = Path("Agentes/meli-knowledge")
AGENT_FILE    = Path("Agentes/mercadolibre-strategist.md")
MAIN_FILE     = KNOWLEDGE_DIR / "meli-knowledge-complete.md"
INDEX_FILE    = KNOWLEDGE_DIR / "meli-index.json"

# Limite por bloque tematico (caracteres)
MAX_PER_CAT   = 15_000
TOTAL_MAX     = 120_000

# Orden y prioridad de categorias para el agente de ventas
CAT_PRIORITY = [
    ('auth',          'Autenticacion y Tokens',           20_000),
    ('items',         'Items, Publicaciones y Catalogo',  25_000),
    ('precios',       'Precios, Costos y Promociones',    20_000),
    ('ventas',        'Gestion de Ventas y Ordenes',      20_000),
    ('envios',        'Envios y Logistica',               15_000),
    ('ads',           'Mercado Ads — API',                15_000),
    ('metricas',      'Metricas, Reputacion y Calidad',   12_000),
    ('facturacion',   'Facturacion y Reportes',           10_000),
    ('notificaciones','Notificaciones y Webhooks',         8_000),
    ('usuarios',      'Usuarios y Aplicaciones',           8_000),
    ('categorias',    'Categorias, Dominios y Busqueda',   8_000),
    ('brand',         'Brand Protection y Tiendas',        6_000),
    ('seguridad',     'Seguridad y Developer Partner',     5_000),
    ('general',       'General',                           5_000),
]


def parse_sections(md_text: str) -> list[dict]:
    sections = []
    current_title = ''
    current_lines = []
    current_cat = ''

    for line in md_text.split('\n'):
        if line.startswith('## [') and ']' in line:
            # Linea tipo: ## [CAT] Titulo
            if current_lines and current_title:
                content = '\n'.join(current_lines).strip()
                if len(content) > 150:
                    sections.append({
                        'title': current_title,
                        'content': content,
                        'category': current_cat,
                    })
            match = re.match(r'## \[(\w+)\] (.+)', line)
            if match:
                current_cat = match.group(1).lower()
                current_title = match.group(2).strip()
            current_lines = []
        elif line.startswith('## ') and len(line) > 5:
            if current_lines and current_title:
                content = '\n'.join(current_lines).strip()
                if len(content) > 150:
                    sections.append({
                        'title': current_title,
                        'content': content,
                        'category': current_cat,
                    })
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines and current_title:
        content = '\n'.join(current_lines).strip()
        if len(content) > 150:
            sections.append({'title': current_title, 'content': content, 'category': current_cat})

    return sections


def build_agent_knowledge(main_file: Path, index: dict) -> str:
    """Construye el bloque de conocimiento organizado por categoria."""
    if not main_file.exists():
        return "_(Ejecuta scraper_meli_docs.py primero)_\n"

    text = main_file.read_text(encoding='utf-8')
    sections = parse_sections(text)

    # Agrupar por categoria
    by_cat: dict[str, list] = {}
    for s in sections:
        cat = s.get('category', 'general')
        by_cat.setdefault(cat, []).append(s)

    # Construir bloques por categoria respetando limites
    output_blocks = []
    total_chars = 0

    for cat_key, cat_label, cat_max in CAT_PRIORITY:
        items = by_cat.get(cat_key, [])
        if not items:
            continue
        if total_chars >= TOTAL_MAX:
            break

        # Eliminar duplicados por titulo
        seen = set()
        unique = []
        for s in items:
            key = re.sub(r'\W+', '', s['title'].lower())[:25]
            if key not in seen:
                seen.add(key)
                unique.append(s)

        block_lines = [f"\n### {cat_label} ({len(unique)} secciones)\n"]
        block_chars = len(block_lines[0])

        for s in unique:
            entry = f"\n#### {s['title']}\n{s['content']}\n"
            if block_chars + len(entry) > cat_max:
                break
            if total_chars + block_chars + len(entry) > TOTAL_MAX:
                break
            block_lines.append(entry)
            block_chars += len(entry)

        if len(block_lines) > 1:
            output_blocks.append('\n'.join(block_lines))
            total_chars += block_chars

    total_pages = index.get('total', 0)
    scraped_at  = index.get('scraped_at', 'desconocido')

    header = (
        f"_Base de conocimiento: {total_pages} paginas de developers.mercadolibre.cl "
        f"scrapeadas el {scraped_at}. "
        f"{total_chars:,} chars de documentacion activa._\n"
    )

    return header + '\n'.join(output_blocks)


def update_agent(agent_content: str, knowledge_block: str, index: dict) -> str:
    total = index.get('total', 0)
    scraped_at = index.get('scraped_at', 'desconocido')

    section = f"""

## BASE DE CONOCIMIENTO — MERCADO LIBRE API & PLATAFORMA

> {total} paginas de documentacion oficial de developers.mercadolibre.cl scrapeadas el {scraped_at}.
> Contexto de negocio: App CLAUDE (Client ID: 7997483236761265), 4 cuentas activas
> (APANTALLATEMX/523916436, AUTOBOT/292395685, BLOWTECHNOLOGIES/391393176, LUTEMAMEXICO/515061615)
> Dashboard: https://apantallatemx.up.railway.app

{knowledge_block}

"""

    # Reemplazar si ya existe
    if '## BASE DE CONOCIMIENTO — MERCADO LIBRE' in agent_content:
        agent_content = re.sub(
            r'\n## BASE DE CONOCIMIENTO — MERCADO LIBRE.*',
            section,
            agent_content,
            flags=re.DOTALL
        )
    else:
        agent_content = agent_content.rstrip() + '\n' + section

    return agent_content


def main():
    print("=" * 60)
    print("  Actualizando mercadolibre-strategist")
    print("  con documentacion oficial de developers.mercadolibre.cl")
    print("=" * 60)

    if not MAIN_FILE.exists() or not INDEX_FILE.exists():
        print(f"\n[ERROR] Archivos no encontrados.")
        print("  Ejecuta primero: python3.13 scraper_meli_docs.py")
        return

    index = json.loads(INDEX_FILE.read_text(encoding='utf-8'))
    total = index.get('total', 0)
    by_cat = index.get('by_category', {})
    print(f"\n[1] Indice: {total} paginas en {len(by_cat)} categorias")
    for cat, items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        print(f"    {cat:18s}: {len(items):3d} paginas")

    print(f"\n[2] Construyendo bloque de conocimiento...")
    knowledge = build_agent_knowledge(MAIN_FILE, index)
    print(f"    {len(knowledge):,} chars generados")

    print(f"\n[3] Leyendo agente actual...")
    agent_content = AGENT_FILE.read_text(encoding='utf-8')
    print(f"    Tamano actual: {len(agent_content):,} chars")

    backup = AGENT_FILE.with_suffix('.md.bak')
    backup.write_text(agent_content, encoding='utf-8')
    print(f"    Backup: {backup}")

    print(f"\n[4] Inyectando conocimiento...")
    updated = update_agent(agent_content, knowledge, index)
    AGENT_FILE.write_text(updated, encoding='utf-8')
    print(f"    Tamano nuevo: {len(updated):,} chars")

    print(f"\n{'='*60}")
    print(f"  COMPLETADO — mercadolibre-strategist actualizado")
    print(f"  {total} paginas de docs oficiales inyectadas")
    print(f"\n  El agente ahora conoce en detalle:")
    print(f"  - Todos los endpoints de la API MeLi con parametros exactos")
    print(f"  - Flujos de autenticacion OAuth y manejo de tokens")
    print(f"  - Como crear, modificar y gestionar publicaciones via API")
    print(f"  - Precios, promociones, envios, pagos, reclamos")
    print(f"  - Mercado Ads API (Product/Brand/Display Ads)")
    print(f"  - Notificaciones y webhooks")
    print(f"  - Metricas de reputacion y calidad")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
