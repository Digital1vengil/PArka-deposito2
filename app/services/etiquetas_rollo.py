"""Etiquetas en rollo térmico: Honeywell PC42t Plus (203 dpi, ZPL/ZSim2), rollo 87 mm, 2 etiquetas de 40 x 25 mm por fila.

Formato según "Módulo de Etiquetas PARKA — Especificación técnica para integración" v1.1 (06/10/2026):
  - Salida A: ZPL crudo enviado a la impresora (spooler de Windows en modo RAW o TCP 9100).
  - Salida B: HTML (una página de {ancho} x 25 mm por fila) impreso por el driver de Windows.
Calibración validada en la PC42t Plus: desplazX +10 mm (^LH80,0), etiqueta derecha +8 mm.
Cada etiqueta: nombre (34 car.), SKU, Code128 (alto 11 mm, módulo 0,25 mm) y el número abajo.
"""
from __future__ import annotations

import math
import socket
import sys
from html import escape

from sqlalchemy.orm import Session

from . import ajustes

# ───────────────────────── configuración ─────────────────────────
DEFAULT_CFG = {
    "w": 40.0, "h": 25.0, "m": 2.0, "g": 3.0,   # etiqueta y liner (mm)
    "zx": 10.0,                                 # desplazX ZPL (ambas etiquetas)
    "offR": 8.0,                                # corrimiento extra etiqueta derecha
    "offX": 0.0, "offY": 0.0,                   # corrimientos solo para la impresión HTML
    "bar": 11.0,                                # alto de barras (mm)
    "max": 34,                                  # máximo de caracteres del nombre
    "darkness": 5, "speed": 3,                  # ^MD / ^PR
}

# ajuste -> clave de cfg
_MAPA = {
    "rollo_desplaz_x": ("zx", float), "rollo_desplaz_der": ("offR", float), "rollo_desplaz_y": ("offY", float),
    "rollo_html_x": ("offX", float), "rollo_oscuridad": ("darkness", int), "rollo_velocidad": ("speed", int),
}


def config(db: Session | None = None) -> dict:
    cfg = dict(DEFAULT_CFG)
    for clave, (k, tipo) in _MAPA.items():
        v = (ajustes.leer(clave, db) or "").strip().replace(",", ".")
        if v:
            try:
                cfg[k] = tipo(float(v))
            except ValueError:
                pass
    cfg["darkness"] = max(-30, min(30, int(cfg["darkness"])))
    cfg["speed"] = max(1, min(6, int(cfg["speed"])))
    return cfg


def dots(mm: float) -> int:
    return int(round(mm * 8))   # 203 dpi = 8 puntos/mm exactos


# ───────────────────────── Code 128 (B / C automático) ─────────────────────────
CODES = [
    "11011001100", "11001101100", "11001100110", "10010011000", "10010001100", "10001001100", "10011001000", "10011000100",
    "10001100100", "11001001000", "11001000100", "11000100100", "10110011100", "10011011100", "10011001110", "10111001100",
    "10011101100", "10011100110", "11001110010", "11001011100", "11001001110", "11011100100", "11001110100", "11101101110",
    "11101001100", "11100101100", "11100100110", "11101100100", "11100110100", "11100110010", "11011011000", "11011000110",
    "11000110110", "10100011000", "10001011000", "10001000110", "10110001000", "10001101000", "10001100010", "11010001000",
    "11000101000", "11000100010", "10110111000", "10110001110", "10001101110", "10111011000", "10111000110", "10001110110",
    "11101110110", "11010001110", "11000101110", "11011101000", "11011100010", "11011101110", "11101011000", "11101000110",
    "11100010110", "11101101000", "11101100010", "11100011010", "11101111010", "11001000010", "11110001010", "10100110000",
    "10100001100", "10010110000", "10010000110", "10000101100", "10000100110", "10110010000", "10110000100", "10011010000",
    "10011000010", "10000110100", "10000110010", "11000010010", "11001010000", "11110111010", "11000010100", "10001111010",
    "10100111100", "10010111100", "10010011110", "10111100100", "10011110100", "10011110010", "11110100100", "11110010100",
    "11110010010", "11011011110", "11011110110", "11110110110", "10101111000", "10100011110", "10001011110", "10111101000",
    "10111100010", "11110101000", "11110100010", "10111011110", "10111101110", "11101011110", "11110101110", "11010000100",
    "11010010000", "11010011100",
]
STOP = "1100011101011"


