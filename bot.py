"""WB Продвижение → Telegram есеп боты (тек оқу).

Режимдер (автоматты түрде жергілікті уақытқа қарай):
  hourly  – әр сағат дәл :00-де: бүгінгі шығын, тапсырыс, әр карточка бойынша
  daily   – күн сайын 09:00: кешегі толық есеп
  weekly  – дүйсенбі 09:00: өткен аптаның (Дс–Жс) есебі
Қолмен: python bot.py hourly|daily|weekly  (күтпей бірден жібереді)

Ескерту: WB күнді Мәскеу уақытымен санайды (Шымкентте 02:00-де басталады),
сондықтан «бүгін» = WB күні.
"""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
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

TZ = timezone(timedelta(hours=CFG.get("utc_offset", 5)))   # Шымкент
MSK = timezone(timedelta(hours=3))                           # WB уақыты
MAX_CPO = {int(k): v for k, v in CFG.get("max_cpo_campaign", {}).items()}
NAMES = {int(k): v for k, v in CFG.get("product_names", {}).items()}
CNAMES = {int(k): v for k, v in CFG.get("campaign_names", {}).items()}
TOP_CARDS = CFG.get("top_cards", 15)


class WBLimit(Exception):
    """WB лимиті (429) — қайталау нәтиже бермеді."""


def rub_rate():
    """Статистика API бағаны рубльмен береді → теңгеге айналдыру курсы."""
    try:
        r = requests.get("https://open.er-api.com/v6/latest/RUB", timeout=20).json()
        return float(r["rates"]["KZT"])
    except Exception:
        return float(CFG.get("rub_to_kzt_fallback", 6.3))


# ---------------------------------------------------------------- HTTP
def wb(method, url, tries=4, max_wait=65, **kw):
    """WB сұранысы. 429 болса WB айтқан уақытты күтіп қайталайды,
    бірақ ұзақ емес — болмаса WBLimit лақтырады (бот құламайды)."""
    for attempt in range(tries):
        try:
            r = requests.request(method, url, headers={"Authorization": WB_TOKEN},
                                 timeout=60, **kw)
        except requests.RequestException:
            time.sleep(10)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            wait = r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After")
            try:
                wait = float(wait) + 2
            except (TypeError, ValueError):
                wait = 20 * (attempt + 1)
            if wait > max_wait or attempt == tries - 1:
                break
            time.sleep(wait)
            continue
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()
    raise WBLimit(url.split("?")[0])


def tg(text):
    for chunk in split_msg(text):
        for attempt in range(3):
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=30,
            )
            if r.status_code == 429:
                time.sleep(5)
                continue
            r.raise_for_status()
            break


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
    try:
        data = wb("GET", f"{ADV}/adv/v1/promotion/count") or {}
    except Exception:
        return {cid: {"name": n, "status": None} for cid, n in CNAMES.items()}
    res = {}
    for grp in data.get("adverts", []) or []:
        if grp.get("status") not in (9, 11):
            continue
        for a in grp.get("advert_list", []) or []:
            cid = a["advertId"]
            res[cid] = {"name": CNAMES.get(cid, str(cid)), "status": grp["status"]}
    ids = [i for i in res if i not in CNAMES]
    for i in range(0, len(ids), 50):
        try:
            info = wb("GET", f"{ADV}/api/advert/v2/adverts",
                      params={"ids": ",".join(map(str, ids[i:i + 50]))}) or {}
            for a in info.get("adverts", info if isinstance(info, list) else []):
                cid = a.get("id") or a.get("advertId")
                nm = (a.get("settings") or {}).get("name") or a.get("name")
                if cid in res and nm:
                    res[cid]["name"] = nm
        except Exception:
            pass
    return res


def fullstats(ids, d1, d2):
    """Кампания статистикасы d1..d2 — БІР сұраныспен (лимит үнемдеу)."""
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
            time.sleep(61)
    return out


