"""Recolección Ecommerce: las ventas del día de ML (Flex / Colecta) y Tienda Nube.

1. El admin (o quien tenga Supervisión) crea la búsqueda con los archivos de la carpeta del día en Drive.
2. Los operarios se suman. La app corta la ruta del depósito en tramos con una cantidad pareja de
   unidades: cada uno busca solo lo suyo (si alguien se suma o se va, se reparte de nuevo lo que falta).
3. Al buscar, cada prenda se escanea: la app la compara con lo que le toca (pantalla verde / roja).
4. En la mesa de control se escanea la etiqueta del envío y las prendas (misma lógica que Control de paquetes).

Se guarda sobre las tablas de Control de paquetes (Control.modo = "ecommerce")."""
from __future__ import annotations

import re
from collections import OrderedDict

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from ..db import ahora, hoy
from ..models import (Articulo, Control, ControlBox, ControlBoxItem, ControlItem, ControlPaquete,
                      ControlParticipante, Usuario)
from . import control as csvc

MODO = "ecommerce"
NOMBRES_TIPO = {"flex": "Flex", "colecta": "Colecta", "tiendanube": "Tienda Nube", "ml": "Mercado Libre",
                "full": "Full", "cambio": "Cambio"}
# Canal de cada venta, para el ícono de la ficha: Mercado Libre, Tienda Nube o cambio
CANAL = {"flex": "ml", "colecta": "ml", "ml": "ml", "full": "ml", "tiendanube": "tn", "cambio": "cambio"}


def venta_corta(p: ControlPaquete) -> dict:
    """Número de venta para mostrar en la ficha: ML = nº de venta (o envío), TN = nº de orden, cambio = CAM-…"""
    canal = CANAL.get(p.tipo or "", "")
    numero = (p.venta_id or p.tracking) if canal == "ml" else p.tracking
    return {"canal": canal, "numero": str(numero or ""), "tracking": p.tracking, "tipo": NOMBRES_TIPO.get(p.tipo, p.tipo or "")}


# ─────────────────────────── crear ───────────────────────────

def _con_extra(paquetes: list[dict], extra: list[dict] | None) -> list[dict]:
    """Suma las órdenes que vinieron por la API de TN; si el CSV trae la misma orden, manda la API."""
    if not extra:
        return paquetes
    nums = {str(p.get("tracking")) for p in extra}
    return [p for p in paquetes if str(p.get("tracking")) not in nums] + [dict(p, items=csvc._items_de(p)) for p in extra]


def crear(db: Session, u: Usuario, archivos: list[tuple[str, bytes]], nombre: str = "",
          origen: str = "drive", extra: list[dict] | None = None) -> tuple[Control, list[str]]:
    paquetes, errores = csvc.leer_archivos(archivos, solo_pendientes=True) if archivos else ([], [])
    paquetes = _con_extra(paquetes, extra)
    if not paquetes:
        raise ValueError(" · ".join(errores) or "No hay ventas para buscar en esos archivos")
    tipos = sorted({p.get("tipo") or "" for p in paquetes} - {""})
    if not nombre.strip():
        nombre = " + ".join(NOMBRES_TIPO.get(t, t) for t in tipos) or "Ventas"
        nombre = f"{nombre} {hoy().strftime('%d/%m')}"
    c = Control(fecha=hoy(), usuario_id=u.id, origen=origen, modo=MODO, nombre=nombre.strip()[:200],
                tipo=(tipos[0] if len(tipos) == 1 else "mixto")[:20],
                archivos=", ".join([n for n, _ in archivos] + (["API"] if extra else []))[:2000])
    db.add(c)
    csvc.agregar_paquetes(db, c, paquetes)
    db.flush()
    sumar_recolectores(db, c)
    repartir(db, c)
    db.commit()
    return c, errores


def sumar_archivos(db: Session, c: Control, archivos: list[tuple[str, bytes]],
                   extra: list[dict] | None = None) -> tuple[int, int, list[str]]:
    paquetes, errores = csvc.leer_archivos(archivos, solo_pendientes=True) if archivos else ([], [])
    paquetes = _con_extra(paquetes, extra)
    nuevos, act = csvc.agregar_paquetes(db, c, paquetes)
    nombres = [n for n, _ in archivos] + (["API"] if extra else [])
    c.archivos = (((c.archivos + ", ") if c.archivos else "") + ", ".join(nombres))[:2000]
    db.flush()
    repartir(db, c)
    db.commit()
    return nuevos, act, errores


