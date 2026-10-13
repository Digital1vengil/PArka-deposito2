// PARKA Depósito · Aviso de Control de paquetes dentro del scanner de Despacho.
// Después de cada salida OK pregunta si ese envío pasó por "Control de paquetes" hoy:
//   controlado        → no hace nada.
//   sin controlar     → aviso amarillo (no deshace la salida).
//   con faltante      → aviso rojo: el paquete salió incompleto.
// Si el envío no se cargó en ningún control, o la app no responde, el scanner sigue igual que antes.
(function () {
  if (typeof processScan !== 'function') return;
  const original = processScan;

  async function revisar(id) {
    try {
      const r = await fetch('/control/api/estado?codigo=' + encodeURIComponent(id), { cache: 'no-store', headers: { 'X-Requested-With': 'fetch' } });
      if (!r.ok) return;
      const d = await r.json();
      if (!d.en_control || d.controlado) return;
      if (d.estado === 'faltante') {
        beepError();
        showBanner('error', '⛔ Paquete con FALTANTE en Control', d.comprador || id, 'Revisalo antes de entregarlo');
      } else {
        beepWarn();
        showBanner('warn', '⚠️ Paquete sin controlar', d.comprador || id, 'No pasó por Control de paquetes (' + (d.estado_txt || d.estado) + ')');
      }
    } catch (e) { /* sin conexión: no molesta */ }
  }

  processScan = function (raw) {
    const id = extractId(String(raw || ''));
    const antes = state && id && state.packages.find(p => p.trackingId === id);
    const estabaPendiente = !!(antes && antes.estado !== 'despachado');
    original(raw);
    if (estabaPendiente && antes.estado === 'despachado') setTimeout(() => revisar(id), 300);
  };
  window.processScan = processScan;
})();
