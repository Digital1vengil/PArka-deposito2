/**
 * PARKA Depósito — Backend Drive (Apps Script)  ·  versión 2026.10.22-1
 * (antes "Recolección Ecommerce — Backend Drive": este archivo lo reemplaza completo)
 *
 * Solo se ocupa de Drive. Toda la lógica y los datos están en PARKA Depósito (Python), que arma los Excel.
 *
 *   DEPOSITO PARKA/
 *     Despachos/      2026-10-16 - Despachos.xlsx     (Flex + Colecta + Tienda Nube del día)
 *     Devoluciones/   2026-10-16 - Devoluciones.xlsx  (lo que se devolvió en el día)
 *     Conteos/        2026-10-16 - Conteos.xlsx       (lo que se contó en el día)
 *     Cierre/         2026-10-16 - Cierre.xlsx        (todo lo realizado y despachado en el día)
 *     Plantilla credito odoo/   archivos de cada devolución ecommerce (ML + TN), uno por carpeta:
 *       01_Cantidades_Netas/      CANT_DEV_2026-10-22_PD-15.xlsx
 *       02_Detalle_Por_Venta/     DET_DEV_2026-10-22_PD-15.xlsx
 *       03_Historial_Recepciones/ HIST_DEV_2026-10-22_PD-15.xlsx
 *       04_Carga_Odoo/            IN_DEV_2026-10-22_PD-15.xlsx   (IN para importar en Odoo)
 *     Recolección Ecommerce/ 2026-10-16/ …            (archivos de ventas que sube el equipo; ver ordenarCarpetas)
 *
 * Cada vez que la app vuelve a mandar un día, el archivo de esa fecha se REEMPLAZA (nunca quedan duplicados).
 * También manda el mail del cierre (acción enviarMail): sale desde la cuenta de Google dueña del script.
 *
 * Primera vez (o después de pegar esta versión): ejecutar  configurar()  desde el editor. Pide permisos,
 * crea las carpetas, genera el TOKEN (si no había) e instala el disparador diario. El TOKEN sale en Registros.
 * Después: Implementar → Administrar implementaciones → editar → Versión nueva (la URL /exec no cambia).
 */

var VERSION = '2026.10.22-1';

var CONFIG = {
  NOMBRE_DEPOSITO: 'DEPOSITO PARKA',
  NOMBRE_RAIZ: 'Recolección Ecommerce',      // carpeta de los archivos de ventas (Recolección)
  ZONA_HORARIA: 'America/Argentina/Buenos_Aires',
  HORA_DISPARADOR: 6,                        // 6 a 7 hs: crea la carpeta del día de Recolección
  FORMATO_CARPETA: 'yyyy-MM-dd'
};

/** Subcarpetas de DEPOSITO PARKA: clave que manda la app → nombre de la carpeta y del archivo. */
var SECCIONES = {
  despachos:    'Despachos',
  devoluciones: 'Devoluciones',
  conteos:      'Conteos',
  cierre:       'Cierre'
};

/** Carpetas extra de DEPOSITO PARKA (archivos con nombre propio, no un Excel por día). */
var CARPETAS_EXTRA = {
  credito_odoo: 'Plantilla credito odoo'   // devoluciones ecommerce (y respaldo si falta una subcarpeta)
};

/** Subcarpetas de "Plantilla credito odoo": los 4 archivos de cada devolución ecommerce. */
var CARPETAS_DEV = {
  dev_01_cantidades: '01_Cantidades_Netas',
  dev_02_detalle:    '02_Detalle_Por_Venta',
  dev_03_historial:  '03_Historial_Recepciones',
  dev_04_odoo:       '04_Carga_Odoo'
};

var MIME_SHEET = 'application/vnd.google-apps.spreadsheet';
var MIME_XLSX  = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

/* ============================== Configuración ============================== */

function configurar() {
  var props = PropertiesService.getScriptProperties();
  if (!props.getProperty('TOKEN')) {
    props.setProperty('TOKEN', Utilities.getUuid().replace(/-/g, ''));
  }
  var dep = carpetaDeposito_();
  Object.keys(SECCIONES).forEach(function (k) { carpetaSeccion_(k); });
  Object.keys(CARPETAS_EXTRA).forEach(function (k) { carpetaExtraOSeccion_(k); });
  Object.keys(CARPETAS_DEV).forEach(function (k) { carpetaExtraOSeccion_(k); });
  var raiz = carpetaRaiz_();
  instalarDisparador();
  var hoy = crearCarpetaHoy();
  Logger.log('DEPOSITO PARKA: %s', dep.getUrl());
  Logger.log('Recolección Ecommerce: %s (%s)', raiz.getName(), raiz.getUrl());
  Logger.log('Carpeta de hoy (Recolección): %s', hoy.url);
  Logger.log('TOKEN para PARKA Depósito: %s', props.getProperty('TOKEN'));
}

