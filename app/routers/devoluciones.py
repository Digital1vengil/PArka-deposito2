"""Sección Devoluciones: buscar artículo → color → talle → cantidad → confirmar (Drive + etiquetas)."""
from sqlalchemy.orm import selectinload
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere
from ..db import get_db, hoy
from ..models import Articulo, Devolucion, DevolucionItem, Usuario
from ..services import devoluciones as svc
from ..services.etiquetas import pdf_etiquetas
from ..services.etiquetas_html import html_etiquetas
from ..services import etiquetas_rollo as rollo
from ..templating import render

router = APIRouter(prefix="/devoluciones")
acceso = requiere("devoluciones")


@router.get("")
def pagina(request: Request, u: Usuario = Depends(acceso)):
    return render(request, "devoluciones.html", u=u)


@router.get("/api/buscar")
def buscar(q: str = "", u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return {"modelos": svc.buscar_modelos(db, q)}


@router.get("/api/codigo")
def por_codigo(c: str = "", u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Escáner: código de barras (o SKU) → variante lista para sumar a la devolución.
    Si no se puede usar, devuelve ok=False con el motivo (para avisar sin escribir nada)."""
    from ..services.catalogo import por_codigo as buscar_codigo
    cod = (c or "").strip()
    if not cod:
        return {"ok": False, "motivo": "Código vacío"}
    a = buscar_codigo(db, cod)
    if a is None:
        return {"ok": False, "motivo": f"El código {cod} no está en el catálogo"}
    if a.inactivo:
        return {"ok": False, "motivo": f"{a.nombre}: está archivado en Odoo"}
    if not a.codigo_barras:
        return {"ok": False, "motivo": f"{a.nombre}: no tiene código de barras"}
    m = svc.marca(a)
    if m not in ("Parka", "Puffers"):
        return {"ok": False, "motivo": f"{a.nombre}: es {m or 'otra marca'} — las devoluciones son solo Parka o Puffers"}
    color, talle = svc.color_talle(a)
    return {"ok": True, "variante": {"id": a.id, "articulo": a.articulo, "color": color, "talle": talle, "marca": m,
                                     "sku": a.sku, "codigo_barras": a.codigo_barras, "nombre": a.nombre}}


@router.get("/api/venta")
def venta(plataforma: str = "", c: str = "", u: Usuario = Depends(acceso)):
    """Nº de venta (ML) / nº de orden (TN), o la etiqueta escaneada → la compra en la plataforma."""
    if not (c or "").strip():
        return {"ok": False, "motivo": "Escribí o escaneá el número", "sin_conexion": False}
    r = svc.buscar_venta(plataforma, c)
    r.setdefault("codigo", svc.codigo_de_etiqueta(c))
    return r


def _venta_ok(v: str) -> str:
    v = svc.codigo_de_etiqueta(v)
    return v[:40] if re.fullmatch(r"[0-9A-Za-z\-]{3,40}", v or "") else ""


class ItemIn(BaseModel):
    articulo_id: int
    cantidad: int = 1
    estado: str = ""       # OPTIMO | FALLA (obligatorio, por prenda; sin valor por defecto)
    plataforma: str = ""   # ML | TN (obligatorio, por prenda)
    venta: str = ""        # nº de venta (ML) o nº de orden (TN) — obligatorio, por prenda


class ConfirmarIn(BaseModel):
    items: list[ItemIn]
    plataforma: str = ""   # compatibilidad: si una prenda no trae plataforma se usa esta


def dev_json(d: Devolucion) -> dict:
    try:
        resp = json.loads(d.drive_resp) if d.drive_ok and d.drive_resp else {}
    except ValueError:
        resp = {}
    return {"id": d.id, "hora": d.creado_at.strftime("%d/%m %H:%M"), "total": d.total, "plataforma": d.plataforma or "",
            "usuario": d.usuario.nombre if d.usuario else "", "usuario_id": d.usuario_id, "drive_ok": d.drive_ok,
            "drive_error": "" if d.drive_ok else d.drive_resp, "drive": resp,
            "items": [{"articulo": i.articulo, "color": i.color, "talle": i.talle, "cantidad": i.cantidad,
                       "estado": i.estado, "plataforma": i.plataforma or "", "venta": i.venta or "", "sku": i.sku}
                      for i in d.items],
            "ventas": list(dict.fromkeys(f"{i.plataforma} {i.venta}" for i in d.items if i.venta))}


def _enviar(db: Session, d: Devolucion) -> None:
    try:
        r = svc.enviar(d)
        d.drive_ok, d.drive_resp = True, json.dumps(r, ensure_ascii=False)[:2000]
    except Exception as e:  # noqa: BLE001
        d.drive_ok, d.drive_resp = False, f"No se pudo guardar en Drive: {e}"[:1000]
    db.commit()


@router.post("/api/confirmar")
def confirmar(body: ConfirmarIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    items = [i for i in body.items if i.cantidad > 0]
    if not items:
        raise HTTPException(400, "Agregá al menos un artículo")
    general = (body.plataforma or "").strip().upper()
    for it in items:
        it.plataforma = (it.plataforma or general).strip().upper()
        if it.plataforma not in ("ML", "TN"):
            raise HTTPException(400, "Cada prenda tiene que tener la plataforma: TN o ML")
        it.estado = (it.estado or "").strip().upper()
        if it.estado not in ("OPTIMO", "FALLA"):
            raise HTTPException(400, "Cada prenda tiene que tener el estado: Óptimo o Falla")
        it.venta = _venta_ok(it.venta)
        if not it.venta:
            raise HTTPException(400, "Cada prenda tiene que tener el número de venta (Mercado Libre) "
                                     "o el número de orden (Tienda Nube)")
    usadas = sorted({it.plataforma for it in items})
    plataforma = "+".join(usadas)
    d = Devolucion(fecha=hoy(), usuario_id=u.id, plataforma=plataforma)
    db.add(d)
    for it in items:
        a = db.get(Articulo, it.articulo_id)
        if not a or a.inactivo:
            raise HTTPException(400, "Hay un artículo que no existe o está archivado en Odoo")
        if svc.marca(a) not in ("Parka", "Puffers"):
            raise HTTPException(400, f"{a.nombre}: las devoluciones son solo de artículos Parka o Puffers")
        color, talle = svc.color_talle(a)
        d.items.append(DevolucionItem(articulo_id=a.id, codigo_barras=a.codigo_barras, sku=a.sku,
                                      articulo=a.articulo, color=color, talle=talle,
                                      estado=it.estado,
                                      plataforma=it.plataforma, venta=it.venta,
                                      cantidad=min(it.cantidad, 999)))
    d.total = sum(i.cantidad for i in d.items)
    db.commit()
    ventas = ", ".join(dict.fromkeys(f"{i.plataforma} {i.venta}" for i in d.items))
    registrar(db, u, "devolucion", f"PD-{d.id} · {ventas} · {d.total} unidades", "devoluciones")
    _enviar(db, d)
    return {"ok": True, "devolucion": dev_json(d)}


@router.post("/api/{did}/reintentar")
def reintentar(did: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    d = db.get(Devolucion, did)
    if not d:
        raise HTTPException(404)
    if d.drive_ok:
        return {"ok": True, "devolucion": dev_json(d)}
    _enviar(db, d)
    if not d.drive_ok:
        raise HTTPException(502, d.drive_resp)
    return {"ok": True, "devolucion": dev_json(d)}


@router.get("/api/historial")
def historial(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    ds = db.scalars(select(Devolucion).options(selectinload(Devolucion.items)).order_by(Devolucion.id.desc()).limit(25)).all()
    return {"devoluciones": [dev_json(d) for d in ds]}


def _lista_etiquetas(db: Session, did: int) -> tuple[Devolucion, list[dict]]:
    d = db.get(Devolucion, did)
    if not d:
        raise HTTPException(404)
    nombres = {a.id: a.nombre for a in db.scalars(select(Articulo).where(
        Articulo.id.in_([i.articulo_id for i in d.items if i.articulo_id]))).all()}
    lista = [{"nombre": nombres.get(i.articulo_id, ""), "articulo": i.articulo, "color": i.color, "talle": i.talle,
              "sku": i.sku, "codigo_barras": i.codigo_barras} for i in d.items for _ in range(i.cantidad)]
    return d, lista


@router.get("/{did}/etiquetas.html")
def etiquetas_html(did: int, desde: int = 1, guias: bool = False, u: Usuario = Depends(acceso),
                   db: Session = Depends(get_db)):
    d, lista = _lista_etiquetas(db, did)
    return HTMLResponse(html_etiquetas(lista, desde=desde, guias=guias, titulo=f"Etiquetas PD-{d.id}"))


@router.get("/{did}/etiquetas.pdf")
def etiquetas(did: int, desde: int = 1, guias: bool = False, u: Usuario = Depends(acceso),
              db: Session = Depends(get_db)):
    d, lista = _lista_etiquetas(db, did)
    return Response(pdf_etiquetas(lista, desde=desde, guias=guias), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="etiquetas_PD-{d.id}.pdf"'})


# ─────────── Etiquetas en rollo Honeywell (40x25, de a 2) ───────────
class RolloIn(BaseModel):
    filas: int = 0   # 0 = todas; 1 = fila de prueba


@router.post("/api/{did}/imprimir-rollo")
def imprimir_rollo(did: int, body: RolloIn = RolloIn(), u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Manda el ZPL directo a la impresora de etiquetas (spooler de Windows RAW o IP:9100)."""
    d, lista = _lista_etiquetas(db, did)
    try:
        return rollo.imprimir(lista, db, f"Etiquetas PD-{d.id}", max_filas=max(0, body.filas))
    except rollo.ImpresoraError as e:
        raise HTTPException(502, str(e)) from e


@router.get("/{did}/etiquetas-rollo.html")
def etiquetas_rollo_html(did: int, filas: int = 0, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Impresión por el driver de Windows (página de {ancho} x 25 mm por fila)."""
    d, lista = _lista_etiquetas(db, did)
    return HTMLResponse(rollo.build_html(lista, rollo.config(db), f"Etiquetas PD-{d.id}", max_filas=max(0, filas)))


@router.get("/{did}/etiquetas.zpl")
def etiquetas_zpl(did: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    d, lista = _lista_etiquetas(db, did)
    return Response(rollo.build_zpl(lista, rollo.config(db)).encode("utf-8"), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="etiquetas_PD-{d.id}.zpl"'})
