# classets-prices

This repository publishes `prices.json` for the Classets classroom simulator.

The file is one daily close, not live ticks. Each price is the latest completed US trading session. The simulator uses that single price for the whole day.

The public address is [https://prices.bigyetigames.com/prices.json](https://prices.bigyetigames.com/prices.json). Until the DNS record for that name exists, the same file is at [https://baswolf.github.io/classets-prices/prices.json](https://baswolf.github.io/classets-prices/prices.json).

A GitHub Action updates the file on weekdays at 21:30 UTC, after the New York close. The workflow can also be run by hand. It reads the symbol list from `symbols.json` (plus anything already in `prices.json`), asks Yahoo Finance for completed daily bars, and commits a new `prices.json` at the repository root.

`symbols.json` holds about 1,900 names: the original 723, the S&P 500, S&P MidCap 400 and S&P SmallCap 600, and a curated list of popular ETFs. Each entry is `{s, n, t, g, f}` (symbol, name, type, group, featured). Class shares keep the dot (`BRK.B`); the script asks Yahoo for `BRK-B`. A full run makes one Yahoo request per symbol, about 40 seconds with 8 workers.

Live intraday quotes for the app come from a separate Cloudflare Worker (`classets-live`), not from this file.

`src/refresh_prices.py` is that job. `python3 src/refresh_prices.py --check` tests the date math without using the network.

## news.json (company filings)

The same workflow also writes `news.json`, served at [https://prices.bigyetigames.com/news.json](https://prices.bigyetigames.com/news.json). The Classets app uses it only in the detailed end-of-day recap, to show each stock's recent company filings with links and to help explain a move. Older app versions never request it.

The only source is [SEC EDGAR](https://www.sec.gov/edgar), which is free and needs no key. The SEC says "All Government-created content on sec.gov and EDGAR public filing content are free to access and reuse" and asks automated tools to stay under 10 requests per second and to send a User-Agent with a name and contact email ([SEC webmaster FAQ: accessing EDGAR data](https://www.sec.gov/os/accessing-edgar-data)). The script waits 0.25 s between requests (about 4 per second), backs off on 403/429, and stops after 600 companies or 7 minutes.

How it works: `src/refresh_news.py` maps the stocks in `prices.json` to SEC CIK numbers (`company_tickers.json`), finds companies that filed in the last 3 days (EDGAR daily form indexes plus the "latest filings" feed), then reads each of those companies' filing list (`data.sec.gov/submissions`). It keeps 8-K, 10-Q, 10-K, 6-K, 20-F, 40-F, 13D, tender-offer and merger (425) filings. Titles are written from the form type and 8-K item numbers, for example "NVIDIA reported quarterly or annual results" for an 8-K item 2.02; they are not news headlines and say nothing about whether results were good or bad.

Format: `{asOf, built, source, days, since, note, items: {SYMBOL: [{t, k, c, src, d, at, u}]}}` with up to 3 filings per symbol (title, form/item, category, source, filing date, acceptance time, link to the filing on sec.gov).

The workflow sends `Classets prices headyeti@bigyetigames.com` as the SEC User-Agent. The repository variable `SEC_USER_AGENT` overrides that contact when it is set (a name and email, for example `Classets prices you@example.com`). A local run still skips itself when `SEC_USER_AGENT` is unset or has no email, and does not change `news.json`. The step is fail-soft: on any error it keeps the previous `news.json`, exits 0, and `prices.json` is committed as usual. A run takes about 5 minutes, well inside the free GitHub Actions allowance for public repositories.

`python3 src/refresh_news.py --check` tests the parsers without using the network.
