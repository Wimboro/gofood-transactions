# GoFood transaction poller

Polls the same transaction list as [the merchant portal today view](https://portal.gofoodmerchant.co.id/transactions?date_range=today) and stores each row in a local SQLite file. It does not log in. It sends the saved access token, and calls the portal's refresh endpoint only when that token is rejected.

## Setup

Python 3.11 or newer. No packages to install.

```bash
cp config.example.json config.json
```

In the browser, open the portal, then DevTools → Application → Cookies → `https://portal.gofoodmerchant.co.id`. Copy `access_token` and `refresh_token` into `config.json`.

```bash
python -m gofood_poller --once
python -m gofood_poller
```

The repeating command waits `poll_seconds` between cycles (default 5). Tokens refreshed by the app are stored in `data/transactions.db` and reused on the next start. After you log in again in the browser, paste the new cookies and run once with `--import-tokens`.

`merchant_ids` can stay empty. Set it when you want one outlet. `date_range` is `today` or `yesterday`, using `timezone` (default `Asia/Jakarta`).

## Speaker

Leave `speaker_url` empty to store rows only. To speak a payment on the QRIS speaker, set `speaker_url` to the speaker backend origin (no trailing slash) and `speaker_token` to that backend's `DEVICE_TOKEN`.

```json
"speaker_url": "http://127.0.0.1:3000",
"speaker_token": "the device token"
```

The poller posts each new paid QRIS or GoPay row to `/gofood/notification`. GoFood stores the amount in sen, so 2300 is spoken as Rp 23. Delivery and pickup orders stay silent (`goresto_online`, `goresto_online_pickup`, `go_food_meal_plan`, `gomart_online`, `go_food_affordable`). Refunds are not spoken. The first poll after `speaker_url` is set marks the rows already in the database and does not announce them. A speaker that is down is retried on the next cycle and does not stop polling.

## Rate limits

One list request per cycle, first page only, unless `max_pages_per_poll` is higher. Extra pages wait one second and stop once the page is already in the database. A 429 waits and backs off instead of retrying immediately. Three forbidden responses in a row stop the process. `poll_seconds` cannot go below 5.
