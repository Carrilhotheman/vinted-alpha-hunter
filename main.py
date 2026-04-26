import os
import json
import time
import random
import requests
import telebot

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEEN_ITEMS_FILE = "seen_items.json"

# Add or remove keywords here.
# resale   = estimated resale price on Depop / Vinted / StockX
# min/max  = price band worth alerting — outside this = skip
KEYWORDS: dict[str, dict] = {
    "Carhartt Detroit": {
        "resale": 130.0, "min_price": 15.0, "max_price": 85.0,
    },
    "Nike Vintage Hoodie": {
        "resale": 85.0, "min_price": 10.0, "max_price": 55.0,
    },
    "Arc'teryx": {
        "resale": 290.0, "min_price": 60.0, "max_price": 230.0,
    },
    "Vintage Levi's": {
        "resale": 75.0, "min_price": 8.0, "max_price": 55.0,
    },
    "Trapstar": {
        "resale": 120.0, "min_price": 20.0, "max_price": 80.0,
    },
    "Stone Island": {
        "resale": 220.0, "min_price": 40.0, "max_price": 170.0,
    },
    "Stussy Vintage": {
        "resale": 90.0, "min_price": 10.0, "max_price": 60.0,
    },
}

# ── Tuning knobs ──────────────────────────────────────────────────────────────
BUYER_FEE_RATE   = 0.05    # 5 % of item price
BUYER_FEE_FIXED  = 0.70    # + fixed €0.70
SHIPPING_ESTIMATE = 4.50   # conservative average
MIN_ROI = 1.1              # % — alerts below this are suppressed

SNIPER_PAGES  = 1          # only need the freshest page
SCANNER_PAGES = 5          # dig 5 pages deep for forgotten gems
PER_PAGE      = 24

# Delay ranges (seconds) — randomised to mimic human browsing
DELAY_BETWEEN_PAGES    = (2.5, 5.0)
DELAY_BETWEEN_KEYWORDS = (3.0, 6.0)
DELAY_PHASE_BREAK      = (8.0, 15.0)   # pause between Sniper and Scanner
DELAY_TELEGRAM         = (0.4, 0.8)    # between Telegram sends

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


# ── Session ───────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.vinted.fr/", timeout=15)
    except requests.RequestException:
        pass
    return session


# ── Low-level fetch ───────────────────────────────────────────────────────────

