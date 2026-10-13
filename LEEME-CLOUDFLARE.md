# PARKA Depósito en Cloudflare

Esta app es independiente de la versión ejecutable (`parka-deposito` / `PARKA-Deposito-App`).

- Corre en **Cloudflare Containers**: un Worker recibe las visitas y se las pasa a un contenedor con la app de Python.
- La base de datos es **Neon Postgres** (proyecto `parka-deposito`, región São Paulo).
- Cada push a `main` en GitHub corre los tests y publica sola (`.github/workflows/deploy.yml`).

## Horario

El contenedor queda despierto de **lunes a viernes de 7:50 a 17:30** (Buenos Aires). En ese horario corren las tareas automáticas: búsqueda de las 8:00, ventas nuevas, respondedor de ML y sincronización con Odoo.

Fuera de ese horario la app igual abre si alguien entra: tarda unos segundos en arrancar y se apaga después de 20 minutos sin uso. El horario se cambia en `src/index.js` (función `enHorario`) y en `wrangler.jsonc` (`triggers`).

## Puesta en marcha (una sola vez)

### 1. Neon

En console.neon.tech, abrí el proyecto, tocá **Connect** y copiá la *connection string* (`postgresql://...?sslmode=require`). Es la contraseña de la base: no la compartas.

### 2. Repo en GitHub

1. Creá un repo **privado** `parka-deposito-cloud`.
2. En GitHub Desktop: *Add local repository* → esta carpeta → *Publish*.

### 3. Token de Cloudflare

1. En dash.cloudflare.com → *My Profile* → *API Tokens* → *Create Token* → plantilla **Edit Cloudflare Workers** → *Create*. Copiá el token.
2. Copiá también el **Account ID**: aparece en el costado de *Workers & Pages*.

### 4. Secretos del repo

En GitHub → repo → *Settings* → *Secrets and variables* → *Actions* → *New repository secret*:

| Nombre | Valor |
|---|---|
| `CLOUDFLARE_API_TOKEN` | el token del paso 3 |
| `CLOUDFLARE_ACCOUNT_ID` | el Account ID |

### 5. Primer deploy

1. En GitHub → *Actions* → **Deploy a Cloudflare** → *Run workflow*. Tarda unos 5 minutos.
2. La primera vez, el contenedor tarda unos minutos más en quedar disponible.

### 6. Secretos de la app

Se cargan en Cloudflare: *Workers & Pages* → `parka-deposito` → *Settings* → *Variables and Secrets* → *Add* → tipo **Secret**.

| Nombre | Valor |
|---|---|
| `DATABASE_URL` | la connection string de Neon |
| `SECRET_KEY` | un texto largo inventado (40+ letras y números) |
| `ADMIN_PIN` | PIN inicial del admin "Vengil" (solo hasta importar la base) |
| `GEMINI_API_KEY` | la clave de Gemini, si la usabas por `.env` |

Odoo, Mercado Libre (API de Costos), Tienda Nube y la app de cambios **no hace falta cargarlos**: viajan dentro de la base que se importa en el paso 7.

### 7. Pasar los datos

1. En la PC, cerrá el ejecutable ("Detener y salir").
2. Abrí `https://parka-deposito.<tu-subdominio>.workers.dev`.
3. Entrá como **Vengil** con el `ADMIN_PIN`.
4. Andá a *Conexiones* → **⬆ Importar base del ejecutable** y subí `Documents\GitHub\PARKA-Deposito-App\datos\parka_deposito.db`.
5. Volvé a entrar con tu usuario de siempre (Diego).

Se copian usuarios y PIN, accesos, conexiones, artículos, ubicaciones, SKU vinculados e historial.

### 8. Corte

Desde ahí se usa solo la versión de la nube. El ejecutable no se borra, queda como respaldo.

En los celulares se abre la misma URL; la cámara funciona directo, porque es HTTPS real.

## Notas

- **Etiquetas:** se siguen imprimiendo con el diálogo de impresión del navegador, en la PC que tiene la Honeywell.
- **Cambios de código:** se hacen en esta carpeta → *Commit* → *Push* en GitHub Desktop → publica solo.
- **Logs:** Cloudflare → `parka-deposito` → *Observability* / *Logs*.
- **Costo aproximado:** plan Workers Paid (US$5) + ~US$2–3 de contenedor en este horario. Neon, gratis.
