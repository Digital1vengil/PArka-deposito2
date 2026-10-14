"""Búsqueda automática de Recolección Ecommerce + avisos (novedades).

Cada N minutos (Conexiones/ajuste re_auto_min, por defecto 3) revisa Mercado Libre y Tienda Nube:
  • Una búsqueda POR DÍA, sin arrastre: lo que quedó pendiente de días anteriores no pasa a la de hoy (la de
    ayer se cierra como está) y las ventas que ya estuvieron en la búsqueda de otro día no se vuelven a sumar.
    Una venta de ML hecha ayer después del corte que ML pide despachar hoy va a la de hoy.
  • Venta nueva de hoy (ML por imprimir / lista, TN por empaquetar) que no está en ninguna búsqueda abierta
    → se suma a la búsqueda automática del día ("Ventas dd/mm"; si no existe se crea) y se reparte entre
      los que estén sumados. Aviso "nueva".
  • Venta que está en una búsqueda abierta y se canceló → el paquete queda CANCELADO (sus prendas salen de la
    búsqueda y la mesa de control lo bloquea). Aviso "cancelada" (rojo, con sonido).
  • Las canceladas que no estaban en la búsqueda NO generan aviso: se ven solo en el recuadro
    "Canceladas · No despachar" (las de hoy, como el panel de ML).
  • La búsqueda del día se abre sola los días hábiles (lunes a viernes, sin feriados) a las 8:00, aunque todavía
    no haya ventas. Antes de las 8, fines de semana y feriados no se abre sola: las ventas esperan al próximo
    día hábil (o a que alguien toque "Revisar ahora").
Los avisos se guardan en la tabla `novedades` y los ve todo el que tenga acceso a Recolección Ecommerce.
"""
from __future__ import annotations

import json
import re
import logging
import threading
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import ahora, hoy
from ..models import Control, ControlPaquete, Novedad, Usuario
from . import ajustes
from . import control as csvc
from . import ecommerce as svc
from . import meli
from . import tiendanube as tn
from . import ventas_api

log = logging.getLogger("parka.ventas_auto")
_lock = threading.Lock()
_ultima = [None]   # datetime de la última revisión (en memoria)
NOMBRE_CANAL = {"flex": "ML Flex", "colecta": "ML Colecta", "tiendanube": "Tienda Nube", "cambio": "Cambio"}
MAX_CONSULTAS = 25   # consultas de estado por vuelta (para no gastar la API)
HORA_APERTURA = 8    # la búsqueda del día se abre sola a esta hora (días hábiles)

# Feriados nacionales de Argentina (inamovibles, trasladables ya movidos y puentes turísticos).
# Se pueden sumar más en el ajuste "re_feriados" (fechas aaaa-mm-dd separadas por coma).
FERIADOS = {
    # 2026
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-03-23", "2026-03-24", "2026-04-02", "2026-04-03",
    "2026-05-01", "2026-05-25", "2026-06-15", "2026-06-20", "2026-07-09", "2026-07-10", "2026-08-17",
    "2026-10-12", "2026-11-23", "2026-12-07", "2026-12-08", "2026-12-25",
    # 2027 (inamovibles; los trasladables y puentes se suman en re_feriados cuando salga el decreto)
    "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-24", "2027-03-26", "2027-04-02", "2027-05-01",
    "2027-05-25", "2027-06-20", "2027-07-09", "2027-12-08", "2027-12-25",
}


def feriados(db=None) -> set[str]:
    try:
        extra = ajustes.leer("re_feriados", db) or ""
    except Exception:  # noqa: BLE001 — sin el ajuste se usan los de la lista
        extra = ""
    return FERIADOS | {x.strip() for x in extra.replace(";", ",").split(",") if x.strip()}


def dia_habil(d: date, db=None) -> bool:
    return d.weekday() < 5 and d.isoformat() not in feriados(db)


