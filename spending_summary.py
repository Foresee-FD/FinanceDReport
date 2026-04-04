import os
import json
import re
from datetime import datetime, timedelta, date
from pathlib import Path
import pytz

import plaid
from plaid.api import plaid_api
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from twilio.rest import Client

# ── Config ──────────────────────────────────────────────────────────────────────
PLAID_CLIENT_ID    = os.environ["PLAID_CLIENT_ID"]
PLAID_SECRET       = os.environ["PLAID_SECRET"]
PLAID_ACCESS_TOKEN = os.environ["PLAID_ACCESS_TOKEN"]

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM        = os.environ["TWILIO_FROM"]
TWILIO_TO          = os.environ["TWILIO_TO"]

WEEKLY_BUDGET = float(os.environ.get("WEEKLY_BUDGET", "200"))
TIMEZONE      = os.environ.get("TIMEZONE", "America/Chicago")
CARD_NAME     = os.environ.get("CARD_NAME", "Robinhood")

STATE_FILE = Path("state.json")
DEDUP_DAYS = 5


# ── State management ─────────────────────────────────────────────────────────────
# state.json structure:
# {
#   "cursor":       "...",        <- Plaid sync cursor (None on very first run)
#   "week_start":   "2026-04-03", <- Friday this budget week began
#   "weekly_total": 90.00,        <- running spend total, carries over daily
#   "setup_done":   false,        <- flips true after first-run setup SMS
#   "seen": {
#     "fingerprint_key": {
#       "first_seen": "2026-04-01",
#       "amount": 15.43,
#       "merchant": "mcdonald's"
#     }
#   }
# }

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "cursor":       None,
        "week_start":   None,
        "weekly_total": 0.0,
        "setup_done":   False,
        "seen":         {},
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def purge_old_entries(seen: dict, today: date) -> dict:
    cutoff = (today - timedelta(days=DEDUP_DAYS)).isoformat()
    return {k: v for k, v in seen.items() if v["first_seen"] >= cutoff}


