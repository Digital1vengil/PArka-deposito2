// Utilidades compartidas
async function api(url, opts = {}) {
  const o = { headers: { 'X-Requested-With': 'fetch' }, ...opts };
  if (o.json !== undefined) {
    o.method = o.method || 'POST';
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  const r = await fetch(url, o);
  if (r.status === 401) { location.href = '/login'; throw new Error('Sesión vencida'); }
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (!r.ok) {
    let msg = (data && (data.detail || data.error)) || ('Error ' + r.status);
    if (r.status === 404 && msg === 'Not Found')
      msg = 'La app abierta es una versión anterior: tocá "Detener y salir" en la ventanita de PARKA Depósito y abrila de nuevo';
    if (Array.isArray(msg)) msg = msg.map(m => m.msg).join(' · ');
    throw new Error(msg);
  }
  return data;
}

function toast(msg, tipo = 'ok', ms = 3000) {
  const cols = { ok: 'bg-teal-700', error: 'bg-red-700', warn: 'bg-amber-700', info: 'bg-violeta' };
  const el = document.createElement('div');
  el.className = 'toast ' + (cols[tipo] || cols.info);
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Sonidos (igual que el scanner)
let _ac = null;
function _tone(freq, dur, type = 'sine', vol = 0.3) {
  try {
    _ac = _ac || new (window.AudioContext || window.webkitAudioContext)();
    const o = _ac.createOscillator(), g = _ac.createGain();
    o.type = type; o.frequency.value = freq; g.gain.value = vol;
    o.connect(g); g.connect(_ac.destination);
    o.start(); g.gain.exponentialRampToValueAtTime(0.0001, _ac.currentTime + dur);
    o.stop(_ac.currentTime + dur);
  } catch {}
}
const beepOk = () => { _tone(880, 0.12); setTimeout(() => _tone(1320, 0.15), 110); if (navigator.vibrate) navigator.vibrate(60); };
const beepError = () => { _tone(220, 0.35, 'square', 0.25); if (navigator.vibrate) navigator.vibrate([120, 60, 120]); };
const beepWarn = () => { _tone(520, 0.2, 'triangle'); setTimeout(() => _tone(520, 0.2, 'triangle'), 250); if (navigator.vibrate) navigator.vibrate(200); };

// Etiquetas: se imprimen desde una página HTML (misma grilla y diseño que el PDF) que abre sola
// el diálogo de impresión. No depende del visor de PDF del navegador ni de pestañas nuevas.
const esCelular = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
function imprimirPDF(url, btn) { return imprimirHTML(url.replace('/etiquetas.pdf', '/etiquetas.html'), btn); }
async function imprimirHTML(urlHtml, btn) {
  if (esCelular) { location.href = urlHtml; return; }
  const txt = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = 'Preparando etiquetas…'; }
  try {
    const r = await fetch(urlHtml, { headers: { 'X-Requested-With': 'fetch' } });
    if (r.status === 401) { location.href = '/login'; return; }
    const html = await r.text();
    if (!r.ok) {
      let msg = 'Error ' + r.status;
      try { const d = JSON.parse(html); msg = d.detail || d.error || msg; } catch {}
      if (r.status === 404) msg = 'La app abierta es una versión anterior: cerrala ("Detener y salir") y abrila de nuevo';
      throw new Error('No se pudieron generar las etiquetas: ' + msg);
    }
    document.getElementById('frame-imprimir')?.remove();
    const f = document.createElement('iframe');
    f.id = 'frame-imprimir';
    f.style.cssText = 'position:fixed;left:-10000px;top:0;width:210mm;height:297mm;border:0;';
    document.body.appendChild(f);
    f.srcdoc = html;   // la página llama sola a window.print() cuando carga
    toast('Abriendo la ventana de impresión…', 'info', 2500);
  } catch (e) {
    toast(e.message, 'error', 7000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = txt; }
  }
}

// Formato de etiquetas (se recuerda por equipo):
//   'rollo'        → Honeywell PC42t 40x25 de a 2, ZPL directo desde la app (sin diálogo)
//   'rollo-driver' → Honeywell por el driver de Windows (diálogo de impresión, página 93x25 mm)
//   'a4'           → hoja A4 autoadhesiva 4,8x2,5 (4x10)
const ETQ_KEY = 'parka_etq_formato';
// Decisión del usuario (06/10): siempre rollo Honeywell con diálogo, en todas las PCs.
function formatoEtiquetas() { return 'rollo-driver'; }
function setFormatoEtiquetas(f) { try { localStorage.setItem(ETQ_KEY, f); } catch {} syncFormatoEtiquetas(); }
function syncFormatoEtiquetas(root = document) {
  const f = formatoEtiquetas();
  root.querySelectorAll('select[data-etq-formato]').forEach(s => { s.value = f; });
  root.querySelectorAll('[data-solo-a4]').forEach(el => el.classList.toggle('hidden', f !== 'a4'));
  root.querySelectorAll('[data-solo-rollo]').forEach(el => el.classList.toggle('hidden', f === 'a4'));
}
function selectorFormatoEtiquetas() {
  return '<span class="inline-block py-2 px-3 rounded-lg bg-white border text-sm">🖨 Rollo Honeywell · con diálogo</span>';
}
// seccion: 'conteo' | 'devoluciones'. prueba=true → A4 con guías / rollo solo la 1.ª fila.
async function imprimirEtiquetas(seccion, id, { desde = 1, prueba = false, btn = null, formato = null } = {}) {
  formato = formato || formatoEtiquetas();
  if (formato === 'a4') return imprimirPDF(`/${seccion}/${id}/etiquetas.pdf?desde=${desde}${prueba ? '&guias=true' : ''}`, btn);
  const urlDriver = `/${seccion}/${id}/etiquetas-rollo.html${prueba ? '?filas=1' : ''}`;
  if (formato === 'rollo-driver') return imprimirHTML(urlDriver, btn);
  const txt = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = 'Imprimiendo…'; }
  try {
    const r = await api(`/${seccion}/api/${id}/imprimir-rollo`, { json: { filas: prueba ? 1 : 0 } });
    toast(`🖨 ${r.etiquetas} etiqueta${r.etiquetas === 1 ? '' : 's'} enviada${r.etiquetas === 1 ? '' : 's'} a ${r.impresora}`, 'ok', 4500);
  } catch (e) {
    toast('No se pudo imprimir directo: ' + e.message + '. Abriendo la impresión por el driver…', 'warn', 8000);
    if (btn) { btn.disabled = false; btn.innerHTML = txt; }
    return imprimirHTML(urlDriver, btn);
  }
  if (btn) { btn.disabled = false; btn.innerHTML = txt; }
}
document.addEventListener('DOMContentLoaded', () => syncFormatoEtiquetas());

