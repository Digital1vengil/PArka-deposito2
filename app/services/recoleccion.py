"""Recolección: de los paquetes del día (ML / TN) a una lista de artículos ordenada por la ruta del depósito."""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..db import ahora, hoy
from ..models import Recoleccion, RecoleccionItem, Usuario
from . import mapeo

SIN_RUTA = mapeo.SIN_RUTA


def _skus(p: dict) -> list[str]:
    return [s.strip() for s in re.split(r"[,;]", str(p.get("sku") or "")) if s.strip()]


def lineas_de_paquetes(paquetes: list[dict]) -> list[dict]:
    """Agrupa por SKU. Un paquete con un solo SKU suma su cantidad; uno con varios SKU suma 1 de cada uno."""
    lineas: dict[str, dict] = {}
    for p in paquetes:
        ref = str(p.get("venta_id") or p.get("ventaId") or p.get("tracking") or p.get("trackingId") or "").strip()
        skus = _skus(p)
        cant = max(1, int(p.get("cantidad") or 1))
        producto = str(p.get("producto") or "").strip()
        if not skus:  # sin SKU: queda por título para vincular a mano
            skus, cant_por = [""], cant
        else:
            cant_por = cant if len(skus) == 1 else 1
        productos = [x.strip() for x in producto.split(" + ")] if len(skus) > 1 else [producto]
        for i, s in enumerate(skus):
            clave = s.upper() or ("TITULO:" + producto.upper())
            prod = productos[i] if i < len(productos) else producto
            ln = lineas.setdefault(clave, {"sku": s, "producto": prod, "cantidad": 0, "ventas": []})
            ln["cantidad"] += cant_por
            if ref and ref not in ln["ventas"]:
                ln["ventas"].append(ref)
    return list(lineas.values())


def aplicar_articulo(db: Session, it: RecoleccionItem, art) -> None:
    d = mapeo.datos_variante(art)
    it.articulo_id, it.articulo, it.color, it.talle, it.codigo_barras = (
        art.id, d["articulo"], d["color"], d["talle"], d["codigo_barras"])
    ubs = mapeo.ubicaciones_de(db, art)
    it.ubicacion = ", ".join(u.codigo for u in ubs)[:120]
    it.orden_ruta = ubs[0].orden if ubs else SIN_RUTA


def crear(db: Session, u: Usuario, paquetes: list[dict], origen: str, tipo: str, nombre: str) -> Recoleccion:
    r = Recoleccion(fecha=hoy(), usuario_id=u.id, origen=origen, tipo=tipo, nombre=nombre[:200],
                    paquetes=len(paquetes))
    db.add(r)
    for ln in lineas_de_paquetes(paquetes):
        it = RecoleccionItem(sku=ln["sku"][:200], producto=ln["producto"], cantidad=ln["cantidad"],
                             ventas=", ".join(ln["ventas"]))
        art = mapeo.articulo_por_sku(db, ln["sku"])
        if art:
            aplicar_articulo(db, it, art)
        r.items.append(it)
    db.commit()
    return r


def recalcular_ubicaciones(db: Session, r: Recoleccion) -> None:
    from ..models import Articulo
    for it in r.items:
        if it.articulo_id:
            art = db.get(Articulo, it.articulo_id)
            if art:
                aplicar_articulo(db, it, art)
    db.commit()


def orden_items(r: Recoleccion) -> list[RecoleccionItem]:
    return sorted(r.items, key=lambda i: (i.recogido >= i.cantidad, i.orden_ruta, i.ubicacion, i.articulo,
                                          i.color, i.talle, i.id))


def item_json(i: RecoleccionItem) -> dict:
    return {"id": i.id, "sku": i.sku, "producto": i.producto, "articulo_id": i.articulo_id, "articulo": i.articulo,
            "color": i.color, "talle": i.talle, "codigo_barras": i.codigo_barras, "ubicacion": i.ubicacion,
            "cantidad": i.cantidad, "recogido": i.recogido, "ventas": i.ventas,
            "completo": i.recogido >= i.cantidad}


def resumen(r: Recoleccion) -> dict:
    total = sum(i.cantidad for i in r.items)
    hecho = sum(min(i.recogido, i.cantidad) for i in r.items)
    return {"id": r.id, "nombre": r.nombre, "origen": r.origen, "tipo": r.tipo, "estado": r.estado,
            "hora": r.creado_at.strftime("%d/%m %H:%M"), "usuario": r.usuario.nombre if r.usuario else "",
            "paquetes": r.paquetes, "lineas": len(r.items), "unidades": total, "recogidas": hecho,
            "sin_vincular": sum(1 for i in r.items if not i.articulo_id),
            "sin_ubicacion": sum(1 for i in r.items if i.articulo_id and not i.ubicacion),
            "faltan": total - hecho,
            "cerrada": r.cerrada_at.strftime("%d/%m %H:%M") if r.cerrada_at else ""}


def detalle(r: Recoleccion) -> dict:
    return {**resumen(r), "items": [item_json(i) for i in orden_items(r)]}


def marcar(i: RecoleccionItem, delta: int, u: Usuario) -> None:
    i.recogido = max(0, min(i.cantidad, i.recogido + delta))
    i.recogido_por_id, i.recogido_at = u.id, ahora()
