// PARKA Depósito en Cloudflare: un Worker delante de un contenedor con la app de Python (FastAPI).
import { Container, getContainer } from "@cloudflare/containers";

export class Deposito extends Container {
  defaultPort = 8080;
  sleepAfter = "20m";        // fuera de horario se apaga si nadie la usa 20 min
  enableInternet = true;     // necesita salir a Odoo, ML, Tienda Nube, Google, Gemini y la base Neon
  pingEndpoint = "container/salud";

  constructor(ctx, env) {
    super(ctx, env);
    // Secretos y variables que la app lee de os.environ (cargados con `wrangler secret put` o en el panel)
    const pasar = [
      "DATABASE_URL", "SECRET_KEY", "ADMIN_NOMBRE", "ADMIN_PIN", "SESION_HORAS",
      "GEMINI_API_KEY", "GEMINI_MODELO", "PARKAHUB_URL", "PARKAHUB_CLIENT_ID", "PARKAHUB_CLIENT_SECRET",
      "GAS_URL", "DEVOL_URL", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO", "CIERRE_AUTOMATICO",
      "PARKA_URL_CELULAR",
    ];
    const vars = {};
    for (const k of pasar) if (env[k] !== undefined && env[k] !== "") vars[k] = String(env[k]);
    this.envVars = vars;
  }
}

const principal = (env) => getContainer(env.DEPOSITO, "principal");

// Horario en que la app tiene que estar prendida sola (tareas automáticas): lun a vie 7:50 a 17:30 (Buenos Aires, UTC-3)
function enHorario(d = new Date()) {
  const ba = new Date(d.getTime() - 3 * 3600 * 1000);
  const dia = ba.getUTCDay();
  const min = ba.getUTCHours() * 60 + ba.getUTCMinutes();
  return dia >= 1 && dia <= 5 && min >= 7 * 60 + 50 && min < 17 * 60 + 30;
}

// PARKA Reparto (app del chofer) llama por Service Binding: esos pedidos no traen CF-Connecting-IP (no pasan por
// internet). Solo a ellos se les agrega una firma con SECRET_KEY, que la app verifica en /api/reparto; la cabecera
// que venga de afuera se borra siempre, así que nadie la puede falsificar.
async function firmaInterna(secreto) {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(secreto), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const s = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode("parka-reparto-interno"));
  return [...new Uint8Array(s)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request, env) {
    const headers = new Headers(request.headers);
    headers.delete("x-reparto-interno");
    if (!request.headers.get("cf-connecting-ip") && env.SECRET_KEY && new URL(request.url).pathname.startsWith("/api/reparto/")) {
      headers.set("x-reparto-interno", await firmaInterna(env.SECRET_KEY));
    }
    return principal(env).fetch(new Request(request, { headers }));
  },

  // Cada minuto (ver triggers en wrangler.jsonc): en horario la mantiene despierta para que corran
  // la búsqueda de las 8:00, las ventas nuevas, el respondedor y la sync de Odoo.
  async scheduled(event, env, ctx) {
    if (!enHorario(new Date(event.scheduledTime))) return;
    ctx.waitUntil(principal(env).fetch(new Request("https://deposito/salud")).then((r) => r.body?.cancel()));
  },
};
