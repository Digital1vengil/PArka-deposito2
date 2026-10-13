"""Etiquetas con código de barras para las hojas A4 autoadhesivas troqueladas.

Hoja: A4 · margen superior 2,1 cm · laterales 0,9 cm · etiqueta 4,8 x 2,5 cm · grilla 4 x 10 (40 por hoja).
Calibración de la impresora: ETIQUETAS_DX / ETIQUETAS_DY (mm), por defecto +1 mm a la derecha.
Imprimir al 100 % / "Tamaño real", sin "Ajustar a página".
"""
from __future__ import annotations

import io

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .. import config

COLS, FILAS = 4, 10
ANCHO, ALTO = 48 * mm, 25 * mm
MARGEN_IZQ, MARGEN_SUP = 9 * mm, 21 * mm


def ean13_valido(codigo: str) -> bool:
    if len(codigo) != 13 or not codigo.isdigit():
        return False
    suma = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(codigo[:12]))
    return (10 - suma % 10) % 10 == int(codigo[12])


# ── Diseño igual a la "Etiqueta de producto" de Odoo (Lato, recuadro violeta, cabecera clara) ──
BORDE = (0x5E / 255, 0x47 / 255, 0x66 / 255)
FONDO_CAB = (0xF8 / 255, 0xF8 / 255, 1.0)
_FUENTES = {"negra": "Helvetica-Bold", "normal": "Helvetica"}


def _registrar_fuentes() -> None:
    from pathlib import Path
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    carpeta = Path(__file__).resolve().parent.parent / "fonts"
    try:
        if "Lato-Black" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("Lato-Black", str(carpeta / "Lato-Black.ttf")))
            pdfmetrics.registerFont(TTFont("Lato", str(carpeta / "Lato-Regular.ttf")))
        _FUENTES.update(negra="Lato-Black", normal="Lato")
    except Exception:  # noqa: BLE001 — sin Lato se usa Helvetica
        pass


# Codificación EAN-13 (barras parejas, como la imagen que genera Odoo)
_L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011"]
_G = ["0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001", "0010111"]
_R = ["1110010", "1100110", "1101100", "1000010", "1011100", "1001110", "1010000", "1000100", "1001000", "1110100"]
_PARIDAD = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def _modulos_ean13(codigo: str) -> str:
    d = [int(c) for c in codigo]
    izq = "".join((_L if p == "L" else _G)[n] for p, n in zip(_PARIDAD[d[0]], d[1:7]))
    der = "".join(_R[n] for n in d[7:13])
    return "101" + izq + "01010" + der + "101"


def _barras(c: canvas.Canvas, x: float, y: float, ancho: float, alto: float, codigo: str) -> None:
    """Dibuja el código centrado en (x .. x+ancho)."""
    if ean13_valido(codigo):
        mods = _modulos_ean13(codigo)
        m = ancho / len(mods)
        c.setFillColorRGB(0, 0, 0)
        i = 0
        while i < len(mods):
            if mods[i] == "1":
                j = i
                while j < len(mods) and mods[j] == "1":
                    j += 1
                c.rect(x + i * m, y, (j - i) * m, alto, stroke=0, fill=1)
                i = j
            else:
                i += 1
    else:
        d = createBarcodeDrawing("Code128", value=codigo, barHeight=alto, humanReadable=False,
                                 barWidth=0.2 * mm, quiet=False)
        esc = min(1.0, ancho / d.width)
        d.scale(esc, 1)
        renderPDF.draw(d, c, x + (ancho - d.width * esc) / 2, y)


def _lineas(texto: str, fuente: str, tam: float, ancho: float, max_lineas: int = 2) -> list[str]:
    palabras, lineas, actual = texto.split(), [], ""
    for p in palabras:
        prueba = (actual + " " + p).strip()
        if stringWidth(prueba, fuente, tam) <= ancho or not actual:
            actual = prueba
        else:
            lineas.append(actual)
            actual = p
    if actual:
        lineas.append(actual)
    if len(lineas) > max_lineas:
        lineas = lineas[:max_lineas]
        while stringWidth(lineas[-1] + "…", fuente, tam) > ancho and lineas[-1]:
            lineas[-1] = lineas[-1][:-1]
        lineas[-1] += "…"
    return lineas


