#!/usr/bin/env python3
"""
Stock Scanner v2.8
FIXES: EUR-API, RSI-Window, NaN-Guards, GBp-Dynamik, Delisting-7d, Weekly-RSI-Penalty, --min-score
"""

import argparse, warnings, time, csv, logging
from datetime import datetime, timezone
warnings.filterwarnings("ignore")
import yfinance as yf
import pandas as pd
import ta
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.columns import Columns
import urllib.request, json

console = Console()
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# SPY-Cache
_spy_cache = None
def get_spy_returns():
    global _spy_cache
    if _spy_cache is None:
        try:
            _spy_cache = yf.Ticker("SPY").history(period="1mo")["Close"]
        except Exception as e:
            logging.warning(f"SPY-Abruf fehlgeschlagen: {e}")
            _spy_cache = pd.Series(dtype=float)
    return _spy_cache

# FIX 1: open.er-api.com primär, Frankfurter Fallback
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
            logging.warning(f"EUR-API {url} nicht erreichbar: {e}")
    return {"USD": 0.952, "GBP": 1.185, "CHF": 1.063, "JPY": 0.0063, "EUR": 1.0}

# FIX 4: Dynamische GBp/GBP-Erkennung für .L-Ticker
def get_currency(ticker, last_price=None):
    if ticker.endswith((".DE", ".PA", ".AS", ".MI", ".MC", ".BR", ".VI")):
        return "EUR"
    elif ticker.endswith(".L"):
        if last_price is not None and last_price > 500:
            return "GBp"
        return "GBP"
    elif ticker.endswith(".SW"):
        return "CHF"
    elif ticker.endswith(".T"):
        return "JPY"
    return "USD"

def to_eur(kurs, ticker, rates, last_price=None):
    cur = get_currency(ticker, last_price=last_price)
    if cur == "EUR":
        return kurs
    elif cur == "GBp":
        return (kurs / 100) * rates.get("GBP", 1.185)
    elif cur == "GBP":
        return kurs * rates.get("GBP", 1.185)
    return kurs * rates.get(cur, 1.0)