def puede_abrir(db=None) -> bool:
    """¿Ya se puede abrir sola la búsqueda de hoy? Lunes a viernes hábiles desde las 8:00."""
    t = ahora()
    return dia_habil(t.date(), db) and t.hour >= HORA_APERTURA


def activo(db=None) -> bool:
    return (ajustes.leer("re_auto", db) or "1").strip() not in ("0", "no", "false", "")


MINUTOS_OPCIONES = (3, 5, 10)


def minutos(db=None) -> int:
    try:
        return max(1, int(ajustes.leer("re_auto_min", db) or 3))
    except ValueError:
        return 3


def estado(db) -> dict:
    try:
        e = json.loads(ajustes.leer("re_auto_estado", db) or "{}")
    except ValueError:
        e = {}
    return {"activo": activo(db), "minutos": minutos(db), **e}


# ─────────────────────────── novedades ───────────────────────────

def avisar(db: Session, tipo: str, texto: str, detalle: str = "", control_id: int | None = None,
           clave: str | None = None, para: str = "") -> Novedad | None:
    """Guarda un aviso. Con `clave` no se repite nunca (ej. la misma cancelación)."""
    if clave and db.scalar(select(Novedad.id).where(Novedad.clave == clave)):
        return None
    n = Novedad(tipo=tipo, texto=texto[:300], detalle=detalle, control_id=control_id, clave=clave, para=para)
    db.add(n)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return None
    return n


def novedad_json(n: Novedad) -> dict:
    return {"id": n.id, "tipo": n.tipo, "texto": n.texto, "detalle": n.detalle, "control_id": n.control_id,
            "hora": n.ts.strftime("%H:%M"), "fecha": n.ts.strftime("%d/%m"), "para": n.para or ""}


def _visibles(supervisa: bool):
    return True if supervisa else ((Novedad.para == "") | Novedad.para.is_(None))


def novedades(db: Session, desde: int = 0, limite: int = 30, supervisa: bool = True) -> list[dict]:
    q = select(Novedad).where(_visibles(supervisa)).order_by(Novedad.id.desc()).limit(limite)
    if desde:
        q = select(Novedad).where(Novedad.id > desde, _visibles(supervisa)).order_by(Novedad.id).limit(limite)
    return [novedad_json(n) for n in db.scalars(q).all()]


def ultima_id(db: Session) -> int:
    return db.scalar(select(Novedad.id).order_by(Novedad.id.desc()).limit(1)) or 0


# ─────────────────────────── "No la encontré" ───────────────────────────

def revisar_stock_faltante(db: Session, items: list, u: Usuario) -> dict:
    """El operario no encontró la prenda: se mira el stock (Odoo en vivo; si no, el guardado). Si no queda stock
    (o no se pudo verificar) se avisa a admins y supervisores."""
    from ..models import Articulo
    from . import catalogo
    res = {"sin_stock": [], "sin_verificar": [], "con_stock": []}
    for it in items:
        if not it.articulo_id:
            res["sin_verificar"].append((it, None))
            continue
        ids = csvc._ids(it.aceptados) or [it.articulo_id]
        arts = [a for a in (db.get(Articulo, x) for x in ids) if a]
        stock, verificado = None, False
        try:
            vivo = catalogo.stock_en_vivo(db, arts)
            if vivo:
                stock, verificado = sum(max(0.0, v) for v in vivo.values()), True
        except Exception:  # noqa: BLE001 — Odoo sin configurar o sin conexión
            pass
        if not verificado:
            guardados = [a.stock for a in arts if a.stock is not None]
            stock = sum(max(0.0, x) for x in guardados) if guardados else None
        clave = "sin_stock" if stock is not None and stock <= 0 else ("con_stock" if stock else "sin_verificar")
        res[clave].append((it, stock))
    for clave, lista in (("sin_stock", res["sin_stock"]), ("sin_verificar", res["sin_verificar"])):
        for it, stock in lista:
            p = it.paquete
            nombre = f"{it.articulo} {it.color} {it.talle}".strip() if it.articulo_id else (it.producto or it.sku)[:60]
            canal = NOMBRE_CANAL.get(p.tipo, p.tipo)
            texto = (f"🚫 SIN STOCK: {nombre}" if clave == "sin_stock" else f"⚠️ No encontraron {nombre} (stock sin verificar)")
            avisar(db, "sin_stock", texto + f" · {canal} {'#' if p.tipo == 'tiendanube' else ''}{p.tracking}",
                   f"Lo marcó {u.nombre}. Comprador: {p.comprador or '-'} · venta {p.venta_id or '-'}"
                   + (f"\nUbicación cargada: {it.ubicacion}" if it.ubicacion else "")
                   + ("\nHay que avisar al comprador / cancelar o reemplazar." if clave == "sin_stock" else
                      "\nRevisá el stock en Odoo."),
                   p.control_id, clave=f"faltante:{it.id}", para="supervision")
    db.commit()
    return {k: len(v) for k, v in res.items()} | {
        "stock": {it.id: s for v in res.values() for it, s in v}}


