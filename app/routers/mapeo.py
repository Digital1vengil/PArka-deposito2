"""Sección Mapeo del depósito: ubicaciones, qué artículo hay en cada una, mapa y etiquetas de ubicación."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere
from ..db import get_db
from ..models import Articulo, SkuVinculo, Ubicacion, UbicacionArticulo, Usuario
from ..services import devoluciones as dsvc
from ..services import mapeo as svc
from ..services.etiquetas import pdf_etiquetas
from ..services.etiquetas_html import html_etiquetas
from ..templating import render

router = APIRouter(prefix="/mapeo")
acceso = requiere("mapeo")
TODOS = Articulo.id.is_not(None)


@router.get("")
def pagina(request: Request, u: Usuario = Depends(acceso)):
    return render(request, "mapeo.html", u=u)


def _conteos(db: Session) -> dict[int, int]:
    return dict(db.execute(select(UbicacionArticulo.ubicacion_id, func.count())
                           .group_by(UbicacionArticulo.ubicacion_id)).all())


@router.get("/api/ubicaciones")
def listar(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    cs = _conteos(db)
    us = db.scalars(select(Ubicacion).order_by(Ubicacion.orden, Ubicacion.codigo)).all()
    return {"ubicaciones": [svc.ubicacion_json(x, cs.get(x.id, 0)) for x in us]}


class LoteIn(BaseModel):
    zona: str = ""
    pasillo: str
    modulo_desde: int = 1
    modulo_hasta: int = 1
    niveles: int = 1


@router.post("/api/ubicaciones/lote")
def crear_lote(body: LoteIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    pasillo = svc.normalizar_codigo(body.pasillo)[:10]
    if not pasillo:
        raise HTTPException(400, "Poné el pasillo (ej: A)")
    d, h = sorted((max(0, body.modulo_desde), max(0, body.modulo_hasta)))
    niveles = max(0, min(body.niveles, 20))
    if (h - d + 1) * max(1, niveles) > 500:
        raise HTTPException(400, "Son demasiadas ubicaciones de una vez (máximo 500)")
    orden = (db.scalar(select(func.max(Ubicacion.orden))) or 0)
    existentes = set(db.scalars(select(Ubicacion.codigo)).all())
    creadas = 0
    for m in range(d, h + 1):
        for n in (range(1, niveles + 1) if niveles else [0]):
            cod = svc.armar_codigo(pasillo, m, n, body.zona)
            if cod in existentes:
                continue
            orden += 1
            db.add(Ubicacion(codigo=cod, zona=body.zona.strip()[:40], pasillo=pasillo, modulo=m, nivel=n, orden=orden))
            existentes.add(cod)
            creadas += 1
    db.commit()
    registrar(db, u, "ubicaciones_creadas", f"Pasillo {pasillo}: {creadas} ubicaciones", "mapeo")
    return {"ok": True, "creadas": creadas}


class UbicacionIn(BaseModel):
    descripcion: str | None = None
    orden: int | None = None
    activa: bool | None = None
    zona: str | None = None


@router.put("/api/ubicaciones/{uid}")
def editar(uid: int, body: UbicacionIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    x = db.get(Ubicacion, uid) or _404()
    if body.descripcion is not None:
        x.descripcion = body.descripcion.strip()[:200]
    if body.orden is not None:
        x.orden = max(0, body.orden)
    if body.activa is not None:
        x.activa = body.activa
    if body.zona is not None:
        x.zona = body.zona.strip()[:40]
    db.commit()
    return {"ok": True, "ubicacion": svc.ubicacion_json(x)}


@router.delete("/api/ubicaciones/{uid}")
def borrar(uid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    x = db.get(Ubicacion, uid) or _404()
    cod = x.codigo
    db.delete(x)
    db.commit()
    registrar(db, u, "ubicacion_borrada", cod, "mapeo")
    return {"ok": True}


class ReordenarIn(BaseModel):
    modo: str = "zigzag"  # zigzag: ida por un pasillo y vuelta por el siguiente | codigo: orden alfabético


@router.post("/api/ubicaciones/reordenar")
def reordenar(body: ReordenarIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    us = db.scalars(select(Ubicacion)).all()
    pasillos = sorted({(x.zona, x.pasillo) for x in us})
    pos = {p: i for i, p in enumerate(pasillos)}

    def clave(x: Ubicacion):
        i = pos[(x.zona, x.pasillo)]
        mod = -x.modulo if (body.modo == "zigzag" and i % 2) else x.modulo
        return (i, mod, x.nivel, x.codigo)
    for n, x in enumerate(sorted(us, key=clave), start=1):
        x.orden = n * 10
    db.commit()
    return {"ok": True}


@router.get("/api/ubicacion")
def por_codigo(codigo: str, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    x = db.scalar(select(Ubicacion).where(Ubicacion.codigo == svc.normalizar_codigo(codigo)))
    if not x:
        raise HTTPException(404, f"No existe la ubicación {codigo}")
    return {"ubicacion": svc.ubicacion_json(x), "asignaciones": [svc.asignacion_json(a) for a in x.asignaciones]}


@router.get("/api/ubicaciones/{uid}")
def detalle(uid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    x = db.get(Ubicacion, uid) or _404()
    return {"ubicacion": svc.ubicacion_json(x), "asignaciones": [svc.asignacion_json(a) for a in x.asignaciones]}


@router.get("/api/buscar")
def buscar(q: str = "", u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Artículos (todas las marcas con código de barras) y dónde está cada uno."""
    modelos = dsvc.buscar_modelos(db, q, filtro=TODOS, limite=20)
    for m in modelos:
        filas = db.scalars(select(UbicacionArticulo).where(
            (func.upper(UbicacionArticulo.modelo) == m["articulo"].upper()) |
            UbicacionArticulo.articulo_id.in_([v["id"] for v in m["variantes"]]))).all()
        m["ubicaciones"] = [svc.asignacion_json(a) for a in sorted(filas, key=lambda a: a.ubicacion.orden)]
    return {"modelos": modelos}