# CSV Export
def export_csv(ergebnisse, label):
    fn = f"scan_{label}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    try:
        with open(fn, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ticker", "kurs", "score", "signal", "rsi", "atr", "info"])
            w.writeheader()
            for e in sorted(ergebnisse, key=lambda x: x["score"], reverse=True):
                w.writerow({
                    "ticker": e["ticker"],
                    "kurs": e["kurs"],
                    "score": e["score"],
                    "signal": e["signal"].replace("🟢", "").replace("🔴", "").replace("🟡", "").strip(),
                    "rsi": e["rsi"],
                    "atr": f"{e['atr']:.2f}" if e["atr"] else "-",
                    "info": e["info"]
                })
        console.print(f"\n[green]✓ CSV: {fn}[/]")
    except OSError as e:
        console.print(f"[red]✗ CSV Export fehlgeschlagen: {e}[/]")

# Watchlists
WATCHLISTS = {
    "us_large_cap": {
        "name": "🇺🇸 US Large Cap",
        "tickers": ["AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AVGO","BRK-B","JPM","V","UNH","JNJ","XOM","PG","MA","HD","CVX","MRK","ABBV","KO","COST","PEP","ADBE","WMT","NFLX","CRM","TMO","DHR","LIN","ABT","ORCL","NKE","PM","INTU","TXN","QCOM","CSCO","ACN","MCD","NEE","HON","VZ","CMCSA","IBM","GE","UPS","RTX","BA","SPGI","CAT","LOW","AXP","AMGN","BLK","BKNG","GILD","SYK","ISRG","MMC","PLD","ZTS","CB","VRTX","REGN","TJX","SCHW","MU","CI","AMAT","DE","MDT","LRCX","ADI","BMY","SBUX","CVS","PYPL","PNC"]
    },
    "dax40": {
        "name": "🇩🇪 DAX 40",
        "tickers": ["SAP.DE","SIE.DE","ALV.DE","MBG.DE","BMW.DE","MUV2.DE","DTE.DE","BAYN.DE","BAS.DE","VOW3.DE","HEN3.DE","IFX.DE","DB1.DE","ADS.DE","HEI.DE","MRK.DE","FME.DE","RHM.DE","SHL.DE","BEI.DE","AIR.DE","CON.DE","DHL.DE","EOAN.DE","FRE.DE","HNR1.DE","LIN.DE","PAH3.DE","PUM.DE","QIA.DE","RWE.DE","SY1.DE","VNA.DE","1COV.DE","ZAL.DE","MTX.DE","PNE3.DE","SZG.DE","WAF.DE","BC8.DE"]
    },
    "europe_stoxx": {
        "name": "🇪🇺 Europa STOXX",
        "tickers": ["ASML.AS","MC.PA","SAN.MC","TTE.PA","BNP.PA","OR.PA","AIR.PA","INGA.AS","NESN.SW","NOVN.SW","ROG.SW","SHEL.L","AZN.L","ULVR.L","HSBA.L","BP.L","GSK.L"]
    },
    "swiss_smi": {
        "name": "🇨🇭 Schweiz SMI",
        "tickers": ["NESN.SW","ROG.SW","NOVN.SW","UHR.SW","ABBN.SW","ZURN.SW","UBSG.SW","GIVN.SW","SREN.SW","LONN.SW"]
    },
    "asia": {
        "name": "🌏🇨🇳 Asien",
        "tickers": ["7203.T","6758.T","9984.T","7267.T","6501.T","8306.T","9432.T","6861.T","BABA","JD","PDD","NIO","XPEV","LI","TSM"]
    },
    "tech_ai": {
        "name": "💻 Tech / AI / Chips",
        "tickers": ["NVDA","AMD","INTC","QCOM","AVGO","TXN","MU","AMAT","LRCX","KLAC","ASML","TSM","MSFT","GOOGL","META","ORCL","CRM","NOW","ADBE","SNOW","PLTR","CRWD","PANW","DDOG","ZS","NET","MDB","OKTA"]
    },
    "energy_resources": {
        "name": "⚡ Energy / Rohstoffe",
        "tickers": ["XOM","CVX","COP","EOG","OXY","MPC","PSX","VLO","SLB","HAL","SHEL","BP","TTE.PA","NEE","DUK"]
    },
    "dividends": {
        "name": "💰 Dividenden",
        "tickers": ["KO","PEP","PG","JNJ","MMM","T","VZ","MO","PM","ABT","MRK","PFE","ABBV","O"]
    },
    "ipos": {
        "name": "🆕 IPOs 2023/2024",
        "tickers": ["ARM","KVYO","CART","BIRK","RDDT","RBRK","IREN","IONQ","ASTS","ACHR","JOBY"]
    },
    "crypto_stocks": {
        "name": "₿ Crypto Stocks",
        "tickers": ["COIN","MSTR","MARA","RIOT","CLSK","CIFR","BITF","HUT","IREN","WULF","XYZ","HOOD"]
    },
    "healthcare": {
        "name": "🏥 Healthcare / Biotech",
        "tickers": ["UNH","JNJ","ABT","LLY","MRK","PFE","ABBV","BMY","AMGN","GILD","BIIB","REGN","VRTX","ISRG","SYK","MDT"]
    },
    "banks_fintech": {
        "name": "🏦 Banken / Fintech",
        "tickers": ["JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF","BRK-B","AIG","MET","XYZ","PYPL","AFRM","SOFI"]
    },
    "reits": {
        "name": "🏢 REITs / Immobilien",
        "tickers": ["O","NNN","ADC","WPC","PLD","STAG","EXR","PSA","BXP","EQR","AVB","CPT","MAA","VNA.DE","LEG.DE"]
    },
}

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

# FIX 5: Delisting-Limit 30 → 7 Tage + timezone-safe
def lade_daten(ticker, period="1y"):
    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval="1d", auto_adjust=True, prepost=False)
        if df.empty or len(df) < 50:
            return pd.DataFrame(), False, f"Keine Daten ({len(df)} Tage)"

        last_date = df.index[-1].to_pydatetime()
        last_date = last_date.replace(tzinfo=timezone.utc) if last_date.tzinfo is None else last_date
        if (datetime.now(timezone.utc) - last_date).days > 7:
            return df, False, f"DELISTED ({last_date.strftime('%d.%m.%Y')})"

        recent = df["Close"].tail(6).pct_change().iloc[1:].abs()
        split_flag = recent.max() > 0.50
        return df, split_flag, ("⚠ SPLIT?" if split_flag else "OK")
    except Exception as e:
        logging.warning(f"{ticker}: {e}")
        return pd.DataFrame(), False, f"ERROR: {str(e)[:40]}"

def berechne_atr(df, window=14):
    try:
        return ta.volatility.AverageTrueRange(
            df["High"], df["Low"], df["Close"], window=window
        ).average_true_range().iloc[-1]
    except Exception as e:
        logging.debug(f"ATR-Berechnung: {e}")
        return None

