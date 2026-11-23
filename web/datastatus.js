// Datenstatus je Kennzahl bzw. Karte: Quelle · Zustand · Stand, z. B. „Binance WebSocket · live · letzte Meldung vor 8 s“
// oder „Coin Metrics · Tageswert · Stand gestern“. Nach format.js laden (braucht L, LOCALE, esc); Tooltip über tips.js (TIPS.ds).
//
// Zustände:  live (WebSocket, Meldungen kommen) · aktuell (Stand im üblichen Takt) · verzögert (Abruf überfällig)
//            · veraltet (deutlich überfällig oder Verbindung getrennt) · nicht verfügbar (keine Daten).
// Arten (kind):
//   live  – at = letzte Meldung; every = übliche Höchstpause in s (Standard 30); connected: false → veraltet
//   poll  – at = Abrufzeit beim Server (fetched_at) bzw. letzte Antwort; every = Takt in s
//   daily – day = letzter Tageswert "YYYY-MM-DD" (UTC); lag = übliche Verzögerung in Tagen (Standard 1 = gestern)
// at: Sekunden, Millisekunden oder ISO-Text. Fehlt at bzw. day (oder na: true) → nicht verfügbar.
//
// quiet: true zeigt bei live/aktuell nur Punkt und Quelle (für viele Marken auf engem Raum).
// Verwendung: dataStatus({...}) liefert HTML, setDataStatus(el, {...}) setzt ein vorhandenes Element.
// Alle .ds-Elemente werden jede Sekunde neu bewertet, das Alter läuft also mit.

const DS_STATES = {
  live: L("live", "live"), ok: L("aktuell", "current"), late: L("verzögert", "delayed"),
  stale: L("veraltet", "stale"), na: L("nicht verfügbar", "not available"),
};

const dsMs = (at) => {
  if (at == null || at === "") return null;
  if (typeof at === "number") return at < 1e12 ? at * 1000 : at;
  const t = Date.parse(at);
  return Number.isNaN(t) ? null : t;
};

function dsAgo(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return L(`vor ${sec} s`, `${sec} s ago`);
  const min = Math.round(sec / 60);
  if (min < 60) return L(`vor ${min} Min.`, `${min} min ago`);
  const h = Math.round(sec / 3600);
  if (h < 48) return L(`vor ${h} Std.`, `${h} h ago`);
  const d = Math.round(sec / 86400);
  return L(`vor ${d} Tagen`, `${d} days ago`);
}

const dsEvery = (s) => s >= 86400 ? L("täglich", "daily") : s >= 3600 ? L(`alle ${Math.round(s / 3600)} Std.`, `every ${Math.round(s / 3600)} h`)
  : s >= 60 ? L(`alle ${Math.round(s / 60)} Min.`, `every ${Math.round(s / 60)} min`) : L(`alle ${s} s`, `every ${s} s`);

