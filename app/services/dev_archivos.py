"""Los 4 archivos de control de una devolución ecommerce (ML + TN), uno por carpeta de Drive.

Se arman al cerrar la devolución en los racks (paso 2: última prenda guardada o "Marcar como devuelto") y también
con "Guardar en Drive sin devolver" (ahí solo sale el historial, todo Pendiente). Van a
DEPOSITO PARKA/Plantilla credito odoo/:

  01_Cantidades_Netas/    CANT_DEV_AAAA-MM-DD_PD-<id>.xlsx   SKU · Descripcion_Articulo · Cantidad_Total_Devuelta
  02_Detalle_Por_Venta/   DET_DEV_AAAA-MM-DD_PD-<id>.xlsx    Fecha_Devolucion · Plataforma · Numero_Venta_Orden · SKU · Descripcion · Cantidad
  03_Historial_Recepciones/ HIST_DEV_AAAA-MM-DD_PD-<id>.xlsx Fecha · Devolucion · Plataforma · Numero_Venta_Orden · SKU · Estado
  04_Carga_Odoo/          IN_DEV_AAAA-MM-DD_PD-<id>.xlsx     plantilla del IN de Odoo (services/in_odoo.py)

Reglas (las mismas del IN, así los 4 cuadran entre sí):
- Solo OPTIMO. FALLA va a scrap (otro circuito) y AJENO no es nuestro: no figuran en ninguno.
- "Devuelto" = unidad escaneada en su rack en el paso 2 que cruza con una fila OPTIMO del paso 1 (PD + SKU) y tiene
  plataforma ML/TN. Es lo que entra al IN, al detalle y a las cantidades netas:
  suma(01) = suma(02) = filas(04) = filas "Devuelto" de 03.
- "Pendiente" = unidad OPTIMO del paso 1 que todavía no se guardó en un rack, o que se guardó pero no se puede
  exportar (sin SKU / sin plataforma): queda para revisar y no entra a Odoo.
- Valores fijos, encabezados exactos, sin filas vacías ni totales. Fechas y códigos como texto.
"""
from __future__ import annotations

import io
import logging
from collections import OrderedDict
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Font

from ..models import Devolucion
from . import in_odoo

log = logging.getLogger("parka.dev_archivos")

PLATAFORMA_NOMBRE = {"ML": "Mercado Libre", "TN": "Tienda Nube"}

# clave del Apps Script (CARPETAS_DEV en deposito-drive.gs 2026.10.22-1) → prefijo del archivo, hoja, encabezados
ARCHIVOS = OrderedDict([
    ("dev_01_cantidades", ("CANT_DEV", "CANTIDADES NETAS",
                           ["SKU", "Descripcion_Articulo", "Cantidad_Total_Devuelta"], [34, 40, 22])),
    ("dev_02_detalle", ("DET_DEV", "DETALLE POR VENTA",
                        ["Fecha_Devolucion", "Plataforma", "Numero_Venta_Orden", "SKU", "Descripcion", "Cantidad"],
                        [16, 14, 22, 34, 40, 10])),
    ("dev_03_historial", ("HIST_DEV", "HISTORIAL RECEPCIONES",
                          ["Fecha", "Devolucion", "Plataforma", "Numero_Venta_Orden", "SKU", "Estado"],
                          [12, 12, 14, 22, 34, 11])),
    ("dev_04_odoo", ("IN_DEV", None, None, None)),   # lo arma in_odoo.excel
])
CARPETA_RESPALDO = in_odoo.CARPETA   # si el Apps Script todavía no tiene las subcarpetas: todo a "Plantilla credito odoo"


def _fecha_dev(d: Devolucion) -> date:
    """Fecha de la devolución (paso 1); si no está cargada, la del paso 2."""
    return d.fecha or in_odoo.fecha_paso2(d)


def _desc(articulo: str, color: str, talle: str) -> str:
    return " ".join(x for x in (articulo, color, talle) if x)


def nombre(d: Devolucion, clave: str, fecha: date | None = None) -> str:
    if clave == "dev_04_odoo":
        return in_odoo.nombre_archivo(d, fecha)
    return f"{ARCHIVOS[clave][0]}_{(fecha or in_odoo.fecha_paso2(d)).isoformat()}_PD-{d.id}.xlsx"


