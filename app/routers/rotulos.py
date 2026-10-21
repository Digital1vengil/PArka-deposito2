"""Rótulos de entrega (pedidos de Magontex en Odoo) y Códigos de clientes (código de barras + QR por cliente)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere
from ..db import get_db
from ..models import Usuario
from ..services import odoo
from ..services import rotulos as svc
from ..templating import render

router = APIRouter()
acceso_rot = requiere("rotulos")
acceso_cli = requiere("clientes")


def url_base(request: Request) -> str:
    """URL pública de la app (detrás del Worker de Cloudflare llega en X-Forwarded-Host)."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if not host.startswith(("localhost", "127.0.0.1", "testserver")):
        proto = "https"
    return f"{proto}://{host}"


def _odoo(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except odoo.OdooError as e:
        raise HTTPException(502, str(e)) from e


# ───────────────────────── Rótulos de entrega ─────────────────────────
@router.get("/rotulos")
def pagina_rotulos(request: Request, u: Usuario = Depends(acceso_rot), db: Session = Depends(get_db)):
    return render(request, "rotulos.html", u=u, conectado=odoo.configurado(db))


@router.get("/rotulos/api/lista")
def lista_rotulos(refrescar: bool = False, todos: bool = False, u: Usuario = Depends(acceso_rot), db: Session = Depends(get_db)):
    peds = _odoo(svc.listar, db, refrescar, todos)
    return {"pedidos": peds, "total": len(peds), "todos": todos,
            "nuevos": sum(1 for p in peds if p["abierto"] and not p["impreso"])}


def _rotulos_pedido(request: Request, db: Session, pid: int, bultos: int) -> tuple[dict, list[dict]]:
    p = _odoo(svc.pedido, db, pid)
    if not p:
        raise HTTPException(404, "No se encontró el pedido en Odoo")
    if not p["cliente"]:
        raise HTTPException(400, f"{p['nombre']} no tiene cliente en Odoo")
    cli = p["cliente"]
    lista = svc.lista_rotulos(p, cli, svc.remitente(db), bultos or p["bultos"],
                              f"{url_base(request)}/clientes/{cli['id']}", svc.fecha_de(p), url_base(request))
    return p, lista


@router.get("/rotulos/{pid}/rotulo.html")
def rotulo_html(pid: int, request: Request, bultos: int = 0, prueba: bool = False,
                u: Usuario = Depends(acceso_rot), db: Session = Depends(get_db)):
    """Rótulos 10x15 (uno por bulto) con el diálogo de impresión. ?prueba=1 saca solo el primero y no cuenta."""
    p, lista = _rotulos_pedido(request, db, pid, bultos)
    if prueba:
        lista = lista[:1]
    else:
        svc.registrar_impresion(db, p["id"], p["nombre"], u.id, len(lista))
        registrar(db, u, "imprimió rótulos", f"{len(lista)} rótulo(s) de {p['nombre']} ({p['origen']})", seccion="rotulos")
    return HTMLResponse(svc.build_html(lista, f"Rótulo {p['nombre']}", arriba=svc.config(db)["arriba"]))


@router.get("/rotulos/{pid}/rotulo.zpl")
def rotulo_zpl(pid: int, request: Request, bultos: int = 0, u: Usuario = Depends(acceso_rot), db: Session = Depends(get_db)):
    p, lista = _rotulos_pedido(request, db, pid, bultos)
    svc.registrar_impresion(db, p["id"], p["nombre"], u.id, len(lista))
    nombre = p["nombre"].replace("/", "-")
    return Response(svc.build_zpl(lista, svc.config(db)).encode("utf-8"), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="rotulo_{nombre}.zpl"'})


@router.get("/b/{codigo}")
def abrir_bulto(codigo: str, u: Usuario = Depends(acceso_cli), db: Session = Depends(get_db)):
    """QR de un bulto escaneado con la cámara común del celular → ficha del cliente de ese pedido."""
    b = svc.leer_codigo_bulto(codigo)
    if not b:
        raise HTTPException(404, "Código de bulto inválido")
    p = _odoo(svc.pedido, db, b[0])
    if not p or not p["cliente"]:
        raise HTTPException(404, "No se encontró el pedido en Odoo")
    return RedirectResponse(f"/clientes/{p['cliente']['id']}", status_code=303)


# ───────────────────────── Códigos de clientes ─────────────────────────
@router.get("/clientes")
def pagina_clientes(request: Request, u: Usuario = Depends(acceso_cli), db: Session = Depends(get_db)):
    return render(request, "clientes.html", u=u, conectado=odoo.configurado(db))


@router.get("/clientes/api/lista")
def lista_clientes(refrescar: bool = False, u: Usuario = Depends(acceso_cli), db: Session = Depends(get_db)):
    cls = _odoo(svc.listar_clientes, db, refrescar)
    return {"clientes": cls, "total": len(cls)}


@router.get("/clientes/codigos.html")
def planilla_codigos(request: Request, ids: str = "", u: Usuario = Depends(acceso_cli), db: Session = Depends(get_db)):
    """Planilla A4 (3 x 8) con código de barras y QR de los clientes elegidos (sin ?ids, todos)."""
    cls = _odoo(svc.listar_clientes, db)
    elegidos = {int(x) for x in ids.split(",") if x.strip().isdigit()}
    if elegidos:
        cls = [c for c in cls if c["id"] in elegidos]
    if not cls:
        raise HTTPException(400, "No hay clientes para imprimir")
    return HTMLResponse(svc.build_planilla(cls, url_base(request)))


def _ficha(db: Session, pid: int) -> dict:
    cli = _odoo(svc.ficha_cliente, db, pid)
    if not cli:
        raise HTTPException(404, "No se encontró el cliente en Odoo")
    return cli


@router.get("/clientes/{pid}")
def ficha(pid: int, request: Request, u: Usuario = Depends(acceso_cli), db: Session = Depends(get_db)):
    cli = _ficha(db, pid)
    url = f"{url_base(request)}/clientes/{cli['id']}"
    return render(request, "cliente_ficha.html", u=u, c=cli, barcode=svc.barcode_svg(cli["codigo"], 16, 0.35),
                  qr=svc.qr_svg(url, 34))


def _rotulos_cliente(request: Request, db: Session, pid: int, bultos: int, entrega: int) -> tuple[dict, list[dict]]:
    cli = _ficha(db, pid)
    ent = next((e for e in cli["entregas"] if e["id"] == entrega), None)
    if ent:
        cli = {**cli, **{k: ent[k] for k in ("calle", "localidad", "cp", "provincia")}}
    return cli, svc.lista_rotulos(None, cli, svc.remitente(db), bultos, f"{url_base(request)}/clientes/{cli['id']}")


@router.get("/clientes/{pid}/rotulo.html")
def rotulo_cliente_html(pid: int, request: Request, bultos: int = 1, entrega: int = 0,
                        u: Usuario = Depends(acceso_cli), db: Session = Depends(get_db)):
    cli, lista = _rotulos_cliente(request, db, pid, bultos, entrega)
    registrar(db, u, "imprimió rótulos", f"{len(lista)} rótulo(s) de {cli['nombre']} ({cli['codigo']})", seccion="clientes")
    return HTMLResponse(svc.build_html(lista, f"Rótulo {cli['codigo']}", arriba=svc.config(db)["arriba"]))


@router.get("/clientes/{pid}/rotulo.zpl")
def rotulo_cliente_zpl(pid: int, request: Request, bultos: int = 1, entrega: int = 0,
                       u: Usuario = Depends(acceso_cli), db: Session = Depends(get_db)):
    cli, lista = _rotulos_cliente(request, db, pid, bultos, entrega)
    return Response(svc.build_zpl(lista, svc.config(db)).encode("utf-8"), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="rotulo_{cli["codigo"]}.zpl"'})
