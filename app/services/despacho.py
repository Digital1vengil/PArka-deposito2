"""Lógica de despacho: lotes, escaneo, tandas y tareas automáticas."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..db import ahora, hoy
from ..models import Lote, Paquete, Tanda, Tarea, Usuario, TIPOS_LOTE
from ..parsers import extraer_id, codigo_comparable

SECCION = "despacho"


def crear_lote(db: Session, usuario: Usuario, tipo: str, nombre: str, paquetes: list[dict],
               drive_ids: list[str] | None = None) -> Lote:
    lote = Lote(fecha=hoy(), tipo=tipo if tipo in TIPOS_LOTE else "otro", nombre=nombre[:200],
                creado_por_id=usuario.id, drive_ids=",".join(drive_ids or []))
    db.add(lote)
    db.flush()
    for p in paquetes:
        db.add(Paquete(lote_id=lote.id, tracking=p["tracking"][:60], venta_id=(p.get("venta_id") or "")[:60],
                       comprador=(p.get("comprador") or "")[:120], producto=p.get("producto") or "",
                       sku=(p.get("sku") or "")[:200], cantidad=int(p.get("cantidad") or 1),
                       tracking_alt=(p.get("tracking_alt") or "")[:80]))
    # Tarea automática para la sección Despacho
    db.add(Tarea(seccion_slug=SECCION, origen="auto", ref=f"lote:{lote.id}",
                 titulo=f"Despachar {TIPOS_LOTE.get(lote.tipo, lote.tipo)} — {nombre[:80]} ({len(paquetes)} paquetes)",
                 vence=lote.fecha, creado_por_id=usuario.id))
    registrar(db, usuario, "lote_cargado",
              f"{TIPOS_LOTE.get(lote.tipo)} · {nombre} · {len(paquetes)} paquetes", SECCION, commit=False)
    db.commit()
    return lote


def progreso_lote(db: Session, lote_id: int) -> tuple[int, int]:
    total = db.scalar(select(func.count()).where(Paquete.lote_id == lote_id)) or 0
    hechos = db.scalar(select(func.count()).where(Paquete.lote_id == lote_id,
                                                   Paquete.estado == "despachado")) or 0
    return hechos, total


def sincronizar_tarea_lote(db: Session, lote: Lote, usuario: Usuario | None) -> None:
    """La tarea automática del lote se completa sola cuando se despacha todo (y se reabre si se deshace)."""
    t = db.scalar(select(Tarea).where(Tarea.ref == f"lote:{lote.id}"))
    if not t:
        return
    hechos, total = progreso_lote(db, lote.id)
    if total and hechos >= total and t.estado != "hecha":
        t.estado = "hecha"
        t.hecha_at = ahora()
        t.hecha_por_id = usuario.id if usuario else None
    elif hechos < total and t.estado == "hecha":
        t.estado = "en_curso"
        t.hecha_at = None
    elif 0 < hechos < total and t.estado == "pendiente":
        t.estado = "en_curso"
        if usuario and not t.asignado_a_id:
            t.asignado_a_id = usuario.id


def lotes_abiertos(db: Session, fecha: date | None = None) -> list[Lote]:
    q = select(Lote).where(Lote.cerrado.is_(False))
    if fecha:
        q = q.where(Lote.fecha == fecha)
    return list(db.scalars(q.order_by(Lote.creado_at)).all())


def tanda_activa(db: Session, usuario: Usuario) -> Tanda | None:
    return db.scalar(select(Tanda).where(Tanda.usuario_id == usuario.id, Tanda.fin.is_(None))
                     .order_by(Tanda.inicio.desc()))


def escanear(db: Session, usuario: Usuario, raw: str) -> dict:
    """Procesa un código leído. Devuelve {resultado: ok|dup|no_encontrado, ...}."""
    codigo = extraer_id(raw)
    if not codigo:
        return {"resultado": "vacio"}
    abiertos = [l.id for l in lotes_abiertos(db)]
    candidatos = []
    if abiertos:
        candidatos = list(db.scalars(select(Paquete).where(Paquete.lote_id.in_(abiertos),
                                                            Paquete.tracking == codigo)
                                     .order_by(Paquete.id)).all())
    if not candidatos and abiertos:
        # Tienda Nube: también se puede escanear el código de tracking del envío
        n = codigo_comparable(codigo)
        if len(n) >= 6:
            alt = db.scalars(select(Paquete).where(Paquete.lote_id.in_(abiertos), Paquete.tracking_alt != "")).all()
            candidatos = [p for p in alt if (t := codigo_comparable(p.tracking_alt)) and (t.startswith(n) or n.startswith(t))]
    if not candidatos:
        registrar(db, usuario, "escaneo_no_encontrado", codigo, SECCION)
        return {"resultado": "no_encontrado", "codigo": codigo}

    pendiente = next((p for p in candidatos if p.estado == "pendiente"), None)
    if pendiente is None:
        p = candidatos[0]
        registrar(db, usuario, "escaneo_dup", codigo, SECCION)
        return {"resultado": "dup", "codigo": codigo, "paquete": paquete_json(p)}

    p = pendiente
    p.estado = "despachado"
    p.escaneado_at = ahora()
    p.escaneado_por_id = usuario.id
    p.codigo_raw = raw[:300]
    t = tanda_activa(db, usuario)
    if t:
        p.tanda_id = t.id
    db.flush()
    sincronizar_tarea_lote(db, p.lote, usuario)
    registrar(db, usuario, "escaneo_ok", f"{codigo} · {p.comprador}", SECCION, commit=False)
    db.commit()
    hechos, total = progreso_lote(db, p.lote_id)
    return {"resultado": "ok", "codigo": codigo, "paquete": paquete_json(p),
            "lote": {"id": p.lote_id, "hechos": hechos, "total": total}}


def marcar_manual(db: Session, usuario: Usuario, paquete: Paquete, despachado: bool) -> None:
    if despachado and paquete.estado != "despachado":
        paquete.estado = "despachado"
        paquete.escaneado_at = ahora()
        paquete.escaneado_por_id = usuario.id
        paquete.codigo_raw = "(manual)"
        accion = "despacho_manual"
    elif not despachado and paquete.estado == "despachado":
        paquete.estado = "pendiente"
        paquete.escaneado_at = None
        paquete.escaneado_por_id = None
        paquete.tanda_id = None
        accion = "despacho_deshacer"
    else:
        return
    db.flush()
    sincronizar_tarea_lote(db, paquete.lote, usuario)
    registrar(db, usuario, accion, f"{paquete.tracking} · {paquete.comprador}", SECCION, commit=False)
    db.commit()


def paquete_json(p: Paquete) -> dict:
    return {
        "id": p.id, "tracking": p.tracking, "venta_id": p.venta_id, "comprador": p.comprador,
        "producto": p.producto, "sku": p.sku, "cantidad": p.cantidad, "estado": p.estado,
        "lote_id": p.lote_id, "tipo": p.lote.tipo if p.lote else "",
        "escaneado_at": p.escaneado_at.strftime("%H:%M") if p.escaneado_at else "",
        "escaneado_por": p.escaneado_por.nombre if p.escaneado_por else "",
    }