/**
 * Opcional: mueve la carpeta "Recolección Ecommerce" adentro de "DEPOSITO PARKA" para tener todo junto.
 * No rompe nada: el script la encuentra por su ID, esté donde esté.
 */
function ordenarCarpetas() {
  var dep = carpetaDeposito_(), raiz = carpetaRaiz_();
  var it = raiz.getParents();
  while (it.hasNext()) if (it.next().getId() === dep.getId()) { Logger.log('Ya estaba adentro'); return; }
  raiz.moveTo(dep);
  Logger.log('Movida: %s', raiz.getUrl());
}

/** Instala (una sola vez) el disparador diario que crea la carpeta del día de Recolección. */
function instalarDisparador() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'crearCarpetaHoy') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('crearCarpetaHoy')
    .timeBased()
    .everyDays(1)
    .atHour(CONFIG.HORA_DISPARADOR)
    .inTimezone(CONFIG.ZONA_HORARIA)
    .create();
}

/* ============================== Carpetas ============================== */

/**
 * Carpeta guardada por ID en las propiedades (no depende del nombre ni de dónde esté).
 * Si se borró o todavía no existe, la busca por nombre dentro de "padre" o la crea.
 */
function carpetaGuardada_(clave, nombre, padre) {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty(clave);
  if (id) {
    try {
      var f = DriveApp.getFolderById(id);
      if (!f.isTrashed()) return f;
    } catch (e) { /* se borró: se crea de nuevo */ }
  }
  var it = padre.getFoldersByName(nombre);
  var carpeta = it.hasNext() ? it.next() : padre.createFolder(nombre);
  props.setProperty(clave, carpeta.getId());
  return carpeta;
}

/** "DEPOSITO PARKA" en Mi unidad. */
function carpetaDeposito_() {
  return carpetaGuardada_('DEPOSITO_ID', CONFIG.NOMBRE_DEPOSITO, DriveApp.getRootFolder());
}

/** Subcarpeta de DEPOSITO PARKA (despachos | devoluciones | conteos | cierre). */
function carpetaSeccion_(seccion) {
  var nombre = SECCIONES[seccion];
  if (!nombre) throw new Error('Sección inválida: ' + seccion + ' (usar ' + Object.keys(SECCIONES).join(', ') + ')');
  return carpetaGuardada_('SECCION_' + seccion.toUpperCase() + '_ID', nombre, carpetaDeposito_());
}

/** Carpeta extra (CARPETAS_EXTRA) o subcarpeta de sección de DEPOSITO PARKA. */
function carpetaExtraOSeccion_(clave) {
  if (CARPETAS_DEV[clave]) {
    return carpetaGuardada_('CARPETA_' + clave.toUpperCase() + '_ID', CARPETAS_DEV[clave], carpetaExtraOSeccion_('credito_odoo'));
  }
  if (CARPETAS_EXTRA[clave]) {
    return carpetaGuardada_('CARPETA_' + clave.toUpperCase() + '_ID', CARPETAS_EXTRA[clave], carpetaDeposito_());
  }
  return carpetaSeccion_(clave);
}

/** Carpeta "Recolección Ecommerce". Las instalaciones nuevas la crean adentro de DEPOSITO PARKA. */
function carpetaRaiz_() {
  var props = PropertiesService.getScriptProperties();
  if (!props.getProperty('RAIZ_ID')) {
    // instalación vieja: estaba en Mi unidad → se respeta; instalación nueva → adentro de DEPOSITO PARKA
    var vieja = DriveApp.getRootFolder().getFoldersByName(CONFIG.NOMBRE_RAIZ);
    if (vieja.hasNext()) props.setProperty('RAIZ_ID', vieja.next().getId());
  }
  return carpetaGuardada_('RAIZ_ID', CONFIG.NOMBRE_RAIZ, carpetaDeposito_());
}

function fechaHoy_() {
  return Utilities.formatDate(new Date(), CONFIG.ZONA_HORARIA, CONFIG.FORMATO_CARPETA);
}

