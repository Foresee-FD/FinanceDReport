import os
import json
import re
from datetime import datetime, timedelta, date
from pathlib import Path
import pytz

import plaid
from plaid.api import plaid_api
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
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
DEDUP_DAYS = 7


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "last_pull_date": None,
        "week_start":     None,
        "weekly_total":   0.0,
        "setup_done":     False,
        "seen":           {},
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


def fetch_transactions(start: date, end: date) -> list:
    client   = get_plaid_client()
    all_txns = []
    offset   = 0

    while True:
        request = TransactionsGetRequest(
            access_token=PLAID_ACCESS_TOKEN,
            start_date=start,
            end_date=end,
            options=TransactionsGetRequestOptions(
                include_personal_finance_category=False,
                include_original_description=False,
                offset=offset,
                count=500,
            ),
        )
        response  = client.transactions_get(request)
        all_txns += response["transactions"]

        if len(all_txns) >= response["total_transactions"]:
            break
        offset = len(all_txns)

    print(f"Fetched {len(all_txns)} transactions ({start} → {end})")
    return all_txns


def is_spending(txn) -> bool:
    if txn["amount"] <= 0:
        return False
    skip_categories = {"Transfer", "Payment", "Interest Charged", "Credit Card"}
    categories = set(txn.get("category") or [])
    return not categories.intersection(skip_categories)


def get_week_start(today: date) -> date:
    days_since_friday = (today.weekday() - 4) % 7
    return today - timedelta(days=days_since_friday)


def truncate(name: str, length: int = 26) -> str:
    return name if len(name) <= length else name[: length - 1] + "..."


def build_setup_message(balance: dict) -> str:
    current   = balance.get("current")
    available = balance.get("available")

    lines = [
        f"{CARD_NAME} Spending Tracker",
        "is up and running!",
        "",
        "You'll get a text every night",
        "at 10pm with your daily spend",
        f"and weekly budget (${WEEKLY_BUDGET:.0f}/wk).",
        "Week resets every Friday.",
        "",
        "Card",
        f"Balance: ${current:.2f}" if current is not None else "Balance: N/A",
    ]
    if available is not None:
        lines.append(f"Available: ${available:.2f}")
    lines += ["", "Your first daily summary follows."]
    return "\n".join(lines)


def build_message(today_txns: list, weekly_total: float, balance: dict, today: date, week_start: date) -> str:
    lines = [f"{CARD_NAME} Daily Spending", ""]

    daily_total = sum(t["amount"] for t in today_txns)
    lines.append(f"Today: ${daily_total:.2f}")
    if today_txns:
        for t in today_txns:
            merchant = truncate(t.get("merchant_name") or t.get("name") or "Unknown")
            lines.append(f"${t['amount']:.2f} — {merchant}")
    else:
        lines.append("No new transactions")

    week_end  = week_start + timedelta(days=6)
    days_left = max((week_end - today).days + 1, 1)
    remaining = WEEKLY_BUDGET - weekly_total
    safe_pace = remaining / days_left

    lines += [
        "",
        "Week",
        f"${weekly_total:.2f} / ${WEEKLY_BUDGET:.2f} used",
        f"${remaining:.2f} left — resets Fri",
        f"Safe pace: ${safe_pace:.2f}/day",
    ]

    current   = balance.get("current")
    available = balance.get("available")

    lines += ["", "Card"]
    lines.append(f"Balance: ${current:.2f}" if current is not None else "Balance: N/A")
    if available is not None:
        lines.append(f"Available: ${available:.2f}")

    return "\n".join(lines)


def send_sms(message: str):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    msg = client.messages.create(body=message, from_=TWILIO_FROM, to=TWILIO_TO)
    print(f"SMS sent: {msg.sid}")


def main():
    tz    = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()

    week_start = get_week_start(today)
    print(f"Today: {today} | Week start: {week_start}")

    state = load_state()
    seen  = state.get("seen", {})

    is_first_run = not state.get("setup_done", False)

    last_pull_date = state.get("last_pull_date")
    if last_pull_date:
        fetch_start = date.fromisoformat(last_pull_date)
    else:
        fetch_start = today - timedelta(days=1)

    print(f"Fetch window: {fetch_start} → {today}")

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

    raw_txns = fetch_transactions(fetch_start, today)
    spending = [t for t in raw_txns if is_spending(t)]

    today_txns = []
    for txn in spending:
        merchant = txn.get("merchant_name") or txn.get("name") or ""
        fp = make_fingerprint(txn["amount"], merchant, str(txn["date"]))

        if fp in seen:
            print(f"  SKIP (already seen): {merchant} ${txn['amount']}")
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

        today_txns.append(txn)

    if is_first_run:
        setup_msg = build_setup_message(balance)
        print("\n── Setup Message ─────────────────")
        print(setup_msg)
        print("──────────────────────────────────\n")
        send_sms(setup_msg)

    message = build_message(today_txns, weekly_total, balance, today, week_start)
    print("\n── Daily Summary ─────────────────")
    print(message)
    print("──────────────────────────────────\n")
    send_sms(message)

    state["last_pull_date"] = today.isoformat()
    state["week_start"]     = week_start.isoformat()
    state["weekly_total"]   = round(weekly_total, 2)
    state["setup_done"]     = True
    state["seen"]           = seen
    save_state(state)
    print(f"State saved. Weekly total: ${weekly_total:.2f}")


if __name__ == "__main__":
    main()