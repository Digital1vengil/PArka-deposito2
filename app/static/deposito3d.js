// Visor 3D del depósito MAGONTEX (Mapeo y ruta de Devoluciones). Necesita three.js r128 + OrbitControls.
// const v = Deposito3D(contenedor, plano, { cuenta(r,i,n), onModulo(r,i,n), onPiso(x,z), alMoverYo(x,z,txt) })
// Metros: x de norte (0) a sur (60), z de oeste (0, persianas) a este (10,40). El monigote es "dónde estás vos":
// se comparte entre pantallas del mismo equipo (localStorage).
(function () {
  const NIV = ['Planta baja', '1er nivel', '2do nivel'];
  const ZCOL = { A: '#2f7f8a', B: '#a8445f', C: '#b06a2a' };
  const VISTA = { T: [-1.2, 61.2], A: [-1.2, 23.5], B: [20.0, 46.5], C: [43.5, 61.2] };
  const SEP = 0.6, CLAVE_YO = 'mapeo_yo';
  const pad2 = i => (i < 10 ? '0' : '') + i;

  window.Deposito3D = function (visor, plano, o = {}) {
    const DEP = plano.deposito, RACKS = DEP.racks, S = plano.escala;
    const rk = c => RACKS.find(r => r.c === c) || null;
    const antes = r => r.v.indexOf('s/n') === 0 ? 'sin número (' + r.v.slice(4) + ')' : 'antes ' + r.v;
    const codMod = (r, i, n) => r.c + (i ? '-C' + pad2(i) : '') + (i && n != null ? '-N' + n : '');
    function partes(cod) {
      const m = String(cod || '').toUpperCase().replace(/\s+/g, '').match(/^([A-Z])(\d{1,2})(?:-?C(\d{1,2}))?(?:-?N(\d))?$/);
      if (!m) return null;
      const r = rk(m[1] + pad2(+m[2])); if (!r) return null;
      const i = m[3] ? +m[3] : null, n = m[4] != null ? +m[4] : null;
      if ((i != null && (i < 1 || i > r.n)) || (n != null && (n > 2 || !i))) return null;
      return { r, i, n, codigo: codMod(r, i, n) };
    }
    const altos = (r, i) => { const d = DEP.distintas[r.c] || {}, k = 'C' + pad2(i) + '-N'; return [0, 1, 2].map(n => d[k + n + '_a'] ?? r.al[n]); };
    const fondo = (r, i, n) => (DEP.distintas[r.c] || {})['C' + pad2(i) + '-N' + n + '_p'] ?? Math.round(r.prof * 100);
    const W = (px, pz) => [px - DEP.nave.L / 2, DEP.nave.W / 2 - pz];
    const deMundo = (wx, wz) => [wx + DEP.nave.L / 2, DEP.nave.W / 2 - wz];
    const rect = (r, s, e, c0, c1) => r.d === 'x' ? { x0: s, x1: e, z0: c0, z1: c1 } : { x0: c0, x1: c1, z0: s, z1: e };
    const colRect = (r, i) => { const p = r.len / r.n, s = r.a0 + p * (i - 1); return rect(r, s, s + p, r.c0, r.c0 + r.prof); };
    const rackAlto = r => { let m = 0; for (let i = 1; i <= r.n; i++) { const h = altos(r, i); m = Math.max(m, h[0] + h[1] + h[2]); } return m / 100; };
    const niveles = (r, i) => { const h = altos(r, i); return [h[0] / 100, (h[0] + h[1]) / 100, (h[0] + h[1] + h[2]) / 100]; };
    function accesoM(r, i) {   // punto del pasillo desde donde se carga (igual que el servidor)
      const paso = r.len / r.n, medio = r.a0 + (i ? (i - 0.5) * paso : r.len / 2), f = rect(r, r.a0, r.a0 + r.len, r.c0, r.c0 + r.prof);
      return r.d === 'x' ? [medio, r.acc === 'este' ? f.z1 + SEP : f.z0 - SEP] : [r.acc === 'sur' ? f.x1 + SEP : f.x0 - SEP, medio];
    }
    // puntos del plano ↔ metros (los caminos del servidor vienen en puntos)
    const aPuntos = (x, z) => [x * S, (DEP.nave.W - z) * S];
    const dePuntos = p => [p[0] / S, DEP.nave.W - p[1] / S];
    const cuenta = o.cuenta || (() => 0);

    const api = { ok: false, DEP, RACKS, NIV, ZCOL, rk, antes, codMod, partes, altos, fondo, colRect, rackAlto, accesoM, aPuntos, dePuntos };
    if (!window.THREE || !THREE.OrbitControls) {
      const c = visor.querySelector('.m3d-cargando'); if (c) c.textContent = 'No se pudo cargar el visor 3D. Recargá la página.';
      return api;
    }

    /* ===== Escena ===== */
    const estado = { zona: 'T', vista: o.vista || 'iso', sel: null, mover: false, yo: null, yoTxt: '' };
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.outputEncoding = THREE.sRGBEncoding;
    visor.insertBefore(renderer.domElement, visor.firstChild);
    const scene = new THREE.Scene(); scene.background = new THREE.Color('#dfe5ec');
    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 500);
    const controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.dampingFactor = 0.09;
    controls.maxPolarAngle = Math.PI / 2 - 0.04; controls.minDistance = 3; controls.maxDistance = 140;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x9aa0a8, 0.75));
    const sol = new THREE.DirectionalLight(0xffffff, 0.75);
    sol.position.set(-18, 30, 22); sol.castShadow = true; sol.shadow.mapSize.set(2048, 2048);
    Object.assign(sol.shadow.camera, { left: -34, right: 34, top: 20, bottom: -20, near: 1, far: 90 });
    sol.shadow.bias = -0.0005; scene.add(sol);
    let piso, grupoCajas = null, grupoNombres, grupoCamino = null, grupoParadas = null, destino = null, moni, moniAnillo;
    const hitMeshes = [];

    function caja(x0, x1, y0, y1, z0, z1, mat) {
      const a = W((x0 + x1) / 2, (z0 + z1) / 2);
      const m = new THREE.Mesh(new THREE.BoxGeometry(Math.abs(x1 - x0), y1 - y0, Math.abs(z1 - z0)), mat);
      m.position.set(a[0], (y0 + y1) / 2, a[1]); return m;
    }
    function instancias(lista, mat, sombra) {
      const m = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), mat, Math.max(lista.length, 1)), ob = new THREE.Object3D();
      m.count = lista.length;
      lista.forEach((b, i) => {
        const a = W((b.x0 + b.x1) / 2, (b.z0 + b.z1) / 2);
        ob.position.set(a[0], (b.y0 + b.y1) / 2, a[1]); ob.scale.set(Math.abs(b.x1 - b.x0), b.y1 - b.y0, Math.abs(b.z1 - b.z0));
        ob.updateMatrix(); m.setMatrixAt(i, ob.matrix); if (b.col) m.setColorAt(i, b.col);
      });
      if (m.instanceColor) m.instanceColor.needsUpdate = true;
      m.castShadow = sombra; m.receiveShadow = true; return m;
    }
    function semilla(str) { let s = 2166136261; for (let i = 0; i < str.length; i++) { s ^= str.charCodeAt(i); s = Math.imul(s, 16777619) >>> 0; }
      return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; }; }

    function construirNave() {
      const g = new THREE.Group(), L = DEP.nave.L, WW = DEP.nave.W, HP = 6.2;
      const losa = caja(-0.5, L + 0.5, -0.25, -0.01, -0.5, WW + 0.5, new THREE.MeshStandardMaterial({ color: 0x9aa1aa, roughness: 1 }));
      losa.receiveShadow = true; g.add(losa);
      piso = new THREE.Mesh(new THREE.PlaneGeometry(L, WW), new THREE.MeshStandardMaterial({ color: 0xe9e7e1, roughness: 0.95 }));
      piso.rotation.x = -Math.PI / 2; piso.receiveShadow = true; g.add(piso);
      const vidrio = new THREE.MeshStandardMaterial({ color: 0x5b8fd6, transparent: true, opacity: 0.16, side: THREE.DoubleSide, depthWrite: false });
      const borde = new THREE.MeshStandardMaterial({ color: 0x3d6fb8, transparent: true, opacity: 0.55, depthWrite: false });
      const pared = (x0, x1, z0, z1) => { const p = caja(x0, x1, 0, HP, z0, z1, vidrio); p.renderOrder = 2; g.add(p); const b = caja(x0, x1, HP - 0.08, HP, z0, z1, borde); b.renderOrder = 3; g.add(b); };
      const PERS = [...DEP.persianas].sort((a, b) => a.x0 - b.x0);
      const cortes = [0].concat(PERS.reduce((a, p) => a.concat([p.x0, p.x1]), [])).concat([L]);
      for (let k = 0; k < cortes.length; k += 2) pared(cortes[k], cortes[k + 1], -0.12, 0);
      pared(0, L, WW, WW + 0.12); pared(-0.12, 0, -0.12, WW + 0.12); pared(L, L + 0.12, -0.12, WW + 0.12);
      PERS.forEach(p => {
        const d = caja(p.x0, p.x1, 3.9, HP, -0.12, 0, vidrio); d.renderOrder = 2; g.add(d);
        const cv = document.createElement('canvas'); cv.width = cv.height = 256; const c = cv.getContext('2d');
        c.fillStyle = '#8a929b'; c.fillRect(0, 0, 256, 256); for (let y = 0; y < 256; y += 12) { c.fillStyle = '#6f7780'; c.fillRect(0, y, 256, 3); }
        const tx = new THREE.CanvasTexture(cv); tx.encoding = THREE.sRGBEncoding;
        g.add(caja(p.x0, p.x1, 0, 3.9, -0.1, -0.04, new THREE.MeshStandardMaterial({ map: tx, roughness: 0.6, metalness: 0.3 })));
        const marco = new THREE.MeshStandardMaterial({ color: 0x2b2f35 });
        g.add(caja(p.x0 - 0.12, p.x0, 0, 4.0, -0.16, 0.02, marco)); g.add(caja(p.x1, p.x1 + 0.12, 0, 4.0, -0.16, 0.02, marco));
        g.add(caja(p.x0 - 0.12, p.x1 + 0.12, 3.9, 4.05, -0.16, 0.02, marco));
        g.add(caja(p.x0, p.x1, 0.003, 0.012, 0.15, 0.55, new THREE.MeshBasicMaterial({ color: 0xf2b705 })));
      });
      DEP.sin_confirmar.forEach(d => {
        const pts = [[d.x0, d.z0], [d.x1, d.z0], [d.x1, d.z1], [d.x0, d.z1], [d.x0, d.z0]].map(p => { const a = W(p[0], p[1]); return new THREE.Vector3(a[0], 0.03, a[1]); });
        const l = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineDashedMaterial({ color: 0xd23030, dashSize: 0.25, gapSize: 0.18 }));
        l.computeLineDistances(); g.add(l);
      });
      scene.add(g);
    }

    function construirRacks() {
      const post = [], larg = [], band = [], franja = [];
      const hitMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false });
      RACKS.forEach(r => {
        const paso = r.len / r.n, c0 = r.c0, c1 = r.c0 + r.prof, hs = [];
        for (let i = 1; i <= r.n; i++) hs.push(altos(r, i));
        for (let k = 0; k <= r.n; k++) {
          const a = r.a0 + paso * k, s = k === 0 ? a : (k === r.n ? a - 0.08 : a - 0.04);
          const hI = k > 0 ? hs[k - 1] : null, hD = k < r.n ? hs[k] : null;
          const hk = Math.max(hI ? hI[0] + hI[1] + hI[2] : 0, hD ? hD[0] + hD[1] + hD[2] : 0) / 100 + 0.06;
          [c0, c1 - 0.08].forEach(cf => { const q = rect(r, s, s + 0.08, cf, cf + 0.08); q.y0 = 0; q.y1 = hk; post.push(q); });
          [0.12, hk - 0.06].forEach(y => { const q = rect(r, s + 0.02, s + 0.06, c0 + 0.08, c1 - 0.08); q.y0 = y - 0.03; q.y1 = y + 0.03; post.push(q); });
        }
        for (let i = 1; i <= r.n; i++) {
          const s = r.a0 + paso * (i - 1), e = s + paso, ys = niveles(r, i);
          ys.forEach(y => [c0, c1 - 0.05].forEach(cf => { const q = rect(r, s, e, cf, cf + 0.05); q.y0 = y - 0.12; q.y1 = y; larg.push(q); }));
          [ys[0], ys[1]].forEach(y => { const q = rect(r, s + 0.04, e - 0.04, c0 + 0.05, c1 - 0.05); q.y0 = y - 0.03; q.y1 = y - 0.01; band.push(q); });
          const cr = colRect(r, i), hm = caja(cr.x0, cr.x1, 0, ys[2] + 0.08, cr.z0, cr.z1, hitMat);
          hm.userData = { r, i, ys }; hitMeshes.push(hm); scene.add(hm);
        }
        const f = rect(r, r.a0, r.a0 + r.len, c0, c1), b = 0.12, t = 0.06, Y0 = 0.004, Y1 = 0.01;
        franja.push({ x0: f.x0 - b, x1: f.x1 + b, z0: f.z0 - b, z1: f.z0 - b + t, y0: Y0, y1: Y1 }, { x0: f.x0 - b, x1: f.x1 + b, z0: f.z1 + b - t, z1: f.z1 + b, y0: Y0, y1: Y1 },
          { x0: f.x0 - b, x1: f.x0 - b + t, z0: f.z0 - b, z1: f.z1 + b, y0: Y0, y1: Y1 }, { x0: f.x1 + b - t, x1: f.x1 + b, z0: f.z0 - b, z1: f.z1 + b, y0: Y0, y1: Y1 });
      });
      scene.add(instancias(post, new THREE.MeshStandardMaterial({ color: 0x2a5ea8, roughness: 0.5, metalness: 0.35 }), true));
      scene.add(instancias(larg, new THREE.MeshStandardMaterial({ color: 0xe8742a, roughness: 0.5, metalness: 0.25 }), true));
      scene.add(instancias(band, new THREE.MeshStandardMaterial({ color: 0xb8bec6, roughness: 0.7, metalness: 0.4 }), true));
      scene.add(instancias(franja, new THREE.MeshBasicMaterial({ color: 0xf2b705 }), false));
    }

    /* cajas solo en los módulos con artículos (el volumen es ilustrativo) */
    function construirCajas() {
      const visible = grupoCajas ? grupoCajas.visible : true;
      if (grupoCajas) { scene.remove(grupoCajas); grupoCajas.geometry.dispose(); }
      const cajas = [];
      RACKS.forEach(r => {
        const paso = r.len / r.n;
        for (let i = 1; i <= r.n; i++) {
          const s = r.a0 + paso * (i - 1), ys = niveles(r, i), base = [0.02, ys[0], ys[1]], libre = [ys[0] - 0.16, ys[1] - ys[0] - 0.16, ys[2] - ys[1] - 0.16];
          for (let n = 0; n < 3; n++) {
            if (!cuenta(r, i, n)) continue;
            const rnd = semilla(r.c + '-' + i + '-' + n), lleno = 0.45 + rnd() * 0.55;
            const largoU = paso - 0.14, fondoU = r.prof - 0.12;
            const nx = Math.max(1, Math.floor(largoU / 0.6)), nz = Math.max(1, Math.floor(fondoU / 0.5));
            const bw = largoU / nx, bd = fondoU / nz, bh = 0.36 + rnd() * 0.1, ns = Math.max(1, Math.floor(libre[n] / (bh + 0.015)));
            for (let ix = 0; ix < nx; ix++) for (let iz = 0; iz < nz; iz++) {
              if (rnd() > lleno) continue;
              const pila = Math.min(ns, 1 + Math.floor(rnd() * ns));
              for (let p = 0; p < pila; p++) {
                const sa = s + 0.07 + ix * bw, sc = r.c0 + 0.06 + iz * bd, q = rect(r, sa + 0.02, sa + bw - 0.02, sc + 0.02, sc + bd - 0.02);
                q.y0 = base[n] + p * (bh + 0.012); q.y1 = q.y0 + bh;
                const col = new THREE.Color(); col.setHSL(0.085 + rnd() * 0.02, 0.42 + rnd() * 0.12, 0.56 + rnd() * 0.1); q.col = col; cajas.push(q);
              }
            }
          }
        }
      });
      grupoCajas = instancias(cajas, new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.9 }), true);
      grupoCajas.visible = visible; scene.add(grupoCajas);
    }

    function cartel(lineas, fondoCol, ancho) {
      const cv = document.createElement('canvas'), c = cv.getContext('2d'), W2 = ancho || 512, H = lineas.length > 1 ? 210 : 130;
      cv.width = W2; cv.height = H; c.fillStyle = fondoCol;
      const rr = 26; c.beginPath(); c.moveTo(rr, 0); c.lineTo(W2 - rr, 0); c.quadraticCurveTo(W2, 0, W2, rr); c.lineTo(W2, H - rr); c.quadraticCurveTo(W2, H, W2 - rr, H);
      c.lineTo(rr, H); c.quadraticCurveTo(0, H, 0, H - rr); c.lineTo(0, rr); c.quadraticCurveTo(0, 0, rr, 0); c.fill();
      c.fillStyle = '#fff'; c.textAlign = 'center'; c.textBaseline = 'middle';
      c.font = '700 104px Inter, "Arial Narrow", sans-serif'; c.fillText(lineas[0], W2 / 2, lineas.length > 1 ? 78 : H / 2 + 4);
      if (lineas[1]) { c.font = '600 40px Inter, system-ui, sans-serif'; c.globalAlpha = 0.92; c.fillText(lineas[1], W2 / 2, 166); }
      const tx = new THREE.CanvasTexture(cv); tx.encoding = THREE.sRGBEncoding; tx.anisotropy = 4;
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tx, depthTest: false, transparent: true }));
      sp.renderOrder = 20; sp.userData.ratio = W2 / H; return sp;
    }
    function construirNombres() {
      grupoNombres = new THREE.Group();
      RACKS.forEach(r => {
        const f = rect(r, r.a0, r.a0 + r.len, r.c0, r.c0 + r.prof), a = W((f.x0 + f.x1) / 2, (f.z0 + f.z1) / 2);
        const sp = cartel([r.c, antes(r)], ZCOL[r.z]);
        sp.position.set(a[0], rackAlto(r) + 0.9 + (r.lug.indexOf('cara este') >= 0 ? 1.25 : 0), a[1]); sp.scale.set(2.8, 2.8 / sp.userData.ratio, 1);
        grupoNombres.add(sp);
      });
      DEP.persianas.forEach(p => { const a = W((p.x0 + p.x1) / 2, -0.3), sp = cartel([p.n], '#4b525b'); sp.position.set(a[0], 4.7, a[1]); sp.scale.set(1.9, 1.9 / sp.userData.ratio, 1); grupoNombres.add(sp); });
      DEP.sin_confirmar.forEach(d => { const a = W((d.x0 + d.x1) / 2, (d.z0 + d.z1) / 2), sp = cartel([d.t], '#c22d2d', 900); sp.position.set(a[0], 0.6, a[1]); sp.scale.set(2.6, 2.6 / sp.userData.ratio, 1); grupoNombres.add(sp); });
      [['NORTE', -1.6], ['SUR', DEP.nave.L + 1.6]].forEach(t => { const a = W(t[1], DEP.nave.W / 2), sp = cartel([t[0]], '#39414b'); sp.position.set(a[0], 0.7, a[1]); sp.scale.set(1.8, 1.8 / sp.userData.ratio, 1); grupoNombres.add(sp); });
      [['OESTE', -1.4], ['ESTE', DEP.nave.W + 1.4]].forEach(t => { const a = W(DEP.nave.L / 2, t[1]), sp = cartel([t[0]], '#39414b', 440); sp.position.set(a[0], 0.5, a[1]); sp.scale.set(1.7, 1.7 / sp.userData.ratio, 1); grupoNombres.add(sp); });
      scene.add(grupoNombres);
    }

    /* ===== Selección: rack, columna y el nivel exacto ===== */
    const selRackLines = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1)), new THREE.LineBasicMaterial({ color: 0x14b8e6 }));
    const selColMesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial({ color: 0x14b8e6, transparent: true, opacity: 0.22, depthWrite: false }));
    const selModMesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial({ color: 0x14b8e6, transparent: true, opacity: 0.5, depthWrite: false }));
    selColMesh.renderOrder = 5; selModMesh.renderOrder = 6;
    [selRackLines, selColMesh, selModMesh].forEach(m => { m.visible = false; scene.add(m); });
    function ubicar(m, x0, x1, y0, y1, z0, z1) { const a = W((x0 + x1) / 2, (z0 + z1) / 2); m.position.set(a[0], (y0 + y1) / 2, a[1]); m.scale.set(Math.abs(x1 - x0), y1 - y0, Math.abs(z1 - z0)); m.visible = true; }
    function resaltar(r, i, n, color) {
      estado.sel = r ? { r, i: i || null, n: i && n != null ? n : null } : null;
      [selRackLines, selColMesh, selModMesh].forEach(m => { m.visible = false; if (color) m.material.color.set(color); });
      if (!r) return;
      const f = rect(r, r.a0, r.a0 + r.len, r.c0, r.c0 + r.prof), pad = 0.08;
      ubicar(selRackLines, f.x0 - pad, f.x1 + pad, 0, rackAlto(r) + 0.12, f.z0 - pad, f.z1 + pad);
      if (!i) return;
      const c = colRect(r, i), ys = [0].concat(niveles(r, i));
      ubicar(selColMesh, c.x0 + 0.02, c.x1 - 0.02, 0.01, ys[3] + 0.05, c.z0 - 0.03, c.z1 + 0.03);
      if (n != null) ubicar(selModMesh, c.x0 + 0.03, c.x1 - 0.03, ys[n] + 0.01, ys[n + 1] - 0.01, c.z0 - 0.05, c.z1 + 0.05);
    }

    /* ===== Monigote ===== */
    function construirMonigote() {
      moni = new THREE.Group();
      const piel = new THREE.MeshStandardMaterial({ color: 0xf1c27d, roughness: 0.7 });
      const ropa = new THREE.MeshStandardMaterial({ color: 0xea580c, roughness: 0.6 });
      const pant = new THREE.MeshStandardMaterial({ color: 0x1f2937, roughness: 0.8 });
      const parte = (geo, mat, x, y, z) => { const m = new THREE.Mesh(geo, mat); m.position.set(x, y, z); m.castShadow = true; moni.add(m); return m; };
      parte(new THREE.CylinderGeometry(0.09, 0.08, 0.8, 12), pant, 0, 0.4, -0.11); parte(new THREE.CylinderGeometry(0.09, 0.08, 0.8, 12), pant, 0, 0.4, 0.11);
      parte(new THREE.CylinderGeometry(0.24, 0.2, 0.7, 16), ropa, 0, 1.15, 0);
      parte(new THREE.CylinderGeometry(0.06, 0.06, 0.62, 10), ropa, 0, 1.12, -0.31); parte(new THREE.CylinderGeometry(0.06, 0.06, 0.62, 10), ropa, 0, 1.12, 0.31);
      parte(new THREE.SphereGeometry(0.19, 20, 16), piel, 0, 1.72, 0);
      const pin = parte(new THREE.ConeGeometry(0.22, 0.45, 18), ropa, 0, 2.45, 0); pin.rotation.x = Math.PI;   // flecha arriba: se ve de lejos
      moniAnillo = new THREE.Mesh(new THREE.RingGeometry(0.45, 0.62, 40), new THREE.MeshBasicMaterial({ color: 0xea580c, transparent: true, opacity: 0.6, side: THREE.DoubleSide, depthWrite: false }));
      moniAnillo.rotation.x = -Math.PI / 2; moniAnillo.position.y = 0.02; moni.add(moniAnillo);
      moni.visible = false; scene.add(moni);
    }
    function latir(t) {
      if (moni && moni.visible) { const k = (t / 1000) % 1.4 / 1.4; moniAnillo.scale.setScalar(1 + k * 1.2); moniAnillo.material.opacity = 0.6 * (1 - k); }
      if (destino) destino.position.y = destino.userData.y + Math.sin(t / 260) * 0.12;
    }
    function ponerYo(x, z, txt, guardar = true) {
      estado.yo = [Math.min(Math.max(x, 0.2), DEP.nave.L - 0.2), Math.min(Math.max(z, 0.2), DEP.nave.W - 0.2)]; estado.yoTxt = txt;
      const a = W(estado.yo[0], estado.yo[1]); moni.position.set(a[0], 0, a[1]); moni.visible = true;
      if (guardar) try { localStorage.setItem(CLAVE_YO, JSON.stringify({ x: estado.yo[0], z: estado.yo[1], txt })); } catch {}
      if (o.alMoverYo) o.alMoverYo(estado.yo[0], estado.yo[1], txt);
    }
    function yoEnPersiana(k) { const p = DEP.persianas.find(x => x.clave === k) || DEP.persianas[0]; ponerYo((p.x0 + p.x1) / 2, SEP, p.n); }
    function yoEnCodigo(cod) { const p = partes(cod); if (!p) return false; const a = accesoM(p.r, p.i); ponerYo(a[0], a[1], p.codigo); return true; }
    function yoGuardado(persiana) {
      let g = null; try { g = JSON.parse(localStorage.getItem(CLAVE_YO) || 'null'); } catch {}
      if (g && isFinite(g.x) && isFinite(g.z)) ponerYo(g.x, g.z, g.txt || 'tu última ubicación', false); else yoEnPersiana(persiana || 'P1');
    }

    /* ===== Camino (puntos del plano, como lo da el servidor) y paradas ===== */
    function camino(tramo, cod) {
      if (grupoCamino) scene.remove(grupoCamino);
      grupoCamino = new THREE.Group(); destino = null;
      const mat = new THREE.MeshBasicMaterial({ color: 0xea580c, transparent: true, opacity: 0.85, depthWrite: false });
      const pts = (tramo || []).map(dePuntos);
      if (pts.length && estado.yo) pts.unshift(estado.yo);
      for (let k = 1; k < pts.length; k++) {   // tramos rectos: una franja en el piso por tramo
        const [ax, az] = pts[k - 1], [bx, bz] = pts[k], t = 0.11;
        const f = caja(Math.min(ax, bx) - t, Math.max(ax, bx) + t, 0.015, 0.03, Math.min(az, bz) - t, Math.max(az, bz) + t, mat);
        f.renderOrder = 4; grupoCamino.add(f);
      }
      const p = partes(cod);
      if (p) {
        const r = p.r, i = p.i || Math.ceil(r.n / 2), c = colRect(r, i), alto = (p.n != null ? niveles(r, i)[p.n] : rackAlto(r)) + 0.55;
        destino = new THREE.Mesh(new THREE.ConeGeometry(0.28, 0.6, 18), new THREE.MeshStandardMaterial({ color: 0xea580c, emissive: 0x7a2a00 }));
        destino.rotation.x = Math.PI; const a = W((c.x0 + c.x1) / 2, (c.z0 + c.z1) / 2); destino.position.set(a[0], alto, a[1]); destino.userData.y = alto;
        grupoCamino.add(destino);
      }
      scene.add(grupoCamino);
    }
    function borrarCamino() { if (grupoCamino) scene.remove(grupoCamino); grupoCamino = null; destino = null; }
    // paradas: [{codigo, n, estado: 'actual' | 'hecha' | 'pendiente' | 'saltada'}] → número arriba de su módulo
    function paradas(lista) {
      if (grupoParadas) scene.remove(grupoParadas);
      grupoParadas = new THREE.Group();
      const COL = { actual: '#ea580c', hecha: '#16a34a', pendiente: '#18181b', saltada: '#a1a1aa' };
      (lista || []).forEach(x => {
        const p = partes(x.codigo); if (!p) return;
        const r = p.r, i = p.i || Math.ceil(r.n / 2), c = colRect(r, i), a = W((c.x0 + c.x1) / 2, (c.z0 + c.z1) / 2);
        const sp = cartel([x.estado === 'hecha' ? '✓' : String(x.n)], COL[x.estado] || COL.pendiente, 200);
        sp.position.set(a[0], rackAlto(r) + (x.estado === 'actual' ? 1.5 : 0.6), a[1]); const k = x.estado === 'actual' ? 0.9 : 0.6;
        sp.scale.set(k, k / sp.userData.ratio, 1); grupoParadas.add(sp);
      });
      scene.add(grupoParadas);
    }

    /* ===== Cámara ===== */
    let anim = null;
    const reducido = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    function volarA(obj, pos) { if (reducido) { controls.target.copy(obj); camera.position.copy(pos); return; } anim = { t0: performance.now(), dur: 750, fT: controls.target.clone(), fP: camera.position.clone(), tT: obj, tP: pos }; }
    function tween() { if (!anim) return; const k = Math.min(1, (performance.now() - anim.t0) / anim.dur), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      controls.target.lerpVectors(anim.fT, anim.tT, e); camera.position.lerpVectors(anim.fP, anim.tP, e); if (k >= 1) anim = null; }
    const direccion = () => estado.vista === 'planta' ? new THREE.Vector3(0, 1, 0.02).normalize() : new THREE.Vector3(-0.32, 0.78, 0.62).normalize();
    function encuadre(x0, x1, alto, inmediato) {
      const span = x1 - x0, cx = (x0 + x1) / 2 - DEP.nave.L / 2, asp = camera.aspect || 1.6;
      const dist = Math.max(9, span * (estado.vista === 'planta' ? 1.0 : 0.82) / Math.min(1, asp / 1.7));
      const t = new THREE.Vector3(cx, alto || 0.8, 0), p = t.clone().add(direccion().multiplyScalar(dist));
      if (inmediato) { controls.target.copy(t); camera.position.copy(p); } else volarA(t, p);
    }
    function irZona(z, inmediato) { estado.zona = z; const v = VISTA[z] || VISTA.T; encuadre(v[0], v[1], 0.8, inmediato); }
    function irRack(r) {
      const f = rect(r, r.a0, r.a0 + r.len, r.c0, r.c0 + r.prof), a = W((f.x0 + f.x1) / 2, (f.z0 + f.z1) / 2);
      const dist = Math.max(10, Math.max(f.x1 - f.x0, f.z1 - f.z0) * 1.25 + 6), t = new THREE.Vector3(a[0], 1.6, a[1]);
      volarA(t, t.clone().add(direccion().multiplyScalar(dist)));
    }
    // encuadra el monigote y el destino juntos
    function irTramo(cod) {
      const p = partes(cod); if (!p) return;
      const f = colRect(p.r, p.i || Math.ceil(p.r.n / 2)), xs = [f.x0, f.x1].concat(estado.yo ? [estado.yo[0]] : []);
      const x0 = Math.min(...xs) - 3, x1 = Math.max(...xs) + 3; encuadre(x0, x1, 0.8);
    }
    function vista(v) { estado.vista = v; if (estado.sel) irRack(estado.sel.r); else irZona(estado.zona); }

    /* ===== Puntero: tocar un módulo o, con "Estoy acá", el piso ===== */
    const ray = new THREE.Raycaster(), v2 = new THREE.Vector2(); let ini = null;
    const el = renderer.domElement;
    el.addEventListener('pointerdown', e => { ini = { x: e.clientX, y: e.clientY }; });
    el.addEventListener('pointerup', e => {
      if (!ini) return; const dx = e.clientX - ini.x, dy = e.clientY - ini.y; ini = null;
      if (dx * dx + dy * dy > 36) return;
      const b = el.getBoundingClientRect(); v2.set(((e.clientX - b.left) / b.width) * 2 - 1, -((e.clientY - b.top) / b.height) * 2 + 1);
      ray.setFromCamera(v2, camera);
      if (estado.mover) {
        const h = ray.intersectObject(piso, false)[0]; if (!h) return;
        const m = deMundo(h.point.x, h.point.z); mover(false); ponerYo(m[0], m[1], 'el punto que marcaste');
        if (o.onPiso) o.onPiso(m[0], m[1]); return;
      }
      const hit = ray.intersectObjects(hitMeshes, false)[0];
      if (!o.onModulo) return;
      if (!hit) { o.onModulo(null); return; }
      const d = hit.object.userData, y = hit.point.y; o.onModulo(d.r, d.i, y < d.ys[0] ? 0 : (y < d.ys[1] ? 1 : 2));
    });
    function mover(on) { estado.mover = on; visor.classList.toggle('mover', on); if (o.alMover) o.alMover(on); }

    function ajustar() { const w = visor.clientWidth, h = visor.clientHeight; if (!w || !h) return; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); }

    construirNave(); construirRacks(); construirCajas(); construirMonigote(); construirNombres();
    ajustar(); new ResizeObserver(ajustar).observe(visor);
    irZona('T', true);
    const carg = visor.querySelector('.m3d-cargando'); if (carg) carg.remove();
    (function loop(t) { requestAnimationFrame(loop); tween(); latir(t || 0); controls.update(); renderer.render(scene, camera); })();

    return Object.assign(api, {
      ok: true, estado, ajustar, irZona, irRack, irTramo, vista, resaltar, camino, borrarCamino, paradas, mover,
      ponerYo, yoEnPersiana, yoEnCodigo, yoGuardado, rehacerCajas: construirCajas,
      // punto del monigote en puntos del plano, para pedirle el camino al servidor
      yoPuntos: () => estado.yo ? aPuntos(estado.yo[0], estado.yo[1]).map(n => +n.toFixed(1)) : null,
      nombres: on => { grupoNombres.visible = on; }, cajas: on => { if (grupoCajas) grupoCajas.visible = on; },
    });
  };
})();
