"""Recolecciones de Odoo (operación "Recolectar", WH/PICK): lista de las abiertas y etiquetas por unidad.

Solo lectura en Odoo. Lo único que se guarda en la app es quién imprimió las etiquetas de cada recolección
(tabla odoo_pick_impresiones), para marcar como NUEVO lo que todavía no se imprimió.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import ahora
from ..models import Articulo, OdooPickImpresion, Usuario
from . import odoo

ESTADOS = {"draft": "Borrador", "waiting": "Esperando otra operación", "confirmed": "En espera",
           "assigned": "Listo"}
CERRADOS = {"done": "Hecho", "cancel": "Cancelado"}
DOMINIO_PICK = [["picking_type_id.sequence_code", "=", "PICK"]]
DOMINIO = DOMINIO_PICK + [["state", "in", list(ESTADOS)]]
LIMITE_TODAS = 500
CAMPOS_PICK = ["name", "origin", "partner_id", "state", "scheduled_date", "create_date", "picking_type_id"]
_CACHE: dict = {"t": 0.0, "data": None}
_CACHE_TODAS: dict = {"t": 0.0, "data": None}
CACHE_SEG = 45


def _m2o(v) -> str:
    return v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else ""


def _fecha(v) -> str:
    """Odoo guarda en UTC → hora de Buenos Aires (UTC-3), "aaaa-mm-dd hh:mm"."""
    if not isinstance(v, str) or not v:
        return ""
    try:
        return (datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S") - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return v[:16]


def leer_odoo(c: "odoo.Cliente", dominio: list | None = None, limite: int = 300) -> list[dict]:
    """Recolecciones (por defecto las abiertas) con sus renglones y los datos de cada artículo (SKU, código, nombre)."""
    picks = c.call("stock.picking", "search_read", DOMINIO if dominio is None else dominio, fields=CAMPOS_PICK, order="create_date desc", limit=limite)
    if not picks:
        return []
    moves = c.call("stock.move", "search_read", [["picking_id", "in", [p["id"] for p in picks]],
                                                  ["state", "!=", "cancel"]],
                   fields=["picking_id", "product_id", "product_uom_qty", "quantity"], order="id")
    pids = sorted({m["product_id"][0] for m in moves if m.get("product_id")})
    prods = {p["odoo_id"]: p for p in odoo.leer_productos(c, [["id", "in", pids]] + odoo.DOMINIO_TODOS)} if pids else {}
    por_pick: dict[int, list[dict]] = {}
    for m in moves:
        pid = m["product_id"][0] if m.get("product_id") else 0
        p = prods.get(pid) or {}
        crudo = _m2o(m.get("product_id"))
        por_pick.setdefault(m["picking_id"][0], []).append({
            "product_id": pid, "sku": p.get("sku", ""), "codigo_barras": p.get("codigo_barras", ""),
            "nombre": p.get("nombre") or crudo.split("] ", 1)[-1], "articulo": p.get("articulo", ""),
            "color": p.get("color", ""), "talle": p.get("talle", ""), "marca": p.get("marca", ""),
            "cantidad": int(round(m.get("product_uom_qty") or 0)), "reservado": int(round(m.get("quantity") or 0)),
        })
    out = []
    for p in picks:
        items = por_pick.get(p["id"], [])
        out.append({"id": p["id"], "nombre": p["name"], "origen": p.get("origin") or "",
                    "cliente": _m2o(p.get("partner_id")).split(", ")[0], "estado": p["state"],
                    "estado_txt": ESTADOS.get(p["state"]) or CERRADOS.get(p["state"], p["state"]),
                    "abierta": p["state"] in ESTADOS, "deposito": _m2o(p.get("picking_type_id")).split(":")[0],
                    "programada": _fecha(p.get("scheduled_date")), "creada": _fecha(p.get("create_date")),
                    "items": items, "unidades": sum(i["cantidad"] for i in items)})
    return out


def listar(db: Session, refrescar: bool = False, todas: bool = False) -> list[dict]:
    """Sin filtro de estado (todas=True) trae también las hechas y canceladas de Recolectar (las últimas LIMITE_TODAS)."""
    cache = _CACHE_TODAS if todas else _CACHE
    if refrescar or cache["data"] is None or time.time() - cache["t"] > CACHE_SEG:
        cache["data"] = leer_odoo(odoo.cliente(db), DOMINIO_PICK, LIMITE_TODAS) if todas else leer_odoo(odoo.cliente(db))
        cache["t"] = time.time()
    picks = cache["data"]
    imp = _impresiones(db, [p["id"] for p in picks])
    return [{**p, "impresion": imp.get(p["id"])} for p in picks]


def _impresiones(db: Session, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    filas = db.execute(select(OdooPickImpresion, Usuario.nombre).outerjoin(Usuario, Usuario.id == OdooPickImpresion.usuario_id)
                       .where(OdooPickImpresion.picking_id.in_(ids)).order_by(OdooPickImpresion.at)).all()
    out: dict[int, dict] = {}
    for f, nombre in filas:   # queda la última
        out[f.picking_id] = {"usuario": nombre or "", "at": f.at.strftime("%d/%m %H:%M"), "etiquetas": f.etiquetas,
                             "veces": out.get(f.picking_id, {}).get("veces", 0) + 1}
    return out


def recoleccion(db: Session, pid: int) -> dict | None:
    """Una recolección (se lee de nuevo de Odoo para imprimir lo que está ahora)."""
    c = odoo.cliente(db)
    picks = leer_odoo(c, [["id", "=", pid]] + DOMINIO_PICK)
    return picks[0] if picks else None


def completar_codigos(db: Session, items: list[dict]) -> None:
    """Si en Odoo falta el código de barras, se busca en el catálogo de la app por SKU."""
    faltan = {i["sku"] for i in items if i["sku"] and not i["codigo_barras"]}
    if not faltan:
        return
    cods = dict(db.execute(select(Articulo.sku, Articulo.codigo_barras).where(Articulo.sku.in_(faltan))).all())
    for i in items:
        if not i["codigo_barras"] and cods.get(i["sku"]):
            i["codigo_barras"] = cods[i["sku"]]


def lista_etiquetas(db: Session, pick: dict, solo: set[int] | None = None) -> list[dict]:
    """Una etiqueta por unidad pedida (en el orden de la recolección)."""
    completar_codigos(db, pick["items"])
    return [{"nombre": i["nombre"], "articulo": i["articulo"], "color": i["color"], "talle": i["talle"],
             "sku": i["sku"], "codigo_barras": i["codigo_barras"]}
            for i in pick["items"] if solo is None or i["product_id"] in solo for _ in range(max(0, i["cantidad"]))]


def registrar_impresion(db: Session, pick: dict, usuario_id: int | None, etiquetas: int) -> None:
    db.add(OdooPickImpresion(picking_id=pick["id"], nombre=pick["nombre"][:40], usuario_id=usuario_id,
                             etiquetas=etiquetas, at=ahora()))
    db.commit()
