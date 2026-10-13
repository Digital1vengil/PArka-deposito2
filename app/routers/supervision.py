from collections import Counter, defaultdict
from datetime import datetime, time, date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..actividad import ACCIONES
from ..auth import requiere
solo_admin = requiere("supervision")
from ..db import get_db, hoy
from ..models import Actividad, Seccion, Usuario, Paquete, TIPOS_LOTE
from ..routers.inicio import contadores_seccion
from ..services.despacho import lotes_abiertos, progreso_lote
from ..templating import render

router = APIRouter(prefix="/supervision")


@router.get("")
def pagina(request: Request, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    usuarios = db.scalars(select(Usuario).where(Usuario.activo.is_(True)).order_by(Usuario.nombre)).all()
    secciones = db.scalars(select(Seccion).order_by(Seccion.orden)).all()
    return render(request, "supervision.html", u=u, usuarios=usuarios, secciones=secciones)


@router.get("/api/datos")
def datos(u: Usuario = Depends(solo_admin), db: Session = Depends(get_db),
          fecha: date | None = None, usuario_id: int | None = None):
    fecha = fecha or hoy()
    inicio, fin = datetime.combine(fecha, time.min), datetime.combine(fecha, time.max)
    secciones = db.scalars(select(Seccion).order_by(Seccion.orden)).all()
    sec = [{"slug": s.slug, "nombre": s.nombre, "icono": s.icono, "activa": s.activa,
            **contadores_seccion(db, s.slug)} for s in secciones]

    lotes = []
    for l in lotes_abiertos(db):
        h, t = progreso_lote(db, l.id)
        lotes.append({"id": l.id, "tipo": TIPOS_LOTE.get(l.tipo, l.tipo), "nombre": l.nombre,
                      "fecha": l.fecha.strftime("%d/%m"), "hechos": h, "total": t})

    q = select(Actividad).where(Actividad.ts >= inicio, Actividad.ts <= fin)
    acts_all = db.scalars(q.order_by(Actividad.ts.desc())).all()
    por_op: dict[str, Counter] = defaultdict(Counter)
    ultima: dict[str, str] = {}
    for a in acts_all:
        n = a.usuario.nombre if a.usuario else "(sistema)"
        por_op[n][a.accion] += 1
        ultima.setdefault(n, a.ts.strftime("%H:%M"))
    operarios = [{"nombre": n, "paquetes": c.get("escaneo_ok", 0) + c.get("despacho_manual", 0),
                  "tareas": c.get("tarea_hecha", 0), "errores": c.get("escaneo_no_encontrado", 0),
                  "acciones": sum(c.values()), "ultima": ultima.get(n, "")} for n, c in por_op.items()]
    operarios.sort(key=lambda o: -o["acciones"])

    acts = acts_all
    if usuario_id:
        acts = [a for a in acts if a.usuario_id == usuario_id]
    feed = [{"hora": a.ts.strftime("%H:%M:%S"), "usuario": a.usuario.nombre if a.usuario else "",
             "seccion": a.seccion_slug, "accion": ACCIONES.get(a.accion, a.accion), "tipo": a.accion,
             "detalle": a.detalle} for a in acts[:200]]
    return {"fecha": fecha.isoformat(), "secciones": sec, "lotes": lotes, "operarios": operarios, "feed": feed}
