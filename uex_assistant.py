#!/usr/bin/env python3
"""
UEX Deal Assistant
------------------
Runs on a schedule (GitHub Actions). Each run it:

  1. Checks your UEX Marketplace deals about every 2 minutes. When a buyer
     opens a deal on one of your listings, it asks them the order questions
     automatically (time, pickup/delivery, quantity), checks stock and holds,
     and pings you on Discord once the buyer has answered everything (or has
     gone quiet for an hour). Other new messages are pinged as usual.
  2. Reads your replies in Discord. When you reply to a notification with
     shorthand like "tell them levski 7-10pm", it writes a polished message
     (using free GitHub Models AI) and posts it as a DRAFT for you to approve.
  3. When you react ✅ to a draft, it sends the message to the buyer on UEX.
     React ❌ to cancel. Reply to a draft with new instructions to redo it.

Only uses the Python standard library. All settings come from environment
variables (GitHub Actions secrets) - see README.md.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

UEX_API = os.environ.get("UEX_API_BASE", "https://api.uexcorp.uk/2.0").rstrip("/")
UEX_APP_TOKEN = os.environ.get("UEX_APP_TOKEN", "")
UEX_SECRET_KEY = os.environ.get("UEX_SECRET_KEY", "")
UEX_USERNAME = os.environ.get("UEX_USERNAME", "").strip().lower()

DISCORD_API = os.environ.get("DISCORD_API", "https://discord.com/api/v10").rstrip("/")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")

# GitHub Models (free AI). GITHUB_TOKEN is provided automatically in Actions.
AI_TOKEN = os.environ.get("GITHUB_TOKEN", "")
AI_URL = os.environ.get("AI_URL", "https://models.github.ai/inference/chat/completions")
AI_MODEL = os.environ.get("AI_MODEL", "openai/gpt-4o-mini")

MY_TIMEZONE = os.environ.get("MY_TIMEZONE", "America/Chicago")
DEFAULT_LOCATION = os.environ.get("DEFAULT_LOCATION", "Levski Station, Nyx")

STATE_FILE = os.environ.get("STATE_FILE", "state/state.json")

# Automated order questions for new deals on YOUR listings. Set AUTO_FLOW=off to disable.
AUTO_FLOW = os.environ.get("AUTO_FLOW", "on").strip().lower() not in ("off", "0", "false", "no")
PICKUP_LOCATION = os.environ.get("PICKUP_LOCATION", "Levski Station, Nyx")
DELIVERY_FEE = float(os.environ.get("DELIVERY_FEE_PERCENT", "10")) / 100
QUIET_PING_MINUTES = int(os.environ.get("QUIET_PING_MINUTES", "60"))
# Trading hours: weekday (0=Mon..6=Sun) -> (start hour, end hour), end 24 = midnight
TRADING_HOURS = {0: (10, 24), 1: (19, 24), 2: (19, 24), 3: (19, 24), 4: (10, 24), 5: (10, 24), 6: (10, 24)}
HOURS_TEXT = "Fri–Mon 10AM–Midnight and Tue–Thu 7PM–Midnight (US Central)"
HOURS_TEXT_SHORT = "Fri–Mon 10AM–Midnight, Tue–Thu 7PM–Midnight US Central"
# How often to check (seconds) and how long each scheduled run keeps checking (minutes)
CHECK_EVERY_SECONDS = int(os.environ.get("CHECK_EVERY_SECONDS", "120"))
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "4.5"))
DRAFT_EXPIRY_HOURS = 24
YES = "✅"  # ✅
NO = "❌"   # ❌


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------

def http(method, url, headers=None, body=None, retries=2):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode() or "null"
                return r.status, json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            if e.code == 429 and attempt < retries:  # rate limited
                wait = 2.0
                try:
                    wait = float(json.loads(raw).get("retry_after", 2))
                except Exception:
                    pass
                time.sleep(min(wait, 10) + 0.5)
                continue
            if e.code >= 500 and attempt < retries:
                time.sleep(2)
                continue
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw[:500]}
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2)
                continue
            return 0, {"error": str(e)}
    return 0, None


# --------------------------------------------------------------------------
# UEX
# --------------------------------------------------------------------------

def uex_headers():
    return {
        "Authorization": f"Bearer {UEX_APP_TOKEN}",
        "secret-key": UEX_SECRET_KEY,
        "Accept": "application/json",
        "User-Agent": "uex-deal-assistant/1.0",
    }


def uex_get(resource, params=None):
    url = f"{UEX_API}/{resource}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    code, js = http("GET", url, uex_headers())
    if not isinstance(js, dict):
        raise RuntimeError(f"UEX {resource}: HTTP {code}, bad response")
    status = js.get("status")
    if status in ("no_messages_found",):
        return []
    if status != "ok":
        raise RuntimeError(f"UEX {resource}: HTTP {code}, status={status} {js.get('message', '')}")
    data = js.get("data") or []
    return data if isinstance(data, list) else [data]


def uex_send_message(neg_hash, text):
    body = {"is_production": 1, "hash": neg_hash, "message": text}
    code, js = http("POST", f"{UEX_API}/marketplace_negotiations_messages/", uex_headers(), body)
    ok = isinstance(js, dict) and js.get("status") == "ok"
    return ok, (js.get("status") if isinstance(js, dict) else f"HTTP {code}")


def my_username(negotiations):
    if UEX_USERNAME:
        return UEX_USERNAME
    for n in negotiations:
        if int(n.get("is_listing_advertiser") or 0) == 1 and n.get("advertiser_username"):
            return n["advertiser_username"].lower()
    return ""


def other_party(n, me):
    adv = (n.get("advertiser_username") or "").lower()
    if me and adv == me:
        return n.get("client_name") or n.get("client_username") or "Buyer"
    if int(n.get("is_listing_advertiser") or 0) == 1:
        return n.get("client_name") or n.get("client_username") or "Buyer"
    return n.get("advertiser_name") or n.get("advertiser_username") or "Seller"


def listing_url(slug):
    return f"https://uexcorp.space/marketplace/item/info/{slug}/" if slug else "https://uexcorp.space/marketplace/manage"


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def dh():
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "User-Agent": "DiscordBot (https://github.com, 1.0) uex-deal-assistant",
    }


def d_post(content=None, embed=None, reply_to=None, ping=True):
    body = {"allowed_mentions": {"users": [DISCORD_USER_ID] if ping else []}}
    if content:
        body["content"] = content[:1990]
    if embed:
        body["embeds"] = [embed]
    if reply_to:
        body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
    code, js = http("POST", f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages", dh(), body)
    if code not in (200, 201):
        raise RuntimeError(f"Discord post failed: HTTP {code} {js}")
    return js["id"]


def d_edit(mid, content=None, embed=None):
    body = {}
    if content is not None:
        body["content"] = content[:1990]
    if embed is not None:
        body["embeds"] = [embed]
    http("PATCH", f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages/{mid}", dh(), body)


def d_react(mid, emoji):
    e = urllib.parse.quote(emoji)
    http("PUT", f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages/{mid}/reactions/{e}/@me", dh())
    time.sleep(0.4)


def d_reacted_by_me(mid, emoji):
    e = urllib.parse.quote(emoji)
    code, js = http("GET", f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages/{mid}/reactions/{e}", dh())
    if code == 404:
        return None  # message deleted
    return isinstance(js, list) and any(u.get("id") == DISCORD_USER_ID for u in js)


def d_new_messages(after_id):
    params = {"limit": 100}
    if after_id:
        params["after"] = after_id
    url = f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages?" + urllib.parse.urlencode(params)
    code, js = http("GET", url, dh())
    if code != 200 or not isinstance(js, list):
        raise RuntimeError(f"Discord read failed: HTTP {code} {js}")
    return sorted(js, key=lambda m: int(m["id"]))  # oldest first


# --------------------------------------------------------------------------
# AI drafting (GitHub Models, free) with safe fallback
# --------------------------------------------------------------------------

def tz_info():
    now_utc = datetime.now(timezone.utc)
    if ZoneInfo:
        local = now_utc.astimezone(ZoneInfo(MY_TIMEZONE))
        abbr = local.strftime("%Z")
        off = local.utcoffset().total_seconds() / 3600
        return local, abbr, off
    return now_utc, "UTC", 0


def draft_reply(instruction, n, buyer_msgs, me):
    local, abbr, off = tz_info()
    offset_txt = f"UTC{'+' if off >= 0 else '-'}{abs(int(off))}"
    history = "\n".join(f"- {m}" for m in buyer_msgs[-6:]) or "(no messages yet - they just opened the deal)"
    system = (
        "You write short, friendly, clear trade messages for a Star Citizen player selling items on the "
        "UEX Marketplace. Turn the seller's shorthand instruction into one polished message to the buyer. "
        "Rules: keep it 1-3 sentences; do not invent prices, quantities, locations, or times the seller did "
        f"not give; the seller's timezone is {abbr} ({offset_txt}); whenever a time is mentioned, write it "
        f"as '<time> {abbr} (<same time> UTC)' with the UTC conversion correct; if the seller just says "
        f"'levski' the place is '{DEFAULT_LOCATION}'; no emojis; no sign-off name; output only the message text."
    )
    user = (
        f"Today is {local.strftime('%A %B %d, %Y, %I:%M %p')} {abbr}.\n"
        f"Item: {n.get('listing_title', 'item')}\n"
        f"Buyer: {other_party(n, me)}\n"
        f"Recent buyer messages:\n{history}\n\n"
        f"Seller's instruction: {instruction}"
    )
    if AI_TOKEN:
        body = {
            "model": AI_MODEL,
            "temperature": 0.3,
            "max_tokens": 200,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        headers = {"Authorization": f"Bearer {AI_TOKEN}", "Accept": "application/json"}
        code, js = http("POST", AI_URL, headers, body)
        try:
            text = js["choices"][0]["message"]["content"].strip().strip('"')
            if text:
                return text, True
        except Exception:
            log(f"AI drafting failed (HTTP {code}): {str(js)[:300]}")
    # Fallback: send what the seller typed, minus a leading "tell them"
    text = instruction.strip()
    for prefix in ("tell them ", "tell him ", "tell her ", "say "):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    return (text[:1].upper() + text[1:]) if text else instruction, False


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------
# Main steps
# --------------------------------------------------------------------------

def notify_embed(title, n, me, text=None, color=0x2B8CFF):
    e = {
        "title": title[:250],
        "url": listing_url(n.get("listing_slug")),
        "color": color,
        "fields": [
            {"name": "Item", "value": str(n.get("listing_title") or "?")[:1000], "inline": True},
            {"name": "With", "value": str(other_party(n, me))[:1000], "inline": True},
        ],
        "footer": {"text": "Reply to this message to answer (e.g. \"tell them levski 7-10pm\")"},
    }
    if n.get("price"):
        try:
            e["fields"].append({"name": "Listed price", "value": f"{float(n['price']):,.0f} {n.get('currency') or 'UEC'}", "inline": True})
        except Exception:
            pass
    if text:
        e["description"] = text[:3900]
    return e


# --------------------------------------------------------------------------
# Automated order questions (flow)
# --------------------------------------------------------------------------

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def item_name(title):
    t = str(title or "item").strip()
    if t.upper().startswith("WTS "):
        t = t[4:]
    if "(" in t:
        t = t.split("(")[0].strip()
    return t or "item"


def fmt_uec(v):
    return f"{v:,.0f}"


def fmt_time(weekday, hour, minute):
    if weekday is None or hour is None:
        return None
    h = hour % 12 or 12
    ap = "AM" if hour < 12 else "PM"
    if hour == 0 and not minute:
        return f"{DAYS[weekday]} Midnight"
    return f"{DAYS[weekday]} {h}{':%02d' % minute if minute else ''}{ap}"


def within_hours(weekday, hour, minute=0):
    if weekday is None or hour is None:
        return True
    start, end = TRADING_HOURS.get(weekday, (0, 24))
    t = hour + (minute or 0) / 60
    if hour == 0 and not minute:  # "midnight" = end of the previous day's window
        return True
    return start <= t < end


def fetch_my_listings(me):
    try:
        data = uex_get("marketplace_listings", {"username": me}) if me else []
    except Exception as ex:
        log(f"listings: {ex}")
        return {}
    out = {}
    for l in data or []:
        for key in (l.get("slug"), str(l.get("id")), (l.get("title") or "").lower()):
            if key:
                out[key] = l
    return out


def find_listing(listings, n):
    for key in (n.get("listing_slug"), str(n.get("id_listing") or ""), (n.get("listing_title") or "").lower()):
        if key and key in listings:
            return listings[key]
    return None


def held_by_others(st, listing_key, exclude_hash):
    total = 0
    for h, rec in st.get("negs", {}).items():
        if h == exclude_hash or rec.get("closed"):
            continue
        f = rec.get("flow") or {}
        if f.get("listing_key") == listing_key:
            total += int(f.get("held") or 0)
    return total


def _fallback_extract(text, local, flow):
    """Very simple parser used only if the free AI is unavailable."""
    import re
    t = text.lower()
    res = {}
    wd = None
    for i, d in enumerate(DAYS):
        if d.lower() in t or re.search(rf"\b{d[:3].lower()}\b", t):
            wd = i
    if wd is None and re.search(r"\b(today|tonight)\b", t):
        wd = local.weekday()
    if wd is None and "tomorrow" in t:
        wd = (local.weekday() + 1) % 7
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        hr = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        mi = int(m.group(2) or 0)
        if wd is None:
            wd = local.weekday()
        res.update(weekday=wd, hour=hr, minute=mi)
        t = t.replace(m.group(0), " ")
    if re.search(r"\b(keep|stick|same|that'?s fine|it'?s fine|yes)\b", t):
        res["keep_time"] = True
        res["keep_quantity"] = True
    if "deliver" in t:
        res["method"] = "delivery"
        m2 = re.search(r"deliver\w*\s+(?:to\s+)?([a-z0-9 \-']{3,40})", text, re.I)
        if m2:
            res["location"] = m2.group(1).strip()
    elif re.search(r"\b(pick ?up|levski)\b", t):
        res["method"] = "pickup"
    elif flow.get("method") == "delivery" and not flow.get("location") and len(text) < 60 and not m:
        res["location"] = text.strip()
    q = re.search(r"\b(\d{1,4})\b", t)
    if q:
        res["quantity"] = int(q.group(1))
    return res


def extract_answers(texts, local, flow, item):
    text = "\n".join(texts)
    known = {k: flow.get(k) for k in ("time", "method", "location", "qty") if flow.get(k)}
    if AI_TOKEN:
        system = (
            "You read a buyer's replies in a Star Citizen marketplace chat and extract order details. "
            "Return ONLY a JSON object with these keys (use null when not mentioned): "
            '"weekday" (0=Monday..6=Sunday, resolve words like tonight/tomorrow using the current date), '
            '"hour" (0-23), "minute" (0-59), '
            '"keep_time" (true if they say to keep/stick with a time they gave before), '
            '"method" ("pickup" or "delivery"), "location" (delivery destination text, as they wrote it), '
            '"quantity" (integer), '
            '"keep_quantity" (true if they want to stay with the quantity they asked for before), '
            '"other_question" (true if they ask the seller something the order questions do not cover, '
            'e.g. price, discount, item condition). Times are US Central unless they say otherwise.'
        )
        user = (f"Now: {local.strftime('%A %Y-%m-%d %H:%M')} (US Central).\nItem: {item}\n"
                f"Already known: {json.dumps(known)}\nBuyer's new message(s):\n{text}")
        body = {"model": AI_MODEL, "temperature": 0, "max_tokens": 200,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        code, js = http("POST", AI_URL, {"Authorization": f"Bearer {AI_TOKEN}", "Accept": "application/json"}, body)
        try:
            raw = js["choices"][0]["message"]["content"].strip()
            raw = raw[raw.index("{"):raw.rindex("}") + 1]
            res = {k: v for k, v in json.loads(raw).items() if v is not None}
            return res
        except Exception:
            log(f"AI extract failed (HTTP {code}): {str(js)[:200]}")
    return _fallback_extract(text, local, flow)


def flow_send(st, h, rec, text):
    ok, status = uex_send_message(h, text)
    f = rec["flow"]
    f.setdefault("bot_texts", []).append(text.strip())
    f["bot_texts"] = f["bot_texts"][-20:]
    f["last_activity"] = time.time()
    if not ok:
        log(f"flow send failed for {h}: {status}")
    return ok


def flow_summary_embed(rec, f, title, color):
    item = f.get("item")
    qty = f.get("qty")
    price = f.get("price") or 0
    lines = []
    if qty:
        lines.append(f"**{qty}x {item}**")
    else:
        lines.append(f"**{item}** (quantity not given)")
    lines.append(f"Time: {f.get('time') or 'not given'}" + ("" if f.get("time_ok", True) else " (outside trading hours, buyer kept it)"))
    if f.get("method") == "delivery":
        lines.append(f"Delivery to: {f.get('location') or 'location not given'}")
    elif f.get("method") == "pickup":
        lines.append(f"Pickup at {PICKUP_LOCATION}")
    else:
        lines.append("Pickup/delivery: not given")
    if qty and price:
        sub = price * qty
        fee = sub * DELIVERY_FEE if f.get("method") == "delivery" else 0
        lines.append(f"{fmt_uec(price)} x {qty} = {fmt_uec(sub)}" + (f" + {int(DELIVERY_FEE*100)}% delivery ({fmt_uec(fee)})" if fee else ""))
        lines.append(f"**Total: {fmt_uec(sub + fee)} aUEC**")
    held, stock = int(f.get("held") or 0), f.get("stock")
    lines.append(f"Stock: {held} on hold for this deal" + (f", {stock} listed in stock" if stock is not None else ""))
    if qty and held < qty:
        lines.append(f"⚠️ Buyer wants {qty} but only {held} could be held - they're waiting in case more free up.")
    return {"title": title[:250], "description": "\n".join(lines)[:3900], "color": color,
            "footer": {"text": "Reply to this message to answer the buyer"}}


def flow_final_text(f, buyer):
    item, qty, price = f["item"], f["qty"], f.get("price") or 0
    where = f"Delivery to {f['location']}" if f["method"] == "delivery" else f"Pickup at Levski"
    out = [f"Thank you {buyer}! The seller has your details and will get back to you soon to confirm.",
           f"{qty}x {item} - {f['time']} - {where}", ""]
    if price:
        sub = price * qty
        out.append(f"{item} - {fmt_uec(price)}/per - {qty}x")
        total = sub
        if f["method"] == "delivery":
            fee = sub * DELIVERY_FEE
            out.append(f"Delivery fee ({int(DELIVERY_FEE*100)}%) - {fmt_uec(fee)}")
            total += fee
        out.append(f"Total: {fmt_uec(total)} aUEC")
    held = int(f.get("held") or 0)
    if f.get("stock") is not None and held < qty:
        out.append("")
        out.append(f"Note: only {held} {'is' if held == 1 else 'are'} available right now. "
                   f"The seller will let you know if more become available.")
    return "\n".join(out).strip()


def flow_start(st, h, rec, n, me, listings):
    l = find_listing(listings, n)
    item = item_name(n.get("listing_title") or (l or {}).get("title"))
    buyer = other_party(n, me)
    key = (l or {}).get("slug") or n.get("listing_slug") or item.lower()
    stock = None
    try:
        stock = int(l["in_stock"]) if l and l.get("in_stock") is not None else None
    except Exception:
        pass
    price = 0.0
    try:
        price = float((l or {}).get("price") or n.get("price") or 0)
    except Exception:
        pass
    rec["flow"] = f = {"status": "collecting", "item": item, "buyer": buyer, "listing_key": key,
                       "stock": stock, "price": price, "held": 0, "started": time.time()}
    if stock is not None and stock <= 0:
        f["status"] = "off"
        flow_send(st, h, rec, f"Hello {buyer}, thanks for your interest in the {item}! This is an automated reply. "
                              f"Unfortunately the {item} is currently sold out. The seller will let you know if more become available.")
        d_post(f"Sold-out item: **{buyer}** opened a deal on **{item}** (0 in stock). Sent them the sold-out message.", ping=False)
        return
    flow_send(st, h, rec,
              f"Hello {buyer}, thanks for your interest in the {item}! This is an automated reply. "
              f"Please answer a few quick questions so the seller can prepare your order:\n"
              f"1. When are you available? Trading hours are {HOURS_TEXT}.\n"
              f"2. Pickup at {PICKUP_LOCATION}, or delivery? Delivery costs an additional {int(DELIVERY_FEE*100)}% fee. If delivery, where to?\n"
              f"3. How many would you like?")


def flow_handle(st, h, rec, texts, local, me, opening=False):
    f = rec["flow"]
    f["last_activity"] = time.time()
    item = f["item"]
    ans = extract_answers(texts, local, f, item)
    got_something = False
    issues = []

    # ---- time
    if ans.get("hour") is not None:
        wd = ans.get("weekday")
        wd = local.weekday() if wd is None else int(wd) % 7
        hr, mi = int(ans["hour"]) % 24, int(ans.get("minute") or 0)
        f.update(weekday=wd, hour=hr, minute=mi, time=fmt_time(wd, hr, mi))
        got_something = True
        if within_hours(wd, hr, mi):
            f["time_ok"] = True
        elif f.get("time_warned_for") == f["time"] or ans.get("keep_time"):
            f["time_ok"] = False  # outside hours but buyer chose to keep it
            f["time_accepted"] = True
        else:
            f["time_ok"] = False
            f["time_accepted"] = False
            f["time_warned_for"] = f["time"]
            issues.append(f"Sorry, that's outside the trading hours ({HOURS_TEXT_SHORT}). Would you like to pick a time "
                          f"within those hours, or keep {f['time']}? The seller may not be available.")
    elif ans.get("keep_time") and f.get("time") and not f.get("time_ok"):
        f["time_accepted"] = True
        got_something = True

    # ---- pickup / delivery
    if ans.get("method") in ("pickup", "delivery"):
        f["method"] = ans["method"]
        got_something = True
    if ans.get("location"):
        loc = str(ans["location"]).strip()[:80]
        f["location"] = loc[:1].upper() + loc[1:]
        if not f.get("method"):
            f["method"] = "delivery"
        got_something = True

    # ---- quantity / stock
    want = ans.get("quantity")
    if want is None and ans.get("keep_quantity") and f.get("asked_qty"):
        want = f["asked_qty"]
    if want is not None:
        got_something = True
        want = max(1, int(want))
        stock = f.get("stock")
        if stock is None:
            f.update(qty=want, held=want, qty_ok=True)
        else:
            others = held_by_others(st, f["listing_key"], h)
            avail = max(0, stock - others)
            reaffirm = f.get("stock_warned") and want == f.get("asked_qty")
            if want <= avail:
                f.update(qty=want, held=want, qty_ok=True)
            elif reaffirm or (avail == 0 and others > 0):
                f.update(qty=want, held=min(want, avail), qty_ok=True)
                if avail == 0 and not reaffirm:
                    issues.append(f"Sorry, all {item} are currently on hold for other deals. "
                                  f"The seller will let you know if any become available.")
            else:
                f.update(asked_qty=want, stock_warned=True, qty_ok=False)
                if others > 0:
                    issues.append(f"Sorry, there are currently {stock} {item} in stock, but {others} are on hold for "
                                  f"another deal. {avail} {'is' if avail == 1 else 'are'} available. Would you like {avail}? Or stay with {want} "
                                  f"in case any become available, the seller can let you know.")
                else:
                    issues.append(f"Sorry, there are currently only {stock} {item} in stock. Would you like {stock}?")

    # ---- buyer asked something the bot can't answer -> hand over to the seller
    if ans.get("other_question") and not got_something:
        f["status"] = "manual"
        mid = d_post(f"<@{DISCORD_USER_ID}> **{f['buyer']}** asked something the assistant can't answer - **{item}**",
                     flow_summary_embed(rec, f, f"Question from {f['buyer']}: " + " / ".join(texts)[:200], 0xE67E22))
        st["notif_map"][mid] = h
        return

    # ---- what's still missing?
    missing = []
    if not f.get("time"):
        missing.append(f"When are you available? Trading hours are {HOURS_TEXT}.")
    if not f.get("method"):
        missing.append(f"Would you like pickup at {PICKUP_LOCATION}, or delivery? Delivery costs an additional "
                       f"{int(DELIVERY_FEE*100)}% fee. If delivery, where to?")
    elif f["method"] == "delivery" and not f.get("location"):
        missing.append(f"Where would you like the {item} delivered?")
    if not f.get("qty") and not f.get("stock_warned"):
        missing.append(f"How many {item} would you like?")

    time_ok = f.get("time_ok") or f.get("time_accepted")
    complete = f.get("time") and time_ok and f.get("method") and (f["method"] == "pickup" or f.get("location")) \
        and f.get("qty") and f.get("qty_ok")

    if complete and not issues:
        flow_send(st, h, rec, flow_final_text(f, f["buyer"]))
        f["status"] = "done"
        mid = d_post(f"<@{DISCORD_USER_ID}> 🔔 **{f['buyer']}** finished the order questions - **{item}**",
                     flow_summary_embed(rec, f, f"New order from {f['buyer']}", 0x2ECC71))
        st["notif_map"][mid] = h
        return

    if not got_something and opening:
        return  # e.g. "I want ts2" - the first message already asks everything
    if not got_something:
        f["unclear"] = int(f.get("unclear") or 0) + 1
        if f["unclear"] >= 2:  # still not getting answers -> stop and hand over
            f["status"] = "manual"
            mid = d_post(f"<@{DISCORD_USER_ID}> The assistant couldn't get answers from **{f['buyer']}** - **{item}**",
                         flow_summary_embed(rec, f, f"Needs you: {f['buyer']}", 0xE67E22))
            st["notif_map"][mid] = h
            return
        issues.insert(0, "Sorry, I didn't catch that.")
    else:
        f["unclear"] = 0

    parts = issues + (["Also, " + missing[0][0].lower() + missing[0][1:]] + missing[1:] if issues and missing else missing)
    if not issues and missing:
        parts = ["Thanks! " + missing[0]] + missing[1:]
    if parts:
        flow_send(st, h, rec, " ".join(parts))


def flow_quiet_check(st):
    for h, rec in st.get("negs", {}).items():
        f = rec.get("flow") or {}
        if f.get("status") != "collecting" or f.get("quiet_pinged") or rec.get("closed"):
            continue
        if time.time() - float(f.get("last_activity") or f.get("started") or time.time()) > QUIET_PING_MINUTES * 60:
            f["quiet_pinged"] = True
            mid = d_post(f"<@{DISCORD_USER_ID}> **{f.get('buyer')}** stopped answering the order questions "
                         f"({QUIET_PING_MINUTES} min) - **{f.get('item')}**",
                         flow_summary_embed(rec, f, f"Partial order from {f.get('buyer')}", 0xF1C40F))
            st["notif_map"][mid] = h


def check_uex(st, first_run):
    negs = uex_get("marketplace_negotiations")
    me = my_username(negs)
    st.setdefault("negs", {})
    st.setdefault("notif_map", {})  # discord message id -> negotiation hash
    seen_msgs = set(st.setdefault("seen_msgs", []))
    new_count = 0
    listings = None
    local = tz_info()[0]

    for n in negs:
        h = n.get("hash")
        if not h:
            continue
        rec = st["negs"].get(h)
        modified = int(n.get("date_modified") or 0)
        is_new_deal = rec is None
        if rec and rec.get("modified") == modified:
            continue  # nothing changed

        try:
            msgs = uex_get("marketplace_negotiations_messages", {"hash": h})
        except Exception as ex:
            log(f"messages for {h}: {ex}")
            msgs = []
        msgs.sort(key=lambda m: (int(m.get("date_added") or 0), int(m.get("id") or 0)))

        rec = rec or {}
        flow = rec.get("flow") or {}
        bot_texts = set(flow.get("bot_texts", []))
        buyer_texts, new_from_them, i_replied_myself = [], [], False
        for m in msgs:
            mid = str(m.get("id"))
            author = (m.get("user_username") or "").lower()
            mine = bool(me) and author == me
            if not mine and m.get("message"):
                buyer_texts.append(m["message"])
            if mid in seen_msgs:
                continue
            seen_msgs.add(mid)
            if mine:
                if m.get("message") and m["message"].strip() not in bot_texts:
                    i_replied_myself = True
            else:
                new_from_them.append(m)

        closed = int(n.get("date_closed") or 0)
        rec.update({"modified": modified, "title": n.get("listing_title"), "slug": n.get("listing_slug"),
                    "other": other_party(n, me), "buyer_texts": buyer_texts[-6:], "closed": closed})
        st["negs"][h] = rec

        if first_run:
            continue  # don't act on old history on the very first run

        am_seller = int(n.get("is_listing_advertiser") or 0) == 1 or (
            bool(me) and (n.get("advertiser_username") or "").lower() == me)

        # You stepped in yourself -> the assistant stops asking questions on this deal
        if rec.get("flow", {}).get("status") == "collecting" and i_replied_myself:
            rec["flow"]["status"] = "manual"

        if is_new_deal and AUTO_FLOW and am_seller and not closed:
            if listings is None:
                listings = fetch_my_listings(me)
            flow_start(st, h, rec, n, me, listings)
            texts = [m["message"] for m in new_from_them if m.get("message")]
            if texts and rec["flow"]["status"] == "collecting":
                flow_handle(st, h, rec, texts, local, me, opening=True)
            continue

        if rec.get("flow", {}).get("status") == "collecting":
            if closed:
                rec["flow"]["status"] = "closed"
                continue
            texts = [m["message"] for m in new_from_them if m.get("message")]
            if texts:
                flow_handle(st, h, rec, texts, local, me)
            continue

        # Normal notifications (no flow running for this deal)
        if is_new_deal:
            text = "\n".join(f"> {m['message']}" for m in new_from_them if m.get("message")) or None
            mid = d_post(f"<@{DISCORD_USER_ID}> New deal on **{n.get('listing_title')}**",
                         notify_embed("New deal opened", n, me, text, 0x2ECC71))
            st["notif_map"][mid] = h
            new_count += 1
        else:
            for m in new_from_them:
                if m.get("message"):
                    body = m["message"]
                    title = f"New message from {m.get('user_name') or m.get('user_username') or 'buyer'}"
                elif m.get("event"):
                    body = f"Deal update: {m['event']}"
                    title = "Deal update"
                else:
                    continue
                mid = d_post(f"<@{DISCORD_USER_ID}> {title} - **{n.get('listing_title')}**",
                             notify_embed(title, n, me, body))
                st["notif_map"][mid] = h
                new_count += 1

    if not first_run:
        flow_quiet_check(st)
    st["seen_msgs"] = list(seen_msgs)[-5000:]
    st["me"] = me
    return negs, me, new_count


def handle_discord_replies(st, negs, me):
    by_hash = {n.get("hash"): n for n in negs}
    st.setdefault("drafts", {})  # draft discord id -> {hash, text, created}
    msgs = d_new_messages(st.get("last_discord_id"))
    for m in msgs:
        st["last_discord_id"] = m["id"]
        if m.get("author", {}).get("id") != DISCORD_USER_ID:
            continue
        ref = (m.get("message_reference") or {}).get("message_id")
        content = (m.get("content") or "").strip()
        if not ref or not content:
            continue
        h = st["notif_map"].get(ref) or (st["drafts"].get(ref) or {}).get("hash")
        if not h:
            continue
        if ref in st["drafts"]:  # user replied to a draft -> replace it
            old = st["drafts"].pop(ref)
            d_edit(ref, content=f"~~Draft replaced~~")
        n = by_hash.get(h) or {"hash": h, "listing_title": st["negs"].get(h, {}).get("title"),
                               "listing_slug": st["negs"].get(h, {}).get("slug")}
        rec = st["negs"].get(h, {})
        if rec.get("closed"):
            d_post("That deal is closed on UEX, so I can't send messages to it.", reply_to=m["id"], ping=False)
            continue
        text, used_ai = draft_reply(content, n, rec.get("buyer_texts", []), me)
        note = "" if used_ai else "\n*(AI unavailable - this is your text as typed)*"
        did = d_post(
            f"**Draft reply to {rec.get('other') or 'buyer'}** about *{rec.get('title') or n.get('listing_title')}*:\n"
            f">>> {text}\n\nReact {YES} to send, {NO} to cancel, or reply to this draft to change it.{note}",
            reply_to=m["id"], ping=False)
        d_react(did, YES)
        d_react(did, NO)
        st["drafts"][did] = {"hash": h, "text": text, "created": time.time()}
        st["notif_map"][did] = h


def handle_approvals(st):
    drafts = st.get("drafts", {})
    for did in list(drafts):
        d = drafts[did]
        if time.time() - d.get("created", 0) > DRAFT_EXPIRY_HOURS * 3600:
            d_edit(did, content=f"~~Draft expired (not sent)~~\n>>> {d['text']}")
            drafts.pop(did)
            continue
        yes = d_reacted_by_me(did, YES)
        if yes is None:
            drafts.pop(did)
            continue
        if yes:
            ok, status = uex_send_message(d["hash"], d["text"])
            if ok:
                d_edit(did, content=f"{YES} **Sent to buyer on UEX:**\n>>> {d['text']}")
            else:
                d_edit(did, content=f"⚠️ **Could not send** ({status}). Nothing was sent.\n>>> {d['text']}")
            drafts.pop(did)
            continue
        if d_reacted_by_me(did, NO):
            d_edit(did, content=f"{NO} ~~Cancelled - not sent~~\n>>> {d['text']}")
            drafts.pop(did)


def run_once():
    st = load_state()
    first_run = not st.get("initialized")

    # Approvals first, so a ✅ is acted on as quickly as possible
    try:
        handle_approvals(st)
    except Exception as ex:
        log(f"approvals: {ex}")

    negs, me = [], st.get("me", "")
    try:
        negs, me, count = check_uex(st, first_run)
        log(f"UEX: {len(negs)} deals checked, {count} notifications sent (me={me or '?'})")
    except Exception as ex:
        log(f"UEX check failed: {ex}")
        if first_run:
            # Don't mark as set up until UEX works, or old history would be re-sent later
            save_state(st)
            return False

    try:
        if first_run:
            # Skip any old Discord messages, then announce we're live
            old = d_new_messages(None)
            if old:
                st["last_discord_id"] = old[-1]["id"]
            mid = d_post(f"<@{DISCORD_USER_ID}> UEX assistant is online. Watching {len(negs)} deal(s). "
                         "New deals get the automated order questions; I'll ping you when a buyer finishes.")
            st["last_discord_id"] = mid
        else:
            handle_discord_replies(st, negs, me)
    except Exception as ex:
        log(f"Discord: {ex}")

    st["initialized"] = True
    st["notif_map"] = dict(list(st.get("notif_map", {}).items())[-2000:])
    save_state(st)
    return True


def main():
    missing = [k for k in ("UEX_APP_TOKEN", "UEX_SECRET_KEY", "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_USER_ID")
               if not os.environ.get(k)]
    if missing:
        log("Missing settings: " + ", ".join(missing))
        sys.exit(1)
    # GitHub only starts this every ~5 minutes, so each run keeps checking
    # every CHECK_EVERY_SECONDS until RUN_MINUTES is up.
    started = time.time()
    while True:
        if not run_once():
            sys.exit(1)
        if time.time() - started + CHECK_EVERY_SECONDS > RUN_MINUTES * 60:
            break
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
