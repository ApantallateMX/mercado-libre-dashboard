"""
Archiva y purga audit_log de producción — pensado para correr como tarea
programada en este servidor dedicado (siempre encendido), no en la
máquina de Jovan.

Flujo (nunca borra sin haber guardado primero):
  1. GET /api/diag/audit-log-export?before_days=30 -> filas + count
  2. Si count == 0, no hay nada que hacer, termina.
  3. Guarda las filas en backups/audit_log/audit_log_<fecha>_<count>.json
  4. Verifica que el archivo se escribió bien (mismo count al releerlo)
  5. Solo entonces: POST /api/diag/audit-log-purge con expected_count=count
     — si en el servidor ya no coincide (entraron filas nuevas justo en
     este rango, no debería pasar con ts fijo pero por si acaso), el
     endpoint rechaza el borrado con 409 y este script no reintenta solo.

Uso:
    py scripts/archive_audit_log.py [--before-days 30] [--dry-run]

Pensado para Programador de tareas de Windows (Task Scheduler), 1x/noche.
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

PROD_URL = "https://apantallatemx.up.railway.app"
DIAG_TOKEN = "dk_b55c96a82a49f04908e0079bda6bee41ce2748be2c11f3b5"
BACKUP_DIR = Path(__file__).parent.parent / "backups" / "audit_log"


def _get(path: str) -> dict:
    url = f"{PROD_URL}{path}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str) -> tuple[int, dict]:
    url = f"{PROD_URL}{path}"
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before-days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="Solo exporta, nunca purga")
    args = ap.parse_args()

    ts_run = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    print(f"[{ts_run}] Exportando audit_log más viejo que {args.before_days} días...")

    export = _get(f"/api/diag/audit-log-export?before_days={args.before_days}&token={DIAG_TOKEN}")
    count = export["count"]
    print(f"  count={count} cutoff={export['cutoff']}")

    if count == 0:
        print("  Nada que archivar. Fin.")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out_file = BACKUP_DIR / f"audit_log_{ts_run}_{count}rows.json"
    out_file.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")

    # Verificación: releer el archivo y confirmar mismo count antes de purgar
    reread = json.loads(out_file.read_text(encoding="utf-8"))
    if reread["count"] != count or len(reread["rows"]) != count:
        print(f"  ERROR: el archivo guardado no coincide (esperado {count}, "
              f"leído {reread['count']}/{len(reread['rows'])}) — NO se purga. "
              f"Revisar {out_file} a mano.")
        sys.exit(1)
    print(f"  Guardado y verificado: {out_file} ({out_file.stat().st_size / 1024:.1f} KB)")

    if args.dry_run:
        print("  --dry-run: no se purga en producción.")
        return

    status, result = _post(
        f"/api/diag/audit-log-purge?before_days={args.before_days}&expected_count={count}&token={DIAG_TOKEN}"
    )
    if status == 200 and result.get("ok"):
        print(f"  Purgado en producción: {result['deleted']} filas.")
    else:
        print(f"  ERROR al purgar (HTTP {status}): {result}. "
              f"El respaldo en {out_file} ya está seguro, solo falta reintentar el purge.")
        sys.exit(1)


if __name__ == "__main__":
    main()