def code128(text: str) -> str:
    """Patrón de módulos ('1' barra, '0' espacio). Numérico → subset C (último dígito impar en B)."""
    vals: list[int] = []
    if text.isdigit() and len(text) >= 2:
        vals.append(105)
        i = 0
        while i + 1 < len(text):
            vals.append(int(text[i:i + 2]))
            i += 2
        if i < len(text):
            vals += [100, ord(text[i]) - 32]
    else:
        vals.append(104)
        for ch in text:
            c = ord(ch) - 32
            vals.append(c if 0 <= c < 95 else 0)
    s = vals[0] + sum(v * k for k, v in enumerate(vals[1:], start=1))
    vals.append(s % 103)
    return "".join(CODES[v] for v in vals) + STOP


def barcode_svg(text: str, alto_mm: float, max_ancho_mm: float) -> str:
    bits = code128(text)
    mod = 0.25 if len(bits) * 0.25 <= max_ancho_mm else 0.125
    w = len(bits) * mod
    rects, i = [], 0
    while i < len(bits):
        if bits[i] == "1":
            j = i
            while j < len(bits) and bits[j] == "1":
                j += 1
            rects.append(f'<rect x="{i * mod:.3f}" y="0" width="{(j - i) * mod:.3f}" height="{alto_mm}"/>')
            i = j
        else:
            i += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{alto_mm}mm" '
            f'viewBox="0 0 {w} {alto_mm}" shape-rendering="crispEdges">{"".join(rects)}</svg>')


# ───────────────────────── datos ─────────────────────────
def _norm(e: dict) -> dict:
    """Item de la app (nombre, sku, codigo_barras, articulo, color, talle) → {name, sku, code}."""
    nombre = (e.get("nombre") or "").strip() or " ".join(
        x for x in (e.get("articulo", ""), f"({e.get('color', '')}, {e.get('talle', '')})" if e.get("color") else "") if x)
    return {"name": nombre, "sku": (e.get("sku") or "").strip(), "code": (e.get("codigo_barras") or "").strip()}


# ───────────────────────── Salida A: ZPL ─────────────────────────
def _zclean(s) -> str:
    return str(s or "").replace("^", " ").replace("~", " ").strip()


