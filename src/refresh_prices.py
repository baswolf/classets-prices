#!/usr/bin/env python3
"""Rebuild prices.json from Yahoo Finance's latest completed daily close.

The Classets classroom simulator trades at one price all day. This script
reads that price from the same daily bars behind the Yahoo Finance page,
then writes prices.json at the repository root. GitHub Pages serves that
file at https://prices.bigyetigames.com/prices.json.

While the US market is open, the bar for today is still forming, so it is
left out. The price kept is the previous session's close — the number Yahoo
shows as the previous close — not the live tick. A phone that sampled the
live tick at 10:00 and another at 14:00 would fill the same order at two
different prices.

Yahoo's chart API does not send Access-Control-Allow-Origin, so the app
cannot call it. Run this once each weekday after the US close. GitHub Pages
sends that header, which is why the file is published from this repository.

    python3 src/refresh_prices.py            # write prices.json
    python3 src/refresh_prices.py --check    # check anchor math, no network
"""
import calendar
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICES_PATH = os.path.join(ROOT, "prices.json")
# The list of symbols to price, with display metadata ({s, n, t, g, f}). Edit this file to
# add or remove names. Anything already in prices.json but missing here is still priced,
# so a student's holding never silently disappears; delete it from both to retire it.
SYMBOLS_PATH = os.path.join(ROOT, "symbols.json")
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2y&includeAdjustedClose=true"
UA = {"User-Agent": "Classets/1.0 (classroom simulator; one daily close)"}
WORKERS = 8
# A handful of delistings should not freeze the other 700 names. A wide
# failure means Yahoo throttled or changed the response, and the old file
# is safer than a half-updated one.
MAX_DROPPED = 15
MAX_DROPPED_SHARE = 0.02   # with ~1,900 names, allow up to 2% (38) before refusing


def yahoo_symbol(sym):
    """Yahoo writes class shares with a hyphen (BRK-B, MOG-A). The app keeps the dot."""
    return sym.replace(".", "-")


def add_months(d, months):
    """Calendar shift that clamps the day, so March 31 minus one month is February 28."""
    m0 = d.month - 1 + months
    year = d.year + m0 // 12
    month = m0 % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def on_or_before(bars, target):
    """Latest bar on or before target. bars are sorted by date ascending."""
    chosen = None
    for bar in bars:
        if bar[0] <= target:
            chosen = bar
        else:
            break
    return chosen


def anchor_targets(session_dates, as_of):
    """The nine dates every symbol is measured against. session_dates is the
    SPY calendar, ascending, and includes as_of as its last date."""
    if not session_dates or session_dates[-1] != as_of:
        raise ValueError("session calendar must end on as_of")
    if len(session_dates) < 4:
        raise ValueError("need at least four sessions to build anchors")

    def latest(target):
        return on_or_before([(d, None, None) for d in session_dates], target)[0]

    return {
        "d1": session_dates[-2],
        "d3": session_dates[-4],
        "d7": latest(as_of - datetime.timedelta(days=7)),
        "m1": latest(add_months(as_of, -1)),
        "m3": latest(add_months(as_of, -3)),
        "m6": latest(add_months(as_of, -6)),
        "ytd": latest(datetime.date(as_of.year - 1, 12, 31)),
        "y1": latest(add_months(as_of, -12)),
    }


def round_px(price):
    if price >= 1:
        return round(price + 0.0, 2)
    return round(price + 0.0, 6)


def period_return(last_adj, prev_adj):
    if not prev_adj or prev_adj <= 0 or not last_adj or last_adj <= 0:
        return 0.0
    return round((last_adj / prev_adj - 1.0) * 100.0, 2)


def returns_for(bars, as_of, targets):
    """bars: list of (date, close, adj) ascending. Price is the unadjusted
    close a student would have paid. The percentages use adjusted closes so
    a split or a dividend is not a fake gain."""
    last = on_or_before(bars, as_of)
    if last is None or last[1] <= 0:
        return None
    out = {}
    for key, target in targets.items():
        prev = on_or_before(bars, target)
        out[key] = period_return(last[2], prev[2] if prev else None)
    return round_px(last[1]), out


