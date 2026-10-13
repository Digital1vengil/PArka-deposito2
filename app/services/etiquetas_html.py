"""Etiquetas como página HTML lista para imprimir (misma grilla y diseño que el PDF).

Se usa para imprimir directo desde el navegador: la página abre sola el diálogo de impresión,
no depende del visor de PDF (que en algunas PCs descarga el archivo o no abre nada).
Hoja A4 · margen sup 2,1 cm · laterales 0,9 cm · etiqueta 4,8 x 2,5 cm · 4 x 10.
"""
from __future__ import annotations

from html import escape

from reportlab.graphics.barcode.code128 import Code128

from .. import config
from .etiquetas import _FUENTES, _lineas, _modulos_ean13, _recortar, _registrar_fuentes, ean13_valido

COLS, FILAS = 4, 10


def _svg_barras(codigo: str) -> str:
    """SVG estirable (viewBox en módulos) con las barras del código."""
    rects, x = [], 0
    if ean13_valido(codigo):
        mods = _modulos_ean13(codigo)
        i = 0
        while i < len(mods):
            if mods[i] == "1":
                j = i
                while j < len(mods) and mods[j] == "1":
                    j += 1
                rects.append((i, j - i))
                i = j
            else:
                i += 1
        total = len(mods)
    else:
        b = Code128(codigo, barWidth=1, quiet=0)
        b.validate(); b.encode(); b.decompose()
        for ch in b.decomposed:
            w = ord(ch.upper()) - 64
            if ch.isupper():
                rects.append((x, w))
            x += w
        total = x or 1
    cuerpo = "".join(f'<rect x="{a}" y="0" width="{w}" height="1"/>' for a, w in rects)
    return (f'<svg viewBox="0 0 {total} 1" preserveAspectRatio="none" '
            f'xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges">{cuerpo}</svg>')


def _etiqueta(e: dict, guias: bool, left: float, top: float) -> str:
    bw = 48 - 2 * 1.2
    pad = 1.3
    nombre = e.get("nombre") or " ".join(
        v for v in (e.get("articulo", ""), f"({e.get('color', '')}, {e.get('talle', '')})") if v)
    tam = 7.2
    lineas = _lineas(nombre, _FUENTES["negra"], tam, (bw - 2 * pad) * 72 / 25.4)
    if len(lineas) > 1:
        tam = 6.6
        lineas = _lineas(nombre, _FUENTES["negra"], tam, (bw - 2 * pad) * 72 / 25.4)
    sku = _recortar(e.get("sku", ""), _FUENTES["normal"], 5, (bw - 2 * pad) * 72 / 25.4)
    codigo = e.get("codigo_barras", "")
    barras = (f'<div class="bc">{_svg_barras(codigo)}</div><div class="num">{escape(codigo)}</div>'
              if codigo else "")
    nom = "".join(f"<div>{escape(ln)}</div>" for ln in lineas)
    return (f'<div class="et{" guia" if guias else ""}" style="left:{left:.2f}mm;top:{top:.2f}mm">'
            f'<div class="box"><div class="cab"><div class="nom" style="font-size:{tam}pt">{nom}</div></div>'
            f'<div class="sku">{escape(sku)}</div>{barras}</div></div>')


def html_etiquetas(etiquetas: list[dict], desde: int = 1, guias: bool = False,
                   titulo: str = "Etiquetas PARKA", auto: bool = True) -> str:
    _registrar_fuentes()
    dx, dy = config.ETIQUETAS_DX, config.ETIQUETAS_DY
    paginas, actual = [], []
    pos = max(1, min(40, desde)) - 1
    for e in etiquetas:
        if pos == COLS * FILAS:
            paginas.append(actual)
            actual, pos = [], 0
        fila, col = divmod(pos, COLS)
        actual.append(_etiqueta(e, guias, 9 + col * 48 + dx, 21 + fila * 25 + dy))
        pos += 1
    paginas.append(actual)
    hojas = "".join(f'<section class="hoja">{"".join(p)}</section>' for p in paginas)
    script = """<script>
  (document.fonts ? document.fonts.ready : Promise.resolve()).then(() => setTimeout(() => window.print(), 150));
</script>""" if auto else ""
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>{escape(titulo)}</title>
<style>
  @font-face {{ font-family: 'LatoE'; font-weight: 900; src: url('/etiquetas-fonts/Lato-Black.ttf'); }}
  @font-face {{ font-family: 'LatoE'; font-weight: 400; src: url('/etiquetas-fonts/Lato-Regular.ttf'); }}
  @page {{ size: A4; margin: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ background: #fff; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  body {{ font-family: 'LatoE', Helvetica, Arial, sans-serif; color: #12172a; }}
  .hoja {{ position: relative; width: 210mm; height: 297mm; overflow: hidden; page-break-after: always; break-after: page; }}
  .hoja:last-child {{ page-break-after: auto; break-after: auto; }}
  .et {{ position: absolute; width: 48mm; height: 25mm; }}
  .et.guia {{ outline: 0.3pt dashed #999; }}
  .box {{ position: absolute; left: 1.2mm; top: 1.2mm; width: 45.6mm; height: 22.6mm; border: 0.9pt solid #5E4766; overflow: hidden; }}
  .cab {{ position: absolute; left: 0; top: 0; right: 0; height: 9.04mm; background: #F8F8FF; }}
  .nom {{ position: absolute; left: 1.3mm; right: 1.3mm; top: 1.0mm; font-weight: 900; line-height: 1.08; white-space: nowrap; overflow: hidden; }}
  .sku {{ position: absolute; left: 1.3mm; right: 1.3mm; top: 10.3mm; font-size: 5pt; line-height: 1; white-space: nowrap; overflow: hidden; }}
  .bc {{ position: absolute; left: 7.75mm; width: 30.1mm; top: 14.6mm; height: 5.4mm; }}
  .bc svg {{ display: block; width: 100%; height: 100%; fill: #000; }}
  .num {{ position: absolute; left: 0; right: 0; bottom: 0.35mm; text-align: center; font-size: 5pt; line-height: 1; }}
  @media screen {{ body {{ background: #e5e5e5; }} .hoja {{ margin: 8mm auto; background: #fff; box-shadow: 0 2px 10px #0003; }} }}
</style></head><body>{hojas}{script}</body></html>"""
