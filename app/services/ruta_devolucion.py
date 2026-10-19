"""Ruta de devolución: a qué rack va cada prenda devuelta y en qué orden recorrerlos.

- Óptimo → la ubicación asignada en Mapeo (variante > color > modelo). Si el modelo está en varios racks se
  elige el que ya es parada de la ruta o, si no, el más cerca de la persiana de salida.
- Falla → el rack de fallas (Mapeo → Croquis). Sin rack de fallas configurado, queda "sin ubicación".
- Sin ubicación conocida → parada final: se escanea el rack donde se deja y la app lo aprende para la próxima.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Articulo, Devolucion, Ubicacion
from . import ajustes, croquis
from .mapeo import normalizar_codigo, ubicaciones_de


def ubicacion(db: Session, codigo: str) -> Ubicacion | None:
    cod = normalizar_codigo(codigo)
    if not cod:
        return None
    u = db.scalar(select(Ubicacion).where(Ubicacion.codigo == cod))
    return u if u and u.activa else None


def _cerca(codigo: str, desde: str) -> int:
    a, s = croquis.acceso(codigo), croquis.inicio(desde)
    return abs(a[0] - s[0]) + abs(a[1] - s[1]) if a else 10 ** 6


def item_json(i) -> dict:
    return {"item_id": i.id, "articulo": i.articulo, "color": i.color, "talle": i.talle, "sku": i.sku,
            "codigo_barras": i.codigo_barras, "cantidad": i.cantidad, "estado": i.estado,
            "venta": i.venta or "", "plataforma": i.plataforma or ""}


def armar(db: Session, d: Devolucion) -> dict:
    desde = ajustes.leer("mapa_inicio", db) or "P1"
    if desde not in croquis.PUERTAS:
        desde = "P1"
    fallas = ubicacion(db, ajustes.leer("mapa_rack_fallas", db))
    candidatos: list[tuple[object, list[str]]] = []
    for i in d.items:
        if i.estado == "AJENO" or not i.articulo_id:
            continue
        if i.estado == "FALLA":
            cods = [fallas.codigo] if fallas else []
        else:
            a = db.get(Articulo, i.articulo_id)
            cods = [u.codigo for u in ubicaciones_de(db, a)] if a else []
        candidatos.append((i, cods))
    paradas: dict[str, list] = {}
    sin: list = []
    # primero las que tienen un solo lugar posible: fijan paradas
    for i, cods in sorted(candidatos, key=lambda x: len(x[1]) != 1):
        if not cods:
            sin.append(i)
            continue
        ya = [c for c in cods if c in paradas]
        elegido = ya[0] if ya else min(cods, key=lambda c: (_cerca(c, desde), cods.index(c)))
        paradas.setdefault(elegido, []).append((i, cods))
    r = croquis.ordenar(list(paradas), desde)
    salida = []
    for n, (cod, tramo) in enumerate(zip(r["orden"], r["tramos"]), start=1):
        u = ubicacion(db, cod)
        salida.append({"n": n, "codigo": cod, "descripcion": croquis.describir(cod) or (u.descripcion if u else ""),
                       "es_fallas": bool(fallas and cod == fallas.codigo), "tramo": tramo,
                       "punto": list(croquis.acceso(cod) or []),
                       "items": [dict(item_json(i), alternativas=[c for c in cods if c != cod])
                                 for i, cods in paradas[cod]]})
    return {"devolucion_id": d.id, "estado": d.ruta_estado or "", "plano": croquis.plano(),
            "inicio": {"clave": desde, "nombre": croquis.PUERTAS[desde][0], "punto": list(croquis.inicio(desde))},
            "rack_fallas": fallas.codigo if fallas else "", "paradas": salida,
            "sin_ubicacion": [item_json(i) for i in sin], "metros_aprox": r["metros_aprox"]}
