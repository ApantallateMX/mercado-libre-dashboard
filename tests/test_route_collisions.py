"""
test_route_collisions.py — Detecta colisiones de rutas (mismo path+método
registrado 2+ veces) entre los ~19 routers de app/api/*.py y las rutas
inline de app/main.py.

Por qué existe (barrido de deuda técnica 2026-08-31, pedido explícito de
Jovan): FastAPI hace first-match-wins por orden de registro -- si dos
handlers distintos quedan registrados para el mismo path+método, el segundo
queda MUERTO sin ningún error al arrancar. Ya pasó 2 veces este mes
(`/stock`, `/api/items/{item_id}/status`) que esto se detectó semanas
después en producción en vez de en desarrollo/CI.

No requiere servidor corriendo -- importa app.main directamente y solo lee
la estructura de rutas ya registrada (app.routes), sin llamar a lifespan()
(que es lo que arranca los loops de fondo reales). Es una importación
pesada (main.py hace seed de credenciales/lee .env.production al importar)
pero no hace ninguna llamada de red ni escribe nada.

Uso:
  pytest tests/test_route_collisions.py -v
"""

import pytest


def _get_app():
    import app.main as m
    return m


def test_no_new_route_collisions():
    """Falla si aparece una colisión de path+método NO documentada en
    app.main._KNOWN_ROUTE_COLLISIONS. Las colisiones ya conocidas (código
    muerto dejado a propósito, ver comentario junto a esa allowlist en
    app/main.py) no deben tumbar el build -- solo las NUEVAS."""
    m = _get_app()
    collisions = m._find_route_collisions(m.app)
    new = [c for c in collisions if not c["known"]]
    assert not new, (
        f"{len(new)} colisión(es) NUEVA(S) de ruta detectada(s) -- la ruta "
        f"registrada DESPUÉS queda muerta (first-match-wins, sin error al "
        f"arrancar). Agregar el fix real (renombrar/eliminar el duplicado) "
        f"o, si es intencional y ya se investigó, agregarla a "
        f"_KNOWN_ROUTE_COLLISIONS en app/main.py con un comentario "
        f"explicando por qué se deja así: {new}"
    )


def test_known_collisions_still_exist():
    """Señal inversa: si una entrada de _KNOWN_ROUTE_COLLISIONS deja de
    aparecer (por ejemplo porque alguien borró el duplicado, como ya pasó
    con `/stock`), avisa para limpiar la allowlist -- una entrada "conocida"
    que ya no existe no hace daño, pero acumula ruido y puede esconder que
    un fix real ya se aplicó y merece registrarse en DEVLOG."""
    m = _get_app()
    collisions = m._find_route_collisions(m.app)
    still_present = {(c["path"], c["method"]) for c in collisions}
    stale = m._KNOWN_ROUTE_COLLISIONS - still_present
    if stale:
        pytest.skip(
            f"Entradas de _KNOWN_ROUTE_COLLISIONS que ya no colisionan en el "
            f"código actual (limpiar la allowlist si el fix ya se registró en "
            f"DEVLOG): {stale}"
        )
