"""Devoluciones: variantes del catálogo (color / talle) y envío al Apps Script "Devoluciones ML"."""
from __future__ import annotations

import json
import re
import urllib.request

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .. import config
from ..models import Articulo, Devolucion

TALLES = re.compile(r"^(XXXS|XXS|XS|S|M|L|XL|XXL|XXXL|XXXXL|[2-6]XL|U|UNICO|ÚNICO|T\d|\d{1,2})$", re.I)
MARCAS = {"no brand", "nobrand", "parka", "parca", "manki", "puffers"}


def es_parka_o_puffers():
    """Las devoluciones son siempre de artículos Parka o Puffers (SKU ...-PARK / ...-PUFE)."""
    return or_(Articulo.sku.like("%-PARK"), Articulo.sku.like("%-PUFE"),
               Articulo.variante.ilike("%parka%"), Articulo.variante.ilike("%parca%"),
               Articulo.variante.ilike("%puffers%"))


def es_no_brand():
    return or_(Articulo.sku.like("%-NOBR"), Articulo.variante.ilike("%no brand%"),
               Articulo.variante.ilike("%nobrand%"))


def es_conteo():
    """El conteo incluye Parka, Puffers y NO BRAND."""
    return or_(es_parka_o_puffers(), es_no_brand())


def color_talle(a: Articulo) -> tuple[str, str]:
    """De "00AXE (Navystripes, XXL)" / "ABBA 00018 (XL, Parka)" saca color y talle."""
    partes = [p.strip() for p in (a.variante or "").split(",") if p.strip()]
    talle = a.talle or next((p for p in partes if TALLES.match(p)), "")
    colores = [p for p in partes if p.upper() != talle.upper() and p.lower() not in MARCAS and not TALLES.match(p)]
    color = ", ".join(colores) or a.color or "Único"
    return color, talle or "Único"


def marca(a: Articulo) -> str:
    sku = (a.sku or "").upper()
    if sku.endswith("-PUFE") or "puffers" in (a.variante or "").lower():
        return "Puffers"
    if sku.endswith("-PARK") or any(k in (a.variante or "").lower() for k in ("parka", "parca")):
        return "Parka"
    partes = [p.strip() for p in (a.variante or "").split(",") if p.strip()]
    m = next((p for p in partes if p.lower() in MARCAS), "")
    if sku.endswith("-NOBR") or m.lower() in ("no brand", "nobrand"):
        return "No Brand"
    return m.capitalize()


# Equivalencias de búsqueda: "W7", "w 7", "W-7", "w007" → W-00007 (género + número del SKU de Odoo,
# 1-CAMP-W-00007-…); "7" o "114" → 00007 / 00114 (número del modelo con ceros a 5 dígitos).
_GEN_NUM = re.compile(r"^([A-Za-z])[\s\-_./]*0*(\d{1,5})$")
_NUM = re.compile(r"^0*(\d{1,5})$")


def tokens_busqueda(q: str) -> list[str]:
    """Separa la búsqueda en palabras, juntando "W 7" / "W - 7" en un solo término."""
    q = re.sub(r"(?<![A-Za-z0-9])([A-Za-z])[\s\-_./]+(\d{1,5})(?![0-9])", r"\1\2", q.strip())
    return [t for t in q.split() if t]


def condicion_token(p: str):
    """Condición de una palabra: nombre o SKU que la contengan, más la equivalencia con el número del SKU."""
    ops = [Articulo.nombre.ilike(f"%{p}%"), Articulo.sku.ilike(f"%{p}%")]
    m = _GEN_NUM.match(p)
    if m and int(m.group(2)) > 0:
        g, n = m.group(1).upper(), int(m.group(2))
        ops.append(Articulo.sku.ilike(f"%-{g}-{n:05d}-%"))
    m = _NUM.match(p)
    if m and int(m.group(1)) > 0:
        n = int(m.group(1))
        ops += [Articulo.sku.ilike(f"%-{n:05d}-%"), Articulo.nombre.ilike(f"%{n:05d}%")]
    return or_(*ops)


def buscar_modelos(db: Session, q: str, limite: int = 30, filtro=None) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    filtro = es_parka_o_puffers() if filtro is None else filtro
    base = select(Articulo).where(Articulo.codigo_barras != "", Articulo.inactivo.is_not(True), filtro)
    if q.isdigit() and len(q) >= 8:
        from .catalogo import por_codigo
        por_codigo(db, q)  # si el código no está local, lo trae de Odoo
    exacto = db.scalar(base.where(or_(Articulo.codigo_barras == q, Articulo.sku == q.upper())))
    if exacto:
        modelos_q = [exacto.articulo]
    else:
        cond = [condicion_token(p) for p in tokens_busqueda(q)]
        modelos_q = []
        for (m,) in db.execute(select(Articulo.articulo).where(Articulo.codigo_barras != "", Articulo.inactivo.is_not(True), filtro, *cond)
                               .distinct().order_by(Articulo.articulo).limit(limite)).all():
            modelos_q.append(m)
    salida = []
    for m in modelos_q:
        vs = db.scalars(base.where(Articulo.articulo == m).order_by(Articulo.id)).all()
        variantes = []
        for a in vs:
            color, talle = color_talle(a)
            variantes.append({"id": a.id, "color": color, "talle": talle, "marca": marca(a), "sku": a.sku,
                              "codigo_barras": a.codigo_barras, "nombre": a.nombre})
        salida.append({"articulo": m, "variantes": variantes,
                       "preseleccion": exacto.id if exacto and exacto.articulo == m else None})
    return salida


def enviar(dev: Devolucion) -> dict:
    """Manda la devolución al Apps Script (crea Crédito y Reingreso en Drive y avisa por mail)."""
    payload = {
        "responsable": dev.usuario.nombre if dev.usuario else "PARKA Depósito",
        "sesion": f"PD-{dev.id}",
        "fecha": dev.creado_at.isoformat(timespec="seconds"),
        "plataforma": dev.plataforma or "",
        # "sector" lleva la plataforma (ML / TN) para que se vea en la columna del Excel de Drive
        "items": [{"codigo": i.codigo_barras, "articulo": i.articulo, "color": i.color, "talle": i.talle,
                   "estado": i.estado, "cantidad": i.cantidad, "sector": i.plataforma or "",
                   "plataforma": i.plataforma or ""} for i in dev.items],
    }
    req = urllib.request.Request(config.DEVOL_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "text/plain;charset=utf-8"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    if isinstance(data, dict) and (data.get("ok") is False or ("error" in data and data.get("ok") is not True)):
        raise RuntimeError(str(data.get("error", "error del script")))
    return data
