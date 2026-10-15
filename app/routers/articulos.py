"""Sección Artículos: catálogo código de barras ↔ SKU, sincronizado desde Odoo (o importado de PDF/Excel)."""
import threading

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select, func, delete
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere_seccion, SinPermiso, puede
from ..db import get_db, SessionLocal, ahora
from ..models import Articulo, Usuario
from ..parsers import articulos_desde_pdf, articulos_desde_tabla
from ..services import catalogo
from ..templating import render

router = APIRouter(prefix="/articulos")
acceso = requiere_seccion("articulos")
IMPORT = {"estado": "libre", "mensaje": "", "cantidad": 0}


def art_json(a: Articulo) -> dict:
    return catalogo.a_json(a)


@router.get("")
def pagina(request: Request, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    est = catalogo.estado(db)
    ultima = db.scalar(select(func.max(Articulo.actualizado_at)))
    return render(request, "articulos.html", u=u, total=est["total"], con_codigo=est["con_codigo"], ultima=ultima,
                  est=est)


@router.get("/sin-codigo.xlsx")
def sin_codigo(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    """Parka / Puffers / No Brand activos que en Odoo no tienen código de barras (para cargárselo allá)."""
    import io
    from fastapi.responses import Response
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from ..services.devoluciones import color_talle, es_conteo, marca
    arts = db.scalars(select(Articulo).where(Articulo.codigo_barras == "", Articulo.sku != "",
                                             Articulo.inactivo.is_not(True), es_conteo())
                      .order_by(Articulo.articulo, Articulo.sku)).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Sin código"
    ws.append(["Artículo", "Color", "Talle", "Marca", "SKU (Referencia interna)", "Categoría", "ID Odoo"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for a in arts:
        color, talle = color_talle(a)
        ws.append([a.articulo, color, talle, marca(a), a.sku, a.categoria, a.odoo_id])
    for col, ancho in zip("ABCDEFG", (24, 18, 8, 10, 34, 16, 10)):
        ws.column_dimensions[col].width = ancho
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="articulos_sin_codigo.xlsx"'})


@router.get("/api/buscar")
def buscar(q: str = "", u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    res, exacto = catalogo.buscar(db, q, limite=100)
    return {"resultados": [art_json(a) for a in res], "exacto": exacto}


def _importar(nombre: str, data: bytes, reemplazar: bool, uid: int):
    db = SessionLocal()
    try:
        IMPORT.update(estado="procesando", mensaje=f"Leyendo {nombre}… (un PDF grande tarda 1 o 2 minutos)", cantidad=0)
        if nombre.lower().endswith(".pdf"):
            arts = articulos_desde_pdf(data)
        else:
            arts = articulos_desde_tabla(nombre, data)
        if not arts:
            raise ValueError("No se encontraron artículos en el archivo")
        if reemplazar:
            db.execute(delete(Articulo))
            db.bulk_insert_mappings(Articulo, arts)
            nuevos, actualizados = len(arts), 0
        else:
            por_codigo = {a.codigo_barras: a for a in db.scalars(select(Articulo).where(Articulo.codigo_barras != ""))}
            por_sku = {a.sku: a for a in db.scalars(select(Articulo).where(Articulo.sku != ""))}
            nuevos = actualizados = 0
            for d in arts:
                x = (d["codigo_barras"] and por_codigo.get(d["codigo_barras"])) or (d["sku"] and por_sku.get(d["sku"]))
                if x:
                    for k, v in d.items():
                        setattr(x, k, v)
                    x.actualizado_at = ahora()
                    actualizados += 1
                else:
                    db.add(Articulo(**d))
                    nuevos += 1
        db.commit()
        registrar(db, db.get(Usuario, uid), "articulos_importados",
                  f"{nombre}: {nuevos} nuevos, {actualizados} actualizados", "articulos")
        IMPORT.update(estado="listo", cantidad=len(arts),
                      mensaje=f"Listo: {len(arts)} artículos ({nuevos} nuevos, {actualizados} actualizados)")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        IMPORT.update(estado="error", mensaje=f"Error: {e}")
    finally:
        db.close()


@router.post("/api/importar")
async def importar(archivo: UploadFile = File(...), reemplazar: bool = Form(False),
                   u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not puede(db, u, "importar_articulos"):
        raise SinPermiso()
    if IMPORT["estado"] == "procesando":
        raise HTTPException(400, "Ya hay una importación en curso")
    data = await archivo.read()
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(400, "Archivo demasiado grande")
    IMPORT.update(estado="procesando", mensaje="Empezando…")
    threading.Thread(target=_importar, args=(archivo.filename or "archivo", data, reemplazar, u.id), daemon=True).start()
    return {"ok": True}


@router.get("/api/importar/estado")
def importar_estado(u: Usuario = Depends(acceso)):
    return IMPORT


# ─────────────── Cruce de SKU (ML / TN ↔ Odoo) ───────────────
from pydantic import BaseModel  # noqa: E402

from ..services import cruce_sku  # noqa: E402


@router.get("/cruce-sku")
def cruce_pagina(request: Request, u: Usuario = Depends(acceso)):
    return render(request, "cruce_sku.html", u=u)


@router.get("/api/cruce-sku")
def cruce_listar(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return cruce_sku.listar(db)


class CruceIn(BaseModel):
    sku: str
    articulo_id: int


@router.post("/api/cruce-sku/confirmar")
def cruce_confirmar(body: CruceIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    try:
        n = cruce_sku.confirmar(db, body.sku, body.articulo_id, u)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    registrar(db, u, "sku_vinculado", f"Cruce SKU {body.sku.upper()} → artículo {body.articulo_id}", "articulos")
    return {"ok": True, "aplicados": n}


class SkuIn(BaseModel):
    sku: str


@router.post("/api/cruce-sku/quitar")
def cruce_quitar(body: SkuIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    if not cruce_sku.quitar(db, body.sku):
        raise HTTPException(404, "Ese SKU no tenía cruce confirmado")
    registrar(db, u, "sku_vinculado", f"Cruce SKU quitado: {body.sku.upper()}", "articulos")
    return {"ok": True}


@router.post("/api/cruce-sku/automaticos")
def cruce_automaticos(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    n = cruce_sku.confirmar_automaticos(db, u)
    registrar(db, u, "sku_vinculado", f"Cruce SKU: {n} automáticos confirmados", "articulos")
    return {"ok": True, "confirmados": n}


@router.get("/cruce-sku.xlsx")
def cruce_excel(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    from fastapi.responses import Response
    return Response(cruce_sku.excel(db), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="cruce_sku.xlsx"'})


@router.post("/api/cruce-sku/importar")
async def cruce_importar(archivo: UploadFile = File(...), u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    try:
        r = cruce_sku.importar(db, await archivo.read(), u)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    registrar(db, u, "sku_vinculado", f"Cruce SKU importado: {r['confirmados']} filas", "articulos")
    return {"ok": True, **r}