def _zpl_modulos(bc: str) -> int:
    if bc.isdigit():
        n = len(bc)
        return 11 * (2 + n // 2 + (2 if n % 2 else 0)) + 13
    return 11 * (len(bc) + 2) + 13


def _zpl_label(x: int, it: dict, cfg: dict) -> str:
    W, h = dots(cfg["w"]), dots(cfg["bar"])
    name = _zclean(it["name"])[: cfg["max"]]
    sku, bc = _zclean(it["sku"]), _zclean(it["code"])
    z = f"^FO{x},10^FB{W},1,0,C^A0N,20,16^FD{name}^FS\n"
    if sku:
        z += f"^FO{x},34^FB{W},1,0,C^A0N,20,16^FD{sku}^FS\n"
    if bc:
        mods = _zpl_modulos(bc)
        mw = 1 if mods * 2 > W - 16 else 2
        bx = x + max(0, (W - mods * mw) // 2)
        z += (f"^BY{mw},2,{h}^FO{bx},60^BCN,{h},N,N,N,A^FD{bc}^FS\n"
              f"^FO{x},{60 + h + 8}^FB{W},1,0,C^A0N,24,22^FD{bc}^FS\n")
    return z


def build_zpl(lista: list[dict], cfg: dict | None = None, max_filas: int = 0) -> str:
    """Una etiqueta por elemento de `lista` (ya expandida por cantidad); de a 2 por fila (^XA…^XZ por fila)."""
    cfg = cfg or DEFAULT_CFG
    labels = [_norm(e) for e in lista]
    H, x1 = dots(cfg["h"]), dots(cfg["m"])
    x2 = dots(cfg["m"] + cfg["w"] + cfg["g"] + cfg["offR"])
    ox = dots(cfg["zx"])
    roll = dots(2 * cfg["m"] + 2 * cfg["w"] + cfg["g"])
    pw = min(832, max(roll, x2 + dots(cfg["w"])) + max(0, ox))
    oy = dots(cfg["offY"])
    lh = f"^LH{max(0, ox)},{max(0, oy)}" + (f"^LS{-ox}" if ox < 0 else "")
    head = f"^XA^CI28\n^PW{pw}^LL{H}{lh}^MD{cfg['darkness']}^PR{cfg['speed']}\n"
    z = []
    for r, i in enumerate(range(0, len(labels), 2)):
        if max_filas and r >= max_filas:
            break
        z.append(head + _zpl_label(x1, labels[i], cfg)
                 + (_zpl_label(x2, labels[i + 1], cfg) if i + 1 < len(labels) else "") + "^XZ\n")
    return "".join(z)


def regla_zpl() -> str:
    """Regla de calibración: marcas cada 5 mm (numeradas cada 10 mm) en todo el ancho del cabezal (832 pt)."""
    z = "^XA^CI28^PW832^LL200^LH0,0\n"
    for mm in range(0, 105, 5):
        x = dots(mm)
        if x > 830:
            break
        if mm % 10 == 0:
            z += f"^FO{x},0^GB2,60,2^FS^FO{x + 3},65^A0N,22,18^FD{mm}^FS\n"
        else:
            z += f"^FO{x},0^GB2,30,2^FS\n"
    return z + "^XZ\n"


# ───────────────────────── Salida B: HTML (driver de Windows) ─────────────────────────
def page_width_mm(cfg: dict) -> int:
    roll = 2 * cfg["m"] + 2 * cfg["w"] + cfg["g"]
    return math.ceil(max(roll, cfg["m"] + cfg["w"] + cfg["g"] + cfg["offX"] + cfg["offR"] + cfg["w"]))


def _n(v: float) -> str:
    return f"{round(float(v), 3):g}"


def _label_html(it: dict, left: float, cfg: dict) -> str:
    name = it["name"][: cfg["max"]]
    T = "position:absolute;left:0;right:0;white-space:nowrap;overflow:hidden"
    cod = ""
    if it["code"]:
        cod = (f'<div style="position:absolute;top:7.5mm;left:50%;transform:translateX(-50%);line-height:0">'
               f'{barcode_svg(it["code"], cfg["bar"], cfg["w"] - 2)}</div>'
               f'<div style="{T};top:{_n(7.5 + cfg["bar"] + 0.8)}mm;font-size:2.9mm;line-height:3mm">{escape(it["code"])}</div>')
    return (f'<div style="position:absolute;left:{_n(left)}mm;top:{_n(cfg["offY"])}mm;width:{_n(cfg["w"])}mm;height:{_n(cfg["h"])}mm;'
            f'text-align:center;font-family:Arial,sans-serif;color:#000;overflow:hidden">'
            f'<div style="{T};top:1.2mm;font-size:2.3mm;line-height:2.6mm">{escape(name)}</div>'
            f'<div style="{T};top:4.2mm;font-size:2.3mm;line-height:2.6mm">{escape(it["sku"])}</div>{cod}</div>')


def build_html(lista: list[dict], cfg: dict | None = None, titulo: str = "Etiquetas", auto_print: bool = True,
               regla: bool = False, max_filas: int = 0) -> str:
    cfg = cfg or DEFAULT_CFG
    labels = [_norm(e) for e in lista]
    pw = page_width_mm(cfg)
    xL = cfg["m"] + cfg["offX"]
    xR = cfg["m"] + cfg["w"] + cfg["g"] + cfg["offX"] + cfg["offR"]
    filas = []
    if regla:
        marcas = "".join(
            f'<div style="position:absolute;left:{mm}mm;top:0;width:0.2mm;height:{4 if mm % 10 == 0 else (2.5 if mm % 5 == 0 else 1.2)}mm;background:#000"></div>'
            + (f'<div style="position:absolute;left:{mm + 0.4}mm;top:4.2mm;font:2.2mm Arial">{mm}</div>' if mm % 10 == 0 else "")
            for mm in range(0, pw + 1))
        cajas = "".join(f'<div style="position:absolute;left:{_n(x)}mm;top:{_n(cfg["offY"])}mm;width:{_n(cfg["w"])}mm;height:{_n(cfg["h"])}mm;'
                        f'box-sizing:border-box;border:0.3mm solid #000"></div>' for x in (xL, xR))
        filas.append(f'<div class="fila">{marcas}{cajas}</div>')
    for r, i in enumerate(range(0, len(labels), 2)):
        if max_filas and r >= max_filas:
            break
        filas.append('<div class="fila">' + _label_html(labels[i], xL, cfg)
                     + (_label_html(labels[i + 1], xR, cfg) if i + 1 < len(labels) else "") + "</div>")
    script = "<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),150));</script>" if auto_print else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{escape(titulo)}</title><style>'
            f'@page{{size:{pw}mm {_n(cfg["h"])}mm;margin:0}}html,body{{margin:0;padding:0;background:#fff}}'
            f'.fila{{position:relative;width:{pw}mm;height:{_n(cfg["h"])}mm;overflow:hidden;break-after:page;page-break-after:always}}'
            f'.fila:last-child{{break-after:auto;page-break-after:auto}}'
            f'@media screen{{body{{background:#ddd;padding:8px}}.fila{{background:#fff;margin:0 0 6px;outline:1px dashed #999}}}}'
            f'</style></head><body>{"".join(filas)}{script}</body></html>')


# ───────────────────────── envío a la impresora ─────────────────────────
class ImpresoraError(Exception):
    pass


def _es_red(nombre: str) -> tuple[str, int] | None:
    """'192.168.0.50' o '192.168.0.50:9100' → impresora de red (TCP crudo)."""
    host, _, puerto = nombre.partition(":")
    partes = host.split(".")
    if len(partes) == 4 and all(p.isdigit() for p in partes):
        return host, int(puerto or 9100)
    if nombre.lower().startswith("tcp://"):
        h, _, p = nombre[6:].partition(":")
        return h, int(p or 9100)
    return None


def listar_impresoras() -> list[str]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class PRINTER_INFO_4(ctypes.Structure):
        _fields_ = [("pPrinterName", wintypes.LPWSTR), ("pServerName", wintypes.LPWSTR), ("Attributes", wintypes.DWORD)]

    ws = ctypes.WinDLL("winspool.drv")
    flags = 0x2 | 0x4   # PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    necesario, cant = wintypes.DWORD(0), wintypes.DWORD(0)
    ws.EnumPrintersW(flags, None, 4, None, 0, ctypes.byref(necesario), ctypes.byref(cant))
    if not necesario.value:
        return []
    buf = ctypes.create_string_buffer(necesario.value)
    if not ws.EnumPrintersW(flags, None, 4, buf, necesario, ctypes.byref(necesario), ctypes.byref(cant)):
        return []
    arr = ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_4))
    return [arr[i].pPrinterName for i in range(cant.value)]


