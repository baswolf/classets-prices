#!/usr/bin/env python3
"""Build news.json: recent SEC filings for the companies in prices.json.

The Classets end-of-day recap explains a stock's move from prices, and when there is
a real reason on record it can point to it. The free, no-key, reuse-permitted source
for that is SEC EDGAR: every US-listed company has to file an 8-K within days of
earnings, a merger, a leadership change, layoffs and other material events, and files
its 10-Q and 10-K reports there. EDGAR content is public and free to reuse; see
https://www.sec.gov/about/webmaster-frequently-asked-questions.

SEC fair-access rules (https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data):
declare who you are in the User-Agent (a name and a contact email) and stay under
10 requests per second. This script sends SEC_USER_AGENT and paces itself to about
4 requests per second. A run makes roughly 15 index requests plus one per company
that actually filed something in the window (typically 100-300).

No headline text from news sites is used: their RSS terms (Google News, Yahoo) only
allow personal, non-commercial reading, not republishing.

Output (separate from prices.json so app 1.1, which reads only prices.json, is untouched):
    {"asOf": "2026-11-13", "built": "...", "source": "SEC EDGAR", "days": 3,
     "items": {"AAPL": [{"t": "...", "k": "8-K 2.02", "c": "earnings",
                         "src": "SEC EDGAR", "d": "2026-11-13",
                         "at": "2026-11-13T21:05:00Z", "u": "https://www.sec.gov/..."}]}}
Up to three filings per symbol, newest first, from the last three US business days.

The job is fail-soft: any problem leaves the previous news.json in place and exits 0,
so prices.json still publishes.

    SEC_USER_AGENT="Classets (Big Yeti Games) you@example.com" python3 src/refresh_news.py
    python3 src/refresh_news.py --check      # parser tests, no network
"""
import datetime
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICES_PATH = os.path.join(ROOT, "prices.json")
NEWS_PATH = os.path.join(ROOT, "news.json")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DAILY_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
CURRENT_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}"
               "&company=&dateb=&owner=include&start={start}&count=100&output=atom")
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accd}-index.htm"

DAYS = 3                 # US business days of filings to keep
PER_SYMBOL = 3
MIN_INTERVAL = 0.25      # seconds between SEC requests (about 4 per second)
MAX_COMPANIES = 600      # submissions requests per run, at most
MAX_SECONDS = 420        # stop asking the SEC after 7 minutes and publish what we have

FORMS = {"8-K", "10-Q", "10-K", "6-K", "20-F", "40-F", "SCHEDULE 13D", "SC 13D", "SC TO-T", "425"}
CURRENT_FORMS = ["8-K", "10-Q", "10-K", "6-K"]

# 8-K items, most important first. Wording is plain English for students.
ITEMS = [
    ("1.03", "disclosed a bankruptcy or receivership", "legal"),
    ("4.02", "said some past financial statements can no longer be relied on", "accounting"),
    ("2.02", "reported quarterly or annual results", "earnings"),
    ("2.01", "completed an acquisition or a sale of a business", "deal"),
    ("5.01", "reported a change in who controls the company", "deal"),
    ("1.05", "disclosed a cybersecurity incident", "risk"),
    ("2.05", "announced restructuring or layoff costs", "restructuring"),
    ("2.06", "reported a write-down of some assets", "accounting"),
    ("3.01", "received a stock exchange listing notice", "listing"),
    ("1.01", "signed a major agreement", "deal"),
    ("5.02", "announced a change among its executives or directors", "leadership"),
    ("2.03", "took on a major new debt or obligation", "financing"),
    ("3.02", "sold new shares that were not publicly offered", "financing"),
    ("7.01", "shared new information with investors", "update"),
    ("8.01", "reported other company news", "update"),
    ("1.02", "ended a major agreement", "deal"),
    ("4.01", "changed its auditor", "accounting"),
    ("5.07", "reported the results of a shareholder vote", "governance"),
    ("5.03", "changed its bylaws or fiscal year", "governance"),
    ("3.03", "changed the rights of its shareholders", "governance"),
]
ITEM_RANK = {code: i for i, (code, _, _) in enumerate(ITEMS)}
ITEM_TEXT = {code: (text, cat) for code, text, cat in ITEMS}
FORM_TEXT = {
    "10-Q": ("filed its quarterly report", "earnings"),
    "10-K": ("filed its annual report", "earnings"),
    "6-K": ("filed a report as a foreign company", "update"),
    "20-F": ("filed its annual report as a foreign company", "earnings"),
    "40-F": ("filed its annual report as a Canadian company", "earnings"),
    "SCHEDULE 13D": ("was the subject of a filing by an investor owning more than 5% who may seek changes", "ownership"),
    "SC 13D": ("was the subject of a filing by an investor owning more than 5% who may seek changes", "ownership"),
    "SC TO-T": ("received a tender offer for its shares", "deal"),
    "425": ("filed communications about a merger", "deal"),
}


