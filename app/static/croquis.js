// Croquis del depósito MAGONTEX en SVG (Mapeo y ruta de Devoluciones).
// dibujarCroquis(plano, { paradas, tramos, actual, hechas, inicio, marcar, conteo, onRack })
(function () {
  const ZC = { A: ['#e3efef', '#9bc3c6', '#2f7d82'], B: ['#f5e6ea', '#d6a3b0', '#a83f5c'], C: ['#f6ece0', '#dcb895', '#b0702e'] };
  const e = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const rackDe = cod => (String(cod || '').match(/^([A-Z]\d{2})/) || [])[1] || '';
  const colDe = cod => +((String(cod || '').match(/-C(\d{1,2})/) || [])[1] || 0);

  window.dibujarCroquis = function (plano, o = {}) {
    const W = plano.ancho, H = plano.alto, M = 26;
    const paradas = o.paradas || [], hechas = o.hechas || new Set(), marcar = o.marcar || {};
    const porRack = {};
    paradas.forEach(p => { const r = rackDe(p.codigo); (porRack[r] = porRack[r] || []).push(p); });
    let s = `<svg viewBox="${-M} ${-M} ${W + 2 * M} ${H + 2 * M + 14}" class="w-full h-auto select-none" style="font-family:inherit">`;
    s += plano.zonas.map(z => `<rect x="${z.x}" y="0" width="${z.w}" height="${H}" fill="${ZC[z.zona][0]}"/>
      <text x="${z.x + z.w / 2}" y="${-9}" text-anchor="middle" font-size="13" font-weight="700" letter-spacing="2" fill="${ZC[z.zona][2]}">ZONA ${z.zona}</text>`).join('');
    s += `<rect x="0" y="0" width="${W}" height="${H}" fill="none" stroke="#71717a" stroke-width="2"/>`;
    s += `<text x="${-12}" y="${H / 2}" font-size="10" fill="#71717a" text-anchor="middle" transform="rotate(-90 ${-12} ${H / 2})">NORTE</text>`;
    s += `<text x="${W + 12}" y="${H / 2}" font-size="10" fill="#71717a" text-anchor="middle" transform="rotate(90 ${W + 12} ${H / 2})">SUR</text>`;
    s += plano.puertas.map(p => `<rect x="${p.x}" y="${H - 1}" width="${p.w}" height="6" fill="#c62828"/>
      <text x="${p.x + p.w / 2}" y="${H + 20}" text-anchor="middle" font-size="10" font-weight="700" fill="#c62828">${e(p.nombre.toUpperCase())}</text>`).join('');
    for (const r of plano.racks) {
      const c = ZC[r.zona], ps = porRack[r.codigo] || [], vert = r.h > r.w;
      const act = ps.some(p => p.n === o.actual), lista = ps.length && ps.every(p => hechas.has(p.n));
      const fill = r.columnas ? (marcar[r.codigo] || c[1]) : act ? '#18181b' : lista ? '#16a34a' : ps.length ? c[2] : (marcar[r.codigo] || c[1]);
      const txt = act || ps.length || lista ? '#fff' : '#27272a';
      s += `<g ${o.onRack ? `style="cursor:pointer" onclick="${o.onRack}('${r.codigo}')"` : ''}>
        <rect x="${r.x}" y="${r.y}" width="${r.w}" height="${r.h}" rx="2" fill="${fill}" stroke="${c[2]}" stroke-width="1.2"><title>${e(r.codigo)}${o.conteo && o.conteo[r.codigo] ? ' · ' + o.conteo[r.codigo] + ' artículos asignados' : ''}</title></rect>`;
      if (r.columnas) {
        for (let k = 1; k < r.columnas; k++) {
          const x = r.x + k * r.w / r.columnas;
          s += `<line x1="${x}" y1="${r.y}" x2="${x}" y2="${r.y + r.h}" stroke="${c[2]}" stroke-width=".8"/>`;
        }
        ps.forEach(p => { const k = colDe(p.codigo); if (!k) return;
          const x = r.x + (k - 1) * r.w / r.columnas;
          s += `<rect x="${x + 1}" y="${r.y + 1}" width="${r.w / r.columnas - 2}" height="${r.h - 2}" fill="${p.n === o.actual ? '#facc15' : hechas.has(p.n) ? '#16a34a' : c[2]}"/>
            <text x="${x + r.w / r.columnas / 2}" y="${r.y + r.h / 2 + 4}" text-anchor="middle" font-size="10" font-weight="700" fill="${p.n === o.actual ? '#18181b' : '#fff'}">C${k}</text>`; });
      }
      const tx = r.x + r.w / 2, ty = r.y + r.h / 2 + 4;
      if (!r.columnas || !ps.length) s += vert
        ? `<text x="${tx + 4}" y="${r.y + r.h / 2}" text-anchor="middle" font-size="11" font-weight="700" fill="${txt}" transform="rotate(-90 ${tx + 4} ${r.y + r.h / 2})">${e(r.codigo)}</text>`
        : `<text x="${tx}" y="${ty}" text-anchor="middle" font-size="11" font-weight="700" fill="${txt}">${e(r.codigo)}</text>`;
      else s += `<text x="${r.x - 4}" y="${ty}" text-anchor="end" font-size="11" font-weight="700" fill="${c[2]}">${e(r.codigo)}</text>`;
      if (o.conteo && o.conteo[r.codigo] && !ps.length) s += `<circle cx="${r.x + r.w - 4}" cy="${r.y + 4}" r="3.5" fill="#7c3aed"/>`;
      s += '</g>';
    }
    (o.tramos || []).forEach((t, i) => {
      if (!t || t.length < 2) return;
      const n = paradas[i] ? paradas[i].n : 0, hecho = hechas.has(n), act = n === o.actual;
      s += `<polyline points="${t.map(p => p.join(',')).join(' ')}" fill="none" stroke="${act ? '#ea580c' : hecho ? '#16a34a' : '#18181b'}" stroke-width="${act ? 3 : 2}" stroke-dasharray="${act || hecho ? '' : '5 4'}" stroke-linejoin="round" opacity="${act ? 1 : .55}"/>`;
    });
    if (o.inicio && o.inicio.punto) s += `<circle cx="${o.inicio.punto[0]}" cy="${o.inicio.punto[1]}" r="6" fill="#c62828" stroke="#fff" stroke-width="2"><title>Salida: ${e(o.inicio.nombre)}</title></circle>`;
    paradas.forEach(p => { if (!p.punto || !p.punto.length) return;
      const act = p.n === o.actual, hecho = hechas.has(p.n);
      s += `<circle cx="${p.punto[0]}" cy="${p.punto[1]}" r="${act ? 10 : 8}" fill="${act ? '#ea580c' : hecho ? '#16a34a' : '#18181b'}" stroke="#fff" stroke-width="2"/>
        <text x="${p.punto[0]}" y="${p.punto[1] + 4}" text-anchor="middle" font-size="${act ? 11 : 10}" font-weight="700" fill="#fff">${hecho ? '✓' : p.n}</text>`; });
    return s + '</svg>';
  };
})();