# FIX 2+3+6: RSI auf vollem Close, NaN-Guards, Weekly-RSI > 80 = -1
def berechne_score(df, spy_returns=None):
    if df.empty or len(df) < 50:
        return 0, {}

    close_full = df["Close"]           # Voller Verlauf für RSI, MACD, SMA200, SMA50
    close20 = df["Close"].iloc[-20:]   # Nur für SMA20, SMA10, BB
    kurs = close_full.iloc[-1]
    score = 0
    details = {}

    try:
        sma200_val = ta.trend.SMAIndicator(close_full, window=200).sma_indicator().iloc[-1]
        if not pd.isna(sma200_val):
            if kurs > sma200_val:
                score += 1; details["SMA200"] = "+1"
            else:
                score -= 3; details["SMA200"] = "-3"

        sma50_val = ta.trend.SMAIndicator(close_full, window=50).sma_indicator().iloc[-1]
        if not pd.isna(sma50_val) and not pd.isna(sma200_val):
            if sma50_val > sma200_val:
                score += 1; details["Cross"] = "+1"

        rsi_val = ta.momentum.RSIIndicator(close_full, window=14).rsi().iloc[-1]
        if not pd.isna(rsi_val):
            if rsi_val < 35:
                score += 2; details["RSI"] = "+2"
            elif rsi_val < 50:
                score += 1; details["RSI"] = "+1"
            elif rsi_val > 75:
                score -= 2; details["RSI"] = "-2"
            elif rsi_val > 70:
                score -= 1; details["RSI"] = "-1"

        macd_hist = ta.trend.MACD(close_full).macd_diff().iloc[-1]
        if not pd.isna(macd_hist):
            if macd_hist > 0:
                score += 1; details["MACD"] = "+1"
            else:
                score -= 1; details["MACD"] = "-1"

        sma20_val = ta.trend.SMAIndicator(close20, window=20).sma_indicator().iloc[-1]
        if not pd.isna(sma20_val):
            if kurs > sma20_val:
                score += 1; details["SMA20"] = "+1"

        sma10_val = ta.trend.SMAIndicator(close20, window=10).sma_indicator().iloc[-1]
        if not pd.isna(sma10_val):
            if kurs < sma10_val:
                score -= 1; details["SMA10"] = "-1"

        bb = ta.volatility.BollingerBands(close20)
        bb_lower = bb.bollinger_lband().iloc[-1]
        if not pd.isna(bb_lower):
            if kurs < bb_lower:
                score += 1; details["BB"] = "+1"

        vol_ma20 = df["Volume"].rolling(20).mean().iloc[-1]
        if not pd.isna(vol_ma20) and vol_ma20 > 0:
            if df["Volume"].iloc[-1] > vol_ma20 * 1.5:
                score += 1; details["Vol"] = "+1"

        weekly = close_full.resample("W").last()
        if len(weekly) >= 2 and not pd.isna(weekly.iloc[-1]) and not pd.isna(weekly.iloc[-2]):
            if weekly.iloc[-1] > weekly.iloc[-2]:
                score += 1; details["Weekly"] = "+1"

        # FIX 6: Weekly-RSI > 80 = -1 Überhitzungs-Signal
        if len(weekly) >= 15:
            w_rsi_score = ta.momentum.RSIIndicator(weekly, window=14).rsi().iloc[-1]
            if not pd.isna(w_rsi_score) and w_rsi_score > 80:
                score -= 1; details["W-RSI>80"] = "-1"

        spy = spy_returns if spy_returns is not None else get_spy_returns()
        if len(spy) > 1 and len(close_full) > 1:
            spy_ret = spy.pct_change(20).iloc[-1]
            stock_ret = close_full.pct_change(20).iloc[-1]
            if not pd.isna(spy_ret) and not pd.isna(stock_ret):
                rs = stock_ret / spy_ret if spy_ret != 0 else 0
                if rs > 1.05:
                    score += 1; details["RS"] = "+1"
                elif rs < 0.95:
                    score -= 1; details["RS"] = "-1"

        high_52w = close_full.rolling(252, min_periods=50).max().iloc[-1]
        low_52w = close_full.rolling(252, min_periods=50).min().iloc[-1]
        if not pd.isna(high_52w) and not pd.isna(low_52w) and high_52w > 0 and low_52w > 0:
            if (high_52w - kurs) / high_52w < 0.10:
                score += 2; details["52W-H"] = "+2"
            elif (kurs - low_52w) / low_52w < 0.10:
                score += 1; details["52W-L"] = "+1"

        if len(df) >= 2:
            prev_close = close_full.iloc[-2]
            if not pd.isna(prev_close) and prev_close > 0:
                gap = abs((kurs / prev_close - 1) * 100)
                if gap > 3:
                    if kurs > prev_close:
                        score += 1; details["Gap↑"] = f"+1 ({gap:.1f}%)"
                    else:
                        score -= 1; details["Gap↓"] = f"-1 ({gap:.1f}%)"

    except Exception as e:
        logging.warning(f"Score-Berechnung fehlgeschlagen: {e}")

    return score, details

# CMD: lists
def cmd_lists(args):
    table = Table(title="📋 Verfügbare Watchlists", box=box.ROUNDED)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Ticker", justify="right", style="green")
    table.add_column("Beispiele", style="dim")

    for wl_id, wl in WATCHLISTS.items():
        count = len(wl["tickers"])
        examples = ", ".join(wl["tickers"][:4]) + ("..." if count > 4 else "")
        table.add_row(wl_id, wl["name"], str(count), examples)

    console.print(table)
    console.print(f"\n[dim]Gesamt: {len(WATCHLISTS)} Listen | Tickers: {len(get_tickers(['all']))}[/]")
    console.print("\n[bold]Beispiele:[/]")
    console.print("  scanner scan dax40")
    console.print("  scanner scan tech_ai crypto_stocks")
    console.print("  scanner scan all")
    console.print("  scanner scan dax40 --only long")
    console.print("  scanner scan dax40 --export")
    console.print("  scanner scan dax40 --min-score 5")
    console.print("  scanner backtest NVDA -p 2y -v")
    console.print("  scanner backtest dax40 -p 1y --depot 25000")