def balance():
    try:
        b = wb("GET", f"{ADV}/adv/v1/balance", tries=2) or {}
        return b.get("net", 0) + b.get("balance", 0) + b.get("bonus", 0)
    except Exception:
        return None


def orders_since(d1):
    """Барлық тапсырыстар (жарнама + органика) d1-ден бастап. Бір сұраныс.
    Лимит болса None қайтарады — есеп бәрібір жіберіледі."""
    try:
        return wb("GET", f"{STAT}/api/v1/supplier/orders",
                  params={"dateFrom": d1.isoformat(), "flag": 0},
                  tries=5, max_wait=70) or []
    except Exception:
        return None


def order_dt(o):
    """Тапсырыс уақыты (WB Мәскеу уақытымен береді) → aware datetime."""
    return datetime.fromisoformat(o["date"][:19]).replace(tzinfo=MSK)


# ---------------------------------------------------------------- aggregation
def day_of(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def agg_ads(stats, camps, d1, d2):
    """Кампания және карточка (nmId) бойынша жарнама, тек d1..d2 күндері."""
    by_camp, by_nm = {}, {}
    for s in stats:
        cid = s.get("advertId")
        c = by_camp.setdefault(cid, {"id": cid,
                                     "name": camps.get(cid, {}).get("name") or CNAMES.get(cid, str(cid)),
                                     "status": camps.get(cid, {}).get("status"),
                                     "sum": 0, "orders": 0, "sum_price": 0,
                                     "views": 0, "clicks": 0})
        for day in s.get("days") or []:
            dd = day_of(day.get("date"))
            if dd is None or not (d1 <= dd <= d2):
                continue
            for k in ("sum", "orders", "sum_price", "views", "clicks"):
                c[k] += day.get(k) or 0
            for app in day.get("apps") or []:
                for nm in app.get("nms") or app.get("nm") or []:
                    nid = nm.get("nmId")
                    n = by_nm.setdefault(nid, {"name": nm.get("name", ""), "sum": 0,
                                               "orders": 0, "sum_price": 0})
                    n["sum"] += nm.get("sum") or 0
                    n["orders"] += nm.get("orders") or 0
                    n["sum_price"] += nm.get("sum_price") or 0
    return by_camp, by_nm


def agg_orders(ords, rate, d1, d2):
    tot = {"cnt": 0, "sum": 0, "cancel": 0}
    by_nm = {}
    for o in ords:
        if not (d1 <= order_dt(o).date() <= d2):
            continue
        price = (o.get("priceWithDisc") or o.get("finishedPrice") or 0) * rate
        n = by_nm.setdefault(o["nmId"], {"cnt": 0, "sum": 0,
                                         "subject": o.get("subject", ""),
                                         "art": o.get("supplierArticle", "")})
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


def card_name(nm, ad=None, od=None):
    nm_ = NAMES.get(nm) or (ad or {}).get("name") or (od or {}).get("subject") or ""
    art = (od or {}).get("art")
    if not nm_:
        nm_ = art or str(nm)
    return nm_[:32]


def build(kind, label, d1, d2, camps, stats, ords, rate, bal, send_at):
    by_camp, ad_nm = agg_ads(stats, camps, d1, d2)
    have_orders = ords is not None
    tot_o, ord_nm = agg_orders(ords or [], rate, d1, d2)

    spend = sum(c["sum"] for c in by_camp.values())
    ad_ord = sum(c["orders"] for c in by_camp.values())

    period = f"{d1:%d.%m.%Y}" if d1 == d2 else f"{d1:%d.%m}–{d2:%d.%m.%Y}"
    L = [f"<b>{escape(label)}</b>", f"🗓 {period}", ""]

    # --- Жалпы
    L.append(f"💸 Жарнамаға кетті: <b>{m(spend)}</b>")
    if have_orders:
        L.append(f"🛒 Барлық тапсырыс: <b>{tot_o['cnt']} дана / {m(tot_o['sum'])}</b>"
                 + (f" (бас тарту {tot_o['cancel']})" if tot_o["cancel"] else ""))
        L.append(f"📢 Оның ішінде жарнамадан: {ad_ord} дана")
        L.append(f"📊 ДРР (шығын ÷ барлық тапсырыс сомасы): <b>{pct(spend, tot_o['sum'])}</b>")
    else:
        L.append(f"📢 Жарнамадан тапсырыс: {ad_ord} дана")
        L.append("🛒 Барлық тапсырыс: WB уақытша бермеді, келесі есепте болады")
    if ad_ord:
        L.append(f"🎯 1 тапсырысқа жарнама: {m(spend / ad_ord)}")
    if kind == "hourly" and have_orders:
        h_end = send_at
        h_start = h_end - timedelta(hours=1)
        last_h = [o for o in ords if not o.get("isCancel")
                  and h_start <= order_dt(o) < h_end]
        L.append(f"⏰ Соңғы 1 сағатта ({h_start.astimezone(TZ):%H:%M}–{h_end.astimezone(TZ):%H:%M}): "
                 f"<b>{len(last_h)} тапсырыс</b>")
    if bal is not None:
        L.append(f"💰 Жарнама балансы: {m(bal)}")

    # --- Әр карточка бойынша
    ids = set(nm for nm, a in ad_nm.items() if a["sum"] > 0 or a["orders"] > 0)
    ids |= set(nm for nm, o in ord_nm.items() if o["cnt"] > 0)
    cards = []
    for nm in ids:
        a = ad_nm.get(nm, {"sum": 0, "orders": 0, "sum_price": 0, "name": ""})
        o = ord_nm.get(nm, {"cnt": 0, "sum": 0})
        cards.append((nm, a, o))
    cards.sort(key=lambda x: (-x[1]["sum"], -x[2]["cnt"]))
    if cards:
        L += ["", "<b>📦 Әр карточка бойынша:</b>"]
        for i, (nm, a, o) in enumerate(cards[:TOP_CARDS], 1):
            L.append(f"\n<b>{i}. {escape(card_name(nm, a, ord_nm.get(nm)))}</b> (арт. {nm})")
            L.append(f"   💸 Жарнамаға: {m(a['sum'])}")
            if have_orders:
                L.append(f"   🛒 Тапсырыс: {o['cnt']} дана / {m(o['sum'])}"
                         f" (жарнамадан {a['orders']})")
            else:
                L.append(f"   📢 Жарнамадан тапсырыс: {a['orders']} дана")
            extra = []
            if a["orders"]:
                extra.append(f"1 тапсырыс {m(a['sum'] / a['orders'])}")
            if have_orders and o["sum"] and a["sum"]:
                extra.append(f"ДРР {pct(a['sum'], o['sum'])}")
            if extra:
                L.append("   🎯 " + " · ".join(extra))
        if len(cards) > TOP_CARDS:
            rest = cards[TOP_CARDS:]
            L.append(f"\n…тағы {len(rest)} карточка: жарнама {m(sum(a['sum'] for _, a, _ in rest))}, "
                     f"тапсырыс {sum(o['cnt'] for _, _, o in rest)} дана")

    # --- Кампаниялар (күндік/апталық есепте ғана)
    if kind != "hourly":
        rows = sorted((c for c in by_camp.values() if c["sum"] > 0), key=lambda c: -c["sum"])
        if rows:
            L += ["", "<b>📣 Кампаниялар:</b>"]
            for c in rows:
                L.append(f"• {escape(c['name'][:28])}: {m(c['sum'])} → {c['orders']} тапсырыс, "
                         f"CTR {pct(c['clicks'], c['views'])}")

    # --- Ескертулер
    W = []
    for c in by_camp.values():
        lim = MAX_CPO.get(c["id"])
        if lim and c["orders"] and c["sum"] / c["orders"] > lim:
            W.append(f"🔴 {escape(c['name'][:28])}: 1 тапсырыс {m(c['sum'] / c['orders'])} "
                     f"(шек {m(lim)})")
    min_spend = CFG.get("alert_zero_orders_spend", 3000)
    for c in by_camp.values():
        if c["orders"] == 0 and c["sum"] >= min_spend:
            W.append(f"🟠 {escape(c['name'][:28])}: {m(c['sum'])} жұмсалды, тапсырыс жоқ")
    if bal is not None and bal < CFG.get("alert_low_balance", 20000):
        W.append(f"🟡 Жарнама балансы аз: {m(bal)}")
    for cid in CFG.get("must_be_active", []):
        st = camps.get(cid, {}).get("status")
        if st is not None and st != 9:
            W.append(f"⏸ {escape(CNAMES.get(cid, str(cid)))} кампаниясы тоқтап тұр")
    if W:
        L += ["", "<b>⚠️ Ескертулер:</b>"] + W
    return "\n".join(L)


# ---------------------------------------------------------------- main
def target_time(now):
    """Есеп қай :00-ге арналған. Cron :50-де іске қосылады.
    :45–:59 → келесі сағат (күтеміз); :00–:29 → GitHub кешікті, бірден жібереміз;
    :30–:44 → тым кеш, келесі іске қосылу жібереді (қайталанбау үшін)."""
    base = now.replace(minute=0, second=0, microsecond=0)
    if now.minute >= 45:
        return base + timedelta(hours=1)
    if now.minute < 30:
        return base
    return None


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "auto" else None
    now = datetime.now(TZ)
    if arg:
        modes, send_at = [arg], now
    else:
        send_at = target_time(now)
        if send_at is None:
            print(f"{now:%H:%M} — тым кеш іске қосылды, өткізіп жібереміз")
            return
        modes = ["hourly"]
        if send_at.hour == CFG.get("daily_hour", 9):
            modes.append("daily")
            if send_at.weekday() == 0:
                modes.append("weekly")

    # WB күні (Мәскеу) — есеп жіберілетін сәттегі
    today = send_at.astimezone(MSK).date()
    periods = {}
    for md in modes:
        if md == "hourly":
            periods[md] = (today, today, f"⏱ Сағаттық есеп — {send_at:%H:%M}")
        elif md == "daily":
            y = today - timedelta(days=1)
            periods[md] = (y, y, "📅 Күндік есеп (кеше)")
        elif md == "weekly":
            mon = today - timedelta(days=today.weekday() + 7)
            periods[md] = (mon, mon + timedelta(days=6), "📈 Апталық есеп (Дс–Жс)")
        else:
            raise SystemExit(f"Белгісіз режим: {md}")

    # Барлық деректі :00-ге дейін алдын ала жинаймыз (әр API-ге бір-бір сұраныс)
    d_from = min(p[0] for p in periods.values())
    d_to = max(p[1] for p in periods.values())
    camps = campaigns()
    try:
        stats = fullstats(list(camps), d_from, d_to) if camps else []
    except Exception:
        stats = []
    bal = balance()
    ords = orders_since(d_from)
    rate = rub_rate()

    # Дәл :00-ді күтеміз
    wait = (send_at - datetime.now(TZ)).total_seconds()
    if wait > 0:
        print(f"Деректер дайын, {send_at:%H:%M}-ге дейін {wait:.0f} с күтеміз")
        time.sleep(wait)

    for md in modes:
        d1, d2, label = periods[md]
        tg(build(md, label, d1, d2, camps, stats, ords, rate, bal, send_at))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # тек нақты қате болса ғана, қысқа хабар
        print(repr(e))
        if TG_TOKEN and TG_CHAT:
            try:
                tg(f"❗ Есеп жіберілмеді: {escape(type(e).__name__)}. Келесі сағатта қайта тырысады.")
            except Exception:
                pass
        sys.exit(1)