def fetch_page(
    session: requests.Session,
    keyword: str,
    order: str,
    page: int,
    max_price: float,
) -> list[dict]:
    params = {
        "search_text": keyword,
        "order": order,
        "page": page,
        "per_page": PER_PAGE,
        "currency": "EUR",
        "price_to": max_price,
    }
    resp = session.get(VINTED_API, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("items", [])


# ── Financial logic ───────────────────────────────────────────────────────────

def buyer_fee(price: float) -> float:
    return round(max(price * BUYER_FEE_RATE + BUYER_FEE_FIXED, BUYER_FEE_FIXED), 2)


def roi_metrics(
    price: float, fees: float, shipping: float, resale: float
) -> tuple[float, float]:
    total = price + fees + shipping
    profit = resale - total
    roi = (profit / total) * 100
    return round(profit, 2), round(roi, 2)


# ── Risk assessment ───────────────────────────────────────────────────────────

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


# ── Message formatting ────────────────────────────────────────────────────────

def build_message(
    item: dict,
    keyword: str,
    profit: float,
    roi: float,
    flags: list[str],
    fees: float,
    mode: str,          # "sniper" | "scanner"
) -> str:
    price = float(item.get("price", 0))
    total = price + fees + SHIPPING_ESTIMATE
    resale = KEYWORDS[keyword]["resale"]
    title = item.get("title", "—")
    brand = item.get("brand_title") or keyword
    reviews = (item.get("user") or {}).get("feedback_count", 0)
    item_url = f"https://www.vinted.fr/items/{item['id']}"

    mode_badge = "🎯 SNIPER — Fresh listing" if mode == "sniper" else "🔍 SCANNER — Hidden gem"

    return (
        f"{mode_badge}\n"
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
        f"🔗 [View on Vinted]({item_url})"
    )


# ── Core item processor (shared by both modes) ────────────────────────────────

def process_items(
    bot: telebot.TeleBot,
    items: list[dict],
    keyword: str,
    seen: set[str],
    mode: str,
) -> int:
    cfg = KEYWORDS[keyword]
    sent = 0

    for item in items:
        item_id = str(item.get("id", ""))
        if not item_id or item_id in seen:
            continue

        # Mark seen immediately so even skipped items aren't re-evaluated
        seen.add(item_id)

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
        msg = build_message(item, keyword, profit, roi, flags, fees, mode)

        try:
            bot.send_message(CHAT_ID, msg, disable_web_page_preview=False)
            sent += 1
            time.sleep(random.uniform(*DELAY_TELEGRAM))
        except Exception as exc:
            print(f"[WARN] Telegram send failed: {exc}")

    return sent


# ── Sniper: newest items, page 1 only ────────────────────────────────────────

def run_sniper(
    bot: telebot.TeleBot,
    session: requests.Session,
    seen: set[str],
) -> int:
    print("[SNIPER] Starting real-time scan...")
    total = 0

    for keyword, cfg in KEYWORDS.items():
        try:
            items = fetch_page(session, keyword, "newest_first", 1, cfg["max_price"])
            total += process_items(bot, items, keyword, seen, "sniper")
        except requests.RequestException as exc:
            print(f"[SNIPER][WARN] '{keyword}': {exc}")
        except Exception as exc:
            print(f"[SNIPER][ERROR] '{keyword}': {exc}")

        time.sleep(random.uniform(*DELAY_BETWEEN_KEYWORDS))

    print(f"[SNIPER] Done — {total} alert(s) sent.")
    return total


# ── Scanner: cheap-first, multiple pages ─────────────────────────────────────

def run_scanner(
    bot: telebot.TeleBot,
    session: requests.Session,
    seen: set[str],
) -> int:
    print("[SCANNER] Starting deep hunt for hidden gems...")
    total = 0

    for keyword, cfg in KEYWORDS.items():
        for page in range(1, SCANNER_PAGES + 1):
            try:
                items = fetch_page(
                    session, keyword, "price_low_to_high", page, cfg["max_price"]
                )
                if not items:
                    break   # no more results for this keyword
                total += process_items(bot, items, keyword, seen, "scanner")
            except requests.RequestException as exc:
                print(f"[SCANNER][WARN] '{keyword}' p{page}: {exc}")
                break       # stop paging on network error
            except Exception as exc:
                print(f"[SCANNER][ERROR] '{keyword}' p{page}: {exc}")
                break

            if page < SCANNER_PAGES:
                time.sleep(random.uniform(*DELAY_BETWEEN_PAGES))

        time.sleep(random.uniform(*DELAY_BETWEEN_KEYWORDS))

    print(f"[SCANNER] Done — {total} alert(s) sent.")
    return total


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="MarkdownV2")
    seen = load_seen()
    session = make_session()

    sniper_alerts = run_sniper(bot, session, seen)

    # Pause between phases so the two bursts of requests don't look robotic
    pause = random.uniform(*DELAY_PHASE_BREAK)
    print(f"[PAUSE] Waiting {pause:.1f}s before scanner phase...")
    time.sleep(pause)

    scanner_alerts = run_scanner(bot, session, seen)

    save_seen(seen)
    print(
        f"\n=== Run complete ===\n"
        f"  Sniper alerts : {sniper_alerts}\n"
        f"  Scanner alerts: {scanner_alerts}\n"
        f"  Items tracked : {len(seen)}"
    )


if __name__ == "__main__":
    main()
