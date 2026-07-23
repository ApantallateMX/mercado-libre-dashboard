"""
mx_zones.py — Mapeo de estado mexicano (código ISO 3166-2:MX, ej. "MX-NLE")
a zona de almacén físico (MTY/CDMX/TJ), para cruzar demanda geográfica del
comprador contra dónde está el stock físico (feature de transferencias
sugeridas entre almacenes).

Es una heurística de negocio (cercanía aproximada), no un cálculo logístico
exacto — documentado así a propósito. CDMX es el default para cualquier
estado no listado explícitamente en MTY o TJ (almacén más grande/central).
"""

# Estados servidos preferentemente desde Monterrey (MAXX, LOC 68)
_MTY_STATES = {
    "MX-NLE",  # Nuevo León
    "MX-TAM",  # Tamaulipas
    "MX-COA",  # Coahuila
    "MX-SLP",  # San Luis Potosí
    "MX-ZAC",  # Zacatecas
    "MX-DUR",  # Durango
}

# Estados servidos preferentemente desde Tijuana (MITIJ, LOC 45/69/43/42)
_TJ_STATES = {
    "MX-BCN",  # Baja California
    "MX-BCS",  # Baja California Sur
    "MX-SON",  # Sonora
    "MX-SIN",  # Sinaloa
    "MX-CHH",  # Chihuahua
    "MX-NAY",  # Nayarit
}


def zone_for_state_code(state_code: str) -> str:
    """Retorna 'MTY', 'TJ', o 'CDMX' (default) para un código de estado
    ISO 3166-2:MX (ej. 'MX-NLE'). CDMX cubre centro, sur, sureste y
    península de Yucatán — todo lo que no cae claramente en MTY o TJ."""
    code = (state_code or "").strip().upper()
    if code in _MTY_STATES:
        return "MTY"
    if code in _TJ_STATES:
        return "TJ"
    return "CDMX"
