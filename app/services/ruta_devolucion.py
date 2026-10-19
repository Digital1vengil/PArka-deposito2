"""Ruta de devolución: a qué rack va cada prenda devuelta y en qué orden recorrerlos.

- Óptimo → la ubicación asignada en Mapeo (variante > color > modelo). Si el modelo está en varios racks se
  elige el que ya es parada de la ruta o, si no, el más cerca de la persiana de salida.
- Falla → el rack de fallas (Mapeo → Croquis). Sin rack de fallas configurado, queda "sin ubicación".
- Sin ubicación conocida → parada final: se escanea el rack donde se deja y la app lo aprende para la próxima.
"""
from __future__ import annotations

import json
import logging
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal, ahora
from ..models import Articulo, Devolucion, Ubicacion, UbicacionArticulo
from . import ajustes, croquis
from .mapeo import normalizar_codigo, ubicaciones_de

log = logging.getLogger(__name__)


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
            "venta": i.venta or "", "plataforma": i.plataforma or "", "guardados": guardados(i)}


# ─────────────── Confirmación de cada prenda en su rack ───────────────
def guardados(i) -> dict[str, int]:
    """{rack: unidades} ya confirmadas de esa prenda."""
    try:
        g = json.loads(i.guardados or "{}")
        return {str(k): int(v) for k, v in g.items() if int(v) > 0} if isinstance(g, dict) else {}
    except (ValueError, TypeError):
        return {}


def _fijar(i, g: dict[str, int]) -> None:
    i.guardados = json.dumps(g, ensure_ascii=False)
    i.devuelto_cant = sum(g.values())
    i.ubicacion = ", ".join(k if n >= i.cantidad else f"{k} x{n}" for k, n in g.items())[:80]


def aprender(db: Session, d: Devolucion, i, codigo: str, usuario_id: int | None) -> int:
    """La prenda (Óptimo) quedó en un rack que Mapeo no tenía para ese artículo: se agrega para la próxima."""
    if i.estado != "OPTIMO" or not i.articulo_id:
        return 0
    fallas = ajustes.leer("mapa_rack_fallas", db)
    a, x = db.get(Articulo, i.articulo_id), ubicacion(db, codigo)
    if not a or not x or (fallas and normalizar_codigo(fallas) == x.codigo):
        return 0
    if x.codigo in {u.codigo for u in ubicaciones_de(db, a)}:
        return 0
    db.add(UbicacionArticulo(ubicacion_id=x.id, modelo=a.articulo, color="", articulo_id=a.id,
                             nota=f"Aprendido en la devolución PD-{d.id}", creado_por_id=usuario_id))
    db.flush()
    return 1


def guardar_prenda(db: Session, d: Devolucion, i, codigo: str, cantidad: int, usuario_id: int | None) -> int:
    """Suma unidades confirmadas en un rack. Devuelve cuántas ubicaciones nuevas aprendió Mapeo."""
    g = guardados(i)
    falta = i.cantidad - sum(g.values())
    if falta <= 0:
        raise ValueError("Esa prenda ya está toda guardada")
    g[codigo] = g.get(codigo, 0) + min(max(1, cantidad), falta)
    _fijar(i, g)
    i.devuelto_at = ahora()
    return aprender(db, d, i, codigo, usuario_id)


def completa(d: Devolucion) -> bool:
    propias = [i for i in d.items if i.estado != "AJENO"]
    return bool(propias) and all((i.devuelto_cant or 0) >= i.cantidad for i in propias)


# ─────────────── Excel del día en Drive: se actualiza apenas se confirma una prenda ───────────────
_lock = threading.Lock()
_estado = {"corriendo": False, "pendiente": set()}


def actualizar_excel_dia(fecha) -> None:
    """Sube el Excel de Devoluciones del día a Drive en segundo plano (si hay otra subida en curso, se encola)."""
    with _lock:
        _estado["pendiente"].add(fecha)
        if _estado["corriendo"]:
            return
        _estado["corriendo"] = True
    threading.Thread(target=_subir_pendientes, daemon=True).start()


def _subir_pendientes() -> None:
    from . import deposito_drive
    while True:
        with _lock:
            if not _estado["pendiente"]:
                _estado["corriendo"] = False
                return
            fecha = _estado["pendiente"].pop()
        try:
            with SessionLocal() as db:
                if deposito_drive.configurado(db) and deposito_drive.automatico(db):
                    deposito_drive.sincronizar(db, fecha, secciones=("devoluciones",))
        except Exception:  # noqa: BLE001 — lo vuelve a intentar el automático de cada 15 min
            log.exception("No se pudo actualizar el Excel de devoluciones del %s", fecha)


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
