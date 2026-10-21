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
from ..services import ruta_devolucion as ruta_svc
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


def _variante(db: Session, cod: str) -> dict:
    """Código de barras o SKU → variante lista para sumar a la devolución, o {ok: False, motivo}."""
    from ..services.catalogo import por_codigo as buscar_codigo
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


@router.get("/api/codigo")
def por_codigo(c: str = "", u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Escáner: código de barras (o SKU) → variante lista para sumar a la devolución.
    Si no se puede usar, devuelve ok=False con el motivo (para avisar sin escribir nada)."""
    cod = (c or "").strip()
    if not cod:
        return {"ok": False, "motivo": "Código vacío"}
    return _variante(db, cod)


@router.get("/api/venta")
def venta(plataforma: str = "", c: str = "", u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Nº de venta (ML) / nº de orden (TN), o la etiqueta escaneada → la compra en la plataforma."""
    if not (c or "").strip():
        return {"ok": False, "motivo": "Escribí o escaneá el número", "sin_conexion": False}
    r = svc.buscar_venta(plataforma, c)
    r.setdefault("codigo", svc.codigo_de_etiqueta(c))
    # "Es el mismo artículo": cada prenda de la venta se busca en el catálogo por su SKU para sumarla con un toque
    for it in r.get("items") or []:
        v = _variante(db, it["sku"].strip()) if (it.get("sku") or "").strip() else \
            {"ok": False, "motivo": "La venta no trae el SKU de esta prenda"}
        it["variante"] = v.get("variante")
        it["motivo"] = "" if v["ok"] else v["motivo"]
    return r


def _venta_ok(v: str) -> str:
    v = svc.codigo_de_etiqueta(v)
    return v[:40] if re.fullmatch(r"[0-9A-Za-z\-]{3,40}", v or "") else ""


NO_ES_NUESTRO = "No es nuestro artículo"


class ItemIn(BaseModel):
    articulo_id: int | None = None   # None solo para un artículo que no vendemos (estado AJENO)
    descripcion: str = ""            # qué llegó, si es un artículo que no vendemos
    llego: str = ""                  # MISMO | DISTINTO | AJENO
    reemplaza: str = ""              # artículo distinto / no lo vendemos: en lugar de qué prenda de la venta llegó
    cantidad: int = 1
    estado: str = ""       # OPTIMO | FALLA (obligatorio, por prenda; sin valor por defecto)
    plataforma: str = ""   # ML | TN (obligatorio, por prenda)
    venta: str = ""        # nº de venta (ML) o nº de orden (TN) — obligatorio, por prenda


class ConfirmarIn(BaseModel):
    items: list[ItemIn]
    plataforma: str = ""   # compatibilidad: si una prenda no trae plataforma se usa esta
    ruta: bool = False     # True = se guarda en la app y se arma la ruta; Drive recién al "Marcar como devuelto"
    yo_x: float | None = None   # dónde está la persona (punto del plano): la ruta sale de ahí
    yo_y: float | None = None


def dev_json(d: Devolucion) -> dict:
    try:
        resp = json.loads(d.drive_resp) if d.drive_ok and d.drive_resp else {}
    except ValueError:
        resp = {}
    return {"id": d.id, "hora": d.creado_at.strftime("%d/%m %H:%M"), "total": d.total, "plataforma": d.plataforma or "",
            "usuario": d.usuario.nombre if d.usuario else "", "usuario_id": d.usuario_id, "drive_ok": d.drive_ok,
            "drive_error": "" if d.drive_ok else d.drive_resp, "drive": resp, "ruta_estado": d.ruta_estado or "",
            "items": [{"articulo": i.articulo, "color": i.color, "talle": i.talle, "cantidad": i.cantidad,
                       "estado": i.estado, "plataforma": i.plataforma or "", "venta": i.venta or "", "llego": i.llego or "", "reemplaza": i.reemplaza or "", "sku": i.sku,
                       "ubicacion": i.ubicacion or "", "devuelto_cant": i.devuelto_cant or 0}
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
        if it.estado == "AJENO":   # no es nuestro artículo: solo nº de venta + el artículo que correspondía (informativo)
            it.descripcion = " ".join((it.descripcion or "").split())[:120] or NO_ES_NUESTRO
            it.articulo_id, it.llego = None, "AJENO"
        elif it.estado not in ("OPTIMO", "FALLA"):
            raise HTTPException(400, "Cada prenda tiene que tener el estado: Óptimo o Falla")
        elif it.articulo_id is None:
            raise HTTPException(400, "Falta el artículo de una prenda")
        else:
            it.llego = (it.llego or "").strip().upper() if (it.llego or "").strip().upper() in ("MISMO", "DISTINTO") else ""
        it.reemplaza = " ".join((it.reemplaza or "").split())[:200] if it.llego in ("DISTINTO", "AJENO") else ""
        it.venta = _venta_ok(it.venta)
        if not it.venta:
            raise HTTPException(400, "Cada prenda tiene que tener el número de venta (Mercado Libre) "
                                     "o el número de orden (Tienda Nube)")
    usadas = sorted({it.plataforma for it in items})
    plataforma = "+".join(usadas)
    d = Devolucion(fecha=hoy(), usuario_id=u.id, plataforma=plataforma)
    db.add(d)
    for it in items:
        if it.estado == "AJENO":
            d.items.append(DevolucionItem(articulo_id=None, articulo=it.descripcion, color="", talle="", estado="AJENO",
                                          plataforma=it.plataforma, venta=it.venta, llego="AJENO", reemplaza=it.reemplaza,
                                          cantidad=min(it.cantidad, 999)))
            continue
        a = db.get(Articulo, it.articulo_id)
        if not a or a.inactivo:
            raise HTTPException(400, "Hay un artículo que no existe o está archivado en Odoo")
        if svc.marca(a) not in ("Parka", "Puffers"):
            raise HTTPException(400, f"{a.nombre}: las devoluciones son solo de artículos Parka o Puffers")
        color, talle = svc.color_talle(a)
        d.items.append(DevolucionItem(articulo_id=a.id, codigo_barras=a.codigo_barras, sku=a.sku,
                                      articulo=a.articulo, color=color, talle=talle,
                                      estado=it.estado,
                                      plataforma=it.plataforma, venta=it.venta, llego=it.llego, reemplaza=it.reemplaza,
                                      cantidad=min(it.cantidad, 999)))
    d.total = sum(i.cantidad for i in d.items)
    db.commit()
    ventas = ", ".join(dict.fromkeys(f"{i.plataforma} {i.venta}" for i in d.items))
    registrar(db, u, "devolucion", f"PD-{d.id} · {ventas} · {d.total} unidades", "devoluciones")
    if body.ruta and any(i.estado != "AJENO" for i in d.items):
        # Paso 1 (al confirmar): se genera todo en Drive — Excel del día ("Pendiente") + Crédito y Reingreso.
        # Paso 2 (en la ruta): cada prenda guardada completa "Ubicación" y "Devuelto al rack" en el Excel del día.
        d.ruta_estado = "PENDIENTE"
        db.commit()
        ruta = ruta_svc.armar(db, d, _yo(body.yo_x, body.yo_y))
        ruta_svc.actualizar_excel_dia(d.fecha)
        _enviar(db, d)
        return {"ok": True, "devolucion": dev_json(d), "ruta": ruta}
    ruta_svc.actualizar_excel_dia(d.fecha)   # el Excel del día se sube ya (en paralelo con Crédito y Reingreso)
    _enviar(db, d)
    return {"ok": True, "devolucion": dev_json(d)}


# ─────────── Ruta de devolución: guardar cada prenda en su rack ───────────
def _dev(db: Session, did: int) -> Devolucion:
    d = db.get(Devolucion, did)
    if not d:
        raise HTTPException(404, "No existe la devolución")
    return d


def _yo(x: float | None, y: float | None) -> tuple[float, float] | None:
    return (x, y) if x is not None and y is not None else None


@router.get("/api/{did}/ruta")
def ver_ruta(did: int, x: float | None = None, y: float | None = None, u: Usuario = Depends(acceso),
             db: Session = Depends(get_db)):
    """La ruta sale de donde está la persona (x, y: punto del plano) o, sin eso, de la persiana de Ajustes."""
    return ruta_svc.armar(db, _dev(db, did), _yo(x, y))


@router.get("/api/camino")
def camino(hasta: str, x: float, y: float, u: Usuario = Depends(acceso)):
    """Camino por los pasillos desde donde está la persona hasta la parada actual."""
    from ..services import plano
    if not plano.partes(hasta):
        raise HTTPException(400, f"{hasta} no es un lugar del plano")
    return plano.camino(plano.desde_punto(x, y), hasta)


@router.get("/api/rack")
def rack(c: str = "", u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Código de barras del rack escaneado → la ubicación (de Mapeo o de Odoo)."""
    x = ruta_svc.ubicacion(db, c)
    db.commit()
    if not x:
        return {"ok": False, "motivo": f"{(c or '').strip() or 'Ese código'} no es un rack de Mapeo ni una ubicación de Odoo"}
    from ..services import plano
    return {"ok": True, "codigo": x.codigo, "descripcion": plano.describir(x.codigo) or x.descripcion}


class GuardadoIn(BaseModel):
    item_id: int
    codigo: str
    cantidad: int = 1


class DevueltoIn(BaseModel):
    guardados: list[GuardadoIn] = []


def _cerrar_devuelta(db: Session, d: Devolucion, u: Usuario) -> None:
    d.ruta_estado = "DEVUELTO"
    db.commit()
    registrar(db, u, "devolucion", f"PD-{d.id} devuelta a los racks", "devoluciones")
    if not d.drive_ok:   # normalmente ya se subió al confirmar
        _enviar(db, d)


@router.post("/api/{did}/guardar")
def guardar_en_rack(did: int, body: GuardadoIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Se confirmó una prenda en su rack: queda guardado al instante y se actualiza el Excel del día en Drive.
    Cuando se confirma la última prenda, la devolución se marca devuelta y se sube sola (Crédito y Reingreso)."""
    d = _dev(db, did)
    if d.ruta_estado != "PENDIENTE":
        raise HTTPException(409, "Esta devolución ya se cerró")
    it = next((i for i in d.items if i.id == body.item_id), None)
    if not it or it.estado == "AJENO":
        raise HTTPException(400, "Esa prenda no es de esta devolución")
    x = ruta_svc.ubicacion(db, body.codigo)
    if not x:
        raise HTTPException(400, f"{body.codigo} no es un rack cargado en Mapeo ni una ubicación de Odoo")
    if it.estado == "OPTIMO" and it.ubic_odoo and x.codigo != it.ubic_odoo:
        raise HTTPException(409, f"No coincide con Odoo: {it.articulo} {it.color} {it.talle} va en {it.ubic_odoo} "
                                 f"y escaneaste {x.codigo}")
    try:
        aprendidas = ruta_svc.guardar_prenda(db, d, it, x.codigo, body.cantidad, u.id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    db.commit()
    completa = ruta_svc.completa(d)
    ruta_svc.actualizar_excel_dia(d.fecha)   # primero el Excel del día (en segundo plano), después Crédito y Reingreso
    if completa:
        _cerrar_devuelta(db, d, u)
    return {"ok": True, "item_id": it.id, "guardados": ruta_svc.guardados(it), "completa": completa,
            "aprendidas": aprendidas, "devolucion": dev_json(d)}


@router.post("/api/{did}/devuelto")
def devuelto(did: int, body: DevueltoIn = DevueltoIn(), u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Cierra la ruta aunque falten prendas (las confirmadas ya están guardadas) y sube a Drive."""
    d = _dev(db, did)
    if d.ruta_estado != "PENDIENTE":
        raise HTTPException(409, "Esta devolución ya se cerró")
    aprendidas = 0
    items = {i.id: i for i in d.items}
    for g in body.guardados:   # compatibilidad: progreso que quedó solo en el equipo (versión anterior)
        it, x = items.get(g.item_id), ruta_svc.ubicacion(db, g.codigo)
        if not it or it.estado == "AJENO":
            raise HTTPException(400, "Hay una prenda que no es de esta devolución")
        if not x:
            raise HTTPException(400, f"{g.codigo} no es un rack cargado en Mapeo")
        if it.estado == "OPTIMO" and it.ubic_odoo and x.codigo != it.ubic_odoo:
            raise HTTPException(409, f"No coincide con Odoo: {it.articulo} va en {it.ubic_odoo} y se marcó {x.codigo}")
        ya = ruta_svc.guardados(it).get(x.codigo, 0)
        if g.cantidad > ya and (it.devuelto_cant or 0) < it.cantidad:
            aprendidas += ruta_svc.guardar_prenda(db, d, it, x.codigo, g.cantidad - ya, u.id)
    d.ruta_estado = "DEVUELTO"
    db.commit()
    ruta_svc.actualizar_excel_dia(d.fecha)
    _cerrar_devuelta(db, d, u)
    return {"ok": True, "devolucion": dev_json(d), "aprendidas": aprendidas}


@router.post("/api/{did}/sin-devolver")
def sin_devolver(did: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """No hay lugar o no hay información: se sube a Drive sin guardar las prendas en los racks."""
    d = _dev(db, did)
    if d.ruta_estado == "PENDIENTE":
        d.ruta_estado = "SIN_DEVOLVER"
        db.commit()
        registrar(db, u, "devolucion", f"PD-{d.id} guardada en Drive sin devolver a los racks", "devoluciones")
        ruta_svc.actualizar_excel_dia(d.fecha)
    if not d.drive_ok:
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
              "sku": i.sku, "codigo_barras": i.codigo_barras} for i in d.items if i.estado != "AJENO"
             for _ in range(i.cantidad)]
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
