import argparse
import json
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

GOFOOD_HOST = "https://api.gojekapi.com"
MARS_HOST = "https://api.gobiz.co.id"
TRANSACTIONS_PATH = "/merchant-analytics/v2/merchants/transactions"
TOKEN_PATH = "/goid/token"
CLIENT_ID = "go-biz-web-new"
APP_ID = "go-biz-web-dashboard"
MIN_POLL_SECONDS = 5
MAX_BACKOFF_SECONDS = 300
FOOD_SOURCES = {
    "goresto_online",
    "goresto_online_pickup",
    "go_food_meal_plan",
    "gomart_online",
    "go_food_affordable",
}


@dataclass
class Settings:
    poll_seconds: float
    page_size: int
    max_pages_per_poll: int
    timezone: str
    date_range: str
    merchant_ids: list[str]
    statuses: list[str]
    payment_types: list[str]
    database: Path
    access_token: str
    refresh_token: str
    speaker_url: str
    speaker_token: str


def load_settings(path: Path) -> Settings:
    if not path.exists():
        example = Path("config.example.json")
        if example.exists():
            path.write_text(example.read_text())
        raise SystemExit(
            f"Created {path}. Paste access_token and refresh_token from the "
            "portal cookies, then run again."
        )
    raw = json.loads(path.read_text())
    poll_seconds = float(raw.get("poll_seconds", MIN_POLL_SECONDS))
    if poll_seconds < MIN_POLL_SECONDS:
        print(
            f"poll_seconds {poll_seconds} is below {MIN_POLL_SECONDS}; using {MIN_POLL_SECONDS}",
            file=sys.stderr,
        )
        poll_seconds = MIN_POLL_SECONDS
    return Settings(
        poll_seconds=poll_seconds,
        page_size=max(1, int(raw.get("page_size", 20))),
        max_pages_per_poll=max(1, int(raw.get("max_pages_per_poll", 1))),
        timezone=str(raw.get("timezone", "Asia/Jakarta")),
        date_range=str(raw.get("date_range", "today")),
        merchant_ids=[str(item) for item in raw.get("merchant_ids", []) if str(item).strip()],
        statuses=[str(item).upper() for item in raw.get("statuses", [])],
        payment_types=[str(item).upper() for item in raw.get("payment_types", [])],
        database=Path(raw.get("database", "data/transactions.db")),
        access_token=str(raw.get("access_token") or ""),
        refresh_token=str(raw.get("refresh_token") or ""),
        speaker_url=str(raw.get("speaker_url") or "").rstrip("/"),
        speaker_token=str(raw.get("speaker_token") or ""),
    )


def js_iso(moment: datetime) -> str:
    utc = moment.astimezone(timezone.utc)
    millis = utc.microsecond // 1000
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def day_window(timezone_name: str, date_range: str) -> tuple[str, str]:
    tz = ZoneInfo(timezone_name)
    today = datetime.now(tz).date()
    if date_range == "yesterday":
        day = today - timedelta(days=1)
    elif date_range == "today":
        day = today
    else:
        raise SystemExit('date_range must be "today" or "yesterday"')
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return js_iso(start), js_iso(end)


# encodeURIComponent, then the portal puts commas and colons back.
_QUERY_SAFE = "-_.!~*'():,"


def encode_query(pairs: list[tuple[str, str]]) -> str:
    parts = []
    for key, value in pairs:
        parts.append(f"{quote(key, safe=_QUERY_SAFE)}={quote(str(value), safe=_QUERY_SAFE)}")
    return "&".join(parts)


