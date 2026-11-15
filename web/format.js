// Gemeinsame Hilfsfunktionen für alle Seiten: Element-Zugriff, Escaping und Zahlenformate.
// Hier ändern statt in den einzelnen Seiten, damit die Darstellung überall gleich bleibt.
// Braucht die Klassen .up, .down und .muted aus base.css.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const dash = '<span class="muted">–</span>';   // Platzhalter für fehlende Werte

// Ganze Zahl mit Vorzeichen (echtes Minuszeichen), z. B. Score-Punkte
const signed = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v);
// Farbklasse für Scores; Schwellen wie in sentiment.py: VERDICT_WARM (Gesamtscore, 15) und SIGNAL_STRONG (Einzelsignal, 50)
const tone = (score, limit = 15) => (score >= limit ? "tone-hot" : score <= -limit ? "tone-cold" : "tone-neutral");

// Anteil (0,05 = 5 %) als Prozent mit Vorzeichen. Das Vorzeichen richtet sich nach dem
// gerundeten Wert, sonst wird z. B. −0,004 % als „−0,0 %“ angezeigt.
const pct = (v, d = 1) => {
  const r = Number((v * 100).toFixed(d));
  return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r).toFixed(d).replace(".", ",") + " %";
};
// Prozentänderung farbig (grün/rot), fehlende Werte als Strich
const change = (v, d = 1) => {
  if (v == null) return dash;
  const r = Number((v * 100).toFixed(d));
  return `<span class="${r > 0 ? "up" : r < 0 ? "down" : "muted"}">${pct(v, d)}</span>`;
};

// Kurse und kleinere Beträge: unter 1 $ nach signifikanten Stellen (sonst wird 0,0033 zu „$0“),
// unter 100 $ mit Cent, darüber ganzzahlig, ab 1 Mio. gekürzt
const usd = (v) => {
  if (v >= 1e9) return "$" + (v / 1e9).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " Mrd.";
  if (v >= 1e6) return "$" + (v / 1e6).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " Mio.";
  if (v !== 0 && Math.abs(v) < 1) return "$" + v.toLocaleString("de-DE", { maximumSignificantDigits: 4 });
  return "$" + v.toLocaleString("de-DE", v < 100 ? { minimumFractionDigits: 2, maximumFractionDigits: 2 } : { maximumFractionDigits: 0 });
};
// Große Beträge (Marktkap., Volumen, TVL) auf 6 signifikante Stellen mit Einheit, z. B. „$1,61238 Bio.“
const usd6 = (v) => {
  if (v == null || v === 0) return dash;
  const [div, unit] = v >= 1e12 ? [1e12, " Bio."] : v >= 1e9 ? [1e9, " Mrd."] : v >= 1e6 ? [1e6, " Mio."] : [1, ""];
  return "$" + (v / div).toLocaleString("de-DE", { minimumSignificantDigits: 6, maximumSignificantDigits: 6 }) + unit;
};

// Fear & Greed (0–100): Zonen wie auf alternative.me / CNN; Farben von Rot (Angst) über Grau zu Grün (Gier)
const FNG_ZONES = [
  { to: 25, name: "Extreme Angst", color: "var(--down)" },
  { to: 45, name: "Angst", color: "color-mix(in srgb, var(--down) 55%, var(--neutral))" },
  { to: 55, name: "Neutral", color: "var(--neutral)" },
  { to: 75, name: "Gier", color: "color-mix(in srgb, var(--up) 55%, var(--neutral))" },
  { to: 100, name: "Extreme Gier", color: "var(--up)" },
];
const fngZone = (v) => FNG_ZONES.find((z) => v <= z.to) || FNG_ZONES[FNG_ZONES.length - 1];
