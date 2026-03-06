"""
users.py — API de gestión de usuarios del dashboard y auditoría.
Solo accesible para rol 'admin'.
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
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


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[int] = None


# ─── Rutas ────────────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
async def list_users_api(request: Request):
    _require_admin(request)
    users = await user_store.list_users()
    rows_html = ""
    for u in users:
        role_label = user_store.ROLES.get(u["role"], u["role"])
        role_color = {
            "admin": "bg-red-100 text-red-700",
            "editor": "bg-blue-100 text-blue-700",
            "editor_meli": "bg-yellow-100 text-yellow-700",
            "editor_amazon": "bg-orange-100 text-orange-700",
            "viewer": "bg-gray-100 text-gray-600",
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
        rows_html += f"""
        <tr class="hover:bg-gray-50 border-b border-gray-100" id="user-row-{u['id']}">
            <td class="px-4 py-3 text-sm font-mono font-semibold text-gray-700">{u['username']}</td>
            <td class="px-4 py-3 text-sm text-gray-700">{u['display_name'] or '—'}</td>
            <td class="px-4 py-3"><span class="px-2 py-0.5 text-xs rounded-full font-medium {role_color}">{role_label}</span></td>
            <td class="px-4 py-3">{active_badge}</td>
            <td class="px-4 py-3">{pw_badge}</td>
            <td class="px-4 py-3 text-xs text-gray-400">{last_login}</td>
            <td class="px-4 py-3 text-right">
                <div class="flex items-center justify-end gap-2">
                    <button onclick="openEditUser({u['id']}, '{u['username']}', '{u['display_name'] or ''}', '{u['role']}', {u['active']})"
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
    return rows_html or '<tr><td colspan="7" class="px-4 py-8 text-center text-gray-400">No hay usuarios</td></tr>'


@router.post("")
async def create_user_api(request: Request, data: CreateUserRequest):
    du = _require_admin(request)
    if data.role not in user_store.ROLES:
        raise HTTPException(status_code=400, detail="Rol inválido")
    if not data.username.strip():
        raise HTTPException(status_code=400, detail="Username requerido")
    try:
        uid = await user_store.create_user(
            username=data.username.strip().lower(),
            display_name=data.display_name.strip(),
            role=data.role,
            created_by=du["username"],
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail="El usuario ya existe")
        raise HTTPException(status_code=500, detail=str(e))
    await user_store.log_action(
        username=du["username"],
        action="create_user",
        detail={"new_user": data.username, "role": data.role},
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
    await user_store.update_user(user_id, **kwargs)
    await user_store.log_action(
        username=du["username"],
        action="update_user",
        detail={"user_id": user_id, **kwargs},
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
    ACTION_LABELS = {
        "login": ("🔑", "bg-blue-50 text-blue-600"),
        "logout": ("🚪", "bg-gray-50 text-gray-500"),
        "price_change": ("💲", "bg-yellow-50 text-yellow-700"),
        "stock_change": ("📦", "bg-green-50 text-green-700"),
        "status_change": ("🔄", "bg-purple-50 text-purple-700"),
        "close_item": ("🚫", "bg-red-50 text-red-600"),
        "sync_activate": ("⚡", "bg-teal-50 text-teal-700"),
        "create_user": ("👤", "bg-indigo-50 text-indigo-700"),
        "update_user": ("✏️", "bg-orange-50 text-orange-700"),
        "reset_password": ("🔐", "bg-pink-50 text-pink-700"),
    }
    if not rows:
        return '<tr><td colspan="6" class="px-4 py-10 text-center text-gray-400">Sin registros</td></tr>'
    html = ""
    for r in rows:
        icon, badge_cls = ACTION_LABELS.get(r["action"], ("•", "bg-gray-50 text-gray-500"))
        detail = r.get("detail") or ""
        try:
            import json as _json
            d = _json.loads(detail) if detail else {}
            detail_str = " · ".join(f"{k}: <b>{v}</b>" for k, v in d.items() if v is not None)
        except Exception:
            detail_str = detail
        item_html = f'<span class="text-xs font-mono text-blue-500">{r["item_id"]}</span>' if r.get("item_id") else "—"
        html += f"""
        <tr class="hover:bg-gray-50 border-b border-gray-100">
            <td class="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">{r['ts']}</td>
            <td class="px-3 py-2 text-sm font-semibold text-gray-700">{r['username']}</td>
            <td class="px-3 py-2">
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium {badge_cls}">
                    {icon} {r['action']}
                </span>
            </td>
            <td class="px-3 py-2">{item_html}</td>
            <td class="px-3 py-2 text-xs text-gray-500">{detail_str}</td>
            <td class="px-3 py-2 text-xs text-gray-400">{r.get('ip') or '—'}</td>
        </tr>"""
    return html