def analizar(d: Devolucion) -> tuple[list[dict], list[dict], list[str]]:
    """(devueltos, pendientes, avisos). Una entrada por unidad."""
    devueltos, avisos = in_odoo.filas(d)
    pd = f"PD-{d.id}"
    exportadas: dict[int, int] = {}
    for e in devueltos:
        exportadas[id(e["_item"])] = exportadas.get(id(e["_item"]), 0) + 1
    pendientes = []
    for i in d.items:
        if i.estado != "OPTIMO":
            continue
        faltan = max(int(i.cantidad or 0) - exportadas.get(id(i), 0), 0)
        plat = (i.plataforma or d.plataforma or "").strip().upper()
        for _ in range(faltan):
            pendientes.append({"fecha": _fecha_dev(d), "devolucion": pd, "plataforma": plat if plat in in_odoo.PLATAFORMAS else "",
                               "venta": (i.venta or "").strip(), "sku": (i.sku or "").strip()})
    return devueltos, pendientes, avisos


def _libro(hoja: str, encabezados: list[str], anchos: list[int], filas: list[list], texto: tuple[str, ...]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = hoja
    ws.append(encabezados)
    for c in ws[1]:
        c.font = Font(bold=True)
    for f in filas:
        ws.append(f)
        for col in texto:   # fechas, n° de venta y códigos como texto (sin perder ceros ni volverse fecha/número)
            c = ws[f"{col}{ws.max_row}"]
            c.number_format = "@"
            if c.value is not None:
                c.value, c.data_type = str(c.value), "s"
    for idx, w in enumerate(anchos):
        ws.column_dimensions[chr(65 + idx)].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _orden_plat(p: str) -> int:
    return in_odoo.PLATAFORMAS.index(p) if p in in_odoo.PLATAFORMAS else len(in_odoo.PLATAFORMAS)


def archivos(d: Devolucion, fecha: date | None = None) -> tuple[list[dict], list[str]]:
    """[{clave, nombre, contenido, filas}] (solo los que tienen filas) y avisos."""
    devueltos, pendientes, avisos = analizar(d)
    fdev = _fecha_dev(d)
    out = []

    # 01 — cantidades netas por SKU (sin venta ni cliente)
    netas: dict[str, list] = {}
    for e in devueltos:
        n = netas.setdefault(e["sku"], [e["sku"], _desc(e["articulo"], e["color"], e["talle"]), 0])
        n[2] += 1
    f01 = [netas[k] for k in sorted(netas)]

    # 02 — detalle por venta: ML primero, después TN; una fila por venta + SKU
    det: dict[tuple, list] = {}
    for e in devueltos:
        k = (e["plataforma"], e["venta"], e["sku"])
        r = det.setdefault(k, [fdev.isoformat(), PLATAFORMA_NOMBRE[e["plataforma"]], e["venta"], e["sku"],
                               _desc(e["articulo"], e["color"], e["talle"]), 0])
        r[5] += 1
    f02 = [det[k] for k in sorted(det, key=lambda k: (_orden_plat(k[0]), k[1], k[2]))]

    # 03 — historial: una fila por unidad, cronológico (Devuelto con la fecha del rack, Pendiente con la del paso 1)
    hist = [((e["fecha"] or fdev), 0, _orden_plat(e["plataforma"]),
             [(e["fecha"] or fdev).isoformat(), e["devolucion"], PLATAFORMA_NOMBRE[e["plataforma"]], e["venta"],
              e["sku"], "Devuelto"]) for e in devueltos]
    hist += [(p["fecha"], 1, _orden_plat(p["plataforma"]),
              [p["fecha"].isoformat(), p["devolucion"], PLATAFORMA_NOMBRE.get(p["plataforma"], ""), p["venta"],
               p["sku"], "Pendiente"]) for p in pendientes]
    f03 = [h[3] for h in sorted(hist, key=lambda h: h[:3])]

    for clave, filas_, texto in (("dev_01_cantidades", f01, ()), ("dev_02_detalle", f02, ("A", "C")),
                                 ("dev_03_historial", f03, ("A", "D"))):
        if filas_:
            _p, hoja, enc, anchos = ARCHIVOS[clave]
            out.append({"clave": clave, "nombre": nombre(d, clave, fecha), "filas": len(filas_),
                        "contenido": _libro(hoja, enc, anchos, filas_, texto)})

    # 04 — IN de Odoo
    contenido, n, _av = in_odoo.excel(d, fecha)
    if contenido is not None:
        out.append({"clave": "dev_04_odoo", "nombre": in_odoo.nombre_archivo(d, fecha), "filas": n, "contenido": contenido})
    return out, avisos


def _subir(db, clave: str, nombre_: str, contenido: bytes) -> tuple[dict, bool]:
    """Sube a la subcarpeta; si el Apps Script publicado todavía no la conoce, a "Plantilla credito odoo"."""
    from . import re_drive
    try:
        return re_drive.guardar_deposito_archivo(db, clave, nombre_, contenido), False
    except re_drive.REDriveError as e:
        if "inválida" not in str(e) and "invalida" not in str(e):
            raise
        log.warning("Apps Script sin la carpeta %s: %s va a %s", clave, nombre_, CARPETA_RESPALDO)
        return re_drive.guardar_deposito_archivo(db, CARPETA_RESPALDO, nombre_, contenido), True


def generar_y_subir(db, d: Devolucion, usuario=None) -> dict:
    """Arma los 4 archivos y los sube cada uno a su carpeta. Nunca corta el cierre de la devolución.
    Devuelve {ok, nombre (el IN), filas (prendas del IN), avisos, url (del IN), archivos: [{clave, nombre, filas, url}]
    | error | motivo}. Las claves nombre/filas/url/motivo se mantienen como antes para la pantalla."""
    from ..actividad import registrar
    try:
        lista, avisos = archivos(d)
    except Exception as e:  # noqa: BLE001
        log.exception("No se pudieron armar los archivos de PD-%s", d.id)
        return {"ok": False, "nombre": "", "filas": 0, "avisos": [], "archivos": [],
                "error": f"No se pudieron armar los archivos de la devolución: {e}"[:300]}
    r = {"ok": True, "nombre": "", "filas": 0, "avisos": list(avisos), "url": "", "archivos": []}
    if not lista:
        r["motivo"] = "No hay prendas OPTIMO en la devolución: no se generan archivos"
        return r
    in_ = next((a for a in lista if a["clave"] == "dev_04_odoo"), None)
    if in_:
        r.update(nombre=in_["nombre"], filas=in_["filas"])
    else:
        r["motivo"] = "No hay prendas OPTIMO guardadas en rack: no se genera IN (sí el historial con lo pendiente)"
    errores, respaldo = [], False
    for a in lista:
        try:
            res, fb = _subir(db, a["clave"], a["nombre"], a["contenido"])
            respaldo |= fb
            r["archivos"].append({"clave": a["clave"], "nombre": a["nombre"], "filas": a["filas"], "url": res.get("url", "")})
            if a["clave"] == "dev_04_odoo":
                r["url"] = res.get("url", "")
        except Exception as e:  # noqa: BLE001 — se pueden bajar desde la app y reintentar
            log.warning("PD-%s: no se pudo subir %s: %s", d.id, a["nombre"], e)
            errores.append(f"{a['nombre']}: {e}")
    if respaldo:
        r["avisos"].append("El Apps Script de Drive todavía no tiene las carpetas 01–04: los archivos quedaron en "
                           "\"Plantilla credito odoo\". Publicá la versión 2026.10.22-1 del script.")
    if errores:
        r.update(ok=False, error=("No se pudo subir a Drive: " + " · ".join(errores))[:300])
    if r["archivos"]:
        try:
            registrar(db, usuario, "drive_deposito",
                      f"PD-{d.id}: {len(r['archivos'])} archivos de devolución ecommerce"
                      + (f" · IN {r['filas']} prendas" if r["filas"] else ""), "devoluciones")
        except Exception:  # noqa: BLE001
            log.warning("No se pudo registrar la actividad de PD-%s", d.id)
    return r


def zip_(d: Devolucion) -> tuple[bytes | None, str]:
    """Los archivos de la devolución en un .zip con sus 4 carpetas (para bajarlos desde la app)."""
    import zipfile
    lista, _ = archivos(d)
    if not lista:
        return None, ""
    carpetas = {"dev_01_cantidades": "01_Cantidades_Netas", "dev_02_detalle": "02_Detalle_Por_Venta",
                "dev_03_historial": "03_Historial_Recepciones", "dev_04_odoo": "04_Carga_Odoo"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for a in lista:
            z.writestr(f"{carpetas[a['clave']]}/{a['nombre']}", a["contenido"])
    return buf.getvalue(), f"DEV_{in_odoo.fecha_paso2(d).isoformat()}_PD-{d.id}.zip"
