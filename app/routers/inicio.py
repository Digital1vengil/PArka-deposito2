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


def contadores_seccion(db: Session, slug: str) -> dict:
    pend = db.scalar(select(func.count()).where(Tarea.seccion_slug == slug, Tarea.estado != "hecha")) or 0
    venc = db.scalar(select(func.count()).where(Tarea.seccion_slug == slug, Tarea.estado != "hecha",
                                                Tarea.vence < hoy())) or 0
    extra = {}
    if slug == "tareas":
        pend = db.scalar(select(func.count()).where(Tarea.estado != "hecha")) or 0
        venc = db.scalar(select(func.count()).where(Tarea.estado != "hecha", Tarea.vence < hoy())) or 0
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
    tarjetas = [(s, contadores_seccion(db, s.slug)) for s in secciones]
    return render(request, "inicio.html", u=u, tarjetas=tarjetas, mis=mis_tareas(db, u), hoy=hoy())


@router.get("/s/{slug}")
def ir_a_seccion(slug: str, request: Request, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    s = db.scalar(select(Seccion).where(Seccion.slug == slug))
    if not s or not puede(db, u, slug):
        raise SinPermiso()
    if s.activa and slug == "pick_odoo":
        return RedirectResponse("/recolecciones-odoo", status_code=303)
    if s.activa and slug in ("despacho", "devoluciones", "conteo", "tareas", "articulos", "asistente", "ml",
                                  "recoleccion", "mapeo", "ecommerce"):
        return RedirectResponse(f"/{slug}", status_code=303)
    return render(request, "proximamente.html", u=u, s=s)
