#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stock Scanner v6.2
v6.2: yfinance 1.2.0 MultiIndex-Fix (beide Reihenfolgen), Cache auto-mkdir + korrupte Dateien
      auto-löschen, TTL Nacht/WE 24h, Score-Trend konsistent mit ICT, bh_start-Logikfix,
      RS absoluter Vergleich, ICT in Scan/Backtest
"""

import argparse, warnings, time, csv, logging, math, os, json, hashlib
import numpy as np
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
warnings.filterwarnings("ignore")
import yfinance as yf
import pandas as pd
import ta
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.columns import Columns
import urllib.request

console = Console()
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

SCORE_LONG  = 4
SCORE_SHORT = 0
CACHE_DIR   = os.path.expanduser("~/.scanner_cache")
LOG_DIR     = os.path.expanduser("~/.scanner_logs")
# Auto-Workers: für I/O-gebundene Netzwerkanfragen höher als CPU-Count
# bulk_download macht 1 API-Call, Workers nur für Analyse-Threads
_AUTO_WORKERS = min(32, max(8, (os.cpu_count() or 4) * 4))

def _cache_ttl():
    """Dynamische TTL: waehrend Handelszeiten 5min, sonst 24h.
    Tagesdaten aendern sich ausserhalb Boersenzeiten nicht → lange TTL sinnvoll.
    7-21 UTC: Europaeische + US-Boersen koennen geoeffnet sein → kurze TTL."""
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() < 5:  # Montag-Freitag
        h = now_utc.hour
        if 7 <= h < 21:   return 300   # Europaeische + US-Boersen (kombiniert)
    return 86400                        # Wochenende / Nacht: 24 Stunden

# ── Disk-Cache ────────────────────────────────────────────────────────────────
def _cache_path(ticker, period):
    key = hashlib.md5(f"{ticker}_{period}".encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{key}.parquet")

def cache_load(ticker, period):
    os.makedirs(CACHE_DIR, exist_ok=True)   # Ordner automatisch anlegen
    path = _cache_path(ticker, period)
    if not os.path.exists(path):
        return None
    if time.time() - os.path.getmtime(path) > _cache_ttl():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        # Korrupte Datei direkt löschen damit nächster Aufruf neu lädt
        try: os.remove(path)
        except Exception: pass
        return None

def cache_save(ticker, period, df):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        df.to_parquet(_cache_path(ticker, period))
    except Exception:
        pass

def cache_clear():
    if not os.path.exists(CACHE_DIR):
        console.print("[yellow]Kein Cache vorhanden.[/]")
        return
    files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".parquet")]
    for f in files:
        os.remove(os.path.join(CACHE_DIR, f))
    console.print(f"[green]✓ Cache geleert ({len(files)} Dateien)[/]")

# ── Signal-Log ───────────────────────────────────────────────────────────────
def log_signal(ticker, alt_signal, neu_signal, score, kurs):
    os.makedirs(LOG_DIR, exist_ok=True)
    fn = os.path.join(LOG_DIR, f"signals_{datetime.now().strftime('%Y%m%d')}.csv")
    neu = not os.path.exists(fn)
    try:
        with open(fn, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["timestamp","ticker","vorher","jetzt","score","kurs"])
            if neu: w.writeheader()
            w.writerow({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ticker":    ticker,
                "vorher":    alt_signal.replace("🟢","").replace("🔴","").replace("🟡","").strip(),
                "jetzt":     neu_signal.replace("🟢","").replace("🔴","").replace("🟡","").strip(),
                "score":     score,
                "kurs":      kurs,
            })
    except OSError as e:
        logging.warning(f"Signal-Log fehlgeschlagen: {e}")

# ── SPY-Cache ────────────────────────────────────────────────────────────────
_spy_cache = None
def get_spy_returns():
    global _spy_cache
    if _spy_cache is None:
        cached = cache_load("SPY", "3mo")
        if cached is not None:
            _spy_cache = cached["Close"]
        else:
            try:
                df = yf.Ticker("SPY").history(period="3mo")
                cache_save("SPY", "3mo", df)
                _spy_cache = df["Close"]
            except Exception as e:
                logging.warning(f"SPY-Abruf fehlgeschlagen: {e}")
                _spy_cache = pd.Series(dtype=float)
    return _spy_cache

# ── EUR-Kurse ─────────────────────────────────────────────────────────────────
def get_eur_rates():
    headers = {"User-Agent": "StockScanner"}
    urls = [
        "https://open.er-api.com/v6/latest/EUR",
        "https://api.frankfurter.app/latest?from=EUR",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
                if "rates" in data:
                    rates_raw = data["rates"]
                    eur_rates = {c: 1/v for c, v in rates_raw.items() if v != 0}
                    eur_rates["EUR"] = 1.0
                    return eur_rates
        except Exception as e:
            logging.warning(f"EUR-API {url}: {e}")
    return {"USD": 0.952, "GBP": 1.185, "CHF": 1.063, "JPY": 0.0063, "EUR": 1.0}

# ── Waehrung ──────────────────────────────────────────────────────────────────
def get_currency(ticker, last_price=None):
    if ticker.endswith((".DE", ".PA", ".AS", ".MI", ".MC", ".BR", ".VI")):
        return "EUR"
    elif ticker.endswith(".L"):
        return "GBp" if (last_price is not None and last_price > 500) else "GBP"
    elif ticker.endswith(".SW"):
        return "CHF"
    elif ticker.endswith(".T"):
        return "JPY"
    return "USD"

def to_eur(kurs, ticker, rates, last_price=None):
    cur = get_currency(ticker, last_price=last_price)
    if cur == "EUR":   return kurs
    elif cur == "GBp": return (kurs / 100) * rates.get("GBP", 1.185)
    elif cur == "GBP": return kurs * rates.get("GBP", 1.185)
    return kurs * rates.get(cur, 1.0)

# ── CSV Export ────────────────────────────────────────────────────────────────
def export_csv(ergebnisse, label):
    fn = f"scan_{label}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    try:
        with open(fn, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["ticker","kurs","score","signal","rsi","atr","info"])
            w.writeheader()
            for e in sorted(ergebnisse, key=lambda x: x["score"], reverse=True):
                w.writerow({
                    "ticker": e["ticker"], "kurs": e["kurs"], "score": e["score"],
                    "signal": e["signal"].replace("🟢","").replace("🔴","").replace("🟡","").strip(),
                    "rsi": e["rsi"], "atr": f"{e['atr']:.2f}" if e["atr"] else "-",
                    "info": e["info"],
                })
        console.print(f"\n[green]✓ CSV: {fn}[/]")
    except OSError as e:
        console.print(f"[red]✗ CSV fehlgeschlagen: {e}[/]")

# ── Watchlists ────────────────────────────────────────────────────────────────
WATCHLISTS = {
    "us_large_cap": {
        "name": "🇺🇸 US Large Cap",
        "tickers": ["AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AVGO","BRK-B","JPM",
                    "V","UNH","JNJ","XOM","PG","MA","HD","CVX","MRK","ABBV","KO","COST","PEP",
                    "ADBE","WMT","NFLX","CRM","TMO","DHR","LIN","ABT","ORCL","NKE","PM","INTU",
                    "TXN","QCOM","CSCO","ACN","MCD","NEE","HON","VZ","CMCSA","IBM","GE","UPS",
                    "RTX","BA","SPGI","CAT","LOW","AXP","AMGN","BLK","BKNG","GILD","SYK","ISRG",
                    "MMC","PLD","ZTS","CB","VRTX","REGN","TJX","SCHW","MU","CI","AMAT","DE",
                    "MDT","LRCX","ADI","BMY","SBUX","CVS","PYPL","PNC"],
    },
    "dax40": {
        "name": "🇩🇪 DAX 40",
        "tickers": ["SAP.DE","SIE.DE","ALV.DE","MBG.DE","BMW.DE","MUV2.DE","DTE.DE","BAYN.DE",
                    "BAS.DE","VOW3.DE","HEN3.DE","IFX.DE","DB1.DE","ADS.DE","HEI.DE","MRK.DE",
                    "FME.DE","RHM.DE","SHL.DE","BEI.DE","AIR.DE","CON.DE","DHL.DE","EOAN.DE",
                    "FRE.DE","HNR1.DE","LIN.DE","PAH3.DE","PUM.DE","QIA.DE","RWE.DE","SY1.DE",
                    "VNA.DE","ZAL.DE","MTX.DE","PNE3.DE","SZG.DE","WAF.DE","BC8.DE","ENR.DE"],
    },
    "europe_stoxx": {
        "name": "🇪🇺 Europa STOXX",
        "tickers": ["ASML.AS","MC.PA","SAN.MC","TTE.PA","BNP.PA","OR.PA","AIR.PA","INGA.AS",
                    "NESN.SW","NOVN.SW","ROG.SW","SHEL.L","AZN.L","ULVR.L","HSBA.L","BP.L","GSK.L"],
    },
    "swiss_smi": {
        "name": "🇨🇭 Schweiz SMI",
        "tickers": ["NESN.SW","ROG.SW","NOVN.SW","UHR.SW","ABBN.SW","ZURN.SW","UBSG.SW",
                    "GIVN.SW","SREN.SW","LONN.SW"],
    },
    "asia": {
        "name": "🌏🇨🇳 Asien",
        "tickers": ["7203.T","6758.T","9984.T","7267.T","6501.T","8306.T","9432.T","6861.T",
                    "BABA","JD","PDD","NIO","XPEV","LI","TSM"],
    },
    "tech_ai": {
        "name": "💻 Tech / AI / Chips",
        "tickers": ["NVDA","AMD","INTC","QCOM","AVGO","TXN","MU","AMAT","LRCX","KLAC","ASML",
                    "TSM","MSFT","GOOGL","META","ORCL","CRM","NOW","ADBE","SNOW","PLTR","CRWD",
                    "PANW","DDOG","ZS","NET","MDB","OKTA"],
    },
    "energy_resources": {
        "name": "⚡ Energy / Rohstoffe",
        "tickers": ["XOM","CVX","COP","EOG","OXY","MPC","PSX","VLO","SLB","HAL","SHEL","BP","NEE","DUK"],
    },
    "dividends": {
        "name": "💰 Dividenden",
        "tickers": ["KO","PEP","PG","JNJ","MMM","T","VZ","MO","PM","ABT","MRK","PFE","ABBV","O"],
    },
    "ipos": {
        "name": "🆕 IPOs 2023/2024",
        "tickers": ["ARM","KVYO","CART","BIRK","RDDT","RBRK","IONQ","ASTS","ACHR","JOBY"],
    },
    "crypto_stocks": {
        "name": "₿ Crypto Stocks",
        "tickers": ["COIN","MSTR","MARA","RIOT","CLSK","CIFR","BITF","HUT","IREN","WULF","XYZ","HOOD"],
    },
    "healthcare": {
        "name": "🏥 Healthcare / Biotech",
        "tickers": ["UNH","JNJ","ABT","LLY","MRK","PFE","ABBV","BMY","AMGN","GILD","BIIB",
                    "REGN","VRTX","ISRG","SYK","MDT"],
    },
    "banks_fintech": {
        "name": "🏦 Banken / Fintech",
        "tickers": ["JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF","BRK-B","AIG",
                    "MET","PYPL","AFRM","SOFI"],
    },
    "reits": {
        "name": "🏢 REITs / Immobilien",
        "tickers": ["O","NNN","ADC","WPC","PLD","STAG","EXR","PSA","BXP","EQR","AVB","CPT",
                    "MAA","VNA.DE","LEG.DE"],
    },
}

PORTFOLIO_FILE = os.path.expanduser("~/.scanner_portfolio.json")

def portfolio_laden():
    """Gespeichertes Portfolio laden.
    Gibt das vollstaendige gespeicherte JSON-Dict zurueck:
      {"gewichte": {ticker: prozent}, "depot": float}  oder {} wenn nicht vorhanden."""
    if not os.path.exists(PORTFOLIO_FILE):
        return {}
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def portfolio_speichern(gewichte_dict, depot=None):
    """Portfolio als JSON speichern. gewichte_dict: {ticker: prozent}"""
    daten = {"gewichte": gewichte_dict}
    if depot: daten["depot"] = depot
    try:
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(daten, f, indent=2)
        console.print(f"[green]✓ Portfolio gespeichert: {PORTFOLIO_FILE}[/]")
        console.print(f"[dim]  Ticker: {', '.join(gewichte_dict.keys())}[/]")
        console.print(f"[dim]  Jetzt: scanner backtest portfolio[/]")
    except OSError as e:
        console.print(f"[red]✗ Speichern fehlgeschlagen: {e}[/]")

def cmd_portfolio_save(args):
    """scanner portfolio-save VWRL.L:13.5 EUNL.DE:83.3 SPGP.L:1.8 3GOL.L:1.4 --depot 757"""
    targets = args.targets or []
    if not targets:
        console.print("[red]✗ Verwendung: scanner portfolio-save TICKER:GEWICHT ... [--depot BETRAG][/]")
        console.print("[dim]  Beispiel: scanner portfolio-save VWRL.L:13.5 EUNL.DE:83.3 SPGP.L:1.8 3GOL.L:1.4 --depot 757[/]")
        return

    gewichte = {}
    for item in targets:
        if ":" in item:
            t, w = item.split(":", 1)
            try:
                gewichte[t.upper()] = float(w)
            except ValueError:
                console.print(f"[yellow]⚠ Ungültiger Wert: {item} – übersprungen[/]")
        else:
            console.print(f"[yellow]⚠ Kein Gewicht für {item} – bitte TICKER:GEWICHT Format nutzen[/]")

    if not gewichte:
        console.print("[red]✗ Keine gültigen Ticker:Gewicht Paare gefunden[/]")
        return

    depot = args.depot  # None wenn nicht angegeben, gespeicherter Wert sonst
    portfolio_speichern(gewichte, depot=depot)

def get_tickers(list_names):
    tickers = []
    for name in list_names:
        if name == "all":
            for wl in WATCHLISTS.values():
                tickers.extend(wl["tickers"])
        elif name in WATCHLISTS:
            tickers.extend(WATCHLISTS[name]["tickers"])
        else:
            tickers.append(name.upper())
    seen = set()
    return [t for t in tickers if not (t in seen or seen.add(t))]

# ── Bulk-Download (yfinance download() statt einzelner Ticker) ───────────────
def _flatten_df(df, ticker=None):
    """Normalisiert MultiIndex-Columns die yfinance liefert.
    Unterstuetzt beide Reihenfolgen:
      Alt (yfinance <1.2): ('Close', 'NVDA') → level=1 ist Ticker
      Neu (yfinance >=1.2): ('NVDA', 'Close') → level=0 ist Ticker
    """
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        if ticker:
            # Herausfinden welches Level den Ticker enthält
            lvl = None
            for l in [0, 1]:
                if ticker in df.columns.get_level_values(l):
                    lvl = l
                    break
            if lvl is not None:
                try:
                    df = df.xs(ticker, axis=1, level=lvl)
                except Exception:
                    # Fallback: das andere Level ist der Spaltenname
                    col_lvl = 1 - lvl
                    df.columns = [c[col_lvl] if isinstance(c, tuple) else c for c in df.columns]
            else:
                # Ticker nicht gefunden – nehme level=1 als Spaltenname
                df.columns = [c[1] if isinstance(c, tuple) and len(c) > 1 else c for c in df.columns]
        else:
            # Kein Ticker bekannt – prüfe ob level=1 OHLCV-Namen enthält
            lvl1_vals = set(df.columns.get_level_values(1))
            if "Close" in lvl1_vals or "Open" in lvl1_vals:
                df.columns = [c[1] if isinstance(c, tuple) else c for c in df.columns]
            else:
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    # Sicherstellen dass 'Close' existiert
    if "Close" not in df.columns and "Adj Close" in df.columns:
        df = df.rename(columns={"Adj Close": "Close"})
    return df

def bulk_download(tickers, period="1y"):
    """Laedt alle Ticker in einem einzigen API-Call. Gibt dict {ticker: df} zurueck."""
    result = {}
    # Zuerst Cache pruefen
    missing = []
    for t in tickers:
        cached = cache_load(t, period)
        if cached is not None:
            result[t] = cached
        else:
            missing.append(t)

    if not missing:
        return result

    try:
        raw = yf.download(
            missing, period=period, interval="1d",
            auto_adjust=True, prepost=False,
            group_by="ticker", threads=True, progress=False
        )
        now = datetime.now(timezone.utc)

        for t in missing:
            try:
                if len(missing) == 1:
                    # Einzelticker: yfinance gibt manchmal MultiIndex zurück
                    df = _flatten_df(raw.copy(), ticker=t)
                else:
                    # Mehrere Ticker: Level automatisch erkennen (yfinance 1.2.0 hat umgekehrten MultiIndex)
                    try:
                        df = _flatten_df(raw.copy(), ticker=t)
                    except Exception:
                        col = raw.get(t)
                        if col is None or not isinstance(col, pd.DataFrame):
                            logging.debug(f"bulk_download: kein DataFrame fuer {t}")
                            continue
                        df = _flatten_df(col.copy(), ticker=t)

                if "Close" not in df.columns:
                    logging.debug(f"bulk_download: kein 'Close' fuer {t}, Spalten: {list(df.columns)}")
                    continue

                df = df.dropna(how="all")
                if df.empty or len(df) < 50:
                    continue

                last_date = df.index[-1].to_pydatetime()
                if last_date.tzinfo is None:
                    last_date = last_date.replace(tzinfo=timezone.utc)
                if (now - last_date).days > 7:
                    continue

                cache_save(t, period, df)
                result[t] = df
            except Exception as e:
                logging.debug(f"bulk_download {t}: {e}")
    except Exception as e:
        logging.warning(f"Bulk-Download fehlgeschlagen: {e}")
        # Fallback: einzeln laden
        for t in missing:
            df, _, _ = lade_daten_single(t, period)
            if not df.empty:
                result[t] = df

    return result

# ── Einzelladen (Fallback + cmd_info) ────────────────────────────────────────
def lade_daten_single(ticker, period="1y"):
    cached = cache_load(ticker, period)
    if cached is not None:
        split_flag = cached["Close"].tail(6).pct_change().iloc[1:].abs().max() > 0.50
        return cached, split_flag, ("⚠ SPLIT?" if split_flag else "OK")
    try:
        df = yf.Ticker(ticker).history(period=period, interval="1d",
                                        auto_adjust=True, prepost=False)
        df = _flatten_df(df, ticker=ticker)  # MultiIndex-Schutz
        if df.empty or len(df) < 50:
            return pd.DataFrame(), False, f"Keine Daten ({len(df)} Tage)"
        last_date = df.index[-1].to_pydatetime()
        last_date = last_date.replace(tzinfo=timezone.utc) if last_date.tzinfo is None else last_date
        if (datetime.now(timezone.utc) - last_date).days > 7:
            return df, False, f"DELISTED ({last_date.strftime('%d.%m.%Y')})"
        cache_save(ticker, period, df)
        split_flag = df["Close"].tail(6).pct_change().iloc[1:].abs().max() > 0.50
        return df, split_flag, ("⚠ SPLIT?" if split_flag else "OK")
    except Exception as e:
        logging.warning(f"{ticker}: {e}")
        return pd.DataFrame(), False, f"ERROR: {str(e)[:40]}"

def berechne_atr(df, window=14):
    try:
        return ta.volatility.AverageTrueRange(
            df["High"], df["Low"], df["Close"], window=window
        ).average_true_range().iloc[-1]
    except Exception:
        return None

# ── Score ─────────────────────────────────────────────────────────────────────
def berechne_score(df, spy_returns=None, _compute_trend=True):
    """Berechnet den technischen Score eines Tickers.
    _compute_trend=False verhindert rekursive Trend-Berechnung (max. 1 Ebene Tiefe)."""
    if df.empty or len(df) < 50:
        return 0, {}
    close = df["Close"]
    kurs  = close.iloc[-1]
    score = 0
    details = {}
    try:
        sma200 = ta.trend.SMAIndicator(close, window=200).sma_indicator().iloc[-1]
        if not pd.isna(sma200):
            if kurs > sma200: score += 1; details["SMA200"] = "+1"
            else:             score -= 3; details["SMA200"] = "-3"

        sma50 = ta.trend.SMAIndicator(close, window=50).sma_indicator().iloc[-1]
        if not pd.isna(sma50) and not pd.isna(sma200) and sma50 > sma200:
            score += 1; details["Cross"] = "+1"

        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        if not pd.isna(rsi_val):
            details["rsi_raw"] = rsi_val
            if rsi_val < 35:   score += 2; details["RSI"] = "+2"
            elif rsi_val < 50: score += 1; details["RSI"] = "+1"
            elif rsi_val > 75: score -= 2; details["RSI"] = "-2"
            elif rsi_val > 70: score -= 1; details["RSI"] = "-1"

        macd_hist = ta.trend.MACD(close).macd_diff().iloc[-1]
        if not pd.isna(macd_hist):
            if macd_hist > 0: score += 1; details["MACD"] = "+1"
            else:             score -= 1; details["MACD"] = "-1"

        sma20 = ta.trend.SMAIndicator(close, window=20).sma_indicator().iloc[-1]
        if not pd.isna(sma20) and kurs > sma20:
            score += 1; details["SMA20"] = "+1"

        sma10 = ta.trend.SMAIndicator(close, window=10).sma_indicator().iloc[-1]
        if not pd.isna(sma10) and kurs < sma10:
            score -= 1; details["SMA10"] = "-1"

        bb_lower = ta.volatility.BollingerBands(close).bollinger_lband().iloc[-1]
        if not pd.isna(bb_lower) and kurs < bb_lower:
            score += 1; details["BB"] = "+1"

        vol_ma20 = df["Volume"].rolling(20).mean().iloc[-1]
        if not pd.isna(vol_ma20) and vol_ma20 > 0 and df["Volume"].iloc[-1] > vol_ma20 * 1.5:
            score += 1; details["Vol"] = "+1"

        weekly = close.resample("W").last()
        if len(weekly) >= 2 and not pd.isna(weekly.iloc[-1]) and not pd.isna(weekly.iloc[-2]):
            if weekly.iloc[-1] > weekly.iloc[-2]:
                score += 1; details["Weekly"] = "+1"
        if len(weekly) >= 15:
            w_rsi = ta.momentum.RSIIndicator(weekly, window=14).rsi().iloc[-1]
            if not pd.isna(w_rsi) and w_rsi > 80:
                score -= 1; details["W-RSI>80"] = "-1"

        spy = spy_returns if spy_returns is not None else get_spy_returns()
        if len(spy) > 1 and len(close) > 1:
            spy_ret   = spy.pct_change(20).iloc[-1]
            stock_ret = close.pct_change(20).iloc[-1]
            if not pd.isna(spy_ret) and not pd.isna(stock_ret):
                diff = stock_ret - spy_ret
                if diff > 0.02:    score += 1; details["RS"] = "+1"
                elif diff < -0.02: score -= 1; details["RS"] = "-1"

        h52 = close.rolling(252, min_periods=50).max().iloc[-1]
        l52 = close.rolling(252, min_periods=50).min().iloc[-1]
        if not pd.isna(h52) and not pd.isna(l52) and h52 > 0 and l52 > 0:
            if (h52 - kurs) / h52 < 0.10:   score += 2; details["52W-H"] = "+2"
            elif (kurs - l52) / l52 < 0.10: score += 1; details["52W-L"] = "+1"

        if len(df) >= 2:
            prev = close.iloc[-2]
            if not pd.isna(prev) and prev > 0:
                gap = abs((kurs / prev - 1) * 100)
                if gap > 3:
                    if kurs > prev: score += 1; details["Gap+"] = f"+1 ({gap:.1f}%)"
                    else:           score -= 1; details["Gap-"] = f"-1 ({gap:.1f}%)"

    except Exception as e:
        logging.warning(f"Score-Berechnung: {e}")

    # ICT-Bonus addieren (vor Score-Trend, damit Trend konsistent ist)
    try:
        ict_score, ict_details = score_ict(df)
        score  += ict_score
        details.update(ict_details)
    except Exception as e:
        logging.debug(f"score_ict in berechne_score: {e}")

    # Score-Trend: Score vor 5 Tagen vs heute (beide inkl. ICT)
    # _compute_trend=False verhindert weitere Rekursion (nur 1 Ebene tief)
    try:
        if _compute_trend and len(df) >= 55:
            score_alt, _ = berechne_score(df.iloc[:-5], spy_returns=spy_returns, _compute_trend=False)
            diff = score - score_alt
            if diff > 0:   details["trend"] = f"+{diff}"
            elif diff < 0: details["trend"] = str(diff)
            else:          details["trend"] = "="
    except Exception as e:
        logging.debug(f"Score-Trend: {e}")

    return score, details

# ── ICT Konzepte (Inner Circle Trader) ───────────────────────────────────────
def detect_fvg(df, lookback=30):
    """Fair Value Gaps: Luecke zwischen Kerze-1-High und Kerze-3-Low (bullisch)
    oder Kerze-1-Low und Kerze-3-High (baerisch)."""
    gaps = []
    highs  = df["High"].values
    lows   = df["Low"].values
    n      = len(df)
    start  = max(0, n - lookback)
    for i in range(start, n - 2):
        if lows[i + 2] > highs[i]:          # Bullischer FVG
            gaps.append({"typ": "bull", "oben": lows[i + 2], "unten": highs[i], "idx": i})
        if highs[i + 2] < lows[i]:          # Baerischer FVG
            gaps.append({"typ": "bear", "oben": lows[i],     "unten": highs[i + 2], "idx": i})
    return gaps

def detect_order_blocks(df, lookback=50, swing_pct=0.015):
    """Order Blocks: letzte Gegenkerze vor einer starken Bewegung.
    Bullisch = rote Kerze vor Anstieg | Baerisch = gruene Kerze vor Absturz."""
    obs    = []
    closes = df["Close"].values
    opens  = df["Open"].values if "Open" in df.columns else df["Close"].values
    n      = len(df)
    start  = max(1, n - lookback)
    for i in range(start, n - 3):
        if closes[i + 1] == 0: continue
        move = (closes[i + 3] - closes[i + 1]) / closes[i + 1]
        if closes[i] < opens[i] and move > swing_pct:       # Bullischer OB
            obs.append({"typ": "bull", "oben": opens[i], "unten": closes[i], "idx": i})
        elif closes[i] > opens[i] and move < -swing_pct:    # Baerischer OB
            obs.append({"typ": "bear", "oben": closes[i], "unten": opens[i], "idx": i})
    return obs

def detect_liquidity_sweep(df, lookback=20):
    """Liquidity Sweep: Kurs durchbricht bekanntes Hoch/Tief kurz und dreht um.
    Signalisiert dass Institutionelle Stop-Orders abgegriffen haben."""
    sweeps = []
    highs  = df["High"].values
    lows   = df["Low"].values
    closes = df["Close"].values
    n      = len(df)
    if n < lookback + 3: return sweeps
    for i in range(lookback, n - 1):
        recent_high = max(highs[i - lookback:i])
        recent_low  = min(lows[i  - lookback:i])
        # Bullischer Sweep: kurz unter Recent Low, dann Erholung
        if lows[i] < recent_low and closes[i] > recent_low:
            sweeps.append({"typ": "bull", "level": recent_low, "idx": i})
        # Baerischer Sweep: kurz ueber Recent High, dann Umkehr
        if highs[i] > recent_high and closes[i] < recent_high:
            sweeps.append({"typ": "bear", "level": recent_high, "idx": i})
    return sweeps

def detect_bos(df, lookback=20):
    """Break of Structure: Kurs bricht ueber/unter den Bereich der letzten N Kerzen.
    Bullisch = neue Staerke | Baerisch = neue Schwaeche."""
    n = len(df)
    if n < lookback + 2: return None
    highs  = df["High"].values
    lows   = df["Low"].values
    closes = df["Close"].values
    recent_high = max(highs[-lookback - 1:-1])
    recent_low  = min(lows[-lookback  - 1:-1])
    if closes[-1] > recent_high: return "bull"
    if closes[-1] < recent_low:  return "bear"
    return None

def score_ict(df, lookback=20):
    """ICT-Score-Bonus basierend auf FVG, Order Blocks, Liquidity Sweep und BOS.
    Gibt (score_delta, details_dict) zurueck – wird zu berechne_score addiert."""
    if df.empty or len(df) < 50:
        return 0, {}
    score   = 0
    details = {}
    try:
        kurs = df["Close"].iloc[-1]

        # Fair Value Gaps
        fvgs        = detect_fvg(df, lookback=lookback)
        in_bull_fvg = any(g["typ"] == "bull" and g["unten"] <= kurs <= g["oben"] for g in fvgs)
        in_bear_fvg = any(g["typ"] == "bear" and g["unten"] <= kurs <= g["oben"] for g in fvgs)
        if in_bull_fvg: score += 2; details["FVG-Bull"] = "+2"
        if in_bear_fvg: score -= 2; details["FVG-Bear"] = "-2"

        # Order Blocks
        obs        = detect_order_blocks(df)
        on_bull_ob = any(o["typ"] == "bull" and o["unten"] <= kurs <= o["oben"] for o in obs)
        on_bear_ob = any(o["typ"] == "bear" and o["unten"] <= kurs <= o["oben"] for o in obs)
        if on_bull_ob: score += 2; details["OB-Bull"] = "+2"
        if on_bear_ob: score -= 2; details["OB-Bear"] = "-2"

        # Liquidity Sweeps (nur letzte 3 Tage relevant)
        sweeps        = detect_liquidity_sweep(df, lookback=lookback)
        recent_sweeps = [s for s in sweeps if s["idx"] >= len(df) - 3]
        if any(s["typ"] == "bull" for s in recent_sweeps): score += 1; details["Sweep-Bull"] = "+1"
        if any(s["typ"] == "bear" for s in recent_sweeps): score -= 1; details["Sweep-Bear"] = "-1"

        # Break of Structure
        bos = detect_bos(df, lookback=lookback)
        if   bos == "bull": score += 1; details["BOS-Bull"] = "+1"
        elif bos == "bear": score -= 1; details["BOS-Bear"] = "-1"

    except Exception as e:
        logging.debug(f"score_ict: {e}")
    return score, details

# ── Backtest Precompute ───────────────────────────────────────────────────────
def precompute_serien(df, spy_returns=None):
    close = df["Close"]
    s = {}
    try:
        s["sma200"]    = ta.trend.SMAIndicator(close, window=200).sma_indicator()
        s["sma50"]     = ta.trend.SMAIndicator(close, window=50).sma_indicator()
        s["sma20"]     = ta.trend.SMAIndicator(close, window=20).sma_indicator()
        s["sma10"]     = ta.trend.SMAIndicator(close, window=10).sma_indicator()
        s["rsi"]       = ta.momentum.RSIIndicator(close, window=14).rsi()
        s["macd_diff"] = ta.trend.MACD(close).macd_diff()
        s["bb_lower"]  = ta.volatility.BollingerBands(close).bollinger_lband()
        s["vol_ma20"]  = df["Volume"].rolling(20).mean()
        s["high_52w"]  = close.rolling(252, min_periods=50).max()
        s["low_52w"]   = close.rolling(252, min_periods=50).min()
        s["pct20"]     = close.pct_change(20)
        s["atr"]       = ta.volatility.AverageTrueRange(
                            df["High"], df["Low"], close, window=14).average_true_range()
        weekly = close.resample("W").last()
        s["weekly"] = weekly
        if len(weekly) >= 15:
            s["weekly_rsi"] = ta.momentum.RSIIndicator(weekly, window=14).rsi()
        if spy_returns is not None and len(spy_returns) > 20:
            s["spy_pct20"] = spy_returns.pct_change(20)
    except Exception as e:
        logging.warning(f"precompute_serien: {e}")
    return s

def score_bei_index(i, df, s):
    # Guard: leere Serien oder Index ausserhalb Bereich
    if not s or df.empty or i < 0 or i >= len(df):
        return 0
    close = df["Close"]
    kurs  = close.iloc[i]
    if pd.isna(kurs) or kurs <= 0:
        return 0
    score = 0
    try:
        sma200_v = s["sma200"].iloc[i] if "sma200" in s else float("nan")
        if not pd.isna(sma200_v):
            score += 1 if kurs > sma200_v else -3

        sma50_v = s["sma50"].iloc[i] if "sma50" in s else float("nan")
        if not pd.isna(sma50_v) and not pd.isna(sma200_v) and sma50_v > sma200_v:
            score += 1

        rsi_v = s["rsi"].iloc[i] if "rsi" in s else float("nan")
        if not pd.isna(rsi_v):
            if rsi_v < 35:   score += 2
            elif rsi_v < 50: score += 1
            elif rsi_v > 75: score -= 2
            elif rsi_v > 70: score -= 1

        macd_v = s["macd_diff"].iloc[i] if "macd_diff" in s else float("nan")
        if not pd.isna(macd_v):
            score += 1 if macd_v > 0 else -1

        sma20_v = s["sma20"].iloc[i] if "sma20" in s else float("nan")
        if not pd.isna(sma20_v) and kurs > sma20_v:
            score += 1

        sma10_v = s["sma10"].iloc[i] if "sma10" in s else float("nan")
        if not pd.isna(sma10_v) and kurs < sma10_v:
            score -= 1

        bb_v = s["bb_lower"].iloc[i] if "bb_lower" in s else float("nan")
        if not pd.isna(bb_v) and kurs < bb_v:
            score += 1

        vol_v   = df["Volume"].iloc[i]
        vol_ma  = s["vol_ma20"].iloc[i] if "vol_ma20" in s else float("nan")
        if not pd.isna(vol_ma) and vol_ma > 0 and vol_v > vol_ma * 1.5:
            score += 1

        if "weekly" in s:
            w = s["weekly"]
            wb = w[w.index <= df.index[i]]
            if len(wb) >= 2 and not pd.isna(wb.iloc[-1]) and not pd.isna(wb.iloc[-2]):
                if wb.iloc[-1] > wb.iloc[-2]:
                    score += 1
            if "weekly_rsi" in s:
                wr = s["weekly_rsi"]
                wrb = wr[wr.index <= df.index[i]]
                if len(wrb) > 0 and not pd.isna(wrb.iloc[-1]) and wrb.iloc[-1] > 80:
                    score -= 1

        if "spy_pct20" in s and "pct20" in s:
            # Datumsbasierte Ausrichtung statt fehlerhaftem Integer-Index-Clamping.
            # asof() liefert den letzten verfuegbaren SPY-Wert an/vor dem Handelstag.
            # Gibt NaN zurueck wenn Datum vor dem ersten SPY-Eintrag liegt → korrekt ignoriert.
            try:
                stock_date = df.index[i]
                spy_pct20  = s["spy_pct20"]
                # Timezone-Normalisierung: beide Seiten auf naive (tz-unaware) bringen
                if hasattr(spy_pct20.index, 'tz') and spy_pct20.index.tz is not None:
                    spy_pct20 = spy_pct20.copy()
                    spy_pct20.index = spy_pct20.index.tz_localize(None)
                if hasattr(stock_date, 'tzinfo') and stock_date.tzinfo is not None:
                    stock_date = stock_date.replace(tzinfo=None)
                spy_ret = spy_pct20.asof(stock_date)
            except Exception:
                spy_ret = float("nan")
            stock_ret = s["pct20"].iloc[i]
            if not pd.isna(spy_ret) and not pd.isna(stock_ret):
                diff = stock_ret - spy_ret
                if diff > 0.02:    score += 1
                elif diff < -0.02: score -= 1

        h52 = s["high_52w"].iloc[i] if "high_52w" in s else float("nan")
        l52 = s["low_52w"].iloc[i]  if "low_52w"  in s else float("nan")
        if not pd.isna(h52) and not pd.isna(l52) and h52 > 0 and l52 > 0:
            if (h52 - kurs) / h52 < 0.10:   score += 2
            elif (kurs - l52) / l52 < 0.10: score += 1

        if i >= 1:
            prev = close.iloc[i-1]
            if not pd.isna(prev) and prev > 0:
                gap = abs((kurs / prev - 1) * 100)
                if gap > 3:
                    score += 1 if kurs > prev else -1
    except Exception as e:
        logging.debug(f"score_bei_index({i}): {e}")

    # ICT-Bonus: nur für den aktuellen (letzten) Index sinnvoll,
    # da ICT Muster auf dem vollen df berechnet werden
    if i == len(df) - 1:
        try:
            ict_score, _ = score_ict(df)
            score += ict_score
        except Exception as e:
            logging.debug(f"score_bei_index ICT({i}): {e}")

    return score

# ── Backtest Metriken ─────────────────────────────────────────────────────────
def berechne_max_drawdown(equity_kurve):
    if len(equity_kurve) < 2: return 0.0
    peak = equity_kurve[0]; max_dd = 0.0
    for v in equity_kurve:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
    return max_dd

def berechne_sharpe(returns_liste):
    if len(returns_liste) < 2: return 0.0
    avg = sum(returns_liste) / len(returns_liste)
    var = sum((r - avg)**2 for r in returns_liste) / (len(returns_liste) - 1)
    std = math.sqrt(var) if var > 0 else 0.0001
    return (avg / std) * math.sqrt(12)

# ── ASCII Equity-Kurve ────────────────────────────────────────────────────────
def ascii_chart(values, width=60, height=10, title=""):
    if len(values) < 2:
        return ""
    mn, mx = min(values), max(values)
    if mx == mn: mx = mn + 1
    rows = []
    for row in range(height):
        threshold = mx - (mx - mn) * row / (height - 1)
        line = ""
        step = max(1, len(values) // width)
        for col in range(min(width, len(values))):
            idx = col * step
            v   = values[min(idx, len(values)-1)]
            line += "█" if v >= threshold else " "
        rows.append(f"{threshold:>10.0f} |{line}|")
    rows.append(" " * 11 + "+" + "-" * min(width, len(values)) + "+")
    if title:
        rows.insert(0, f" {title}")
    return "\n".join(rows)

# ── CMD: portfolio ────────────────────────────────────────────────────────────
def _kelly_berechnen(close_serie):
    """Kelly-Kriterium aus 30-Tage-Returns einer Close-Serie."""
    ret30 = close_serie.pct_change(30).dropna() * 100
    pos   = ret30[ret30 > 0]
    neg   = ret30[ret30 < 0]
    if len(pos) == 0 or len(neg) == 0:
        return None, None, None, None
    win_rate  = len(pos) / len(ret30)
    avg_win   = pos.mean()
    avg_loss  = abs(neg.mean())
    odds      = avg_win / avg_loss if avg_loss > 0 else 1
    kelly     = win_rate - (1 - win_rate) / odds
    return win_rate, avg_win, avg_loss, kelly

def cmd_portfolio(args):
    """Portfolio-Analyse: Signale, Korrelation, Kelly, Markowitz, Empfehlungen."""

    # ── Portfolio einlesen ────────────────────────────────────────────────────
    portfolio = {}  # {ticker: rohgewicht}
    pf_file   = args.file if hasattr(args, "file") and args.file else None

    if pf_file:
        try:
            with open(pf_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    t = row.get("ticker", row.get("Ticker", "")).upper().strip()
                    w = float(row.get("gewicht", row.get("weight", row.get("Gewicht", 1))))
                    if t: portfolio[t] = w
        except Exception as e:
            console.print(f"[red]✗ CSV Fehler: {e}[/]")
            console.print("[dim]Format: ticker,gewicht   (z.B. NVDA,30)[/]")
            return
    elif args.targets:
        for item in args.targets:
            if ":" in item:
                t, w = item.split(":", 1)
                portfolio[t.upper()] = float(w)
            else:
                portfolio[item.upper()] = 1.0
    else:
        console.print("[red]✗ Portfolio angeben:[/]")
        console.print("  scanner portfolio NVDA:30 AAPL:20 SAP:50")
        console.print("  scanner portfolio --file mein_portfolio.csv")
        console.print("\n[dim]CSV-Format: ticker,gewicht (z.B. NVDA,30)[/]")
        return

    if not portfolio:
        console.print("[red]✗ Keine Positionen gefunden.[/]")
        return

    # Gewichte auf 100% normalisieren
    total     = sum(portfolio.values())
    portfolio = {t: w / total * 100 for t, w in portfolio.items()}
    tickers   = list(portfolio.keys())

    console.print(Panel(
        "\n".join(f"  {t}: {w:.1f}%" for t, w in portfolio.items()),
        title=f"💼 Portfolio – {len(tickers)} Positionen", border_style="cyan"
    ))

    period   = args.period or "1y"
    rates    = get_eur_rates()
    spy_data = get_spy_returns()
    depot    = args.depot if hasattr(args, "depot") and args.depot is not None else 10000

    console.print("[dim]Lade Daten...[/]", end="\r")
    data_map = bulk_download(tickers, period=period)
    console.print(f"[green]✓ {len(data_map)}/{len(tickers)} Ticker geladen[/]")

    # ── 1. Aktuelle Signale + ICT ─────────────────────────────────────────────
    console.print("\n[bold cyan]═══ 1. Aktuelle Signale (inkl. ICT) ═══[/]")
    sig_table   = Table(box=box.ROUNDED)
    sig_table.add_column("Ticker",   style="cyan", no_wrap=True)
    sig_table.add_column("Gewicht",  justify="right")
    sig_table.add_column("Kurs",     justify="right", style="green")
    sig_table.add_column("Score",    justify="right")
    sig_table.add_column("ICT+",     justify="right")
    sig_table.add_column("Total",    justify="right")
    sig_table.add_column("Signal",   style="bold magenta")
    sig_table.add_column("RSI",      justify="right")

    scores_dict = {}
    returns_dict = {}
    ict_cache = {}   # {ticker: (ict_score, ict_details)} – einmal berechnen, dreimal nutzen

    for ticker in tickers:
        df = data_map.get(ticker, pd.DataFrame())
        if df.empty:
            sig_table.add_row(ticker, f"{portfolio[ticker]:.1f}%",
                              "-", "-", "-", "-", "[dim]n/a[/]", "-")
            scores_dict[ticker] = 0
            ict_cache[ticker]   = (0, {})
            continue

        score_base, details_base = berechne_score(df, spy_returns=spy_data)
        ict_score,  ict_details  = score_ict(df)
        ict_cache[ticker] = (ict_score, ict_details)
        # berechne_score enthält ICT bereits – deshalb nur Basis ohne ICT separat anzeigen
        # Wir trennen: Base ohne ICT, ICT-Teil separat
        score_ohne_ict = score_base - ict_score
        total_score    = score_base   # enthält ICT bereits

        scores_dict[ticker]  = total_score
        returns_dict[ticker] = df["Close"].pct_change().dropna()

        kurs_raw = df["Close"].iloc[-1]
        kurs_eur = to_eur(kurs_raw, ticker, rates, last_price=kurs_raw)
        rsi_val  = details_base.get("rsi_raw")
        rsi_str  = f"{rsi_val:.0f}" if rsi_val is not None else "-"
        rsi_col  = "green" if rsi_val is not None and rsi_val < 35 else "red" if rsi_val is not None and rsi_val > 70 else "white"
        sc_col   = "green" if total_score >= 5 else "red" if total_score <= 0 else "yellow"
        ict_col  = "green" if ict_score > 0 else "red" if ict_score < 0 else "dim"
        sig      = ("🟢 LONG"   if total_score >= SCORE_LONG
               else "🔴 SHORT"  if total_score <= SCORE_SHORT
               else "🟡 NEUTRAL")

        sig_table.add_row(
            ticker[:10], f"{portfolio[ticker]:.1f}%",
            f"{kurs_eur:.2f}EUR",
            f"[{sc_col}]{score_ohne_ict}[/]",
            f"[{ict_col}]{ict_score:+d}[/]",
            f"[bold {sc_col}]{total_score}[/]",
            sig,
            f"[{rsi_col}]{rsi_str}[/]",
        )
    console.print(sig_table)

    # ── 2. Korrelationsmatrix ─────────────────────────────────────────────────
    console.print("\n[bold cyan]═══ 2. Korrelationsmatrix ═══[/]")
    valid_tickers = [t for t in tickers if t in returns_dict and len(returns_dict[t]) > 20]

    if len(valid_tickers) >= 2:
        ret_df = pd.DataFrame({t: returns_dict[t] for t in valid_tickers}).dropna()
        corr   = ret_df.corr()

        corr_table = Table(box=box.SIMPLE)
        corr_table.add_column("", style="cyan bold")
        for t in corr.columns:
            corr_table.add_column(t[:7], justify="right")

        for t1 in corr.index:
            row = [t1[:7]]
            for t2 in corr.columns:
                v = corr.loc[t1, t2]
                if t1 == t2:
                    row.append("[dim]1.00[/]")
                elif abs(v) >= 0.8:
                    row.append(f"[bold red]{v:.2f}[/]")
                elif abs(v) >= 0.6:
                    row.append(f"[yellow]{v:.2f}[/]")
                else:
                    row.append(f"[green]{v:.2f}[/]")
            corr_table.add_row(*row)

        console.print(Panel(
            corr_table,
            title="Korrelation  [green]<0.6 gut[/]  [yellow]0.6-0.8 mittel[/]  [red]>0.8 Klumpenrisiko[/]",
            border_style="blue"
        ))

        # Klumpenrisiko-Warnungen
        high_pairs = []
        for i, t1 in enumerate(corr.index):
            for t2 in list(corr.columns)[i + 1:]:
                v = corr.loc[t1, t2]
                if v > 0.8:
                    high_pairs.append(f"  ⚠ {t1} ↔ {t2}: {v:.2f}  →  kaum Diversifikation")
        if high_pairs:
            console.print(Panel("\n".join(high_pairs),
                                title="⚠ Klumpenrisiko", style="yellow"))
        else:
            console.print("[green]✓ Keine kritischen Korrelationen (>0.8) gefunden.[/]")
    else:
        console.print("[yellow]Mindestens 2 Ticker mit Daten nötig für Korrelation.[/]")
        ret_df = pd.DataFrame()

    # ── 3. Kelly-Kriterium ────────────────────────────────────────────────────
    console.print("\n[bold cyan]═══ 3. Kelly-Kriterium (optimale Positionsgrößen) ═══[/]")
    kelly_table = Table(box=box.ROUNDED)
    kelly_table.add_column("Ticker",      style="cyan")
    kelly_table.add_column("Aktuell",     justify="right")
    kelly_table.add_column("Win-Rate",    justify="right")
    kelly_table.add_column("Ø Gewinn",    justify="right")
    kelly_table.add_column("Ø Verlust",   justify="right")
    kelly_table.add_column("Kelly %",     justify="right")
    kelly_table.add_column("½ Kelly %",   justify="right", style="bold")
    kelly_table.add_column("Empfehlung",  justify="center")

    kelly_results = {}
    for ticker in tickers:
        df = data_map.get(ticker, pd.DataFrame())
        if df.empty or len(df) < 60:
            kelly_table.add_row(ticker, f"{portfolio[ticker]:.1f}%",
                                "-", "-", "-", "-", "-", "[dim]n/a[/]")
            continue

        wr, avg_win, avg_loss, kelly = _kelly_berechnen(df["Close"])
        if kelly is None:
            kelly_table.add_row(ticker, f"{portfolio[ticker]:.1f}%",
                                "-", "-", "-", "-", "-", "[dim]n/a[/]")
            continue

        half_kelly   = max(0.0, kelly / 2) * 100
        kelly_pct    = kelly * 100
        aktuell      = portfolio[ticker]
        kelly_results[ticker] = half_kelly

        if half_kelly > aktuell + 5:   empf = "[green]↑ erhöhen[/]"
        elif half_kelly < aktuell - 5: empf = "[red]↓ reduzieren[/]"
        else:                          empf = "[dim]✓ ok[/]"

        wr_col = "green" if wr >= 0.55 else "red" if wr < 0.45 else "yellow"
        k_col  = "green" if kelly_pct > 0 else "red"

        kelly_table.add_row(
            ticker,
            f"{aktuell:.1f}%",
            f"[{wr_col}]{wr * 100:.1f}%[/]",
            f"[green]+{avg_win:.1f}%[/]",
            f"[red]-{avg_loss:.1f}%[/]",
            f"[{k_col}]{kelly_pct:.1f}%[/]",
            f"{half_kelly:.1f}%",
            empf,
        )
    console.print(kelly_table)
    console.print(Panel(
        "[dim]½ Kelly = konservative Variante des Kelly-Kriteriums.\n"
        "Empfehlung: nie mehr als ½ Kelly pro Position riskieren.\n"
        "Negativer Kelly = historisch schlechtes Chance/Risiko-Verhältnis.[/]",
        title="ℹ Kelly-Hinweis", border_style="dim"
    ))

    # ── 4. Markowitz-Optimierung ──────────────────────────────────────────────
    console.print("\n[bold cyan]═══ 4. Markowitz-Optimierung (Max Sharpe) ═══[/]")

    if len(valid_tickers) < 2 or ret_df.empty:
        console.print("[yellow]Markowitz übersprungen – zu wenige Daten.[/]")
    else:
        try:
            mean_ret  = ret_df.mean().values * 252        # annualisierte Renditen
            cov_mat   = ret_df.cov().values   * 252       # annualisierte Kovarianz
            n_assets  = len(valid_tickers)

            # Monte-Carlo: 8000 zufaellige Portfolios
            n_sim      = 8000
            best_sharpe, best_w, best_r, best_v = -999, None, 0, 0

            rng = np.random.default_rng(42)
            for _ in range(n_sim):
                w = rng.dirichlet(np.ones(n_assets))
                r = float(np.dot(w, mean_ret))
                v = float(np.sqrt(w @ cov_mat @ w))
                sh = r / v if v > 0 else -999
                if sh > best_sharpe:
                    best_sharpe, best_w, best_r, best_v = sh, w, r, v

            mw_table = Table(box=box.ROUNDED)
            mw_table.add_column("Ticker",            style="cyan")
            mw_table.add_column("Aktuell",           justify="right")
            mw_table.add_column("Markowitz Optimal", justify="right", style="bold")
            mw_table.add_column("Differenz",         justify="right")
            mw_table.add_column("Empfehlung",        justify="center")

            for i, ticker in enumerate(valid_tickers):
                aktuell = portfolio.get(ticker, 0)
                optimal = best_w[i] * 100
                diff    = optimal - aktuell
                if diff > 3:    empf = "[green]↑ erhöhen[/]"
                elif diff < -3: empf = "[red]↓ reduzieren[/]"
                else:           empf = "[dim]✓ ok[/]"
                d_col = "green" if diff > 0 else "red"
                mw_table.add_row(
                    ticker,
                    f"{aktuell:.1f}%",
                    f"{optimal:.1f}%",
                    f"[{d_col}]{diff:+.1f}%[/]",
                    empf,
                )
            console.print(mw_table)
            sh_col = "green" if best_sharpe > 0.5 else "yellow" if best_sharpe > 0 else "red"
            console.print(Panel(
                f"  Erwartete Rendite (ann.): [green]{best_r * 100:+.1f}%[/]\n"
                f"  Volatilität (ann.):       {best_v * 100:.1f}%\n"
                f"  Sharpe-Ratio:             [{sh_col}]{best_sharpe:.2f}[/]\n\n"
                f"  [dim]Berechnet aus 8.000 Monte-Carlo-Simulationen.[/]",
                title="📐 Markowitz – Max-Sharpe-Portfolio", border_style="green"
            ))

        except Exception as e:
            console.print(f"[red]Markowitz Fehler: {e}[/]")

    # ── 5. ICT Detail-Übersicht ───────────────────────────────────────────────
    console.print("\n[bold cyan]═══ 5. ICT Detail-Übersicht ═══[/]")
    ict_table = Table(box=box.ROUNDED)
    ict_table.add_column("Ticker",    style="cyan")
    ict_table.add_column("FVG",       justify="center")
    ict_table.add_column("OB",        justify="center")
    ict_table.add_column("Sweep",     justify="center")
    ict_table.add_column("BOS",       justify="center")
    ict_table.add_column("ICT Total", justify="right")

    for ticker in tickers:
        df = data_map.get(ticker, pd.DataFrame())
        if df.empty:
            ict_table.add_row(ticker, "-", "-", "-", "-", "-")
            continue
        kurs   = df["Close"].iloc[-1]
        fvgs   = detect_fvg(df)
        obs    = detect_order_blocks(df)
        sweeps = detect_liquidity_sweep(df)
        bos    = detect_bos(df)

        bull_fvg = any(g["typ"] == "bull" and g["unten"] <= kurs <= g["oben"] for g in fvgs)
        bear_fvg = any(g["typ"] == "bear" and g["unten"] <= kurs <= g["oben"] for g in fvgs)
        bull_ob  = any(o["typ"] == "bull" and o["unten"] <= kurs <= o["oben"] for o in obs)
        bear_ob  = any(o["typ"] == "bear" and o["unten"] <= kurs <= o["oben"] for o in obs)
        recent_s = [s for s in sweeps if s["idx"] >= len(df) - 3]
        bull_sw  = any(s["typ"] == "bull" for s in recent_s)
        bear_sw  = any(s["typ"] == "bear" for s in recent_s)

        fvg_str  = "[green]Bull[/]" if bull_fvg else "[red]Bear[/]" if bear_fvg else "[dim]–[/]"
        ob_str   = "[green]Bull[/]" if bull_ob  else "[red]Bear[/]" if bear_ob  else "[dim]–[/]"
        sw_str   = "[green]Bull[/]" if bull_sw  else "[red]Bear[/]" if bear_sw  else "[dim]–[/]"
        bos_str  = "[green]Bull[/]" if bos == "bull" else "[red]Bear[/]" if bos == "bear" else "[dim]–[/]"

        # Aus Cache lesen statt erneut score_ict() aufzurufen
        ict_sc, _ = ict_cache.get(ticker, (0, {}))
        ict_col   = "green" if ict_sc > 0 else "red" if ict_sc < 0 else "dim"
        ict_table.add_row(ticker, fvg_str, ob_str, sw_str, bos_str,
                          f"[bold {ict_col}]{ict_sc:+d}[/]")
    console.print(ict_table)

    # ── 6. Gesamtempfehlung ───────────────────────────────────────────────────
    console.print("\n[bold cyan]═══ 6. Gesamtempfehlung ═══[/]")
    empf_lines = []
    for ticker in tickers:
        sc  = scores_dict.get(ticker, 0)
        w   = portfolio[ticker]
        kelly_w = kelly_results.get(ticker)

        if sc <= SCORE_SHORT:
            if w > 5:
                empf_lines.append(f"  🔴 [bold red]{ticker}[/] Score={sc} → SHORT-Signal | Gewicht {w:.1f}% → [red]deutlich reduzieren[/]")
            else:
                empf_lines.append(f"  🔴 [red]{ticker}[/] Score={sc} → SHORT-Signal | Gewicht bereits niedrig, beobachten")
        elif sc >= SCORE_LONG:
            if kelly_w is not None and w < kelly_w - 5:
                empf_lines.append(
                    f"  🟢 [bold green]{ticker}[/] Score={sc} → LONG | Gewicht {w:.1f}% unter ½Kelly ({kelly_w:.1f}%) → [green]erhöhen[/]")
            else:
                empf_lines.append(f"  🟢 [green]{ticker}[/] Score={sc} → LONG | Position halten")
        else:
            empf_lines.append(f"  🟡 [yellow]{ticker}[/] Score={sc} → NEUTRAL | beobachten")

    console.print(Panel("\n".join(empf_lines) if empf_lines else "Keine Empfehlungen.",
                        title="💡 Handlungsempfehlungen", border_style="bright_blue"))

    # Risikohinweis
    console.print(Panel(
        "[dim]⚠ Alle Signale basieren auf historischen Kursdaten und technischer Analyse.\n"
        "Dies ist keine Anlageberatung. Eigene Recherche und Risikomanagement\n"
        "sind unerlässlich. Vergangene Performance ≠ zukünftige Rendite.[/]",
        style="dim", border_style="dim"
    ))

# ── CMD: lists ────────────────────────────────────────────────────────────────
def cmd_lists(args):
    table = Table(title="📋 Watchlists", box=box.ROUNDED)
    table.add_column("ID",       style="cyan", no_wrap=True)
    table.add_column("Name",     style="bold")
    table.add_column("Ticker",   justify="right", style="green")
    table.add_column("Beispiele",style="dim")
    for wl_id, wl in WATCHLISTS.items():
        count = len(wl["tickers"])
        examples = ", ".join(wl["tickers"][:4]) + ("..." if count > 4 else "")
        table.add_row(wl_id, wl["name"], str(count), examples)
    console.print(table)
    console.print(f"\n[dim]Gesamt: {len(WATCHLISTS)} Listen | Unique: {len(get_tickers(['all']))} Ticker[/]")
    console.print("\n[bold]Befehle:[/]")
    for ex in [
        "scanner scan dax40", "scanner scan all --workers 20",
        "scanner scan dax40 --only long --top 10",
        "scanner info NVDA", "scanner compare NVDA AMD INTC",
        "scanner backtest NVDA -p 2y -v",
        "scanner backtest dax40 -p 1y --depot 25000",
        "scanner alert dax40 --interval 300",
        "scanner cache-clear",
    ]:
        console.print(f"  {ex}")

# ── Scan-Helfer ───────────────────────────────────────────────────────────────
def _verarbeite_ticker(ticker, df, rates, spy_data):
    # FIX: precompute_serien statt berechne_score — alle Indikatoren nur einmal berechnen
    serien  = precompute_serien(df, spy_returns=spy_data)
    score   = score_bei_index(len(df) - 1, df, serien)
    details = {}

    # details aus precomputed Serien fuer Score-Trend und RSI befuellen
    try:
        i = len(df) - 1
        close = df["Close"]
        kurs  = close.iloc[-1]

        rsi_v = serien["rsi"].iloc[-1] if "rsi" in serien else float("nan")
        if not pd.isna(rsi_v):
            details["rsi_raw"] = rsi_v

        # Score-Trend: Vergleich mit Score vor 5 Tagen
        # _compute_trend=False: verhindert weitere Rekursion, wir brauchen nur den Rohscore
        if i >= 55:
            try:
                score_alt, _ = berechne_score(df.iloc[:-5], spy_returns=spy_data, _compute_trend=False)
                diff = score - score_alt
                details["trend"] = f"+{diff}" if diff > 0 else (str(diff) if diff < 0 else "=")
            except Exception:
                pass
    except Exception as e:
        logging.debug(f"{ticker} details: {e}")

    rsi_val = kurs_val = "-"
    atr_val = None
    datendatum = "-"
    split_flag = False
    split_info = "OK"
    try:
        split_flag = df["Close"].tail(6).pct_change().iloc[1:].abs().max() > 0.50
        split_info = "⚠ SPLIT?" if split_flag else "OK"
        if len(df) < 200: split_info = f"WENIG DATEN ({len(df)}d)"

        rsi_raw  = details.get("rsi_raw")
        rsi_val  = f"{rsi_raw:.0f}" if rsi_raw is not None else "-"
        kurs_raw = df["Close"].iloc[-1]
        kurs_eur = to_eur(kurs_raw, ticker, rates, last_price=kurs_raw)
        kurs_val = f"{kurs_eur:.2f}EUR"
        atr_raw  = serien["atr"].iloc[-1] if "atr" in serien and not pd.isna(serien["atr"].iloc[-1]) else None
        atr_val  = to_eur(atr_raw, ticker, rates, last_price=kurs_raw) if atr_raw else None
        datendatum = df.index[-1].strftime("%d.%m.%Y")
    except Exception as e:
        logging.warning(f"{ticker}: {e}")

    return {
        "ticker": ticker + (" ⚠" if split_flag else ""),
        "kurs": kurs_val, "score": score, "signal": "NEUTRAL",
        "rsi": rsi_val, "atr": atr_val, "info": split_info,
        "details": details, "datendatum": datendatum,
    }

def weise_signale(ergebnisse, min_score, min_score_short):
    scores = [e["score"] for e in ergebnisse]
    long_count = short_count = neutral_count = 0
    long_threshold = short_threshold = 0

    if scores:
        if min_score is not None:
            long_threshold  = min_score
            short_threshold = min_score_short if min_score_short is not None else min_score - 7
            for e in ergebnisse:
                if e["score"] >= long_threshold:
                    e["signal"] = "🟢 LONG";    long_count += 1
                elif e["score"] <= short_threshold:
                    e["signal"] = "🔴 SHORT";   short_count += 1
                else:
                    e["signal"] = "🟡 NEUTRAL"; neutral_count += 1
        else:
            ss = sorted(scores); n = len(ss)
            long_threshold  = ss[int(n * 0.8)]
            short_threshold = ss[int(n * 0.2)]
            if long_threshold == short_threshold:
                for e in ergebnisse:
                    e["signal"] = "🟡 NEUTRAL"; neutral_count += 1
            else:
                for e in ergebnisse:
                    if e["score"] >= long_threshold:
                        e["signal"] = "🟢 LONG";    long_count += 1
                    elif e["score"] <= short_threshold:
                        e["signal"] = "🔴 SHORT";   short_count += 1
                    else:
                        e["signal"] = "🟡 NEUTRAL"; neutral_count += 1

    return ergebnisse, long_count, short_count, neutral_count, long_threshold, short_threshold

def drucke_scan_tabelle(ergebnisse_show, version_str="v6.2", sort_key="score"):
    """sort_key: 'score' | 'rsi' | 'atr' | 'ticker'"""
    table = Table(
        title=f"📊 STOCK SCANNER {version_str} – {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        box=box.ROUNDED
    )
    table.add_column("Ticker", style="cyan", no_wrap=True)
    table.add_column("Kurs",   justify="right", style="green")
    table.add_column("Score",  justify="right")
    table.add_column("Trend",  justify="center")
    table.add_column("Signal", style="bold magenta")
    table.add_column("RSI",    justify="right")
    table.add_column("ATR",    justify="right", style="dim")

    def _sort_fn(e):
        if sort_key == "rsi":
            try:    return float(e["rsi"])
            except: return 999
        if sort_key == "atr":
            return e["atr"] if e["atr"] else 0
        if sort_key == "ticker":
            return e["ticker"]
        return e["score"]  # default

    reverse = sort_key != "ticker"
    for e in sorted(ergebnisse_show, key=_sort_fn, reverse=reverse):
        atr_str  = f"{e['atr']:.2f}" if e["atr"] else "-"
        sc       = e["score"]
        sc_col   = "green" if sc >= 5 else "red" if sc <= 0 else "yellow"
        sc_str   = f"[{sc_col}]{sc}[/]"
        trend    = e["details"].get("trend", "")
        if trend.startswith("+"): trend_str = f"[green]{trend}[/]"
        elif trend.startswith("-"): trend_str = f"[red]{trend}[/]"
        else: trend_str = "[dim]=[/]"
        try:
            rn = float(e["rsi"])
            rc = "green" if rn < 35 else "red" if rn > 70 else "white"
            rsi_str = f"[{rc}]{e['rsi']}[/]"
        except (ValueError, TypeError):
            rsi_str = e["rsi"]
        table.add_row(e["ticker"][:12], e["kurs"], sc_str, trend_str,
                      e["signal"], rsi_str, atr_str)
    return table

# ── CMD: scan ────────────────────────────────────────────────────────────────
def cmd_scan(args):
    list_names = args.list.split(",") if args.list else ["us_large_cap"]
    tickers    = get_tickers(list_names)
    min_score  = args.min_score
    min_score_short = args.min_score_short if hasattr(args, "min_score_short") else None
    workers    = args.workers if hasattr(args, "workers") and args.workers else _AUTO_WORKERS
    period     = args.period or "1y"
    top_n      = args.top if hasattr(args, "top") and args.top else None
    sort_key   = args.sort if hasattr(args, "sort") and args.sort else "score"

    names = []
    for n in list_names:
        if n == "all":         names.append("🌍 Alle Listen")
        elif n in WATCHLISTS:  names.append(WATCHLISTS[n]["name"])
        else:                  names.append(n)

    mode_str = f"absolut (--min-score {min_score})" if min_score is not None else "dynamisch (Top/Bot 20%)"
    console.print(Panel(
        f"[bold]Liste:[/]   {' + '.join(names)}\n"
        f"[bold]Tickers:[/] {len(tickers)}  |  [bold]Workers:[/] {workers}  |  [bold]Periode:[/] {period}\n"
        f"[bold]Modus:[/]   {mode_str}",
        title="🔍 Scan gestartet", border_style="blue"
    ))

    rates    = get_eur_rates()
    spy_data = get_spy_returns()

    console.print("[dim]Bulk-Download laeuft...[/]", end="\r")
    data_map = bulk_download(tickers, period=period)
    console.print(f"[green]✓ {len(data_map)}/{len(tickers)} Ticker geladen[/]")

    ergebnisse = []
    with Progress(SpinnerColumn(), TextColumn("[cyan]{task.description}"),
                  BarColumn(bar_width=20),
                  "[progress.percentage]{task.percentage:>5.1f}%",
                  "[dim]{task.completed}/{task.total}[/]",
                  transient=True, console=console) as progress:
        task = progress.add_task("Analysiere...", total=len(tickers))

        def _analyse(ticker):
            df = data_map.get(ticker, pd.DataFrame())
            if df.empty:
                return {"ticker": ticker, "kurs": "-", "score": 0, "signal": "NEUTRAL",
                        "rsi": "-", "atr": None, "info": "Keine Daten",
                        "details": {}, "datendatum": "-"}
            return _verarbeite_ticker(ticker, df, rates, spy_data)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_analyse, t): t for t in tickers}
            for future in as_completed(futures):
                try:
                    ergebnisse.append(future.result())
                except Exception as e:
                    t = futures[future]
                    ergebnisse.append({"ticker": t, "kurs": "-", "score": 0,
                                       "signal": "NEUTRAL", "rsi": "-", "atr": None,
                                       "info": f"ERROR: {str(e)[:30]}", "details": {}, "datendatum": "-"})
                progress.advance(task)

    ergebnisse, lc, sc_, nc, lt, st = weise_signale(ergebnisse, min_score, min_score_short)

    if hasattr(args, "only") and args.only:
        filter_map = {"long": "🟢 LONG", "short": "🔴 SHORT", "neutral": "🟡 NEUTRAL"}
        ergebnisse_show = [e for e in ergebnisse if e["signal"] == filter_map.get(args.only)]
    else:
        ergebnisse_show = ergebnisse

    if top_n:
        ergebnisse_show = sorted(ergebnisse_show, key=lambda x: x["score"], reverse=True)[:top_n]

    table   = drucke_scan_tabelle(ergebnisse_show, sort_key=sort_key)
    schwelle_str = (f"absolut | LONG>={lt} | SHORT<={st}" if min_score is not None
                    else f"dynamisch | LONG>={lt} | SHORT<={st}")
    sort_str = f"  |  Sortierung: {sort_key}" if sort_key != "score" else ""
    summary = Panel(
        f"🟢 LONG: {lc} | 🔴 SHORT: {sc_} | 🟡 NEUTRAL: {nc}\n{schwelle_str}{sort_str}",
        title="Summary", border_style="bright_blue"
    )
    console.print(Columns([table, summary]))

    # Sektor-Heatmap
    _drucke_heatmap(ergebnisse)

    if hasattr(args, "export") and args.export:
        export_csv(ergebnisse_show, args.list.replace(",", "_"))

    warns = list(set(e["info"] for e in ergebnisse
                     if any(x in e["info"] for x in ["DELIST","SPLIT","WENIG"])))
    if warns:
        console.print(Panel("\n".join(sorted(warns)), title="⚠ Warnungen", style="yellow"))

# ── Sektor-Heatmap ────────────────────────────────────────────────────────────
def _drucke_heatmap(ergebnisse):
    ticker_score = {e["ticker"].replace(" ⚠","").strip(): e["score"] for e in ergebnisse}
    heatmap = Table(title="🌡 Sektor-Heatmap (Avg Score)", box=box.SIMPLE)
    heatmap.add_column("Sektor",    style="bold")
    heatmap.add_column("Avg Score", justify="right")
    heatmap.add_column("Balken",    justify="left")

    sektoren = []
    for wl_id, wl in WATCHLISTS.items():
        scores = [ticker_score[t] for t in wl["tickers"] if t in ticker_score]
        if not scores: continue
        avg = sum(scores) / len(scores)
        sektoren.append((wl["name"], avg, len(scores)))

    for name, avg, cnt in sorted(sektoren, key=lambda x: x[1], reverse=True):
        col   = "green" if avg >= 3 else "red" if avg <= 0 else "yellow"
        bars  = int(abs(avg) * 2)
        balken = f"[{col}]{'█' * min(bars, 20)}[/]"
        heatmap.add_row(name, f"[{col}]{avg:+.1f}[/]", balken)

    console.print(Panel(heatmap, title="Sektor-Heatmap", border_style="dim"))

# ── CMD: compare ──────────────────────────────────────────────────────────────
def cmd_compare(args):
    tickers = [t.upper() for t in args.targets] if args.targets else []
    if len(tickers) < 2:
        console.print("[red]✗ Mindestens 2 Ticker: scanner compare NVDA AMD INTC[/]")
        return

    period   = args.period or "1y"
    rates    = get_eur_rates()
    spy_data = get_spy_returns()

    console.print(f"[cyan]Vergleiche: {' vs '.join(tickers)} ({period})...[/]")
    data_map = bulk_download(tickers, period=period)

    table = Table(title=f"📊 Vergleich: {' vs '.join(tickers)}", box=box.ROUNDED)
    table.add_column("Metrik",    style="cyan", no_wrap=True)
    for t in tickers:
        table.add_column(t, justify="right")

    # Daten pro Ticker sammeln
    rows = {
        "Kurs (EUR)":    [],
        "Score":         [],
        "Score-Trend":   [],
        "RSI(14)":       [],
        "MACD":          [],
        "SMA200":        [],
        "52W-Hoch %":    [],
        "52W-Tief %":    [],
        "ATR (EUR)":     [],
        "Signal":        [],
        "Letzter Tag":   [],
    }

    for ticker in tickers:
        df = data_map.get(ticker, pd.DataFrame())
        if df.empty:
            for k in rows: rows[k].append("[dim]n/a[/]")
            continue

        score, details = berechne_score(df, spy_returns=spy_data)
        kurs_raw = df["Close"].iloc[-1]
        kurs_eur = to_eur(kurs_raw, ticker, rates, last_price=kurs_raw)

        rsi_val  = details.get("rsi_raw", float("nan"))
        rsi_str  = f"{rsi_val:.1f}" if not pd.isna(rsi_val) else "-"
        rsi_col  = "green" if not pd.isna(rsi_val) and rsi_val < 35 else \
                   "red"   if not pd.isna(rsi_val) and rsi_val > 70 else "white"

        close    = df["Close"]
        macd_h   = ta.trend.MACD(close).macd_diff().iloc[-1]
        sma200_v = ta.trend.SMAIndicator(close, window=200).sma_indicator().iloc[-1]
        atr      = berechne_atr(df)
        atr_eur  = to_eur(atr, ticker, rates, last_price=kurs_raw) if atr else None

        h52 = close.rolling(252, min_periods=50).max().iloc[-1]
        l52 = close.rolling(252, min_periods=50).min().iloc[-1]
        h52_pct = (kurs_raw / h52 - 1) * 100 if not pd.isna(h52) and h52 > 0 else float("nan")
        l52_pct = (kurs_raw / l52 - 1) * 100 if not pd.isna(l52) and l52 > 0 else float("nan")

        sc_col  = "green" if score >= 5 else "red" if score <= 0 else "yellow"
        trend   = details.get("trend", "=")
        tr_col  = "green" if trend.startswith("+") else "red" if trend.startswith("-") else "dim"

        sig     = "🟢 LONG" if score >= SCORE_LONG else "🔴 SHORT" if score <= SCORE_SHORT else "🟡 NEUTRAL"
        sm200_c = "green" if not pd.isna(sma200_v) and kurs_raw > sma200_v else "red"

        rows["Kurs (EUR)"].append(f"{kurs_eur:.2f}")
        rows["Score"].append(f"[{sc_col}]{score}[/]")
        rows["Score-Trend"].append(f"[{tr_col}]{trend}[/]")
        rows["RSI(14)"].append(f"[{rsi_col}]{rsi_str}[/]")
        rows["MACD"].append(f"[green]+[/]" if not pd.isna(macd_h) and macd_h > 0 else "[red]-[/]")
        rows["SMA200"].append(f"[{sm200_c}]{'✓' if not pd.isna(sma200_v) and kurs_raw > sma200_v else '✗'}[/]")
        rows["52W-Hoch %"].append(f"[dim]{h52_pct:+.1f}%[/]" if not pd.isna(h52_pct) else "-")
        rows["52W-Tief %"].append(f"{l52_pct:+.1f}%" if not pd.isna(l52_pct) else "-")
        rows["ATR (EUR)"].append(f"{atr_eur:.2f}" if atr_eur else "-")
        rows["Signal"].append(sig)
        rows["Letzter Tag"].append(df.index[-1].strftime("%d.%m.%Y"))

    for metrik, werte in rows.items():
        table.add_row(metrik, *werte)

    console.print(table)

# ── CMD: alert ────────────────────────────────────────────────────────────────
def cmd_alert(args):
    global _spy_cache
    list_names = args.list.split(",") if args.list else ["us_large_cap"]
    tickers    = get_tickers(list_names)
    period     = args.period or "1y"
    interval   = args.interval if hasattr(args, "interval") and args.interval else 300
    min_score  = args.min_score
    min_score_short = args.min_score_short if hasattr(args, "min_score_short") else None

    console.print(Panel(
        f"[bold]Tickers:[/] {len(tickers)}  |  [bold]Intervall:[/] {interval}s\n"
        f"Meldet nur bei Signal-Aenderungen. [dim]Ctrl+C zum Beenden.[/]",
        title="🔔 Alert-Modus gestartet", border_style="yellow"
    ))

    rates      = get_eur_rates()
    spy_data   = get_spy_returns()
    letzter_stand    = {}
    letzter_refresh  = datetime.now(timezone.utc)

    try:
        while True:
            # Rates + SPY jede Stunde neu laden
            jetzt = datetime.now(timezone.utc)
            if (jetzt - letzter_refresh).total_seconds() >= 3600:
                console.print("[dim]Aktualisiere EUR-Kurse + SPY...[/]")
                rates      = get_eur_rates()
                _spy_cache = None  # Cache invalidieren → get_spy_returns() lädt neu
                spy_data   = get_spy_returns()
                letzter_refresh = jetzt

            console.print(f"\n[dim]{datetime.now().strftime('%H:%M:%S')} – Pruefe {len(tickers)} Ticker...[/]")
            data_map   = bulk_download(tickers, period=period)
            ergebnisse = []
            for ticker, df in data_map.items():
                ergebnisse.append(_verarbeite_ticker(ticker, df, rates, spy_data))

            ergebnisse, *_ = weise_signale(ergebnisse, min_score, min_score_short)

            aenderungen = []
            for e in ergebnisse:
                t   = e["ticker"].replace(" ⚠","").strip()
                sig = e["signal"]
                alt = letzter_stand.get(t)
                if alt is not None and alt != sig:
                    aenderungen.append((t, alt, sig, e["score"], e["kurs"]))
                letzter_stand[t] = sig

            if aenderungen:
                a_table = Table(title="🚨 Signal-Aenderungen!", box=box.ROUNDED)
                a_table.add_column("Ticker", style="cyan")
                a_table.add_column("Vorher")
                a_table.add_column("Jetzt",  style="bold")
                a_table.add_column("Score",  justify="right")
                a_table.add_column("Kurs",   justify="right")
                for t, alt, neu, sc, kurs in aenderungen:
                    sc_col = "green" if sc >= 5 else "red" if sc <= 0 else "yellow"
                    a_table.add_row(t, alt, neu, f"[{sc_col}]{sc}[/]", kurs)
                    log_signal(t, alt, neu, sc, kurs)
                console.print(a_table)
                log_fn = os.path.join(LOG_DIR, f"signals_{datetime.now().strftime('%Y%m%d')}.csv")
                console.print(f"[dim]Signal-Log: {log_fn}[/]")
            else:
                console.print("[dim]Keine Aenderungen.[/]")

            if letzter_stand:
                console.print(f"[dim]Naechste Pruefung in {interval}s...[/]")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Alert-Modus beendet.[/]")

# ── CMD: watch (Live-Kurs) ───────────────────────────────────────────────────
def cmd_watch(args):
    tickers  = [t.upper() for t in args.targets] if args.targets else []
    if not tickers:
        console.print("[red]✗ Ticker angeben: scanner watch NVDA AAPL[/]")
        return

    interval = args.interval if hasattr(args, "interval") and args.interval else 10
    rates    = get_eur_rates()
    spy_data = get_spy_returns()
    period   = args.period or "1y"

    console.print(Panel(
        f"[bold]Ticker:[/] {' | '.join(tickers)}\n"
        f"[bold]Refresh:[/] alle {interval}s  |  [dim]Ctrl+C zum Beenden[/]",
        title="👁 Live Watch", border_style="cyan"
    ))

    # Historische Daten einmal laden fuer Score-Berechnung
    console.print("[dim]Lade historische Daten...[/]")
    hist_map = bulk_download(tickers, period=period)
    prev_prices = {}

    try:
        while True:
            table = Table(
                title=f"👁 LIVE WATCH – {datetime.now().strftime('%H:%M:%S')}",
                box=box.ROUNDED
            )
            table.add_column("Ticker",   style="cyan bold", no_wrap=True)
            table.add_column("Kurs EUR", justify="right")
            table.add_column("Aend.",    justify="right")
            table.add_column("Score",    justify="right")
            table.add_column("Trend",    justify="center")
            table.add_column("RSI",      justify="right")
            table.add_column("Signal",   style="bold magenta")
            table.add_column("Vol",      justify="right", style="dim")

            for ticker in tickers:
                try:
                    # Aktuellen Kurs live holen (kein Cache)
                    live  = yf.Ticker(ticker)
                    info  = live.fast_info
                    kurs_raw = getattr(info, "last_price", None)
                    if kurs_raw is None or kurs_raw == 0:
                        table.add_row(ticker, "[dim]n/a[/]", "-", "-", "-", "-", "-", "-")
                        continue

                    kurs_eur = to_eur(kurs_raw, ticker, rates, last_price=kurs_raw)

                    # Aenderung zu letztem Refresh
                    prev     = prev_prices.get(ticker, kurs_eur)
                    delta    = kurs_eur - prev
                    delta_pct= (delta / prev * 100) if prev > 0 else 0
                    aend_col = "green" if delta >= 0 else "red"
                    aend_str = f"[{aend_col}]{delta_pct:+.2f}%[/]"
                    prev_prices[ticker] = kurs_eur

                    # Score aus historischen Daten + aktuellem Kurs
                    df_hist = hist_map.get(ticker, pd.DataFrame())
                    score, details = berechne_score(df_hist, spy_returns=spy_data)

                    rsi_val  = details.get("rsi_raw")
                    rsi_str  = f"{rsi_val:.0f}" if rsi_val is not None else "-"
                    rsi_col  = "green" if rsi_val is not None and rsi_val < 35 else "red" if rsi_val is not None and rsi_val > 70 else "white"

                    sc_col   = "green" if score >= 5 else "red" if score <= 0 else "yellow"
                    trend    = details.get("trend", "=")
                    tr_col   = "green" if trend.startswith("+") else "red" if trend.startswith("-") else "dim"

                    sig      = ("🟢 LONG" if score >= SCORE_LONG
                                else "🔴 SHORT" if score <= SCORE_SHORT else "🟡 NEUTRAL")

                    # Volumen
                    vol_str = "-"
                    try:
                        vol = getattr(info, "three_month_average_volume", None)
                        if vol:
                            vol_str = f"{vol/1e6:.1f}M"
                    except Exception:
                        pass

                    table.add_row(
                        ticker[:10],
                        f"{kurs_eur:.2f}",
                        aend_str,
                        f"[{sc_col}]{score}[/]",
                        f"[{tr_col}]{trend}[/]",
                        f"[{rsi_col}]{rsi_str}[/]",
                        sig,
                        vol_str,
                    )
                except Exception as e:
                    table.add_row(ticker, "[red]ERR[/]", "-", "-", "-", "-", "-", "-")
                    logging.debug(f"watch {ticker}: {e}")

            # Terminal leeren und Tabelle neu zeichnen
            console.clear()
            console.print(table)
            console.print(f"[dim]Refresh in {interval}s – Ctrl+C zum Beenden[/]")
            time.sleep(interval)

    except KeyboardInterrupt:
        console.print("\n[yellow]Watch beendet.[/]")

# ── CMD: info ─────────────────────────────────────────────────────────────────
def cmd_info(args):
    if not args.ticker:
        console.print("[red]✗ Ticker: scanner info NVDA[/]")
        return

    ticker = args.ticker.upper()
    period = args.period if hasattr(args, "period") and args.period else "1y"
    console.print(f"[cyan]Lade {ticker} ({period})...[/]")

    df, split_flag, split_info = lade_daten_single(ticker, period=period)
    if df.empty:
        console.print(Panel(f"[bold red]{ticker}:[/] {split_info}", style="red"))
        return

    spy_data = get_spy_returns()
    score, details = berechne_score(df, spy_returns=spy_data)
    rates    = get_eur_rates()
    kurs     = df["Close"].iloc[-1]
    kurs_eur = to_eur(kurs, ticker, rates, last_price=kurs)

    datendatum  = df.index[-1].strftime("%d.%m.%Y")
    anzahl_tage = len(df)
    daten_warn  = anzahl_tage < 200

    close  = df["Close"]
    sma20  = ta.trend.SMAIndicator(close, window=20).sma_indicator().iloc[-1]
    sma50  = ta.trend.SMAIndicator(close, window=50).sma_indicator().iloc[-1]
    sma200 = ta.trend.SMAIndicator(close, window=200).sma_indicator().iloc[-1]
    rsi    = details.get("rsi_raw") or ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
    macd_h = ta.trend.MACD(close).macd_diff().iloc[-1]
    bb     = ta.volatility.BollingerBands(close)
    bb_up  = bb.bollinger_hband().iloc[-1]
    bb_lo  = bb.bollinger_lband().iloc[-1]
    atr    = berechne_atr(df)

    h52         = close.rolling(252, min_periods=50).max().iloc[-1]
    l52         = close.rolling(252, min_periods=50).min().iloc[-1]
    h52_eur     = to_eur(h52, ticker, rates, last_price=kurs)
    l52_eur     = to_eur(l52, ticker, rates, last_price=kurs)

    weekly_close = close.resample("W").last()
    try:    w_rsi = ta.momentum.RSIIndicator(weekly_close, window=14).rsi().iloc[-1]
    except: w_rsi = None

    trend_str = "↑ [bold green]AUFWAERTSTREND[/]" if kurs > sma200 else "↓ [bold red]ABWAERTSTREND[/]"
    cross_str = "[green]✓ Golden Cross[/]" if sma50 > sma200 else "[red]✗ Death Cross[/]"
    vs_sma200 = (kurs / sma200 - 1) * 100

    try:
        spy_ret   = spy_data.pct_change(20).iloc[-1]
        stock_ret = close.pct_change(20).iloc[-1]
        rs        = stock_ret / spy_ret if spy_ret != 0 else 0
        diff_pct  = (stock_ret - spy_ret) * 100
        # Label: absoluten Renditeunterschied verwenden, nicht rs-Ratio
        # (rs-Ratio versagt wenn beide Werte negativ sind)
        if diff_pct > 5:    rs_label = "stark outperform"
        elif diff_pct > 1:  rs_label = "outperform"
        elif diff_pct < -5: rs_label = "stark underperform"
        elif diff_pct < -1: rs_label = "underperform"
        else:               rs_label = "neutral"
        rs_str = f"{diff_pct:+.1f}% {rs_label} vs SPY"
    except: rs_str = "n/a"

    if w_rsi and not pd.isna(w_rsi):
        wt_str = (f"[green]✓ Bullisch[/] (W-RSI={w_rsi:.0f})" if w_rsi > 50
                  else f"[red]✗ Baerisch[/] (W-RSI={w_rsi:.0f})")
        if w_rsi > 80: wt_str += " [yellow]⚠ Ueberhitzt[/]"
    else: wt_str = "n/a"

    trend_val = details.get("trend", "=")
    if score >= SCORE_LONG:   sig_str = f"[green]🟢 LONG[/] (Score: {score}, Trend: {trend_val})"
    elif score <= SCORE_SHORT: sig_str = f"[red]🔴 SHORT[/] (Score: {score}, Trend: {trend_val})"
    else:                      sig_str = f"[yellow]🟡 NEUTRAL[/] (Score: {score}, Trend: {trend_val})"

    ov = Text()
    ov.append("Kurs:         "); ov.append(f"{kurs_eur:.2f}EUR\n", style="bold green")
    ov.append(f"Signal:       {sig_str}\n")
    ov.append(f"Trend Daily:  {trend_str}\n")
    ov.append("vs SMA200:    "); ov.append(f"{vs_sma200:+.1f}% (SMA200={to_eur(sma200,ticker,rates,last_price=kurs):.2f}EUR)\n", style="dim")
    ov.append(f"SMA50/200:    {cross_str} (SMA50={to_eur(sma50,ticker,rates,last_price=kurs):.2f}EUR)\n")
    ov.append(f"Weekly-Trend: {wt_str}\n")
    ov.append("RS vs SPY:    "); ov.append(f"{rs_str}\n", style="dim")
    ov.append("52W Hoch:     "); ov.append(f"{h52_eur:.2f}EUR\n", style="dim")
    ov.append("52W Tief:     "); ov.append(f"{l52_eur:.2f}EUR\n", style="dim")
    ov.append("Letzter Tag:  ")
    ov.append(f"{datendatum} ({anzahl_tage} Handelstage)\n",
              style="yellow" if daten_warn else "dim")
    if split_flag: ov.append("⚠ SPLIT erkannt!\n", style="bold yellow")
    if daten_warn: ov.append(f"⚠ Nur {anzahl_tage} Tage Daten\n", style="bold yellow")
    console.print(Panel(ov, title=f"📋 DETAIL: {ticker}", border_style="green"))

    ind_table = Table(box=box.SIMPLE)
    ind_table.add_column("Indikator", style="cyan")
    ind_table.add_column("Wert",      justify="right")
    ind_table.add_column("Status")
    ind_table.add_row("RSI(14)",   f"{rsi:.2f}",
                      "[green]Ueberverkauft[/]" if rsi < 35 else "[red]Ueberkauft[/]" if rsi > 70 else "-")
    ind_table.add_row("MACD-Hist", f"{macd_h:.4f}",
                      "[green]Bullisch[/]" if macd_h > 0 else "[red]Baerisch[/]")
    ind_table.add_row("SMA20",  f"{to_eur(sma20, ticker,rates,last_price=kurs):.2f}EUR",
                      "[green]✓[/]" if kurs > sma20  else "[red]✗[/]")
    ind_table.add_row("SMA50",  f"{to_eur(sma50, ticker,rates,last_price=kurs):.2f}EUR",
                      "[green]✓[/]" if kurs > sma50  else "[red]✗[/]")
    ind_table.add_row("SMA200", f"{to_eur(sma200,ticker,rates,last_price=kurs):.2f}EUR",
                      "[green]✓[/]" if kurs > sma200 else "[red]✗[/]")
    ind_table.add_row("BB-Upper", f"{to_eur(bb_up,ticker,rates,last_price=kurs):.2f}EUR", "-")
    ind_table.add_row("BB-Lower", f"{to_eur(bb_lo,ticker,rates,last_price=kurs):.2f}EUR", "-")
    ind_table.add_row("ATR(14)", f"{to_eur(atr,ticker,rates,last_price=kurs):.2f}EUR" if atr else "-", "-")
    if w_rsi and not pd.isna(w_rsi):
        ws = "[green]Bullisch[/]" if w_rsi > 50 else "[red]Baerisch[/]"
        if w_rsi > 80: ws += " [yellow]⚠[/]"
        ind_table.add_row("Weekly RSI", f"{w_rsi:.1f}", ws)
    console.print(Panel(ind_table, title="Indikatoren", border_style="blue"))

    sc_table = Table(box=box.SIMPLE)
    sc_table.add_column("Status", justify="center")
    sc_table.add_column("Indikator")
    sc_table.add_column("Punkte", justify="right")
    score_map = {
        "SMA200":   ("Kurs > SMA200",               details.get("SMA200",  "+0")),
        "Cross":    ("Golden Cross SMA50>SMA200",    details.get("Cross",   "+0")),
        "RSI":      (f"RSI={rsi:.1f}",              details.get("RSI",     "0")),
        "MACD":     (f"MACD {'bull.' if macd_h>0 else 'baer.'}", details.get("MACD","0")),
        "SMA20":    ("Kurs > SMA20",                details.get("SMA20",   "+0")),
        "SMA10":    ("Kurs < SMA10 (baerisch)",     details.get("SMA10",   "+0")),
        "BB":       ("Kurs unter BB-Lower",         details.get("BB",      "+0")),
        "Vol":      ("Volume Spike >1.5x",          details.get("Vol",     "+0")),
        "Weekly":   ("Weekly-Trend bullisch",       details.get("Weekly",  "+0")),
        "W-RSI>80": ("Weekly-RSI > 80",             details.get("W-RSI>80","+0")),
        "RS":       (f"RS: {rs_str}",               details.get("RS",      "0")),
        "52W-H":    ("52W Hoch Naehe (<10%)",       details.get("52W-H",   "+0")),
        "52W-L":    ("52W Tief Naehe (<10%)",       details.get("52W-L",   "+0")),
        "Gap+":     ("Gap >3% bullisch",            details.get("Gap+",    "+0")),
        "Gap-":     ("Gap >3% baerisch",            details.get("Gap-",    "+0")),
    }
    for key, (label, pts) in score_map.items():
        if key in details:
            icon = ("[green]✓[/]" if pts.startswith("+") and pts != "+0"
                    else "[red]✗[/]" if pts.startswith("-") else "[yellow]⚠[/]")
            sc_table.add_row(icon, label, pts)
    console.print(Panel(sc_table, title=f"Score-Details: {score} Punkte", border_style="magenta"))

    if atr:
        atr_eur      = to_eur(atr, ticker, rates, last_price=kurs)
        sl_long      = kurs_eur - 2 * atr_eur
        sl_short     = kurs_eur + 2 * atr_eur
        sl_long_fix  = kurs_eur * 0.95
        sl_short_fix = kurs_eur * 1.05
        tp1 = kurs_eur * 1.08; tp2 = kurs_eur * 1.15; tp3 = kurs_eur + 3 * atr_eur
        ds1 = kurs_eur * 0.92; ds2 = kurs_eur * 0.85; ds3 = kurs_eur - 3 * atr_eur

        # Signal-Richtung bestimmen – nur relevante Seite anzeigen
        is_long    = score >= SCORE_LONG
        is_short   = score <= SCORE_SHORT
        is_neutral = not is_long and not is_short

        risk_table = Table(box=box.SIMPLE)
        risk_table.add_column("Level", style="bold")
        if is_long or is_neutral:
            risk_table.add_column("LONG (Fix)",  justify="right")
            risk_table.add_column("LONG (ATR)",  justify="right", style="green")
        if is_short or is_neutral:
            risk_table.add_column("SHORT (Fix)", justify="right")
            risk_table.add_column("SHORT (ATR)", justify="right", style="red")

        def _row(*cols):
            """Nur die Spalten hinzufügen die auch angezeigt werden."""
            row = []
            if is_long or is_neutral:   row += list(cols[0:2])
            if is_short or is_neutral:  row += list(cols[2:4])
            risk_table.add_row(*row)

        risk_table.add_row(
            "[dim]Stop-Loss[/]",
            *([f"{sl_long_fix:.2f}EUR",  f"{sl_long:.2f}EUR"]  if is_long  or is_neutral else []),
            *([f"{sl_short_fix:.2f}EUR", f"{sl_short:.2f}EUR"] if is_short or is_neutral else []),
        )
        risk_table.add_row(
            "[green]TP1[/]",
            *([f"{tp1:.2f}EUR", "-"] if is_long  or is_neutral else []),
            *([f"{ds1:.2f}EUR", "-"] if is_short or is_neutral else []),
        )
        risk_table.add_row(
            "[green]TP2[/]",
            *([f"{tp2:.2f}EUR", "-"] if is_long  or is_neutral else []),
            *([f"{ds2:.2f}EUR", "-"] if is_short or is_neutral else []),
        )
        risk_table.add_row(
            "[green]TP3[/]",
            *(["-", f"{tp3:.2f}EUR"] if is_long  or is_neutral else []),
            *(["-", f"{ds3:.2f}EUR"] if is_short or is_neutral else []),
        )

        # Farbe der Risiko-Tabelle je nach Signal
        risk_border = "green" if is_long else "red" if is_short else "yellow"
        console.print(Panel(risk_table, title="Risiko-Management", border_style=risk_border))

        depot         = args.depot if hasattr(args, "depot") and args.depot is not None else 10000
        risiko        = depot * 0.02
        sl_dist_long  = kurs_eur - sl_long
        sl_dist_short = sl_short - kurs_eur
        stueck_long   = int(risiko / sl_dist_long)  if sl_dist_long  > 0 else 0
        stueck_short  = int(risiko / sl_dist_short) if sl_dist_short > 0 else 0

        # Nur die relevante Richtung anzeigen
        if is_long:
            sizing_text = (
                f"Max-Risiko: [bold]{risiko:.0f}EUR (2% des Depots)[/]\n\n"
                f"[green]→ LONG kaufen:[/]\n"
                f"   {stueck_long} Aktien × {kurs_eur:.2f}EUR = [bold green]{stueck_long * kurs_eur:.0f}EUR[/]\n"
                f"   Stop-Loss bei [red]{sl_long:.2f}EUR[/]  "
                f"(Abstand: {sl_dist_long:.2f}EUR | Verlust max: [red]{stueck_long * sl_dist_long:.0f}EUR[/])"
            )
        elif is_short:
            sizing_text = (
                f"Max-Risiko: [bold]{risiko:.0f}EUR (2% des Depots)[/]\n\n"
                f"[red]→ SHORT verkaufen:[/]\n"
                f"   {stueck_short} Aktien × {kurs_eur:.2f}EUR = [bold red]{stueck_short * kurs_eur:.0f}EUR[/]\n"
                f"   Stop-Loss bei [red]{sl_short:.2f}EUR[/]  "
                f"(Abstand: {sl_dist_short:.2f}EUR | Verlust max: [red]{stueck_short * sl_dist_short:.0f}EUR[/])"
            )
        else:
            # NEUTRAL: beide Seiten zeigen aber klar als Szenarien kennzeichnen
            sizing_text = (
                f"Max-Risiko: [bold]{risiko:.0f}EUR (2% des Depots)[/]  "
                f"[yellow]Signal: NEUTRAL – kein klarer Trade[/]\n\n"
                f"[green]Szenario LONG:[/]   {stueck_long} Aktien × {kurs_eur:.2f}EUR = "
                f"[bold]{stueck_long * kurs_eur:.0f}EUR[/]   "
                f"Stop: [red]{sl_long:.2f}EUR[/]\n"
                f"[red]Szenario SHORT:[/]  {stueck_short} Aktien × {kurs_eur:.2f}EUR = "
                f"[bold]{stueck_short * kurs_eur:.0f}EUR[/]   "
                f"Stop: [red]{sl_short:.2f}EUR[/]"
            )
        console.print(Panel(sizing_text,
                            title=f"Position Sizing (Depot: {depot:,.0f}EUR)",
                            border_style=risk_border))

# ── CMD: backtest ─────────────────────────────────────────────────────────────
def cmd_backtest(args):
    ticker_input  = args.targets if args.targets else []
    period        = args.period or "1y"
    haltezeit     = args.haltezeit if hasattr(args, "haltezeit") and args.haltezeit else 30
    depot         = args.depot if hasattr(args, "depot") and args.depot is not None else None
    kosten_eur    = args.kosten   if hasattr(args, "kosten")    and args.kosten is not None else 1.0
    seit_str      = args.seit     if hasattr(args, "seit")      and args.seit else None
    use_sl        = not (hasattr(args, "no_sl") and args.no_sl)
    use_trailing  = hasattr(args, "trailing") and args.trailing

    # --weights: Gewichtungen parsen z.B. "NVDA:30,AAPL:20" oder "30,20,50"
    weights_raw = args.weights if hasattr(args, "weights") and args.weights else None
    gewichte    = {}   # {ticker: anteil 0..1}

    # --seit: Startdatum filtern
    seit_datum = None
    if seit_str:
        try:
            seit_datum = datetime.strptime(seit_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            console.print(f"[red]✗ --seit Format: YYYY-MM-DD (z.B. 2023-01-01)[/]")
            return

    if not ticker_input:
        tickers = get_tickers(["us_large_cap"]); label = "US Large Cap"
    elif len(ticker_input) == 1 and ticker_input[0].lower() == "portfolio":
        # Gespeichertes Portfolio laden
        gespeichert = portfolio_laden()
        if not gespeichert:
            console.print(Panel(
                "Kein Portfolio gespeichert.\n\n"
                "Einmalig speichern mit:\n"
                "[bold]scanner portfolio-save VWRL.L:13.5 EUNL.DE:83.3 SPGP.L:1.8 3GOL.L:1.4 --depot 757[/]",
                title="[red]✗ Kein Portfolio[/]", border_style="red"
            ))
            return
        tickers    = list(gespeichert.get("gewichte", {}).keys())
        gewichte   = {t: w for t, w in gespeichert["gewichte"].items()}
        if depot is None and "depot" in gespeichert:
            depot = gespeichert["depot"]
        label = "💼 Mein Portfolio"
        console.print(f"[dim]Portfolio geladen: {', '.join(f'{t}({w:.1f}%)' for t, w in gewichte.items())}[/]")
    elif ticker_input[0] in WATCHLISTS or ticker_input[0] == "all":
        tickers = get_tickers(ticker_input); label = " + ".join(ticker_input)
    else:
        # Ticker:Gewicht Syntax erkennen (z.B. NVDA:30 AAPL:20)
        tickers = []
        for item in ticker_input:
            if ":" in item:
                t, w = item.split(":", 1)
                tickers.append(t.upper())
                try: gewichte[t.upper()] = float(w)
                except ValueError: pass
            else:
                tickers.append(item.upper())
        label = " ".join(tickers)

    # --weights Flag parsen (z.B. --weights "13.5,83.3,1.8,1.4")
    if weights_raw and not gewichte:
        try:
            werte = [float(x) for x in weights_raw.replace(",", " ").split()]
            if len(werte) == len(tickers):
                gewichte = {t: w for t, w in zip(tickers, werte)}
            else:
                console.print(f"[yellow]⚠ --weights: {len(werte)} Werte aber {len(tickers)} Ticker – ignoriert[/]")
        except ValueError:
            console.print("[yellow]⚠ --weights: ungültiges Format – ignoriert[/]")

    # Gewichte normalisieren auf 1.0
    if depot is None:
        depot = 10000  # Default falls weder --depot noch Portfolio-Depot gesetzt
    if gewichte:
        total_w = sum(gewichte.values())
        gewichte = {t: w / total_w for t, w in gewichte.items()}
        # fehlende Ticker gleichmäßig verteilen
        ohne = [t for t in tickers if t not in gewichte]
        if ohne:
            rest = 1.0 - sum(gewichte.values())
            for t in ohne:
                gewichte[t] = rest / len(ohne)
    else:
        # Gleichgewichtung
        gewichte = {t: 1.0 / len(tickers) for t in tickers}

    sl_mode = "ATR x2" if use_sl else "kein SL"
    if use_trailing: sl_mode += " + Trailing"

    gewicht_str = "  |  ".join(f"{t}: {gewichte[t]*100:.1f}%" for t in tickers[:6])
    if len(tickers) > 6: gewicht_str += " ..."
    hat_gewichte = weights_raw or any(":" in str(x) for x in ticker_input)

    console.print(Panel(
        f"[bold]Tickers:[/]  {len(tickers)}  |  [bold]Periode:[/] {period}"
        + (f"  |  [bold]Seit:[/] {seit_str}" if seit_str else "") + "\n"
        f"[bold]Haltezeit:[/]{haltezeit}d  |  [bold]Depot:[/] {depot:,.0f}EUR  |  [bold]Kosten:[/] {kosten_eur:.2f}EUR/Trade\n"
        f"[bold]Stop-Loss:[/] {sl_mode}\n"
        f"[bold]Gewichte:[/]  {gewicht_str}\n"
        f"[bold]Strategie:[/]Top 20% = LONG | Bottom 20% = SHORT (rollierend)",
        title=f"📊 Backtest v6.2 – {label}", border_style="blue"
    ))

    rates    = get_eur_rates()
    spy_data = get_spy_returns()

    console.print("[dim]Bulk-Download laeuft...[/]", end="\r")
    data_map = bulk_download(tickers, period=period)
    console.print(f"[green]✓ {len(data_map)}/{len(tickers)} Ticker geladen[/]")

    resultate = []

    with Progress(SpinnerColumn(), TextColumn("[cyan]{task.description}"),
                  BarColumn(bar_width=20),
                  "[progress.percentage]{task.percentage:>5.1f}%",
                  "[dim]{task.completed}/{task.total}[/]",
                  transient=True, console=console) as progress:
        task = progress.add_task("Backteste...", total=len(tickers))

        for ticker in tickers:
            progress.update(task, description=f"[bold]{ticker:<10}[/]")
            try:
                df = data_map.get(ticker, pd.DataFrame())
                if df.empty or len(df) < 100:
                    progress.advance(task); continue

                # --seit: DataFrame filtern
                if seit_datum is not None:
                    df_idx = df.index.tz_localize("UTC") if df.index.tzinfo is None else df.index
                    df = df[df_idx >= seit_datum]
                    if len(df) < 60:
                        progress.advance(task); continue

                # Depot-Anteil für diesen Ticker
                ticker_depot = depot * gewichte.get(ticker, 1.0 / len(tickers))

                check_tage = list(range(60, len(df) - haltezeit, 5))
                if not check_tage:
                    progress.advance(task); continue

                serien     = precompute_serien(df, spy_returns=spy_data)
                scores_all = [score_bei_index(i, df, serien) for i in check_tage]

                trades       = []
                equity       = ticker_depot
                equity_kurve = [ticker_depot]
                bh_start_idx = check_tage[0]
                bh_start     = to_eur(df["Close"].iloc[bh_start_idx], ticker, rates, last_price=df["Close"].iloc[bh_start_idx])
                next_idx     = 0

                for idx, i in enumerate(check_tage):
                    if i < next_idx: continue
                    score    = scores_all[idx]
                    past     = scores_all[:idx+1]
                    if len(past) < 5: continue
                    ps       = sorted(past); np_ = len(ps)
                    long_th  = ps[int(np_ * 0.8)]
                    short_th = ps[int(np_ * 0.2)]
                    if long_th == short_th: continue

                    kauf_raw = df["Close"].iloc[i]
                    kauf_eur = to_eur(kauf_raw, ticker, rates, last_price=kauf_raw)

                    atr_raw  = serien["atr"].iloc[i] if "atr" in serien and not pd.isna(serien["atr"].iloc[i]) else None
                    atr_eur  = to_eur(atr_raw, ticker, rates, last_price=kauf_raw) if atr_raw else kauf_eur * 0.025
                    sl_dist  = max(2 * atr_eur, kauf_eur * 0.02)
                    stueck   = int((ticker_depot * 0.02) / sl_dist) if sl_dist > 0 else 1
                    kosten   = kosten_eur * 2

                    if score >= long_th:
                        # LONG: simuliere Tag fuer Tag bis Haltezeit oder SL/Trailing
                        stop_price   = kauf_eur - 2 * atr_eur if use_sl else None
                        peak_eur     = kauf_eur
                        end_idx      = min(i + haltezeit, len(df) - 1)
                        exit_idx     = end_idx
                        exit_reason  = "Zeit"

                        for j in range(i + 1, end_idx + 1):
                            cur_raw = df["Close"].iloc[j]
                            cur_eur = to_eur(cur_raw, ticker, rates, last_price=kauf_raw)

                            # Trailing Stop: Stop zieht mit Gewinn mit
                            if use_trailing and cur_eur > peak_eur:
                                peak_eur   = cur_eur
                                stop_price = peak_eur - 2 * atr_eur

                            if use_sl and stop_price is not None and cur_eur <= stop_price:
                                exit_idx    = j
                                exit_reason = "SL"
                                break

                        verk_raw = df["Close"].iloc[exit_idx]
                        verk_eur = to_eur(verk_raw, ticker, rates, last_price=kauf_raw)
                        ret      = (verk_raw / kauf_raw - 1) * 100
                        gewinn   = (verk_eur - kauf_eur) * stueck - kosten
                        equity  += gewinn; equity_kurve.append(equity)
                        trades.append({
                            "datum": df.index[i].strftime("%d.%m.%Y"),
                            "signal": "LONG", "kauf": kauf_eur, "verkauf": verk_eur,
                            "return": ret, "gewinn": gewinn, "exit": exit_reason,
                        })
                        next_idx = exit_idx

                    elif score <= short_th:
                        # SHORT: simuliere Tag fuer Tag
                        stop_price   = kauf_eur + 2 * atr_eur if use_sl else None
                        floor_eur    = kauf_eur  # fuer Trailing SHORT
                        end_idx      = min(i + haltezeit, len(df) - 1)
                        exit_idx     = end_idx
                        exit_reason  = "Zeit"

                        for j in range(i + 1, end_idx + 1):
                            cur_raw = df["Close"].iloc[j]
                            cur_eur = to_eur(cur_raw, ticker, rates, last_price=kauf_raw)

                            if use_trailing and cur_eur < floor_eur:
                                floor_eur  = cur_eur
                                stop_price = floor_eur + 2 * atr_eur

                            if use_sl and stop_price is not None and cur_eur >= stop_price:
                                exit_idx    = j
                                exit_reason = "SL"
                                break

                        verk_raw = df["Close"].iloc[exit_idx]
                        verk_eur = to_eur(verk_raw, ticker, rates, last_price=kauf_raw)
                        ret      = (kauf_raw / verk_raw - 1) * 100
                        gewinn   = (kauf_eur - verk_eur) * stueck - kosten
                        equity  += gewinn; equity_kurve.append(equity)
                        trades.append({
                            "datum": df.index[i].strftime("%d.%m.%Y"),
                            "signal": "SHORT", "kauf": kauf_eur, "verkauf": verk_eur,
                            "return": ret, "gewinn": gewinn, "exit": exit_reason,
                        })
                        next_idx = exit_idx

                if trades:
                    returns  = [t["return"] for t in trades]
                    wins     = [r for r in returns if r > 0]
                    sl_hits  = sum(1 for t in trades if t["exit"] == "SL")
                    bh_end   = to_eur(df["Close"].iloc[-1], ticker, rates, last_price=df["Close"].iloc[-1])
                    bh_ret   = (bh_end / bh_start - 1) * 100 if bh_start > 0 else 0
                    resultate.append({
                        "ticker":       ticker,
                        "gewicht":      gewichte.get(ticker, 1.0 / len(tickers)),
                        "trades":       len(trades),
                        "winrate":      len(wins) / len(trades) * 100,
                        "avg_return":   sum(returns) / len(returns),
                        "best":         max(returns),
                        "worst":        min(returns),
                        "alle_trades":  trades,
                        "equity_start": ticker_depot,
                        "equity_end":   equity,
                        "equity_kurve": equity_kurve,
                        "max_drawdown": berechne_max_drawdown(equity_kurve),
                        "sharpe":       berechne_sharpe(returns),
                        "bh_return":    bh_ret,
                        "sl_hits":      sl_hits,
                    })
            except Exception as e:
                logging.warning(f"Backtest {ticker}: {e}")
            progress.advance(task)

    if not resultate:
        console.print("[red]✗ Keine Backtest-Daten verfuegbar[/]")
        return

    bt_table = Table(
        title=f"📊 Backtest – {label} – {period} – {haltezeit}d – {sl_mode}",
        box=box.ROUNDED
    )
    bt_table.add_column("Ticker",     style="cyan", no_wrap=True)
    bt_table.add_column("Gewicht",    justify="right", style="dim")
    bt_table.add_column("Trades",     justify="right")
    bt_table.add_column("SL-Hits",    justify="right", style="dim")
    bt_table.add_column("Win-Rate",   justify="right")
    bt_table.add_column("Avg Return", justify="right")
    bt_table.add_column("B&H",        justify="right")
    bt_table.add_column("Sharpe",     justify="right")
    bt_table.add_column("Max DD",     justify="right", style="red")
    bt_table.add_column("Depot Ende", justify="right")

    for r in sorted(resultate, key=lambda x: x["avg_return"], reverse=True):
        wr_col = "green" if r["winrate"] >= 55 else "red" if r["winrate"] < 45 else "yellow"
        ar_col = "green" if r["avg_return"] > 0 else "red"
        de_col = "green" if r["equity_end"] >= r["equity_start"] else "red"
        sh_col = "green" if r["sharpe"] > 0.5 else "red" if r["sharpe"] < 0 else "yellow"
        bh_col = "green" if r["bh_return"] > 0 else "red"
        sl_pct = r["sl_hits"] / r["trades"] * 100 if r["trades"] > 0 else 0
        bt_table.add_row(
            r["ticker"], f"{r['gewicht']*100:.1f}%",
            str(r["trades"]),
            f"[dim]{r['sl_hits']} ({sl_pct:.0f}%)[/]",
            f"[{wr_col}]{r['winrate']:.1f}%[/]",
            f"[{ar_col}]{r['avg_return']:+.1f}%[/]",
            f"[{bh_col}]{r['bh_return']:+.1f}%[/]",
            f"[{sh_col}]{r['sharpe']:.2f}[/]",
            f"{r['max_drawdown']:.1f}%",
            f"[{de_col}]{r['equity_end']:,.0f}EUR[/]",
        )
    console.print(bt_table)

    # Equity-Kurve (Einzel-Ticker)
    if len(resultate) == 1:
        r = resultate[0]
        chart_str = ascii_chart(r["equity_kurve"], width=60, height=8,
                                title=f"Equity-Kurve: {r['ticker']}")
        if chart_str:
            console.print(Panel(chart_str, title="📈 Equity-Kurve", border_style="green"))

        if hasattr(args, "verbose") and args.verbose:
            t_table = Table(title=f"Alle Trades – {r['ticker']}", box=box.SIMPLE)
            t_table.add_column("Datum",   style="dim")
            t_table.add_column("Signal",  style="bold")
            t_table.add_column("Exit",    justify="center")
            t_table.add_column("Kauf",    justify="right")
            t_table.add_column("Verkauf", justify="right")
            t_table.add_column("Return",  justify="right")
            t_table.add_column("Gewinn",  justify="right")
            for t in r["alle_trades"]:
                rc   = "green" if t["return"] > 0 else "red"
                gc   = "green" if t["gewinn"] > 0 else "red"
                ex_c = "red" if t["exit"] == "SL" else "dim"
                t_table.add_row(
                    t["datum"],
                    f"[green]{t['signal']}[/]" if t["signal"] == "LONG" else f"[red]{t['signal']}[/]",
                    f"[{ex_c}]{t['exit']}[/]",
                    f"{t['kauf']:.2f}", f"{t['verkauf']:.2f}",
                    f"[{rc}]{t['return']:+.1f}%[/]",
                    f"[{gc}]{t['gewinn']:+.0f}EUR[/]",
                )
            console.print(t_table)

    alle_returns = [t["return"] for r in resultate for t in r["alle_trades"]]
    alle_wins    = [r for r in alle_returns if r > 0]
    alle_sl      = sum(r["sl_hits"] for r in resultate)
    gesamt_wr    = len(alle_wins) / len(alle_returns) * 100 if alle_returns else 0
    gesamt_avg   = sum(alle_returns) / len(alle_returns) if alle_returns else 0
    gesamt_sh    = berechne_sharpe(alle_returns)
    # Gewichteter Drawdown und B&H
    avg_dd       = sum(r["max_drawdown"] * r["gewicht"] for r in resultate)
    avg_bh       = sum(r["bh_return"]    * r["gewicht"] for r in resultate)
    # Gesamtdepot: Summe aller gewichteten Depot-Enden
    gesamt_start = sum(r["equity_start"] for r in resultate)
    gesamt_ende  = sum(r["equity_end"]   for r in resultate)
    # B&H Depot-Ende simulieren (Startkapital * B&H-Return gewichtet)
    bh_depot_ende = sum(r["equity_start"] * (1 + r["bh_return"] / 100) for r in resultate)
    best_ticker  = max(resultate, key=lambda x: x["avg_return"])["ticker"]
    worst_ticker = min(resultate, key=lambda x: x["avg_return"])["ticker"]

    # Strategie vs B&H: Gewinn in EUR vergleichen
    strat_gewinn = gesamt_ende - gesamt_start
    bh_gewinn    = bh_depot_ende - gesamt_start

    console.print(Panel(
        f"📈 Gesamt Trades:      {len(alle_returns)}\n"
        f"🛑 Stop-Loss Exits:    {alle_sl} ({alle_sl/max(len(alle_returns),1)*100:.0f}%)\n"
        f"🎯 Win-Rate:           [{'green' if gesamt_wr >= 55 else 'red'}]{gesamt_wr:.1f}%[/]\n"
        f"💰 Avg Return:         [{'green' if gesamt_avg > 0 else 'red'}]{gesamt_avg:+.1f}% pro Trade ({haltezeit}d)[/]\n"
        f"📐 Sharpe-Ratio:       [{'green' if gesamt_sh > 0.5 else 'red'}]{gesamt_sh:.2f}[/]\n"
        f"📉 Gew. Max-Drawdown:  {avg_dd:.1f}%\n"
        f"💼 Strategie Depot:    [{'green' if gesamt_ende >= gesamt_start else 'red'}]{gesamt_ende:,.0f}EUR[/]"
        f"  ([{'green' if strat_gewinn >= 0 else 'red'}]{strat_gewinn:+.0f}EUR[/])\n"
        f"📊 Buy & Hold Depot:   [{'green' if bh_depot_ende >= gesamt_start else 'red'}]{bh_depot_ende:,.0f}EUR[/]"
        f"  ([{'green' if bh_gewinn >= 0 else 'red'}]{bh_gewinn:+.0f}EUR[/]  |  gew. {avg_bh:+.1f}%)\n"
        f"🏆 Bester:             {best_ticker}  |  💀 Schlechtester: {worst_ticker}",
        title="📊 Gesamt Summary (gewichtet)", border_style="bright_blue"
    ))

    # ── Empfehlung ────────────────────────────────────────────────────────────
    punkte_strat = 0
    punkte_bh    = 0
    gruende_strat = []
    gruende_bh    = []

    if strat_gewinn > bh_gewinn:
        punkte_strat += 2
        gruende_strat.append(f"Mehr Gewinn: {strat_gewinn:+.0f}EUR vs {bh_gewinn:+.0f}EUR")
    else:
        punkte_bh += 2
        gruende_bh.append(f"Mehr Gewinn: {bh_gewinn:+.0f}EUR vs {strat_gewinn:+.0f}EUR")

    if gesamt_sh > 0.5:
        punkte_strat += 1
        gruende_strat.append(f"Gute Sharpe-Ratio: {gesamt_sh:.2f}")
    else:
        punkte_bh += 1
        gruende_bh.append(f"Strategie Sharpe schwach ({gesamt_sh:.2f}) → B&H risikoärmer")

    if gesamt_wr >= 55:
        punkte_strat += 1
        gruende_strat.append(f"Win-Rate {gesamt_wr:.1f}% ≥ 55%")
    else:
        punkte_bh += 1
        gruende_bh.append(f"Win-Rate nur {gesamt_wr:.1f}% → B&H zuverlässiger")

    if avg_dd < 15:
        punkte_strat += 1
        gruende_strat.append(f"Drawdown unter Kontrolle: {avg_dd:.1f}%")
    else:
        punkte_bh += 1
        gruende_bh.append(f"Hoher Drawdown {avg_dd:.1f}% → B&H stabiler")

    if len(alle_returns) < 5:
        punkte_bh += 1
        gruende_bh.append("Zu wenig Trades für verlässliche Aussage")

    empfehlung_strat = punkte_strat > punkte_bh
    empfehlung_gleich = punkte_strat == punkte_bh

    if empfehlung_gleich:
        empf_text  = "🤝 [yellow]UNENTSCHIEDEN[/] – beide Ansätze gleichwertig"
        empf_style = "yellow"
    elif empfehlung_strat:
        empf_text  = "🤖 [green]AKTIVE STRATEGIE empfohlen[/]"
        empf_style = "green"
    else:
        empf_text  = "📦 [cyan]BUY & HOLD empfohlen[/]"
        empf_style = "cyan"

    gruende_str = ""
    if gruende_strat and empfehlung_strat:
        gruende_str = "\n".join(f"  ✓ {g}" for g in gruende_strat)
    elif gruende_bh and not empfehlung_strat:
        gruende_str = "\n".join(f"  ✓ {g}" for g in gruende_bh)
    else:
        gruende_str = "\n".join(f"  ~ {g}" for g in (gruende_strat + gruende_bh))

    console.print(Panel(
        f"{empf_text}\n\n{gruende_str}\n\n"
        f"[dim]Strategie: {punkte_strat} Punkte  |  Buy & Hold: {punkte_bh} Punkte[/]",
        title="💡 Empfehlung", border_style=empf_style
    ))

    if gesamt_wr < 50:
        console.print(Panel("⚠ Win-Rate unter 50% – Strategie kritisch prüfen", style="yellow"))
    elif gesamt_wr >= 60:
        console.print(Panel("✓ Gute Win-Rate!", style="green"))
# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="📊 Stock Scanner v6.2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Beispiele:
  scanner lists
  scanner scan dax40
  scanner scan dax40 --sort rsi
  scanner scan all --only long --top 10
  scanner scan dax40 --min-score 5
  scanner info NVDA --depot 25000
  scanner compare NVDA AMD INTC
  scanner watch NVDA AAPL TSLA
  scanner backtest NVDA -p 2y -v
  scanner backtest NVDA --trailing --seit 2023-01-01
  scanner backtest NVDA:30 AAPL:20 SAP:50 -p 1y
  scanner backtest VWRL.L EUNL.DE SPGP.L 3GOL.L --weights "13.5,83.3,1.8,1.4" --depot 757
  scanner backtest portfolio
  scanner portfolio-save VWRL.L:13.5 EUNL.DE:83.3 SPGP.L:1.8 3GOL.L:1.4 --depot 757
  scanner cache-clear

Auto-Workers: {_AUTO_WORKERS} (CPU-Count * 2)
        """
    )
    parser.add_argument("cmd", choices=[
        "scan","info","lists","backtest","compare","alert","watch",
        "cache-clear","portfolio","portfolio-save"
    ])
    parser.add_argument("targets",            nargs="*", help="Watchlist-IDs oder Ticker")
    parser.add_argument("-p","--period",      choices=["1mo","3mo","6mo","1y","2y","5y"], default="1y")
    parser.add_argument("-v","--verbose",     action="store_true")
    parser.add_argument("-e","--export",      action="store_true")
    parser.add_argument("--haltezeit",        type=int,   default=30)
    parser.add_argument("--depot",            type=float, default=None)
    parser.add_argument("--only",             choices=["long","short","neutral"])
    parser.add_argument("--sort",             choices=["score","rsi","atr","ticker"],
                        default="score",      help="Sortierung der Scan-Tabelle (default: score)")
    parser.add_argument("--min-score",        type=int,   default=None, dest="min_score")
    parser.add_argument("--min-score-short",  type=int,   default=None, dest="min_score_short")
    parser.add_argument("--workers",          type=int,   default=None,
                        help=f"Parallele Downloads (default: auto={_AUTO_WORKERS})")
    parser.add_argument("--kosten",           type=float, default=1.0,
                        help="Trade-Kosten EUR pro Trade (default: 1.0)")
    parser.add_argument("--top",              type=int,   default=None,
                        help="Nur Top N Ergebnisse anzeigen")
    parser.add_argument("--interval",         type=int,   default=300,
                        help="Intervall Sekunden fuer alert/watch (default: 300/10)")
    parser.add_argument("--seit",             type=str,   default=None,
                        help="Backtest Startdatum YYYY-MM-DD")
    parser.add_argument("--trailing",         action="store_true",
                        help="Trailing Stop im Backtest")
    parser.add_argument("--no-sl",            action="store_true", dest="no_sl",
                        help="Stop-Loss im Backtest deaktivieren")
    parser.add_argument("--weights",          type=str,   default=None,
                        help="Depot-Gewichte für backtest z.B. '30,50,20' oder direkt NVDA:30 AAPL:50")
    parser.add_argument("--file",             type=str,   default=None,
                        help="Portfolio CSV-Datei (Format: ticker,gewicht)")

    args = parser.parse_args()

    if args.targets:
        args.list   = ",".join(args.targets)
        args.ticker = args.targets[0] if args.cmd == "info" else None
    else:
        args.list   = "us_large_cap"
        args.ticker = None

    # Watch-Default: 10s statt 300s
    if args.cmd == "watch" and args.interval == 300:
        args.interval = 10

    if   args.cmd == "scan":        cmd_scan(args)
    elif args.cmd == "info":        cmd_info(args)
    elif args.cmd == "lists":       cmd_lists(args)
    elif args.cmd == "backtest":    cmd_backtest(args)
    elif args.cmd == "compare":     cmd_compare(args)
    elif args.cmd == "alert":       cmd_alert(args)
    elif args.cmd == "watch":       cmd_watch(args)
    elif args.cmd == "portfolio":      cmd_portfolio(args)
    elif args.cmd == "portfolio-save": cmd_portfolio_save(args)
    elif args.cmd == "cache-clear":    cache_clear()

if __name__ == "__main__":
    main()
