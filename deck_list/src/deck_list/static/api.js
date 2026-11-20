// Every call the pages make to the server goes through here.
//
// An answer that is not JSON means the request never reached the app: it was starting up, or
// the connection dropped. Those are retried a few times on their own. Anything that still goes
// wrong, and any error on the page, is sent to the server log ("Page reported" lines in
// deck-list's stderr), so problems can be seen without asking.

const RETRY_DELAYS_MS = [400, 800, 1500, 2500, 4000];
const pendingReports = [];
let flushing = false;

async function flushReports() {
  if (flushing || !pendingReports.length) return;
  flushing = true;
  const entries = pendingReports.splice(0, pendingReports.length);
  try {
    const resp = await fetch("/api/client-log", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entries }), keepalive: true,
    });
    if (!resp.ok) throw new Error(String(resp.status));
  } catch {
    pendingReports.unshift(...entries.slice(-40)); // keep the latest ones for the next try
  } finally {
    flushing = false;
  }
}

export function report(message, detail = {}) {
  pendingReports.push({
    time: new Date().toISOString(), page: location.pathname + location.search,
    message: String(message).slice(0, 300), detail: JSON.stringify(detail).slice(0, 1500),
  });
  if (pendingReports.length > 50) pendingReports.splice(0, pendingReports.length - 50);
  flushReports();
}

export class AppUnreachable extends Error {}

export async function api(path, { method = "GET", body } = {}) {
  const init = body === undefined ? { method } : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  let problem = "";
  for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt++) {
    if (attempt) await new Promise((r) => setTimeout(r, RETRY_DELAYS_MS[attempt - 1]));
    let resp;
    let text = "";
    try {
      resp = await fetch(path, init);
      text = await resp.text();
    } catch (e) {
      problem = `no answer (${e.message})`;
      continue;
    }
    try {
      const data = JSON.parse(text);
      if (attempt) report("server call worked after retrying", { path, method, attempts: attempt + 1, problem });
      flushReports();
      return { ok: resp.ok, status: resp.status, data };
    } catch {
      problem = `HTTP ${resp.status}, not JSON: ${text.slice(0, 160)}`;
    }
  }
  report("server call failed after retrying", { path, method, problem });
  throw new AppUnreachable("the app did not answer");
}

addEventListener("error", (e) => report("page error", { error: e.message, where: `${e.filename}:${e.lineno}` }));
addEventListener("unhandledrejection", (e) => report("unhandled promise rejection", { error: String(e.reason?.stack || e.reason) }));
addEventListener("online", flushReports);
