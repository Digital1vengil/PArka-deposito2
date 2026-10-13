"""Control de paquetes: al armar cada paquete se escanea la etiqueta de envío y después cada prenda.
La app compara contra la venta (etiquetas ZPL de ML, planilla de ventas de ML o Tienda Nube) y no deja
cerrar el paquete si algo no coincide."""
from __future__ import annotations

import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import ahora, hoy
from ..models import (Articulo, Control, ControlEvento, ControlItem, ControlPaquete, Tarea, Usuario)
from ..parsers import (autodetectar, codigo_comparable, extraer_id, leer_archivo, leer_tabla, normalizar_codigo,
                       tn_pendiente, ventas_ml)
from . import mapeo
from .devoluciones import marca

SIN_RUTA = mapeo.SIN_RUTA
_NO_TRACKING = ("costo", "cargo", "ingreso", "precio", "forma", "fecha", "estado", "tipo", "demora", "total")


# ─────────────────────────── archivos → paquetes ───────────────────────────

def _items_de(p: dict) -> list[dict]:
    """Los artículos de un paquete del parser. Si no trae 'items' (versión vieja) se arma con el SKU."""
    if p.get("items"):
        return [dict(i) for i in p["items"]]
    skus = [s.strip() for s in re.split(r"[,;]", str(p.get("sku") or "")) if s.strip()]
    cant = max(1, int(p.get("cantidad") or 1))
    prods = [x.strip() for x in str(p.get("producto") or "").split(" + ")]
    if not skus:
        return [dict(sku="", producto=str(p.get("producto") or ""), variante="", cantidad=cant)]
    return [dict(sku=s, producto=prods[i] if i < len(prods) and len(skus) > 1 else str(p.get("producto") or ""),
                 variante="", cantidad=cant if len(skus) == 1 else 1) for i, s in enumerate(skus)]


def _header_planilla(nombre: str, data: bytes) -> tuple[list[str], list[list[str]]]:
    """Como leer_tabla, pero si arriba hay una fila de grupos ("Ventas | Facturación | Envíos…")
    busca la fila que de verdad tiene los encabezados (la que dice SKU o venta)."""
    headers, filas = leer_tabla(nombre, data)
    def es_header(f): return any("sku" in c.lower() or "venta" in c.lower() or "orden" in c.lower() for c in f)
    if not es_header(headers):
        for i, f in enumerate(filas[:8]):
            if es_header(f):
                return [h.strip() for h in f], filas[i + 1:]
    return headers, filas


def _col_tracking(headers: list[str]) -> str:
    claves = ("seguimiento", "tracking", "n° envío", "nº envío", "número de envío", "numero de envio",
              "id de envío", "id envío", "shipment", "código de envío", "codigo de envio")
    for h in headers:
        lc = h.lower()
        if any(k in lc for k in claves) and not any(n in lc for n in _NO_TRACKING):
            return h
    return ""


def paquetes_de_planilla(nombre: str, data: bytes) -> tuple[list[dict], bool]:
    """Planilla de ventas de ML (una fila por venta / artículo). Devuelve (paquetes, tiene_tracking).
    Si no trae número de envío, los paquetes quedan por nº de venta para cruzarlos con las etiquetas."""
    headers, filas = _header_planilla(nombre, data)
    m = autodetectar(headers)
    m["tracking"] = _col_tracking(headers)
    idx = {k: (headers.index(v) if v in headers else -1) for k, v in m.items()}

    def val(f, k):
        i = idx.get(k, -1)
        return f[i].strip() if 0 <= i < len(f) and f[i] else ""

    por: dict[str, dict] = {}
    for f in filas:
        trk, venta = normalizar_codigo(val(f, "tracking")), normalizar_codigo(val(f, "venta_id"))
        clave = trk or venta
        if not clave:
            continue
        try:
            cant = max(1, int(float(val(f, "cantidad").replace(",", ".") or 1)))
        except ValueError:
            cant = 1
        p = por.setdefault(clave, dict(tracking=trk, venta_id=venta, comprador=val(f, "comprador"), items=[]))
        if val(f, "sku") or val(f, "producto"):
            p["items"].append(dict(sku=val(f, "sku"), producto=val(f, "producto"), variante=val(f, "variante"),
                                   cantidad=cant))
    return list(por.values()), bool(m["tracking"])


