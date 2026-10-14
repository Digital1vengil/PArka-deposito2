"""Sección Recolección Ecommerce (reemplaza a Recolección y a Control de paquetes).

- Preparar: el admin (o Supervisión) crea la búsqueda con los archivos de la carpeta del día en Drive.
- Equipo: los operarios se suman; la app reparte la ruta en tramos parejos.
- Mi búsqueda: cada uno ve lo suyo en orden de ruta y escanea lo que encuentra (verde / rojo).
- Mesa de control: se usa la API de /control (etiqueta del envío → prendas)."""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import puede, requiere
from ..db import get_db, hoy
from ..models import Control, ControlItem, Usuario
from ..services import ecommerce as svc
from ..services import re_drive
from ..services import meli
from ..services import tiendanube as tn
from ..services import ventas_api
from ..services import ventas_auto
from ..templating import render

router = APIRouter(prefix="/ecommerce")
acceso = requiere("ecommerce")
MAX_BYTES = 15 * 1024 * 1024


def _organiza(db: Session, u: Usuario) -> bool:
    """Crear búsquedas, repartir y sacar gente: admin o quien tenga Supervisión."""
    return puede(db, u, "supervision")


def _exigir_organiza(db: Session, u: Usuario) -> None:
    if not _organiza(db, u):
        raise HTTPException(403, "Esto lo hace quien organiza la búsqueda (admin o con acceso a Supervisión)")


def _control(db: Session, cid: int) -> Control:
    c = db.get(Control, cid)
    if not c or c.modo != svc.MODO:
        raise HTTPException(404, "No existe esa búsqueda")
    return c


def _abierta(c: Control) -> None:
    if c.estado != "abierto":
        raise HTTPException(400, "Esta búsqueda ya está cerrada")


