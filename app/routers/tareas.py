from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import usuario_actual, secciones_de, SinPermiso, puede
from ..db import get_db, ahora, hoy
from ..models import Seccion, Tarea, Usuario
from ..services.tareas import tareas_de_seccion, tareas_de_secciones, mis_tareas, tarea_json
from ..templating import render

router = APIRouter(prefix="/tareas")


NO_TAREAS = ("tareas", "articulos", "smartpost")


def _secciones_tareas(db: Session, u: Usuario):
    return [s for s in secciones_de(db, u) if s.slug not in NO_TAREAS]


def _puede_ver(db: Session, u: Usuario, slug: str) -> bool:
    if slug in NO_TAREAS:
        return False
    s = db.scalar(select(Seccion).where(Seccion.slug == slug))
    return bool(s and puede(db, u, slug))


@router.get("")
def pagina(request: Request, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    secs = _secciones_tareas(db, u)
    usuarios = db.scalars(select(Usuario).where(Usuario.activo.is_(True)).order_by(Usuario.nombre)).all()
    return render(request, "tareas.html", u=u, secciones=secs, usuarios=usuarios,
                  puede_asignar=puede(db, u, "asignar_tareas"))


@router.get("/api/lista")
def lista(u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    secs = _secciones_tareas(db, u)
    por_sec = tareas_de_secciones(db, [s.slug for s in secs])
    return {
        "mias": [tarea_json(t) for t in mis_tareas(db, u)],
        "secciones": [{"slug": s.slug, "nombre": s.nombre, "icono": s.icono,
                       "tareas": [tarea_json(t) for t in por_sec.get(s.slug, [])]} for s in secs],
    }


class NuevaTarea(BaseModel):
    seccion: str
    titulo: str
    detalle: str = ""
    prioridad: str = "normal"
    asignado_a_id: int | None = None
    vence: date | None = None


@router.post("/api")
def crear(body: NuevaTarea, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    if not _puede_ver(db, u, body.seccion):
        raise SinPermiso()
    if not body.titulo.strip():
        raise HTTPException(400, "Poné un título")
    asignado = body.asignado_a_id if puede(db, u, "asignar_tareas") else None
    t = Tarea(seccion_slug=body.seccion, titulo=body.titulo.strip()[:200], detalle=body.detalle.strip(),
              prioridad="alta" if body.prioridad == "alta" else "normal", origen="manual",
              asignado_a_id=asignado, vence=body.vence or hoy(), creado_por_id=u.id)
    db.add(t)
    db.flush()
    det = t.titulo + (f" → {db.get(Usuario, asignado).nombre}" if asignado else "")
    registrar(db, u, "tarea_creada", det, body.seccion, commit=False)
    db.commit()
    return {"ok": True, "tarea": tarea_json(t)}


def _tarea(db: Session, u: Usuario, tid: int) -> Tarea:
    t = db.get(Tarea, tid)
    if not t:
        raise HTTPException(404, "Tarea inexistente")
    if not _puede_ver(db, u, t.seccion_slug):
        raise SinPermiso()
    return t


@router.post("/api/{tid}/tomar")
def tomar(tid: int, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    t = _tarea(db, u, tid)
    if t.asignado_a_id and t.asignado_a_id != u.id and not puede(db, u, "asignar_tareas"):
        raise HTTPException(400, f"La tarea ya la tiene {t.asignado_a.nombre}")
    t.asignado_a_id = u.id
    if t.estado == "pendiente":
        t.estado = "en_curso"
    registrar(db, u, "tarea_tomada", t.titulo, t.seccion_slug, commit=False)
    db.commit()
    return {"ok": True, "tarea": tarea_json(t)}


@router.post("/api/{tid}/completar")
def completar(tid: int, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    t = _tarea(db, u, tid)
    if t.origen == "auto" and t.ref.startswith("lote:"):
        raise HTTPException(400, "Esta tarea se completa sola cuando se despachan todos los paquetes del lote")
    t.estado, t.hecha_at, t.hecha_por_id = "hecha", ahora(), u.id
    if not t.asignado_a_id:
        t.asignado_a_id = u.id
    registrar(db, u, "tarea_hecha", t.titulo, t.seccion_slug, commit=False)
    db.commit()
    return {"ok": True, "tarea": tarea_json(t)}


@router.post("/api/{tid}/reabrir")
def reabrir(tid: int, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    if not puede(db, u, "asignar_tareas"):
        raise SinPermiso()
    t = _tarea(db, u, tid)
    t.estado, t.hecha_at, t.hecha_por_id = "pendiente", None, None
    registrar(db, u, "tarea_reabierta", t.titulo, t.seccion_slug, commit=False)
    db.commit()
    return {"ok": True}


class Asignar(BaseModel):
    usuario_id: int | None = None


@router.post("/api/{tid}/asignar")
def asignar(tid: int, body: Asignar, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    if not puede(db, u, "asignar_tareas"):
        raise SinPermiso()
    t = _tarea(db, u, tid)
    t.asignado_a_id = body.usuario_id
    dest = db.get(Usuario, body.usuario_id).nombre if body.usuario_id else "sin asignar"
    registrar(db, u, "tarea_asignada", f"{t.titulo} → {dest}", t.seccion_slug, commit=False)
    db.commit()
    return {"ok": True, "tarea": tarea_json(t)}


@router.post("/api/{tid}/borrar")
def borrar(tid: int, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    if not puede(db, u, "asignar_tareas"):
        raise SinPermiso()
    t = _tarea(db, u, tid)
    db.delete(t)
    db.commit()
    return {"ok": True}