# ─────────────────────────── equipo ───────────────────────────

def recolectores(db: Session) -> list[Usuario]:
    """Usuarios activos con rol "Buscador / Recolector" que pueden usar Recolección Ecommerce."""
    from ..auth import puede
    return [x for x in db.scalars(select(Usuario).where(Usuario.rol == "recolector", Usuario.activo.is_(True))
                                  .order_by(Usuario.nombre)).all() if puede(db, x, "ecommerce")]


def sumar_recolectores(db: Session, c: Control) -> list[str]:
    """Búsqueda nueva → entran solos los Buscadores / Recolectores (sin tocar "Me sumo"). No reparte ni hace commit."""
    nuevos = [x.nombre for x in recolectores(db) if _activar(c, x.id)]
    if nuevos:
        db.flush()
    return nuevos


def activos(c: Control) -> list[ControlParticipante]:
    return [p for p in c.participantes if p.activo]


def participante(c: Control, u: Usuario) -> ControlParticipante | None:
    return next((p for p in c.participantes if p.usuario_id == u.id), None)


def _activar(c: Control, usuario_id: int) -> bool:
    """Deja a la persona activa en la búsqueda. Devuelve True si cambió algo."""
    p = next((x for x in c.participantes if x.usuario_id == usuario_id), None)
    if p and p.activo:
        return False
    if p:
        p.activo, p.unido_at, p.salio_at = True, ahora(), None
    else:
        c.participantes.append(ControlParticipante(usuario_id=usuario_id))
    return True


def sumarse(db: Session, c: Control, u: Usuario) -> None:
    if not _activar(c, u.id):
        return
    db.flush()
    repartir(db, c)
    db.commit()


def sumar_personas(db: Session, c: Control, usuario_ids: list[int]) -> list[str]:
    """Quien organiza suma directamente a varias personas (sin que toquen "Me sumo"). Se reparte una sola vez.
    Devuelve los nombres de los que se sumaron recién."""
    nuevos = []
    for uid in dict.fromkeys(usuario_ids):
        u = db.get(Usuario, uid)
        if u and u.activo and _activar(c, uid):
            nuevos.append(u.nombre)
    if nuevos:
        db.flush()
        repartir(db, c)
        db.commit()
    return nuevos


def salir(db: Session, c: Control, usuario_id: int) -> None:
    p = next((x for x in c.participantes if x.usuario_id == usuario_id), None)
    if not p or not p.activo:
        return
    p.activo, p.salio_at = False, ahora()
    db.flush()
    repartir(db, c)
    db.commit()


# ─────────────────────────── reparto ───────────────────────────

def _items(c: Control) -> list[ControlItem]:
    """Las prendas a buscar (las de ventas canceladas ya no se buscan)."""
    return [i for p in c.paquetes if not p.cancelado for i in p.items]


def pendiente_busqueda(i: ControlItem) -> int:
    """Unidades que todavía hay que traer de esa línea."""
    if i.faltante:
        return 0
    return max(0, i.cantidad - i.buscado)


def clave_grupo(i: ControlItem) -> str:
    """La misma prenda (mismo artículo o, sin vincular, mismo SKU) en varios paquetes se busca junta."""
    if i.articulo_id:
        return f"a{i.articulo_id}"
    return "s" + re.sub(r"[^A-Z0-9]", "_", (i.sku or i.producto or str(i.id)).upper())[:80]


def _orden(i: ControlItem) -> tuple:
    return (i.orden_ruta, (i.ubicacion or "").split(",")[0], i.articulo or "~", i.color, i.talle, i.sku, i.id)


def es_multi(p: ControlPaquete) -> bool:
    """Venta (o carrito de ML: mismo envío) con 2 o más prendas: va en una sola ficha de búsqueda, la busca
    una sola persona y termina en un mismo BOX."""
    return sum(i.cantidad for i in p.items) >= 2


