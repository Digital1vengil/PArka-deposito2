/* PARKA Depósito — Despacho: botón "Avisar por WhatsApp" (totales del día).
   Abre WhatsApp (app o web) con el chat del destinatario y el texto ya escrito; se toca Enviar.
   Totales: registro compartido del despacho en vivo (/despacho/api/vivo/...) + envíos manuales SmartPost de este equipo. */
(function () {
  'use strict';
  var BASE = '/despacho/api/whatsapp';
  var DIAS = ['domingo', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado'];

  function aviso(m, t, ms) { try { toast(m, t || 'info', ms || 3000); } catch (e) { alert(m); } }
  function hoyLocal() { var d = new Date(); return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2); }

  function manualesHoy() {
    var ml = 0, tn = 0, h1 = hoyLocal(), h2 = new Date().toISOString().slice(0, 10);
    try {
      JSON.parse(localStorage.getItem('parka_envios_manuales') || '[]').forEach(function (m) {
        if (m.fecha !== h1 && m.fecha !== h2) return;
        if (m.plataforma === 'TN') tn++; else ml++;
      });
    } catch (e) {}
    return { ml: ml, tn: tn };
  }

  function fechaTexto(iso) {
    var p = iso.split('-'), d = new Date(+p[0], +p[1] - 1, +p[2]);
    var dia = DIAS[d.getDay()];
    return dia.charAt(0).toUpperCase() + dia.slice(1) + ' ' + p[2] + '/' + p[1] + '/' + p[0];
  }

  function armarTexto(d, man) {
    var t = d.totales || {};
    var manual = man.ml + man.tn;
    var total = (d.total || 0) + manual;
    var l = ['*PARKA · Despacho del día*', fechaTexto(d.fecha), '',
      'Colecta: ' + (t.colecta || 0),
      'Flex Mercado Libre: ' + (t.flex || 0),
      'Flex Tienda Nube: ' + (t.flex_tn || 0),
      'Tienda Nube: ' + (t.tiendanube || 0)];
    if (t.cambios) l.push('Cambios: ' + t.cambios);
    if (manual) l.push('Envíos manuales (SmartPost): ' + manual);
    l.push('', '*Total: ' + total + (total === 1 ? ' paquete*' : ' paquetes*'));
    return { texto: l.join('\n'), total: total };
  }

  async function avisar() {
    var b = document.getElementById('wa-despacho');
    var prev = b.innerHTML;
    b.disabled = true; b.innerHTML = 'Armando el resumen...';
    try {
      var r = await fetch(BASE + '/resumen', { credentials: 'same-origin' });
      var d = await r.json();
      if (!d || !d.ok) throw new Error('sin datos');
      var m = armarTexto(d, manualesHoy());
      if (!m.total) { aviso('Todavía no hay despachos registrados hoy', 'warn', 4000); return; }
      var url = 'https://wa.me/' + d.numero + '?text=' + encodeURIComponent(m.texto);
      var w = window.open(url, '_blank');
      if (!w) location.href = url;
      aviso('Se abrió WhatsApp con el resumen para ' + (d.nombre || 'el destinatario') + ' · tocá Enviar', 'ok', 5000);
    } catch (e) {
      aviso('No se pudo armar el resumen del día', 'error', 4000);
    } finally {
      b.disabled = false; b.innerHTML = prev;
    }
  }

  async function cambiarNumero(ev) {
    ev.preventDefault();
    var d = await (await fetch(BASE + '/resumen', { credentials: 'same-origin' })).json();
    var n = window.prompt('Número de WhatsApp de ' + (d.nombre || 'destinatario') + ' (con código de país, ej. 5491156053000):', d.numero || '');
    if (n === null) return;
    var r = await fetch(BASE + '/destino', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ numero: n, nombre: d.nombre || '' }) });
    var j = await r.json().catch(function () { return {}; });
    if (r.ok && j.ok) { aviso('Número guardado: +' + j.numero, 'ok'); pintarDestino(); }
    else aviso(j.detail || 'No se pudo guardar el número', 'error', 4000);
  }

  async function pintarDestino() {
    try {
      var d = await (await fetch(BASE + '/resumen', { credentials: 'same-origin' })).json();
      var s = document.getElementById('wa-despacho-dest');
      if (!s || !d.ok) return;
      s.innerHTML = 'A ' + (d.nombre || '') + ' · +' + d.numero +
        (d.puede_editar ? ' · <a href="#" id="wa-despacho-cambiar" style="color:#15803d;font-weight:700;">cambiar</a>' : '');
      var a = document.getElementById('wa-despacho-cambiar');
      if (a) a.addEventListener('click', cambiarNumero);
    } catch (e) {}
  }

  function montar() {
    if (document.getElementById('wa-despacho')) return true;
    var ancla = document.getElementById('sp-cierre');
    if (!ancla) return false;
    ancla.style.marginBottom = '8px';
    ancla.insertAdjacentHTML('afterend',
      '<button id="wa-despacho" style="width:100%;padding:16px;border:0;border-radius:16px;background:#25d366;color:#fff;font-weight:800;font-size:16px;box-shadow:0 2px 6px rgba(0,0,0,.12);">' +
      '&#128172; Avisar a Gabriel por WhatsApp</button>' +
      '<p id="wa-despacho-dest" style="margin:4px 0 12px;font-size:11px;color:#64748b;text-align:center;"></p>');
    document.getElementById('wa-despacho').addEventListener('click', avisar);
    pintarDestino();
    return true;
  }

  var intentos = 0;
  (function esperar() { if (!montar() && intentos++ < 40) setTimeout(esperar, 250); })();
})();
