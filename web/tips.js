// Gemeinsame Tooltips für Dashboard (index.html) und Detailseite (coin.html).
// Braucht die Styles für [data-tip] und #tip aus base.css; das #tip-Element wird bei Bedarf angelegt.

// Erklärungen für Tooltips. Die Schwellen entsprechen denen in sentiment.py.
const TIPS = {
  fng: "Fear & Greed Index von alternative.me (0–100), berechnet aus Volatilität, Volumen, Social Media, BTC-Dominanz und Google Trends. 0 = extreme Angst, 100 = extreme Gier. Im Score: 50 → 0, ab 80 → +100, bis 20 → −100, linear dazwischen.",
  stablecoins: "Gesamte Marktkapitalisierung aller Stablecoins (DefiLlama). Wachsende Bestände gelten als „trockenes Pulver“, das in Krypto fließen kann; schrumpfende als Kapitalabfluss. Nicht Teil des Scores.",
  dominance: "Anteil von Bitcoin an der gesamten Krypto-Marktkapitalisierung (CoinGecko). Über 55 % spricht für eine vorsichtige, BTC-lastige Phase; fallende Dominanz dafür, dass Altcoins Kapital anziehen. Nicht Teil des Scores.",
  trending: "Die meistgesuchten Coins auf CoinGecko in den letzten 24 Stunden (max. 15) – ein Maß für Aufmerksamkeit, nicht für Qualität. Kleine Zahl links = Rang (1 = meistgesucht), je kräftiger die Färbung, desto weiter oben. Rechts die Kursänderung in 24 h. Klick auf einen Coin zeigt ihn als zusätzliche Karte.",
  top20: "Die 20 größten Kryptowährungen nach Marktkapitalisierung (CoinGecko), ohne an den US-Dollar gebundene Stablecoins wie USDT und USDC (die Lücken in den Rängen). Gold-Token wie XAUT bleiben drin. Kleine Zahl links = Marktkap.-Rang, rechts die Kursänderung in 24 h. Klick auf einen Coin zeigt ihn als zusätzliche Karte.",
  score: "Durchschnitt aller Einzelsignale. Jedes Signal liefert −100 (extreme Angst) bis +100 (Überhitzung / Gier), stufenlos je nach Messwert. Ab ±15 gilt der Markt als leicht gierig bzw. ängstlich, ab +40 als überhitzt, ab −40 als extrem ängstlich.",
  mcap: "Marktkapitalisierung in US-Dollar: aktueller Preis × umlaufende Coins (CoinGecko). Rang = Platz unter allen Kryptowährungen nach Marktkapitalisierung. Nicht Teil des Scores.",
  ma200: "Durchschnitt der Tages-Schlusskurse der letzten 200 Tage (Binance). Gilt als Trennlinie zwischen langfristigem Auf- und Abwärtstrend.",
  distance: "Wie weit der aktuelle Preis über (+) oder unter (−) dem MA200 liegt. Im Score: +10 % (normaler Aufwärtstrend) → 0, ab +35 % stark gestreckt → +100, ab −20 % → −100, linear dazwischen.",
  funding: "Durchschnittliche Funding Rate der letzten 7 Tage auf Binance Futures, je Zahlung (meist alle 8 Stunden). Positiv: Longs zahlen an Shorts, der Markt ist long-lastig. Im Score: 0,01 % (Binance-Standard) → 0, ab 0,05 % → +100, ab −0,01 % → −100, linear dazwischen.",
  longShort: "Verhältnis der Binance-Futures-Konten mit Long- zu Short-Position. 2,0 heißt: doppelt so viele Konten long. Gilt als Kontraindikator. Auf Binance sind Kleinanleger meist deutlich long, daher gilt 2,0 als normal. Im Score: 2,0 → 0, ab 3,5 → +100, bis 0,9 → −100, linear dazwischen.",
  fundingHist: "Funding-Zahlungen auf Binance Futures der letzten 90 Tage, je Zahlung (meist alle 8 Stunden, bei manchen Coins alle 4 h oder 1 h). Positiv (orange): Longs zahlen an Shorts, der Markt ist long-lastig. Negativ (blau): Shorts zahlen. Die Linie ist der gleitende 7-Tage-Durchschnitt, der in den Score eingeht; gestrichelt der Binance-Standard von 0,01 %. Erfahrungswerte: ab 0,05 % viel Long-Hebel, unter −0,01 % dominieren Shorts.",
  oiHist: "Open Interest (Fläche, rechte Achse): Gesamtwert aller offenen Futures-Positionen auf Binance in USD, in 4-h-Schritten über 30 Tage. Gepunktet der Kurs (linke Achse). Steigt das OI mit dem Kurs, wird der Anstieg mit Hebel getragen; fällt es stark, werden Positionen abgebaut oder liquidiert. Die Änderung über 7 Tage geht in den Score ein.",
  oi: "Open Interest: Gesamtwert aller offenen Futures-Positionen auf Binance in USD. Zeigt, wie viel gehebeltes Kapital im Markt steckt.",
  oiChange: "Veränderung des Open Interest in 7 Tagen. Viel neuer Hebel erhöht das Risiko heftiger Liquidationswellen (Gier). Ein starker Rückgang zeigt, dass Positionen abgebaut oder zwangsliquidiert wurden – typisch nach Panik. Im Score: 0 % → 0, ab +15 % → +100, ab −15 % → −100, linear dazwischen.",
  dvol: "Deribit Volatility Index: die von Optionspreisen erwartete Schwankung der nächsten 30 Tage, als Jahreswert in %. In Klammern der Abstand zum 90-Tage-Durchschnitt in Standardabweichungen (σ). Nur für BTC und ETH verfügbar. Im Score: 0σ → 0, ab −1,5σ ungewöhnlich ruhig (Sorglosigkeit) → +100, ab +1,5σ ungewöhnlich hoch (Panik) → −100, linear dazwischen.",
  momentum7: "Kursänderung der letzten 7 Tage (CoinGecko). Ersatz-Signal für Coins ohne Binance-Futures oder -Kursdaten. Im Score: 0 % → 0, ab +25 % → +100, ab −25 % → −100, linear dazwischen.",
  momentum30: "Kursänderung der letzten 30 Tage (CoinGecko). Ersatz-Signal für Coins ohne Binance-Futures oder -Kursdaten. Im Score: 0 % → 0, ab +50 % → +100, ab −50 % → −100, linear dazwischen.",
  turnover: "Handelsvolumen der letzten 24 h im Verhältnis zur Marktkapitalisierung (CoinGecko). Sehr hoher Umschlag zeigt hektischen Handel: im Aufwärtstrend Spekulationsrausch (+), im Abwärtstrend Panikverkäufe (−). Ersatz-Signal. Im Score: bis 5 % → 0, ab 50 % → ±100.",
  rsiHourly: "RSI über 14 Stunden statt Tage, berechnet aus den stündlichen Kursen der letzten 7 Tage (CoinGecko). Ersatz, wenn keine Binance-Tageskerzen verfügbar sind; reagiert schneller als der Tages-RSI. Im Score: 50 → 0, ab 70 → +100, bis 30 → −100.",
  rsi: "Relative Strength Index über 14 Tage (0–100): misst, wie stark die Kursgewinne die Verluste zuletzt überwogen haben. Im Score: 50 → 0, ab 70 überkauft → +100, bis 30 überverkauft → −100, linear dazwischen.",
};
const SIGNAL_TIPS = {
  "Fear & Greed": "fng", "Funding Rate": "funding", "Open Interest": "oiChange",
  "Preis vs. MA200": "distance", "RSI (14)": "rsi", "Long/Short Ratio": "longShort", "Optionsvola (DVOL)": "dvol",
  "Momentum 7T": "momentum7", "Momentum 30T": "momentum30", "Umschlag": "turnover", "RSI (14 Std.)": "rsiHourly",
};
const tipAttr = (key) => (key && TIPS[key] ? ` data-tip="${key}" tabindex="0"` : "");