# FIX 7: cmd_scan mit --min-score Flag
def cmd_scan(args):
    list_names = args.list.split(",") if args.list else ["us_large_cap"]
    tickers = get_tickers(list_names)
    min_score = args.min_score if hasattr(args, "min_score") else None

    names = []
    for n in list_names:
        if n == "all":
            names.append("🌍 Alle Listen")
        elif n in WATCHLISTS:
            names.append(WATCHLISTS[n]["name"])
        else:
            names.append(n)

    mode_str = f"absolut (--min-score {min_score})" if min_score is not None else "dynamisch (Top/Bot 20%)"
    console.print(Panel(
        f"[bold]Liste:[/] {' + '.join(names)}\n[bold]Tickers:[/] {len(tickers)}\n[bold]Modus:[/] {mode_str}",
        title="🔍 Scan gestartet", border_style="blue"
    ))

    rates = get_eur_rates()
    spy_data = get_spy_returns()
    ergebnisse = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        "[progress.percentage]{task.percentage:>5.1f}%",
        "[dim]{task.completed}/{task.total}[/]",
        transient=True, console=console,
    ) as progress:
        task = progress.add_task("Scanne...", total=len(tickers))
        for ticker in tickers:
            progress.update(task, description=f"[bold]{ticker:<10}[/]")

            df, split_flag, split_info = lade_daten(ticker)
            score, details = berechne_score(df, spy_returns=spy_data)

            rsi_val = "-"
            kurs_val = "-"
            atr_val = None

            if not df.empty:
                try:
                    rsi_val = f"{ta.momentum.RSIIndicator(df['Close'], window=14).rsi().iloc[-1]:.0f}"
                    kurs_raw = df["Close"].iloc[-1]
                    kurs_eur = to_eur(kurs_raw, ticker, rates, last_price=kurs_raw)
                    kurs_val = f"{kurs_eur:.2f}€"
                    atr_val = berechne_atr(df)
                except Exception as e:
                    logging.warning(f"{ticker} Kurs/RSI: {e}")

            ergebnisse.append({
                "ticker": ticker + (" ⚠" if split_flag else ""),
                "kurs": kurs_val,
                "score": score,
                "signal": "NEUTRAL",
                "rsi": rsi_val,
                "atr": atr_val,
                "info": split_info,
                "details": details,
            })
            progress.advance(task)
            time.sleep(0.05)

    scores = [e["score"] for e in ergebnisse]
    long_count = short_count = neutral_count = 0
    long_threshold = short_threshold = 0

    if scores:
        if min_score is not None:
            long_threshold = min_score
            short_threshold = min_score - 7
            for e in ergebnisse:
                if e["score"] >= long_threshold:
                    e["signal"] = "🟢 LONG"; long_count += 1
                elif e["score"] <= short_threshold:
                    e["signal"] = "🔴 SHORT"; short_count += 1
                else:
                    e["signal"] = "🟡 NEUTRAL"; neutral_count += 1
        else:
            scores_sorted = sorted(scores)
            n = len(scores_sorted)
            long_threshold = scores_sorted[int(n * 0.8)]
            short_threshold = scores_sorted[int(n * 0.2)]
            for e in ergebnisse:
                if e["score"] >= long_threshold:
                    e["signal"] = "🟢 LONG"; long_count += 1
                elif e["score"] <= short_threshold:
                    e["signal"] = "🔴 SHORT"; short_count += 1
                else:
                    e["signal"] = "🟡 NEUTRAL"; neutral_count += 1

    if hasattr(args, "only") and args.only:
        filter_map = {"long": "🟢 LONG", "short": "🔴 SHORT", "neutral": "🟡 NEUTRAL"}
        ergebnisse_show = [e for e in ergebnisse if e["signal"] == filter_map.get(args.only)]
    else:
        ergebnisse_show = ergebnisse

    table = Table(
        title=f"📊 STOCK SCANNER v2.8 – {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        box=box.ROUNDED
    )
    table.add_column("Ticker", style="cyan", no_wrap=True)
    table.add_column("Kurs €", justify="right", style="green")
    table.add_column("Score", justify="right")
    table.add_column("Signal", style="bold magenta")
    table.add_column("RSI", justify="right")
    table.add_column("ATR", justify="right", style="dim")

    for e in sorted(ergebnisse_show, key=lambda x: x["score"], reverse=True):
        atr_str = f"{e['atr']:.2f}" if e["atr"] else "-"
        table.add_row(e["ticker"][:12], e["kurs"], str(e["score"]), e["signal"], e["rsi"], atr_str)

    if min_score is not None:
        schwelle_str = f"Modus: absolut | LONG >= {long_threshold} | SHORT <= {short_threshold}"
    else:
        schwelle_str = f"Modus: dynamisch | LONG >= {long_threshold} | SHORT <= {short_threshold}"

    summary = Panel(
        f"🟢 LONG: {long_count} | 🔴 SHORT: {short_count} | 🟡 NEUTRAL: {neutral_count}\n"
        f"{schwelle_str}",
        title="Summary", border_style="bright_blue"
    )

    console.print(Columns([table, summary]))

    if hasattr(args, "export") and args.export:
        export_csv(ergebnisse_show, args.list.replace(",", "_"))

    warns = list(set(e["info"] for e in ergebnisse if "DELIST" in e["info"] or "SPLIT" in e["info"]))
    if warns:
        console.print(Panel("\n".join(warns), title="⚠ Warnungen", style="yellow"))