// Zustand und Texte aus den Angaben
function dsEval(o, now = Date.now()) {
  const src = o.src || "";
  if (o.na) return { state: "na", text: `${src} · ${DS_STATES.na}`, tip: o.note || "" };
  if (o.kind === "daily") {
    if (!o.day) return { state: "na", text: `${src} · ${DS_STATES.na}`, tip: o.note || "" };
    const day = Date.parse(o.day.slice(0, 10) + "T00:00:00Z");
    const today = Date.parse(new Date(now).toISOString().slice(0, 10) + "T00:00:00Z");
    const lag = Math.round((today - day) / 864e5), okLag = o.lag ?? 1;
    const state = lag <= okLag ? "ok" : lag <= okLag + 2 ? "late" : "stale";
    const when = lag <= 0 ? L("Stand heute", "as of today") : lag === 1 ? L("Stand gestern", "as of yesterday")
      : L("Stand ", "as of ") + new Date(day).toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit", timeZone: "UTC" });
    return {
      state, text: `${src} · ${state === "ok" ? L("Tageswert", "daily value") : DS_STATES[state]} · ${when}`,
      tip: [L("Aktualisierung: täglich (Tageswerte, UTC)", "Updates: daily (daily values, UTC)"),
        L("Letzter Tageswert: ", "Latest daily value: ") + new Date(day).toLocaleDateString(LOCALE, { timeZone: "UTC" }), o.note].filter(Boolean).join("\n"),
    };
  }
  const at = dsMs(o.at);
  // verbunden, aber seit dem Start noch keine Meldung (z. B. Liquidationen, die selten kommen): trotzdem live
  if (at == null && o.kind === "live" && o.connected) {
    return { state: "live", text: `${src} · live · ${L("verbunden, noch keine Meldung", "connected, no message yet")}`,
      tip: L("Aktualisierung: laufend (WebSocket)", "Updates: continuous (WebSocket)") };
  }
  if (at == null) return { state: "na", text: `${src} · ${DS_STATES.na}`, tip: o.note || "" };
  const age = (now - at) / 1000, stand = new Date(at).toLocaleString(LOCALE);
  if (o.kind === "live") {
    const every = o.every || 30;
    const state = o.connected === false ? "stale" : age <= every ? "live" : age <= every * 4 ? "late" : o.connected ? "live" : "stale";
    const detail = o.connected === false ? L("Verbindung getrennt", "disconnected") : L("letzte Meldung ", "last message ") + dsAgo(age);
    return {
      state, text: `${src} · ${DS_STATES[state]} · ${detail}`,
      tip: [L("Aktualisierung: laufend (WebSocket)", "Updates: continuous (WebSocket)"), L("Letzte Meldung: ", "Last message: ") + stand, o.note].filter(Boolean).join("\n"),
    };
  }
  // poll: üblich ist bis zu zweimal der Takt (Server-Cache plus Abruf der Seite)
  const every = o.every || 300;
  const state = age <= every * 2 + 60 ? "ok" : age <= every * 4 + 120 ? "late" : "stale";
  return {
    state, text: `${src} · ${DS_STATES[state]} · ${L("Stand", "as of")} ${dsAgo(age)}`,
    tip: [L("Aktualisierung: ", "Updates: ") + dsEvery(every), L("Stand: ", "As of: ") + stand, o.note].filter(Boolean).join("\n"),
  };
}

function dsRender(el) {
  let o;
  try { o = JSON.parse(el.dataset.ds || "{}"); } catch (e) { return; }
  const r = dsEval(o);
  el.className = "ds st-" + r.state + (o.small ? " small" : "");
  // quiet: solange alles in Ordnung ist, nur Punkt und Quelle (Details im Tooltip); auffällig erst bei Verzug
  const text = o.quiet && (r.state === "ok" || r.state === "live") ? o.src || "" : r.text;
  el.innerHTML = `<i aria-hidden="true"></i>${esc(text)}`;
  el.dataset.tip = "ds";
  el.dataset.tipNow = [L("Quelle: ", "Source: ") + (o.src || "–"), L("Status: ", "Status: ") + DS_STATES[r.state], r.tip].filter(Boolean).join("\n");
  el.tabIndex = 0;
}

function dataStatus(o) {
  return `<span class="ds" data-ds="${esc(JSON.stringify(o))}"></span>`;
}

function setDataStatus(el, o) {
  if (typeof el === "string") el = document.getElementById(el);
  if (!el) return;
  el.dataset.ds = JSON.stringify(o);
  dsRender(el);
}

// neu eingefügte Marken sofort füllen, danach jede Sekunde das Alter nachziehen
function dsRefresh() { document.querySelectorAll(".ds[data-ds]").forEach(dsRender); }
new MutationObserver((list) => {
  if (list.some((m) => [...m.addedNodes].some((n) => n.nodeType === 1 && (n.matches(".ds") || n.querySelector(".ds"))))) dsRefresh();
}).observe(document.documentElement, { childList: true, subtree: true });
setInterval(dsRefresh, 1000);
