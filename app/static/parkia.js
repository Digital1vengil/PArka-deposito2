// ParkIA — asistente flotante. Usa la misma charla que /asistente (historial guardado por usuario),
// así que te acompaña de una pantalla a otra: si lo dejaste abierto, se vuelve a abrir en la siguiente.
const parkia = (function () {
  const KEY = 'pd_parkia_abierto', KEY_TXT = 'pd_parkia_borrador';
  const $ = id => document.getElementById(id);
  const panel = $('parkia'), fab = $('parkia-fab'), chat = $('parkia-chat'), txt = $('parkia-txt'), sugs = $('parkia-sugs');
  if (!panel || !fab) return {};
  if (location.pathname.startsWith('/asistente')) { fab.remove(); panel.remove(); return {}; }  // ya estás en la pantalla grande
  let cargado = false, ocupado = false, confirmar = 0;
  const ss = { get: k => { try { return sessionStorage.getItem(k); } catch { return null; } },
               set: (k, v) => { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch {} } };

  function md(t) {
    let h = esc(t);
    h = h.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>');
    h = h.replace(/^(\s*)[-*] (.+)$/gm, '$1• $2');
    return h.replace(/\n/g, '<br>');
  }
  function burbuja(m) {
    const yo = m.rol === 'user';
    const d = document.createElement('div');
    d.className = 'parkia-msg ' + (yo ? 'yo' : 'ia');
    d.innerHTML = md(m.texto);
    if (!yo && !m.temporal) {
      const pie = document.createElement('div');
      pie.className = 'mt-1.5 flex gap-2 items-center text-[11px] text-zinc-500';
      pie.innerHTML = (m.consultas ? `<span class="truncate">🔎 ${esc(m.consultas)}</span>` : '') + '<button type="button" class="underline ml-auto">Copiar</button>';
      pie.querySelector('button').onclick = () => { navigator.clipboard.writeText(m.texto); toast('Copiado'); };
      d.appendChild(pie);
    }
    chat.appendChild(d);
    chat.scrollTop = chat.scrollHeight;
    return d;
  }
  function bienvenida() {
    burbuja({ rol: 'model', temporal: true, texto: '¡Hola! Soy **ParkIA**. Preguntame por una venta, un MLA, preguntas sin responder, devoluciones o cómo viene el día.' });
  }
  async function cargar() {
    try {
      const d = await api('/asistente/api/historial');
      chat.innerHTML = '';
      if (!d.mensajes.length) bienvenida();
      d.mensajes.forEach(burbuja);
      sugs.classList.toggle('hidden', d.mensajes.length > 0);
      cargado = true;
    } catch (e) { chat.innerHTML = ''; burbuja({ rol: 'model', temporal: true, texto: '⚠️ ' + e.message }); }
  }
  function abrir() {
    panel.classList.remove('hidden'); fab.classList.add('abierto'); document.body.classList.add('parkia-on');
    fab.setAttribute('aria-label', 'Cerrar ParkIA'); ss.set(KEY, '1');
    if (!cargado) cargar(); else chat.scrollTop = chat.scrollHeight;
    if (window.matchMedia('(min-width: 640px)').matches) txt.focus();
  }
  function cerrar() {
    panel.classList.add('hidden'); fab.classList.remove('abierto'); document.body.classList.remove('parkia-on');
    fab.setAttribute('aria-label', 'Abrir ParkIA'); ss.set(KEY, null);
  }
  const toggle = () => panel.classList.contains('hidden') ? abrir() : cerrar();

  async function enviar() {
    const texto = txt.value.trim();
    if (!texto || ocupado) return;
    ocupado = true; $('parkia-env').disabled = true;
    txt.value = ''; ss.set(KEY_TXT, null);
    sugs.classList.add('hidden');
    chat.querySelectorAll('.parkia-msg.ia:not(:has(button))').forEach(n => n.remove());  // saca la bienvenida
    burbuja({ rol: 'user', texto });
    const pensando = burbuja({ rol: 'model', temporal: true, texto: 'Pensando…' });
    try {
      const r = await api('/asistente/api/mensaje', { json: { texto } });
      pensando.remove();
      burbuja({ rol: 'model', texto: r.texto, consultas: r.consultas });
    } catch (e) {
      pensando.remove();
      burbuja({ rol: 'model', temporal: true, texto: '⚠️ ' + e.message });
      txt.value = texto;
    }
    ocupado = false; $('parkia-env').disabled = false; txt.focus();
  }
  async function borrar(b) {  // doble toque, sin confirm()
    if (Date.now() - confirmar > 4000) { confirmar = Date.now(); b.textContent = '¿Seguro?'; b.style.width = 'auto'; b.style.padding = '0 .4rem';
      setTimeout(() => { b.textContent = '🗑'; b.style.width = b.style.padding = ''; }, 4000); return; }
    confirmar = 0; b.textContent = '🗑'; b.style.width = b.style.padding = '';
    try { await api('/asistente/api/borrar', { method: 'POST' }); cargar(); } catch (e) { toast(e.message, 'error'); }
  }

  sugs.querySelectorAll('.parkia-sug').forEach(s => s.onclick = () => { txt.value = s.textContent.trim(); txt.focus(); });
  txt.addEventListener('input', () => ss.set(KEY_TXT, txt.value || null));
  txt.value = ss.get(KEY_TXT) || '';
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !panel.classList.contains('hidden')) cerrar(); });
  if (ss.get(KEY) === '1') abrir();
  return { abrir, cerrar, toggle, enviar, borrar };
})();
