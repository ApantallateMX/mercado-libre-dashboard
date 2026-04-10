"""
Update BinManager Agent with Scraped Knowledge
================================================
Inyecta el conocimiento scrapeado de BinManager
en el agente binmanager-specialist.

USO: python3.13 update_bm_agent.py
"""

import json
import re
import time
from pathlib import Path

KNOWLEDGE_DIR = Path("Agentes/bm-knowledge")
AGENT_FILE    = Path(".claude/agents/binmanager-specialist.md")
MAIN_FILE     = KNOWLEDGE_DIR / "binmanager-knowledge.md"
INDEX_FILE    = KNOWLEDGE_DIR / "bm-index.json"
API_FILE      = KNOWLEDGE_DIR / "binmanager-api-endpoints.json"


def update_agent(agent_content: str, bm_knowledge: str, index: dict, api_by_cat: dict) -> str:
    total_pages = index.get('pages_explored', 0)
    total_eps   = index.get('endpoints_discovered', 0)
    generated   = index.get('generated_at', 'desconocido')

    # Construir resumen de endpoints por categoria
    ep_summary = []
    for cat, eps in sorted(api_by_cat.items()):
        if eps:
            ep_summary.append(f"  - **{cat.upper()}**: {len(eps)} endpoints")
    ep_lines = '\n'.join(ep_summary)

    section = f"""

## BASE DE CONOCIMIENTO — BINMANAGER SISTEMA COMPLETO

> Explorado: {total_pages} pantallas | {total_eps} endpoints API descubiertos | {generated}
> Sistema: https://binmanager.mitechnologiesinc.com
> Credenciales: jovan.rodriguez@mitechnologiesinc.com / 123456 / COMPANYID=1

### Endpoints API descubiertos por categoria:
{ep_lines}

---

{bm_knowledge}

"""

    if '## BASE DE CONOCIMIENTO — BINMANAGER' in agent_content:
        agent_content = re.sub(
            r'\n## BASE DE CONOCIMIENTO — BINMANAGER.*',
            section,
            agent_content,
            flags=re.DOTALL
        )
    else:
        agent_content = agent_content.rstrip() + '\n' + section

    return agent_content


def main():
    print("=" * 60)
    print("  Actualizando binmanager-specialist")
    print("=" * 60)

    if not MAIN_FILE.exists() or not INDEX_FILE.exists():
        print(f"\n[ERROR] Archivos no encontrados.")
        print("  Ejecuta primero: python3.13 scraper_binmanager.py")
        return

    index      = json.loads(INDEX_FILE.read_text(encoding='utf-8'))
    bm_content = MAIN_FILE.read_text(encoding='utf-8')
    api_by_cat = json.loads(API_FILE.read_text(encoding='utf-8')) if API_FILE.exists() else {}

    print(f"\n[1] Conocimiento:")
    print(f"    Paginas exploradas : {index.get('pages_explored', 0)}")
    print(f"    Endpoints API      : {index.get('endpoints_discovered', 0)}")
    print(f"    Formularios        : {index.get('forms_found', 0)}")

    agent_content = AGENT_FILE.read_text(encoding='utf-8')
    print(f"\n[2] Agente actual: {len(agent_content):,} chars")

    backup = AGENT_FILE.with_suffix('.md.bak')
    backup.write_text(agent_content, encoding='utf-8')

    # Limitar contenido para no hacer el agente demasiado grande
    if len(bm_content) > 100_000:
        bm_content = bm_content[:100_000] + "\n\n_(contenido truncado — ver bm-knowledge/ para version completa)_"

    updated = update_agent(agent_content, bm_content, index, api_by_cat)
    AGENT_FILE.write_text(updated, encoding='utf-8')

    print(f"\n[3] Agente actualizado: {len(updated):,} chars")
    print(f"\n{'='*60}")
    print(f"  COMPLETADO — binmanager-specialist actualizado")
    print(f"  El agente ahora conoce:")
    print(f"  - Estructura completa del sistema BinManager")
    print(f"  - Todos los endpoints API reales (interceptados)")
    print(f"  - Formularios y parametros de cada seccion")
    print(f"  - Logica de stock disponible vs reservado")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