def make_fingerprint(amount: float, merchant: str, txn_date: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", merchant.lower())
    amount_str = f"{round(amount, 2):.2f}"
    d = date.fromisoformat(txn_date)
    bucket = (d - timedelta(days=d.toordinal() % 3)).isoformat()
    return f"{normalized}|{amount_str}|{bucket}"


# ── Plaid ────────────────────────────────────────────────────────────────────────

def get_plaid_client() -> plaid_api.PlaidApi:
    config = plaid.Configuration(
        host=plaid.Environment.Production,
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(config))


def fetch_balance() -> dict:
    client   = get_plaid_client()
    response = client.accounts_balance_get(
        AccountsBalanceGetRequest(access_token=PLAID_ACCESS_TOKEN)
    )
    for account in response["accounts"]:
        if account["type"] == "credit":
            bal = account["balances"]
            return {
                "current":   bal.get("current"),
                "available": bal.get("available"),
                "limit":     bal.get("limit"),
            }
    bal = response["accounts"][0]["balances"]
    return {
        "current":   bal.get("current"),
        "available": bal.get("available"),
        "limit":     bal.get("limit"),
    }


def fetch_new_transactions(cursor: str | None) -> tuple[list, list, str]:
    client = get_plaid_client()
    added, removed = [], []

    while True:
        kwargs = {"access_token": PLAID_ACCESS_TOKEN}
        if cursor:
            kwargs["cursor"] = cursor

        response = client.transactions_sync(TransactionsSyncRequest(**kwargs))
        added   += response["added"]
        removed += response["removed"]
        cursor   = response["next_cursor"]

        if not response["has_more"]:
            break

    return added, removed, cursor


def is_spending(txn) -> bool:
    if txn["amount"] <= 0:
        return False
    skip_categories = {"Transfer", "Payment", "Interest Charged", "Credit Card"}
    categories = set(txn.get("category") or [])
    return not categories.intersection(skip_categories)


# ── Date helpers ─────────────────────────────────────────────────────────────────

def get_week_start(today: date) -> date:
    days_since_friday = (today.weekday() - 4) % 7
    return today - timedelta(days=days_since_friday)


# ── Message builders ─────────────────────────────────────────────────────────────

def truncate(name: str, length: int = 26) -> str:
    return name if len(name) <= length else name[: length - 1] + "…"


def build_setup_message(balance: dict) -> str:
    divider = "─" * 12
    current   = balance.get("current")
    available = balance.get("available")
    limit     = balance.get("limit")

    lines = [
        f"{CARD_NAME} Spending Tracker",
        "is up and running",
        divider,
        "You'll get a text every night",
        "at 10pm with your daily spend",
        f"and weekly budget (${WEEKLY_BUDGET:.0f}/wk).",
        "Week resets every Friday.",
        divider,
        "Current Card Balance:",
        f"  Owed:      ${current:.2f}" if current is not None else "  Owed:      N/A",
    ]
    if available is not None:
        lines.append(f"  Available: ${available:.2f}")
    if limit is not None:
        lines.append(f"  Limit:     ${limit:.2f}")
    lines.append(divider)
    lines.append("Your first daily summary")
    lines.append("follows right after this.")
    return "\n".join(lines)


def build_message(today_txns: list, weekly_total: float, balance: dict) -> str:
    divider = "─" * 12
    lines = [f"{CARD_NAME} Daily Spending", divider]

    daily_total = 0.0
    if today_txns:
        for t in today_txns:
            amt         = t["amount"]
            daily_total += amt
            merchant    = truncate(t.get("merchant_name") or t.get("name") or "Unknown")
            pending_tag = " ⏲" if t.get("pending") else ""
            lines.append(f"${amt:>7.2f}  {merchant}{pending_tag}")
    else:
        lines.append("  No new transactions today")

    remaining = WEEKLY_BUDGET - weekly_total
    pct_used  = min((weekly_total / WEEKLY_BUDGET) * 100, 100) if WEEKLY_BUDGET else 0
    filled    = round(pct_used / 10)
    bar       = "█" * filled + "░" * (10 - filled)

    current   = balance.get("current")
    available = balance.get("available")
    limit     = balance.get("limit")

    lines += [
        divider,
        f"Daily Total:  ${daily_total:.2f}",
        divider,
        f"Weekly Remaining: ${remaining:.2f}",
        f"[{bar}] {pct_used:.0f}%",
        f"${weekly_total:.2f} of ${WEEKLY_BUDGET:.0f} used (resets Fri)",
        divider,
        "Card Balance:",
        f"  Owed:      ${current:.2f}" if current is not None else "  Owed:      N/A",
    ]
    if available is not None:
        lines.append(f"  Available: ${available:.2f}")
    if limit is not None:
        lines.append(f"  Limit:     ${limit:.2f}")

    lines.append("⏳ = pending")
    return "\n".join(lines)


# ── Twilio ───────────────────────────────────────────────────────────────────────

def send_sms(message: str):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    msg = client.messages.create(body=message, from_=TWILIO_FROM, to=TWILIO_TO)
    print(f"SMS sent: {msg.sid}")


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    tz    = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()

    week_start = get_week_start(today)
    print(f"Today: {today} | Week start: {week_start}")

    state  = load_state()
    cursor = state.get("cursor")
    seen   = state.get("seen", {})

    is_first_run = not state.get("setup_done", False)

    stored_week_start = state.get("week_start")
    if stored_week_start == week_start.isoformat():
        weekly_total = float(state.get("weekly_total", 0.0))
        print(f"Continuing week of {week_start} | Carried-over total: ${weekly_total:.2f}")
    else:
        weekly_total = 0.0
        seen = {}
        print(f"New week detected ({stored_week_start} → {week_start}) | Resetting weekly total")

    seen = purge_old_entries(seen, today)

    print("Fetching card balance...")
    balance = fetch_balance()
    print(f"Balance → owed: ${balance['current']}  available: ${balance['available']}")

    added, removed, new_cursor = fetch_new_transactions(cursor)
    print(f"Plaid returned {len(added)} added, {len(removed)} removed")

    for txn in removed:
        fp = make_fingerprint(
            txn["amount"],
            txn.get("merchant_name") or txn.get("name") or "",
            str(txn["date"]),
        )
        if fp in seen:
            weekly_total = max(0.0, weekly_total - txn["amount"])
            seen.pop(fp, None)
            print(f"  REMOVED & subtracted: {txn.get('merchant_name') or txn.get('name')} ${txn['amount']}")

    today_txns = []
    for txn in [t for t in added if is_spending(t)]:
        merchant = txn.get("merchant_name") or txn.get("name") or ""
        fp = make_fingerprint(txn["amount"], merchant, str(txn["date"]))

        if fp in seen:
            print(f"  SKIP (duplicate): {merchant} ${txn['amount']}")
            continue

        seen[fp] = {
            "first_seen": today.isoformat(),
            "amount":     txn["amount"],
            "merchant":   merchant,
        }

        txn_date = date.fromisoformat(str(txn["date"]))

        if txn_date >= week_start:
            weekly_total += txn["amount"]
            print(f"  +${txn['amount']:.2f} {merchant} → weekly total now ${weekly_total:.2f}")

        if txn_date == today:
            today_txns.append(txn)

    if is_first_run:
        setup_msg = build_setup_message(balance)
        print("\n── Setup Message ─────────────────")
        print(setup_msg)
        print("──────────────────────────────────\n")
        send_sms(setup_msg)

    message = build_message(today_txns, weekly_total, balance)
    print("\n── Daily Summary ─────────────────")
    print(message)
    print("──────────────────────────────────\n")
    send_sms(message)

    state["cursor"]       = new_cursor
    state["week_start"]   = week_start.isoformat()
    state["weekly_total"] = round(weekly_total, 2)
    state["setup_done"]   = True
    state["seen"]         = seen
    save_state(state)
    print(f"State saved. Weekly total: ${weekly_total:.2f}")


if __name__ == "__main__":
    main()