def _orden_ficha(i: ControlItem) -> tuple:
    """Orden de la guía: las prendas de una venta de varias quedan juntas, donde está la primera de la ruta."""
    if es_multi(i.paquete):
        return (min(_orden(x) for x in i.paquete.items), i.paquete_id, _orden(i))
    return (_orden(i), 0, ())


def clave_ficha(i: ControlItem) -> str:
    """Unidad del reparto: la misma prenda (ventas de 1) o la venta entera (ventas de 2 o más)."""
    return f"v{i.paquete_id}" if es_multi(i.paquete) else clave_grupo(i)


def repartir(db: Session, c: Control) -> None:
    """Corta la ruta en tramos seguidos con unidades parejas, uno por operario activo (en el orden en que se
    sumaron). Lo que alguien ya empezó a traer sigue siendo suyo mientras esté activo. Sin operarios, nada
    queda asignado."""
    gente = [p.usuario_id for p in activos(c)]
    items = [i for i in _items(c) if pendiente_busqueda(i) > 0]
    if not gente:
        for i in items:
            i.asignado_id = None
        return
    fijos, libres = [], []
    for i in items:
        (fijos if i.buscado > 0 and i.asignado_id in gente else libres).append(i)
    # una venta de varias prendas que alguien ya empezó sigue entera con esa persona
    dueno = {i.paquete_id: i.asignado_id for i in _items(c)
             if i.buscado > 0 and i.asignado_id in gente and es_multi(i.paquete)}
    for i in list(libres):
        if i.paquete_id in dueno:
            i.asignado_id = dueno[i.paquete_id]
            libres.remove(i)
            fijos.append(i)
    # fichas (misma prenda, o la venta entera si lleva 2 o más) en orden de ruta: no se parten entre dos personas
    grupos: "OrderedDict[str, list[ControlItem]]" = OrderedDict()
    for i in sorted(libres, key=_orden):
        grupos.setdefault(clave_ficha(i), []).append(i)
    carga = {g: 0 for g in gente}
    for i in fijos:
        carga[i.asignado_id] += pendiente_busqueda(i)
    total = sum(carga.values()) + sum(pendiente_busqueda(i) for i in libres)
    objetivo = total / len(gente)
    k = 0
    # recorrido: se llena a cada uno hasta su parte justa y se pasa al siguiente
    for lista in grupos.values():
        unidades = sum(pendiente_busqueda(i) for i in lista)
        while k < len(gente) - 1 and carga[gente[k]] >= objetivo:
            k += 1
        # si este grupo la pasa mucho de su parte y el siguiente está más vacío, va al siguiente
        if (k < len(gente) - 1 and carga[gente[k]] > 0
                and carga[gente[k]] + unidades - objetivo > objetivo - carga[gente[k]]):
            k += 1
        for i in lista:
            i.asignado_id = gente[k]
        carga[gente[k]] += unidades


# ─────────────────────────── búsqueda (operario) ───────────────────────────

