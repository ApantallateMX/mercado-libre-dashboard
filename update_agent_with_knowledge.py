"""
Update Amazon Agent with Scraped Knowledge
===========================================
Lee Seller Central + SP-API docs scrapeados y actualiza
el agente amazon-specialist con secciones organizadas por tema.

USO: python3.13 update_agent_with_knowledge.py
"""

import json
import re
import time
from pathlib import Path

KNOWLEDGE_DIR = Path("Agentes/amazon-knowledge")
AGENT_FILE    = Path("Agentes/amazon-specialist.md")
SC_FILE       = KNOWLEDGE_DIR / "amazon-seller-central-knowledge.md"
API_FILE      = KNOWLEDGE_DIR / "amazon-spapi-knowledge.md"
INDEX_FILE    = KNOWLEDGE_DIR / "index.json"

# Cuanto incluir de cada fuente en el agente (en caracteres)
SC_MAX_CHARS  = 70_000
API_MAX_CHARS = 90_000

# Keywords por prioridad para SC (ventas, decisiones rapidas)
SC_PRIORITY_KEYWORDS = [
    'buy box', 'buybox', 'tarifa', 'comision', 'fee', 'referral',
    'listing', 'titulo', 'imagen', 'atributo', 'categoria',
    'fba', 'fulfillment', 'inventario', 'stock', 'restock',
    'suspension', 'account health', 'defect rate', 'cancelacion',
    'sponsored', 'ads', 'publicidad', 'campana', 'keyword',
    'brand registry', 'a+ content', 'review', 'calificacion',
    'devolucion', 'reembolso', 'pago', 'ciclo', 'reporte',
    'precio', 'repricing', 'ipi', 'stranded', 'fnsku',
]

# Keywords para SP-API (tecnico, operaciones por API)
API_PRIORITY_KEYWORDS = [
    'orders', 'ordenes', 'listings', 'catalog', 'fba inventory',
    'inbound', 'outbound', 'pricing', 'sales', 'reports',
    'feeds', 'notifications', 'finances', 'merchant fulfillment',
    'a+ content', 'messaging', 'solicitations', 'rate limit',
    'use case', 'reference', 'endpoint', 'response', 'request',
    'authentication', 'token', 'refresh', 'sandbox',
    'report type', 'feed type', 'notification type',
    'data kiosk', 'analytics', 'easy ship',
]


def parse_sections(md_text: str) -> list[dict]:
    """Divide un MD en secciones por heading ##."""
    sections = []
    current_title = ''
    current_lines = []

    for line in md_text.split('\n'):
        if line.startswith('## ') and len(line) > 5:
            if current_lines and current_title:
                content = '\n'.join(current_lines).strip()
                if len(content) > 200:
                    sections.append({'title': current_title, 'content': content})
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines and current_title:
        content = '\n'.join(current_lines).strip()
        if len(content) > 200:
            sections.append({'title': current_title, 'content': content})

    return sections


def score_section(section: dict, keywords: list[str]) -> int:
    title   = section['title'].lower()
    snippet = section['content'][:600].lower()
    score   = 0
    for kw in keywords:
        if kw in title:   score += 3
        if kw in snippet: score += 1
    return score


def build_knowledge_block(md_file: Path, keywords: list[str], max_chars: int, label: str) -> str:
    """Lee el archivo MD, prioriza secciones y construye bloque para el agente."""
    if not md_file.exists():
        return f"_(No se encontro {md_file.name} — ejecuta el scraper primero)_\n"

    text = md_file.read_text(encoding='utf-8')
    sections = parse_sections(text)

    if not sections:
        return "_(Sin contenido)_\n"

    # Ordenar por relevancia
    sections.sort(key=lambda s: score_section(s, keywords), reverse=True)

    # Eliminar duplicados por titulo
    seen_titles = set()
    unique = []
    for s in sections:
        key = re.sub(r'\W+', '', s['title'].lower())[:30]
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(s)

    # Construir bloque respetando limite de chars
    lines = []
    total = 0
    for s in unique:
        block = f"### {s['title']}\n{s['content']}\n"
        if total + len(block) > max_chars:
            break
        lines.append(block)
        total += len(block)

    pct = total / max_chars * 100
    header = f"_{label} — {len(lines)} secciones, {total:,} chars ({pct:.0f}% del limite)_\n\n"
    return header + '\n---\n\n'.join(lines)


