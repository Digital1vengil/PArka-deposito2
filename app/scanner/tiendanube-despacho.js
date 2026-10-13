// PARKA Depósito · Tienda Nube dentro del scanner de Despacho.
// Cuando sale una orden de Tienda Nube (escaneada o "Marcar salida" a mano) avisa a la app,
// que la marca como ENVIADA en Tienda Nube (Conexiones → Tienda Nube). Si la conexión está apagada
// o no responde, el scanner sigue igual; los errores se reintentan solos desde la app.
(function () {
  if (typeof processScan !== 'function') return;

  function esTN() { return !!(state && state.dispatchType === 'tiendanube'); }
  function pkgDe(id) { return esTN() && typeof findTnPackage === 'function' ? findTnPackage(id) : null; }

  async function avisar(pkg) {
    const numero = String(pkg.ventaId || pkg.trackingId || '').replace(/^#/, '').trim();
    if (!/^\d+$/.test(numero)) return;
    try {
      const r = await fetch('/api/tiendanube/despachado', {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
        body: JSON.stringify({ numero })
      });
      if (!r.ok) return;
      const d = await r.json();
      if (d.estado === 'error' && typeof toast === 'function') toast('Tienda Nube: ' + d.mensaje + ' (se reintenta solo)', 'warn', 5000);
    } catch (e) { /* sin conexión: no molesta */ }
  }

  function envolver(fn, idDe) {
    return function () {
      const id = idDe.apply(null, arguments);
      const antes = id ? pkgDe(id) : null;
      const pendiente = !!(antes && antes.estado !== 'despachado');
      const r = fn.apply(this, arguments);
      if (pendiente && antes.estado === 'despachado') setTimeout(() => avisar(antes), 200);
      return r;
    };
  }

  processScan = envolver(processScan, raw => extractId(String(raw || '')));
  window.processScan = processScan;
  if (typeof marcarManual === 'function') {
    marcarManual = envolver(marcarManual, id => id);
    window.marcarManual = marcarManual;
  }
})();