def grupos_de(c: Control, usuario_id: int | None = None, solo_pendientes: bool = True,
              excluir: int | None = None) -> list[dict]:
    """La lista de búsqueda agrupada por prenda y ordenada por la ruta. usuario_id=None: de todos.
    excluir=uid: lo que buscan los demás (agrupado por prenda y por quién la busca)."""
    out: "OrderedDict[str, dict]" = OrderedDict()
    for i in sorted(_items(c), key=_orden_ficha):
        if usuario_id is not None and i.asignado_id != usuario_id:
            continue
        if excluir is not None and i.asignado_id == excluir:
            continue
        if solo_pendientes and pendiente_busqueda(i) == 0 and not i.faltante:
            continue
        multi = es_multi(i.paquete)
        clave = clave_grupo(i) + (f"-p{i.paquete_id}" if multi else "") \
            + (f"-u{i.asignado_id or 0}" if excluir is not None else "")
        g = out.setdefault(clave, {
            "venta": venta_json(i.paquete) if multi else None,
            "clave": clave, "item_ids": [], "asignado_id": i.asignado_id, "trajo": [], "articulo_id": i.articulo_id, "articulo": i.articulo,
            "color": i.color, "talle": i.talle, "codigo_barras": i.codigo_barras, "sku": i.sku,
            "producto": i.producto, "variante": i.variante, "ubicacion": i.ubicacion, "vinculado": bool(i.articulo_id),
            "cantidad": 0, "buscado": 0, "faltan": 0, "faltante": 0, "paquetes": [], "en_box": 0, "boxes": [],
            "asignado": i.asignado.nombre if i.asignado else "", "ventas": [], "canales": [], "sku_odoo": ""})
        g["item_ids"].append(i.id)
        vc = venta_corta(i.paquete)
        if vc["numero"] and vc["numero"] not in [v["numero"] for v in g["ventas"]]:
            g["ventas"].append(vc)
        if vc["canal"] and vc["canal"] not in g["canales"]:
            g["canales"].append(vc["canal"])
        if i.buscado and i.buscado_por and i.buscado_por.nombre not in g["trajo"]:
            g["trajo"].append(i.buscado_por.nombre)
        g["cantidad"] += i.cantidad
        g["buscado"] += min(i.buscado, i.cantidad)
        g["faltan"] += pendiente_busqueda(i)
        g["en_box"] += en_box(i)
        for bi in i.box_items:
            if bi.box.numero not in g["boxes"]:
                g["boxes"].append(bi.box.numero)
        if i.faltante:
            g["faltante"] += i.cantidad - min(i.buscado, i.cantidad)
        tipo = NOMBRES_TIPO.get(i.paquete.tipo, i.paquete.tipo)
        g["paquetes"].append(f"{tipo + ' ' if tipo else ''}{i.paquete.tracking}")
    # SKU de Odoo de cada prenda vinculada (una sola consulta)
    ids = {g["articulo_id"] for g in out.values() if g["articulo_id"]}
    db = object_session(c)
    if ids and db is not None:
        skus = dict(db.execute(select(Articulo.id, Articulo.sku).where(Articulo.id.in_(ids))).all())
        for g in out.values():
            g["sku_odoo"] = skus.get(g["articulo_id"], "") if g["articulo_id"] else ""
    return list(out.values())


def venta_json(p: ControlPaquete) -> dict:
    """Cabecera de la ficha de una venta de 2 o más prendas."""
    vivos = [i for i in p.items]
    return {"clave": f"v{p.id}", "paquete_id": p.id, "tracking": p.tracking, "venta_id": p.venta_id,
            "comprador": p.comprador, "tipo": NOMBRES_TIPO.get(p.tipo, p.tipo or ""),
            "prendas": sum(i.cantidad for i in vivos), "lineas": len(vivos)}


def progreso(c: Control) -> dict:
    """Cuánto falta buscar, en total y por operario."""
    items = _items(c)
    por: "OrderedDict[int, dict]" = OrderedDict()
    for p in c.participantes:
        por[p.usuario_id] = {"usuario_id": p.usuario_id, "nombre": p.usuario.nombre if p.usuario else "?",
                             "activo": p.activo, "unidades": 0, "buscadas": 0, "faltantes": 0,
                             "unido": p.unido_at.strftime("%H:%M") if p.unido_at else ""}
    sin_asignar = 0
    for i in items:
        falta = pendiente_busqueda(i)
        if i.asignado_id in por:
            d = por[i.asignado_id]
            d["unidades"] += i.cantidad
            d["buscadas"] += min(i.buscado, i.cantidad)
            d["faltantes"] += (i.cantidad - min(i.buscado, i.cantidad)) if i.faltante else 0
        elif falta:
            sin_asignar += falta
    total = sum(i.cantidad for i in items)
    buscadas = sum(min(i.buscado, i.cantidad) for i in items)
    return {"unidades": total, "buscadas": buscadas, "faltan": sum(pendiente_busqueda(i) for i in items),
            "faltantes": sum(i.cantidad - min(i.buscado, i.cantidad) for i in items if i.faltante),
            "sin_asignar": sin_asignar, "equipo": list(por.values())}


