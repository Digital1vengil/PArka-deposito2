"""Mapeo del depósito: ubicaciones, qué hay en cada una y vínculo SKU de ML/TN ↔ artículo."""
from __future__ import annotations

import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import Articulo, SkuVinculo, Ubicacion, UbicacionArticulo
from .devoluciones import color_talle, marca

SIN_RUTA = 999999


def normalizar_codigo(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().upper())[:30]


def armar_codigo(pasillo: str, modulo: int, nivel: int, zona: str = "") -> str:
    partes = [p for p in (normalizar_codigo(zona), normalizar_codigo(pasillo)) if p]
    cod = "-".join(partes) or "U"
    if modulo:
        cod += f"-{modulo:02d}"
    if nivel:
        cod += f"-{nivel}"
    return cod


def ubicacion_json(u: Ubicacion, conteo: int | None = None) -> dict:
    return {"id": u.id, "codigo": u.codigo, "zona": u.zona, "pasillo": u.pasillo, "modulo": u.modulo,
            "nivel": u.nivel, "orden": u.orden, "descripcion": u.descripcion, "activa": u.activa,
            "articulos": len(u.asignaciones) if conteo is None else conteo}


def asignacion_json(a: UbicacionArticulo) -> dict:
    v = a.articulo_ref
    detalle = ""
    if v:
        color, talle = color_talle(v)
        detalle = f"{color} · {talle}"
    elif a.color:
        detalle = a.color
    return {"id": a.id, "ubicacion_id": a.ubicacion_id, "codigo": a.ubicacion.codigo if a.ubicacion else "",
            "modelo": a.modelo, "color": a.color, "articulo_id": a.articulo_id,
            "detalle": detalle or "Todos los colores y talles", "nota": a.nota}


def ubicaciones_de(db: Session, art: Articulo) -> list[Ubicacion]:
    """Dónde está una variante: primero lo asignado a la variante puntual, después a su color, después al modelo."""
    color, _ = color_talle(art)
    filas = db.scalars(select(UbicacionArticulo).where(or_(
        UbicacionArticulo.articulo_id == art.id,
        (UbicacionArticulo.articulo_id.is_(None)) & (func.upper(UbicacionArticulo.modelo) == (art.articulo or "").upper()),
    ))).all()
    variante = [f.ubicacion for f in filas if f.articulo_id == art.id]
    por_color = [f.ubicacion for f in filas if f.articulo_id is None and f.color and f.color.lower() == color.lower()]
    modelo = [f.ubicacion for f in filas if f.articulo_id is None and not f.color]
    elegidas = variante or por_color or modelo
    vistas, salida = set(), []
    for u in sorted(elegidas, key=lambda x: (x.orden, x.codigo)):
        if u.activa and u.id not in vistas:
            vistas.add(u.id)
            salida.append(u)
    return salida


def articulo_por_sku(db: Session, sku: str) -> Articulo | None:
    """SKU de ML/TN → artículo: vínculo guardado, SKU igual, código de barras igual, o SKU contenido."""
    s = (sku or "").strip()
    if not s:
        return None
    v = db.scalar(select(SkuVinculo).where(SkuVinculo.sku_externo == s.upper()))
    if v:
        return db.get(Articulo, v.articulo_id)
    a = db.scalar(select(Articulo).where(func.upper(Articulo.sku) == s.upper()).limit(1))
    if a:
        return a
    if s.isdigit():
        from .catalogo import por_codigo  # local y, si no está, consulta a Odoo
        a = por_codigo(db, s)
        if a:
            return a
    cands = candidatos_por_sku_ml(db, s)
    return cands[0] if cands else None


# ── SKU de Mercado Libre (M-114-BLACK-XL) ↔ SKU de Odoo (1-CAMP-M-00114-BLCK-XL-PARK) ──
_ALIAS_COLOR = {
    "BLCK": ("BLACK", "BLK", "NEGRO", "NEG"), "WHIT": ("WHITE", "WHT", "BLANCO", "BCO"),
    "GREY": ("GRAY", "GRIS", "GRY"), "GREN": ("GREEN", "GRN", "VERDE"), "BRWN": ("BROWN", "BRN", "MARRON", "MARRÓN"),
    "BEIG": ("BEIGE", "BGE"), "NAVY": ("NVY", "MARINO"), "OLIV": ("OLIVE", "OLIVA"), "REDD": ("RED", "ROJO"),
    "PINK": ("ROSA",), "BLUE": ("AZUL", "BLU"), "KHAK": ("KHAKI", "CAQUI"), "CHOC": ("CHOCOLATE",),
    "UNIC": ("UNICO", "ÚNICO", "UNICA"),
}
_CANON = {a: k for k, alias in _ALIAS_COLOR.items() for a in alias + (k,)}
_TALLE_EQ = {"XXXL": "3XL", "XXL": "XXL", "2XL": "XXL", "XXXXL": "4XL"}
_SKU_ML = re.compile(r"^([MWUB])-0*(\d{1,5})-([A-Z]{2,12})(?:-([A-Z0-9]{1,5}))?$")


