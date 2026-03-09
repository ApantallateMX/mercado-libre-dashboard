#!/bin/bash
# run_tests.sh — Ejecuta la suite de tests del dashboard
#
# Uso:
#   ./tests/run_tests.sh              # todos los tests básicos
#   ./tests/run_tests.sh --full       # incluye tests lentos (APIs externas)
#   ./tests/run_tests.sh --smoke      # solo smoke tests (más rápido)
#   ./tests/run_tests.sh --unit       # solo tests unitarios (sin servidor)
#
# Variables de entorno opcionales:
#   TEST_BASE_URL=http://localhost:8000
#   TEST_SESSION=<valor de cookie dash_session del browser>

set -e

BASE_URL="${TEST_BASE_URL:-http://localhost:8000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "═══════════════════════════════════════════════════════"
echo "  Dashboard QA Test Suite"
echo "  Servidor: $BASE_URL"
echo "  Fecha: $(date)"
echo "═══════════════════════════════════════════════════════"

# Verificar que pytest esté disponible
if ! python3.13 -m pytest --version > /dev/null 2>&1; then
    echo "⚠  pytest no instalado. Instalando..."
    pip install pytest pytest-asyncio httpx
fi

# Verificar que el servidor esté corriendo (para tests de integración)
SERVER_UP=false
if curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/" | grep -q "200\|302"; then
    SERVER_UP=true
    echo "✓ Servidor accesible en $BASE_URL"
else
    echo "⚠  Servidor NO accesible en $BASE_URL"
    echo "   Los tests de integración serán ignorados"
fi

case "${1:-}" in
    --smoke)
        echo ""
        echo "► Ejecutando: Smoke Tests"
        python3.13 -m pytest tests/test_smoke.py -v
        ;;
    --unit)
        echo ""
        echo "► Ejecutando: Tests unitarios (sin servidor)"
        python3.13 -m pytest tests/test_data_integrity.py tests/test_health_checker.py -v
        ;;
    --full)
        echo ""
        echo "► Ejecutando: Suite completa (incluye APIs externas)"
        python3.13 -m pytest tests/ -v
        ;;
    *)
        echo ""
        echo "► Ejecutando: Suite estándar"
        python3.13 -m pytest tests/ -v -m "not slow"
        ;;
esac

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Tests completados"
echo "═══════════════════════════════════════════════════════"
