"""
users.py — API de gestión de usuarios del dashboard y auditoría.
Solo accesible para rol 'admin'.
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, List
from app.services import user_store

router = APIRouter(prefix="/api/users", tags=["users"])


# ─── Helpers de permisos ──────────────────────────────────────────────────────
def _get_dashboard_user(request: Request) -> dict:
    du = getattr(request.state, "dashboard_user", None)
    if not du:
        raise HTTPException(status_code=401, detail="No autenticado")
    return du


def _require_admin(request: Request) -> dict:
    du = _get_dashboard_user(request)
    if du["role"] != "admin":
        raise HTTPException(status_code=403, detail="Se requiere rol Administrador")
    return du


def _require_write_meli(request: Request) -> dict:
    du = _get_dashboard_user(request)
    if du["role"] not in user_store.ROLE_CAN_WRITE_MELI:
        raise HTTPException(status_code=403, detail="Sin permisos para modificar en Mercado Libre")
    return du


def _require_write_amazon(request: Request) -> dict:
    du = _get_dashboard_user(request)
    if du["role"] not in user_store.ROLE_CAN_WRITE_AMAZON:
        raise HTTPException(status_code=403, detail="Sin permisos para modificar en Amazon")
    return du


def _get_client_ip(request: Request) -> str:
    return request.headers.get("X-Forwarded-For", request.client.host if request.client else "")


# ─── Modelos ──────────────────────────────────────────────────────────────────
class CreateUserRequest(BaseModel):
    username: str
    display_name: str
    role: str
    allowed_sections: Optional[List[str]] = None


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[int] = None
    allowed_sections: Optional[List[str]] = None


# ─── Rutas ────────────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
async def list_users_api(request: Request):
    import json as _json
    _require_admin(request)
    users = await user_store.list_users()
    rows_html = ""
    for u in users:
        role_label = user_store.ROLES.get(u["role"], u["role"])
        role_color = {
            "admin":               "bg-red-100 text-red-700",
            "editor":              "bg-blue-100 text-blue-700",
            "editor_meli":         "bg-yellow-100 text-yellow-700",
            "editor_amazon":       "bg-orange-100 text-orange-700",
            "editor_facturacion":  "bg-purple-100 text-purple-700",
            "viewer":              "bg-gray-100 text-gray-600",
        }.get(u["role"], "bg-gray-100 text-gray-600")
        active_badge = (
            '<span class="px-2 py-0.5 text-xs rounded-full bg-green-100 text-green-700">Activo</span>'
            if u["active"]
            else '<span class="px-2 py-0.5 text-xs rounded-full bg-gray-100 text-gray-400">Inactivo</span>'
        )
        pw_badge = (
            '<span class="text-xs text-orange-500">Pendiente</span>'
            if u["must_change_pw"] or not u.get("password_hash")
            else '<span class="text-xs text-green-600">OK</span>'
        )
        last_login = u.get("last_login") or "—"
        # Secciones restringidas
        raw_sections = u.get("allowed_sections")
        sections_list = user_store._parse_allowed_sections(raw_sections)
        if sections_list:
            section_labels = {k: v for k, v in user_store.ALL_SECTIONS}
            chips = "".join(
                f'<span class="px-1.5 py-0.5 text-xs bg-purple-50 text-purple-600 rounded">{section_labels.get(s, s)}</span>'
                for s in sections_list
            )
            sections_html = f'<div class="flex flex-wrap gap-1">{chips}</div>'
            # Serializar para pasar al JS
            sections_json = _json.dumps(sections_list).replace('"', '&quot;')
        else:
            sections_html = '<span class="text-xs text-gray-400">Todas</span>'
            sections_json = "[]"
        display_esc = (u['display_name'] or '').replace("'", "\\'")
        rows_html += f"""
        <tr class="hover:bg-gray-50 border-b border-gray-100" id="user-row-{u['id']}">
            <td class="px-4 py-3 text-sm font-mono font-semibold text-gray-700">{u['username']}</td>
            <td class="px-4 py-3 text-sm text-gray-700">{u['display_name'] or '—'}</td>
            <td class="px-4 py-3"><span class="px-2 py-0.5 text-xs rounded-full font-medium {role_color}">{role_label}</span></td>
            <td class="px-4 py-3">{sections_html}</td>
            <td class="px-4 py-3">{active_badge}</td>
            <td class="px-4 py-3">{pw_badge}</td>
            <td class="px-4 py-3 text-xs text-gray-400">{last_login}</td>
            <td class="px-4 py-3 text-right">
                <div class="flex items-center justify-end gap-2">
                    <button onclick="openEditUser({u['id']}, '{u['username']}', '{display_esc}', '{u['role']}', {u['active']}, {sections_json})"
                            class="text-xs px-2 py-1 bg-blue-50 text-blue-600 rounded hover:bg-blue-100">
                        Editar
                    </button>
                    <button onclick="resetUserPw({u['id']}, '{u['username']}')"
                            class="text-xs px-2 py-1 bg-yellow-50 text-yellow-600 rounded hover:bg-yellow-100">
                        Reset PW
                    </button>
                    {'<button onclick="toggleUser(' + str(u['id']) + ', ' + str(u['active']) + ')" class="text-xs px-2 py-1 bg-red-50 text-red-500 rounded hover:bg-red-100">Desactivar</button>' if u['active'] else '<button onclick="toggleUser(' + str(u['id']) + ', ' + str(u['active']) + ')" class="text-xs px-2 py-1 bg-green-50 text-green-600 rounded hover:bg-green-100">Activar</button>'}
                </div>
            </td>
        </tr>"""
    return rows_html or '<tr><td colspan="8" class="px-4 py-8 text-center text-gray-400">No hay usuarios</td></tr>'


@router.post("")
async def create_user_api(request: Request, data: CreateUserRequest):
    du = _require_admin(request)
    if data.role not in user_store.ROLES:
        raise HTTPException(status_code=400, detail="Rol inválido")
    if not data.username.strip():
        raise HTTPException(status_code=400, detail="Username requerido")
    # allowed_sections: None o lista vacía → sin restricción
    sections = data.allowed_sections or []
    try:
        uid = await user_store.create_user(
            username=data.username.strip().lower(),
            display_name=data.display_name.strip(),
            role=data.role,
            created_by=du["username"],
            allowed_sections=sections if sections else None,
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail="El usuario ya existe")
        raise HTTPException(status_code=500, detail=str(e))
    await user_store.log_action(
        username=du["username"],
        action="create_user",
        detail={"new_user": data.username, "role": data.role, "sections": sections},
        ip=_get_client_ip(request),
        user_id=du["id"],
    )
    return {"ok": True, "user_id": uid}


@router.put("/{user_id}")
async def update_user_api(request: Request, user_id: int, data: UpdateUserRequest):
    du = _require_admin(request)
    kwargs = {k: v for k, v in data.dict().items() if v is not None}
    if "role" in kwargs and kwargs["role"] not in user_store.ROLES:
        raise HTTPException(status_code=400, detail="Rol inválido")
    # allowed_sections puede ser lista vacía (sin restricción) — tratarla como None para limpiar
    if "allowed_sections" in kwargs:
        sections = kwargs["allowed_sections"]
        kwargs["allowed_sections"] = sections if sections else None
    await user_store.update_user(user_id, **kwargs)
    await user_store.log_action(
        username=du["username"],
        action="update_user",
        detail={"user_id": user_id, **{k: v for k, v in kwargs.items() if k != "allowed_sections"}},
        ip=_get_client_ip(request),
        user_id=du["id"],
    )
    return {"ok": True}


@router.post("/{user_id}/reset-password")
async def reset_password_api(request: Request, user_id: int):
    """Fuerza al usuario a crear nueva contraseña en el próximo login."""
    du = _require_admin(request)
    target = await user_store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    async with __import__("aiosqlite").connect(__import__("app.config", fromlist=["DATABASE_PATH"]).DATABASE_PATH) as db:
        await db.execute(
            "UPDATE dashboard_users SET must_change_pw=1, password_hash=NULL, password_salt=NULL WHERE id=?",
            (user_id,)
        )
        await db.commit()
    await user_store.delete_user_sessions(user_id)
    await user_store.log_action(
        username=du["username"],
        action="reset_password",
        detail={"target_user": target["username"]},
        ip=_get_client_ip(request),
        user_id=du["id"],
    )
    return {"ok": True}


# ─── Auditoría ────────────────────────────────────────────────────────────────

ACTION_META: dict = {
    # ML acciones
    "ml_item_created":    ("🚀", "bg-green-50 text-green-700",   "Publicó listing"),
    "ml_item_reactivated":("🔁", "bg-teal-50 text-teal-700",     "Reactivó listing"),
    "ml_mark_launched":   ("📌", "bg-green-50 text-green-600",   "Marcó lanzado"),
    "ml_price_update":    ("🏷", "bg-yellow-50 text-yellow-700", "Cambio precio ML"),
    "ml_price_synced":    ("🔄", "bg-yellow-50 text-yellow-600", "Sincronizó precio ML"),
    "ml_stock_update":    ("📦", "bg-blue-50 text-blue-700",     "Cambio stock ML"),
    "ml_variation_stock": ("📦", "bg-blue-50 text-blue-600",     "Stock variación ML"),
    "ml_title_update":    ("🔤", "bg-purple-50 text-purple-700", "Cambio título ML"),
    "ml_description_update": ("📝","bg-purple-50 text-purple-600","Cambio descripción ML"),
    "ml_status_update":   ("⏸",  "bg-orange-50 text-orange-700","Cambio estado ML"),
    "ml_shipping_update": ("🚚", "bg-gray-50 text-gray-600",     "Cambio envío ML"),
    "ml_pictures_update": ("🖼",  "bg-indigo-50 text-indigo-600","Cambio fotos ML"),
    "ml_attributes_update":("⚙", "bg-gray-50 text-gray-600",    "Cambio atributos ML"),
    "ml_item_closed":     ("🚫", "bg-red-50 text-red-600",       "Cerró listing ML"),
    "ml_concentration":   ("⚡", "bg-teal-50 text-teal-700",     "Concentración stock"),
    # Amazon acciones
    "amz_price_update":   ("🏷", "bg-yellow-50 text-yellow-700", "Cambio precio Amazon"),
    "amz_listing_update": ("✏️", "bg-orange-50 text-orange-700", "Editó listing Amazon"),
    "amz_stock_update":   ("📦", "bg-blue-50 text-blue-700",     "Cambio stock Amazon"),
    # Sistema / usuarios
    "login":              ("🔑", "bg-blue-50 text-blue-600",     "Inicio sesión"),
    "logout":             ("🚪", "bg-gray-50 text-gray-500",     "Cerró sesión"),
    "create_user":        ("👤", "bg-indigo-50 text-indigo-700", "Creó usuario"),
    "update_user":        ("✏️", "bg-orange-50 text-orange-700", "Editó usuario"),
    "reset_password":     ("🔐", "bg-pink-50 text-pink-700",     "Reset contraseña"),
}


def _render_timeline_rows(rows: list) -> str:
    """Genera HTML de filas <tr> para el timeline de auditoría."""
    import json as _json
    if not rows:
        return '<tr><td colspan="5" class="px-4 py-10 text-center text-gray-400">Sin registros en este período</td></tr>'
    html = ""
    for r in rows:
        icon, badge_cls, label = ACTION_META.get(r["action"], ("•", "bg-gray-50 text-gray-500", r["action"]))
        detail_raw = r.get("detail") or ""
        try:
            d = _json.loads(detail_raw) if detail_raw else {}
            parts = []
            for k, v in d.items():
                if v is None:
                    continue
                if k == "price":
                    parts.append(f"${v:,.0f}" if isinstance(v, (int, float)) else str(v))
                elif k == "qty":
                    parts.append(f"qty: <b>{v}</b>")
                elif k == "status":
                    parts.append(f"→ <b>{v}</b>")
                elif k == "title":
                    parts.append(f'"{str(v)[:50]}"')
                elif k == "fields":
                    parts.append(f"campos: {', '.join(v) if isinstance(v, list) else v}")
                else:
                    parts.append(f"{k}: <b>{v}</b>")
            detail_str = " · ".join(parts)
        except Exception:
            detail_str = detail_raw[:100] if detail_raw else ""
        item_html = f'<span class="font-mono text-blue-500">{r["item_id"]}</span>' if r.get("item_id") else "—"
        ts = (r.get("ts") or "")[:16].replace("T", " ")
        html += f"""
        <tr class="hover:bg-gray-50 border-b border-gray-100">
            <td class="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">{ts}</td>
            <td class="px-3 py-2">
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium {badge_cls}">
                    {icon} {label}
                </span>
            </td>
            <td class="px-3 py-2 text-xs">{item_html}</td>
            <td class="px-3 py-2 text-xs text-gray-500">{detail_str}</td>
            <td class="px-3 py-2 text-xs text-gray-400">{r.get('ip') or '—'}</td>
        </tr>"""
    return html


@router.get("/audit/log", response_class=HTMLResponse)
async def audit_log_api(
    request: Request,
    username: str = None,
    action: str = None,
    date_from: str = None,
    limit: int = 100,
    offset: int = 0,
):
    _require_admin(request)
    rows = await user_store.get_audit_log(
        limit=limit, offset=offset,
        username=username or None,
        action=action or None,
        date_from=date_from or None,
    )
    return _render_timeline_rows(rows)


@router.get("/audit/summary", response_class=HTMLResponse)
async def audit_users_summary(request: Request, days: int = 7):
    """Tarjetas de actividad por usuario para la vista principal de auditoría."""
    _require_admin(request)
    users_data = await user_store.get_audit_users_summary(days=days)

    if not users_data:
        return '<p class="text-center text-gray-400 py-12">Sin actividad registrada en este período.</p>'

    all_users = await user_store.list_users()
    role_map = {u["username"]: u.get("role", "viewer") for u in all_users}
    display_map = {u["username"]: u.get("display_name") or u["username"] for u in all_users}

    ROLE_COLOR = {
        "admin":               "bg-red-100 text-red-700",
        "editor":              "bg-blue-100 text-blue-700",
        "editor_meli":         "bg-yellow-100 text-yellow-700",
        "editor_amazon":       "bg-orange-100 text-orange-700",
        "editor_facturacion":  "bg-purple-100 text-purple-700",
        "viewer":              "bg-gray-100 text-gray-500",
    }
    ROLE_LABELS = user_store.ROLES

    html = '<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">'
    for u in users_data:
        un = u["username"]
        role = role_map.get(un, "viewer")
        role_label = ROLE_LABELS.get(role, role)
        role_cls = ROLE_COLOR.get(role, "bg-gray-100 text-gray-500")
        display = display_map.get(un, un)
        last_ts = (u.get("last_action") or "")[:16].replace("T", " ")
        launches = u.get("launches", 0) or 0
        prices   = u.get("prices", 0) or 0
        stocks   = u.get("stocks", 0) or 0
        total    = u.get("total", 0) or 0
        others   = total - launches - prices - stocks

        html += f"""
        <div class="bg-white rounded-xl shadow-sm border border-gray-100 p-5 flex flex-col gap-3">
            <div class="flex items-start justify-between gap-2">
                <div>
                    <p class="font-semibold text-gray-800">{display}</p>
                    <p class="text-xs text-gray-400 font-mono">{un}</p>
                </div>
                <span class="px-2 py-0.5 text-xs rounded-full font-medium shrink-0 {role_cls}">{role_label}</span>
            </div>
            <div class="grid grid-cols-2 gap-2 text-sm">
                <div class="flex items-center gap-2">
                    <span class="text-base">🚀</span>
                    <div>
                        <p class="font-bold text-gray-700 leading-none">{launches}</p>
                        <p class="text-[10px] text-gray-400">lanzados</p>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <span class="text-base">🏷</span>
                    <div>
                        <p class="font-bold text-gray-700 leading-none">{prices}</p>
                        <p class="text-[10px] text-gray-400">precios</p>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <span class="text-base">📦</span>
                    <div>
                        <p class="font-bold text-gray-700 leading-none">{stocks}</p>
                        <p class="text-[10px] text-gray-400">stocks</p>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <span class="text-base">⚙</span>
                    <div>
                        <p class="font-bold text-gray-700 leading-none">{others}</p>
                        <p class="text-[10px] text-gray-400">otros</p>
                    </div>
                </div>
            </div>
            <div class="flex items-center justify-between pt-1 border-t border-gray-50">
                <p class="text-[10px] text-gray-400">Última acción: {last_ts or '—'}</p>
                <button onclick="window._auditShowUser('{un}', '{display}')"
                        class="text-xs px-3 py-1 bg-blue-50 text-blue-600 rounded-lg hover:bg-blue-100 font-medium transition">
                    Ver detalle
                </button>
            </div>
        </div>"""
    html += "</div>"
    return html


@router.get("/audit/user-timeline", response_class=HTMLResponse)
async def audit_user_timeline_api(
    request: Request,
    username: str,
    days: int = 7,
    action: str = None,
    offset: int = 0,
):
    """Timeline de actividad de un usuario específico."""
    _require_admin(request)
    data = await user_store.get_audit_user_timeline(
        username=username,
        days=days,
        action_filter=action or None,
        limit=50,
        offset=offset,
    )
    return _render_timeline_rows(data["rows"])


@router.get("/audit/user-stats")
async def audit_user_stats_api(
    request: Request,
    username: str,
    days: int = 7,
):
    """Estadísticas de un usuario para los KPI cards del panel de detalle."""
    _require_admin(request)
    data = await user_store.get_audit_user_timeline(
        username=username, days=days, limit=1, offset=0
    )
    return data["stats"]