def transaction_query(settings: Settings, page: int) -> str:
    start_time, end_time = day_window(settings.timezone, settings.date_range)
    pairs: list[tuple[str, str]] = [
        ("from", str(page * settings.page_size)),
        ("size", str(settings.page_size)),
        ("start_time", start_time),
        ("end_time", end_time),
    ]
    if settings.statuses:
        pairs.append(("statuses", ",".join(settings.statuses)))
    if settings.payment_types:
        pairs.append(("payment_types", ",".join(settings.payment_types)))
    if settings.merchant_ids:
        pairs.append(("merchant_ids", ",".join(settings.merchant_ids)))
    return encode_query(pairs)


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transactions (
          id TEXT PRIMARY KEY,
          merchant_id TEXT,
          order_id TEXT,
          transaction_time TEXT,
          currency TEXT,
          gross_amount TEXT,
          payment_type TEXT,
          transaction_status TEXT,
          transaction_source TEXT,
          raw_json TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          notified_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_transactions_time
          ON transactions(transaction_time);
        CREATE TABLE IF NOT EXISTS app_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          access_token TEXT NOT NULL,
          refresh_token TEXT NOT NULL,
          device_id TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
    if "notified_at" not in columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN notified_at TEXT")
    return conn


def load_session(conn: sqlite3.Connection, settings: Settings, import_tokens: bool) -> dict:
    row = conn.execute("SELECT * FROM session WHERE id = 1").fetchone()
    now = datetime.now(timezone.utc).isoformat()
    if row and not import_tokens:
        return dict(row)
    access = settings.access_token.strip()
    refresh = settings.refresh_token.strip()
    if not access or not refresh:
        raise SystemExit(
            "No saved session. Put access_token and refresh_token in config.json, "
            "or pass --import-tokens after updating them."
        )
    device_id = row["device_id"] if row else str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO session (id, access_token, refresh_token, device_id, updated_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          access_token = excluded.access_token,
          refresh_token = excluded.refresh_token,
          updated_at = excluded.updated_at
        """,
        (access, refresh, device_id, now),
    )
    conn.commit()
    stored = conn.execute("SELECT * FROM session WHERE id = 1").fetchone()
    return dict(stored)


def save_tokens(conn: sqlite3.Connection, access_token: str, refresh_token: str) -> None:
    conn.execute(
        """
        UPDATE session
        SET access_token = ?, refresh_token = ?, updated_at = ?
        WHERE id = 1
        """,
        (access_token, refresh_token, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def upsert_transactions(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    ids = [
        str(row.get("wallstreet_transaction_id") or row.get("id") or "").strip()
        for row in rows
    ]
    existing = known_ids(conn, [tx_id for tx_id in ids if tx_id])
    inserted = 0
    updated = 0
    for row, tx_id in zip(rows, ids):
        if not tx_id:
            continue
        payload = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO transactions (
              id, merchant_id, order_id, transaction_time, currency, gross_amount,
              payment_type, transaction_status, transaction_source, raw_json,
              first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              merchant_id = excluded.merchant_id,
              order_id = excluded.order_id,
              transaction_time = excluded.transaction_time,
              currency = excluded.currency,
              gross_amount = excluded.gross_amount,
              payment_type = excluded.payment_type,
              transaction_status = excluded.transaction_status,
              transaction_source = excluded.transaction_source,
              raw_json = excluded.raw_json,
              updated_at = excluded.updated_at
            """,
            (
                tx_id,
                _text(row.get("merchant_id")),
                _text(row.get("order_id")),
                _text(row.get("transaction_time")),
                _text(row.get("currency")),
                _text(row.get("gross_amount")),
                _text(row.get("payment_type")),
                _text(row.get("transaction_status")),
                _text(row.get("transaction_source")),
                payload,
                now,
                now,
            ),
        )
        if tx_id in existing:
            updated += 1
        else:
            inserted += 1
            existing.add(tx_id)
    conn.commit()
    return inserted, updated


def _text(value) -> str | None:
    if value is None:
        return None
    return str(value)


def known_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM transactions WHERE id IN ({marks})",
        ids,
    ).fetchall()
    return {row["id"] for row in rows}


class ApiError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _request(url: str, method: str, headers: dict, body: bytes | None = None) -> tuple[int, dict, dict]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            status = response.status
            header_map = dict(response.headers.items())
    except HTTPError as error:
        raw = error.read()
        status = error.code
        header_map = dict(error.headers.items()) if error.headers else {}
    except URLError as error:
        raise ApiError(0, f"network error: {error.reason}") from error
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"data": payload}
    return status, payload, header_map


def portal_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Authentication-Type": "go-id",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def token_headers(device_id: str, timezone_name: str) -> dict:
    return {
        "Authentication-Type": "go-id",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-DeviceOS": "Web",
        "Accept-Language": "id",
        "X-User-Locale": "id-ID",
        "X-AppVersion": "-",
        "Gojek-Country-Code": "ID",
        "Gojek-Timezone": timezone_name,
        "X-Platform": "Web",
        "X-User-Type": "merchant",
        "x-appId": APP_ID,
        "x-uniqueid": device_id,
        "X-PhoneMake": "Linux",
        "X-PhoneModel": "Web",
    }