def resumen(c: Control, u: Usuario | None = None) -> dict:
    r = csvc.resumen(c)
    pr = progreso(c)
    tipos: dict[str, int] = {}
    for p in c.paquetes:
        t = NOMBRES_TIPO.get(p.tipo, p.tipo or "Otro")
        tipos[t] = tipos.get(t, 0) + 1
    yo = participante(c, u) if u else None
    return {**r, "fecha": c.fecha.isoformat(), "busqueda": pr, "tipos": tipos, "drive_resultado": c.drive_resultado,
            "me_sume": bool(yo and yo.activo)}


def marcar_buscado(db: Session, i: ControlItem, u: Usuario, delta: int) -> None:
    """Suma / resta una unidad. Queda registrado quién la trajo; si era de otro (confirmado), pasa a ser suya."""
    i.buscado = max(0, min(i.cantidad, i.buscado + delta))
    i.buscado_at = ahora()
    if delta > 0:
        i.buscado_por_id = u.id
        if i.asignado_id != u.id:
            i.asignado_id = u.id
    elif i.buscado == 0:
        i.buscado_por_id = None
    db.commit()


def es_de_otro(i: ControlItem, u: Usuario) -> Usuario | None:
    """La prenda la tiene asignada otra persona que sigue en la búsqueda → quién."""
    if i.asignado_id in (None, u.id) or not i.asignado:
        return None
    activo = any(p.usuario_id == i.asignado_id and p.activo for p in i.paquete.control.participantes)
    return i.asignado if activo else None


def escanear_busqueda(db: Session, c: Control, u: Usuario, codigo: str) -> dict:
    """El operario escanea la prenda que encontró: ¿es una de las que le tocan?"""
    art = csvc.articulo_de_codigo(db, codigo)
    if not art:
        return {"ok": False, "motivo": "desconocido",
                "mensaje": f"El código {codigo} no está en el catálogo de artículos. Dejala donde estaba."}
    nombre = csvc.nombre_art(art)
    lineas = [i for i in sorted(_items(c), key=_orden)
              if i.articulo_id and art.id in csvc._ids(i.aceptados)]
    mias = [i for i in lineas if i.asignado_id == u.id and pendiente_busqueda(i) > 0]
    if mias:
        i = mias[0]
        marcar_buscado(db, i, u, +1)
        g = next((x for x in grupos_de(c, u.id, solo_pendientes=False) if i.id in x["item_ids"]), None)
        return {"ok": True, "escaneado": nombre, "item_id": i.id, "grupo": g,
                "mensaje": f"OK · {nombre}" + (f" · faltan {g['faltan']}" if g and g["faltan"] else "")}
    libres = [i for i in lineas if pendiente_busqueda(i) > 0]
    if libres:
        i = libres[0]
        quien = es_de_otro(i, u)
        if quien is None:          # nadie la tiene (o quien la tenía se fue): es tuya
            marcar_buscado(db, i, u, +1)
            g = next((x for x in grupos_de(c, u.id, solo_pendientes=False) if i.id in x["item_ids"]), None)
            return {"ok": True, "escaneado": nombre, "item_id": i.id, "grupo": g, "mensaje": f"OK · {nombre}"}
        ids = [x.id for x in libres if x.asignado_id == i.asignado_id]
        return {"ok": False, "motivo": "otro", "escaneado": nombre, "quien": quien.nombre, "item_ids": ids,
                "mensaje": f"{nombre} la tiene asignada {quien.nombre}. ¿La trajiste vos?"}
    if lineas:
        return {"ok": False, "motivo": "sobra", "escaneado": nombre,
                "mensaje": f"Ya se juntaron todas las unidades de {nombre}. Esta sobra: dejala donde estaba."}
    # ¿Es la misma prenda en otra marca (No Brand) o en otro talle/color?
    return {"ok": False, "motivo": "equivocado", "escaneado": nombre,
            "mensaje": f"{nombre} no está en las ventas de esta búsqueda. Revisá talle, color y marca (tiene que ser Parka)."}


def articulo_json(a: Articulo) -> dict:
    return {"id": a.id, "nombre": csvc.nombre_art(a), "codigo_barras": a.codigo_barras, "sku": a.sku}