def completed_bars(payload, now):
    """Daily bars with the still-forming session removed.

    Yahoo's timestamp for a daily bar is that session's open. Returns a list
    of (date, close, adjclose) in the exchange's own calendar.
    """
    chart = payload.get("chart") or {}
    result = (chart.get("result") or [None])[0]
    if not result:
        err = (chart.get("error") or {}).get("description") or "no result"
        raise ValueError(err)
    meta = result.get("meta") or {}
    offset = meta.get("gmtoffset") or 0
    tz = datetime.timezone(datetime.timedelta(seconds=int(offset)))
    regular = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
    session_start = regular.get("start")
    session_end = regular.get("end")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    adj_block = ((result.get("indicators") or {}).get("adjclose") or [{}])[0]
    adjusted = adj_block.get("adjclose") or []
    bars = []
    for i, stamp in enumerate(timestamps):
        if session_end and session_start and now < session_end and stamp >= session_start:
            continue
        if i >= len(closes) or closes[i] is None:
            continue
        close = float(closes[i])
        if close <= 0:
            continue
        adj = close
        if i < len(adjusted) and adjusted[i] is not None and float(adjusted[i]) > 0:
            adj = float(adjusted[i])
        day = datetime.datetime.fromtimestamp(stamp, tz).date()
        bars.append((day, close, adj))
    # Yahoo sometimes publishes a finished session's daily bar late, or with a null
    # close, for hours after the bell (on 2026-11-10 the evening run still saw 11-09 as
    # the last bar). The chart meta already carries that session's official close, so
    # when the meta is for a completed session newer than the last bar, use it.
    rmt = meta.get("regularMarketTime")
    rmp = meta.get("regularMarketPrice")
    if rmt and rmp and float(rmp) > 0:
        forming = bool(session_start and session_end and now < session_end and rmt >= session_start)
        rday = datetime.datetime.fromtimestamp(rmt, tz).date()
        if not forming and (not bars or rday > bars[-1][0]):
            bars.append((rday, float(rmp), float(rmp)))
    if not bars:
        raise ValueError("no completed bars")
    return bars


def fetch_chart(sym, now=None, attempts=4):
    url = CHART.format(sym=urllib.request.quote(yahoo_symbol(sym)))
    delay = 1.0
    last_err = None
    for _ in range(attempts):
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=25) as res:
                payload = json.loads(res.read().decode("utf-8"))
            bars = completed_bars(payload, time.time() if now is None else now)
            # One last print is still a price. A name that stopped trading
            # (Yahoo sometimes keeps a single bar) stays in the portfolio at
            # that last trade instead of vanishing. Period returns come out 0
            # when there is no earlier bar to compare with.
            return bars
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("%s: %s" % (sym, last_err))


def universe(previous):
    """symbols.json first (the curated list), then anything only in the old file."""
    out, seen = [], set()
    try:
        with open(SYMBOLS_PATH) as fh:
            listed = json.load(fh)
    except FileNotFoundError:
        listed = []
    for x in listed + previous["items"]:
        s = x.get("s")
        if not s or s in seen:
            continue
        seen.add(s)
        out.append({"s": s, "n": x.get("n") or s, "t": x.get("t") or "stock",
                    "g": x.get("g") or "Stocks", "f": x.get("f", 0)})
    out.sort(key=lambda x: x["s"])
    return out


def build_prices(previous, spy_bars, series, now_date, names=None):
    as_of = spy_bars[-1][0]
    targets = anchor_targets([b[0] for b in spy_bars], as_of)
    items = []
    dropped = []
    for old in (names if names is not None else previous["items"]):
        bars = series.get(old["s"])
        if not bars:
            dropped.append(old["s"])
            continue
        priced = returns_for(bars, as_of, targets)
        if priced is None:
            dropped.append(old["s"])
            continue
        price, rets = priced
        items.append({
            "s": old["s"],
            "n": old["n"],
            "t": old["t"],
            "g": old["g"],
            "p": price,
            "r": rets,
            "f": old.get("f", 0),
        })
    allowed = max(MAX_DROPPED, int(MAX_DROPPED_SHARE * (len(items) + len(dropped))))
    if len(dropped) > allowed or len(items) < 100:
        raise SystemExit(
            "refresh aborted: %d symbols failed (%s). prices.json was not changed."
            % (len(dropped), ", ".join(dropped[:20]))
        )
    return {
        "asOf": as_of.isoformat(),
        "built": now_date.isoformat(),
        "items": items,
    }, dropped


def load_previous():
    with open(PRICES_PATH) as fh:
        data = json.load(fh)
    if not isinstance(data.get("items"), list) or len(data["items"]) < 100:
        raise SystemExit("prices.json does not look like the price universe")
    return data


def write_prices(data):
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    tmp = PRICES_PATH + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.write("\n")
    os.replace(tmp, PRICES_PATH)


