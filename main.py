import os
import json
import sys
import time
import random
import requests
import telebot

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SEEN_ITEMS_FILE = "seen_items.json"

# resale     = realistic resale value (Depop / eBay / StockX)
# min_price  = below this is likely fake or not worth shipping
# max_price  = above this the margin is gone
# min_roi    = minimum ROI % to trigger an alert (1.5 – 1.7 range)
KEYWORDS: dict[str, dict] = {
    "Carhartt Detroit": {
        "resale": 130.0, "min_price": 15.0, "max_price": 85.0, "min_roi": 1.7,
    },
    "Nike Vintage Hoodie": {
        "resale": 85.0,  "min_price": 10.0, "max_price": 55.0, "min_roi": 1.6,
    },
    "Arc'teryx": {
        "resale": 290.0, "min_price": 60.0, "max_price": 230.0, "min_roi": 1.5,
    },
    "Vintage Levi's": {
        "resale": 75.0,  "min_price": 8.0,  "max_price": 55.0, "min_roi": 1.7,
    },
    "Trapstar": {
        "resale": 120.0, "min_price": 20.0, "max_price": 80.0, "min_roi": 1.6,
    },
    "Stone Island": {
        "resale": 220.0, "min_price": 40.0, "max_price": 170.0, "min_roi": 1.5,
    },
    "Stussy Vintage": {
        "resale": 90.0,  "min_price": 10.0, "max_price": 60.0, "min_roi": 1.6,
    },
}

BUYER_FEE_RATE    = 0.05   # 5 % of item price
BUYER_FEE_FIXED   = 0.70   # + €0.70 fixed
SHIPPING_ESTIMATE = 4.50   # conservative average

PER_PAGE              = 24
DELAY_BETWEEN_KEYWORDS = (2.0, 4.5)   # seconds, randomised
DELAY_TELEGRAM         = (0.4, 0.8)

VINTED_API = "https://www.vinted.fr/api/v2/catalog/items"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.vinted.fr/",
    "Origin": "https://www.vinted.fr",
}


# ── Persistence ───────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if not os.path.exists(SEEN_ITEMS_FILE):
        return set()
    try:
        with open(SEEN_ITEMS_FILE, "r") as fh:
            data = json.load(fh)
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(seen: set[str]) -> None:
    try:
        with open(SEEN_ITEMS_FILE, "w") as fh:
            json.dump(sorted(seen), fh)
    except OSError as exc:
        print(f"[WARN] Could not save seen_items.json: {exc}")


# ── Session ───────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.vinted.fr/", timeout=15)
    except requests.RequestException:
        pass
    return session


# ── Vinted fetch ──────────────────────────────────────────────────────────────

def fetch_newest(
    session: requests.Session, keyword: str, max_price: float
) -> list[dict]:
    params = {
        "search_text": keyword,
        "order": "newest_first",
        "page": 1,
        "per_page": PER_PAGE,
        "currency": "EUR",
        "price_to": max_price,
    }
    resp = session.get(VINTED_API, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("items", [])


# ── Financials ────────────────────────────────────────────────────────────────

def buyer_fee(price: float) -> float:
    return round(max(price * BUYER_FEE_RATE + BUYER_FEE_FIXED, BUYER_FEE_FIXED), 2)


def roi_metrics(
    price: float, fees: float, shipping: float, resale: float
) -> tuple[float, float]:
    total = price + fees + shipping
    profit = resale - total
    roi = (profit / total) * 100
    return round(profit, 2), round(roi, 2)


# ── Risk ──────────────────────────────────────────────────────────────────────

def assess_risk(item: dict) -> list[str]:
    flags: list[str] = []
    user = item.get("user") or {}

    count = user.get("feedback_count", 0)
    if count == 0:
        flags.append("zero reviews")
    elif count < 3:
        flags.append(f"only {count} review(s)")

    rep = user.get("feedback_reputation")
    if rep is not None and rep < 0.80:
        flags.append(f"rep {rep:.0%}")

    if not item.get("photos"):
        flags.append("no photos")

    return flags


def risk_label(flags: list[str]) -> str:
    if not flags:
        return "🟢 Low"
    if len(flags) == 1:
        return f"🟡 Medium — {flags[0]}"
    return f"🔴 High — {', '.join(flags)}"


# ── Message ───────────────────────────────────────────────────────────────────

def build_message(
    item: dict, keyword: str, profit: float, roi: float,
    flags: list[str], fees: float,
) -> str:
    price   = float(item.get("price", 0))
    total   = price + fees + SHIPPING_ESTIMATE
    resale  = KEYWORDS[keyword]["resale"]
    title   = item.get("title", "—")
    brand   = item.get("brand_title") or keyword
    reviews = (item.get("user") or {}).get("feedback_count", 0)
    url     = f"https://www.vinted.fr/items/{item['id']}"

    return (
        f"🎯 *SNIPER — Fresh listing*\n"
        f"🏷 *{brand}*\n"
        f"📝 {title}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Price: €{price:.2f}\n"
        f"📦 Est\\. Shipping: €{SHIPPING_ESTIMATE:.2f}\n"
        f"🛡 Buyer Fee: €{fees:.2f}\n"
        f"💸 Total Cost: €{total:.2f}\n"
        f"📈 Est\\. Resale: €{resale:.2f}\n"
        f"💹 Profit: €{profit:.2f}  \\|  ROI: {roi:.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Risk: {risk_label(flags)}\n"
        f"⭐ Seller reviews: {reviews}\n"
        f"🔗 [View on Vinted]({url})"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set.")
        sys.exit(1)

    bot     = telebot.TeleBot(BOT_TOKEN, parse_mode="MarkdownV2")
    seen    = load_seen()
    session = make_session()
    alerts  = 0

    for keyword, cfg in KEYWORDS.items():
        try:
            items = fetch_newest(session, keyword, cfg["max_price"])
        except requests.RequestException as exc:
            print(f"[WARN] Fetch failed for '{keyword}': {exc}")
            time.sleep(random.uniform(*DELAY_BETWEEN_KEYWORDS))
            continue

        for item in items:
            item_id = str(item.get("id", ""))
            if not item_id or item_id in seen:
                continue

            seen.add(item_id)

            try:
                price = float(item.get("price", 0))
            except (TypeError, ValueError):
                continue

            if not (cfg["min_price"] <= price <= cfg["max_price"]):
                continue

            fees           = buyer_fee(price)
            profit, roi    = roi_metrics(price, fees, SHIPPING_ESTIMATE, cfg["resale"])

            if roi < cfg["min_roi"]:
                continue

            flags = assess_risk(item)
            msg   = build_message(item, keyword, profit, roi, flags, fees)

            try:
                bot.send_message(CHAT_ID, msg, disable_web_page_preview=False)
                alerts += 1
                time.sleep(random.uniform(*DELAY_TELEGRAM))
            except Exception as exc:
                print(f"[WARN] Telegram send failed: {exc}")

        time.sleep(random.uniform(*DELAY_BETWEEN_KEYWORDS))

    save_seen(seen)
    print(f"Run complete — {alerts} alert(s) sent, {len(seen)} items tracked.")


if __name__ == "__main__":
    main()