def leer_archivos(archivos: list[tuple[str, bytes]], tipo: str = "",
                  base: list[dict] | None = None, solo_pendientes: bool = False) -> tuple[list[dict], list[str]]:
    """Etiquetas (TXT/ZPL) y planillas (Excel/CSV de ML o TN) juntas → paquetes combinados por nº de envío.
    Una planilla sin nº de envío se cruza con las etiquetas por nº de venta. `base`: paquetes ya cargados
    (para sumar la planilla después del TXT). `solo_pendientes`: de Tienda Nube solo las órdenes sin enviar."""
    paquetes: dict[str, dict] = {p["tracking"]: dict(p) for p in (base or []) if p.get("tracking")}
    sin_envio: list[dict] = []
    errores: list[str] = []
    for nombre, data in archivos:
        es_planilla = nombre.lower().endswith((".xlsx", ".xlsm", ".csv"))
        try:
            ml = ventas_ml(nombre, data) if es_planilla else None
            if ml is not None:
                lista, con_envio = ml, True
            elif es_planilla:
                r = leer_archivo(nombre, data, tipo)
                if r["formato"] == "tiendanube":
                    lista, con_envio = r["paquetes"], True
                    if solo_pendientes:
                        antes = len(lista)
                        lista = [p for p in lista if tn_pendiente(p)]
                        if antes != len(lista):
                            errores.append(f"{nombre}: se omitieron {antes - len(lista)} órdenes ya enviadas o canceladas")
                else:
                    lista, con_envio = paquetes_de_planilla(nombre, data)
            else:
                r = leer_archivo(nombre, data, tipo)
                lista, con_envio = r["paquetes"], True
        except ValueError as e:
            errores.append(f"{nombre}: {e}")
            continue
        if not lista:
            errores.append(f"{nombre}: no se encontraron paquetes")
            continue
        for p in lista:
            p = dict(p, items=_items_de(p))
            if con_envio and p.get("tracking"):
                _combinar(paquetes, p["tracking"], p)
            else:
                sin_envio.append(p)
    # planilla sin nº de envío: se cruza por venta
    por_venta = {codigo_comparable(p.get("venta_id")): k for k, p in paquetes.items() if p.get("venta_id")}
    sueltas = 0
    for p in sin_envio:
        k = por_venta.get(codigo_comparable(p.get("venta_id")))
        if k:
            _combinar(paquetes, k, p)
        else:
            sueltas += 1
    if sueltas:
        errores.append(f"{sueltas} ventas de la planilla no tienen etiqueta cargada (no se pueden controlar "
                       "sin el nº de envío): cargá también el TXT de etiquetas")
    return list(paquetes.values()), errores


def _combinar(paquetes: dict[str, dict], clave: str, nuevo: dict) -> None:
    """Junta lo que dice la etiqueta con lo que dice la planilla del mismo paquete."""
    actual = paquetes.get(clave)
    if not actual:
        paquetes[clave] = dict(nuevo, tracking=nuevo.get("tracking") or clave)
        return
    for campo in ("venta_id", "comprador", "tracking_alt", "tipo"):
        if not actual.get(campo) and nuevo.get(campo):
            actual[campo] = nuevo[campo]
    viejos, nuevos = actual.get("items") or [], nuevo.get("items") or []
    if not nuevos:
        return
    if not viejos:
        actual["items"] = nuevos
        return
    # completar SKU / variante / cantidad que falten, ítem por ítem
    for it in viejos:
        par = next((n for n in nuevos if n.get("sku") and codigo_comparable(n["sku"]) == codigo_comparable(it.get("sku"))), None)
        if par is None and len(viejos) == 1 and len(nuevos) == 1:
            par = nuevos[0]
        if par:
            for campo in ("sku", "variante", "producto"):
                if not it.get(campo) and par.get(campo):
                    it[campo] = par[campo]
            if len(viejos) > 1:  # la etiqueta no dice cuántas de cada una: manda la planilla
                it["cantidad"] = max(1, int(par.get("cantidad") or 1))
    conocidos = {codigo_comparable(i.get("sku")) for i in viejos if i.get("sku")}
    for n in nuevos:
        if n.get("sku") and codigo_comparable(n["sku"]) not in conocidos and len(viejos) > 1:
            viejos.append(n)


