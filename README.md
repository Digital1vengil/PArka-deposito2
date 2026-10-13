# PARKA Depósito

App de operaciones por **secciones** para el equipo de depósito de PARKA.
Etapa 1: **Despacho** (el scanner migrado), **tareas**, **supervisión** y **cierre diario por mail**.

## Qué hace

| Parte | Detalle |
|---|---|
| Login | Cada operario elige su nombre y entra con su PIN (4–8 números). 5 intentos fallidos = bloqueo de 5 min. La sesión dura 12 h. |
| Roles | **Admin** (ve todo), **Despacho**, **Depósito**. Cada sección define qué roles la ven (menú *Secciones*). |
| Despacho | Cargar lotes **Flex / Colecta / Tienda Nube** desde etiquetas ZPL de ML (.txt) o planillas CSV/XLSX (detecta columnas solo; si no, elegís cuál es el nº de envío). Escaneo por cámara (barras o QR), lector USB/Bluetooth o a mano. Iniciar/Finalizar despacho descarga el Excel de la tanda como el scanner viejo. Si se corta el WiFi, los escaneos quedan guardados y se envían solos. |
| Tareas | Cada lote crea una tarea automática que se completa sola al despachar todo. El admin crea tareas manuales y las asigna a un operario; los operarios las toman y las marcan hechas. |
| Supervisión | Pendientes por sección, lotes abiertos, rendimiento por operario (paquetes, tareas, errores) y registro de actividad (quién hizo qué y cuándo). |
| Cierre diario | Mail a gerencia@magontex.com.ar con despachados/no despachados por tipo, tareas por sección, actividad por operario y stock/faltantes + Excel adjunto. Automático a la hora configurada o manual desde *Cierre*. |
| Mercado Libre (vía ParkaHub) | Sección **🛒 ML** (admin): ventas de hoy/7 días, preguntas sin responder con la sugerencia de ParkaHub, devoluciones por motivo y SKU, reputación y buscador (venta, MLA, envío o modelo). En Despacho, cada escaneo Flex/Colecta se valida en vivo contra ML (semáforo de ParkaHub: si la venta se canceló, se deshace la salida y suena el error) y el botón **Cotejar con ML** muestra etiquetas que faltan cargar y paquetes que ML no da por listos. Todo de solo lectura; sin token de ParkaHub el scanner funciona igual que antes. |
| Depósito | Aparece como "Próximamente" (etapa 2: conteos, ubicaciones, reposición, devoluciones). |

## Probarla en tu PC (Windows)

1. Tener Python 3.10 o más nuevo instalado (marcar "Add to PATH" al instalar).
2. Doble clic en **`run.bat`** (la primera vez instala todo, tarda un par de minutos).
3. Abrir <http://localhost:8000> → entrar como **Vengil** con PIN **1234** → cambiar el PIN en *Usuarios*.
4. Desde un celular en el mismo WiFi: `http://IP-DE-TU-PC:8000` (la cámara del celu solo funciona con HTTPS, o sea cuando esté en Render; en la PC sirve el lector o la carga a mano).

A mano, desde la terminal de VS Code:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

## Mail del cierre

En `.env` completar `SMTP_USER` y `SMTP_PASS`. Con Gmail/Google Workspace: activar verificación en 2 pasos en la cuenta que envía y crear una **contraseña de aplicación** (<https://myaccount.google.com/apppasswords>); esa va en `SMTP_PASS`.

## Subirla a la nube (Render)

1. Publicar esta carpeta como repo privado con GitHub Desktop.
2. En Render: **New → Blueprint** → elegir el repo. Crea la app + base de datos Postgres con `render.yaml`.
3. Completar `ADMIN_PIN`, `SMTP_USER`, `SMTP_PASS` cuando lo pida.
4. Queda en una URL fija con HTTPS (ej. `https://parka-deposito.onrender.com`), instalable como app en tablets y celulares.

> El plan gratis de Render se duerme sin uso y no dispara el cierre automático: usar el plan *Starter*.

## Tests

```bash
pip install pytest httpx
python -m pytest -q
```

## Estructura

```
app/
  main.py            arranque, sesiones, cierre programado
  models.py          usuarios, secciones, lotes, paquetes, tandas, tareas, actividad, cierres
  parsers.py         lectura ZPL / CSV / XLSX (portado del scanner)
  services/          lógica de despacho, tareas y cierre
  routers/           páginas y API por sección
  templates/         pantallas (Tailwind)
  static/            JS/CSS, PWA
```
