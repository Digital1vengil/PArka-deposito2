"""Hojas de ruta del chofer (las arma el depósito; el chofer las recorre en la app PARKA Reparto).

Cada pedido de Odoo (Magontex: Recolectar / Órdenes de entrega) va a una parada:
  - expreso PROPIO (o sin expreso) → parada en la dirección del cliente;
  - cualquier otro expreso → parada en el depósito del expreso (dirección del Transportista en Odoo), con todos
    los clientes de ese expreso juntos.
Los datos se copian al armar la hoja (la app del chofer no consulta Odoo). Cada bulto ("3 de 7") se entrega
escaneando el QR de su rótulo (…/b/<pedido>-<bulto>-<total>).
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..db import ahora, hoy
from ..models import BultoRuta, HojaRuta, ParadaRuta, Usuario
from . import odoo, rotulos

SECCION = "hojas_ruta"


class HojaError(Exception):
    pass


def es_propio(expreso: str) -> bool:
    return rotulos._norm(expreso) in ("", "propio", "sin expreso")


def _localidad(d: dict) -> str:
    loc = " ".join(x for x in (d.get("localidad", ""), f"(CP {d['cp']})" if d.get("cp") else "") if x)
    return ", ".join(x for x in (loc, d.get("provincia", "")) if x)


def armar_paradas(pedidos: list[dict], bultos: dict[int, int] | None = None) -> list[dict]:
    """Pedidos (de rotulos.leer_pedidos) → paradas con sus bultos. Expresos primero, después clientes directos."""
    bultos = bultos or {}
    paradas: dict[tuple, dict] = {}
    for p in pedidos:
        cli = p.get("cliente")
        if not cli:
            continue
        if es_propio(cli.get("expreso", "")) or not cli.get("expreso_id"):
            clave = ("cliente", cli["id"])
            datos = {"tipo": "cliente", "ref_id": cli["id"], "nombre": cli["nombre"], "direccion": cli.get("calle", ""),
                     "localidad": _localidad(cli), "telefono": cli.get("telefono", "")}
        else:
            ed = cli.get("expreso_dir") or {}
            clave = ("expreso", cli["expreso_id"])
            datos = {"tipo": "expreso", "ref_id": cli["expreso_id"], "nombre": cli["expreso"],
                     "direccion": ed.get("calle", ""), "localidad": _localidad(ed), "telefono": ""}
        par = paradas.setdefault(clave, {**datos, "bultos": []})
        total = max(1, min(99, int(bultos.get(p["id"]) or p.get("bultos") or 1)))
        for i in range(1, total + 1):
            par["bultos"].append({"picking_id": p["id"], "picking_nombre": p["nombre"], "origen": p.get("origen", ""),
                                  "cliente_id": cli["id"], "cliente_nombre": cli["nombre"], "cliente_codigo": cli["codigo"],
                                  "bulto": i, "total": total})
    return sorted(paradas.values(), key=lambda x: (x["tipo"] != "expreso", rotulos._norm(x["nombre"])))


def pedidos_en_hojas_abiertas(db: Session, ids: list[int] | None = None) -> dict[int, int]:
    """Pedido → id de la hoja de ruta abierta en la que ya está."""
    q = (select(BultoRuta.picking_id, BultoRuta.hoja_id).join(HojaRuta, HojaRuta.id == BultoRuta.hoja_id)
         .where(HojaRuta.estado == "abierta").distinct())
    if ids is not None:
        q = q.where(BultoRuta.picking_id.in_(ids))
    return {pid: hid for pid, hid in db.execute(q).all()}


def crear_hoja(db: Session, u: Usuario, chofer_id: int, pedidos_ids: list[int], bultos: dict[int, int] | None = None,
               fecha: date | None = None, nota: str = "") -> HojaRuta:
    chofer = db.get(Usuario, chofer_id)
    if not chofer or not chofer.activo or chofer.rol != "chofer":
        raise HojaError("Elegí un chofer (usuario activo con rol Chofer)")
    ids = sorted({int(i) for i in pedidos_ids})
    if not ids:
        raise HojaError("Elegí al menos un pedido")
    ya = pedidos_en_hojas_abiertas(db, ids)
    if ya:
        raise HojaError(f"Hay pedidos que ya están en otra hoja de ruta abierta (n.º {', '.join(str(h) for h in sorted(set(ya.values())))})")
    peds = rotulos.leer_pedidos(odoo.cliente(db), [["id", "in", ids]] + rotulos.DOMINIO_BASE, limite=len(ids))
    faltan = set(ids) - {p["id"] for p in peds if p.get("cliente")}
    if faltan:
        raise HojaError(f"{len(faltan)} pedido(s) no se encontraron en Odoo o no tienen cliente")
    paradas = armar_paradas(peds, bultos)
    hoja = HojaRuta(fecha=fecha or hoy(), chofer_id=chofer.id, nota=nota[:300], creada_por=u.id)
    db.add(hoja)
    db.flush()
    for orden, par in enumerate(paradas, start=1):
        pr = ParadaRuta(hoja_id=hoja.id, orden=orden, tipo=par["tipo"], ref_id=par["ref_id"], nombre=par["nombre"][:160],
                        direccion=par["direccion"][:250], localidad=par["localidad"][:160], telefono=par["telefono"][:80])
        db.add(pr)
        db.flush()
        for b in par["bultos"]:
            db.add(BultoRuta(hoja_id=hoja.id, parada_id=pr.id, picking_id=b["picking_id"],
                             picking_nombre=b["picking_nombre"][:40], origen=b["origen"][:40], cliente_id=b["cliente_id"],
                             cliente_nombre=b["cliente_nombre"][:160], cliente_codigo=b["cliente_codigo"][:20],
                             bulto=b["bulto"], total=b["total"]))
    n = sum(len(p["bultos"]) for p in paradas)
    registrar(db, u, "hoja de ruta creada", f"Hoja {hoja.id} · {chofer.nombre} · {len(paradas)} paradas · {n} bultos",
              seccion=SECCION, commit=False)
    db.commit()
    return hoja


def _paradas(db: Session, hoja_id: int) -> list[ParadaRuta]:
    return list(db.scalars(select(ParadaRuta).where(ParadaRuta.hoja_id == hoja_id).order_by(ParadaRuta.orden, ParadaRuta.id)))


def detalle(db: Session, hoja: HojaRuta) -> dict:
    """Hoja con sus paradas, y en cada parada los clientes con sus pedidos y bultos."""
    bultos = list(db.scalars(select(BultoRuta).where(BultoRuta.hoja_id == hoja.id)
                             .order_by(BultoRuta.cliente_nombre, BultoRuta.picking_id, BultoRuta.bulto)))
    por_parada: dict[int, list[BultoRuta]] = {}
    for b in bultos:
        por_parada.setdefault(b.parada_id, []).append(b)
    paradas = []
    for p in _paradas(db, hoja.id):
        bs = por_parada.get(p.id, [])
        clientes: dict[int, dict] = {}
        for b in bs:
            c = clientes.setdefault(b.cliente_id, {"id": b.cliente_id, "nombre": b.cliente_nombre,
                                                    "codigo": b.cliente_codigo, "pedidos": {}})
            ped = c["pedidos"].setdefault(b.picking_id, {"id": b.picking_id, "nombre": b.picking_nombre,
                                                         "origen": b.origen, "total": b.total, "bultos": []})
            ped["bultos"].append({"bulto": b.bulto, "entregado": b.entregado_at is not None,
                                  "at": b.entregado_at.strftime("%H:%M") if b.entregado_at else ""})
        cls = []
        for c in clientes.values():
            peds = list(c["pedidos"].values())
            for ped in peds:
                ped["entregados"] = sum(1 for x in ped["bultos"] if x["entregado"])
            cls.append({**c, "pedidos": peds, "bultos": sum(len(x["bultos"]) for x in peds),
                        "entregados": sum(x["entregados"] for x in peds)})
        paradas.append({"id": p.id, "orden": p.orden, "tipo": p.tipo, "nombre": p.nombre, "direccion": p.direccion,
                        "localidad": p.localidad, "telefono": p.telefono, "estado": p.estado, "nota": p.nota,
                        "cerrada": p.cerrada_at.strftime("%H:%M") if p.cerrada_at else "", "clientes": cls,
                        "bultos": len(bs), "entregados": sum(1 for b in bs if b.entregado_at)})
    chofer = db.get(Usuario, hoja.chofer_id)
    return {"id": hoja.id, "fecha": hoja.fecha.strftime("%d/%m/%Y"), "fecha_iso": hoja.fecha.isoformat(),
            "estado": hoja.estado, "nota": hoja.nota, "chofer_id": hoja.chofer_id, "chofer": chofer.nombre if chofer else "",
            "paradas": paradas, "bultos": len(bultos), "entregados": sum(1 for b in bultos if b.entregado_at),
            "paradas_hechas": sum(1 for p in paradas if p["estado"] != "pendiente")}


def resumen(db: Session, hoja: HojaRuta, nombres: dict[int, str] | None = None) -> dict:
    tot, ent = db.execute(select(func.count(BultoRuta.id), func.count(BultoRuta.entregado_at))
                          .where(BultoRuta.hoja_id == hoja.id)).one()
    npar, hechas = db.execute(select(func.count(ParadaRuta.id), func.count(ParadaRuta.cerrada_at))
                              .where(ParadaRuta.hoja_id == hoja.id)).one()
    nombres = nombres if nombres is not None else dict(db.execute(select(Usuario.id, Usuario.nombre)).all())
    return {"id": hoja.id, "fecha": hoja.fecha.strftime("%d/%m/%Y"), "estado": hoja.estado,
            "chofer": nombres.get(hoja.chofer_id, ""), "nota": hoja.nota, "bultos": tot or 0, "entregados": ent or 0,
            "paradas": npar or 0, "paradas_hechas": hechas or 0}


def listar(db: Session, abiertas: bool = True, limite: int = 60) -> list[dict]:
    q = select(HojaRuta).order_by(HojaRuta.fecha.desc(), HojaRuta.id.desc()).limit(limite)
    if abiertas:
        q = q.where(HojaRuta.estado == "abierta")
    nombres = dict(db.execute(select(Usuario.id, Usuario.nombre)).all())
    return [resumen(db, h, nombres) for h in db.scalars(q)]


def hojas_de_chofer(db: Session, chofer: Usuario) -> list[dict]:
    hs = db.scalars(select(HojaRuta).where(HojaRuta.chofer_id == chofer.id, HojaRuta.estado == "abierta")
                    .order_by(HojaRuta.fecha, HojaRuta.id))
    return [resumen(db, h) for h in hs]


def reordenar(db: Session, hoja: HojaRuta, parada_ids: list[int]) -> None:
    paradas = {p.id: p for p in _paradas(db, hoja.id)}
    if set(parada_ids) != set(paradas):
        raise HojaError("El orden no coincide con las paradas de la hoja")
    for i, pid in enumerate(parada_ids, start=1):
        paradas[pid].orden = i
    db.commit()


def cerrar(db: Session, u: Usuario, hoja: HojaRuta) -> None:
    hoja.estado, hoja.cerrada_at = "cerrada", ahora()
    r = resumen(db, hoja)
    registrar(db, u, "hoja de ruta cerrada", f"Hoja {hoja.id} · {r['entregados']}/{r['bultos']} bultos entregados",
              seccion=SECCION, commit=False)
    db.commit()


def borrar(db: Session, u: Usuario, hoja: HojaRuta) -> None:
    if db.scalar(select(func.count(BultoRuta.id)).where(BultoRuta.hoja_id == hoja.id, BultoRuta.entregado_at.is_not(None))):
        raise HojaError("La hoja ya tiene bultos entregados: cerrala en vez de borrarla")
    for modelo in (BultoRuta, ParadaRuta):
        for x in db.scalars(select(modelo).where(modelo.hoja_id == hoja.id)):
            db.delete(x)
    db.flush()
    db.delete(hoja)
    registrar(db, u, "hoja de ruta borrada", f"Hoja {hoja.id}", seccion=SECCION, commit=False)
    db.commit()


# ───────────────────────── chofer (app PARKA Reparto) ─────────────────────────
def escanear(db: Session, chofer: Usuario, hoja: HojaRuta, parada_id: int, texto: str) -> dict:
    """El chofer escanea el QR de un rótulo en una parada. Devuelve {ok, tipo, mensaje, bulto}."""
    if hoja.estado != "abierta":
        return {"ok": False, "tipo": "cerrada", "mensaje": "La hoja de ruta está cerrada"}
    cod = rotulos.leer_codigo_bulto(texto)
    if not cod:
        return {"ok": False, "tipo": "invalido", "mensaje": "No es el QR de un rótulo de bulto"}
    pid, nb, total = cod
    b = db.scalar(select(BultoRuta).where(BultoRuta.hoja_id == hoja.id, BultoRuta.picking_id == pid, BultoRuta.bulto == nb))
    if not b:
        return {"ok": False, "tipo": "no_esta", "mensaje": f"El bulto {nb} de {total} no está en esta hoja de ruta"}
    info = {"cliente": b.cliente_nombre, "pedido": b.origen or b.picking_nombre, "bulto": b.bulto, "total": b.total}
    if b.parada_id != parada_id:
        otra = db.get(ParadaRuta, b.parada_id)
        return {"ok": False, "tipo": "otra_parada", "bulto": info,
                "mensaje": f"Este bulto es de otra parada: {otra.orden}. {otra.nombre}" if otra else "Este bulto es de otra parada"}
    if b.entregado_at:
        return {"ok": False, "tipo": "ya", "bulto": info, "mensaje": f"Ya estaba entregado ({b.entregado_at:%H:%M})"}
    b.entregado_at, b.entregado_por = ahora(), chofer.id
    db.commit()
    return {"ok": True, "tipo": "ok", "bulto": info, "mensaje": f"{b.cliente_nombre} · bulto {b.bulto} de {b.total}"}


def cerrar_parada(db: Session, chofer: Usuario, hoja: HojaRuta, parada: ParadaRuta, nota: str = "") -> dict:
    bs = list(db.scalars(select(BultoRuta).where(BultoRuta.parada_id == parada.id)))
    faltan = sum(1 for b in bs if not b.entregado_at)
    parada.estado = "incompleta" if faltan else "entregada"
    parada.nota, parada.cerrada_at = nota[:300], ahora()
    registrar(db, chofer, "parada entregada" if not faltan else "parada incompleta",
              f"Hoja {hoja.id} · {parada.nombre} · {len(bs) - faltan}/{len(bs)} bultos" + (f" · {nota[:120]}" if nota else ""),
              seccion=SECCION, commit=False)
    db.commit()
    return {"estado": parada.estado, "faltan": faltan}