# CMD: info
def cmd_info(args):
    if not args.ticker:
        console.print("[red]✗ Ticker angeben: scanner info NVDA")
        return

    ticker = args.ticker.upper()
    console.print(f"[cyan]Lade Daten für {ticker}...[/]")

    df, split_flag, split_info = lade_daten(ticker)
    spy_data = get_spy_returns()
    score, details = berechne_score(df, spy_returns=spy_data)

    if df.empty:
        console.print(Panel(f"[bold red]{ticker}:[/] {split_info}", style="red"))
        return

    rates = get_eur_rates()
    kurs = df["Close"].iloc[-1]
    kurs_eur = to_eur(kurs, ticker, rates, last_price=kurs)

    sma20 = ta.trend.SMAIndicator(df["Close"], window=20).sma_indicator().iloc[-1]
    sma50 = ta.trend.SMAIndicator(df["Close"], window=50).sma_indicator().iloc[-1]
    sma200 = ta.trend.SMAIndicator(df["Close"], window=200).sma_indicator().iloc[-1]
    rsi = ta.momentum.RSIIndicator(df["Close"], window=14).rsi().iloc[-1]
    macd_obj = ta.trend.MACD(df["Close"])
    macd_h = macd_obj.macd_diff().iloc[-1]
    bb = ta.volatility.BollingerBands(df["Close"])
    bb_up = bb.bollinger_hband().iloc[-1]
    bb_lo = bb.bollinger_lband().iloc[-1]
    atr = berechne_atr(df)

    high_52w = df["Close"].rolling(252, min_periods=50).max().iloc[-1]
    low_52w = df["Close"].rolling(252, min_periods=50).min().iloc[-1]
    high_52w_eur = to_eur(high_52w, ticker, rates, last_price=kurs)
    low_52w_eur = to_eur(low_52w, ticker, rates, last_price=kurs)

    weekly_close = df["Close"].resample("W").last()
    try:
        w_rsi = ta.momentum.RSIIndicator(weekly_close, window=14).rsi().iloc[-1]
    except Exception:
        w_rsi = None

    trend_str = "↑ [bold green]AUFWÄRTSTREND[/]" if kurs > sma200 else "↓ [bold red]ABWÄRTSTREND[/]"
    cross_str = "[green]✓ Golden Cross[/]" if sma50 > sma200 else "[red]✗ Death Cross[/]"
    vs_sma200 = (kurs / sma200 - 1) * 100

    try:
        spy_ret = spy_data.pct_change(20).iloc[-1]
        stock_ret = df["Close"].pct_change(20).iloc[-1]
        rs = stock_ret / spy_ret if spy_ret != 0 else 0
        rs_str = f"{(stock_ret - spy_ret)*100:+.1f}% {'stark' if rs > 1.1 else 'neutral'} vs SPY"
    except Exception:
        rs_str = "n/a"

    if w_rsi and not pd.isna(w_rsi):
        wt_str = (f"[green]✓ Bullisch[/] (W-RSI={w_rsi:.0f})" if w_rsi > 50
                  else f"[red]✗ Bärisch[/] (W-RSI={w_rsi:.0f})")
        if w_rsi > 80:
            wt_str += " [yellow]⚠ Überhitzt[/]"
    else:
        wt_str = "n/a"

    if score >= 4:
        sig_str = f"[green]🟢 LONG[/] (Score: {score})"
    elif score <= 0:
        sig_str = f"[red]🔴 SHORT[/] (Score: {score})"
    else:
        sig_str = f"[yellow]🟡 NEUTRAL[/] (Score: {score})"

    ov = Text()
    ov.append("Kurs: "); ov.append(f"{kurs_eur:.2f}€\n", style="bold green")
    ov.append(f"Signal: {sig_str}\n")
    ov.append(f"Trend (Daily): {trend_str}\n")
    ov.append("vs SMA200: "); ov.append(f"{vs_sma200:+.1f}% (SMA200={to_eur(sma200,ticker,rates,last_price=kurs):.2f}€)\n", style="dim")
    ov.append(f"SMA50/200: {cross_str} (SMA50={to_eur(sma50,ticker,rates,last_price=kurs):.2f}€)\n")
    ov.append(f"Weekly-Trend: {wt_str}\n")
    ov.append("RS vs SPY: "); ov.append(f"{rs_str}\n", style="dim")
    ov.append("52W Hoch: "); ov.append(f"{high_52w_eur:.2f}€\n", style="dim")
    ov.append("52W Tief: "); ov.append(f"{low_52w_eur:.2f}€\n", style="dim")
    if split_flag:
        ov.append("⚠ SPLIT erkannt!\n", style="bold yellow")

    console.print(Panel(ov, title=f"📋 DETAIL: {ticker}", border_style="green"))

    ind_table = Table(box=box.SIMPLE)
    ind_table.add_column("Indikator", style="cyan")
    ind_table.add_column("Wert", justify="right")
    ind_table.add_column("Status")
    ind_table.add_row("RSI(14)", f"{rsi:.2f}", "[green]Überverkauft[/]" if rsi < 35 else "[red]Überkauft[/]" if rsi > 70 else "-")
    ind_table.add_row("MACD-Hist", f"{macd_h:.4f}", "[green]Bullisch[/]" if macd_h > 0 else "[red]Bärisch[/]")
    ind_table.add_row("SMA20", f"{to_eur(sma20,ticker,rates,last_price=kurs):.2f}€", "[green]✓[/]" if kurs > sma20 else "[red]✗[/]")
    ind_table.add_row("SMA50", f"{to_eur(sma50,ticker,rates,last_price=kurs):.2f}€", "[green]✓[/]" if kurs > sma50 else "[red]✗[/]")
    ind_table.add_row("SMA200", f"{to_eur(sma200,ticker,rates,last_price=kurs):.2f}€", "[green]✓[/]" if kurs > sma200 else "[red]✗[/]")
    ind_table.add_row("BB-Upper", f"{to_eur(bb_up,ticker,rates,last_price=kurs):.2f}€", "-")
    ind_table.add_row("BB-Lower", f"{to_eur(bb_lo,ticker,rates,last_price=kurs):.2f}€", "-")
    ind_table.add_row("ATR(14)", f"{to_eur(atr,ticker,rates,last_price=kurs):.2f}€" if atr else "-", "-")
    if w_rsi and not pd.isna(w_rsi):
        w_status = "[green]Bullisch[/]" if w_rsi > 50 else "[red]Bärisch[/]"
        if w_rsi > 80:
            w_status += " [yellow]⚠[/]"
        ind_table.add_row("Weekly RSI", f"{w_rsi:.1f}", w_status)

    console.print(Panel(ind_table, title="Indikatoren", border_style="blue"))

    sc_table = Table(box=box.SIMPLE)
    sc_table.add_column("Status", justify="center")
    sc_table.add_column("Indikator")
    sc_table.add_column("Punkte", justify="right")

    score_map = {
        "SMA200": ("Kurs > SMA200", "+1" if kurs > sma200 else "-3"),
        "Cross": ("Golden Cross SMA50>SMA200", "+1"),
        "RSI": (f"RSI={rsi:.1f}", details.get("RSI", "0")),
        "MACD": (f"MACD={macd_h:.4f} {'bullisch' if macd_h > 0 else 'bärisch'}", details.get("MACD", "0")),
        "SMA20": ("Kurs > SMA20 bullisch", details.get("SMA20", "+0")),
        "SMA10": ("Kurs < SMA10", details.get("SMA10", "+0")),
        "BB": ("Kurs unter BB-Lower", details.get("BB", "+0")),
        "Vol": ("Volume Spike >1.5x", details.get("Vol", "+0")),
        "Weekly": ("Weekly-Trend bullisch", details.get("Weekly", "+0")),
        "W-RSI>80": ("Weekly-RSI > 80 (Überhitzt)", details.get("W-RSI>80", "+0")),
        "RS": (f"RS vs SPY: {rs_str}", details.get("RS", "0")),
        "52W-H": ("52W Hoch Nähe (<10%)", details.get("52W-H", "+0")),
        "52W-L": ("52W Tief Nähe (<10%)", details.get("52W-L", "+0")),
        "Gap↑": ("Gap >3% bullisch", details.get("Gap↑", "+0")),
        "Gap↓": ("Gap >3% bärisch", details.get("Gap↓", "+0")),
    }

    for key, (label, pts) in score_map.items():
        if key in details:
            icon = ("[green]✓[/]" if pts.startswith("+") and pts != "+0"
                    else "[red]✗[/]" if pts.startswith("-") else "[yellow]⚠[/]")
            sc_table.add_row(icon, label, pts)

    console.print(Panel(sc_table, title=f"Score-Details → {score} Punkte", border_style="magenta"))

    if atr:
        atr_eur = to_eur(atr, ticker, rates, last_price=kurs)
        sl_fix = kurs_eur * 0.95
        sl_atr = kurs_eur - 2 * atr_eur
        tp1 = kurs_eur * 1.08
        tp2 = kurs_eur * 1.15
        tp3 = kurs_eur * 1.25
        tp3_atr = kurs_eur + 3 * atr_eur

        risk_table = Table(box=box.SIMPLE)
        risk_table.add_column("Level", style="bold")
        risk_table.add_column("Fix-Prozent", justify="right")
        risk_table.add_column("ATR-basiert", justify="right")
        risk_table.add_row("[red]Stop-Loss[/]", f"{sl_fix:.2f}€", f"{sl_atr:.2f}€")
        risk_table.add_row("[green]TP1[/]", f"{tp1:.2f}€", "-")
        risk_table.add_row("[green]TP2[/]", f"{tp2:.2f}€", "-")
        risk_table.add_row("[green]TP3[/]", f"{tp3:.2f}€", f"{tp3_atr:.2f}€")

        console.print(Panel(risk_table, title="Risiko-Management (LONG)", border_style="red"))

        depot = args.depot if hasattr(args, "depot") and args.depot else 10000
        risiko = depot * 0.02
        sl_dist = kurs_eur - sl_atr
        stueck = int(risiko / sl_dist) if sl_dist > 0 else 0
        pos_wert = stueck * kurs_eur

        console.print(Panel(
            f"Max-Risiko: [bold]{risiko:.0f}€ (2%)[/]\n"
            f"Stückzahl: [bold]{stueck} Aktien[/]\n"
            f"Positionswert: [bold]{pos_wert:.0f}€[/]",
            title=f"Position Sizing (2%-Regel, Depot: {depot:,.0f}€)", border_style="dim"
        ))