# ─────────────────────────── la búsqueda del día ───────────────────────────

def busqueda_del_dia(db: Session) -> Control | None:
    return db.scalar(select(Control).where(Control.modo == svc.MODO, Control.origen == "auto",
                                           Control.fecha == hoy(), Control.estado == "abierto")
                     .order_by(Control.id.desc()).limit(1))


def _crear_busqueda(db: Session) -> Control:
    admin = db.scalar(select(Usuario).where(Usuario.rol == "admin", Usuario.activo.is_(True)).order_by(Usuario.id))
    c = Control(fecha=hoy(), usuario_id=admin.id if admin else None, origen="auto", modo=svc.MODO,
                nombre=f"Ventas {hoy().strftime('%d/%m')}", tipo="mixto", archivos="API automática")
    db.add(c)
    db.flush()
    svc.sumar_recolectores(db, c)      # los Buscadores / Recolectores entran solos
    return c


def _cerrar_dias_anteriores(db: Session) -> int:
    """Una búsqueda por día, sin arrastre: las búsquedas automáticas de días anteriores se cierran como están
    (lo que quedó pendiente NO pasa a la de hoy)."""
    viejas = list(db.scalars(select(Control).where(Control.modo == svc.MODO, Control.origen == "auto",
                                                   Control.estado == "abierto", Control.fecha < hoy())).all())
    for v in viejas:
        v.estado, v.cerrado_at = "cerrado", ahora()
    if viejas:
        db.flush()
    return len(viejas)


def _de_dias_anteriores(db: Session) -> set[str]:
    """Nº de envío / venta de todo lo que ya estuvo en una búsqueda de Recolección Ecommerce de otro día."""
    filas = db.execute(select(ControlPaquete.tracking, ControlPaquete.tracking_alt, ControlPaquete.venta_id)
                       .join(Control, ControlPaquete.control_id == Control.id)
                       .where(Control.modo == svc.MODO, Control.fecha < hoy())).all()
    return {str(x) for f in filas for x in f if x}


def _claves(p: dict) -> list[str]:
    return [str(k) for k in [p.get("tracking"), p.get("venta_id"), *(p.get("ordenes") or [])] if k]