def refresh():
    previous = load_previous()
    names = universe(previous)
    symbols = [item["s"] for item in names]
    if "SPY" not in symbols:
        raise SystemExit("SPY is missing from prices.json; it sets the session calendar")
    print("fetching %d symbols from Yahoo" % len(symbols), file=sys.stderr)
    series = {}
    errors = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_chart, sym): sym for sym in symbols}
        done = 0
        for fut in as_completed(futures):
            sym = futures[fut]
            done += 1
            try:
                series[sym] = fut.result()
            except Exception as err:
                errors.append(str(err))
            if done % 100 == 0:
                print("  %d/%d" % (done, len(symbols)), file=sys.stderr)
    if "SPY" not in series:
        raise SystemExit("SPY failed (%s); prices.json was not changed" % "; ".join(errors[:5]))
    data, dropped = build_prices(previous, series["SPY"], series,
                                 datetime.datetime.now(datetime.timezone.utc).date(), names)
    write_prices(data)
    print("asOf %s  symbols %d  dropped %d" % (data["asOf"], len(data["items"]), len(dropped)))
    if dropped:
        print("dropped: " + ", ".join(dropped))
    if errors and not dropped:
        print("retries recovered from %d errors" % len(errors), file=sys.stderr)
    aapl = next((item for item in data["items"] if item["s"] == "AAPL"), None)
    if aapl:
        print("AAPL %s  1-day %s%%" % (aapl["p"], aapl["r"]["d1"]))


def self_test():
    # Weekdays ending 2026-11-06 (a Friday), long enough to cover a one-year anchor.
    dates = []
    day = datetime.date(2025, 1, 2)
    while day <= datetime.date(2026, 11, 6):
        if day.weekday() < 5:
            dates.append(day)
        day += datetime.timedelta(days=1)
    as_of = dates[-1]
    targets = anchor_targets(dates, as_of)
    assert targets["d1"] == dates[-2], targets
    assert targets["d3"] == dates[-4], targets
    assert targets["d7"] <= as_of - datetime.timedelta(days=7)
    assert add_months(datetime.date(2026, 3, 31), -1) == datetime.date(2026, 2, 28)
    assert add_months(datetime.date(2026, 1, 31), -1) == datetime.date(2025, 12, 31)

    bars = [(d, 100.0, 100.0) for d in dates]
    bars[-1] = (dates[-1], 110.0, 110.0)
    price, rets = returns_for(bars, as_of, targets)
    assert price == 110.0, price
    assert rets["d1"] == 10.0, rets
    assert set(rets) == {"d1", "d3", "d7", "m1", "m3", "m6", "ytd", "y1"}

    # A bar timestamped at today's open is dropped while the session is open.
    today_open = 1_700_000_000
    payload = {
        "chart": {
            "result": [{
                "meta": {
                    "gmtoffset": -18000,
                    "currentTradingPeriod": {
                        "regular": {"start": today_open, "end": today_open + 6 * 3600}
                    },
                },
                "timestamp": [today_open - 86400, today_open],
                "indicators": {
                    "quote": [{"close": [10.0, 11.0]}],
                    "adjclose": [{"adjclose": [10.0, 11.0]}],
                },
            }]
        }
    }
    kept = completed_bars(payload, now=today_open + 60)
    assert len(kept) == 1 and kept[0][1] == 10.0, kept
    both = completed_bars(payload, now=today_open + 7 * 3600)
    assert len(both) == 2 and both[-1][1] == 11.0, both

    # A finished session missing from the bars is taken from the meta's close...
    late = json.loads(json.dumps(payload))
    late["chart"]["result"][0]["timestamp"] = [today_open - 86400]
    late["chart"]["result"][0]["indicators"] = {"quote": [{"close": [10.0]}], "adjclose": [{"adjclose": [10.0]}]}
    late["chart"]["result"][0]["meta"].update({"regularMarketTime": today_open + 6 * 3600 + 1,
                                               "regularMarketPrice": 12.5})
    got = completed_bars(late, now=today_open + 9 * 3600)
    assert len(got) == 2 and got[-1][1] == 12.5, got
    # ...but never while that session is still trading.
    late["chart"]["result"][0]["meta"]["regularMarketTime"] = today_open + 3600
    got = completed_bars(late, now=today_open + 2 * 3600)
    assert len(got) == 1 and got[-1][1] == 10.0, got
    print("self-test ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        self_test()
    else:
        refresh()