// --- Tooltips: Maus (Hover), Tastatur und Touch (beide über den Fokus) ---
let tipTarget = null;

function tipEl() {
  let tip = document.getElementById("tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "tip";
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    document.body.appendChild(tip);
  }
  return tip;
}

// data-tip-now (optional): Zusatzzeile zum aktuellen Wert, wird unter der Erklärung angehängt
function showTip(el) {
  const tip = tipEl();
  tipTarget = el;
  tip.textContent = TIPS[el.dataset.tip] + (el.dataset.tipNow ? "\n\n" + el.dataset.tipNow : "");
  tip.hidden = false;
  const r = el.getBoundingClientRect();
  const t = tip.getBoundingClientRect();
  const margin = 8;
  const left = Math.min(Math.max(margin, r.left), window.innerWidth - t.width - margin);
  // unterhalb anzeigen, bei Platzmangel oberhalb
  const below = r.bottom + 6;
  const top = below + t.height > window.innerHeight - margin ? r.top - t.height - 6 : below;
  tip.style.left = left + "px";
  tip.style.top = top + "px";
  tip.classList.add("show");
}

function hideTip() {
  tipTarget = null;
  const tip = tipEl();
  tip.classList.remove("show");
  tip.hidden = true;
}

document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-tip]");
  if (el && el !== tipTarget) showTip(el);
  else if (!el && tipTarget) hideTip();
});
document.addEventListener("focusin", (e) => {
  const el = e.target.closest("[data-tip]");
  if (el) showTip(el);
});
document.addEventListener("focusout", hideTip);
document.addEventListener("mouseout", (e) => { if (!e.relatedTarget) hideTip(); });  // Maus verlässt das Fenster
window.addEventListener("scroll", hideTip, { passive: true });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });
