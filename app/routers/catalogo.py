"""API común del catálogo (artículos de Odoo con código de barras) para todas las secciones.

Cualquier usuario logueado la puede usar; sincronizar requiere admin o la función "importar_articulos".
    GET  /api/catalogo/buscar?q=…           texto, SKU o código de barras (exacto=true si fue código/SKU)
    GET  /api/catalogo/codigo/{codigo}      un código escaneado → artículo (404 si no existe ni en Odoo)
    GET  /api/catalogo/articulo/{id}
    GET  /api/catalogo/modelo?articulo=…    todas las variantes de un modelo
    POST /api/catalogo/stock {ids:[…]}      stock actual en Odoo
    GET  /api/catalogo/estado               conexión y última sincronización
    POST /api/catalogo/sincronizar
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import SinPermiso, puede, usuario_actual
from ..db import get_db
from ..models import Articulo, Usuario
from ..services import catalogo, odoo

router = APIRouter(prefix="/api/catalogo")


@router.get("/buscar")
def buscar(q: str = "", limite: int = 100, marca: str = "", con_codigo: bool = False,
           u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    res, exacto = catalogo.buscar(db, q, limite=max(1, min(limite, 300)), marca=marca, con_codigo=con_codigo)
    return {"resultados": [catalogo.a_json(a) for a in res], "exacto": exacto}


@router.get("/codigo/{codigo}")
def por_codigo(codigo: str, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    a = catalogo.por_codigo(db, codigo)
    if a is None:
        raise HTTPException(404, "Código no encontrado en el catálogo ni en Odoo")
    return catalogo.a_json(a)


@router.get("/articulo/{aid}")
def articulo(aid: int, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    a = db.get(Articulo, aid)
    if a is None:
        raise HTTPException(404, "Artículo no encontrado")
    return catalogo.a_json(a)


@router.get("/modelo")
def modelo(articulo: str, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    vs = catalogo.modelo(db, articulo)
    return {"articulo": articulo, "variantes": [catalogo.a_json(a) for a in vs]}


class StockIn(BaseModel):
    ids: list[int]


@router.post("/stock")
def stock(body: StockIn, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    arts = [a for a in (db.get(Articulo, i) for i in body.ids[:200]) if a is not None]
    try:
        return {"ok": True, "stock": catalogo.stock_en_vivo(db, arts)}
    except odoo.OdooError as e:
        return {"ok": False, "mensaje": str(e), "stock": {a.id: a.stock for a in arts}}


@router.get("/estado")
def estado(u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    return catalogo.estado(db)


@router.post("/sincronizar")
def sincronizar(u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    if not puede(db, u, "importar_articulos"):
        raise SinPermiso()
    if not odoo.configurado(db):
        raise HTTPException(400, "Falta configurar Odoo en Conexiones (usuario y API key)")
    try:
        catalogo.sincronizar_en_segundo_plano(u.nombre)
    except odoo.OdooError as e:
        raise HTTPException(400, str(e))
    registrar(db, u, "articulos_sync_odoo", "Sincronización con Odoo iniciada", "articulos")
    return {"ok": True}
