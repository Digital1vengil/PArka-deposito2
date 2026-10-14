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
from ..routers.inicio import contadores_seccion, conteo_tareas
from ..services.despacho import lotes_abiertos, progreso_lote, progreso_lotes
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
    tt = conteo_tareas(db)
    sec = [{"slug": s.slug, "nombre": s.nombre, "icono": s.icono, "activa": s.activa,
            **contadores_seccion(db, s.slug, tt)} for s in secciones]

    abiertos = lotes_abiertos(db)
    prog = progreso_lotes(db, [l.id for l in abiertos])
    lotes = []
    for l in abiertos:
        h, t = prog.get(l.id, (0, 0))
        lotes.append({"id": l.id, "tipo": TIPOS_LOTE.get(l.tipo, l.tipo), "nombre": l.nombre,
                      "fecha": l.fecha.strftime("%d/%m"), "hechos": h, "total": t})

    # Resumen por operario agrupado en la base (antes se traían todas las actividades del día cada 15 s)
    rango = (Actividad.ts >= inicio, Actividad.ts <= fin)
    nombres = {x.id: x.nombre for x in db.scalars(select(Usuario)).all()}
    por_op: dict[str, Counter] = defaultdict(Counter)
    ultima_ts: dict[str, datetime] = {}
    for uid, accion, n, mx in db.execute(select(Actividad.usuario_id, Actividad.accion, func.count(),
                                                func.max(Actividad.ts)).where(*rango)
                                         .group_by(Actividad.usuario_id, Actividad.accion)).all():
        nom = nombres.get(uid, "(sistema)") if uid else "(sistema)"
        por_op[nom][accion] += n
        if mx and (nom not in ultima_ts or mx > ultima_ts[nom]):
            ultima_ts[nom] = mx
    operarios = [{"nombre": n, "paquetes": c.get("escaneo_ok", 0) + c.get("despacho_manual", 0),
                  "tareas": c.get("tarea_hecha", 0), "errores": c.get("escaneo_no_encontrado", 0),
                  "acciones": sum(c.values()), "ultima": ultima_ts[n].strftime("%H:%M") if n in ultima_ts else ""}
                 for n, c in por_op.items()]
    operarios.sort(key=lambda o: -o["acciones"])

    q = select(Actividad).where(*rango)
    if usuario_id:
        q = q.where(Actividad.usuario_id == usuario_id)
    acts = db.scalars(q.order_by(Actividad.ts.desc(), Actividad.id.desc()).limit(200)).all()
    feed = [{"hora": a.ts.strftime("%H:%M:%S"), "usuario": nombres.get(a.usuario_id, "") if a.usuario_id else "",
             "seccion": a.seccion_slug, "accion": ACCIONES.get(a.accion, a.accion), "tipo": a.accion,
             "detalle": a.detalle} for a in acts]
    return {"fecha": fecha.isoformat(), "secciones": sec, "lotes": lotes, "operarios": operarios, "feed": feed}