def error_message(payload: dict) -> str:
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("message") or first.get("code") or "request failed")
    return str(payload.get("message") or payload.get("code") or "request failed")


def extract_transactions(payload: dict) -> tuple[list[dict], int | None]:
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = body.get("transactions") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return [], None
    total = body.get("total") if isinstance(body, dict) else None
    return [row for row in rows if isinstance(row, dict)], total if isinstance(total, int) else None


def fetch_page(settings: Settings, session: dict, page: int) -> tuple[list[dict], int | None]:
    query = transaction_query(settings, page)
    url = f"{GOFOOD_HOST}{TRANSACTIONS_PATH}?{query}"
    status, payload, headers = _request(url, "GET", portal_headers(session["access_token"]))
    if status == 401:
        raise ApiError(401, "access token rejected")
    if status == 429:
        raise ApiError(429, "rate limited", _retry_after(headers))
    if status >= 400:
        raise ApiError(status, error_message(payload))
    return extract_transactions(payload)


def refresh_tokens(conn: sqlite3.Connection, session: dict, timezone_name: str) -> None:
    body = json.dumps(
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "data": {"refresh_token": session["refresh_token"]},
        }
    ).encode("utf-8")
    status, payload, headers = _request(
        f"{MARS_HOST}{TOKEN_PATH}",
        "POST",
        token_headers(session["device_id"], timezone_name),
        body,
    )
    if status == 429:
        raise ApiError(429, "token refresh rate limited", _retry_after(headers))
    token_body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    access = token_body.get("access_token") if isinstance(token_body, dict) else None
    refresh = token_body.get("refresh_token") if isinstance(token_body, dict) else None
    if status >= 400 or not access or not refresh:
        raise ApiError(status or 401, "refresh failed; paste new cookies into config.json and run with --import-tokens")
    session["access_token"] = str(access)
    session["refresh_token"] = str(refresh)
    save_tokens(conn, session["access_token"], session["refresh_token"])


def _retry_after(headers: dict) -> float | None:
    raw = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def should_announce(payment_type: str | None, transaction_source: str | None, transaction_status: str | None) -> bool:
    status = (transaction_status or "").lower()
    payment = (payment_type or "").upper()
    source = (transaction_source or "").lower()
    return (
        status in {"settlement", "capture"}
        and payment in {"QRIS", "GOPAY"}
        and source not in FOOD_SOURCES
    )


def whole_rupiah(value) -> int | None:
    # GoFood returns IDR in sen. Rp 23 is stored as 2300.
    if value is None or value == "":
        return None
    try:
        amount = int(round(float(value) / 100))
    except (TypeError, ValueError):
        return None
    if amount < 1:
        return None
    return amount