function chipTarea(t) {
  let h = '';
  if (t.vencida) h += '<span class="chip chip-vencida">VENCIDA</span> ';
  else h += `<span class="chip chip-${t.estado}">${{ pendiente: 'PENDIENTE', en_curso: 'EN CURSO', hecha: 'HECHA' }[t.estado]}</span> `;
  if (t.prioridad === 'alta') h += '<span class="chip chip-alta">ALTA</span> ';
  if (t.origen === 'auto') h += '<span class="chip chip-auto">AUTO</span> ';
  return h;
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
}

// Marca en el menú la sección actual
document.querySelectorAll('#nav-lateral a.nav-link, #nav-mobile a.nav-link').forEach(a => {
  const h = a.getAttribute('href');
  if (h === '/' ? location.pathname === '/' : location.pathname.startsWith(h)) a.classList.add('activo');
});

// Menú lateral (computadora): achicar a solo íconos / agrandar. Se recuerda en este equipo.
function pdLateral() {
  const chico = document.documentElement.classList.toggle('lat-chico');
  try { localStorage.setItem('pd_lateral', chico ? 'chico' : 'grande'); } catch (e) {}
}

// Catálogo común (artículos de Odoo con código de barras). Para usar desde cualquier sección:
//   const a = await Catalogo.codigo('2000000016603');   // null si no existe
//   const {resultados, exacto} = await Catalogo.buscar('thor negro xl');
//   const {variantes} = await Catalogo.modelo('THOR 00114');
//   const {stock} = await Catalogo.stock([a.id]);        // stock actual en Odoo
const Catalogo = {
  async buscar(q, { limite = 100, marca = '', conCodigo = false } = {}) {
    const p = new URLSearchParams({ q, limite, marca, con_codigo: conCodigo });
    return api('/api/catalogo/buscar?' + p);
  },
  async codigo(codigo) {
    try { return await api('/api/catalogo/codigo/' + encodeURIComponent(String(codigo).trim())); }
    catch (e) { if (/no encontrado/i.test(e.message)) return null; throw e; }
  },
  articulo(id) { return api('/api/catalogo/articulo/' + id); },
  modelo(articulo) { return api('/api/catalogo/modelo?articulo=' + encodeURIComponent(articulo)); },
  stock(ids) { return api('/api/catalogo/stock', { json: { ids } }); },
  estado() { return api('/api/catalogo/estado'); },
};
window.Catalogo = Catalogo;