def update_agent(agent_content: str, sc_block: str, api_block: str, index: dict) -> str:
    sc_count  = len(index.get('seller_central', []))
    api_count = len(index.get('sp_api', []))
    scraped_at = index.get('scraped_at', 'desconocido')

    knowledge_section = f"""

## 9. BASE DE CONOCIMIENTO — SELLER CENTRAL MX

> {sc_count} paginas de Centro de Ayuda scrapeadas el {scraped_at}.
> Usar para respuestas rapidas: tarifas, politicas, listings, FBA, ads, account health.

{sc_block}

---

## 10. BASE DE CONOCIMIENTO — SP-API COMPLETO

> {api_count} paginas de documentacion tecnica de Amazon SP-API scrapeadas el {scraped_at}.
> Usar para: saber que endpoints existen, como llamarlos, que datos retornan, rate limits.
> Contexto tecnico del negocio: Seller ID A20NFIUQNEYZ1E, Marketplace MX A1AM78C64UM0Y8,
> App VeKtorClaude (amzn1.sp.solution.edc432e9-c674-4a48-a6f0-11891a51f840)

{api_block}

"""

    # Reemplazar secciones 9 y 10 si ya existen
    if '## 9. BASE DE CONOCIMIENTO' in agent_content:
        agent_content = re.sub(
            r'\n## 9\. BASE DE CONOCIMIENTO.*',
            knowledge_section,
            agent_content,
            flags=re.DOTALL
        )
    else:
        agent_content = agent_content.rstrip() + '\n' + knowledge_section

    return agent_content


def main():
    print("=" * 60)
    print("  Actualizando agente amazon-specialist")
    print("  Fuentes: Seller Central + SP-API Docs")
    print("=" * 60)

    # Verificar archivos
    missing = []
    if not SC_FILE.exists():  missing.append(str(SC_FILE))
    if not API_FILE.exists(): missing.append(str(API_FILE))
    if not INDEX_FILE.exists(): missing.append(str(INDEX_FILE))

    if missing:
        print(f"\n[ERROR] Archivos faltantes:")
        for m in missing: print(f"  - {m}")
        print("\n  Ejecuta primero: python3.13 scraper_amazon_help.py")
        return

    # Leer indice
    index = json.loads(INDEX_FILE.read_text(encoding='utf-8'))
    sc_count  = len(index.get('seller_central', []))
    api_count = len(index.get('sp_api', []))
    print(f"\n[1] Indice: {sc_count} paginas SC + {api_count} paginas SP-API")

    # Construir bloques
    print(f"\n[2] Procesando Seller Central ({SC_MAX_CHARS:,} chars max)...")
    sc_block = build_knowledge_block(SC_FILE, SC_PRIORITY_KEYWORDS, SC_MAX_CHARS,
                                     f"Seller Central MX — {sc_count} paginas")

    print(f"\n[3] Procesando SP-API Docs ({API_MAX_CHARS:,} chars max)...")
    api_block = build_knowledge_block(API_FILE, API_PRIORITY_KEYWORDS, API_MAX_CHARS,
                                      f"SP-API Developer Docs — {api_count} paginas")

    # Leer agente
    print(f"\n[4] Leyendo agente...")
    agent_content = AGENT_FILE.read_text(encoding='utf-8')
    print(f"    Tamano actual: {len(agent_content):,} chars")

    # Backup
    backup = AGENT_FILE.with_suffix('.md.bak')
    backup.write_text(agent_content, encoding='utf-8')
    print(f"    Backup: {backup}")

    # Actualizar
    print(f"\n[5] Inyectando conocimiento al agente...")
    updated = update_agent(agent_content, sc_block, api_block, index)
    AGENT_FILE.write_text(updated, encoding='utf-8')
    print(f"    Tamano nuevo: {len(updated):,} chars")

    # Resumen
    print(f"\n{'='*60}")
    print(f"  COMPLETADO")
    print(f"  Seller Central : {sc_count} paginas inyectadas")
    print(f"  SP-API Docs    : {api_count} paginas inyectadas")
    print(f"  Agente         : {AGENT_FILE}")
    print(f"\n  El agente ahora sabe como:")
    print(f"  - Gestionar listings, FBA, ads, account health via SC")
    print(f"  - Usar cada endpoint de SP-API con parametros exactos")
    print(f"  - Construir integraciones y automatizaciones via API")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