# ─────────────────────────── artículo de cada línea ───────────────────────────

def _ids(txt: str) -> list[int]:
    return [int(x) for x in (txt or "").split(",") if x.strip().isdigit()]


def aplicar_articulo(db: Session, it: ControlItem, art: Articulo) -> None:
    d = mapeo.datos_variante(art)
    eqs = mapeo.equivalentes(db, art)
    pq = it.paquete
    if pq is not None and pq.control is not None and pq.control.modo == "ecommerce":
        # Ecommerce es todo Parka (Parka / Puffers): la misma prenda No Brand no se acepta
        propias = [a for a in eqs if marca(a) in ("Parka", "Puffers")]
        if propias:
            eqs = propias
    it.articulo_id, it.articulo, it.color, it.talle, it.codigo_barras = (
        art.id, d["articulo"], d["color"], d["talle"], d["codigo_barras"])
    it.aceptados = ",".join(str(a.id) for a in eqs)
    ubs, vistas = [], set()
    for a in eqs:
        for u in mapeo.ubicaciones_de(db, a):
            if u.id not in vistas:
                vistas.add(u.id)
                ubs.append(u)
    ubs.sort(key=lambda u: (u.orden, u.codigo))
    it.ubicacion = ", ".join(u.codigo for u in ubs)[:200]
    it.orden_ruta = ubs[0].orden if ubs else SIN_RUTA


def resolver_item(db: Session, it: ControlItem) -> None:
    art = mapeo.articulo_por_sku(db, it.sku) if it.sku else None
    if art:
        aplicar_articulo(db, it, art)


def crear(db: Session, u: Usuario, paquetes: list[dict], origen: str, tipo: str, nombre: str,
          archivos: str = "") -> Control:
    c = Control(fecha=hoy(), usuario_id=u.id, origen=origen, tipo=tipo, nombre=nombre[:200], archivos=archivos[:2000])
    db.add(c)
    agregar_paquetes(db, c, paquetes)
    db.commit()
    return c


def agregar_paquetes(db: Session, c: Control, paquetes: list[dict]) -> tuple[int, int]:
    """Suma paquetes nuevos y completa los que ya estaban (si todavía no se empezaron a controlar)."""
    existentes = {p.tracking: p for p in c.paquetes}
    nuevos = actualizados = 0
    for p in paquetes:
        trk = str(p.get("tracking") or "").strip()
        if not trk:
            continue
        items = _items_de(p)
        cp = existentes.get(trk)
        if cp and p.get("etiqueta"):
            cp.etiqueta = str(p["etiqueta"])[:20]   # la etiqueta se pudo haber impreso después
        if cp:
            if cp.estado != "pendiente" or any(i.escaneado or i.faltante for i in cp.items):
                continue
            cp.venta_id = cp.venta_id or str(p.get("venta_id") or "")[:60]
            cp.comprador = cp.comprador or str(p.get("comprador") or "")[:120]
            cp.tracking_alt = cp.tracking_alt or str(p.get("tracking_alt") or "")[:80]
            antes = [(i.sku, i.variante, i.cantidad) for i in cp.items]
            if items and antes != [(str(i.get("sku") or "")[:200], str(i.get("variante") or "")[:200],
                                    max(1, int(i.get("cantidad") or 1))) for i in items]:
                cp.items.clear()
                db.flush()
                _cargar_items(db, cp, items)
                actualizados += 1
            continue
        cp = ControlPaquete(tracking=trk[:60], tracking_alt=str(p.get("tracking_alt") or "")[:80],
                            venta_id=str(p.get("venta_id") or "")[:60], comprador=str(p.get("comprador") or "")[:120],
                            tipo=str(p.get("tipo") or "")[:20], etiqueta=str(p.get("etiqueta") or "")[:20],
                            unidades_etiqueta=int(p.get("unidades") or 0))
        c.paquetes.append(cp)
        _cargar_items(db, cp, items)
        existentes[trk] = cp
        nuevos += 1
        if cp.unidades_etiqueta and cp.unidades_etiqueta != sum(i.cantidad for i in cp.items):
            cp.nota = (f"La etiqueta dice {cp.unidades_etiqueta} unidades y la lista suma "
                       f"{sum(i.cantidad for i in cp.items)}: revisá la venta")
    return nuevos, actualizados