def speaker_primed(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM app_state WHERE key = 'speaker_primed'").fetchone()
    return bool(row and row["value"] == "1")


def mark_notified(conn: sqlite3.Connection, tx_id: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if tx_id is None:
        conn.execute(
            "UPDATE transactions SET notified_at = ? WHERE notified_at IS NULL",
            (now,),
        )
    else:
        conn.execute(
            "UPDATE transactions SET notified_at = ? WHERE id = ?",
            (now, tx_id),
        )


def post_speaker(settings: Settings, row: sqlite3.Row, amount: int) -> bool:
    url = f"{settings.speaker_url}/gofood/notification"
    body = json.dumps(
        {
            "order_id": row["id"],
            "amount": amount,
            "payment_type": row["payment_type"] or "",
            "transaction_source": row["transaction_source"] or "",
            "transaction_status": row["transaction_status"] or "",
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {settings.speaker_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        status, payload, _headers = _request(url, "POST", headers, body)
    except ApiError as error:
        log(f"speaker {row['id']}: {error}")
        return False
    if status >= 400:
        log(f"speaker {row['id']}: {status} {error_message(payload)}")
        return False
    return True


def notify_speaker(conn: sqlite3.Connection, settings: Settings) -> int:
    if not settings.speaker_url:
        return 0
    if not speaker_primed(conn):
        mark_notified(conn)
        conn.execute(
            """
            INSERT INTO app_state (key, value) VALUES ('speaker_primed', '1')
            ON CONFLICT(key) DO UPDATE SET value = '1'
            """
        )
        conn.commit()
        log("speaker primed; existing payments stay silent")
        return 0
    rows = conn.execute(
        """
        SELECT id, gross_amount, payment_type, transaction_status, transaction_source
        FROM transactions
        WHERE notified_at IS NULL
        """
    ).fetchall()
    spoken = 0
    for row in rows:
        if not should_announce(row["payment_type"], row["transaction_source"], row["transaction_status"]):
            mark_notified(conn, row["id"])
            continue
        amount = whole_rupiah(row["gross_amount"])
        if amount is None:
            mark_notified(conn, row["id"])
            continue
        if not post_speaker(settings, row, amount):
            continue
        mark_notified(conn, row["id"])
        spoken += 1
    conn.commit()
    return spoken


def poll_once(conn: sqlite3.Connection, settings: Settings, session: dict) -> tuple[int, int, int]:
    fetched = 0
    inserted = 0
    updated = 0
    refreshed = False
    for page in range(settings.max_pages_per_poll):
        try:
            rows, _total = fetch_page(settings, session, page)
        except ApiError as error:
            if error.status == 401 and not refreshed:
                refresh_tokens(conn, session, settings.timezone)
                refreshed = True
                rows, _total = fetch_page(settings, session, page)
            else:
                raise
        ids = [
            str(row.get("wallstreet_transaction_id") or row.get("id") or "")
            for row in rows
        ]
        ids = [item for item in ids if item]
        already = known_ids(conn, ids)
        page_inserted, page_updated = upsert_transactions(conn, rows)
        fetched += len(rows)
        inserted += page_inserted
        updated += page_updated
        if len(rows) < settings.page_size:
            break
        if ids and already.issuperset(ids):
            break
        if page + 1 < settings.max_pages_per_poll:
            time.sleep(1)
    return fetched, inserted, updated


def log(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {message}", flush=True)


def sleep_for(seconds: float) -> None:
    jitter = min(1.0, seconds * 0.15)
    time.sleep(seconds + (jitter * ((time.monotonic() * 1000) % 100) / 100))


def run(settings: Settings, once: bool, import_tokens: bool) -> None:
    conn = connect(settings.database)
    session = load_session(conn, settings, import_tokens)
    failures = 0
    forbidden = 0
    while True:
        try:
            fetched, inserted, updated = poll_once(conn, settings, session)
            spoken = notify_speaker(conn, settings)
            failures = 0
            forbidden = 0
            message = f"saved fetched={fetched} new={inserted} updated={updated}"
            if settings.speaker_url:
                message += f" spoken={spoken}"
            log(message)
            if once:
                return
            sleep_for(settings.poll_seconds)
        except ApiError as error:
            if error.status == 401:
                log(error.args[0] if error.args else "unauthorized")
                raise SystemExit(1) from error
            if error.status == 403:
                forbidden += 1
                log(f"forbidden ({forbidden}/3): {error}")
                if forbidden >= 3:
                    raise SystemExit("stopped after repeated 403") from error
                sleep_for(60 * forbidden)
                continue
            if error.status == 429:
                failures += 1
                wait = error.retry_after or min(MAX_BACKOFF_SECONDS, settings.poll_seconds * (2**failures))
                log(f"rate limited, sleeping {wait:.0f}s")
                if once:
                    raise SystemExit(1) from error
                time.sleep(wait)
                continue
            failures += 1
            wait = min(MAX_BACKOFF_SECONDS, settings.poll_seconds * (2**failures))
            log(f"request failed ({error.status}): {error}. sleeping {wait:.0f}s")
            if once:
                raise SystemExit(1) from error
            time.sleep(wait)
        except KeyboardInterrupt:
            log("stopped")
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll GoFood merchant transactions into SQLite")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true", help="Fetch one cycle and exit")
    parser.add_argument(
        "--import-tokens",
        action="store_true",
        help="Replace the saved session with tokens from config.json",
    )
    args = parser.parse_args()
    settings = load_settings(Path(args.config))
    run(settings, once=args.once, import_tokens=args.import_tokens)


if __name__ == "__main__":
    main()