def _traer_de_dias_anteriores(db: Session, c: Control, pendientes: list[dict]) -> list[dict]:
    """Toda venta que ML / TN siguen mostrando para despachar HOY ("Envíos de hoy": etiquetas por imprimir,
    listas, demoradas; TN por empaquetar) va a la búsqueda de hoy, aunque haya estado en la búsqueda de un día
    anterior (ej. vendida ayer después del corte, o demorada). Pasa con lo ya buscado / controlado.
    Lo de días anteriores que ML / TN ya no muestran pendiente se queda donde estaba."""
    idx: dict[str, dict] = {}
    for p in pendientes:
        if p.get("hoy"):
            for k in _claves(p):
                idx.setdefault(k, p)
    if not idx:
        return []
    filas = db.scalars(select(ControlPaquete).join(Control, ControlPaquete.control_id == Control.id)
                       .where(Control.modo == svc.MODO, Control.fecha < hoy())
                       .order_by(ControlPaquete.id.desc())).all()
    ya = {cp.tracking for cp in c.paquetes}
    movidas = []
    for cp in filas:
        if cp.cancelado:
            continue
        p = idx.get(cp.tracking) or idx.get(cp.tracking_alt or "") or idx.get(cp.venta_id or "")
        if not p:
            continue
        trk = str(p["tracking"])
        if trk in ya:
            continue          # ya está en la de hoy (o vino de otra búsqueda vieja más nueva)
        if cp.tracking != trk:
            cp.tracking_alt = cp.tracking_alt or cp.tracking
            cp.tracking = trk[:60]
        if p.get("etiqueta"):
            cp.etiqueta = str(p["etiqueta"])[:20]
        vieja = db.get(Control, cp.control_id)
        if vieja is not None:
            vieja.paquetes.remove(cp)
        c.paquetes.append(cp)
        ya.add(trk)
        movidas.append(p)
    if movidas:
        db.flush()
        log.info("Ventas que estaban en búsquedas de días anteriores y se despachan hoy → búsqueda del día: %s",
                 [p["tracking"] for p in movidas])
    return movidas


def _es_del_dia(p: dict, previas: set[str] | None = None) -> bool:
    """Venta que va a la búsqueda de HOY: todo lo que ML / TN muestran para despachar hoy (etiquetas por imprimir,
    listas y demoradas; TN por empaquetar). Lo de "Próximos días" no entra."""
    return bool(p.get("hoy"))


def reparar_arrastre(db: Session) -> int:
    """Una vez: deshace el arrastre de ventas de días anteriores a la búsqueda del día (versiones viejas lo
    hacían). Los paquetes vuelven a la búsqueda de su día, que queda cerrada."""
    if ajustes.leer("re_fix_arrastre", db) == "1":
        return 0
    movidos = 0
    for n in db.scalars(select(Novedad).where(Novedad.clave.like("arrastre:%"))).all():
        c = db.get(Control, n.control_id) if n.control_id else None
        if c is None:
            continue
        previa = db.scalar(select(Control).where(Control.modo == svc.MODO, Control.origen == "auto",
                                                 Control.fecha < c.fecha).order_by(Control.fecha.desc(), Control.id.desc()).limit(1))
        nuevas = set()
        for m in db.scalars(select(Novedad).where(Novedad.control_id == c.id, Novedad.clave.like("nueva:%"))).all():
            nuevas.add(m.clave.split(":")[-1])
        # lo que entró en la apertura (aviso de apertura) también es del día
        for m in db.scalars(select(Novedad).where(Novedad.control_id == c.id, Novedad.clave.like("apertura:%"))).all():
            nuevas |= set(re.findall(r"\b(\d{4,})\b", m.detalle or ""))
        corte = min((cp.id for cp in c.paquetes if cp.tracking in nuevas), default=None)
        if previa is None or corte is None:
            continue
        for cp in list(c.paquetes):
            if cp.id < corte and cp.tracking not in nuevas:
                c.paquetes.remove(cp)
                previa.paquetes.append(cp)
                movidos += 1
        db.delete(n)
        if movidos:
            db.flush()
            svc.repartir(db, c)
    ajustes.guardar(db, "re_fix_arrastre", "1")
    return movidos


NOMBRE_CORTO = {"colecta": "Colecta", "flex": "Flex", "tiendanube": "Tienda Nube"}


def _por_canal(c: Control) -> str:
    n: dict[str, int] = {}
    for cp in c.paquetes:
        if not cp.cancelado:
            k = NOMBRE_CORTO.get(cp.tipo, cp.tipo or "otras")
            n[k] = n.get(k, 0) + 1
    return " + ".join(f"{v} {k}" for k, v in n.items())


