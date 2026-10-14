// PARKA Depósito · Despacho en vivo (sin Drive).
// La lista del día ya no se baja de la carpeta de Drive: se trae directo de las APIs.
//   Colecta / Flex → envíos de Mercado Libre "Listas para despachar" (etiqueta impresa) que ML pide despachar HOY.
//   Tienda Nube    → órdenes "Por enviar" (empaquetadas, sin enviar).
// Además:
//   • Día activo: la lista vale solo para el día en que se cargó. Una sesión de otro día no se retoma y no deja escanear.
//   • Al escanear, la etiqueta tiene que coincidir con un envío / nº de venta de la lista. Si no está, se le pregunta
//     a ML / TN qué es (cancelada, de otro día, Flex en vez de Colecta…). Si es una lista de hoy que entró después, se suma.
//   • Lo que ya salió hoy (escaneado en cualquier dispositivo) queda registrado en el servidor: en "Ventas del día"
//     aparece en verde y "Cargar" trae solo los nuevos ingresos (no se puede volver a cargar lo despachado).
//   • Cada minuto se revisan las canceladas: si una venta de la lista se cancela, se saca de la lista y aparece
//     un aviso rojo que hay que confirmar (si ya estaba escaneada: "sacala del despacho").
// Todo lo demás (Iniciar / Finalizar despacho → Excel en Drive, cierre del día por mail, SmartPost) sigue igual.
(function () {
  if (typeof processScan !== 'function') return;

  var TIPOS = { colecta: 'Colecta', flex: 'Flex', tiendanube: 'Tienda Nube', cambios: 'Cambios' };
  var FUENTE = { tiendanube: 'Tienda Nube', cambios: 'la app de cambios' };
  var CANC_MS = 60 * 1000, LISTA_MS = 2 * 60 * 1000;
  var ultimaLista = 0, revisando = false, avisoDia = '', horaRevision = '';

  function hoyLocal() {
    var d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function diaDe(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return '';
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function ddmm(iso) { return iso ? iso.slice(8, 10) + '/' + iso.slice(5, 7) : ''; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function tipoSel() { return (typeof driveTipo !== 'undefined' && driveTipo) || 'flex'; }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) {} }

  async function getJSON(url) {
    var r = await fetch(url, { cache: 'no-store', headers: { 'X-Requested-With': 'fetch' } });
    if (!r.ok) throw new Error('El servidor respondió ' + r.status);
    return r.json();
  }
  function postJSON(url, body) {
    return fetch(url, { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      body: JSON.stringify(body) }).catch(function () {});
  }

  // ───────────── 1. Día activo: una sesión de otro día no se retoma ─────────────
  (function archivarSesionVieja() {
    try {
      var st = JSON.parse(lsGet(LS_KEY) || 'null');
      if (!st || !st.packages) return;
      var dia = st.fecha || (st.startedAt ? diaDe(st.startedAt) : '');
      if (dia && dia !== hoyLocal()) {
        lsSet(LS_KEY + '_anterior', JSON.stringify(st));
        lsDel(LS_KEY);
        window._pkSesionArchivada = dia;
      }
    } catch (e) {}
  })();

  function diaVencido() { return !!(state && state.vivo && state.fecha && state.fecha !== hoyLocal()); }

  // ───────────── 2. Recuadro "Ventas del día" (reemplaza la lista de Drive) ─────────────
  // Historial de sesiones / despachos: ya no se muestra (pedido del usuario)
  (function ocultarHistorial() {
    var st = document.createElement('style');
    st.textContent = '#history-zone,#despacho-history-zone{display:none!important}';
    document.head.appendChild(st);
  })();

  var BOTONES = [
    { tipo: 'colecta', icono: 'barcode', sub: 'Mercado Libre' },
    { tipo: 'flex', icono: 'qr-code', sub: 'Mercado Libre' },
    { tipo: 'tiendanube', icono: 'shopping-bag', sub: 'Por enviar' },
    { tipo: 'cambios', icono: 'repeat', sub: 'App de cambios' }
  ];

  function montarRecuadro() {
    var zone = document.getElementById('drive-files-zone');
    if (!zone || zone.dataset.vivo) return;
    zone.dataset.vivo = '1';
    var titulo = zone.querySelector('p.font-semibold');
    if (titulo) titulo.innerHTML = '<i data-lucide="rocket" class="icon text-forest-600"></i> Iniciar despacho';
    var act = document.getElementById('btn-refresh-drive-files');
    if (act) act.style.display = 'none';
    var tabs = document.getElementById('btn-drive-colecta');
    if (tabs && tabs.parentElement) tabs.parentElement.style.display = 'none';
    var buscar = document.getElementById('drive-search');
    if (buscar && buscar.parentElement) buscar.parentElement.style.display = 'none';
    var pie = zone.querySelector('p.text-slate-400.text-xs.mt-2');
    if (pie) pie.innerHTML = 'Elegí el tipo y se carga solo lo de hoy: Colecta y Flex <b>listas para despachar</b> en Mercado Libre · ' +
      'Tienda Nube órdenes <b>por enviar</b> · Cambios aceptados en la app de cambios';
    var subir = document.getElementById('btn-upload-pc');
    if (subir) {
      subir.className = 'w-full mt-3 text-slate-400 text-xs font-semibold flex items-center justify-center gap-1';
      subir.style.cssText = 'background:none;border:0;padding:6px 0;';
      subir.innerHTML = '<i data-lucide="upload" class="icon" style="width:12px;height:12px;"></i> Sin conexión: cargar archivo desde la PC';
      var lista = document.getElementById('drive-files-list');
      if (lista) lista.insertAdjacentElement('afterend', subir);
    }
    if (window._pkSesionArchivada) {
      zone.insertAdjacentHTML('afterbegin', '<div class="mb-3 bg-amber-50 border border-amber-300 rounded-xl p-2 text-amber-800 text-xs">' +
        'La lista del ' + esc(ddmm(window._pkSesionArchivada)) + ' quedó cerrada: cada día se despacha con su propia lista.</div>');
    }
  }

  function filaPaquete(p) {
    if (p.escaneado) {
      return '<div class="flex items-center gap-2 py-1.5 px-1.5 my-0.5 rounded-lg text-xs" style="background:#dcfce7;border:1px solid #86efac">' +
        '<span style="color:#15803d;font-weight:900;flex-shrink:0">✓</span>' +
        '<span class="font-mono flex-shrink-0" style="color:#16a34a">' + esc(p.cambio ? String(p.trackingId) : String(p.trackingId).slice(-6)) + '</span>' +
        '<span class="font-semibold truncate" style="color:#166534">' + esc(p.comprador || '(sin nombre)') + '</span>' +
        '<span class="truncate flex-1 text-right" style="color:#15803d;font-weight:600">despachada' +
        (p.escaneadoHora ? ' ' + esc(p.escaneadoHora) : '') + '</span></div>';
    }
    return '<div class="flex items-center gap-2 py-1.5 border-b border-slate-100 last:border-0 text-xs">' +
      '<span class="font-mono text-slate-400 flex-shrink-0">' + esc(p.cambio ? String(p.trackingId) : String(p.trackingId).slice(-6)) + '</span>' +
      '<span class="text-slate-700 font-semibold truncate">' + esc(p.comprador || '(sin nombre)') + '</span>' +
      '<span class="text-slate-400 truncate flex-1 text-right">' + esc(p.producto || p.sku || '') + '</span></div>';
  }

  // Sesión abierta de HOY y del mismo tipo (la que está en este dispositivo)
  function sesionDeHoy(tipo) {
    return !!(state && state.vivo && state.fecha === hoyLocal() && state.dispatchType === tipo && state.packages);
  }
  // Suma lo que este dispositivo ya despachó (por si el aviso al servidor todavía no llegó)
  function marcarEscaneadosLocales(tipo, paquetes) {
    if (!sesionDeHoy(tipo)) return;
    paquetes.forEach(function (p) {
      if (p.escaneado) return;
      var l = buscar(state.packages, p.trackingId) || (p.ventaId && buscar(state.packages, p.ventaId));
      if (l && l.estado === 'despachado') {
        p.escaneado = true;
        if (l.scannedAt) { var t = new Date(l.scannedAt); p.escaneadoHora = String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0'); }
      }
    });
  }

  // Pantalla de inicio: 4 botones. Al tocar uno se trae todo lo de hoy de ese tipo y arranca el despacho.
  function cargarVivo() {
    montarRecuadro();
    var zone = document.getElementById('drive-files-zone');
    var list = document.getElementById('drive-files-list');
    if (!zone || !list) return;
    zone.classList.remove('hidden');
    var abierta = state && state.vivo && state.fecha === hoyLocal() && state.packages ? state.dispatchType : '';
    list.innerHTML = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">' + BOTONES.map(function (b) {
      var enCurso = abierta === b.tipo;
      return '<button data-pk-iniciar="' + b.tipo + '" style="background:#111;color:#fff;border:0;border-radius:16px;padding:18px 8px;' +
        'display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;font:800 16px system-ui;box-shadow:0 2px 6px rgba(0,0,0,.15);min-height:96px">' +
        '<i data-lucide="' + b.icono + '" class="icon" style="width:26px;height:26px;"></i>' + TIPOS[b.tipo] +
        '<span style="font:600 11px system-ui;color:' + (enCurso ? '#86efac' : '#cbd5e1') + '">' + (enCurso ? 'en curso · sumar nuevas' : b.sub) + '</span></button>';
    }).join('') + '</div><div id="pk-iniciar-msg"></div>';
    list.querySelectorAll('[data-pk-iniciar]').forEach(function (btn) {
      btn.addEventListener('click', function () { iniciar(btn.getAttribute('data-pk-iniciar'), btn); });
    });
    if (window.lucide) lucide.createIcons();
  }

  var iniciando = false;
  async function iniciar(tipo, btn) {
    if (iniciando) return;
    iniciando = true;
    var msg = document.getElementById('pk-iniciar-msg');
    if (msg) msg.innerHTML = '';
    var html0 = btn.innerHTML;
    btn.innerHTML = '<span class="animate-pulse" style="font:700 14px system-ui">Cargando ' + TIPOS[tipo] + '…</span>';
    try { driveTipo = tipo; } catch (e) {}
    var d;
    try {
      d = await getJSON('/despacho/api/vivo?tipo=' + tipo + '&refrescar=1');
    } catch (e) {
      d = { ok: false, conectado: true, error: 'No se pudo consultar: ' + e.message };
    }
    iniciando = false;
    btn.innerHTML = html0;
    if (window.lucide) lucide.createIcons();
    var falla = function (titulo, texto, rojo) {
      if (!msg) return;
      msg.innerHTML = '<div style="margin-top:12px;border-radius:12px;padding:12px;font-size:12px;' +
        (rojo ? 'background:#fef2f2;border:1px solid #fecaca;color:#b91c1c' : 'background:#f8fafc;border:1px solid #e2e8f0;color:#475569') + '">' +
        '<p style="font-weight:700;margin:0 0 2px">' + esc(titulo) + '</p>' + esc(texto) + '</div>';
    };
    if (!d.ok) { falla(d.conectado === false ? 'Sin conexión con ' + (FUENTE[tipo] || 'Mercado Libre') : 'No se pudo leer ' + TIPOS[tipo], d.error, true); return; }
    marcarEscaneadosLocales(tipo, d.paquetes);
    var nuevas = d.paquetes.filter(function (p) { return !p.escaneado; }).length;
    if (!nuevas) {
      var nada = tipo === 'tiendanube' ? 'órdenes por enviar' : tipo === 'cambios' ? 'cambios para despachar' : 'envíos listos para despachar';
      falla(TIPOS[tipo] + ': nada para cargar', d.paquetes.length ? 'Todo lo de hoy ya se despachó.' :
        'No hay ' + nada + ' hoy' + (d.por_imprimir ? ' (' + d.por_imprimir + ' con etiqueta por imprimir: entran solas cuando se impriman).' : '.'), false);
      return;
    }
    empezar(tipo, d);
  }

  function empezar(tipo, d) {
    // Solo los nuevos ingresos: lo que ya salió hoy no se vuelve a cargar
    var nuevos = JSON.parse(JSON.stringify(d.paquetes.filter(function (p) { return !p.escaneado; })));
    if (!nuevos.length) { toast('No hay ventas nuevas: todo lo de hoy ya se despachó', 'info', 3000); return; }
    if (sesionDeHoy(tipo)) {
      // Ya hay una lista de hoy abierta en este dispositivo: se le suman los nuevos (lo escaneado queda como está)
      var fuera = {};
      (state.canceladas || []).forEach(function (x) { fuera[String(x.trackingId)] = 1; });
      var add = mergePackages(nuevos.filter(function (p) { return !fuera[String(p.trackingId)]; }));
      state._completionNotified = false;
      saveState();
      showScreen('screen-scanner');
      refreshUI();
      toast(add ? '➕ ' + add + ' nueva' + (add !== 1 ? 's' : '') + ' sumada' + (add !== 1 ? 's' : '') + ' a la lista de hoy'
                : 'Esas ventas ya estaban en la lista de hoy', 'ok', 3500);
      return;
    }
    dispatchType = tipo; persistDispatchType(tipo);
    parsedFile = {
      type: tipo === 'tiendanube' ? 'tiendanube' : 'zpl',
      packages: nuevos,
      fileName: TIPOS[tipo] + ' ' + ddmm(d.fecha) + (tipo === 'tiendanube' ? ' · Tienda Nube' : tipo === 'cambios' ? ' · App de cambios' : ' · Mercado Libre')
    };
    startDispatch();
    if (!state) return;
    state.vivo = true;
    state.fecha = d.fecha || hoyLocal();
    state.dispatchType = tipo;
    state.canceladas = [];
    saveState();
    ultimaLista = Date.now();
    horaRevision = d.hora || '';
    toast('✅ ' + nuevos.length + ' ' + (tipo === 'tiendanube' ? 'órdenes' : tipo === 'cambios' ? 'cambios' : 'envíos') + ' de ' + TIPOS[tipo] + ' cargados', 'ok', 3500);
    pintarBanda();
  }

  // El navegador de archivos de Drive pasa a ser la lista en vivo
  loadDriveFiles = cargarVivo;
  window.loadDriveFiles = cargarVivo;

  // ───────────── 3. Buscar un código en la lista (envío, nº de venta o nº de orden) ─────────────
  function norm(x) { return String(x == null ? '' : x).replace(/^#/, '').trim().toUpperCase(); }
  function buscar(lista, id) {
    var n = norm(id);
    if (!n || !lista) return null;
    for (var i = 0; i < lista.length; i++) {
      var p = lista[i];
      if (norm(p.trackingId) === n || (p.ventaId && norm(p.ventaId) === n) ||
          (p.ordenes && p.ordenes.indexOf(n) !== -1)) return p;
    }
    return null;
  }

  // ───────────── 4. Canceladas ─────────────
  var avisos = [];
  function aviso(titulo, l1, l2) {
    avisos.push({ titulo: titulo, l1: l1, l2: l2 });
    var cont = document.getElementById('pk-aviso-cancelada');
    if (!cont) {
      cont = document.createElement('div');
      cont.id = 'pk-aviso-cancelada';
      cont.style.cssText = 'position:fixed;inset:0;z-index:300;background:rgba(127,29,29,.94);display:flex;align-items:center;justify-content:center;padding:20px;overflow-y:auto';
      document.body.appendChild(cont);
    }
    cont.innerHTML = '<div style="max-width:420px;width:100%;text-align:center">' + avisos.map(function (a) {
      return '<div style="background:rgba(0,0,0,.25);border-radius:16px;padding:14px;margin-bottom:10px">' +
        '<div style="font-size:40px;line-height:1">⛔</div>' +
        '<p style="margin:8px 0 4px;font:900 21px system-ui;color:#fff">' + esc(a.titulo) + '</p>' +
        '<p style="margin:0;font:700 16px system-ui;color:#fecaca">' + esc(a.l1) + '</p>' +
        '<p style="margin:4px 0 0;font:600 14px system-ui;color:#fde68a">' + esc(a.l2) + '</p></div>';
    }).join('') + '<button id="pk-aviso-ok" style="width:100%;padding:14px;border:0;border-radius:14px;background:#fff;color:#7f1d1d;font:800 16px system-ui">Entendido</button></div>';
    document.getElementById('pk-aviso-ok').onclick = function () { avisos = []; cont.remove(); };
    beepError(); setTimeout(beepError, 550);
  }

  function marcarCancelada(pkg) {
    var i = state.packages.indexOf(pkg);
    if (i === -1) return;
    state.packages.splice(i, 1);
    var ya = pkg.estado === 'despachado';
    state.canceladas = state.canceladas || [];
    state.canceladas.push(Object.assign({}, pkg, { canceladaAt: new Date().toISOString(), yaEscaneada: ya }));
    if (ya) {
      currentDespacho = currentDespacho.filter(function (r) { return String(r['Numero de Etiqueta']) !== String(pkg.trackingId); });
      currentDespacho.forEach(function (r, k) { r['Paquetes Despachados'] = k + 1; });
    }
    saveState();
    refreshUI();
    aviso('VENTA CANCELADA — NO DESPACHAR', (pkg.comprador || '(sin nombre)') + ' · venta ' + (pkg.ventaId || pkg.trackingId),
      ya ? 'Ya la escaneaste: SACALA del despacho' : 'Se sacó de la lista. No la despaches.');
    postJSON('/despacho/api/vivo/cancelada-vista', { tipo: state.dispatchType, tracking: String(pkg.trackingId),
      venta: String(pkg.ventaId || ''), comprador: pkg.comprador || '', ya_escaneada: ya });
  }

  function aplicarCanceladas(lista) {
    (lista || []).forEach(function (c) {
      var p = buscar(state.packages, c.trackingId) || (c.ventaId && buscar(state.packages, c.ventaId));
      if (!p && c.ordenes) for (var k = 0; k < c.ordenes.length && !p; k++) p = buscar(state.packages, c.ordenes[k]);
      if (p) marcarCancelada(p);
    });
  }

  async function revisar(forzar) {
    if (revisando || !state || !state.vivo) return;
    if (diaVencido()) {
      if (avisoDia !== state.fecha) {
        avisoDia = state.fecha;
        toast('La lista es del ' + ddmm(state.fecha) + '. Finalizá el despacho y cargá la de hoy.', 'warn', 8000);
        pintarBanda();
      }
      return;
    }
    revisando = true;
    var tipo = state.dispatchType;
    try {
      var c = await getJSON('/despacho/api/vivo/canceladas?tipo=' + tipo);
      if (c.ok && state && state.vivo) aplicarCanceladas(c.canceladas);
      if ((forzar || Date.now() - ultimaLista >= LISTA_MS) && state && state.vivo) {
        ultimaLista = Date.now();
        var d = await getJSON('/despacho/api/vivo?tipo=' + tipo + (forzar ? '&refrescar=1' : ''));
        if (d.ok) horaRevision = d.hora || '';
        if (d.ok && state && state.vivo && state.dispatchType === tipo) {
          aplicarCanceladas(d.canceladas);
          var fuera = {};
          (state.canceladas || []).forEach(function (x) { fuera[String(x.trackingId)] = 1; });
          // lo que ya salió hoy (en este u otro dispositivo) no se vuelve a sumar
          var nuevas = d.paquetes.filter(function (p) { return !fuera[String(p.trackingId)] && !p.escaneado; });
          nuevas.forEach(function (p) { p.nueva = true; });
          var n = mergePackages(nuevas);
          if (n) {
            state._completionNotified = false;
            saveState(); refreshUI();
            beepWarn();
            toast('➕ ' + n + ' venta' + (n !== 1 ? 's' : '') + ' nueva' + (n !== 1 ? 's' : '') + ' para despachar: se sumaron a la lista', 'info', 6000);
          } else if (forzar) {
            toast('No hay ventas nuevas (' + (horaRevision || '') + ')', 'info', 2500);
          }
          pintarBanda();
        }
      }
    } catch (e) { /* sin conexión: se reintenta en el próximo ciclo */ }
    revisando = false;
  }
  setInterval(function () { revisar(false); }, CANC_MS);
  window.pkBuscarNuevas = function () { revisar(true); };

  // ───────────── 5. Escaneo: tiene que coincidir con la lista del día ─────────────
  var interno = processScan;
  var ultimoFuera = '', ultimoFueraT = 0;

  // noVisto = todavía no se mostró "No encontrado" (Tienda Nube: primero se consulta la guía de SmartPost)
  function noEncontrado(id, detalle) {
    beepError();
    showBanner('error', '❌ No encontrado', 'Código: ' + (id.length > 20 ? id.slice(0, 20) + '…' : id),
      detalle || 'Este paquete no está en la lista de despacho');
  }
  async function explicar(id, tipo, noVisto) {
    try {
      var r = await getJSON('/despacho/api/vivo/envio?tipo=' + tipo + '&codigo=' + encodeURIComponent(id));
      if (!state || state.dispatchType !== tipo) return;
      if (!r.ok) { if (noVisto) noEncontrado(id, r.error || ''); return; }
      if (r.motivo === 'agregar' && r.paquete) {
        var cancel = buscar(state.canceladas || [], r.paquete.trackingId);
        if (cancel) {
          beepError();
          showBanner('error', '⛔ VENTA CANCELADA — NO DESPACHAR', cancel.comprador || cancel.trackingId, 'Venta ' + (cancel.ventaId || cancel.trackingId));
          return;
        }
        var yaEstaba = !!buscar(state.packages, r.paquete.trackingId);
        if (r.guia) { state.spGuias = state.spGuias || {}; state.spGuias[norm(id)] = String(r.paquete.trackingId); }
        if (!yaEstaba) {
          r.paquete.nueva = true;
          mergePackages([r.paquete]);
          state._completionNotified = false;
        }
        saveState(); refreshUI();
        lastScanRaw = ''; lastScanTime = 0;
        interno(String(r.paquete.trackingId));
        if (r.guia) toast('📦 Guía SmartPost ' + r.guia + ' → orden #' + r.paquete.trackingId + (r.guia_via === 'nombre' ? ' (por nombre)' : '') + (yaEstaba ? '' : ' · se sumó a la lista'), 'info', 3500);
        else toast(tipo === 'tiendanube' ? '➕ Orden por enviar: se sumó a la lista' : '➕ Entró recién en ML: se sumó a la lista', 'info', 3500);
        return;
      }
      if (r.motivo === 'ya_escaneada') {
        beepWarn();
        showBanner('warn', '⚠️ Ya fue despachado hoy', 'Código: ' + (id.length > 20 ? id.slice(0, 20) + '…' : id), r.mensaje || '');
        return;
      }
      if (r.motivo === 'cancelada') {
        aviso('VENTA CANCELADA — NO DESPACHAR', 'Código ' + id, 'No está en la lista: no la despaches');
        return;
      }
      beepError();
      showBanner('error', '❌ No va en este despacho', 'Código: ' + (id.length > 20 ? id.slice(0, 20) + '…' : id), r.mensaje || '');
    } catch (e) { if (noVisto) noEncontrado(id, 'Sin conexión para consultar la guía'); }
  }

  processScan = function (raw) {
    if (!state || !state.vivo) return interno(raw);
    var id = extractId(String(raw || ''));
    if (!id) return;
    if (diaVencido()) {
      beepError();
      showBanner('error', '⛔ Lista de otro día', 'Esta lista es del ' + ddmm(state.fecha), 'Finalizá el despacho y cargá la lista de hoy');
      return;
    }
    var c = buscar(state.canceladas, id);
    if (c) {
      beepError();
      showBanner('error', '⛔ VENTA CANCELADA — NO DESPACHAR', c.comprador || c.trackingId, 'Venta ' + (c.ventaId || c.trackingId));
      return;
    }
    var p = buscar(state.packages, id);
    if (p) return interno(norm(p.trackingId) === norm(id) ? raw : String(p.trackingId));
    if (state.dispatchType === 'tiendanube' && typeof findTnPackage === 'function' && findTnPackage(id)) return interno(raw);
    var ahora = Date.now();
    if (state.dispatchType === 'tiendanube') {
      // Etiqueta de SmartPost: el nº de guía no es el nº de orden → se consulta la guía y se busca la orden
      var g = state.spGuias && state.spGuias[norm(id)];
      if (g && buscar(state.packages, g)) return interno(String(g));
      if (id === ultimoFuera && ahora - ultimoFueraT < 4000) return;
      ultimoFuera = id; ultimoFueraT = ahora;
      showBanner('idle', '🔎 Buscando la guía…', 'Código: ' + (id.length > 20 ? id.slice(0, 20) + '…' : id), 'Consultando SmartPost y Tienda Nube');
      explicar(id, 'tiendanube', true);
      return;
    }
    interno(raw); // muestra "No encontrado"
    if (id === ultimoFuera && ahora - ultimoFueraT < 4000) return;
    ultimoFuera = id; ultimoFueraT = ahora;
    explicar(id, state.dispatchType);
  };
  window.processScan = processScan;

  // "Marcar salida" a mano también respeta el día activo
  if (typeof marcarManual === 'function') {
    var marcarInterno = marcarManual;
    marcarManual = function (id, silencioso) {
      if (diaVencido()) {
        if (!silencioso) { beepError(); showBanner('error', '⛔ Lista de otro día', 'Esta lista es del ' + ddmm(state.fecha), 'Cargá la lista de hoy'); }
        return false;
      }
      return marcarInterno.apply(this, arguments);
    };
    window.marcarManual = marcarManual;
  }

  // ───────────── 6b. SKU (y color / talle) en cada paquete ─────────────
  function lineasSku(pkg) {
    var its = (pkg.items || []).filter(function (i) { return i.sku || i.color || i.talle; });
    if (!its.length && pkg.sku) its = String(pkg.sku).split(/\s*,\s*/).map(function (s) { return { sku: s }; });
    if (!its.length) return '';
    return '<div style="margin-top:4px">' + its.map(function (i) {
      var extra = [i.color, i.talle].filter(Boolean).join(' · ');
      return '<p style="margin:2px 0 0;font:700 13px ui-monospace,Menlo,Consolas,monospace;color:#1e293b;word-break:break-all">' +
        '🏷️ ' + esc(i.sku || '(sin SKU)') + (i.cantidad > 1 ? ' <span style="color:#b45309">×' + i.cantidad + '</span>' : '') +
        (extra ? ' <span style="font:600 11px system-ui;color:#64748b">' + esc(extra) + '</span>' : '') + '</p>';
    }).join('') + '</div>';
  }
  var NUEVA = '<span style="display:inline-block;margin-left:6px;padding:1px 6px;border-radius:999px;background:#15803d;color:#fff;font:700 10px system-ui;vertical-align:middle">NUEVA</span>';
  if (typeof renderCard === 'function') {
    var cardInterno = renderCard;
    renderCard = function (pkg) {
      var h = cardInterno(pkg);
      var sk = lineasSku(pkg);
      if (sk) h = h.replace('<p class="text-slate-400 text-xs mt-1 font-mono">', sk + '<p class="text-slate-400 text-xs mt-1 font-mono">');
      if (pkg.nueva && pkg.estado !== 'despachado') h = h.replace(/(<p class="text-slate-800 font-semibold text-sm truncate">[^<]*)/, '$1' + NUEVA);
      return h;
    };
    window.renderCard = renderCard;
  }
  if (typeof renderTnCard === 'function') {
    var tnInterno = renderTnCard;
    renderTnCard = function (pkg) {
      var h = tnInterno(pkg);
      var sk = lineasSku(pkg);
      if (sk) h = h.replace(/(<div class="flex-1 min-w-0">\s*<p class="text-slate-800 font-semibold text-sm truncate">[^<]*<\/p>)/, '$1' + sk);
      if (pkg.nueva && pkg.estado !== 'despachado') h = h.replace(/(<p class="text-slate-800 font-semibold text-sm truncate">[^<]*)/, '$1' + NUEVA);
      return h;
    };
    window.renderTnCard = renderTnCard;
  }

  // Canceladas de hoy en un desplegable (cerrado salvo que haya una ya escaneada que hay que sacar)
  function desplegableCanceladas(lista, abierto) {
    return '<details class="mt-2"' + (abierto ? ' open' : '') + ' style="background:#fef2f2;border:1px solid #fca5a5;border-radius:12px;padding:8px 10px">' +
      '<summary style="cursor:pointer;color:#b91c1c;font:700 12px system-ui">⛔ ' + lista.length + ' cancelada' + (lista.length !== 1 ? 's' : '') +
      ' hoy · NO DESPACHAR <span style="font-weight:500;color:#dc2626">(tocá para ver)</span></summary>' +
      '<div style="margin-top:6px;max-height:220px;overflow-y:auto">' + lista.map(function (c) {
        return '<p style="margin:3px 0;color:#b91c1c;font:500 12px system-ui">' + esc(c.comprador || '(sin nombre)') +
          ' · venta ' + esc(c.ventaId || c.trackingId) + (c.yaEscaneada ? ' · <b>estaba escaneada: sacala</b>' : '') + '</p>';
      }).join('') + '</div></details>';
  }

  // ───────────── 6. Banda arriba de la lista: origen, día y canceladas ─────────────
  function pintarBanda() {
    var lp = document.getElementById('list-pending');
    if (!lp || !state || !state.vivo) return;
    var viejo = document.getElementById('pk-banda-vivo');
    if (viejo) viejo.remove();
    var canc = state.canceladas || [];
    var vencido = diaVencido();
    var html = '<div id="pk-banda-vivo" class="mx-3 mt-2">' +
      '<p class="text-[11px] ' + (vencido ? 'text-red-600 font-bold' : 'text-slate-500') + '">' +
      (vencido ? '⛔ Lista del ' + esc(ddmm(state.fecha)) + ': ya no se puede despachar con ella. Finalizá y cargá la de hoy.'
               : '📡 En vivo · ' + esc(ddmm(state.fecha)) + ' · las ventas nuevas se suman solas cada 2 min' +
                 (horaRevision ? ' (última revisión ' + esc(horaRevision) + ')' : '') +
                 ' · <a href="#" style="font-weight:700;text-decoration:underline" onclick="event.preventDefault(); pkBuscarNuevas()">⟳ Buscar ahora</a>') + '</p>';
    if (canc.length) html += desplegableCanceladas(canc, canc.some(function (x) { return x.yaEscaneada; }));
    html += '</div>';
    lp.insertAdjacentHTML('afterbegin', html);
  }
  // ───────────── 7. Lo que sale queda registrado en el servidor (compartido entre dispositivos) ─────────────
  var reportados = {};   // tracking → true (ya avisado al servidor en esta carga de la página)
  function sincronizarEscaneos() {
    if (!state || !state.vivo || !state.packages || state.fecha !== hoyLocal()) return;
    var tipo = state.dispatchType;
    state.packages.forEach(function (p) {
      var k = String(p.trackingId);
      if (p.estado === 'despachado' && !reportados[k]) {
        reportados[k] = true;
        postJSON('/despacho/api/vivo/escaneado', { tipo: tipo, tracking: k, venta: String(p.ventaId || ''), comprador: p.comprador || '' });
      } else if (p.estado !== 'despachado' && reportados[k]) {
        delete reportados[k];
        postJSON('/despacho/api/vivo/escaneado/quitar', { tipo: tipo, tracking: k });
      }
    });
  }
  var saveInterno = saveState;
  saveState = function () {
    var r = saveInterno.apply(this, arguments);
    try { sincronizarEscaneos(); } catch (e) {}
    return r;
  };
  window.saveState = saveState;

  var refreshInterno = refreshUI;
  refreshUI = function () {
    var r = refreshInterno.apply(this, arguments);
    try { pintarBanda(); } catch (e) {}
    return r;
  };
  window.refreshUI = refreshUI;

  // Al abrir con una lista en vivo ya cargada: revisar canceladas enseguida
  document.addEventListener('DOMContentLoaded', function () {
    montarRecuadro();
    setTimeout(function () { if (state && state.vivo) { try { sincronizarEscaneos(); } catch (e) {} ultimaLista = 0; revisar(false); pintarBanda(); } }, 1500);
  });
})();