// ───────── Avisos (campana) de Recolección Ecommerce: entró una venta / se canceló / sin stock ─────────
// Todas las pantallas consultan cada 30 s. La campana de arriba a la derecha muestra cuántos avisos hay sin leer
// y abre un panel desplegable al costado. Las cancelaciones y faltantes además quedan en pantalla hasta "Entendido".
window.pdAvisos = (function () {
  if (!window.PD_NOVEDADES) return { toggle() {}, cerrar() {}, leerTodo() {}, permiso() {} };
  const KEY = 'pd_novedad_ultima', KEY_VISTO = 'pd_novedad_vista';
  let ultimo = null, visto = 0, lista = [], filtro = 'todos';
  try { const v = localStorage.getItem(KEY); if (v !== null) ultimo = +v; visto = +(localStorage.getItem(KEY_VISTO) || 0); } catch (e) {}
  const panel = () => document.getElementById('pd-avisos');
  const esAlerta = n => n.tipo === 'cancelada' || n.tipo === 'sin_stock';
  const ICONO = { nueva: '🛍️', cancelada: '⛔', sin_stock: '⚠️', info: 'ℹ️' };

  function badge() {
    const el = document.getElementById('pd-campana-n'); if (!el) return;
    const n = lista.filter(x => x.id > visto).length;
    if (n > 0) { el.textContent = n > 99 ? '99+' : n; el.classList.remove('hidden'); } else el.classList.add('hidden');
    const c = document.getElementById('pd-campana');
    if (c) c.classList.toggle('campana-alerta', lista.some(x => x.id > visto && esAlerta(x)));
  }
  function irA(cid) {
    cerrar();
    if (location.pathname.startsWith('/ecommerce') && typeof window.abrir === 'function') window.abrir(cid);
    else location.href = '/ecommerce#re' + cid;
  }
  function pintar() {
    const el = document.getElementById('pd-avisos-lista'); if (!el) return;
    const ver = lista.filter(n => filtro === 'todos' || (filtro === 'nueva' ? !esAlerta(n) : esAlerta(n)));
    if (!ver.length) { el.innerHTML = '<p class="avisos-vacio">No hay avisos.</p>'; return; }
    let dia = '';
    el.innerHTML = ver.map(n => {
      const cab = n.fecha !== dia ? `<div class="avisos-dia">${n.fecha === hoyTxt() ? 'Hoy' : n.fecha}</div>` : '';
      dia = n.fecha;
      const largo = (n.detalle || '').split('\n').length > 2;
      return cab + `<div class="aviso ${esAlerta(n) ? 'aviso-alerta' : ''} ${n.id > visto ? 'aviso-nuevo' : ''}" data-id="${n.id}">
        <span class="aviso-ico">${ICONO[n.tipo] || '🔔'}</span>
        <div class="aviso-cuerpo">
          <div class="aviso-txt">${esc(n.texto.replace(/^[🛍️⛔⚠️📦\s]+/u, ''))}</div>
          ${n.detalle ? (largo ? `<details class="aviso-det"><summary>Ver detalle</summary><div>${esc(n.detalle)}</div></details>` : `<div class="aviso-det">${esc(n.detalle)}</div>`) : ''}
          <div class="aviso-pie"><span>${n.hora}</span>${n.para ? '<span class="aviso-chip">solo supervisión</span>' : ''}
            ${n.control_id ? `<button type="button" class="avisos-link" data-ir="${n.control_id}">Ver búsqueda →</button>` : ''}</div>
        </div></div>`;
    }).join('');
    el.querySelectorAll('[data-ir]').forEach(b => b.onclick = () => irA(+b.dataset.ir));
  }
  function hoyTxt() { const d = new Date(); return String(d.getDate()).padStart(2, '0') + '/' + String(d.getMonth() + 1).padStart(2, '0'); }
  function leerTodo() {
    visto = lista.length ? Math.max(visto, ...lista.map(x => x.id)) : visto;
    try { localStorage.setItem(KEY_VISTO, String(visto)); } catch (e) {}
    badge(); setTimeout(pintar, 1200);
  }
  async function cargar() {
    try { lista = (await api('/ecommerce/api/novedades')).novedades || []; } catch (e) { return; }
    if (!visto && lista.length) { visto = Math.max(...lista.map(x => x.id)); try { localStorage.setItem(KEY_VISTO, String(visto)); } catch (e) {} }
    badge(); if (!panel().classList.contains('hidden')) pintar();
  }
  function abrirPanel() {
    const p = panel(); p.classList.remove('hidden');
    document.getElementById('pd-campana').setAttribute('aria-expanded', 'true');
    pintar(); permisoBtn(); leerTodo();
  }
  function cerrar() {
    const p = panel(); if (!p || p.classList.contains('hidden')) return;
    p.classList.add('hidden'); document.getElementById('pd-campana').setAttribute('aria-expanded', 'false');
  }
  function toggle(ev) { if (ev) ev.stopPropagation(); panel().classList.contains('hidden') ? abrirPanel() : cerrar(); }
  function permisoBtn() { const b = document.getElementById('pd-avisos-permiso'); if (b && (!('Notification' in window) || Notification.permission === 'granted')) b.classList.add('hidden'); }
  function permiso() {
    if (!('Notification' in window)) { toast('Este navegador no permite avisos', 'warn'); return; }
    Notification.requestPermission().then(r => { toast(r === 'granted' ? 'Listo: vas a ver los avisos aunque estés en otra pestaña' : 'No se activaron', r === 'granted' ? 'ok' : 'warn'); permisoBtn(); });
  }
  document.addEventListener('click', e => { const p = panel(); if (p && !p.classList.contains('hidden') && !p.contains(e.target) && !e.target.closest('#pd-campana')) cerrar(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') cerrar(); });
  document.querySelectorAll('.avisos-filtros button').forEach(b => b.onclick = () => {
    filtro = b.dataset.f; document.querySelectorAll('.avisos-filtros button').forEach(x => x.classList.toggle('activo', x === b)); pintar();
  });

  function cartel(n) {
    let box = document.getElementById('pd-cancel-box');
    if (!box) {
      box = document.createElement('div'); box.id = 'pd-cancel-box';
      box.style.cssText = 'position:fixed;top:4.2rem;left:50%;transform:translateX(-50%);z-index:95;width:min(560px,94vw);display:flex;flex-direction:column;gap:.5rem';
      document.body.appendChild(box);
    }
    const d = document.createElement('div');
    d.style.cssText = 'background:#b91c1c;color:#fff;border-radius:14px;padding:.9rem 1rem;box-shadow:0 10px 30px rgba(0,0,0,.3)';
    d.innerHTML = '<div style="font-weight:900;font-size:1.05rem"></div><div style="font-size:.85rem;opacity:.95;margin-top:.25rem;white-space:pre-line"></div>'
      + '<div style="display:flex;gap:.5rem;justify-content:flex-end;margin-top:.6rem"><button style="background:#fff;color:#b91c1c;font-weight:800;border-radius:10px;padding:.35rem .9rem">Entendido</button></div>';
    d.children[0].textContent = n.texto; d.children[1].textContent = n.detalle || '';
    d.querySelector('button').onclick = () => d.remove();
    box.appendChild(d);
  }
  function mostrar(n) {
    if (esAlerta(n)) { cartel(n); try { beepError(); setTimeout(beepError, 450); } catch (e) {} }
    else { try { beepOk(); } catch (e) {} }
    if ('Notification' in window && Notification.permission === 'granted' && document.hidden) {
      try { new Notification(n.tipo === 'cancelada' ? 'Venta cancelada · no despachar' : n.tipo === 'sin_stock' ? 'Prenda sin stock' : 'Venta nueva', { body: n.texto, tag: 'pd-' + n.id }); } catch (e) {}
    }
  }
  async function revisar() {
    try {
      const d = await api('/ecommerce/api/novedades?desde=' + (ultimo === null ? -1 : ultimo));
      if (ultimo !== null && d.novedades.length) {
        d.novedades.slice(-5).forEach(mostrar);
        const c = document.getElementById('pd-campana');
        if (c) { c.classList.remove('campana-suena'); void c.offsetWidth; c.classList.add('campana-suena'); }
        window.dispatchEvent(new CustomEvent('pd-novedad', { detail: d.novedades }));
      }
      ultimo = d.ultimo;
      try { localStorage.setItem(KEY, String(ultimo)); } catch (e) {}
      if (d.novedades.length || !lista.length) await cargar(); else badge();
    } catch (e) { /* sin conexión: se reintenta */ }
  }
  window.pdNovedadesYa = revisar;
  // Con la pestaña oculta no se consulta (ahorra pedidos al servidor); al volver se revisa enseguida.
  setTimeout(revisar, 1500);
  setInterval(() => { if (!document.hidden) revisar(); }, 30000);
  setInterval(() => { if (!document.hidden) cargar(); }, 120000);     // el aviso "Búsqueda del día" se actualiza con el total
  let ocultaDesde = 0;
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { ocultaDesde = Date.now(); return; }
    if (Date.now() - ocultaDesde > 20000) revisar();
  });
  return { toggle, cerrar, leerTodo, permiso, cargar };
})();