class Sec:
    def __init__(self, agent):
        self.agent = agent
        self.last = 0.0
        self.count = 0
        self.started = time.time()

    def out_of_time(self):
        return time.time() - self.started > MAX_SECONDS

    def get(self, url, attempts=3):
        delay = 2.0
        err = None
        for _ in range(attempts):
            wait = self.last + MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.count += 1
            req = urllib.request.Request(url, headers={"User-Agent": self.agent,
                                                       "Accept-Encoding": "gzip, deflate"})
            try:
                with urllib.request.urlopen(req, timeout=45) as res:
                    body = res.read()
                    if res.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return body.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                err = e
                if e.code == 404:
                    return None
                if e.code in (403, 429):
                    # Over the limit or not recognised: back off hard, never hammer.
                    time.sleep(10)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                err = e
            time.sleep(delay)
            delay *= 2
        raise RuntimeError("%s: %s" % (url, err))


def business_days_back(today, n):
    """The n most recent weekdays up to and including today, newest first."""
    out, d = [], today
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return out


def parse_daily_index(text):
    """(form, cik, yyyy-mm-dd) rows from a form.YYYYMMDD.idx file."""
    rows = []
    body = text.split("\n")
    start = 0
    for i, line in enumerate(body):
        if line.startswith("----"):
            start = i + 1
            break
    pat = re.compile(r"^(\S.*?)\s{2,}.*?\s(\d{1,10})\s+(\d{8}|\d{4}-\d\d-\d\d)\s+edgar/\S+\s*$")
    for line in body[start:]:
        m = pat.match(line)
        if not m:
            continue
        form = m.group(1).strip()
        d = m.group(3)
        if len(d) == 8:
            d = "%s-%s-%s" % (d[:4], d[4:6], d[6:])
        rows.append((form, int(m.group(2)), d))
    return rows


def parse_current_atom(text):
    """(form, cik, yyyy-mm-dd) rows from EDGAR's latest-filings Atom feed."""
    rows = []
    for entry in re.findall(r"<entry>(.*?)</entry>", text, re.S):
        t = re.search(r"<title>(.*?) - .*?\((\d{10})\)", entry, re.S)
        d = re.search(r"Filed:&lt;/b&gt;\s*(\d{4}-\d\d-\d\d)", entry)
        if t and d:
            rows.append((html.unescape(t.group(1)).strip(), int(t.group(2)), d.group(1)))
    return rows


def headline(name, form, items):
    """Plain-English title, a short kind code and a category."""
    if form.startswith("8-K"):
        codes = sorted([c for c in items if c in ITEM_TEXT], key=lambda c: ITEM_RANK[c])
        if not codes:
            return name + " filed a current report", "8-K", "update"
        parts = [ITEM_TEXT[c][0] for c in codes[:2]]
        return (name + " " + " and ".join(parts), "8-K " + ", ".join(codes[:2]), ITEM_TEXT[codes[0]][1])
    text, cat = FORM_TEXT.get(form, ("filed a " + form, "update"))
    return name + " " + text, form, cat


def short_name(n):
    n = re.sub(r",?\s+(Inc\.?|Corp\.?|Corporation|Co\.?|Ltd\.?|plc|PLC|N\.V\.|S\.A\.|Holdings?|Group|Company|Incorporated|Class [A-Z])$", "", n.strip())
    return re.sub(r",?\s+(Inc\.?|Corp\.?|Corporation|Ltd\.?|plc)$", "", n).strip() or n


