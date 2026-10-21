import io
import json
import re

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, HTTPException
from pathlib import Path

from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, RedirectResponse
from openpyxl import Workbook
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere_seccion, SinPermiso, puede
from ..db import get_db, hoy, ahora
from ..models import DespachoEscaneo, Lote, Paquete, Tanda, Usuario, TIPOS_LOTE
from ..parsers import leer_archivo
from ..services import ajustes
from ..services import despacho as svc, drive, parkahub, ventas_api, meli
from ..services import tiendanube as tn
from ..services.tareas import tareas_de_seccion, tarea_json
from ..templating import render

from ..services.cambios import CambiosError

router = APIRouter(prefix="/despacho")
acceso = requiere_seccion("despacho")
MAX_BYTES = 15 * 1024 * 1024


async def _leer(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    out = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(400, f"{f.filename}: archivo demasiado grande")
        out.append((f.filename or "archivo", data))
    return out


# ─────────────── Scanner (igual que la app PARKA Despacho) ───────────────
# Hasta conectar la API de Mercado Libre, Despacho funciona exactamente como el scanner:
# se sirve la misma app (app/scanner/index.html) y habla directo con el Apps Script de Drive.
SCANNER_DIR = Path(__file__).resolve().parent.parent / "scanner"
SCANNER_ARCHIVOS = {"despacho-vivo.js", "styles.css", "manifest.json", "icon-192.png", "icon-512.png", "tema-parka.css", "ml-semaforo.js", "control-despacho.js", "tiendanube-despacho.js", "whatsapp-despacho.js"}
# Colores del scanner original (verde oscuro) → verde de la marca PARKA Supply Co.
_COLORES = {"#2f5d3a": "#1A7F4B", "#43592d": "#1A7F4B", "#547038": "#38B475", "#6a8c47": "#38B475",
            "#374a25": "#146a3e", "#263524": "#146a3e", "#f4f7f0": "#f5f5f4"}


def _scanner_html() -> str:
    html = (SCANNER_DIR / "index.html").read_text(encoding="utf-8")
    for viejo, nuevo in _COLORES.items():
        html = html.replace(viejo, nuevo).replace(viejo.upper(), nuevo)
    tema = '<link rel="stylesheet" href="tema-parka.css">\n<script src="ml-semaforo.js"></script>\n<script src="control-despacho.js"></script>\n<script src="tiendanube-despacho.js"></script>\n<script src="despacho-vivo.js"></script>\n<script src="whatsapp-despacho.js"></script>'
    html = html.replace("</body>", tema + "\n</body>", 1) if "</body>" in html else html + tema
    # cada versión con su propia dirección: el celular no puede quedarse con un script o estilo viejo
    from ..templating import VERSION
    return re.sub(r'((?:src|href)=")([\w.-]+\.(?:js|css))"', lambda m: f'{m.group(1)}{m.group(2)}?v={VERSION}"', html)


@router.get("")
def pagina(request: Request, u: Usuario = Depends(acceso)):
    return render(request, "despacho_scanner.html", u=u, ml_ok=parkahub.disponible(),
                  cotejo_ok=parkahub.configurado(), ml_fuente=parkahub.fuente())


@router.get("/scanner/")
def scanner_raiz(u: Usuario = Depends(acceso)):
    return HTMLResponse(_scanner_html(), headers={"Cache-Control": "no-cache"})


@router.get("/scanner/index.html")
def scanner_index(u: Usuario = Depends(acceso)):
    return HTMLResponse(_scanner_html(), headers={"Cache-Control": "no-cache"})


@router.get("/scanner/{archivo}")
def scanner_archivo(archivo: str, u: Usuario = Depends(acceso)):
    if archivo not in SCANNER_ARCHIVOS or not (SCANNER_DIR / archivo).exists():
        raise HTTPException(404)
    return FileResponse(SCANNER_DIR / archivo, headers={"Cache-Control": "no-cache"})


@router.get("/lotes")
def pagina_lotes(u: Usuario = Depends(acceso)):
    """El Despacho nuevo (beta) se quitó: Despacho funciona con el scanner hasta conectar la API de ML."""
    return RedirectResponse("/despacho", status_code=303)


@router.get("/escanear")
def escanear_pagina(request: Request, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return render(request, "escanear.html", u=u)


def _preview(archivos: list[tuple[str, bytes]], tipo: str, mp: dict | None) -> dict:
    resultados = []
    for nombre, data in archivos:
        try:
            r = leer_archivo(nombre, data, tipo, mp)
            resultados.append({"archivo": nombre, "formato": r["formato"], "headers": r["headers"],
                               "mapeo": r["mapeo"], "muestra": r["muestra"],
                               "cantidad": len(r["paquetes"]), "paquetes": r["paquetes"][:8]})
        except ValueError as e:
            resultados.append({"archivo": nombre, "error": str(e)})
    return {"resultados": resultados}


def _crear(db: Session, u: Usuario, archivos: list[tuple[str, bytes]], tipo: str, nombre: str,
           mp: dict | None, drive_ids: list[str] | None = None) -> dict:
    paquetes, nombres, errores = [], [], []
    for nom, data in archivos:
        try:
            r = leer_archivo(nom, data, tipo, mp)
        except ValueError as e:
            errores.append(f"{nom}: {e}")
            continue
        if not r["paquetes"]:
            errores.append(f"{nom}: no se encontraron paquetes (revisá la columna del número de envío)")
            continue
        paquetes.extend(r["paquetes"])
        nombres.append(nom)
    if not paquetes:
        raise HTTPException(400, " · ".join(errores) or "No se encontraron paquetes")
    vistos, unicos = set(), []
    for p in paquetes:
        if p["tracking"] not in vistos:
            vistos.add(p["tracking"])
            unicos.append(p)
    nombre_lote = nombre.strip() or (nombres[0] + (f" +{len(nombres)-1}" if len(nombres) > 1 else ""))
    lote = svc.crear_lote(db, u, tipo, nombre_lote, unicos, drive_ids)
    return {"ok": True, "lote_id": lote.id, "cantidad": len(unicos), "errores": errores}


@router.post("/api/preview")
async def preview(tipo: str = Form("flex"), mapeo: str = Form(""), archivos: list[UploadFile] = File(...),
                  u: Usuario = Depends(acceso)):
    return _preview(await _leer(archivos), tipo, json.loads(mapeo) if mapeo else None)


@router.post("/api/lotes")
async def crear_lotes(tipo: str = Form("flex"), nombre: str = Form(""), mapeo: str = Form(""),
                      archivos: list[UploadFile] = File(...), u: Usuario = Depends(acceso),
                      db: Session = Depends(get_db)):
    return _crear(db, u, await _leer(archivos), tipo, nombre, json.loads(mapeo) if mapeo else None)


# ─────────────── Google Drive (carpeta PARKA Despacho del scanner) ───────────────
def _drive_error(e: Exception):
    raise HTTPException(502, str(e))


@router.get("/api/drive/archivos")
def drive_archivos(tipo: str = "flex", u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    try:
        d = drive.listar(tipo)
    except drive.DriveError as e:
        _drive_error(e)
    usados = set()
    for (ids,) in db.execute(select(Lote.drive_ids).where(Lote.drive_ids != "")).all():
        usados.update(i for i in ids.split(",") if i)
    grupos = d.get("groups") or []
    for g in grupos:
        for f in g.get("files", []):
            f["cargado"] = f["id"] in usados
    return {"carpeta": d.get("rootFolderUrl", ""), "grupos": grupos[:15], "hoy": hoy().isoformat()}


class DriveSel(BaseModel):
    ids: list[str]
    tipo: str = "flex"
    nombre: str = ""
    mapeo: dict | None = None


def _bajar(ids: list[str]) -> list[tuple[str, bytes]]:
    if not ids:
        raise HTTPException(400, "Elegí al menos un archivo")
    try:
        return [drive.obtener_archivo(i) for i in ids[:20]]
    except drive.DriveError as e:
        _drive_error(e)


@router.post("/api/drive/preview")
def drive_preview(body: DriveSel, u: Usuario = Depends(acceso)):
    return _preview(_bajar(body.ids), body.tipo, body.mapeo)


@router.post("/api/drive/lotes")
def drive_crear(body: DriveSel, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return _crear(db, u, _bajar(body.ids), body.tipo, body.nombre, body.mapeo, body.ids)


@router.post("/api/drive/informar")
def drive_informar(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Sube a Drive los despachados que falten (por si falló algún 'Finalizar despacho')."""
    try:
        return {"ok": True, "resultado": drive.enviar_despachados(db, _paquetes_abiertos_y_hoy(db), u)}
    except drive.DriveError as e:
        _drive_error(e)


def _paquetes_abiertos_y_hoy(db: Session) -> list[Paquete]:
    from sqlalchemy import or_
    lotes = db.scalars(select(Lote).where(or_(Lote.fecha == hoy(), Lote.cerrado.is_(False)))).all()
    return [p for l in lotes for p in l.paquetes]


@router.get("/api/estado")
def estado(u: Usuario = Depends(acceso), db: Session = Depends(get_db), lote: int | None = None):
    lotes = svc.lotes_abiertos(db)
    # también mostrar lotes cerrados hoy
    cerrados_hoy = db.scalars(select(Lote).where(Lote.fecha == hoy(), Lote.cerrado.is_(True))).all()
    lj = []
    todos = list(lotes) + list(cerrados_hoy)
    prog = svc.progreso_lotes(db, [l.id for l in todos])
    for l in todos:
        h, t = prog.get(l.id, (0, 0))
        lj.append({"id": l.id, "tipo": l.tipo, "tipo_nombre": TIPOS_LOTE.get(l.tipo, l.tipo), "nombre": l.nombre,
                   "fecha": l.fecha.strftime("%d/%m"), "hoy": l.fecha == hoy(), "cerrado": l.cerrado,
                   "hechos": h, "total": t,
                   "creado_por": l.creado_por.nombre if l.creado_por else "",
                   "hora": l.creado_at.strftime("%H:%M")})
    ids = [l.id for l in lotes] if lote is None else [lote]
    paquetes = []
    if ids:
        paquetes = [svc.paquete_json(p) for p in db.scalars(
            select(Paquete).where(Paquete.lote_id.in_(ids)).order_by(Paquete.escaneado_at.desc().nulls_first(),
                                                                    Paquete.id)).all()]
    t = svc.tanda_activa(db, u)
    tanda = None
    if t:
        tanda = {"id": t.id, "inicio": t.inicio.strftime("%H:%M"),
                 "cantidad": db.scalar(select(func.count()).where(Paquete.tanda_id == t.id)) or 0}
    tareas = [tarea_json(x) for x in tareas_de_seccion(db, "despacho")]
    sin_drive = svc.sin_drive(db)
    return {"lotes": lj, "paquetes": paquetes, "tanda": tanda, "tareas": tareas, "usuario": u.nombre,
            "es_admin": u.es_admin, "drive": drive.configurado(), "sin_drive": sin_drive}


class Escaneo(BaseModel):
    codigo: str


@router.post("/api/scan")
def scan(body: Escaneo, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return svc.escanear(db, u, body.codigo)


class Marcar(BaseModel):
    despachado: bool


@router.post("/api/paquetes/{pid}/marcar")
def marcar(pid: int, body: Marcar, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    p = db.get(Paquete, pid)
    if not p:
        raise HTTPException(404, "Paquete inexistente")
    if not body.despachado and not u.es_admin and p.escaneado_por_id != u.id:
        raise SinPermiso()
    svc.marcar_manual(db, u, p, body.despachado)
    return {"ok": True, "paquete": svc.paquete_json(p)}


@router.post("/api/tanda/iniciar")
def tanda_iniciar(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    t = svc.tanda_activa(db, u)
    if not t:
        t = Tanda(usuario_id=u.id)
        db.add(t)
        registrar(db, u, "tanda_inicio", "", "despacho", commit=False)
        db.commit()
    return {"ok": True, "id": t.id}


@router.post("/api/tanda/finalizar")
def tanda_finalizar(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    t = svc.tanda_activa(db, u)
    if not t:
        raise HTTPException(400, "No hay un despacho iniciado")
    t.fin = ahora()
    registrar(db, u, "tanda_fin", f"{len(t.paquetes)} paquetes", "despacho", commit=False)
    db.commit()
    res = {"ok": True, "id": t.id, "cantidad": len(t.paquetes), "drive": None, "drive_error": ""}
    if drive.configurado() and t.paquetes:
        try:
            res["drive"] = drive.enviar_despachados(db, list(t.paquetes), u)
        except drive.DriveError as e:
            res["drive_error"] = str(e)
            registrar(db, u, "drive_error", str(e)[:300], "despacho")
    return res


def _xlsx(filas: list[dict], nombre: str) -> StreamingResponse:
    wb = Workbook()
    ws = wb.active
    ws.title = "Despacho"
    if filas:
        ws.append(list(filas[0].keys()))
        for f in filas:
            ws.append(list(f.values()))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@router.get("/tanda/{tid}.xlsx")
def tanda_excel(tid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    t = db.get(Tanda, tid)
    if not t or (t.usuario_id != u.id and not u.es_admin):
        raise HTTPException(404)
    filas = [{"Paquetes Despachados": i + 1, "Numero de Etiqueta": p.tracking, "Nombre de la Persona": p.comprador,
              "Cantidad de Prendas": p.cantidad, "SKU Despachados": p.sku or p.producto}
             for i, p in enumerate(sorted(t.paquetes, key=lambda p: p.escaneado_at or ahora()))]
    return _xlsx(filas, f"Despacho_{t.inicio.strftime('%Y-%m-%d_%H-%M')}.xlsx")


@router.get("/lotes/{lid}.xlsx")
def lote_excel(lid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    l = db.get(Lote, lid)
    if not l:
        raise HTTPException(404)
    filas = [{"Tracking": p.tracking, "Venta": p.venta_id, "Comprador": p.comprador, "Producto": p.producto,
              "SKU": p.sku, "Cantidad": p.cantidad, "Estado": "Despachado" if p.estado == "despachado" else "Pendiente",
              "Hora": p.escaneado_at.strftime("%d/%m %H:%M") if p.escaneado_at else "",
              "Operario": p.escaneado_por.nombre if p.escaneado_por else ""} for p in l.paquetes]
    return _xlsx(filas, f"lote_{l.id}_{l.tipo}_{l.fecha.isoformat()}.xlsx")


@router.post("/api/lotes/{lid}/cerrar")
def cerrar_lote(lid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    l = db.get(Lote, lid)
    if not l:
        raise HTTPException(404)
    l.cerrado = True
    h, t = svc.progreso_lote(db, l.id)
    registrar(db, u, "lote_cerrado", f"{l.nombre} · {h}/{t} despachados", "despacho", commit=False)
    db.commit()
    return {"ok": True}


@router.post("/api/lotes/{lid}/reabrir")
def reabrir_lote(lid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    l = db.get(Lote, lid)
    if not l:
        raise HTTPException(404)
    l.cerrado = False
    db.commit()
    return {"ok": True}


@router.post("/api/lotes/{lid}/borrar")
def borrar_lote(lid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not u.es_admin:
        raise SinPermiso()
    l = db.get(Lote, lid)
    if not l:
        raise HTTPException(404)
    from ..models import Tarea
    for t in db.scalars(select(Tarea).where(Tarea.ref == f"lote:{l.id}")).all():
        db.delete(t)
    registrar(db, u, "lote_borrado", l.nombre, "despacho", commit=False)
    db.delete(l)
    db.commit()
    return {"ok": True}


# ─────────────── Mercado Libre vía ParkaHub: semáforo por escaneo y cotejo ───────────────
@router.get("/api/ml/estado")
def ml_estado(u: Usuario = Depends(acceso)):
    return {"conectado": parkahub.disponible(), "fuente": parkahub.fuente()}


@router.get("/api/ml/verificar")
def ml_verificar(codigo: str, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Lo llama el scanner después de cada lectura (Flex/Colecta). Si ParkaHub no está, el scanner sigue igual."""
    if not parkahub.disponible():
        return {"ok": False, "sin_conexion": True}
    try:
        v = parkahub.verificar_envio(codigo)
    except parkahub.ParkaHubError as e:
        return {"ok": False, "error": str(e)}
    if v["veredicto"] in ("red", "yellow"):
        registrar(db, u, "escaneo_frenado_ml", f"{v['codigo']} · {v['mensaje']} · {v.get('motivo') or ''}", "despacho")
    return {"ok": True, **v}


class CotejoIn(BaseModel):
    tipo: str
    paquetes: list[dict]
    refrescar: bool = False


@router.post("/api/ml/cotejo")
def ml_cotejo(body: CotejoIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    try:
        d = parkahub.listos_ml(body.tipo, body.refrescar)
    except parkahub.ParkaHubError as e:
        raise HTTPException(400, str(e)) from None
    r = parkahub.cotejar(body.paquetes[:3000], d.get("ready") or [], d.get("states") or {})
    r["frescura"] = d.get("fresh") or {}
    registrar(db, u, "cotejo_ml", f"{body.tipo} · coinciden {r['coinciden']} · faltan {len(r['solo_ml'])} · sobran {len(r['solo_lista'])}", "despacho")
    return r


# ─────────────── Despacho en vivo (ML Flex / Colecta y Tienda Nube, sin Drive) ───────────────
TIPOS_VIVO = ("flex", "colecta", "tiendanube", "cambios")


def _tipo_vivo(tipo: str) -> str:
    if tipo not in TIPOS_VIVO:
        raise HTTPException(400, "Tipo inválido (flex, colecta, tiendanube o cambios)")
    return tipo


def _escaneos_hoy(db: Session, tipo: str) -> dict[str, DespachoEscaneo]:
    """Lo que ya salió hoy de este tipo (cualquier dispositivo), por nº de envío y por nº de venta."""
    regs = db.scalars(select(DespachoEscaneo).where(DespachoEscaneo.fecha == hoy(), DespachoEscaneo.tipo == tipo)).all()
    idx: dict[str, DespachoEscaneo] = {}
    for r in regs:
        idx[r.tracking] = r
        if r.venta:
            idx.setdefault("v:" + r.venta, r)
    return idx


def _ya_escaneado(idx: dict, p: dict):
    return idx.get(str(p.get("trackingId") or "")) or (p.get("ventaId") and idx.get("v:" + str(p["ventaId"]))) or None


@router.get("/api/vivo")
def vivo(tipo: str, refrescar: bool = False, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Lista del día para el scanner: ML "Listas para despachar" de hoy (Flex o Colecta) o TN "Por enviar".
    Cada paquete viene con `escaneado` (ya salió hoy, desde este u otro dispositivo) para mostrarlo en verde
    y que "Cargar" traiga solo los nuevos."""
    _tipo_vivo(tipo)
    try:
        d = ventas_api.despacho(tipo, refrescar)
        idx = _escaneos_hoy(db, tipo)
        paquetes = []
        for p in d["paquetes"]:
            p = dict(p)
            r = _ya_escaneado(idx, p)
            p["escaneado"] = bool(r)
            if r:
                p["escaneadoHora"] = r.at.strftime("%H:%M") if r.at else ""
            paquetes.append(p)
        d = dict(d, paquetes=paquetes, escaneados=sum(1 for p in paquetes if p["escaneado"]))
        return {"ok": True, **d}
    except (meli.MeliError, tn.TNError, CambiosError) as e:
        from ..services import cambios as cam
        conectado = (tn.configurado() if tipo == "tiendanube" else cam.configurado() if tipo == "cambios"
                     else meli.configurado())
        return {"ok": False, "conectado": conectado, "error": str(e), "fecha": hoy().isoformat()}


@router.get("/api/vivo/canceladas")
def vivo_canceladas(tipo: str, u: Usuario = Depends(acceso)):
    """Canceladas recientes del tipo (consulta liviana, cada minuto mientras hay una lista abierta)."""
    _tipo_vivo(tipo)
    if tipo == "cambios":
        from ..services import cambios as cam
        try:
            lista = cam.para_busqueda(refrescar=False)[1] if cam.configurado() else []
        except cam.CambiosError as e:
            return {"ok": False, "error": str(e), "canceladas": []}
        return {"ok": True, "fecha": hoy().isoformat(), "canceladas": [ventas_api._para_scanner(p) for p in lista]}
    fuente = "tn" if tipo == "tiendanube" else "ml"
    try:
        lista = ventas_api.canceladas(fuente)
    except (meli.MeliError, tn.TNError, CambiosError) as e:
        return {"ok": False, "error": str(e), "canceladas": []}
    if fuente == "ml":
        lista = [p for p in lista if p.get("tipo") == tipo]
        if tipo == "flex":
            lista = lista + ventas_api.flex_tn()[1]
    return {"ok": True, "fecha": hoy().isoformat(), "canceladas": [ventas_api._para_scanner(p) for p in lista]}


@router.get("/api/vivo/envio")
def vivo_envio(codigo: str, tipo: str, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Etiqueta escaneada que no está en la lista del día: qué es en ML / TN (o la suma si entró recién)."""
    _tipo_vivo(tipo)
    idx = _escaneos_hoy(db, tipo)
    cod = str(codigo or "").strip().lstrip("#")
    ya = idx.get(cod) or idx.get("v:" + cod)
    if ya:
        return {"ok": True, "motivo": "ya_escaneada",
                "mensaje": f"Ya se despachó hoy a las {ya.at.strftime('%H:%M') if ya.at else '?'} — no se vuelve a cargar"}
    try:
        r = ventas_api.explicar_envio(codigo, tipo)
    except (meli.MeliError, tn.TNError, CambiosError) as e:
        return {"ok": False, "error": str(e)}
    if r.get("motivo") == "agregar" and r.get("paquete") and _ya_escaneado(idx, r["paquete"]):
        return {"ok": True, "motivo": "ya_escaneada", "mensaje": "Ya se despachó hoy — no se vuelve a cargar"}
    if r["motivo"] not in ("agregar",):
        registrar(db, u, "escaneo_fuera_de_lista", f"{tipo} · {codigo} · {r['mensaje']}", "despacho")
    return {"ok": True, **r}


class AvisoCancelada(BaseModel):
    tipo: str
    tracking: str
    venta: str = ""
    comprador: str = ""
    ya_escaneada: bool = False


@router.post("/api/vivo/cancelada-vista")
def vivo_cancelada_vista(body: AvisoCancelada, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """El scanner avisa que una venta de su lista se canceló: queda en la actividad (y tarea si ya estaba escaneada)."""
    det = f"{body.tipo} · envío {body.tracking} · venta {body.venta} · {body.comprador}"
    registrar(db, u, "venta_cancelada_despacho", det + (" · YA ESTABA ESCANEADA" if body.ya_escaneada else ""), "despacho")
    if body.ya_escaneada:
        from ..models import Tarea
        ref = f"cancelada:{body.tracking}"[:60]
        if not db.scalars(select(Tarea).where(Tarea.ref == ref)).first():
            db.add(Tarea(seccion_slug="despacho", origen="auto", ref=ref, prioridad="alta", vence=hoy(),
                         titulo=f"Sacar del despacho: venta cancelada {body.venta or body.tracking}"[:200],
                         detalle=f"Se canceló después de escanearla. {det}"))
            db.commit()
    return {"ok": True}


class EscaneoVivo(BaseModel):
    tipo: str
    tracking: str
    venta: str = ""
    comprador: str = ""


@router.post("/api/vivo/escaneado")
def vivo_escaneado(body: EscaneoVivo, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """El scanner avisa que un envío de la lista en vivo salió (escaneado o "Marcar salida"). Queda registrado
    por día y tipo: la lista del día lo muestra en verde y no se vuelve a cargar."""
    _tipo_vivo(body.tipo)
    tr = str(body.tracking or "").strip().lstrip("#")[:40]
    if not tr:
        raise HTTPException(400, "Falta el nº de envío")
    q = select(DespachoEscaneo).where(DespachoEscaneo.fecha == hoy(), DespachoEscaneo.tipo == body.tipo,
                                      DespachoEscaneo.tracking == tr)
    if not db.scalars(q).first():
        db.add(DespachoEscaneo(fecha=hoy(), tipo=body.tipo, tracking=tr, venta=str(body.venta or "")[:40],
                               comprador=str(body.comprador or "")[:160], usuario_id=u.id))
        db.commit()
        if body.tipo == "cambios":
            return _cambio_despachado(db, u, tr, body)
    return {"ok": True}


def _cambio_despachado(db: Session, u: Usuario, tr: str, body) -> dict:
    """Salió un cambio en el scanner → en la app de cambios queda "Despachado" (si está activado en Conexiones)."""
    from ..services import cambios as cam
    registrar(db, u, "cambio_despachado", f"{tr} · orden {body.venta or '-'} · {body.comprador or ''} (scanner)", "despacho")
    if not cam.marcar_activo(db):
        return {"ok": True}
    try:
        cam.marcar(tr, "Despachado")
        return {"ok": True, "cambio": "Despachado"}
    except cam.CambiosError as e:
        registrar(db, u, "cambio_despachado_error", f"{tr}: {e}"[:200], "despacho")
        return {"ok": True, "cambio_error": str(e)}


@router.post("/api/vivo/escaneado/quitar")
def vivo_escaneado_quitar(body: EscaneoVivo, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Se desmarcó una salida (o era una cancelada que se sacó del despacho): vuelve a figurar pendiente."""
    _tipo_vivo(body.tipo)
    tr = str(body.tracking or "").strip().lstrip("#")
    for r in db.scalars(select(DespachoEscaneo).where(DespachoEscaneo.fecha == hoy(), DespachoEscaneo.tipo == body.tipo,
                                                      DespachoEscaneo.tracking == tr)).all():
        db.delete(r)
    db.commit()
    return {"ok": True}


# ─────────────── Cierre del día por mail (botones "Cierre" del scanner) ───────────────
@router.post("/api/cierre-mail")
def cierre_mail(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """El scanner ya no le pide el cierre al Apps Script viejo: sale el cierre nuevo de DEPOSITO PARKA."""
    try:
        return drive.cerrar_dia(db, u)
    except drive.DriveError as e:
        raise HTTPException(502, str(e))


# ─────────────── Aviso por WhatsApp: totales despachados hoy ───────────────
class WhatsappDespacho(BaseModel):
    numero: str
    nombre: str = ""


@router.get("/api/whatsapp/resumen")
def whatsapp_resumen(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Totales de lo que salió hoy (registro compartido del despacho en vivo) + a quién se avisa."""
    from ..services.deposito_drive import es_tn_flex
    totales = {t: 0 for t in TIPOS_VIVO}
    totales["flex_tn"] = 0  # lo de Tienda Nube que salió en Flex se cuenta aparte
    for r in db.scalars(select(DespachoEscaneo).where(DespachoEscaneo.fecha == hoy())).all():
        t = "flex_tn" if r.tipo == "flex" and es_tn_flex(r.tracking) else r.tipo
        if t in totales:
            totales[t] += 1
    return {"ok": True, "fecha": hoy().isoformat(), "totales": totales, "total": sum(totales.values()),
            "numero": ajustes.leer("wa_despacho_numero", db), "nombre": ajustes.leer("wa_despacho_nombre", db),
            "puede_editar": u.es_admin}


@router.post("/api/whatsapp/destino")
def whatsapp_destino(body: WhatsappDespacho, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not u.es_admin:
        raise HTTPException(403, "Solo un admin puede cambiar el número")
    num = "".join(ch for ch in body.numero if ch.isdigit())
    if len(num) < 10 or len(num) > 15:
        raise HTTPException(400, "Número inválido: con código de país, ej. 5491156053000")
    ajustes.guardar(db, "wa_despacho_numero", num)
    if body.nombre.strip():
        ajustes.guardar(db, "wa_despacho_nombre", body.nombre.strip()[:60])
    return {"ok": True, "numero": num}
