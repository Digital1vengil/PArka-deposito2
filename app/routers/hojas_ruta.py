"""Hojas de ruta: el depósito las arma (sección "Hojas de ruta") y el chofer las recorre en la app PARKA Reparto,
que es un proyecto aparte (Worker de Cloudflare) y entra por /api/reparto con:
  - cabecera X-Reparto-Clave = clave compartida (Conexiones → App del chofer), que guarda el Worker de Reparto;
  - cabecera Authorization: Bearer <token> que devuelve /api/reparto/login (nombre + PIN de un usuario Chofer).
"""
import hmac
from datetime import date
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..auth import intentar_login, requiere
from ..db import get_db
from ..models import HojaRuta, ParadaRuta, Usuario
from ..services import ajustes, odoo, rotulos
from ..services import hojas_ruta as svc
from ..templating import render

router = APIRouter()
acceso = requiere("hojas_ruta")
TOKEN_HORAS = 16


def _hoja(db: Session, hid: int) -> HojaRuta:
    h = db.get(HojaRuta, hid)
    if not h:
        raise HTTPException(404, "No existe esa hoja de ruta")
    return h


def _choferes(db: Session) -> list[Usuario]:
    return list(db.scalars(select(Usuario).where(Usuario.rol == "chofer", Usuario.activo.is_(True)).order_by(Usuario.nombre)))


