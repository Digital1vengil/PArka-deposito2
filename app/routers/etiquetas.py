"""Etiquetas en rollo Honeywell (ZPL): estado de la impresora, regla de calibración y fila de prueba."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import solo_admin, usuario_actual
from ..db import get_db
from ..models import Articulo, Usuario
from ..services import etiquetas_rollo as rollo

router = APIRouter()


@router.get("/api/etiquetas/rollo/estado")
def estado(u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    imps = rollo.listar_impresoras()
    imp = rollo.impresora_configurada(db)
    cfg = rollo.config(db)
    return {"impresora": imp, "impresoras": imps, "directo": bool(imp),
            "ancho_pagina_mm": rollo.page_width_mm(cfg), "config": cfg}


def _muestra(db: Session) -> list[dict]:
    arts = db.scalars(select(Articulo).where(Articulo.codigo_barras != "", Articulo.codigo_barras.is_not(None))
                      .order_by(Articulo.id).limit(2)).all()
    if not arts:
        return [{"nombre": "PRUEBA PARKA", "sku": "1-CAMP-M-00000-BLCK-M-PARK", "codigo_barras": "2000000000000"}] * 2
    return [{"nombre": a.nombre, "sku": a.sku, "codigo_barras": a.codigo_barras} for a in arts] * (2 // len(arts))


@router.post("/admin/api/etiquetas/rollo/{que}")
def probar_impresion(que: str, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    try:
        imp = rollo.impresora_configurada(db)
        if que == "regla":
            rollo.enviar_zpl(rollo.regla_zpl(), imp, "Regla de calibración PARKA")
            return {"ok": True, "mensaje": f"Regla enviada a {imp}"}
        if que == "prueba":
            r = rollo.imprimir(_muestra(db), db, "Prueba de etiquetas PARKA", max_filas=1)
            registrar(db, u, "conexiones_guardadas", "prueba de impresora de etiquetas")
            return {"ok": True, "mensaje": f"Fila de prueba enviada a {r['impresora']}"}
    except rollo.ImpresoraError as e:
        return {"ok": False, "mensaje": str(e)}
    raise HTTPException(404)


@router.get("/admin/etiquetas/rollo/regla.html")
def regla_html(u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    return HTMLResponse(rollo.build_html(_muestra(db), rollo.config(db), "Regla de calibración", regla=True, max_filas=1))