# CMD: backtest
def cmd_backtest(args):
    ticker_input = args.targets if args.targets else []
    period = args.period or "1y"
    haltezeit = args.haltezeit if hasattr(args, "haltezeit") and args.haltezeit else 30
    depot = args.depot if hasattr(args, "depot") and args.depot else 10000

    if not ticker_input:
        tickers = get_tickers(["us_large_cap"])
        label = "US Large Cap"
    elif ticker_input[0] in WATCHLISTS or ticker_input[0] == "all":
        tickers = get_tickers(ticker_input)
        label = " + ".join(ticker_input)
    else:
        tickers = [t.upper() for t in ticker_input]
        label = " ".join(tickers)

    console.print(Panel(
        f"[bold]Tickers:[/] {len(tickers)}\n"
        f"[bold]Periode:[/] {period}\n"
        f"[bold]Haltezeit:[/] {haltezeit} Tage\n"
        f"[bold]Depot:[/] {depot:,.0f}€\n"
        f"[bold]Strategie:[/] Score Top 20% = LONG | Bottom 20% = SHORT\n"
        f"[bold]Schwelle:[/] rollierend (kein Look-Ahead)",
        title=f"📊 Backtest v2.8 – {label}", border_style="blue"
    ))

    rates = get_eur_rates()
    spy_data = get_spy_returns()
    resultate = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        "[progress.percentage]{task.percentage:>5.1f}%",
        "[dim]{task.completed}/{task.total}[/]",
        transient=True, console=console,
    ) as progress:
        task = progress.add_task("Backteste...", total=len(tickers))
        for ticker in tickers:
            progress.update(task, description=f"[bold]{ticker:<10}[/]")

            try:
                df, _, _ = lade_daten(ticker, period=period)
                if df.empty or len(df) < 100:
                    progress.advance(task)
                    continue

                check_tage = list(range(60, len(df) - haltezeit, 5))
                scores_all = []
                for i in check_tage:
                    s, _ = berechne_score(df.iloc[:i], spy_returns=spy_data)
                    scores_all.append(s)

                if not scores_all:
                    progress.advance(task)
                    continue

                trades = []
                equity = depot
                next_trade_idx = 0

                for idx, i in enumerate(check_tage):
                    if i < next_trade_idx:
                        continue

                    score = scores_all[idx]
                    past_scores = scores_all[:idx + 1]
                    if len(past_scores) < 5:
                        continue

                    ps_sorted = sorted(past_scores)
                    n_past = len(ps_sorted)
                    long_thresh = ps_sorted[int(n_past * 0.8)]
                    short_thresh = ps_sorted[int(n_past * 0.2)]

                    end_idx = min(i + haltezeit, len(df) - 1)
                    kauf = df["Close"].iloc[i]
                    verkauf = df["Close"].iloc[end_idx]
                    kauf_eur = to_eur(kauf, ticker, rates, last_price=kauf)
                    verkauf_eur = to_eur(verkauf, ticker, rates, last_price=kauf)

                    if score >= long_thresh:
                        ret = (verkauf / kauf - 1) * 100
                        sl_dist = kauf_eur * 0.05
                        stueck = int((depot * 0.02) / sl_dist) if sl_dist > 0 else 1
                        gewinn = (verkauf_eur - kauf_eur) * stueck
                        equity += gewinn
                        trades.append({
                            "datum": df.index[i].strftime("%d.%m.%Y"),
                            "signal": "LONG",
                            "kauf": kauf_eur,
                            "verkauf": verkauf_eur,
                            "return": ret,
                            "gewinn": gewinn
                        })
                        next_trade_idx = end_idx

                    elif score <= short_thresh:
                        ret = (kauf / verkauf - 1) * 100
                        sl_dist = kauf_eur * 0.05
                        stueck = int((depot * 0.02) / sl_dist) if sl_dist > 0 else 1
                        gewinn = (kauf_eur - verkauf_eur) * stueck
                        equity += gewinn
                        trades.append({
                            "datum": df.index[i].strftime("%d.%m.%Y"),
                            "signal": "SHORT",
                            "kauf": kauf_eur,
                            "verkauf": verkauf_eur,
                            "return": ret,
                            "gewinn": gewinn
                        })
                        next_trade_idx = end_idx

                if trades:
                    returns = [t["return"] for t in trades]
                    wins = [r for r in returns if r > 0]
                    resultate.append({
                        "ticker": ticker,
                        "trades": len(trades),
                        "winrate": len(wins) / len(trades) * 100,
                        "avg_return": sum(returns) / len(returns),
                        "best": max(returns),
                        "worst": min(returns),
                        "alle_trades": trades,
                        "equity_start": depot,
                        "equity_end": equity,
                    })

            except Exception as e:
                logging.warning(f"Backtest {ticker}: {e}")

            progress.advance(task)
            time.sleep(0.02)

    if not resultate:
        console.print("[red]✗ Keine Backtest-Daten verfügbar[/]")
        return

    bt_table = Table(
        title=f"📊 Backtest – {label} – {period} – {haltezeit}d Haltezeit",
        box=box.ROUNDED
    )
    bt_table.add_column("Ticker", style="cyan", no_wrap=True)
    bt_table.add_column("Trades", justify="right")
    bt_table.add_column("Win-Rate", justify="right")
    bt_table.add_column("Ø Return", justify="right")
    bt_table.add_column("Bester", justify="right", style="green")
    bt_table.add_column("Schlechtester", justify="right", style="red")
    bt_table.add_column("Depot Ende", justify="right")

    for r in sorted(resultate, key=lambda x: x["avg_return"], reverse=True):
        wr_col = "green" if r["winrate"] >= 55 else "red" if r["winrate"] < 45 else "yellow"
        ar_col = "green" if r["avg_return"] > 0 else "red"
        de_col = "green" if r["equity_end"] >= r["equity_start"] else "red"
        bt_table.add_row(
            r["ticker"],
            str(r["trades"]),
            f"[{wr_col}]{r['winrate']:.1f}%[/]",
            f"[{ar_col}]{r['avg_return']:+.1f}%[/]",
            f"+{r['best']:.1f}%",
            f"{r['worst']:.1f}%",
            f"[{de_col}]{r['equity_end']:,.0f}€[/]"
        )

    console.print(bt_table)

    if len(resultate) == 1 and hasattr(args, "verbose") and args.verbose:
        r = resultate[0]
        t_table = Table(title=f"📋 Alle Trades – {r['ticker']}", box=box.SIMPLE)
        t_table.add_column("Datum", style="dim")
        t_table.add_column("Signal", style="bold")
        t_table.add_column("Kauf €", justify="right")
        t_table.add_column("Verkauf €", justify="right")
        t_table.add_column("Return", justify="right")
        t_table.add_column("Gewinn €", justify="right")

        for t in r["alle_trades"]:
            rc = "green" if t["return"] > 0 else "red"
            gc = "green" if t["gewinn"] > 0 else "red"
            t_table.add_row(
                t["datum"],
                f"[green]{t['signal']}[/]" if t["signal"] == "LONG" else f"[red]{t['signal']}[/]",
                f"{t['kauf']:.2f}€",
                f"{t['verkauf']:.2f}€",
                f"[{rc}]{t['return']:+.1f}%[/]",
                f"[{gc}]{t['gewinn']:+.0f}€[/]"
            )

        console.print(t_table)

    alle_returns = [t["return"] for r in resultate for t in r["alle_trades"]]
    alle_wins = [r for r in alle_returns if r > 0]
    gesamt_wr = len(alle_wins) / len(alle_returns) * 100 if alle_returns else 0
    gesamt_avg = sum(alle_returns) / len(alle_returns) if alle_returns else 0
    best_ticker = max(resultate, key=lambda x: x["avg_return"])["ticker"]
    worst_ticker = min(resultate, key=lambda x: x["avg_return"])["ticker"]
    avg_depot = sum(r["equity_end"] for r in resultate) / len(resultate)

    console.print(Panel(
        f"📈 Gesamt Trades: {len(alle_returns)}\n"
        f"🎯 Win-Rate: [{'green' if gesamt_wr >= 55 else 'red'}]{gesamt_wr:.1f}%[/]\n"
        f"💰 Ø Return: [{'green' if gesamt_avg > 0 else 'red'}]{gesamt_avg:+.1f}% ({haltezeit}d)[/]\n"
        f"💼 Ø Depot-Ende: [{'green' if avg_depot >= depot else 'red'}]{avg_depot:,.0f}€[/] (Start: {depot:,.0f}€)\n"
        f"🏆 Bester Ticker: {best_ticker}\n"
        f"💀 Schlechtester: {worst_ticker}",
        title="📊 Gesamt Summary", border_style="bright_blue"
    ))

    if gesamt_wr < 50:
        console.print(Panel("⚠ Win-Rate unter 50% – Strategie funktioniert hier nicht optimal", style="yellow"))
    elif gesamt_wr >= 60:
        console.print(Panel("✓ Gute Win-Rate! Strategie funktioniert gut für diese Aktien.", style="green"))

