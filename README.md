# classets-prices

This repository publishes `prices.json` for the Classets classroom simulator.

The file is one daily close, not live ticks. Each price is the latest completed US trading session. The simulator uses that single price for the whole day.

The public address is [https://prices.bigyetigames.com/prices.json](https://prices.bigyetigames.com/prices.json). Until the DNS record for that name exists, the same file is at [https://baswolf.github.io/classets-prices/prices.json](https://baswolf.github.io/classets-prices/prices.json).

A GitHub Action updates the file on weekdays at 21:30 UTC, after the New York close. The workflow can also be run by hand. It reads the symbol list already in `prices.json`, asks Yahoo Finance for completed daily bars, and commits a new `prices.json` at the repository root.

`src/refresh_prices.py` is that job. `python3 src/refresh_prices.py --check` tests the date math without using the network.