def lista(db: Session, limite: int = 30) -> list[Control]:
    from sqlalchemy.orm import selectinload
    return list(db.scalars(select(Control).where(Control.modo == MODO)
                           .options(selectinload(Control.paquetes), selectinload(Control.participantes),
                                    selectinload(Control.boxes))
                           .order_by(Control.id.desc()).limit(limite)).all())


def paquetes_pendientes_hoy(db: Session) -> int:
    from sqlalchemy import func
    return db.scalar(select(func.count()).select_from(ControlPaquete).join(Control).where(
        Control.fecha == hoy(), Control.modo == MODO,
        ControlPaquete.estado.in_(("pendiente", "en_curso", "faltante")))) or 0


# ─────────────────────────── BOX (cajas de la mesa) ───────────────────────────
# Cada persona tiene SUS BOX: las primeras N prendas que marca como "La tengo" (5 por defecto, configurable)
# van a su BOX 1, las siguientes a su BOX 2, y así (numerados por persona: "Lucas · BOX 1", "Ana · BOX 1").
# Se asigna solo al marcar la prenda. Una venta de varias prendas no se parte: reserva su lugar entero en el
# BOX donde cae la primera. En la mesa de control se ven los BOX de cada persona con las etiquetas que
# esperan ser escaneadas; al escanear la etiqueta del envío aparece de quién y qué BOX es.

BOX_DEFECTO = 5


def box_cantidad(db: Session) -> int:
    from . import ajustes
    try:
        return max(1, min(50, int(ajustes.leer("re_box_cant", db) or BOX_DEFECTO)))
    except ValueError:
        return BOX_DEFECTO


def en_box(i: ControlItem) -> int:
    return sum(b.cantidad for b in i.box_items)


def box_activo(b: ControlBox) -> bool:
    """Sigue en la mesa mientras alguna de sus ventas no esté controlada (las canceladas no lo traban)."""
    return any(not bi.item.paquete.cancelado and bi.item.paquete.estado != "completo" for bi in b.items)


def unidades_box(b: ControlBox) -> int:
    return sum(bi.cantidad for bi in b.items)


def _ocupado(b: ControlBox) -> int:
    """Lugar que ocupa: las ventas enteras que tiene adentro (una venta de 2 reserva 2 aunque falte traer una)."""
    ps = OrderedDict((bi.item.paquete_id, bi.item.paquete) for bi in b.items)
    return sum(sum(i.cantidad for i in p.items) for p in ps.values())


def boxes_de(c: Control, usuario_id: int) -> list[ControlBox]:
    return sorted((b for b in c.boxes if b.usuario_id == usuario_id), key=lambda b: b.numero)


def _box_de_venta(p: ControlPaquete, usuario_id: int) -> ControlBox | None:
    for i in p.items:
        for bi in i.box_items:
            if bi.box.usuario_id == usuario_id:
                return bi.box
    return None


def nombre_box(b: ControlBox) -> str:
    return (f"{b.usuario.nombre} · " if b.usuario else "") + f"BOX {b.numero}"


def en_mano(c: Control, usuario_id: int) -> list[tuple[ControlItem, int]]:
    """Prendas que la persona marcó y todavía no tienen BOX (se asignan solas con `asignar_box`)."""
    out = []
    for i in sorted(_items(c), key=_orden_ficha):
        quien = i.buscado_por_id or i.asignado_id
        n = min(i.buscado, i.cantidad) - en_box(i)
        if n > 0 and quien == usuario_id:
            out.append((i, n))
    return out


def asignar_box(db: Session, c: Control, usuario_id: int) -> list[ControlBox]:
    """Pone en el BOX de la persona lo que acaba de marcar. Devuelve los BOX que se llenaron con esto."""
    mano = en_mano(c, usuario_id)
    if not mano:
        return []
    cap = box_cantidad(db)
    mios = boxes_de(c, usuario_id)
    actual = mios[-1] if mios else None
    antes: dict[int, int] = {}
    tocados: "OrderedDict[int, ControlBox]" = OrderedDict()
    for i, n in mano:
        p = i.paquete
        box = _box_de_venta(p, usuario_id)
        if box is None:
            k = sum(x.cantidad for x in p.items)
            if actual is None or _ocupado(actual) >= cap or (_ocupado(actual) and _ocupado(actual) + k > cap):
                actual = ControlBox(numero=(actual.numero if actual else 0) + 1, usuario_id=usuario_id)
                c.boxes.append(actual)
            box = actual
        antes.setdefault(id(box), unidades_box(box))
        box.items.append(ControlBoxItem(item=i, cantidad=n))
        tocados[id(box)] = box
    db.commit()
    return [b for k, b in tocados.items() if antes[k] < cap <= unidades_box(b)]


