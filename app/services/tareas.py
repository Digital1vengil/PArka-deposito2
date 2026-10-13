from __future__ import annotations

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from ..db import hoy
from ..models import Tarea, Usuario


def tareas_de_seccion(db: Session, slug: str, incluir_hechas_hoy: bool = True) -> list[Tarea]:
    q = select(Tarea).where(Tarea.seccion_slug == slug)
    if incluir_hechas_hoy:
        from datetime import datetime, time
        inicio = datetime.combine(hoy(), time.min)
        q = q.where(or_(Tarea.estado != "hecha", Tarea.hecha_at >= inicio))
    else:
        q = q.where(Tarea.estado != "hecha")
    tareas = list(db.scalars(q).all())
    orden_estado = {"en_curso": 0, "pendiente": 1, "hecha": 2}
    tareas.sort(key=lambda t: (orden_estado.get(t.estado, 9), t.prioridad != "alta",
                               t.vence or hoy(), t.creada_at))
    return tareas


def mis_tareas(db: Session, u: Usuario) -> list[Tarea]:
    return list(db.scalars(select(Tarea).where(Tarea.asignado_a_id == u.id, Tarea.estado != "hecha")
                           .order_by(Tarea.vence, Tarea.creada_at)).all())


def tarea_json(t: Tarea, hoy_=None) -> dict:
    hoy_ = hoy_ or hoy()
    return {
        "id": t.id, "seccion": t.seccion_slug, "titulo": t.titulo, "detalle": t.detalle,
        "origen": t.origen, "prioridad": t.prioridad, "estado": t.estado,
        "asignado_a": t.asignado_a.nombre if t.asignado_a else "",
        "asignado_a_id": t.asignado_a_id,
        "vence": t.vence.strftime("%d/%m") if t.vence else "",
        "vencida": t.vencida(hoy_),
        "hecha_por": t.hecha_por.nombre if t.hecha_por else "",
        "hecha_at": t.hecha_at.strftime("%H:%M") if t.hecha_at else "",
    }