def _cargar_items(db: Session, cp: ControlPaquete, items: list[dict]) -> None:
    for i in items:
        it = ControlItem(sku=str(i.get("sku") or "")[:200], producto=str(i.get("producto") or ""),
                         variante=str(i.get("variante") or "")[:200], cantidad=max(1, int(i.get("cantidad") or 1)))
        cp.items.append(it)
        resolver_item(db, it)


# ─────────────────────────── estado ───────────────────────────

def item_json(i: ControlItem) -> dict:
    return {"id": i.id, "sku": i.sku, "producto": i.producto, "variante": i.variante, "articulo_id": i.articulo_id,
            "articulo": i.articulo, "color": i.color, "talle": i.talle, "codigo_barras": i.codigo_barras,
            "ubicacion": i.ubicacion, "cantidad": i.cantidad, "escaneado": i.escaneado, "faltante": i.faltante,
            "completo": i.escaneado >= i.cantidad, "vinculado": bool(i.articulo_id),
            "buscado": i.buscado, "asignado": i.asignado.nombre if i.asignado else "",
            "trajo": i.buscado_por.nombre if i.buscado and i.buscado_por else ""}


def unidades(p: ControlPaquete) -> tuple[int, int]:
    total = sum(i.cantidad for i in p.items)
    return sum(min(i.escaneado, i.cantidad) for i in p.items), total


def paquete_json(p: ControlPaquete, con_items: bool = True) -> dict:
    hecho, total = unidades(p)
    d = {"id": p.id, "control_id": p.control_id, "tracking": p.tracking, "tracking_alt": p.tracking_alt,
         "venta_id": p.venta_id, "comprador": p.comprador, "tipo": p.tipo, "estado": p.estado, "errores": p.errores, "nota": p.nota,
         "etiqueta": p.etiqueta or "", "cancelado": bool(p.cancelado),
         "escaneadas": hecho, "unidades": total, "sin_vincular": sum(1 for i in p.items if not i.articulo_id),
         "controlado_por": p.controlado_por.nombre if p.controlado_por else "",
         "controlado_at": p.controlado_at.strftime("%H:%M") if p.controlado_at else "",
         "resumen": " + ".join(f"{i.cantidad}× {i.articulo or i.producto[:40]}"
                               + (f" {i.color} {i.talle}" if i.articulo_id else "") for i in p.items)}
    # Recolección Ecommerce: en qué BOX de la mesa quedaron las prendas de esta venta
    nums = []
    for i in p.items:
        for bi in i.box_items:
            if bi.box.numero not in nums:
                nums.append(bi.box.numero)
    d["boxes"] = sorted(nums)
    d["sin_box"] = total - sum(bi.cantidad for i in p.items for bi in i.box_items) if nums else total
    if con_items:
        d["items"] = [item_json(i) for i in p.items]
    return d


def resumen(c: Control) -> dict:
    est = {k: 0 for k in ("pendiente", "en_curso", "completo", "faltante")}
    for p in c.paquetes:
        est[p.estado] = est.get(p.estado, 0) + 1
    return {"id": c.id, "nombre": c.nombre, "tipo": c.tipo, "origen": c.origen, "estado": c.estado,
            "hora": c.creado_at.strftime("%d/%m %H:%M"), "usuario": c.usuario.nombre if c.usuario else "",
            "paquetes": len(c.paquetes), **est, "errores": sum(p.errores for p in c.paquetes),
            "sin_vincular": sum(1 for p in c.paquetes for i in p.items if not i.articulo_id),
            "archivos": c.archivos}


def detalle(c: Control) -> dict:
    orden = {"en_curso": 0, "pendiente": 1, "faltante": 2, "completo": 3}
    ps = sorted(c.paquetes, key=lambda p: (orden.get(p.estado, 9), p.id))
    return {**resumen(c), "lista": [paquete_json(p, con_items=False) for p in ps]}


