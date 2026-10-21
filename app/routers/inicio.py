from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..auth import usuario_actual, secciones_de, SinPermiso, puede
from ..db import get_db, hoy
from ..models import Seccion, Tarea, Paquete, Usuario
from ..services.despacho import lotes_abiertos
from ..services.tareas import mis_tareas
from ..templating import render

router = APIRouter()


def conteo_tareas(db: Session) -> dict[str, tuple[int, int]]:
    """Tareas abiertas y vencidas por sección, en una sola consulta (clave "*" = todas)."""
    from sqlalchemy import case
    filas = db.execute(select(Tarea.seccion_slug, func.count(),
                              func.sum(case((Tarea.vence < hoy(), 1), else_=0)))
                       .where(Tarea.estado != "hecha").group_by(Tarea.seccion_slug)).all()
    r = {slug: (int(n or 0), int(v or 0)) for slug, n, v in filas}
    r["*"] = (sum(n for n, _ in r.values()), sum(v for _, v in r.values()))
    return r


def contadores_seccion(db: Session, slug: str, tareas: dict[str, tuple[int, int]] | None = None) -> dict:
    if tareas is None:
        tareas = conteo_tareas(db)
    pend, venc = tareas.get("*" if slug == "tareas" else slug, (0, 0))
    extra = {}
    if slug in ("asistente", "ml"):
        extra["panel"] = True
    if slug == "devoluciones":
        from ..models import Devolucion
        extra["devoluciones_hoy"] = db.scalar(select(func.count()).where(Devolucion.fecha == hoy())) or 0
    if slug == "conteo":
        from ..models import Conteo
        extra["conteos_hoy"] = db.scalar(select(func.count()).where(Conteo.fecha == hoy())) or 0
    if slug == "ecommerce":
        from ..models import Control
        from ..services.ecommerce import paquetes_pendientes_hoy
        extra["ecommerce_abiertas"] = db.scalar(select(func.count()).select_from(Control).where(
            Control.modo == "ecommerce", Control.estado == "abierto")) or 0
        extra["ecommerce_pendientes"] = paquetes_pendientes_hoy(db)
    if slug == "recoleccion":
        from ..models import Recoleccion
        extra["recolecciones_abiertas"] = db.scalar(select(func.count()).where(Recoleccion.estado == "abierta")) or 0
    if slug == "control":
        from ..models import Control, ControlPaquete
        extra["control_pendientes"] = db.scalar(select(func.count()).select_from(ControlPaquete).join(Control).where(
            Control.fecha == hoy(), ControlPaquete.estado.in_(("pendiente", "en_curso", "faltante")))) or 0
    if slug == "mapeo":
        from ..models import Ubicacion
        extra["ubicaciones"] = db.scalar(select(func.count()).select_from(Ubicacion)) or 0
    if slug == "articulos":
        from ..models import Articulo
        extra["articulos"] = db.scalar(select(func.count()).select_from(Articulo)) or 0
    if slug == "despacho":
        ids = [l.id for l in lotes_abiertos(db)]
        extra["paquetes_pendientes"] = (db.scalar(select(func.count()).where(
            Paquete.lote_id.in_(ids), Paquete.estado == "pendiente")) or 0) if ids else 0
    return {"tareas": pend, "vencidas": venc, **extra}


@router.get("/")
def inicio(request: Request, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    secciones = secciones_de(db, u)
    tt = conteo_tareas(db)
    tarjetas = [(s, contadores_seccion(db, s.slug, tt)) for s in secciones]
    return render(request, "inicio.html", u=u, tarjetas=tarjetas, mis=mis_tareas(db, u), hoy=hoy())


@router.get("/s/{slug}")
def ir_a_seccion(slug: str, request: Request, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    s = db.scalar(select(Seccion).where(Seccion.slug == slug))
    if not s or not puede(db, u, slug):
        raise SinPermiso()
    if s.activa and slug == "pick_odoo":
        return RedirectResponse("/recolecciones-odoo", status_code=303)
    if s.activa and slug in ("rotulos", "clientes"):
        return RedirectResponse(f"/{slug}", status_code=303)
    if s.activa and slug == "hojas_ruta":
        return RedirectResponse("/hojas-ruta", status_code=303)
    if s.activa and slug in ("despacho", "devoluciones", "conteo", "tareas", "articulos", "asistente", "ml",
                                  "recoleccion", "mapeo", "ecommerce", "cambios"):
        return RedirectResponse(f"/{slug}", status_code=303)
    return render(request, "proximamente.html", u=u, s=s)