# ───────────────────────── depósito ─────────────────────────
@router.get("/hojas-ruta")
def pagina(request: Request, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return render(request, "hojas_ruta.html", u=u, conectado=odoo.configurado(db),
                  choferes=[{"id": c.id, "nombre": c.nombre} for c in _choferes(db)],
                  reparto=bool((ajustes.leer("reparto_clave", db) or "").strip()))


@router.get("/hojas-ruta/api/pedidos")
def pedidos(refrescar: bool = False, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Pedidos abiertos de Magontex para armar la hoja, con el destino que tendrían (expreso o cliente)."""
    try:
        peds = rotulos.listar(db, refrescar)
    except odoo.OdooError as e:
        raise HTTPException(502, str(e)) from e
    en_hoja = svc.pedidos_en_hojas_abiertas(db, [p["id"] for p in peds])
    out = []
    for p in peds:
        c = p.get("cliente") or {}
        propio = svc.es_propio(c.get("expreso", "")) or not c.get("expreso_id")
        out.append({"id": p["id"], "nombre": p["nombre"], "origen": p["origen"], "tipo": p["tipo"],
                    "estado_txt": p["estado_txt"], "bultos": p["bultos"], "cliente": c.get("nombre", ""),
                    "codigo": c.get("codigo", ""), "expreso": c.get("expreso", ""), "localidad": c.get("localidad", ""),
                    "destino": "Cliente (entrega directa)" if propio else f"Expreso {c.get('expreso', '')}",
                    "sin_cliente": not c, "hoja": en_hoja.get(p["id"]), "impreso": bool(p.get("impreso"))})
    return {"pedidos": out}


class HojaIn(BaseModel):
    chofer_id: int
    pedidos: list[int]
    bultos: dict[int, int] = {}
    fecha: date | None = None
    nota: str = ""


@router.post("/hojas-ruta/api/crear")
def crear(body: HojaIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    try:
        h = svc.crear_hoja(db, u, body.chofer_id, body.pedidos, body.bultos, body.fecha, body.nota.strip())
    except svc.HojaError as e:
        raise HTTPException(400, str(e)) from e
    except odoo.OdooError as e:
        raise HTTPException(502, str(e)) from e
    rotulos._CACHE.update(t=0.0)
    return svc.detalle(db, h)


@router.get("/hojas-ruta/api/lista")
def lista(abiertas: bool = True, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return {"hojas": svc.listar(db, abiertas)}


@router.get("/hojas-ruta/api/{hid}")
def ver(hid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return svc.detalle(db, _hoja(db, hid))


class OrdenIn(BaseModel):
    paradas: list[int]


@router.post("/hojas-ruta/api/{hid}/orden")
def orden(hid: int, body: OrdenIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    h = _hoja(db, hid)
    try:
        svc.reordenar(db, h, body.paradas)
    except svc.HojaError as e:
        raise HTTPException(400, str(e)) from e
    return svc.detalle(db, h)


@router.post("/hojas-ruta/api/{hid}/cerrar")
def cerrar(hid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    h = _hoja(db, hid)
    svc.cerrar(db, u, h)
    return svc.detalle(db, h)


@router.post("/hojas-ruta/api/{hid}/borrar")
def borrar(hid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    try:
        svc.borrar(db, u, _hoja(db, hid))
    except svc.HojaError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@router.get("/hojas-ruta/{hid}/imprimir.html")
def imprimir(hid: int, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Hoja de ruta en A4 (respaldo en papel para el chofer, con lugar para firma en cada parada)."""
    d, e = svc.detalle(db, _hoja(db, hid)), escape
    filas = []
    for p in d["paradas"]:
        cls = "".join(f"<tr><td>{e(c['nombre'])} <small>{e(c['codigo'])}</small></td><td>"
                      + "<br>".join(f"{e(x['origen'] or x['nombre'])} · {len(x['bultos'])} bulto{'s' if len(x['bultos']) != 1 else ''}"
                                    for x in c["pedidos"]) + f"</td><td class=n>{c['bultos']}</td></tr>" for c in p["clientes"])
        filas.append(f"""<section><h2>{p['orden']}. {e(p['nombre'])} <small>{'Expreso' if p['tipo'] == 'expreso' else 'Cliente'}</small></h2>
<p>{e(p['direccion'])}{' · ' + e(p['localidad']) if p['localidad'] else ''}{' · Tel. ' + e(p['telefono']) if p['telefono'] else ''}</p>
<table><tr><th>Cliente</th><th>Pedidos</th><th class=n>Bultos</th></tr>{cls}</table>
<div class=f><span>Recibió (nombre y firma): ______________________________</span><span>Hora: ________</span></div></section>""")
    html = f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Hoja de ruta {d['id']}</title><style>
@page{{size:A4;margin:12mm}}body{{font-family:Arial,sans-serif;font-size:11pt;color:#000}}h1{{font-size:16pt;margin:0 0 2mm}}
section{{border:1px solid #000;padding:3mm;margin-bottom:4mm;break-inside:avoid}}h2{{font-size:13pt;margin:0}}h2 small{{font-weight:400;font-size:9pt}}
p{{margin:1mm 0 2mm}}table{{width:100%;border-collapse:collapse}}td,th{{border-top:1px solid #999;padding:1mm;text-align:left;vertical-align:top}}
.n{{text-align:right;width:18mm}}.f{{display:flex;justify-content:space-between;margin-top:4mm;font-size:10pt}}</style></head><body>
<h1>Hoja de ruta n.º {d['id']} · {e(d['fecha'])}</h1><p>Chofer: <b>{e(d['chofer'])}</b> · {len(d['paradas'])} paradas · {d['bultos']} bultos{' · ' + e(d['nota']) if d['nota'] else ''}</p>
{''.join(filas)}<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),300))</script></body></html>"""
    return HTMLResponse(html)


# ───────────────────────── API de la app del chofer (PARKA Reparto) ─────────────────────────
def _serializador() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="parka-reparto")


def reparto_habilitado(request: Request, db: Session = Depends(get_db)) -> None:
    clave = (ajustes.leer("reparto_clave", db) or "").strip()
    if not clave:
        raise HTTPException(503, "La app del chofer no está habilitada (Conexiones → App del chofer)")
    if not hmac.compare_digest(request.headers.get("x-reparto-clave", ""), clave):
        raise HTTPException(401, "Clave de la app del chofer incorrecta")


def chofer_actual(request: Request, _: None = Depends(reparto_habilitado), db: Session = Depends(get_db)) -> Usuario:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    try:
        uid = _serializador().loads(token, max_age=TOKEN_HORAS * 3600)
    except SignatureExpired as e:
        raise HTTPException(401, "La sesión venció: volvé a entrar") from e
    except BadSignature as e:
        raise HTTPException(401, "Entrá con tu nombre y PIN") from e
    u = db.get(Usuario, uid)
    if not u or not u.activo or u.rol != "chofer":
        raise HTTPException(401, "Usuario sin acceso a la app del chofer")
    return u


def _hoja_chofer(db: Session, chofer: Usuario, hid: int) -> HojaRuta:
    h = db.get(HojaRuta, hid)
    if not h or h.chofer_id != chofer.id:
        raise HTTPException(404, "No tenés esa hoja de ruta")
    return h


@router.get("/api/reparto/choferes")
def reparto_choferes(_: None = Depends(reparto_habilitado), db: Session = Depends(get_db)):
    return {"choferes": [{"id": c.id, "nombre": c.nombre} for c in _choferes(db)]}


class LoginIn(BaseModel):
    usuario_id: int
    pin: str


@router.post("/api/reparto/login")
def reparto_login(body: LoginIn, _: None = Depends(reparto_habilitado), db: Session = Depends(get_db)):
    u = db.get(Usuario, body.usuario_id)
    if not u or u.rol != "chofer":
        raise HTTPException(401, "Elegí tu nombre de la lista")
    ok, msg = intentar_login(db, u, body.pin.strip())
    if not ok:
        raise HTTPException(401, msg)
    return {"token": _serializador().dumps(u.id), "nombre": u.nombre, "horas": TOKEN_HORAS}


@router.get("/api/reparto/hojas")
def reparto_hojas(chofer: Usuario = Depends(chofer_actual), db: Session = Depends(get_db)):
    return {"chofer": chofer.nombre, "hojas": svc.hojas_de_chofer(db, chofer)}


@router.get("/api/reparto/hojas/{hid}")
def reparto_hoja(hid: int, chofer: Usuario = Depends(chofer_actual), db: Session = Depends(get_db)):
    return svc.detalle(db, _hoja_chofer(db, chofer, hid))


class EscaneoIn(BaseModel):
    parada_id: int
    codigo: str


@router.post("/api/reparto/hojas/{hid}/escanear")
def reparto_escanear(hid: int, body: EscaneoIn, chofer: Usuario = Depends(chofer_actual), db: Session = Depends(get_db)):
    return svc.escanear(db, chofer, _hoja_chofer(db, chofer, hid), body.parada_id, body.codigo)


class CerrarParadaIn(BaseModel):
    nota: str = ""


@router.post("/api/reparto/hojas/{hid}/paradas/{pid}/cerrar")
def reparto_cerrar_parada(hid: int, pid: int, body: CerrarParadaIn = CerrarParadaIn(),
                          chofer: Usuario = Depends(chofer_actual), db: Session = Depends(get_db)):
    h = _hoja_chofer(db, chofer, hid)
    p = db.get(ParadaRuta, pid)
    if not p or p.hoja_id != h.id:
        raise HTTPException(404, "Esa parada no es de esta hoja")
    return svc.cerrar_parada(db, chofer, h, p, body.nota.strip())
