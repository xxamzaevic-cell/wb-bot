"""WB Продвижение → Telegram есеп боты (тек оқу).

Режимдер (автоматты түрде жергілікті уақытқа қарай):
  hourly  – әр сағат: бүгінгі шығын, тапсырыс, ДРР, баланс, ескертулер
  daily   – күн сайын 09:00: кешегі толық есеп (кампания және тауар бойынша)
  weekly  – дүйсенбі 09:00: өткен аптаның (Дс–Жс) есебі
Қолмен: python bot.py hourly|daily|weekly
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from html import escape

import requests

ADV = "https://advert-api.wildberries.ru"
STAT = "https://statistics-api.wildberries.ru"

WB_TOKEN = os.environ.get("WB_TOKEN", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

TZ = timezone(timedelta(hours=CFG.get("utc_offset", 5)))
MAX_CPO = {int(k): v for k, v in CFG.get("max_cpo", {}).items()}
NAMES = {int(k): v for k, v in CFG.get("product_names", {}).items()}


# ---------------------------------------------------------------- HTTP
def wb(method, url, **kw):
    """WB сұранысы: 429 болса күтіп қайталайды."""
    for attempt in range(4):
        r = requests.request(method, url, headers={"Authorization": WB_TOKEN}, timeout=60, **kw)
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def tg(text):
    for chunk in split_msg(text):
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": chunk, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        r.raise_for_status()


def split_msg(text, limit=3900):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


# ---------------------------------------------------------------- WB data
def campaigns():
    """Белсенді (9) және тоқтатылған (11) кампаниялар: {id: {'name','status'}}"""
    data = wb("GET", f"{ADV}/adv/v1/promotion/count") or {}
    res = {}
    for grp in data.get("adverts", []) or []:
        if grp.get("status") not in (9, 11):
            continue
        for a in grp.get("advert_list", []) or []:
            res[a["advertId"]] = {"name": str(a["advertId"]), "status": grp["status"]}
    ids = list(res)
    for i in range(0, len(ids), 50):
        try:
            info = wb("POST", f"{ADV}/adv/v1/promotion/adverts", json=ids[i:i + 50]) or []
            for a in info:
                if a.get("advertId") in res:
                    res[a["advertId"]]["name"] = a.get("name") or res[a["advertId"]]["name"]
        except requests.HTTPError:
            pass
    return res


def fullstats(ids, d1, d2):
    """Кампания статистикасы d1..d2 (date). v3, болмаса v2."""
    out = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            data = wb("GET", f"{ADV}/adv/v3/fullstats", params={
                "ids": ",".join(map(str, chunk)),
                "beginDate": d1.isoformat(), "endDate": d2.isoformat()})
        except requests.HTTPError:
            data = wb("POST", f"{ADV}/adv/v2/fullstats", json=[
                {"id": c, "interval": {"begin": d1.isoformat(), "end": d2.isoformat()}}
                for c in chunk])
        out.extend(data or [])
        if i + 50 < len(ids):
            time.sleep(61)  # лимит: минутына 1 сұраныс
    return out


def balance():
    try:
        b = wb("GET", f"{ADV}/adv/v1/balance") or {}
        return b.get("net", 0) + b.get("balance", 0) + b.get("bonus", 0)
    except Exception:
        return None


def orders(d1, d2):
    """Барлық тапсырыстар (жарнама + органика) d1..d2 аралығында."""
    data = wb("GET", f"{STAT}/api/v1/supplier/orders",
              params={"dateFrom": d1.isoformat(), "flag": 0}) or []
    res = []
    for o in data:
        day = datetime.fromisoformat(o["date"][:19]).date()
        if d1 <= day <= d2:
            res.append(o)
    return res


# ---------------------------------------------------------------- aggregation
def agg_campaigns(stats, camps):
    """Кампания және тауар бойынша жиынтық."""
    by_camp, by_nm = {}, {}
    for s in stats:
        cid = s.get("advertId")
        c = by_camp.setdefault(cid, {"name": camps.get(cid, {}).get("name", str(cid)),
                                     "status": camps.get(cid, {}).get("status"),
                                     "sum": 0, "orders": 0, "sum_price": 0,
                                     "views": 0, "clicks": 0})
        for k in ("sum", "orders", "sum_price", "views", "clicks"):
            c[k] += s.get(k) or 0
        for day in s.get("days") or []:
            for app in day.get("apps") or []:
                for nm in app.get("nms") or app.get("nm") or []:
                    n = by_nm.setdefault(nm["nmId"], {"name": nm.get("name", ""), "sum": 0,
                                                     "orders": 0, "sum_price": 0})
                    n["sum"] += nm.get("sum") or 0
                    n["orders"] += nm.get("orders") or 0
                    n["sum_price"] += nm.get("sum_price") or 0
    return by_camp, by_nm


def agg_orders(ords):
    tot = {"cnt": 0, "sum": 0, "cancel": 0}
    by_nm = {}
    for o in ords:
        price = o.get("priceWithDisc") or o.get("finishedPrice") or 0
        n = by_nm.setdefault(o["nmId"], {"cnt": 0, "sum": 0, "subject": o.get("subject", "")})
        if o.get("isCancel"):
            tot["cancel"] += 1
            continue
        tot["cnt"] += 1
        tot["sum"] += price
        n["cnt"] += 1
        n["sum"] += price
    return tot, by_nm


# ---------------------------------------------------------------- formatting
def m(x):
    return f"{round(x):,}".replace(",", " ") + " ₸"


def pct(a, b):
    return f"{a / b * 100:.1f}%" if b else "—"


def nm_name(nm, fallback=""):
    return NAMES.get(nm) or fallback or str(nm)


def build(title, d1, d2, camps, detailed):
    stats = fullstats(list(camps), d1, d2) if camps else []
    by_camp, by_nm = agg_campaigns(stats, camps)
    tot_o, ord_nm = agg_orders(orders(d1, d2))
    bal = balance()

    spend = sum(c["sum"] for c in by_camp.values())
    ad_ord = sum(c["orders"] for c in by_camp.values())
    ad_sum = sum(c["sum_price"] for c in by_camp.values())

    L = [f"<b>{escape(title)}</b>",
         f"🗓 {d1:%d.%m}" + (f"–{d2:%d.%m.%Y}" if d1 != d2 else f".{d1:%Y}"), "",
         f"💸 Жарнама шығыны: <b>{m(spend)}</b>",
         f"🛒 Барлық тапсырыс: <b>{tot_o['cnt']} дана / {m(tot_o['sum'])}</b>"
         + (f" (бас тарту {tot_o['cancel']})" if tot_o["cancel"] else ""),
         f"📢 Жарнамадан тапсырыс: {ad_ord} дана / {m(ad_sum)}",
         f"📊 ДРР (барлық тапсырысқа): <b>{pct(spend, tot_o['sum'])}</b>",
         f"🎯 1 тапсырысқа жарнама (CPO): {m(spend / ad_ord) if ad_ord else '—'}"]
    if bal is not None:
        L.append(f"💰 Жарнама балансы: {m(bal)}")

    # Қай кампанияға қанша кетті
    rows = sorted(by_camp.values(), key=lambda c: -c["sum"])
    rows = [c for c in rows if c["sum"] > 0]
    if rows:
        L += ["", "<b>Ақша қайда кетті (кампания):</b>"]
        for c in rows[: 15 if detailed else 6]:
            cpo = c["sum"] / c["orders"] if c["orders"] else None
            L.append(f"• {escape(c['name'][:28])}: {m(c['sum'])} → {c['orders']} зак., "
                     f"CPO {m(cpo) if cpo else '—'}, ДРР {pct(c['sum'], c['sum_price'])}")

    # Тауар бойынша (органика + жарнама)
    if detailed and ord_nm:
        L += ["", "<b>Тауар бойынша:</b>"]
        for nm, o in sorted(ord_nm.items(), key=lambda x: -x[1]["sum"])[:12]:
            a = by_nm.get(nm, {"sum": 0, "orders": 0})
            L.append(f"• {escape(nm_name(nm, o['subject'])[:26])}: {o['cnt']} зак. / {m(o['sum'])}; "
                     f"жарнама {m(a['sum'])}, ДРР {pct(a['sum'], o['sum'])}")

    # Ескертулер
    W = []
    for nm, lim in MAX_CPO.items():
        a = by_nm.get(nm)
        if a and a["orders"] and a["sum"] / a["orders"] > lim:
            W.append(f"🔴 {escape(nm_name(nm))}: CPO {m(a['sum'] / a['orders'])} > шек {m(lim)}")
    min_spend = CFG.get("alert_zero_orders_spend", 3000)
    for c in rows:
        if c["orders"] == 0 and c["sum"] >= min_spend:
            W.append(f"🟠 {escape(c['name'][:28])}: {m(c['sum'])} жұмсалды, тапсырыс жоқ")
    if bal is not None and bal < CFG.get("alert_low_balance", 20000):
        W.append(f"🟡 Жарнама балансы аз: {m(bal)}")
    for cid in CFG.get("must_be_active", []):
        st = camps.get(cid, {}).get("status")
        if st != 9:
            W.append(f"⏸ Кампания {cid} белсенді емес")
    if W:
        L += ["", "<b>⚠️ Ескертулер:</b>"] + W
    else:
        L += ["", "✅ Ескерту жоқ"]
    return "\n".join(L)


# ---------------------------------------------------------------- main
def main():
    now = datetime.now(TZ)
    today = now.date()
    mode = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "auto" else None
    modes = [mode] if mode else ["hourly"]
    if not mode and now.hour == CFG.get("daily_hour", 9):
        modes.append("daily")
        if now.weekday() == 0:
            modes.append("weekly")

    camps = campaigns()
    for md in modes:
        if md == "hourly":
            text = build(f"⏱ Сағаттық есеп ({now:%H:%M})", today, today, camps, False)
        elif md == "daily":
            y = today - timedelta(days=1)
            text = build("📅 Күндік есеп (кеше)", y, y, camps, True)
        elif md == "weekly":
            mon = today - timedelta(days=today.weekday() + 7)
            text = build("📈 Апталық есеп (Дс–Жс)", mon, mon + timedelta(days=6), camps, True)
        else:
            raise SystemExit(f"Белгісіз режим: {md}")
        tg(text)
        if md != modes[-1]:
            time.sleep(61)  # WB лимиті


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # қате болса да Telegram-ға хабар жібереміз
        if TG_TOKEN and TG_CHAT:
            tg(f"❗ Бот қатесі: {escape(str(e))[:500]}")
        raise