def _actualizar_estado(p: ControlPaquete, u: Usuario) -> None:
    hecho, total = unidades(p)
    if any(i.faltante for i in p.items):
        p.estado = "faltante"
    elif total and hecho >= total:
        p.estado = "completo"
        p.controlado_at, p.controlado_por_id = ahora(), u.id
    elif hecho or p.iniciado_at:
        p.estado = "en_curso"
    else:
        p.estado = "pendiente"
    if p.estado != "completo":
        p.controlado_at, p.controlado_por_id = None, None


def evento(db: Session, p: ControlPaquete, u: Usuario, tipo: str, codigo: str = "", detalle_: str = "") -> None:
    db.add(ControlEvento(control_id=p.control_id, paquete_id=p.id, usuario_id=u.id, tipo=tipo,
                         codigo=codigo[:80], detalle=detalle_))


# ─────────────────────────── escaneo ───────────────────────────

def buscar_paquete(c: Control, codigo: str) -> ControlPaquete | None:
    cid = codigo_comparable(extraer_id(codigo))
    if not cid:
        return None
    for p in c.paquetes:
        if cid in (codigo_comparable(p.tracking), codigo_comparable(p.tracking_alt)) and cid:
            return p
    for p in c.paquetes:  # Tienda Nube: nº de orden; o el nº de venta
        if p.venta_id and codigo_comparable(p.venta_id) == cid:
            return p
    return None


def articulo_de_codigo(db: Session, codigo: str) -> Articulo | None:
    cod = normalizar_codigo(codigo)
    if not cod:
        return None
    from .catalogo import por_codigo  # local y, si no está, consulta a Odoo
    a = por_codigo(db, cod, consultar_odoo=cod.isdigit())
    return a or mapeo.articulo_por_sku(db, cod)


def nombre_art(a: Articulo) -> str:
    d = mapeo.datos_variante(a)
    m = marca(a)
    return f"{d['articulo']} · {d['color']} · {d['talle']}" + (f" ({m})" if m else "")


def escanear_prenda(db: Session, p: ControlPaquete, u: Usuario, codigo: str) -> dict:
    """Compara la prenda escaneada con lo que lleva el paquete. Devuelve {"ok": bool, ...}.
    Si no coincide se registra el error y el paquete no avanza."""
    art = articulo_de_codigo(db, codigo)
    if not p.iniciado_at:
        p.iniciado_at = ahora()
    if not art:
        p.errores += 1
        evento(db, p, u, "desconocido", codigo, "Código que no está en el catálogo")
        _actualizar_estado(p, u)
        db.commit()
        return {"ok": False, "motivo": "desconocido",
                "mensaje": f"El código {codigo} no está en el catálogo de artículos. No la pongas en el paquete."}
    cands = [i for i in p.items if i.articulo_id and art.id in _ids(i.aceptados)]
    pendiente = next((i for i in cands if i.escaneado < i.cantidad and not i.faltante), None)
    if pendiente:
        pendiente.escaneado += 1
        _actualizar_estado(p, u)
        db.commit()
        return {"ok": True, "item": item_json(pendiente), "escaneado": nombre_art(art), "completo": p.estado == "completo"}
    if cands:
        p.errores += 1
        evento(db, p, u, "sobra", codigo, f"{nombre_art(art)}: ya estaban todas las unidades")
        db.commit()
        return {"ok": False, "motivo": "sobra", "escaneado": nombre_art(art),
                "mensaje": f"Ya escaneaste las {cands[0].cantidad} unidad(es) de {nombre_art(art)}. Esta sobra: sacala."}
    p.errores += 1
    esperado = [f"{i.cantidad}× {i.articulo} · {i.color} · {i.talle}" if i.articulo_id else f"{i.cantidad}× {i.producto}"
                for i in p.items if i.escaneado < i.cantidad]
    sin_vinc = [i for i in p.items if not i.articulo_id and i.escaneado < i.cantidad]
    evento(db, p, u, "equivocado", codigo, f"Escaneó {nombre_art(art)} · esperaba {' / '.join(esperado)}")
    _actualizar_estado(p, u)
    db.commit()
    return {"ok": False, "motivo": "equivocado", "escaneado": nombre_art(art), "esperado": esperado,
            "articulo_id": art.id, "puede_vincular": [i.id for i in sin_vinc],
            "mensaje": "NO COINCIDE: esta prenda no va en este paquete."}


