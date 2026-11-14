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
  funding: "Durchschnittliche Funding Rate der letzten 7 Tage auf Binance Futures, pro 8 Stunden. Coins, die alle 4 h oder 1 h zahlen, werden auf 8 h umgerechnet (×2 bzw. ×8), damit die Schwellen für alle gleich gelten. Positiv: Longs zahlen an Shorts, der Markt ist long-lastig. Im Score: 0,01 % (Binance-Standard) → 0, ab 0,05 % → +100, ab −0,01 % → −100, linear dazwischen.",
  longShort: "Verhältnis der Binance-Futures-Konten mit Long- zu Short-Position. 2,0 heißt: doppelt so viele Konten long. Gilt als Kontraindikator. Auf Binance sind Kleinanleger meist deutlich long, daher gilt 2,0 als normal. Im Score: 2,0 → 0, ab 3,5 → +100, bis 0,9 → −100, linear dazwischen.",
  fundingHist: "Funding-Zahlungen auf Binance Futures der letzten 90 Tage, umgerechnet auf 8 Stunden (manche Coins zahlen alle 4 h oder 1 h; die Legende nennt dann den Takt und den tatsächlich gezahlten Satz). Positiv (orange): Longs zahlen an Shorts, der Markt ist long-lastig. Negativ (blau): Shorts zahlen. Die Linie ist der gleitende 7-Tage-Durchschnitt, der in den Score eingeht; gestrichelt der Binance-Standard von 0,01 %. Erfahrungswerte: ab 0,05 % viel Long-Hebel, unter −0,01 % dominieren Shorts.",
  oiHist: "Open Interest (Fläche, rechte Achse): Gesamtwert aller offenen Futures-Positionen auf Binance in USD, in 4-h-Schritten über 30 Tage. Gepunktet der Kurs (linke Achse). Steigt das OI mit dem Kurs, wird der Anstieg mit Hebel getragen; fällt es stark, werden Positionen abgebaut oder liquidiert. Die Änderung über 7 Tage geht in den Score ein.",
  scoreHist: "Überhitzungs-Score je Tag, rückwirkend mit derselben Logik berechnet wie der aktuelle Score – mit den Daten, die bis zum Ende des jeweiligen Tages (UTC) vorlagen. Der heutige Punkt entspricht dem aktuellen Score. Open Interest und Long/Short Ratio liefert Binance nur für 30 Tage, davor fehlen sie (Markierung im Chart); CoinGecko-Ersatzsignale sind nicht enthalten. Gepunktet der Tagesschlusskurs (linke Achse). Linien bei ±15 (leicht gierig/ängstlich) und ±40 (überhitzt/extreme Angst) sind Erfahrungswerte.",
  ath: "Höchster je gehandelter Kurs (CoinGecko), mit Abstand des aktuellen Kurses und Datum. Nicht Teil des Scores.",
  atl: "Niedrigster je gehandelter Kurs (CoinGecko), mit Abstand des aktuellen Kurses nach oben und Datum. Nicht Teil des Scores.",
  w52: "Höchster und tiefster Kurs der letzten 52 Wochen (Binance-Tageskerzen, inklusive Dochte), mit Abstand des aktuellen Kurses und Datum. Bei jüngeren Coins seit Handelsstart. Der Strich zeigt die Lage des Kurses in der Spanne. Nicht Teil des Scores.",
  vol30: "Realisierte Volatilität der letzten 30 Tage: Standardabweichung der täglichen Kursänderungen, hochgerechnet auf ein Jahr (× √365). „pro Tag“ ist die typische Tagesschwankung. Verglichen mit dem Durchschnitt der 30-Tage-Werte im letzten Jahr; ab ±25 % Abweichung gilt sie als deutlich höher bzw. ruhiger als üblich (Erfahrungswert). Nicht Teil des Scores.",
  drawdown: "Größter Kursrückgang im letzten Jahr von einem Hoch bis zum darauffolgenden Tief (Tages-Schlusskurse), mit Zeitraum und Erholung seitdem. Darunter, wie weit der Kurs aktuell unter dem höchsten Schlusskurs des Jahres liegt. Nicht Teil des Scores.",
  maDist: "Abstand des Kurses zum gleitenden Durchschnitt der letzten 50 bzw. 200 Tages-Schlusskurse (Binance). Der Abstand zum MA200 geht als Signal „Preis vs. MA200“ in den Score ein. Liegt der MA50 über dem MA200, gilt der Trend als aufwärts gerichtet (Golden Cross), darunter als abwärts (Death Cross) – ein träges Signal, das oft erst spät dreht.",
  range24: "Tiefst- und Höchstkurs der letzten 24 Stunden (CoinGecko). Der Strich zeigt, wo der aktuelle Kurs in dieser Spanne liegt; die Schwankung ist der Abstand von Tief zu Hoch. Nicht Teil des Scores.",
  changes: "Kursänderung über 24 Stunden, 7 Tage, 30 Tage und 1 Jahr (CoinGecko). Nicht Teil des Scores; bei Coins ohne Binance-Daten fließen 7 T und 30 T als Ersatz-Signal „Momentum“ ein.",
  volume: "Gehandeltes Volumen der letzten 24 Stunden über alle Börsen in USD (CoinGecko). Umschlag = Volumen / Marktkapitalisierung: hohe Werte zeigen hektischen Handel. Nicht Teil des Scores (außer als Ersatz-Signal „Umschlag“).",
  fdv: "Fully Diluted Valuation: Kurs × maximale Menge (oder Gesamtmenge, wenn es kein Maximum gibt). Liegt die Marktkapitalisierung weit darunter, kommen noch viele Coins auf den Markt (z. B. durch Freischaltungen), die den Kurs belasten können. Nicht Teil des Scores.",
  supply: "Umlaufende Coins (CoinGecko). Mit Maximum: Anteil, der bereits ausgegeben ist. Ohne Maximum kann die Menge unbegrenzt wachsen (Inflation); „ausgegeben“ ist die Gesamtmenge inklusive gesperrter Coins. Nicht Teil des Scores.",
  sectorChart: "Veränderung der Marktkapitalisierung je Sparte in den letzten 24 Stunden (CoinGecko). Steigt eine Sparte deutlich stärker als der Rest, fließt Kapital dorthin (Rotation). Mehr als 24 h liefert CoinGecko für Sparten nicht. Stablecoins fehlen hier: ihre Marktkap. folgt der ausgegebenen Menge, nicht dem Kurs.",
  sectorTurnover: "Handelsvolumen der letzten 24 h im Verhältnis zur Marktkapitalisierung der Sparte. Hohe Werte zeigen viel spekulativen Handel.",
  liqMap: "Geschätzte Liquidationsniveaus, kein echtes Orderbuch (Liquidationsaufträge sind nicht öffentlich). Modell: Steigt das Open Interest in einer Stunde, gelten neue Positionen als zum Durchschnittskurs eröffnet, je zur Hälfte long und short, mit angenommenen Hebeln (5x 10 %, 10x 25 %, 25x 30 %, 50x 20 %, 100x 15 %). Daraus folgen die Liquidationskurse: Longs darunter, Shorts darüber. Erreicht der Kurs ein Niveau, gilt es als abgeräumt; sinkt das OI, schrumpfen alle Niveaus anteilig. Je kräftiger die Farbe, desto mehr Volumen liegt dort. Große Cluster wirken oft wie Magnete, weil Liquidationen den Kurs dorthin ziehen können – ein Erfahrungswert, keine Regel. Positionen von vor dem Zeitraum fehlen.",
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
  // Ohne Eintrag in TIPS (z. B. Zeilen in Ranglisten) nur die Zusatzzeile zeigen
  tip.textContent = [TIPS[el.dataset.tip], el.dataset.tipNow].filter(Boolean).join("\n\n");
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