@router.get("")
def pagina(request: Request, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return render(request, "ecommerce.html", u=u, organiza=_organiza(db, u), drive_ok=re_drive.configurado(db),
                  tn_ok=tn.configurado(db), ml_ok=meli.configurado(), auto=ventas_auto.estado(db),
                  puede_vincular=puede(db, u, "supervision"))


# ─────────────── preparar ───────────────

@router.get("/api/drive")
def drive_hoy(fecha: str = "", u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    try:
        if fecha and fecha != hoy().isoformat():
            date.fromisoformat(fecha)
            d = re_drive.listar(db, fecha)
        else:
            d = re_drive.hoy(db)
    except ValueError:
        raise HTTPException(400, "Fecha inválida")
    except re_drive.REDriveError as e:
        raise HTTPException(502, str(e))
    archivos = [a for a in (d.get("archivos") or []) if a.get("tipo") in ("ml", "tn", "otro")]
    return {"fecha": d.get("fecha") or fecha, "url": d.get("url", ""), "existe": d.get("existe", True),
            "archivos": archivos}


class DriveIn(BaseModel):
    ids: list[str] = []
    nombre: str = ""
    tn_api: bool = False


def _ordenes_tn(db: Session, pedir: bool) -> list[dict] | None:
    """Órdenes pagadas sin enviar de Tienda Nube por la API (si se pidió y está conectada)."""
    if not pedir:
        return None
    if not tn.configurado(db):
        raise HTTPException(400, "Tienda Nube no está conectada (Conexiones → Tienda Nube)")
    try:
        return tn.ordenes_pendientes()
    except tn.TNError as e:
        raise HTTPException(502, f"Tienda Nube: {e}")


@router.post("/api/crear/drive")
def crear_drive(body: DriveIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    if not body.ids and not body.tn_api:
        raise HTTPException(400, "Marcá al menos un archivo o las órdenes de Tienda Nube")
    extra = _ordenes_tn(db, body.tn_api)
    try:
        datos = [re_drive.archivo(db, i) for i in body.ids[:20]]
    except re_drive.REDriveError as e:
        raise HTTPException(502, str(e))
    return _crear(db, u, datos, body.nombre, "drive" if datos else "tiendanube", extra)


async def _leer_subidos(archivos: list[UploadFile]) -> list[tuple[str, bytes]]:
    datos = []
    for f in archivos:
        b = await f.read()
        if len(b) > MAX_BYTES:
            raise HTTPException(400, f"{f.filename}: archivo demasiado grande")
        datos.append((f.filename or "archivo", b))
    return datos


@router.post("/api/crear/archivo")
async def crear_archivo(nombre: str = Form(""), tn_api: str = Form(""),
                        archivos: list[UploadFile] = File(default_factory=list),
                        u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    pedir = tn_api in ("1", "true", "on")
    if not archivos and not pedir:
        raise HTTPException(400, "Elegí los archivos de ventas")
    extra = _ordenes_tn(db, pedir)
    return _crear(db, u, await _leer_subidos(archivos), nombre, "archivo", extra)


def _crear(db: Session, u: Usuario, datos: list[tuple[str, bytes]], nombre: str, origen: str,
           extra: list[dict] | None = None) -> dict:
    if extra is not None and not extra and not datos:
        raise HTTPException(400, "Tienda Nube no tiene órdenes pagadas sin enviar")
    try:
        c, errores = svc.crear(db, u, datos, nombre, origen, extra)
    except ValueError as e:
        raise HTTPException(400, str(e))
    registrar(db, u, "ecommerce_creada", f"RE-{c.id} · {c.nombre} · {len(c.paquetes)} paquetes", "ecommerce")
    return {"ok": True, "busqueda": svc.resumen(c, u), "errores": errores}


@router.post("/api/{cid}/agregar")
async def agregar(cid: int, archivos: list[UploadFile] = File(...), u: Usuario = Depends(acceso),
                  db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    c = _control(db, cid)
    _abierta(c)
    nuevos, act, errores = svc.sumar_archivos(db, c, await _leer_subidos(archivos))
    return {"ok": True, "nuevos": nuevos, "actualizados": act, "errores": errores, "busqueda": svc.resumen(c, u)}


# ─────────────── ventas por API (TN "Por empaquetar" + ML por imprimir / listas) ───────────────

def _json_venta(p: dict) -> dict:
    return {k: p.get(k) for k in ("clave", "tracking", "venta_id", "comprador", "tipo", "etiqueta", "subestado",
                                  "hasta", "hoy", "cantidad", "medio")} | {
        "resumen": " + ".join(f"{i.get('cantidad', 1)}× {i.get('producto', '')[:45]}"
                              + (f" ({' '.join(x for x in (i.get('color'), i.get('talle')) if x)})"
                                 if (i.get('color') or i.get('talle')) and "(" not in i.get('producto', '') else "")
                              for i in p.get("items") or [])}


@router.get("/api/ventas")
def ventas(refrescar: bool = False, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    d = ventas_api.todas(refrescar)
    return {"hora": d["hora"],
            **{k: {"ok": d[k]["ok"], "conectado": d[k]["conectado"], "error": d[k]["error"],
                   "ventas": [_json_venta(p) for p in d[k]["paquetes"]],
                   "canceladas": [_json_venta(p) for p in d[k].get("canceladas") or []]} for k in ("tn", "ml")}}


# ─────────────── búsqueda automática y avisos ───────────────

@router.get("/api/novedades")
def ver_novedades(desde: int = 0, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """desde=-1: solo el último id (para arrancar sin mostrar avisos viejos). desde=0: las últimas 30."""
    if desde < 0:
        return {"ultimo": ventas_auto.ultima_id(db), "novedades": []}
    ns = ventas_auto.novedades(db, desde, supervisa=_organiza(db, u))
    return {"ultimo": max([n["id"] for n in ns] + [desde]), "novedades": ns}


@router.get("/api/auto")
def auto_estado(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    c = ventas_auto.busqueda_del_dia(db)
    return {**ventas_auto.estado(db), "busqueda_id": c.id if c else None}


class AutoIn(BaseModel):
    activo: bool | None = None
    minutos: int | None = None


@router.post("/api/auto")
def auto_cambiar(body: AutoIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    from ..services import ajustes
    if body.activo is not None:
        ajustes.guardar(db, "re_auto", "1" if body.activo else "0")
        registrar(db, u, "ecommerce_auto", "búsqueda automática " + ("activada" if body.activo else "pausada"), "ecommerce")
    if body.minutos is not None:
        if body.minutos not in ventas_auto.MINUTOS_OPCIONES:
            raise HTTPException(400, "Elegí 3, 5 o 10 minutos")
        ajustes.guardar(db, "re_auto_min", str(body.minutos))
        registrar(db, u, "ecommerce_auto", f"búsqueda automática cada {body.minutos} min", "ecommerce")
    return ventas_auto.estado(db)


@router.post("/api/auto/ahora")
def auto_ahora(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Revisar ML + TN ya (sin esperar los minutos)."""
    _exigir_organiza(db, u)
    r = ventas_auto.sincronizar(db, forzar=True)
    c = ventas_auto.busqueda_del_dia(db)
    return {**r, "busqueda_id": c.id if c else None}


class VentasIn(BaseModel):
    claves: list[str]
    nombre: str = ""


@router.post("/api/crear/ventas")
def crear_ventas(body: VentasIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    if not body.claves:
        raise HTTPException(400, "Marcá al menos una venta")
    extra = ventas_api.por_claves(body.claves)
    if not extra:
        raise HTTPException(400, "Esas ventas ya no están pendientes: tocá ↻ Actualizar")
    return _crear(db, u, [], body.nombre, "api", extra)


@router.post("/api/{cid}/agregar-ventas")
def agregar_ventas(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Suma a una búsqueda abierta las ventas nuevas de hoy (TN por empaquetar + ML por imprimir / listas)
    y actualiza si la etiqueta de ML ya se imprimió. Lo que ya está no se duplica."""
    _exigir_organiza(db, u)
    c = _control(db, cid)
    _abierta(c)
    d = ventas_api.todas(refrescar=True)
    extra = [p for k in ("tn", "ml") for p in d[k]["paquetes"] if p.get("hoy")]
    errores = [f"{'Tienda Nube' if k == 'tn' else 'Mercado Libre'}: {d[k]['error']}" for k in ("tn", "ml") if d[k]["error"]]
    nuevos, act, err2 = svc.sumar_archivos(db, c, [], extra) if extra else (0, 0, [])
    db.commit()
    registrar(db, u, "ecommerce_agregado", f"RE-{c.id} · ventas por API · {nuevos} nuevas", "ecommerce")
    return {"ok": True, "nuevos": nuevos, "actualizados": act, "errores": errores + err2, "busqueda": svc.resumen(c, u)}


@router.post("/api/{cid}/agregar-tn")
def agregar_tn(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Suma a una búsqueda abierta las órdenes de Tienda Nube que entraron después (las que ya están no se duplican)."""
    _exigir_organiza(db, u)
    c = _control(db, cid)
    _abierta(c)
    extra = _ordenes_tn(db, True)
    nuevos, act, errores = svc.sumar_archivos(db, c, [], extra)
    registrar(db, u, "ecommerce_agregado", f"RE-{c.id} · Tienda Nube API · {nuevos} órdenes nuevas", "ecommerce")
    return {"ok": True, "nuevos": nuevos, "actualizados": act, "errores": errores, "busqueda": svc.resumen(c, u)}


@router.get("/api/lista")
def lista(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return {"busquedas": [svc.resumen(c, u) for c in svc.lista(db)], "hoy": hoy().isoformat()}


@router.get("/api/{cid}")
def ver(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    c = _control(db, cid)
    return {"busqueda": svc.resumen(c, u)}


# ─────────────── equipo ───────────────

@router.post("/api/{cid}/sumarme")
def sumarme(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    c = _control(db, cid)
    _abierta(c)
    svc.sumarse(db, c, u)
    registrar(db, u, "ecommerce_sumado", f"RE-{c.id} · {c.nombre}", "ecommerce")
    return {"ok": True, "busqueda": svc.resumen(c, u)}


@router.get("/api/{cid}/candidatos")
def candidatos(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Personas que quien organiza puede sumar: usuarios activos con acceso a Recolección Ecommerce."""
    from sqlalchemy import select
    _exigir_organiza(db, u)
    c = _control(db, cid)
    dentro = {p.usuario_id for p in svc.activos(c)}
    gente = db.scalars(select(Usuario).where(Usuario.activo.is_(True)).order_by(Usuario.nombre)).all()
    return {"usuarios": [{"id": x.id, "nombre": x.nombre, "rol": x.rol, "esta": x.id in dentro}
                         for x in gente if puede(db, x, "ecommerce")]}


class SumarIn(BaseModel):
    usuario_ids: list[int]


@router.post("/api/{cid}/sumar")
def sumar(cid: int, body: SumarIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Quien organiza suma gente directamente a la búsqueda (no hace falta que toquen "Me sumo")."""
    _exigir_organiza(db, u)
    c = _control(db, cid)
    _abierta(c)
    if not body.usuario_ids:
        raise HTTPException(400, "Elegí al menos una persona")
    validos = [x for x in body.usuario_ids[:50]
               if (p := db.get(Usuario, x)) and p.activo and puede(db, p, "ecommerce")]
    if not validos:
        raise HTTPException(400, "Esas personas no tienen acceso a Recolección Ecommerce")
    nuevos = svc.sumar_personas(db, c, validos)
    if nuevos:
        registrar(db, u, "ecommerce_sumado", f"RE-{c.id} · {c.nombre} · sumó a {', '.join(nuevos)}", "ecommerce")
    return {"ok": True, "sumados": nuevos, "busqueda": svc.resumen(c, u)}


@router.post("/api/{cid}/salir")
def salir(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    c = _control(db, cid)
    svc.salir(db, c, u.id)
    return {"ok": True, "busqueda": svc.resumen(c, u)}


@router.post("/api/{cid}/sacar/{uid}")
def sacar(cid: int, uid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    c = _control(db, cid)
    svc.salir(db, c, uid)
    return {"ok": True, "busqueda": svc.resumen(c, u)}


@router.post("/api/{cid}/repartir")
def repartir(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    c = _control(db, cid)
    svc.repartir(db, c)
    db.commit()
    return {"ok": True, "busqueda": svc.resumen(c, u)}


# ─────────────── búsqueda ───────────────

@router.get("/api/{cid}/mia")
def mia(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    c = _control(db, cid)
    if c.estado == "abierto":
        svc.asignar_box(db, c, u.id)        # lo marcado antes de los BOX automáticos entra a su BOX
    return {"grupos": svc.grupos_de(c, u.id, solo_pendientes=False),
            "otros": svc.grupos_de(c, None, solo_pendientes=False, excluir=u.id), "busqueda": svc.resumen(c, u),
            "box": svc.estado_box(db, c, u.id)}


@router.get("/api/{cid}/todo")
def todo(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """La guía completa (todas las personas), para quien organiza."""
    c = _control(db, cid)
    return {"grupos": svc.grupos_de(c, None, solo_pendientes=False), "busqueda": svc.resumen(c, u)}


class CodigoIn(BaseModel):
    codigo: str


@router.post("/api/{cid}/encontre")
def encontre(cid: int, body: CodigoIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    c = _control(db, cid)
    _abierta(c)
    cod = body.codigo.strip()
    if not cod:
        raise HTTPException(400, "Código vacío")
    r = svc.escanear_busqueda(db, c, u, cod)
    if not r["ok"]:
        registrar(db, u, "ecommerce_error", f"RE-{c.id}: {r.get('escaneado') or cod} ({r['motivo']})", "ecommerce")
        return {**r, "busqueda": svc.resumen(c, u)}
    return {**r, **_box_tras_marcar(db, c, u), "busqueda": svc.resumen(c, u)}


def _box_tras_marcar(db: Session, c: Control, u: Usuario) -> dict:
    """Después de "La tengo": la prenda entra sola al BOX de la persona. Si el BOX se llenó (o ya no le queda
    nada por buscar), se avisa para llevarlo a la mesa."""
    llenos = svc.asignar_box(db, c, u.id)
    est = svc.estado_box(db, c, u.id)
    if not llenos and est["pendientes"] == 0 and est["actual"] and not est["lleno"]:
        b = svc.boxes_de(c, u.id)[-1]
        if svc.box_activo(b):
            llenos = [b]
    if llenos:
        registrar(db, u, "ecommerce_box", f"RE-{c.id}: " + ", ".join(
            f"BOX {b.numero} ({svc.unidades_box(b)})" for b in llenos) + " a la mesa", "ecommerce")
    return {"box": est, "box_llevar": [svc.box_json(b) for b in llenos]}


class ItemsIn(BaseModel):
    item_ids: list[int]
    ajena: bool = False      # "Sí, la traje yo" sobre una prenda asignada a otra persona


def _lineas(db: Session, c: Control, ids: list[int]) -> list[ControlItem]:
    out = []
    for iid in ids[:50]:
        i = db.get(ControlItem, iid)
        if i and i.paquete.control_id == c.id:
            out.append(i)
    return out


@router.post("/api/{cid}/la-tengo")
def la_tengo(cid: int, body: ItemsIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """"✓ La tengo": +1 a mano (la otra opción es escanear el código de barras, que además verifica la prenda)."""
    c = _control(db, cid)
    _abierta(c)
    ls = [i for i in _lineas(db, c, body.item_ids) if svc.pendiente_busqueda(i) > 0 and not i.paquete.cancelado]
    if not ls:
        raise HTTPException(400, "Ya están todas")
    mias = [i for i in ls if i.asignado_id == u.id]
    i = (mias or ls)[0]
    quien = svc.es_de_otro(i, u)
    if quien and not body.ajena:
        return {"ok": False, "motivo": "otro", "quien": quien.nombre,
                "mensaje": f"La tiene asignada {quien.nombre}. ¿La trajiste vos?"}
    svc.marcar_buscado(db, i, u, +1)
    if quien:
        registrar(db, u, "ecommerce_ajena", f"RE-{c.id}: trajo una prenda de {quien.nombre}", "ecommerce")
    falta = sum(svc.pendiente_busqueda(i) for i in _lineas(db, c, body.item_ids))
    return {"ok": True, "faltan": falta, **_box_tras_marcar(db, c, u)}


@router.post("/api/{cid}/deshacer")
def deshacer(cid: int, body: ItemsIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """−1: me equivoqué al escanear (la prenda vuelve a su lugar y sale de su BOX)."""
    c = _control(db, cid)
    _abierta(c)
    try:
        svc.restar(db, c, _lineas(db, c, body.item_ids), u)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ─────────────── BOX: cajas de la mesa (cada persona tiene los suyos, se llenan solos) ───────────────

@router.get("/api/{cid}/boxes")
def box_lista(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    c = _control(db, cid)
    return {"boxes": svc.boxes(c), "cantidad": svc.box_cantidad(db)}


class BoxCantIn(BaseModel):
    cantidad: int


@router.post("/api/box/cantidad")
def box_cantidad(body: BoxCantIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Cuántas prendas lleva cada BOX (lo cambia quien organiza)."""
    _exigir_organiza(db, u)
    if not 1 <= body.cantidad <= 50:
        raise HTTPException(400, "Elegí entre 1 y 50 prendas por BOX")
    from ..services import ajustes
    ajustes.guardar(db, "re_box_cant", str(body.cantidad))
    registrar(db, u, "ecommerce_box", f"BOX de {body.cantidad} prendas", "ecommerce")
    return {"ok": True, "cantidad": svc.box_cantidad(db)}


class FaltanteIn(BaseModel):
    item_ids: list[int]
    nota: str = ""


@router.post("/api/{cid}/faltante")
def faltante(cid: int, body: FaltanteIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """No la encuentra: marca como faltante las unidades que quedan de esa prenda (queda una tarea)."""
    from ..services import control as csvc
    c = _control(db, cid)
    _abierta(c)
    marcadas = []
    for iid in body.item_ids[:50]:
        i = db.get(ControlItem, iid)
        if i and i.paquete.control_id == c.id and svc.pendiente_busqueda(i) > 0:
            csvc.marcar_faltante(db, i, u, body.nota.strip())
            marcadas.append(i)
    registrar(db, u, "ecommerce_faltante", f"RE-{c.id}: {len(marcadas)} línea(s)", "ecommerce")
    st = ventas_auto.revisar_stock_faltante(db, marcadas, u) if marcadas else {}
    aviso = ("No queda stock: se avisó a los supervisores." if st.get("sin_stock") else
             "Se avisó a los supervisores para que revisen el stock." if st.get("sin_verificar") else
             "En el sistema figura stock: quedó una tarea para buscarla." if st.get("con_stock") else "")
    return {"ok": True, "lineas": len(marcadas), "sin_stock": st.get("sin_stock", 0), "aviso": aviso}


@router.post("/api/{cid}/quitar-faltante")
def quitar_faltante(cid: int, body: FaltanteIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    from ..services import control as csvc
    c = _control(db, cid)
    _abierta(c)
    for iid in body.item_ids[:50]:
        i = db.get(ControlItem, iid)
        if i and i.paquete.control_id == c.id and i.faltante:
            csvc.quitar_faltante(db, i, u)
    svc.repartir(db, c)
    db.commit()
    return {"ok": True}


# ─────────────── cierre ───────────────

@router.post("/api/{cid}/cerrar")
def cerrar(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Cierra la búsqueda y guarda el Excel del resultado en la carpeta del día (si Drive está conectado)."""
    _exigir_organiza(db, u)
    from ..db import ahora
    from .control import excel
    c = _control(db, cid)
    c.estado, c.cerrado_at = "cerrado", ahora()
    db.commit()
    aviso = ""
    if re_drive.configurado(db):
        try:
            x = excel(cid, u, db)
            r = re_drive.guardar(db, c.fecha.isoformat(), f"RE-{c.id} {c.nombre.replace('/', '-')}.xlsx", x.body)
            c.drive_resultado = (r.get("url") or "")[:300]
            db.commit()
        except re_drive.REDriveError as e:
            aviso = f"Se cerró, pero no se pudo guardar el Excel en Drive: {e}"
    registrar(db, u, "ecommerce_cerrada", f"RE-{c.id} · {c.nombre}", "ecommerce")
    return {"ok": True, "busqueda": svc.resumen(c, u), "aviso": aviso}


@router.post("/api/{cid}/reabrir")
def reabrir(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    _exigir_organiza(db, u)
    c = _control(db, cid)
    c.estado, c.cerrado_at = "abierto", None
    db.commit()
    return {"ok": True, "busqueda": svc.resumen(c, u)}


@router.delete("/api/{cid}")
def borrar(cid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    from .control import borrar as borrar_control
    c = _control(db, cid)
    if not u.es_admin and c.usuario_id != u.id:
        raise HTTPException(403, "Solo quien la creó (o un admin) puede borrarla")
    return borrar_control(cid, u, db)