def filings_for(sub, cik, since, until):
    r = (sub.get("filings") or {}).get("recent") or {}
    forms = r.get("form") or []
    out = []
    for i, form in enumerate(forms):
        if form not in FORMS:
            continue
        d = (r.get("filingDate") or [""])[i]
        if not d or d < since or d > until:
            continue
        acc = (r.get("accessionNumber") or [""])[i]
        doc = (r.get("primaryDocument") or [""])[i]
        items = [x.strip() for x in ((r.get("items") or [""])[i] or "").split(",") if x.strip()]
        at = (r.get("acceptanceDateTime") or [""])[i]
        accd = acc.replace("-", "")
        url = (ARCHIVE_URL.format(cik=cik, acc=accd, doc=doc) if doc and "/" not in doc
               else INDEX_URL.format(cik=cik, acc=accd, accd=acc))
        out.append({"form": form, "d": d, "at": at[:19] + "Z" if at else "", "items": items, "u": url})
    out.sort(key=lambda x: (x["at"] or x["d"]), reverse=True)
    return out


def build(sec, prices, today):
    days = business_days_back(today, DAYS)
    since, until = days[-1].isoformat(), today.isoformat()
    names = {x["s"]: x.get("n") or x["s"] for x in prices["items"] if x.get("t") == "stock"}
    tick = json.loads(sec.get(TICKERS_URL))
    cik_of = {}
    for row in tick.values():
        t = str(row.get("ticker", "")).upper().replace("-", ".")
        if t in names and t not in cik_of:
            cik_of[t] = int(row["cik_str"])
    syms_of = {}
    for s, c in cik_of.items():
        syms_of.setdefault(c, []).append(s)
    print("matched %d of %d stocks to SEC CIKs" % (len(cik_of), len(names)), file=sys.stderr)

    candidates = set()
    # Finished days: the daily form index (published after about 10 p.m. ET).
    for d in days:
        if d >= today:
            continue
        url = DAILY_URL.format(y=d.year, q=(d.month - 1) // 3 + 1, ymd=d.strftime("%Y%m%d"))
        try:
            text = sec.get(url)
        except RuntimeError as e:
            print("daily index skipped: %s" % e, file=sys.stderr)
            continue
        for form, cik, fd in parse_daily_index(text or ""):
            if form in FORMS and cik in syms_of:
                candidates.add(cik)
    # Today (and anything the index has not caught up with): the latest-filings feed.
    for form in CURRENT_FORMS:
        for page in range(8 if form == "8-K" else 3):
            try:
                text = sec.get(CURRENT_URL.format(form=urllib.request.quote(form), start=page * 100))
            except RuntimeError as e:
                print("latest filings skipped: %s" % e, file=sys.stderr)
                break
            rows = parse_current_atom(text or "")
            for f, cik, fd in rows:
                if fd >= since and cik in syms_of:
                    candidates.add(cik)
            if len(rows) < 100 or (rows and min(r[2] for r in rows) < since):
                break
    print("%d companies filed in %s..%s" % (len(candidates), since, until), file=sys.stderr)

    items = {}
    for n, cik in enumerate(sorted(candidates)):
        if n >= MAX_COMPANIES or sec.out_of_time():
            print("stopped early after %d companies" % n, file=sys.stderr)
            break
        try:
            raw = sec.get(SUBMISSIONS_URL.format(cik=cik))
        except RuntimeError as e:
            print("skip CIK %d: %s" % (cik, e), file=sys.stderr)
            continue
        if not raw:
            continue
        try:
            sub = json.loads(raw)
        except ValueError:
            continue
        found = filings_for(sub, cik, since, until)
        if not found:
            continue
        for sym in syms_of[cik]:
            nm = short_name(names[sym])
            lst = []
            for f in found:
                t, k, c = headline(nm, f["form"], f["items"])
                lst.append({"t": t, "k": k, "c": c, "src": "SEC EDGAR", "d": f["d"], "at": f["at"], "u": f["u"]})
                if len(lst) >= PER_SYMBOL:
                    break
            items[sym] = lst
    return {"asOf": prices.get("asOf", ""), "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "SEC EDGAR", "days": DAYS, "since": since,
            "note": "Company filings from SEC EDGAR, not news articles. Titles are written by Classets from each filing's form type and 8-K item numbers.",
            "items": items}


def write(data):
    tmp = NEWS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
        fh.write("\n")
    os.replace(tmp, NEWS_PATH)


def main():
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in agent:
        print("news skipped: set SEC_USER_AGENT to a name and contact email (SEC fair-access rule)."
              " news.json was not changed.")
        return
    try:
        with open(PRICES_PATH) as fh:
            prices = json.load(fh)
        ny = datetime.timezone(datetime.timedelta(hours=-5))
        today = datetime.datetime.now(ny).date()
        sec = Sec(agent)
        data = build(sec, prices, today)
        if not data["items"] and os.path.exists(NEWS_PATH):
            print("no filings found; keeping the previous news.json")
            return
        write(data)
        print("news: %d symbols with filings, %d SEC requests" % (len(data["items"]), sec.count))
    except Exception as e:     # fail-soft: prices must still publish
        print("news skipped (%s); news.json was not changed." % e)


def self_test():
    idx = ("Form Type   Company Name                                                  CIK         Date Filed  File Name\n"
           "---------------------------------------------------------------------------------------------------------------\n"
           "10-Q        APPLE INC                                                     320193      20261113    edgar/data/320193/0000320193-26-000030.txt\n"
           "8-K         NVIDIA CORP                                                   1045810     20261113    edgar/data/1045810/0001045810-26-000101.txt\n"
           "SCHEDULE 13D  SOME CO                                                     12345       20261113    edgar/data/12345/0000012345-26-000001.txt\n")
    rows = parse_daily_index(idx)
    assert rows[0] == ("10-Q", 320193, "2026-11-13"), rows
    assert rows[1] == ("8-K", 1045810, "2026-11-13"), rows
    assert rows[2][0] == "SCHEDULE 13D", rows
    atom = ("<entry><title>8-K - Tempest Therapeutics, Inc. (0001544227) (Filer)</title>"
            "<summary type=\"html\"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-13 &lt;b&gt;AccNo:&lt;/b&gt; x</summary></entry>")
    assert parse_current_atom(atom) == [("8-K", 1544227, "2026-11-13")]
    t, k, c = headline("Apple", "8-K", ["9.01", "7.01", "2.02"])
    assert t == "Apple reported quarterly or annual results and shared new information with investors", t
    assert k == "8-K 2.02, 7.01" and c == "earnings", (k, c)
    assert headline("Apple", "10-K", [])[0] == "Apple filed its annual report"
    assert headline("X", "8-K", ["9.01"])[1] == "8-K"
    assert short_name("Apple Inc.") == "Apple" and short_name("NVIDIA Corp.") == "NVIDIA"
    assert short_name("Berkshire Hathaway Inc. Class B") == "Berkshire Hathaway", short_name("Berkshire Hathaway Inc. Class B")
    d = business_days_back(datetime.date(2026, 11, 16), 3)   # a Monday
    assert d == [datetime.date(2026, 11, 16), datetime.date(2026, 11, 13), datetime.date(2026, 11, 12)], d
    sub = {"filings": {"recent": {"form": ["4", "8-K", "10-Q"], "filingDate": ["2026-11-13", "2026-11-13", "2026-10-01"],
           "accessionNumber": ["a", "0000320193-26-000031", "c"], "primaryDocument": ["x", "aapl-8k.htm", "z"],
           "items": ["", "2.02,9.01", ""], "acceptanceDateTime": ["", "2026-11-13T21:05:00.000Z", ""]}}}
    f = filings_for(sub, 320193, "2026-11-11", "2026-11-13")
    assert len(f) == 1 and f[0]["u"] == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000031/aapl-8k.htm", f
    assert f[0]["items"] == ["2.02", "9.01"] and f[0]["at"] == "2026-11-13T21:05:00Z"
    print("news self-test ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        self_test()
    else:
        main()