def paquetes_que_llevan(c: Control, art: Articulo) -> list[ControlPaquete]:
    return [p for p in c.paquetes if p.estado in ("pendiente", "en_curso")
            and any(i.articulo_id and art.id in _ids(i.aceptados) and i.escaneado < i.cantidad for i in p.items)]


def marcar_faltante(db: Session, it: ControlItem, u: Usuario, nota: str = "") -> None:
    p = it.paquete
    it.faltante = True
    if nota:
        p.nota = nota[:300]
    _actualizar_estado(p, u)
    nombre = f"{it.articulo} {it.color} {it.talle}".strip() if it.articulo_id else it.producto[:60]
    evento(db, p, u, "faltante", "", f"{nombre} · falta {it.cantidad - it.escaneado}" + (f" · {nota}" if nota else ""))
    ref = f"control-item:{it.id}"
    if not db.scalar(select(Tarea).where(Tarea.ref == ref)):
        db.add(Tarea(seccion_slug="ecommerce", titulo=f"Faltante para envío {p.tracking}: {nombre}"[:200],
                     detalle=(f"Comprador: {p.comprador} · venta {p.venta_id}\n"
                              f"Ubicación: {it.ubicacion or 'sin ubicación'}" + (f"\nNota: {nota}" if nota else "")),
                     origen="auto", ref=ref, prioridad="alta", vence=hoy(), creado_por_id=u.id))
    db.commit()


def quitar_faltante(db: Session, it: ControlItem, u: Usuario) -> None:
    it.faltante = False
    _actualizar_estado(it.paquete, u)
    db.commit()


def reiniciar(db: Session, p: ControlPaquete, u: Usuario) -> None:
    for i in p.items:
        i.escaneado, i.faltante = 0, False
    p.iniciado_at = None
    _actualizar_estado(p, u)
    evento(db, p, u, "reinicio", "", "Se vació el paquete para controlarlo de nuevo")
    db.commit()


def vincular(db: Session, it: ControlItem, art: Articulo, u: Usuario) -> int:
    """Vincula el SKU de la venta al artículo del catálogo (queda recordado) y lo aplica a las líneas
    iguales de los paquetes abiertos. Devuelve cuántas líneas se actualizaron."""
    if it.sku:
        mapeo.vincular_sku(db, it.sku, art.id, u.id)
    n = 0
    abiertos = db.scalars(select(ControlItem).join(ControlPaquete).join(Control).where(
        Control.estado == "abierto", or_(ControlItem.id == it.id,
                                         (ControlItem.sku != "") & (ControlItem.sku == it.sku)))).all()
    for x in abiertos:
        if x.escaneado == 0 or x.id == it.id:
            aplicar_articulo(db, x, art)
            n += 1
    db.commit()
    return n


def donde_hay(db: Session, it: ControlItem) -> list[dict]:
    """Guía de búsqueda: todas las ubicaciones donde puede estar la prenda (todas las marcas aceptadas)."""
    out = []
    for aid in _ids(it.aceptados):
        a = db.get(Articulo, aid)
        if not a:
            continue
        ubs = mapeo.ubicaciones_de(db, a)
        out.append({"articulo_id": a.id, "nombre": nombre_art(a), "codigo_barras": a.codigo_barras, "sku": a.sku,
                    "ubicaciones": [{"codigo": u.codigo, "descripcion": u.descripcion, "orden": u.orden} for u in ubs]})
    return out


def buscar(db: Session, q: str, solo_hoy: bool = True, limite: int = 30) -> list[ControlPaquete]:
    """Buscador manual: nº de envío, nº de venta, comprador, SKU o título."""
    q = q.strip()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    base = select(ControlPaquete).join(Control).outerjoin(ControlItem).where(or_(
        ControlPaquete.tracking.like(like), ControlPaquete.tracking_alt.like(like),
        ControlPaquete.venta_id.like(like), ControlPaquete.comprador.ilike(like),
        ControlItem.sku.ilike(like), ControlItem.producto.ilike(like), ControlItem.articulo.ilike(like)))
    if solo_hoy:
        base = base.where(Control.fecha == hoy())
    return list(db.scalars(base.distinct().order_by(ControlPaquete.id.desc()).limit(limite)).all())
