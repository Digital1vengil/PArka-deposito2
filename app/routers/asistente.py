"""Sección Asistente (Gemini) + pantalla Conexiones (ParkaHub y Gemini, solo admin)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..actividad import registrar
from ..auth import requiere, solo_admin
from ..db import get_db
from ..models import Usuario
from ..services import ajustes, asistente, parkahub
from ..templating import render

router = APIRouter()
acceso = requiere("asistente")

SUGERENCIAS = [
    "¿Cómo viene el día? Resumen de despacho, devoluciones y tareas",
    "¿Qué preguntas de compradores hay sin responder? Proponé respuestas",
    "Buscá la venta 2000000000000 y decime qué lleva y si se puede despachar",
    "¿Qué SKUs se devolvieron más en los últimos 30 días y por qué motivo?",
    "¿Cómo está la reputación en Mercado Libre?",
]


@router.get("/asistente")
def pagina(request: Request, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return render(request, "asistente.html", u=u, sugerencias=SUGERENCIAS,
                  gemini_ok=asistente.configurado(), parkahub_ok=parkahub.disponible())


@router.get("/asistente/api/historial")
def ver_historial(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    return {"mensajes": asistente.historial(db, u)}


class MensajeIn(BaseModel):
    texto: str


@router.post("/asistente/api/mensaje")
def mensaje(body: MensajeIn, u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    try:
        r = asistente.responder(db, u, body.texto)
    except asistente.AsistenteError as e:
        raise HTTPException(400, str(e)) from None
    registrar(db, u, "asistente_consulta", body.texto[:120], seccion="asistente")
    return r


@router.post("/asistente/api/borrar")
def borrar(u: Usuario = Depends(acceso), db: Session = Depends(get_db)):
    asistente.borrar_historial(db, u)
    return {"ok": True}


# ─────────────── Conexiones (solo admin) ───────────────
@router.get("/admin/conexiones")
def conexiones(request: Request, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    return render(request, "admin_conexiones.html", u=u, aj=ajustes.resumen(db))


class AjustesIn(BaseModel):
    valores: dict[str, str]


@router.post("/admin/api/conexiones")
def guardar(body: AjustesIn, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    cambiados = []
    for k, v in body.valores.items():
        if k not in ajustes.CLAVES:
            raise HTTPException(400, f"ajuste desconocido: {k}")
        v = (v or "").strip()
        if ajustes.CLAVES[k][2] and (not v or v.startswith("•")):
            continue  # secreto sin cambios (se muestra enmascarado)
        ajustes.guardar(db, k, v)
        cambiados.append(k)
    if any(k.startswith("despachos_") for k in cambiados):
        from ..services import despachos
        despachos.olvidar()
    if any(k.startswith("cambios_") for k in cambiados):
        from ..services import cambios
        cambios.olvidar()
    if any(k.startswith("meli_") for k in cambiados):
        from ..services import meli
        meli.olvidar_cache()
    if any(k.startswith("sp_") for k in cambiados):
        from ..services import smartpost
        smartpost.olvidar()
    registrar(db, u, "conexiones_guardadas", ", ".join(cambiados))
    return {"ok": True, "guardados": cambiados, "aj": ajustes.resumen(db)}


@router.get("/admin/api/conexiones/meli/perfil")
def meli_perfil(u: Usuario = Depends(solo_admin)):
    """Rutas de ML que la clave cargada puede leer / escribir (lo que devuelve el proxy de Costos)."""
    from ..services import meli
    try:
        return {"ok": True, "perfil": meli.perfil()}
    except meli.MeliError as e:
        return {"ok": False, "mensaje": str(e)}


@router.get("/admin/api/conexiones/meli/crudo")
def meli_crudo(path: str = "/users/me", u: Usuario = Depends(solo_admin)):
    """Diagnóstico: la respuesta tal cual del proxy de Costos para una ruta de lectura."""
    from ..services import meli
    try:
        return {"ok": True, "path": path, "respuesta": meli.crudo(path)}
    except meli.MeliError as e:
        return {"ok": False, "mensaje": str(e), "status": e.status}


@router.get("/admin/api/conexiones/smartpost/guia")
def smartpost_guia(codigo: str, u: Usuario = Depends(solo_admin)):
    """Probar con una etiqueta real: qué devuelve SmartPost para ese nº de guía y a qué orden de TN corresponde."""
    from ..services import smartpost, ventas_api
    from ..services import tiendanube as tn
    codigo = (codigo or "").strip()
    if not codigo:
        raise HTTPException(400, "Escribí o escaneá un nº de guía")
    if smartpost.panel_configurado():
        try:
            pg = smartpost.guia_en_panel(codigo)
        except smartpost.SPError as e:
            return {"ok": False, "mensaje": str(e)}
        if not pg:
            return {"ok": True, "encontrada": False,
                    "mensaje": f"La guía {codigo} no está en Mis Pedidos ni en las guías de hoy del panel de SmartPost"}
        return {"ok": True, "encontrada": True, "guia": pg["guia"], "estado": pg["estado"],
                "nombre": pg.get("destinatario", ""), "remitos": [pg["remito"]] if pg.get("remito") else [],
                "textos": [], "orden": pg.get("orden", ""), "via": "panel" if pg.get("orden") else "",
                "aviso_tn": "" if pg.get("orden") else "Guía sin orden de Tienda Nube (entra igual en Despacho si es de hoy)",
                "crudo": pg}
    try:
        g = smartpost.leer_guia(codigo)
    except smartpost.SPError as e:
        return {"ok": False, "mensaje": str(e)}
    if not g["encontrada"]:
        return {"ok": True, "encontrada": False, "mensaje": f"SmartPost no encontró la guía {codigo}"}
    por_enviar, aviso_tn = [], ""
    try:
        por_enviar = ventas_api.tn_por_enviar(refrescar=False) if tn.configurado() else []
    except tn.TNError as e:
        aviso_tn = str(e)
    smartpost.olvidar()
    m = smartpost.orden_de_guia(codigo, por_enviar)
    return {"ok": True, "encontrada": True, "guia": g["guia"], "estado": g["estado"], "nombre": g["nombre"],
            "remitos": g["remitos"], "textos": g["textos"], "orden": m["numero"], "via": m["via"],
            "aviso_tn": aviso_tn, "crudo": g["crudo"]}


@router.post("/admin/api/conexiones/probar/{que}")
def probar(que: str, u: Usuario = Depends(solo_admin), db: Session = Depends(get_db)):
    if que == "parkahub":
        try:
            r = parkahub.probar()
        except parkahub.ParkaHubError as e:
            return {"ok": False, "mensaje": str(e)}
        return {"ok": True, "mensaje": "Conectado a ParkaHub" + (" · Mercado Libre conectado" if r["ml_conectado"]
                                                                  else " · ojo: ML aparece desconectado en ParkaHub")}
    if que == "meli":
        from ..services import meli
        try:
            r = meli.probar()
        except meli.MeliError as e:
            return {"ok": False, "mensaje": str(e)}
        return {"ok": True, "mensaje": f"Conectado a Mercado Libre · cuenta {r['cuenta'] or '?'} (id {r['id']})"
                + (f" · perfil \"{r['perfil']}\"" if r["perfil"] else "")
                + ("" if not parkahub.configurado() else " · ojo: con ParkaHub cargado se usa ParkaHub primero")}
    if que == "cambios":
        from ..services import cambios
        try:
            r = cambios.probar()
        except cambios.CambiosError as e:
            return {"ok": False, "mensaje": str(e)}
        return {"ok": True, "mensaje": f"Conectado a la app de cambios · {r['total']} cambios · {r['a_preparar']} para preparar / despachar"}
    if que == "smartpost":
        from ..services import smartpost
        partes, ok = [], True
        if smartpost.configurado():
            try:
                r = smartpost.probar()
                partes.append("API ✓" + (f" (cliente {r['cliente']})" if r["cliente"] else ""))
            except smartpost.SPError as e:
                ok = False
                partes.append(f"API: {e}")
        if smartpost.panel_configurado():
            try:
                r = smartpost.probar_panel()
                partes.append(f"Panel ✓ ({r['pedidos']} pedidos de Tienda Nube leídos, último #{r['ultimo'] or '-'})")
            except smartpost.SPError as e:
                ok = False
                partes.append(f"Panel: {e}")
        else:
            partes.append("Panel: falta usuario y contraseña")
        return {"ok": ok, "mensaje": " · ".join(partes)}
    if que == "tiendanube":
        from ..services import tiendanube as tn
        try:
            r = tn.probar()
        except tn.TNError as e:
            return {"ok": False, "mensaje": str(e)}
        return {"ok": True, "mensaje": f"Conectado a Tienda Nube · {r['tienda']} (id {r['id']}) · "
                f"{r['pendientes']} órdenes pagadas sin enviar"}
    if que == "gemini":
        try:
            asistente._llamar({"contents": [{"role": "user", "parts": [{"text": "Respondé solo: ok"}]}]})
        except asistente.AsistenteError as e:
            return {"ok": False, "mensaje": str(e)}
        return {"ok": True, "mensaje": f"Gemini responde ({ajustes.leer('gemini_modelo', db)})"}
    if que == "odoo":
        from ..services import odoo
        try:
            r = odoo.probar(db)
        except odoo.OdooError as e:
            return {"ok": False, "mensaje": str(e)}
        return {"ok": True, "mensaje": f"Conectado a Odoo · {r['con_codigo']} productos con código de barras"}
    if que == "rollo":
        from ..services import etiquetas_rollo as rollo
        imps = rollo.listar_impresoras()
        imp = rollo.impresora_configurada(db)
        if not imp:
            return {"ok": False, "mensaje": "No hay impresora cargada ni se encontró una Honeywell en esta PC"
                    + (f" (impresoras: {', '.join(imps)})" if imps else "")}
        if rollo._es_red(imp):
            return {"ok": True, "mensaje": f"Impresora de red {imp} (se prueba al imprimir)"}
        if imps and imp not in imps:
            return {"ok": False, "mensaje": f'"{imp}" no está en esta PC. Disponibles: {", ".join(imps)}'}
        return {"ok": True, "mensaje": f"Lista: {imp}" + ("" if imps else " (no se pudo listar las impresoras de Windows)")}
    if que == "ecommerce":
        from ..services import re_drive
        try:
            r = re_drive.estado(db)
        except re_drive.REDriveError as e:
            return {"ok": False, "mensaje": str(e)}
        return {"ok": True, "mensaje": f"Conectado · carpeta \"Recolección Ecommerce\" (Apps Script {r.get('version', '')})"}
    raise HTTPException(404)
