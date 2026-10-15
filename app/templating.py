from pathlib import Path

from fastapi.templating import Jinja2Templates

from .models import ROLES, TIPOS_LOTE, ESTADOS_TAREA
from .actividad import ACCIONES

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_v = Path(__file__).parent / "VERSION"
VERSION = _v.read_text(encoding="utf-8").strip() if _v.exists() else "dev"
templates.env.globals.update(ROLES=ROLES, TIPOS_LOTE=TIPOS_LOTE, ESTADOS_TAREA=ESTADOS_TAREA,
                             ACCIONES=ACCIONES, APP_NOMBRE="PARKA Depósito", VERSION=VERSION)


_STATIC = Path(__file__).parent / "static"


def static_v(nombre: str) -> str:
    """Marca de versión por archivo (fecha de modificación): el navegador baja el archivo nuevo
    apenas cambia, aunque la app que está corriendo sea de una versión anterior."""
    try:
        return f"{VERSION}-{int((_STATIC / nombre).stat().st_mtime)}"
    except OSError:
        return VERSION


templates.env.globals["static_v"] = static_v


def render(request, nombre: str, status_code: int = 200, **ctx):
    u = ctx.get("u")
    if u is not None and "acc" not in ctx:
        # secciones y permisos salen de la caché en memoria (auth.secciones_todas): sin otra conexión a la base
        from .auth import accesos_de, secciones_todas
        ctx["acc"] = accesos_de(None, u)
        if "orden_menu" not in ctx:
            ctx["orden_menu"] = [s.slug for s in secciones_todas()]
    ctx.setdefault("acc", set())
    ctx.setdefault("orden_menu", [])
    return templates.TemplateResponse(request, nombre, ctx, status_code=status_code)