function validarFecha_(fecha) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(fecha || '')) throw new Error('Fecha inválida: ' + fecha);
  return fecha;
}

/** Carpeta de Recolección de una fecha (yyyy-MM-dd). Si crear=true y no existe, la crea. */
function carpetaDeFecha_(fecha, crear) {
  validarFecha_(fecha);
  var raiz = carpetaRaiz_();
  var it = raiz.getFoldersByName(fecha);
  if (it.hasNext()) return it.next();
  return crear ? raiz.createFolder(fecha) : null;
}

/** Crea (si no existe) la carpeta de hoy de Recolección. La usa el disparador diario. */
function crearCarpetaHoy() {
  return conLock_(function () {
    var c = carpetaDeFecha_(fechaHoy_(), true);
    return { id: c.getId(), nombre: c.getName(), url: c.getUrl() };
  });
}

/* ============================== Archivos ============================== */

/**
 * Clasifica por nombre (PARKA Depósito vuelve a verificar por encabezados):
 *   ml        -> "..._Mercado_Libre....xlsx" (Flex o Colecta)
 *   tn        -> "ventas-<uuid>.csv" (exportación de ventas de Tienda Nube)
 *   resultado -> archivos que guarda la app (empiezan con "RE-")
 */
function tipoArchivo_(nombre) {
  var n = nombre.toLowerCase();
  if (n.indexOf('re-') === 0) return 'resultado';
  if (n.indexOf('mercado_libre') >= 0 || n.indexOf('mercado libre') >= 0) return 'ml';
  if (/\.csv$/.test(n)) return 'tn';
  return 'otro';
}

function infoArchivo_(f) {
  return {
    id: f.getId(),
    nombre: f.getName(),
    tipo: tipoArchivo_(f.getName()),
    mime: f.getMimeType(),
    bytes: f.getSize(),
    url: f.getUrl(),
    modificado: Utilities.formatDate(f.getLastUpdated(), CONFIG.ZONA_HORARIA, "yyyy-MM-dd'T'HH:mm:ss")
  };
}

function listarCarpeta_(carpeta) {
  var out = [];
  var it = carpeta.getFiles();
  while (it.hasNext()) {
    var f = it.next();
    if (!f.isTrashed()) out.push(infoArchivo_(f));
  }
  out.sort(function (a, b) { return a.modificado < b.modificado ? -1 : 1; });
  return out;
}

/** ¿El archivo está (a cualquier profundidad) dentro de la carpeta "raizId"? */
function estaDentroDe_(archivo, raizId) {
  var pendientes = [archivo.getParents()], vistos = {}, vueltas = 0;
  while (pendientes.length && vueltas++ < 50) {
    var it = pendientes.pop();
    while (it.hasNext()) {
      var p = it.next(), id = p.getId();
      if (id === raizId) return true;
      if (vistos[id]) continue;
      vistos[id] = true;
      pendientes.push(p.getParents());
    }
  }
  return false;
}

/** Devuelve el contenido en base64. Si es una Hoja de Google, la exporta como .xlsx. */
function leerArchivo_(id) {
  var f = DriveApp.getFileById(id);
  // se pueden leer archivos de Recolección y de DEPOSITO PARKA, nada más del Drive
  if (!estaDentroDe_(f, carpetaRaiz_().getId()) && !estaDentroDe_(f, carpetaDeposito_().getId())) {
    throw new Error('El archivo no está en "' + CONFIG.NOMBRE_RAIZ + '" ni en "' + CONFIG.NOMBRE_DEPOSITO + '"');
  }
  var blob, nombre = f.getName(), mime = f.getMimeType();
  if (mime === MIME_SHEET) {
    var url = 'https://docs.google.com/spreadsheets/d/' + id + '/export?format=xlsx';
    blob = UrlFetchApp.fetch(url, { headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() } }).getBlob();
    nombre += '.xlsx';
    mime = MIME_XLSX;
  } else {
    blob = f.getBlob();
  }
  return { id: id, nombre: nombre, mime: mime, bytes: blob.getBytes().length,
           base64: Utilities.base64Encode(blob.getBytes()) };
}