class AsignarIn(BaseModel):
    ubicacion_id: int
    modelo: str = ""
    color: str = ""
    articulo_id: int | None = None
    nota: str = ""


@router.post("/api/asignar")
def asignar(body: AsignarIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    x = db.get(Ubicacion, body.ubicacion_id) or _404()
    modelo, color = body.modelo.strip(), body.color.strip()
    if body.articulo_id:
        a = db.get(Articulo, body.articulo_id) or _404()
        modelo, color = a.articulo, ""
    if not modelo:
        raise HTTPException(400, "Elegí el artículo")
    ya = db.scalar(select(UbicacionArticulo).where(
        UbicacionArticulo.ubicacion_id == x.id, func.upper(UbicacionArticulo.modelo) == modelo.upper(),
        UbicacionArticulo.color == color,
        (UbicacionArticulo.articulo_id == body.articulo_id) if body.articulo_id else UbicacionArticulo.articulo_id.is_(None)))
    if ya:
        return {"ok": True, "repetido": True, "asignacion": svc.asignacion_json(ya)}
    a = UbicacionArticulo(ubicacion_id=x.id, modelo=modelo[:120], color=color[:60], articulo_id=body.articulo_id,
                          nota=body.nota.strip()[:120], creado_por_id=u.id)
    db.add(a)
    db.commit()
    registrar(db, u, "ubicacion_asignada", f"{modelo} {color} → {x.codigo}".replace("  ", " "), "mapeo")
    return {"ok": True, "asignacion": svc.asignacion_json(a)}


@router.delete("/api/asignacion/{aid}")
def quitar(aid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    a = db.get(UbicacionArticulo, aid) or _404()
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.get("/api/vinculos")
def vinculos(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    vs = db.scalars(select(SkuVinculo).order_by(SkuVinculo.id.desc()).limit(300)).all()
    return {"vinculos": [{"id": v.id, "sku": v.sku_externo, "articulo": v.articulo.nombre if v.articulo else "?",
                          "fecha": v.creado_at.strftime("%d/%m/%Y")} for v in vs]}


@router.delete("/api/vinculos/{vid}")
def borrar_vinculo(vid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    v = db.get(SkuVinculo, vid) or _404()
    db.delete(v)
    db.commit()
    return {"ok": True}


def _lista_ubicaciones(db: Session, ids: str) -> list[dict]:
    lista_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    q = select(Ubicacion).order_by(Ubicacion.orden, Ubicacion.codigo)
    if lista_ids:
        q = q.where(Ubicacion.id.in_(lista_ids))
    us = db.scalars(q).all()
    if not us:
        raise HTTPException(400, "No hay ubicaciones para imprimir")
    lista = []
    for x in us:
        partes = [f"Pasillo {x.pasillo}" if x.pasillo else "", f"Módulo {x.modulo}" if x.modulo else "",
                  f"Nivel {x.nivel}" if x.nivel else ""]
        lista.append({"nombre": f"UBICACIÓN {x.codigo}", "sku": " · ".join(p for p in [x.zona] + partes if p),
                      "codigo_barras": x.codigo})
    return lista


@router.get("/etiquetas.html")
def etiquetas_html(ids: str = "", desde: int = 1, guias: bool = False, u: Usuario = Depends(acceso),
                   db: Session = Depends(get_db)):
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html_etiquetas(_lista_ubicaciones(db, ids), desde=desde, guias=guias,
                                       titulo="Etiquetas de ubicación"))


@router.get("/etiquetas.pdf")
def etiquetas(ids: str = "", desde: int = 1, guias: bool = False, u: Usuario = Depends(acceso),
              db: Session = Depends(get_db)):
    lista = _lista_ubicaciones(db, ids)
    return Response(pdf_etiquetas(lista, desde=desde, guias=guias), media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="etiquetas_ubicaciones.pdf"'})


def _404():
    raise HTTPException(404, "No encontrado")