def _talle_norm(t: str) -> str:
    t = (t or "").strip().upper()
    return _TALLE_EQ.get(t, t)


def _subsecuencia(corta: str, larga: str) -> bool:
    it = iter(larga)
    return all(ch in it for ch in corta)


def colores_equivalentes(a: str, b: str) -> bool:
    """BLK ~ BLCK ~ BLACK, GRAY ~ GREY, BEIGE ~ BEIG (códigos de color de ML y de Odoo)."""
    a, b = (a or "").strip().upper(), (b or "").strip().upper()
    if not a or not b:
        return False
    if a == b or _CANON.get(a, a) == _CANON.get(b, b):
        return True
    corta, larga = sorted((a, b), key=len)
    return len(corta) >= 3 and corta[0] == larga[0] and _subsecuencia(corta, larga)


def _orden_marca(a: Articulo) -> int:
    return {"Parka": 0, "Puffers": 1}.get(marca(a), 2) + (0 if a.codigo_barras else 5)


def candidatos_por_sku_ml(db: Session, sku: str) -> list[Articulo]:
    """Variantes del catálogo (todas las marcas) que corresponden a un SKU con formato de ML:
    género-número-color-talle. Primero Parka, después Puffers y No Brand."""
    m = _SKU_ML.match((sku or "").strip().upper())
    if not m:
        return []
    genero, num, color = m.group(1), m.group(2).zfill(5), m.group(3)
    if genero == "B":  # mochilas / bolsos: en Odoo son unisex y sin talle (4-MOCH-U-03014-BLCK-PARK)
        genero = "U"
    talle = _talle_norm(m.group(4) or "")
    filas = db.scalars(select(Articulo).where(Articulo.sku.like(f"%-{genero}-{num}-%"))).all()
    out = []
    for a in filas:
        partes = (a.sku or "").upper().split("-")
        if len(partes) < 6 or partes[2] != genero or partes[3] != num:
            continue
        if not talle:  # sin talle: SKU de 6 partes (…-COLOR-MARCA)
            if len(partes) == 6 and colores_equivalentes(partes[4], color):
                out.append(a)
            continue
        if len(partes) >= 7 and _talle_norm(partes[5]) == talle and colores_equivalentes(partes[4], color):
            out.append(a)
    return sorted(out, key=lambda a: (_orden_marca(a), a.id))


def equivalentes(db: Session, art: Articulo) -> list[Articulo]:
    """La misma prenda en otras marcas (Parka / No Brand): mismo modelo, color y talle."""
    color, talle = color_talle(art)
    filas = db.scalars(select(Articulo).where(func.upper(Articulo.articulo) == (art.articulo or "").upper())).all()
    out = [a for a in filas if a.id != art.id and color_talle(a)[0].lower() == color.lower()
           and _talle_norm(color_talle(a)[1]) == _talle_norm(talle)]
    return [art] + sorted(out, key=lambda a: (_orden_marca(a), a.id))


def vincular_sku(db: Session, sku: str, articulo_id: int, usuario_id: int | None) -> SkuVinculo:
    clave = sku.strip().upper()[:200]
    v = db.scalar(select(SkuVinculo).where(SkuVinculo.sku_externo == clave))
    if v:
        v.articulo_id = articulo_id
    else:
        v = SkuVinculo(sku_externo=clave, articulo_id=articulo_id, creado_por_id=usuario_id)
        db.add(v)
    return v


def datos_variante(a: Articulo) -> dict:
    color, talle = color_talle(a)
    return {"articulo_id": a.id, "articulo": a.articulo, "color": color, "talle": talle,
            "codigo_barras": a.codigo_barras, "marca": marca(a), "nombre": a.nombre, "sku_catalogo": a.sku}