/** Guarda "base64" con ese nombre en la carpeta, reemplazando el archivo anterior del mismo nombre. */
function reemplazarArchivo_(carpeta, nombre, base64, mime) {
  if (!nombre || !base64) throw new Error('Faltan nombre o contenido');
  var blob = Utilities.newBlob(Utilities.base64Decode(base64), mime || MIME_XLSX, nombre);
  var nuevo = carpeta.createFile(blob);
  // se borran los viejos DESPUÉS de crear el nuevo: si algo falla a mitad, nunca queda la carpeta sin archivo
  var existentes = carpeta.getFilesByName(nombre);
  while (existentes.hasNext()) {
    var f = existentes.next();
    if (f.getId() !== nuevo.getId()) f.setTrashed(true);
  }
  return { id: nuevo.getId(), nombre: nuevo.getName(), url: nuevo.getUrl() };
}

/** Recolección: guarda un resultado en la carpeta de la fecha. */
function guardarArchivo_(fecha, nombre, base64, mime) {
  return conLock_(function () {
    return reemplazarArchivo_(carpetaDeFecha_(fecha || fechaHoy_(), true), nombre, base64, mime);
  });
}

/** Nombre del archivo del día de una sección: "2026-10-16 - Despachos.xlsx". */
function nombreDelDia_(seccion, fecha) {
  return validarFecha_(fecha) + ' - ' + SECCIONES[seccion] + '.xlsx';
}

/**
 * DEPOSITO PARKA: guarda los archivos del día.
 *   body.fecha    = 'yyyy-MM-dd'
 *   body.archivos = [{seccion: 'despachos'|'devoluciones'|'conteos'|'cierre', base64, mime?}, …]
 * Devuelve {fecha, archivos: [{seccion, id, nombre, url}], carpeta}.
 */
function guardarDeposito_(body) {
  var fecha = validarFecha_(body.fecha || fechaHoy_());
  var archivos = body.archivos || [];
  if (!archivos.length) throw new Error('No vino ningún archivo');
  return conLock_(function () {
    var out = archivos.map(function (a) {
      var carpeta = carpetaSeccion_(a.seccion);   // valida la sección
      var r = reemplazarArchivo_(carpeta, nombreDelDia_(a.seccion, fecha), a.base64, a.mime);
      r.seccion = a.seccion;
      return r;
    });
    return { fecha: fecha, archivos: out, carpeta: carpetaDeposito_().getUrl() };
  });
}

/**
 * DEPOSITO PARKA: guarda UN archivo con nombre propio en la carpeta de una sección (reemplaza el del mismo nombre).
 * body.seccion = 'devoluciones' | …, body.nombre = 'IN_DEV_2026-10-21_PD-14.xlsx', body.base64, body.mime?
 * Lo usa el IN de devoluciones para Odoo (se arma al confirmar la devolución en los racks).
 * Devuelve {seccion, id, nombre, url}.
 */
