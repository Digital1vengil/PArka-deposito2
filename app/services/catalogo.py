"""Catálogo único de artículos para todas las secciones.

- `sincronizar()` trae de Odoo todos los productos (SKU, código de barras, color, talle, marca, stock).
- `por_codigo()` / `buscar()` / `modelo()` son lo que deberían usar las secciones para encontrar artículos.
  Si un código escaneado no está en la base local y Odoo está configurado, se consulta a Odoo en el momento
  y el artículo queda guardado.
- La API común está en /api/catalogo (routers/catalogo.py) y en el navegador `Catalogo.*` (static/app.js).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import SessionLocal, ahora
from ..models import Ajuste, Articulo
from ..parsers import normalizar_codigo
from . import odoo

log = logging.getLogger("parka.catalogo")

CAMPOS_SYNC = ("sku", "codigo_barras", "nombre", "articulo", "variante", "color", "talle", "color_nombre",
               "marca", "categoria", "stock", "meli_id", "inactivo", "odoo_id")
CLAVE_ESTADO = "odoo_sync_estado"
_LOCK = threading.Lock()
ESTADO = {"estado": "libre", "mensaje": ""}  # estado de la sincronización en curso (en memoria)
_NO_ESTA: dict[str, float] = {}  # códigos que Odoo no tiene (para no consultar de nuevo enseguida)
NO_ESTA_SEG = 600


# ───────────── Datos para mostrar / API ─────────────
def a_json(a: Articulo) -> dict:
    from .devoluciones import color_talle, marca as marca_de
    color, talle = color_talle(a)
    return {"id": a.id, "codigo_barras": a.codigo_barras, "sku": a.sku, "nombre": a.nombre, "articulo": a.articulo,
            "variante": a.variante, "color": a.color, "color_nombre": a.color_nombre or color, "talle": talle,
            "marca": a.marca or marca_de(a), "categoria": a.categoria or "", "stock": a.stock,
            "meli_id": a.meli_id or "", "odoo_id": a.odoo_id or None, "inactivo": bool(a.inactivo)}


def _guardar_estado(db: Session, datos: dict) -> None:
    a = db.get(Ajuste, CLAVE_ESTADO)
    txt = json.dumps(datos, ensure_ascii=False)
    if a is None:
        db.add(Ajuste(clave=CLAVE_ESTADO, valor=txt))
    else:
        a.valor = txt
    db.commit()


def ultima_sync(db: Session) -> dict:
    a = db.get(Ajuste, CLAVE_ESTADO)
    try:
        return json.loads(a.valor) if a and a.valor else {}
    except ValueError:
        return {}


def estado(db: Session) -> dict:
    total = db.scalar(select(func.count()).select_from(Articulo).where(Articulo.inactivo.is_not(True))) or 0
    con_codigo = db.scalar(select(func.count()).where(Articulo.codigo_barras != "", Articulo.inactivo.is_not(True))) or 0
    de_odoo = db.scalar(select(func.count()).where(Articulo.odoo_id > 0)) or 0
    return {"configurado": odoo.configurado(db), "total": total, "con_codigo": con_codigo, "de_odoo": de_odoo,
            "ultima": ultima_sync(db), "en_curso": dict(ESTADO)}


# ───────────── Sincronización ─────────────
def _aplicar(x: Articulo, d: dict) -> bool:
    cambio = False
    for k in CAMPOS_SYNC:
        if k in d and getattr(x, k) != d[k]:
            setattr(x, k, d[k])
            cambio = True
    if cambio:
        x.actualizado_at = ahora()
    return cambio


def guardar_productos(db: Session, productos: list[dict], completo: bool) -> dict:
    """Inserta/actualiza artículos con lo que vino de Odoo. Con `completo`, los que ya no están se archivan."""
    locales = db.scalars(select(Articulo)).all()
    por_odoo = {a.odoo_id: a for a in locales if a.odoo_id}
    por_codigo = {a.codigo_barras: a for a in locales if a.codigo_barras}
    por_sku = {a.sku.upper(): a for a in locales if a.sku}
    nuevos = actualizados = archivados = 0
    vistos: set[int] = set()
    for d in productos:
        x = por_odoo.get(d["odoo_id"])
        if x is None and d["codigo_barras"]:
            c = por_codigo.get(d["codigo_barras"])
            x = c if c is not None and not c.odoo_id else None
        if x is None and d["sku"]:
            c = por_sku.get(d["sku"].upper())
            x = c if c is not None and not c.odoo_id else None
        # Un código de barras pertenece a un solo artículo: si lo tenía otro (dato viejo), se le saca.
        if d["codigo_barras"]:
            otro = por_codigo.get(d["codigo_barras"])
            if otro is not None and otro is not x and otro.odoo_id != d["odoo_id"]:
                otro.codigo_barras = ""
        if x is None:
            x = Articulo(**{k: d[k] for k in CAMPOS_SYNC if k in d})
            db.add(x)
            nuevos += 1
        elif _aplicar(x, d):
            actualizados += 1
        por_odoo[d["odoo_id"]] = x
        if d["codigo_barras"]:
            por_codigo[d["codigo_barras"]] = x
        if d["sku"]:
            por_sku[d["sku"].upper()] = x
        vistos.add(d["odoo_id"])
    if completo:
        for oid, a in por_odoo.items():
            if oid not in vistos and not a.inactivo:
                a.inactivo = True
                archivados += 1
        # Los que venían del PDF/Excel y no aparecen en Odoo se archivan (no se borran: puede haber
        # devoluciones o conteos que los usan). Por seguridad, solo si Odoo devolvió un catálogo completo.
        viejos = [a for a in locales if not a.odoo_id and not a.inactivo]
        activos = sum(1 for a in locales if not a.inactivo)
        if viejos and len(productos) >= 0.5 * activos:
            for a in viejos:
                a.inactivo = True
                archivados += 1
    db.commit()
    return {"nuevos": nuevos, "actualizados": actualizados, "archivados": archivados}


def sincronizar(db: Session, quien: str = "automático") -> dict:
    """Sincronización completa desde Odoo (unos 6700 productos, tarda menos de un minuto)."""
    if not _LOCK.acquire(blocking=False):
        raise odoo.OdooError("Ya hay una sincronización en curso")
    inicio = time.time()
    try:
        ESTADO.update(estado="procesando", mensaje="Conectando con Odoo…")
        c = odoo.cliente(db)
        c.login()
        ESTADO.update(mensaje="Leyendo productos de Odoo…")
        productos = odoo.leer_productos(c)
        if not productos:
            raise odoo.OdooError("Odoo no devolvió productos (¿el usuario tiene permiso de Inventario?)")
        ESTADO.update(mensaje=f"Guardando {len(productos)} artículos…")
        r = guardar_productos(db, productos, completo=True)
        r.update(ok=True, cantidad=len(productos), fecha=ahora().isoformat(timespec="seconds"), quien=quien,
                 segundos=round(time.time() - inicio, 1),
                 con_codigo=sum(1 for p in productos if p["codigo_barras"]))
        _guardar_estado(db, r)
        ESTADO.update(estado="listo", mensaje=(f"Listo: {r['cantidad']} artículos de Odoo ({r['nuevos']} nuevos, "
                                               f"{r['actualizados']} actualizados, {r['archivados']} archivados)"))
        return r
    except Exception as e:
        db.rollback()
        msg = str(e) if isinstance(e, odoo.OdooError) else f"{type(e).__name__}: {e}"
        previo = ultima_sync(db)
        _guardar_estado(db, {**{k: v for k, v in previo.items() if k != "error"}, "ok": False, "error": msg,
                             "fecha_error": ahora().isoformat(timespec="seconds")})
        ESTADO.update(estado="error", mensaje=f"Error: {msg}")
        raise odoo.OdooError(msg) from e
    finally:
        _LOCK.release()


def sincronizar_en_segundo_plano(quien: str) -> None:
    if ESTADO.get("estado") == "procesando":
        raise odoo.OdooError("Ya hay una sincronización en curso")
    ESTADO.update(estado="procesando", mensaje="Empezando…")

    def correr():
        db = SessionLocal()
        try:
            sincronizar(db, quien)
        except odoo.OdooError as e:
            log.warning("Sincronización con Odoo: %s", e)
        finally:
            db.close()
    threading.Thread(target=correr, daemon=True).start()


def toca_sincronizar(db: Session) -> bool:
    """Para el scheduler: ¿pasaron los minutos configurados desde la última sincronización?"""
    from . import ajustes
    if not odoo.configurado(db) or ESTADO.get("estado") == "procesando":
        return False
    try:
        minutos = int(ajustes.leer("odoo_sync_minutos", db) or 0)
    except ValueError:
        minutos = 60
    if minutos <= 0:
        return False
    u = ultima_sync(db)
    ref = u.get("fecha_error") if u.get("ok") is False else u.get("fecha")
    if not ref:
        return True
    try:
        return (ahora() - datetime.fromisoformat(ref)).total_seconds() >= minutos * 60
    except ValueError:
        return True


# ───────────── Búsqueda (lo que usan las secciones) ─────────────
def _local_por_codigo(db: Session, cod: str) -> Articulo | None:
    a = db.scalar(select(Articulo).where(Articulo.codigo_barras == cod).order_by(Articulo.inactivo).limit(1))
    if a is None:
        a = db.scalar(select(Articulo).where(func.upper(Articulo.sku) == cod.upper()).order_by(Articulo.inactivo).limit(1))
    return a


def por_codigo(db: Session, codigo: str, consultar_odoo: bool = True) -> Articulo | None:
    """Código de barras (o SKU de Odoo) → artículo. Si no está local, lo busca en Odoo y lo guarda."""
    cod = normalizar_codigo(codigo)
    if not cod:
        return None
    a = _local_por_codigo(db, cod)
    if a is not None or not consultar_odoo or not odoo.configurado(db):
        return a
    if time.time() - _NO_ESTA.get(cod, 0) < NO_ESTA_SEG:
        return None
    try:
        c = odoo.cliente(db, timeout=8)
        dominio = ["|", ["barcode", "=", cod], ["default_code", "=ilike", cod]] + DOMINIO_ACTIVOS_Y_NO
        productos = odoo.leer_productos(c, dominio=dominio, limite=5)
    except odoo.OdooError as e:
        log.info("Consulta a Odoo por %s: %s", cod, e)
        return None
    if not productos:
        _NO_ESTA[cod] = time.time()
        return None
    guardar_productos(db, productos, completo=False)
    return _local_por_codigo(db, cod)


DOMINIO_ACTIVOS_Y_NO = ["|", ["active", "=", True], ["active", "=", False]]


def buscar(db: Session, q: str, limite: int = 100, marca: str = "", con_codigo: bool = False,
           incluir_inactivos: bool = False) -> tuple[list[Articulo], bool]:
    """Devuelve (artículos, exacto). Exacto = el texto era un código de barras o SKU."""
    q = (q or "").strip()
    if not q:
        return [], False
    a = por_codigo(db, q, consultar_odoo=q.isdigit() and len(q) >= 8)
    if a is not None:
        return [a], True
    cond = [or_(Articulo.nombre.ilike(f"%{p}%"), Articulo.sku.ilike(f"%{p}%"), Articulo.codigo_barras.like(f"%{p}%"),
                Articulo.color_nombre.ilike(f"%{p}%")) for p in q.split()]
    if not incluir_inactivos:
        cond.append(Articulo.inactivo.is_not(True))
    if con_codigo:
        cond.append(Articulo.codigo_barras != "")
    if marca:
        cond.append(func.lower(Articulo.marca) == marca.lower())
    res = db.scalars(select(Articulo).where(*cond).order_by(Articulo.articulo, Articulo.sku).limit(limite)).all()
    return list(res), False


def modelo(db: Session, articulo: str, incluir_inactivos: bool = False) -> list[Articulo]:
    """Todas las variantes (color / talle / marca) de un modelo, p. ej. "THOR 00114"."""
    cond = [func.upper(Articulo.articulo) == (articulo or "").strip().upper()]
    if not incluir_inactivos:
        cond.append(Articulo.inactivo.is_not(True))
    return list(db.scalars(select(Articulo).where(*cond).order_by(Articulo.color, Articulo.id)).all())


def stock_en_vivo(db: Session, articulos: list[Articulo]) -> dict[int, float]:
    """Stock actual en Odoo (qty_available) para estos artículos; actualiza el guardado."""
    ids = {a.odoo_id: a for a in articulos if a.odoo_id}
    if not ids:
        return {}
    c = odoo.cliente(db, timeout=15)
    out = {}
    for f in c.call("product.product", "read", list(ids), fields=["qty_available"]):
        a = ids.get(f["id"])
        if a is not None:
            a.stock = float(f.get("qty_available") or 0)
            out[a.id] = a.stock
    db.commit()
    return out