def impresora_configurada(db: Session | None = None) -> str:
    """La cargada en Conexiones; si no hay, la primera que parezca Honeywell / PC42 / ZPL."""
    nombre = (ajustes.leer("rollo_impresora", db) or "").strip()
    if nombre:
        return nombre
    for p in listar_impresoras():
        if any(k in p.lower() for k in ("pc42", "honeywell", "zebra", "zpl", "zdesigner")):
            return p
    return ""


def _raw_windows(nombre: str, datos: bytes, doc: str) -> None:
    import ctypes
    from ctypes import wintypes

    class DOC_INFO_1(ctypes.Structure):
        _fields_ = [("pDocName", wintypes.LPWSTR), ("pOutputFile", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR)]

    ws = ctypes.WinDLL("winspool.drv", use_last_error=True)
    ws.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    ws.StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(DOC_INFO_1)]
    ws.StartPagePrinter.argtypes = ws.EndPagePrinter.argtypes = ws.EndDocPrinter.argtypes = \
        ws.ClosePrinter.argtypes = [wintypes.HANDLE]
    ws.WritePrinter.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    h = wintypes.HANDLE()
    if not ws.OpenPrinterW(nombre, ctypes.byref(h), None):
        raise ImpresoraError(f'No se encontró la impresora "{nombre}" en esta PC (error {ctypes.get_last_error()})')
    try:
        ultimo = 0
        for tipo in ("RAW", "XPS_PASS"):   # XPS_PASS para drivers v4
            di = DOC_INFO_1(doc, None, tipo)
            if not ws.StartDocPrinterW(h, 1, ctypes.byref(di)):
                ultimo = ctypes.get_last_error()
                continue
            try:
                ws.StartPagePrinter(h)
                escritos = wintypes.DWORD(0)
                buf = ctypes.create_string_buffer(datos, len(datos))
                ok = ws.WritePrinter(h, buf, len(datos), ctypes.byref(escritos))
                ws.EndPagePrinter(h)
            finally:
                ws.EndDocPrinter(h)
            if ok and escritos.value == len(datos):
                return
            ultimo = ctypes.get_last_error()
        raise ImpresoraError(f"La impresora no aceptó el trabajo RAW (error {ultimo})")
    finally:
        ws.ClosePrinter(h)


def enviar_zpl(zpl: str, impresora: str, doc: str = "Etiquetas PARKA") -> None:
    """Manda el ZPL (UTF-8 sin BOM) a la impresora: IP[:puerto] → TCP 9100; si no, spooler de Windows RAW."""
    if not impresora:
        raise ImpresoraError("No hay impresora de etiquetas configurada (Conexiones → Impresora de etiquetas)")
    datos = zpl.encode("utf-8")
    red = _es_red(impresora)
    if red:
        try:
            with socket.create_connection(red, timeout=6) as s:
                s.sendall(datos)
        except OSError as e:
            raise ImpresoraError(f"No se pudo conectar con la impresora {red[0]}:{red[1]} ({e})") from e
        return
    if sys.platform != "win32":
        raise ImpresoraError("La impresión directa por USB solo funciona con la app abierta en Windows")
    _raw_windows(impresora, datos, doc)


def imprimir(lista: list[dict], db: Session | None = None, doc: str = "Etiquetas PARKA", max_filas: int = 0) -> dict:
    if not lista:
        raise ImpresoraError("No hay etiquetas para imprimir")
    if max_filas:
        lista = lista[: 2 * max_filas]
    imp = impresora_configurada(db)
    zpl = build_zpl(lista, config(db))
    enviar_zpl(zpl, imp, doc)
    return {"ok": True, "impresora": imp, "etiquetas": len(lista), "filas": math.ceil(len(lista) / 2)}