function guardarDepositoArchivo_(body) {
  var nombre = String(body.nombre || '').replace(/[\\/:*?"<>|]/g, '_').trim();
  if (!/\.xlsx$/i.test(nombre)) throw new Error('Nombre de archivo inválido: ' + body.nombre);
  return conLock_(function () {
    var r = reemplazarArchivo_(carpetaExtraOSeccion_(body.seccion), nombre, body.base64, body.mime);
    r.seccion = body.seccion;
    return r;
  });
}

/** DEPOSITO PARKA: carpetas y (si se pide fecha) los archivos de ese día. */
function estadoDeposito_(fecha) {
  var dep = carpetaDeposito_(), out = { id: dep.getId(), url: dep.getUrl(), secciones: {} };
  Object.keys(SECCIONES).forEach(function (k) {
    var c = carpetaSeccion_(k), s = { nombre: SECCIONES[k], url: c.getUrl() };
    if (fecha) {
      var it = c.getFilesByName(nombreDelDia_(k, fecha));
      s.archivo = it.hasNext() ? infoArchivo_(it.next()) : null;
    }
    out.secciones[k] = s;
  });
  return out;
}

/* ============================== Mail ============================== */

/**
 * Manda un mail con adjuntos opcionales.
 *   body.para, body.copia?, body.asunto, body.html, body.adjuntos = [{nombre, base64, mime?}]
 */
function enviarMail_(body) {
  if (!body.para) throw new Error('Falta el destinatario');
  if (!body.asunto) throw new Error('Falta el asunto');
  var adjuntos = (body.adjuntos || []).map(function (a) {
    return Utilities.newBlob(Utilities.base64Decode(a.base64), a.mime || MIME_XLSX, a.nombre);
  });
  var opciones = { htmlBody: body.html || '', attachments: adjuntos, name: 'PARKA Depósito' };
  if (body.copia) opciones.cc = body.copia;
  MailApp.sendEmail(body.para, body.asunto, 'El detalle está en el Excel adjunto.', opciones);
  return { para: body.para, copia: body.copia || '', restantesHoy: MailApp.getRemainingDailyQuota() };
}

/** Ejecutar una vez desde el editor después de pegar esta versión: pide el permiso para mandar mails. */
function autorizarMail() {
  Logger.log('Mails que quedan hoy: %s', MailApp.getRemainingDailyQuota());
}

/* ============================== API web ============================== */
/*
 * Todas las llamadas llevan token. Respuesta: {"ok": true, "data": …} o {"ok": false, "error": "…"}.
 *   GET  ?token=…&action=estado                       -> versión, carpetas y fecha de hoy
 *   GET  ?token=…&action=hoy                          -> crea si falta la carpeta de hoy (Recolección) + archivos
 *   GET  ?token=…&action=listar&fecha=2026-10-06      -> archivos de esa fecha (Recolección, sin crear)
 *   GET  ?token=…&action=archivo&id=FILE_ID           -> archivo en base64
 *   GET  ?token=…&action=deposito&fecha=2026-10-16    -> carpetas de DEPOSITO PARKA y archivos de ese día
 *   POST {token, action:"guardar", fecha, nombre, base64, mime}   -> Recolección: guarda en la carpeta de la fecha
 *   POST {token, action:"guardarDeposito", fecha, archivos:[…]}   -> DEPOSITO PARKA: reemplaza los del día
 *   POST {token, action:"enviarMail", para, copia?, asunto, html, adjuntos:[…]} -> manda el mail (cierre)
 */

function doGet(e) {
  return responder_(function () {
    var p = (e && e.parameter) || {};
    verificarToken_(p.token);
    switch (p.action) {
      case 'estado':
        var raiz = carpetaRaiz_(), dep = carpetaDeposito_();
        return { version: VERSION, hoy: fechaHoy_(),
                 raiz: { id: raiz.getId(), url: raiz.getUrl() },
                 deposito: { id: dep.getId(), url: dep.getUrl() } };
      case 'hoy':
        var info = crearCarpetaHoy();
        info.fecha = fechaHoy_();
        info.archivos = listarCarpeta_(DriveApp.getFolderById(info.id));
        return info;
      case 'listar':
        var fecha = p.fecha || fechaHoy_();
        var c = carpetaDeFecha_(fecha, false);
        if (!c) return { fecha: fecha, existe: false, archivos: [] };
        return { fecha: fecha, existe: true, id: c.getId(), url: c.getUrl(), archivos: listarCarpeta_(c) };
      case 'archivo':
        if (!p.id) throw new Error('Falta id');
        return leerArchivo_(p.id);
      case 'deposito':
        return estadoDeposito_(p.fecha ? validarFecha_(p.fecha) : null);
      default:
        throw new Error('Acción desconocida: ' + p.action);
    }
  });
}

function doPost(e) {
  return responder_(function () {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    verificarToken_(body.token);
    switch (body.action) {
      case 'guardar':
        return guardarArchivo_(body.fecha, body.nombre, body.base64, body.mime);
      case 'guardarDeposito':
        return guardarDeposito_(body);
      case 'guardarDepositoArchivo':
        return guardarDepositoArchivo_(body);
      case 'enviarMail':
        return enviarMail_(body);
      default:
        throw new Error('Acción desconocida: ' + body.action);
    }
  });
}

/* ============================== Utilidades ============================== */

function verificarToken_(token) {
  var esperado = PropertiesService.getScriptProperties().getProperty('TOKEN');
  if (!esperado) throw new Error('Falta ejecutar configurar() en el editor del script');
  if (!token || token !== esperado) throw new Error('Token inválido');
}

/** Un solo proceso a la vez tocando carpetas: evita carpetas o archivos duplicados si llegan dos pedidos juntos. */
function conLock_(fn) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    return fn();
  } finally {
    lock.releaseLock();
  }
}

function responder_(fn) {
  var out;
  try {
    out = { ok: true, data: fn() };
  } catch (err) {
    out = { ok: false, error: String((err && err.message) || err) };
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

/** Prueba rápida desde el editor: crea las carpetas y muestra qué archivos hay hoy. */
function probarDeposito() {
  Logger.log(JSON.stringify(estadoDeposito_(fechaHoy_()), null, 2));
}