def _juntar_en_la_del_dia(db: Session, c: Control, ventas_hoy: list[dict]) -> int:
    """Pasa a la búsqueda del día las ventas de hoy que estaban en otra búsqueda abierta de Recolección Ecommerce
    (con lo ya buscado / controlado). Una búsqueda que queda vacía se cierra."""
    idx: dict[str, dict] = {}
    for p in ventas_hoy:
        for k in [p.get("tracking"), p.get("venta_id"), *(p.get("ordenes") or [])]:
            if k:
                idx.setdefault(str(k), p)
    ya = {cp.tracking for cp in c.paquetes}
    movidos = 0
    for otra in _abiertas(db):
        if otra.id == c.id or otra.fecha != c.fecha:
            continue          # solo búsquedas de hoy: lo de otros días no se mezcla
        for cp in list(otra.paquetes):
            if cp.cancelado:
                continue
            p = idx.get(cp.tracking) or idx.get(cp.tracking_alt or "") or idx.get(cp.venta_id or "")
            if not p:
                continue
            trk = str(p["tracking"])
            if trk in ya:
                continue          # ya está en la del día (la misma venta cargada dos veces)
            if cp.tracking != trk:
                cp.tracking_alt = cp.tracking_alt or cp.tracking
                cp.tracking = trk[:60]
            cp.tipo = cp.tipo or str(p.get("tipo") or "")[:20]
            if p.get("etiqueta"):
                cp.etiqueta = str(p["etiqueta"])[:20]
            otra.paquetes.remove(cp)
            c.paquetes.append(cp)
            ya.add(trk)
            movidos += 1
        if not otra.paquetes:
            otra.estado, otra.cerrado_at = "cerrado", ahora()
    if movidos:
        db.flush()
    return movidos


def _actualizar_apertura(db: Session, c: Control) -> None:
    """El aviso "Se abrió la búsqueda del día" siempre muestra el total de ventas de la salida (incluye las ya
    buscadas / controladas y las que fueron entrando)."""
    n = db.scalar(select(Novedad).where(Novedad.control_id == c.id, Novedad.texto.like("%úsqueda del día (%"))
                  .order_by(Novedad.id).limit(1))
    if not n:
        return
    total = sum(1 for cp in c.paquetes if not cp.cancelado)
    texto = (f"🛍️ Búsqueda del día ({c.fecha.strftime('%d/%m')}): {total} venta(s) en total"
             + (f" · {_por_canal(c)}" if total else ""))[:300]
    if n.texto != texto:
        n.texto = texto
        n.detalle = "\n".join(f"{NOMBRE_CANAL.get(cp.tipo, cp.tipo or '')} {'#' if cp.tipo == 'tiendanube' else ''}{cp.tracking}"
                              + (f" · {cp.comprador}" if cp.comprador else "")
                              + (" ✓" if cp.estado == "completo" else "")
                              for cp in c.paquetes if not cp.cancelado)


def _abiertas(db: Session) -> list[Control]:
    return list(db.scalars(select(Control).where(Control.modo == svc.MODO, Control.estado == "abierto")).all())


def _desc(p: dict) -> str:
    canal = NOMBRE_CANAL.get(p.get("tipo"), p.get("tipo") or "")
    nro = ("#" if p.get("tipo") == "tiendanube" else "") + str(p.get("tracking"))
    prendas = " + ".join(f"{i.get('cantidad', 1)}× {str(i.get('producto') or i.get('sku') or '')[:40]}"
                         for i in p.get("items") or [])
    return f"{canal} {nro}" + (f" · {p.get('comprador')}" if p.get("comprador") else "") + (f" · {prendas}" if prendas else "")


def _cancelada(cp: ControlPaquete, cancel: set[str], pend: set[str], consultas: list[int]) -> bool:
    """¿Esta venta de la búsqueda se canceló? Se mira en las listas y, si desapareció de pendientes, se consulta."""
    if cp.tracking in cancel:
        return True
    if cp.tracking in pend or consultas[0] >= MAX_CONSULTAS:
        return False
    try:
        if cp.tipo in ("flex", "colecta"):
            if not meli.configurado():
                return False
            ya = cp.tracking in ventas_api._estado
            st = ventas_api.estado_ml(cp.tracking)
            if not ya:
                consultas[0] += 1
            return st == "cancelled"
        if cp.tipo == "tiendanube" and cp.tracking.isdigit():
            if not tn.configurado():
                return False
            consultas[0] += 1
            return ventas_api.estado_tn(cp.tracking) == "cancelled"
    except Exception:  # noqa: BLE001 — una consulta que falla no frena la revisión
        return False
    return False


