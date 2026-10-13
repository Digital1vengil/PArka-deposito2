// PARKA Depósito · Semáforo de Mercado Libre (vía ParkaHub) para el scanner.
// Después de cada lectura OK en Flex/Colecta pregunta a ParkaHub el estado del envío en vivo:
//   verde    → queda despachado.
//   rojo     → VENTA CANCELADA: se deshace la salida y suena el error.
//   amarillo → no corresponde (ya despachado, otra logística, falta factura…): se deshace y se avisa.
// Si ParkaHub no está conectado o no responde, el scanner sigue funcionando exactamente igual que antes.
(function () {
  if (typeof processScan !== 'function') return;
  const original = processScan;
  let conectado = null;            // null = no sé todavía
  const badge = document.createElement('div');
  badge.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:60;font:600 11px system-ui;padding:3px 8px;border-radius:999px;color:#fff;opacity:.85;pointer-events:none';
  function pintar(txt, color) { badge.textContent = txt; badge.style.background = color; }
  pintar('ML …', '#6b6b6b');
  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(badge));
  if (document.body) document.body.appendChild(badge);

  fetch('/despacho/api/ml/estado', { cache: 'no-store' }).then(r => r.json()).then(d => {
    conectado = !!d.conectado;
    pintar(conectado ? 'ML: validando' : 'ML: sin conectar', conectado ? '#15803d' : '#8a96ad');
  }).catch(() => { conectado = false; pintar('ML: sin conectar', '#8a96ad'); });

  function tipoActual() { return (state && state.dispatchType) || (typeof dispatchType !== 'undefined' ? dispatchType : '') || ''; }

  function deshacer(id) {
    const pkg = state && state.packages.find(p => p.trackingId === id);
    if (!pkg || pkg.estado !== 'despachado') return null;
    pkg.estado = 'pendiente';
    delete pkg.scannedAt; delete pkg.scannedCode; delete pkg.manual;
    state._completionNotified = false;
    currentDespacho = currentDespacho.filter(r => r['Numero de Etiqueta'] !== id);
    currentDespacho.forEach((r, i) => { r['Paquetes Despachados'] = i + 1; });
    saveState();
    refreshUI();
    return pkg;
  }

  async function validar(id) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 8000);
    try {
      const r = await fetch('/despacho/api/ml/verificar?codigo=' + encodeURIComponent(id), { cache: 'no-store', signal: ctl.signal });
      const d = await r.json();
      if (!d.ok) { pintar(d.sin_conexion ? 'ML: sin conectar' : 'ML: sin validar', '#b45309'); return; }
      if (d.veredicto === 'green') { pintar('ML ✓ ' + id.slice(-5), '#15803d'); return; }
      if (d.motivo === 'not_found') {            // puede ser un código del correo: se avisa, no se deshace
        beepWarn();
        showBanner('warn', '⚠️ ML no reconoce este envío', id, d.detalle || '');
        pintar('ML ? ' + id.slice(-5), '#b45309');
        return;
      }
      const pkg = deshacer(id);
      beepError();
      showBanner('error', d.veredicto === 'red' ? '⛔ ' + (d.mensaje || 'NO DESPACHAR') : '⚠️ No corresponde — se quitó de la salida',
        (pkg && (pkg.comprador || pkg.trackingId)) || id, d.detalle || '');
      pintar('ML ✕ ' + id.slice(-5), '#b91c1c');
    } catch (e) {
      pintar('ML: sin validar', '#b45309');
    } finally { clearTimeout(t); }
  }

  processScan = function (raw) {
    const id = extractId(String(raw || ''));
    const pkgAntes = state && id && state.packages.find(p => p.trackingId === id);
    const estabaPendiente = !!(pkgAntes && pkgAntes.estado !== 'despachado');
    original(raw);
    const tipo = tipoActual();
    if (conectado === false || !estabaPendiente || (tipo !== 'flex' && tipo !== 'colecta')) return;
    if (pkgAntes.estado === 'despachado') validar(id);
  };
  window.processScan = processScan;
})();