# Main
def main():
    parser = argparse.ArgumentParser(
        description="📊 Stock Scanner v2.8",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  scanner lists
  scanner scan
  scanner scan dax40
  scanner scan tech_ai crypto_stocks
  scanner scan all
  scanner scan dax40 --only long
  scanner scan dax40 --export
  scanner scan dax40 --min-score 5
  scanner scan all --min-score 6
  scanner info NVDA
  scanner info NVDA --depot 25000
  scanner backtest NVDA -p 2y
  scanner backtest NVDA -p 2y --haltezeit 14 -v
  scanner backtest dax40 -p 1y --depot 25000
        """
    )

    parser.add_argument("cmd", choices=["scan", "info", "lists", "backtest"])
    parser.add_argument("targets", nargs="*", help="Watchlist-IDs oder Ticker")
    parser.add_argument("-p", "--period",
                        choices=["1mo", "3mo", "6mo", "1y", "2y", "5y"], default="1y")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Einzelne Trades anzeigen (backtest)")
    parser.add_argument("-e", "--export", action="store_true",
                        help="CSV Export")
    parser.add_argument("--haltezeit", type=int, default=30,
                        help="Haltezeit in Tagen (backtest, default: 30)")
    parser.add_argument("--depot", type=float, default=10000,
                        help="Depotgroesse in Euro (default: 10000)")
    parser.add_argument("--only", choices=["long", "short", "neutral"],
                        help="Nur LONG/SHORT/NEUTRAL anzeigen (scan)")
    parser.add_argument("--min-score", type=int, default=None, dest="min_score",
                        help="Absoluter Score-Filter: LONG >= X, SHORT <= X-7")

    args = parser.parse_args()

    if args.targets:
        args.list = ",".join(args.targets)
        args.ticker = args.targets[0] if args.cmd == "info" else None
    else:
        args.list = "us_large_cap"
        args.ticker = None

    if args.cmd == "scan":
        cmd_scan(args)
    elif args.cmd == "info":
        cmd_info(args)
    elif args.cmd == "lists":
        cmd_lists(args)
    elif args.cmd == "backtest":
        cmd_backtest(args)

if __name__ == "__main__":
    main()