def _etiqueta(c: canvas.Canvas, x: float, y: float, e: dict, guias: bool) -> None:
    """x, y = esquina inferior izquierda del troquel (48 x 25 mm)."""
    if guias:
        c.setDash(1, 2)
        c.setLineWidth(0.3)
        c.setStrokeColorRGB(0.6, 0.6, 0.6)
        c.rect(x, y, ANCHO, ALTO)
        c.setDash()
    m = 1.2 * mm                                   # aire dentro del troquel
    bx, by, bw, bh = x + m, y + m, ANCHO - 2 * m, ALTO - 2 * m
    pad = 1.3 * mm
    cab_h = bh * 0.40
    # cabecera clara con el nombre (como Odoo: "THOR 00114 (BLACK, M, Parka)")
    c.setFillColorRGB(*FONDO_CAB)
    c.rect(bx, by + bh - cab_h, bw, cab_h, stroke=0, fill=1)
    nombre = e.get("nombre") or " ".join(v for v in (e.get("articulo", ""), f"({e.get('color','')}, {e.get('talle','')})") if v)
    tam = 7.2
    lineas = _lineas(nombre, _FUENTES["negra"], tam, bw - 2 * pad)
    if len(lineas) > 1:
        tam = 6.6
        lineas = _lineas(nombre, _FUENTES["negra"], tam, bw - 2 * pad)
    c.setFillColorRGB(0.07, 0.09, 0.15)
    c.setFont(_FUENTES["negra"], tam)
    ty = by + bh - pad - tam * 0.78
    for ln in lineas:
        c.drawString(bx + pad, ty, ln)
        ty -= tam * 1.08
    # SKU
    c.setFont(_FUENTES["normal"], 5)
    c.drawString(bx + pad, by + bh - cab_h - 2.6 * mm, _recortar(e.get("sku", ""), _FUENTES["normal"], 5, bw - 2 * pad))
    # código de barras + número debajo
    codigo = e.get("codigo_barras", "")
    if codigo:
        ancho_b = bw * 0.66
        _barras(c, bx + (bw - ancho_b) / 2, by + 2.6 * mm, ancho_b, 5.4 * mm, codigo)
        c.setFont(_FUENTES["normal"], 5)
        c.setFillColorRGB(0, 0, 0)
        c.drawCentredString(bx + bw / 2, by + 0.75 * mm, codigo)
    # recuadro violeta
    c.setStrokeColorRGB(*BORDE)
    c.setLineWidth(0.9)
    c.rect(bx, by, bw, bh, stroke=1, fill=0)


def _recortar(texto: str, fuente: str, tam: float, ancho: float) -> str:
    if stringWidth(texto, fuente, tam) <= ancho:
        return texto
    while texto and stringWidth(texto + "…", fuente, tam) > ancho:
        texto = texto[:-1]
    return texto + "…"


def pdf_etiquetas(etiquetas: list[dict], desde: int = 1, guias: bool = False) -> bytes:
    """Una entrada por etiqueta (repetir según la cantidad). desde = nº de troquel donde arrancar (1 a 40)."""
    _registrar_fuentes()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Etiquetas devolución PARKA")
    dx, dy = config.ETIQUETAS_DX * mm, config.ETIQUETAS_DY * mm
    pos = max(1, min(40, desde)) - 1
    for e in etiquetas:
        if pos == COLS * FILAS:
            c.showPage()
            pos = 0
        fila, col = divmod(pos, COLS)
        x = MARGEN_IZQ + col * ANCHO + dx
        y = A4[1] - MARGEN_SUP - (fila + 1) * ALTO - dy
        _etiqueta(c, x, y, e, guias)
        pos += 1
    c.showPage()
    c.save()
    return buf.getvalue()
