import os
import json
import time
import requests
import telebot

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEEN_ITEMS_FILE = "seen_items.json"

# Per-keyword config: estimated resale price, and the price band worth hunting.
# Anything outside [min_price, max_price] is skipped (too cheap = likely fake,
# too expensive = margin gone).
KEYWORDS: dict[str, dict] = {
    "Carhartt Detroit": {
        "resale": 130.0,
        "min_price": 15.0,
        "max_price": 85.0,
    },
    "Nike Vintage Hoodie": {
        "resale": 85.0,
        "min_price": 10.0,
        "max_price": 55.0,
    },
    "Arc'teryx": {
        "resale": 290.0,
        "min_price": 60.0,
        "max_price": 230.0,
    },
    "Vintage Levi's": {
        "resale": 75.0,
        "min_price": 8.0,
        "max_price": 55.0,
    },
}

# Vinted buyer-protection fee: 5 % of item price + €0.70 fixed, min €0.70.
BUYER_FEE_RATE = 0.05
BUYER_FEE_FIXED = 0.70
SHIPPING_ESTIMATE = 4.50   # conservative average for small parcel

# Minimum ROI (%) before an alert is sent.
MIN_ROI = 15.0

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
    with open(SEEN_ITEMS_FILE, "r") as fh:
        data = json.load(fh)
    return set(data) if isinstance(data, list) else set()


def save_seen(seen: set[str]) -> None:
    with open(SEEN_ITEMS_FILE, "w") as fh:
        json.dump(sorted(seen), fh)


# ── Vinted session & search ───────────────────────────────────────────────────

def make_session() -> requests.Session:
    """Prime a session with a real browser cookie from Vinted's homepage."""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.vinted.fr/", timeout=15)
    except requests.RequestException:
        pass  # continue without cookie — API may still respond
    return session


def fetch_items(session: requests.Session, keyword: str, max_price: float) -> list[dict]:
    params = {
        "search_text": keyword,
        "order": "newest_first",
        "page": 1,
        "per_page": 24,
        "currency": "EUR",
        "price_to": max_price,
    }
    resp = session.get(VINTED_API, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("items", [])


# ── Financial logic ───────────────────────────────────────────────────────────

def buyer_fee(price: float) -> float:
    return round(max(price * BUYER_FEE_RATE + BUYER_FEE_FIXED, BUYER_FEE_FIXED), 2)


def roi_metrics(price: float, fees: float, shipping: float, resale: float) -> tuple[float, float]:
    total_cost = price + fees + shipping
    profit = resale - total_cost
    roi = (profit / total_cost) * 100
    return round(profit, 2), round(roi, 2)


# ── Risk assessment ───────────────────────────────────────────────────────────

def assess_risk(item: dict) -> list[str]:
    flags: list[str] = []
    user = item.get("user") or {}

    feedback_count = user.get("feedback_count", 0)
    if feedback_count == 0:
        flags.append("zero reviews")
    elif feedback_count < 3:
        flags.append("< 3 reviews")

    reputation = user.get("feedback_reputation", 1.0)
    if reputation is not None and reputation < 0.80:
        flags.append(f"rep {reputation:.0%}")

    if not item.get("photos"):
        flags.append("no photos")

    return flags


def risk_label(flags: list[str]) -> str:
    if not flags:
        return "🟢 Low"
    if len(flags) == 1:
        return f"🟡 Medium — {flags[0]}"
    return f"🔴 High — {', '.join(flags)}"


# ── Message formatting ────────────────────────────────────────────────────────

def build_message(
    item: dict,
    keyword: str,
    profit: float,
    roi: float,
    flags: list[str],
    fees: float,
) -> str:
    price = float(item.get("price", 0))
    total = price + fees + SHIPPING_ESTIMATE
    resale = KEYWORDS[keyword]["resale"]
    title = item.get("title", "—")
    brand = item.get("brand_title") or keyword
    seller_reviews = (item.get("user") or {}).get("feedback_count", 0)
    item_url = f"https://www.vinted.fr/items/{item['id']}"

    return (
        f"🎯 *Alpha Hunt — {brand}*\n"
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
        f"⭐ Seller reviews: {seller_reviews}\n"
        f"🔗 [View on Vinted]({item_url})"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="MarkdownV2")
    seen = load_seen()
    session = make_session()
    alerts_sent = 0

    for keyword, cfg in KEYWORDS.items():
        try:
            items = fetch_items(session, keyword, cfg["max_price"])
        except requests.RequestException as exc:
            print(f"[WARN] fetch failed for '{keyword}': {exc}")
            continue
        except Exception as exc:
            print(f"[ERROR] unexpected error for '{keyword}': {exc}")
            continue

        for item in items:
            item_id = str(item.get("id", ""))
            if not item_id or item_id in seen:
                continue

            seen.add(item_id)  # mark before any filtering so we don't revisit

            try:
                price = float(item.get("price", 0))
            except (TypeError, ValueError):
                continue

            if not (cfg["min_price"] <= price <= cfg["max_price"]):
                continue

            fees = buyer_fee(price)
            profit, roi = roi_metrics(price, fees, SHIPPING_ESTIMATE, cfg["resale"])

            if roi < MIN_ROI:
                continue

            flags = assess_risk(item)
            msg = build_message(item, keyword, profit, roi, flags, fees)

            try:
                bot.send_message(CHAT_ID, msg, disable_web_page_preview=False)
                alerts_sent += 1
                time.sleep(0.4)  # stay under Telegram rate limit
            except Exception as exc:
                print(f"[WARN] Telegram send failed: {exc}")

        time.sleep(2)  # be polite between keyword searches

    save_seen(seen)
    print(f"Run complete — {alerts_sent} alert(s) sent, {len(seen)} items tracked.")


if __name__ == "__main__":
    main()