def sincronizar(db: Session, forzar: bool = False) -> dict:
    """Una vuelta: suma ventas nuevas a la búsqueda del día y marca / avisa las canceladas."""
    if not forzar and not activo(db):
        return {"ok": False, "motivo": "apagado"}
    from . import cambios as cambios_svc
    if not (tn.configurado(db) or meli.configurado() or cambios_svc.configurado(db)):
        return {"ok": False, "motivo": "sin conexiones"}
    with _lock:
        abrir = forzar or puede_abrir(db)
        _cerrar_dias_anteriores(db)
        d = ventas_api.todas(refrescar=True)
        errores = [f"{'Tienda Nube' if k == 'tn' else 'Mercado Libre'}: {d[k]['error']}" for k in ("tn", "ml") if d[k]["error"]]
        pendientes = [p for k in ("tn", "ml") for p in d[k]["paquetes"]]
        canceladas = [p for k in ("tn", "ml") for p in d[k].get("canceladas") or []]
        # cambios aceptados de la app de cambios: se busca la prenda que le mandamos al cliente
        if cambios_svc.configurado(db):
            try:
                c_pend, c_canc = cambios_svc.para_busqueda()
                pendientes += c_pend
                canceladas += c_canc
            except Exception as e:  # noqa: BLE001 — la app de cambios caída no frena ML / TN
                errores.append(f"App de cambios: {e}")
        c = busqueda_del_dia(db)
        creada = False
        if c is None and abrir:
            c, creada = _crear_busqueda(db), True
        # ventas que quedaron en la búsqueda de ayer pero ML las pide hoy → pasan a la de hoy
        traidas = _traer_de_dias_anteriores(db, c, pendientes) if c is not None else []
        del_dia = [p for p in pendientes if _es_del_dia(p)]
        # 0) todas las ventas de hoy van a LA búsqueda del día: las que estaban en otra búsqueda abierta
        #    (armada a mano, de archivo…) pasan con lo que ya se buscó / controló
        if c is not None:
            juntadas = _juntar_en_la_del_dia(db, c, del_dia)
        else:
            juntadas = 0
        abiertas = _abiertas(db)
        en_busqueda: dict[str, ControlPaquete] = {}
        de_hoy: set[str] = set()
        for cc in abiertas:
            for cp in cc.paquetes:
                en_busqueda.setdefault(cp.tracking, cp)
                if cc.fecha == hoy():
                    de_hoy.update(x for x in (cp.tracking, cp.tracking_alt, cp.venta_id) if x)
        # 1) ventas nuevas de hoy (una búsqueda vieja que quedó abierta no las frena)
        nuevas = [p for p in del_dia if not any(k in de_hoy for k in _claves(p))]
        if c is None:
            nuevas = []          # antes de las 8, fin de semana o feriado: esperan a que se abra la del día
        if nuevas:
            csvc.agregar_paquetes(db, c, nuevas)
        if nuevas or juntadas or traidas:
            db.flush()
            svc.repartir(db, c)
        if creada:
            total = len(c.paquetes)
            avisar(db, "nueva", f"🛍️ Se abrió la búsqueda del día ({hoy().strftime('%d/%m')}): {total} venta(s) en total"
                   + (f" · {_por_canal(c)}" if total else ""),
                   "\n".join(_desc(p) for p in traidas + nuevas) if (nuevas or traidas) else
                   ("" if total else "Todavía no hay ventas: van a ir entrando solas."),
                   c.id, clave=f"apertura:{hoy().isoformat()}:{c.id}")
        if traidas and not creada:
            total = len(c.paquetes)
            for p in traidas:
                avisar(db, "nueva", f"🛍️ Pasó a hoy (sale hoy): {_desc(p)}",
                       f"Estaba en la búsqueda de un día anterior sin terminar. La búsqueda del día ahora tiene {total} ventas.",
                       c.id, clave=f"traida:{hoy().isoformat()}:{p['clave']}")
        if nuevas and not creada:
            total = len(c.paquetes)
            for p in nuevas:
                avisar(db, "nueva", f"🛍️ Entró una venta: {_desc(p)}", f"La búsqueda del día ahora tiene {total} ventas.",
                       c.id, clave=f"nueva:{p['clave']}")
        # la etiqueta de ML se imprimió después → se actualiza en la búsqueda
        por_trk = {p["tracking"]: p for p in pendientes}
        for trk, cp in en_busqueda.items():
            if trk in por_trk and por_trk[trk].get("etiqueta") and cp.etiqueta != por_trk[trk]["etiqueta"]:
                cp.etiqueta = por_trk[trk]["etiqueta"]
        # 2) canceladas
        cancel_trk = {p["tracking"] for p in canceladas}
        pend_trk = set(por_trk)
        consultas = [0]
        tocadas = set()
        n_cancel = 0
        for trk, cp in en_busqueda.items():
            if cp.cancelado or cp.estado == "completo" and trk in pend_trk:
                continue
            if _cancelada(cp, cancel_trk, pend_trk, consultas):
                cp.cancelado, cp.cancelado_at = True, ahora()
                cp.nota = ("VENTA CANCELADA · NO DESPACHAR. " + (cp.nota or ""))[:300]
                tocadas.add(cp.control_id)
                n_cancel += 1
                juntadas = sum(min(i.buscado, i.cantidad) for i in cp.items)
                avisar(db, "cancelada", f"⛔ {'Cambio CANCELADO' if cp.tipo == 'cambio' else 'Venta CANCELADA'} · no despachar: {NOMBRE_CANAL.get(cp.tipo, cp.tipo)} "
                       f"{'#' if cp.tipo == 'tiendanube' else ''}{trk}" + (f" · {cp.comprador}" if cp.comprador else ""),
                       (f"Ya se juntaron {juntadas} prenda(s): devolvelas a su lugar." if juntadas else
                        "Sus prendas salieron de la búsqueda."), cp.control_id, clave=f"cancel:{cp.tipo}:{trk}")
        for cid in tocadas:
            cc = db.get(Control, cid)
            if cc:
                svc.repartir(db, cc)
        # las canceladas que no estaban en la búsqueda no generan aviso: se ven en "Canceladas · No despachar"
        if c is not None and not creada:
            _actualizar_apertura(db, c)
        res = {"hora": ahora().strftime("%H:%M"), "nuevas": len(nuevas) + len(traidas), "canceladas": n_cancel, "juntadas": juntadas,
               "pendientes": len(pendientes), "errores": errores, "busqueda": c.id if c else None}
        ajustes.guardar(db, "re_auto_estado", json.dumps(res))   # hace commit
        _ultima[0] = ahora()
        return {"ok": True, **res}


def toca(db: Session) -> bool:
    if not activo(db):
        return False
    u = _ultima[0]
    if busqueda_del_dia(db) is None and puede_abrir(db):
        return True       # 8:00 de un día hábil: se abre enseguida, sin esperar la vuelta
    return u is None or (ahora() - u).total_seconds() >= minutos(db) * 60 - 5


def limpiar_avisos_cancelada_sueltos(db: Session) -> int:
    """Borra los avisos viejos "Cancelada · no despachar" de ventas que no estaban en ninguna búsqueda."""
    from sqlalchemy import delete
    r = db.execute(delete(Novedad).where(Novedad.tipo == "cancelada", Novedad.control_id.is_(None),
                                         Novedad.clave.like("cancel:%")))
    db.commit()
    return r.rowcount or 0