def restar(db: Session, c: Control, lineas: list[ControlItem], u: Usuario) -> None:
    """−1: me equivoqué. Si la prenda ya estaba en un BOX, sale de ese BOX (si el BOX queda vacío, se borra)."""
    todas = [i for i in lineas if i.buscado > 0]
    if not todas:
        raise ValueError("No hay nada para restar")
    i = todas[-1]
    if i.paquete.estado == "completo":
        raise ValueError("Esa venta ya se controló en la mesa: no se puede restar")
    if min(i.buscado, i.cantidad) <= en_box(i) and i.box_items:
        bi = i.box_items[-1]
        box = bi.box
        bi.cantidad -= 1
        if bi.cantidad <= 0:
            box.items.remove(bi)
            if bi in i.box_items:
                i.box_items.remove(bi)
            db.delete(bi)
        if not box.items:
            c.boxes.remove(box)
            db.delete(box)
        db.flush()
    marcar_buscado(db, i, u, -1)


def estado_box(db: Session, c: Control, usuario_id: int) -> dict:
    """Cómo viene el BOX de la persona (el último es el que está armando)."""
    cap = box_cantidad(db)
    mios = boxes_de(c, usuario_id)
    pend = sum(pendiente_busqueda(i) for i in _items(c) if i.asignado_id == usuario_id)
    act = mios[-1] if mios else None
    return {"cantidad": cap, "pendientes": pend, "en_mano": sum(n for _, n in en_mano(c, usuario_id)),
            "actual": act.numero if act else None, "en_actual": unidades_box(act) if act else 0,
            "lleno": bool(act and unidades_box(act) >= cap),
            "boxes": [{"numero": b.numero, "unidades": unidades_box(b), "activo": box_activo(b)} for b in mios]}


def box_json(b: ControlBox) -> dict:
    ventas = OrderedDict((bi.item.paquete_id, bi.item.paquete) for bi in b.items)
    vs = [_venta_box(p, b) for p in ventas.values()]
    return {"id": b.id, "numero": b.numero, "activo": box_activo(b), "nombre": nombre_box(b),
            "usuario_id": b.usuario_id, "quien": b.usuario.nombre if b.usuario else "",
            "hora": b.creado_at.strftime("%H:%M") if b.creado_at else "",
            "unidades": unidades_box(b), "ventas": vs,
            "controladas": sum(1 for v in vs if v["estado"] == "completo"),
            "esperando": sum(1 for v in vs if v["estado"] != "completo" and not v["cancelado"])}


def _venta_box(p: ControlPaquete, b: ControlBox) -> dict:
    lineas = [bi for bi in b.items if bi.item.paquete_id == p.id]
    return {"paquete_id": p.id, "tracking": p.tracking, "venta_id": p.venta_id, "comprador": p.comprador,
            "tipo": NOMBRES_TIPO.get(p.tipo, p.tipo or ""), "estado": p.estado, "cancelado": bool(p.cancelado),
            "unidades": sum(bi.cantidad for bi in lineas), "total": sum(i.cantidad for i in p.items),
            "prendas": [f"{bi.cantidad}× " + (f"{bi.item.articulo} {bi.item.color} {bi.item.talle}".strip()
                                              if bi.item.articulo_id else (bi.item.producto or bi.item.sku)[:60])
                        for bi in lineas]}


def boxes(c: Control) -> list[dict]:
    """Lista de BOX por persona (en orden de número)."""
    out = [box_json(b) for b in c.boxes]
    out.sort(key=lambda x: (x["quien"].lower(), x["numero"]))
    return out
