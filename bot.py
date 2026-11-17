"""WB Продвижение → Telegram есеп боты (тек оқу).

Режимдер (автоматты түрде жергілікті уақытқа қарай):
  hourly  – әр сағат дәл :00-де: бүгінгі шығын, тапсырыс, әр карточка бойынша
  daily   – күн сайын 09:00: кешегі жарнама есебі + WB қаржы есебі (дайын болғанда)
  weekly  – дүйсенбі: өткен аптаның жарнама + қаржы есебі
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
EVERY = int(CFG.get("report_every_hours", 2))   # сағаттық есеп жиілігі


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
        h_start = h_end - timedelta(hours=EVERY)
        last_h = [o for o in ords if not o.get("isCancel")
                  and h_start <= order_dt(o) < h_end]
        L.append(f"⏰ Соңғы {EVERY} сағатта ({h_start.astimezone(TZ):%H:%M}–{h_end.astimezone(TZ):%H:%M}): "
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


# ---------------------------------------------------------------- қаржы есебі (WB finance API)
FIN = "https://finance-api.wildberries.ru"
VAT_RATE = float(CFG.get("vat_rate", 16))          # ИП НДС төлеушісі
TAX_K = VAT_RATE / 100                              # пайдадан 16%
STATE_FILE = os.path.join(HERE, "state", "state.json")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def load_costs():
    """Себестоимость: GitHub Secret COSTS_CSV (жасырын), болмаса costs.csv.
    Формат: nmId;cost (₸ / дана), әр жолда бір артикул."""
    res = {}
    try:
        text = os.environ.get("COSTS_CSV", "").strip()
        if not text:
            with open(os.path.join(HERE, "costs.csv"), encoding="utf-8") as f:
                text = f.read()
        for line in text.splitlines():
            if True:
                parts = [p.strip() for p in line.strip().replace("\t", ";").split(";")]
                if len(parts) < 2 or not parts[0].isdigit() or not parts[1]:
                    continue
                try:
                    res[parts[0]] = float(parts[1].replace(",", ".").replace(" ", ""))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    for k, v in CFG.get("cost_price", {}).items():
        res[str(k)] = float(v)
    return res


def num(x):
    """finance API ақшаны жол (string) ретінде береді."""
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def fin_list(d1, d2, period):
    """Есептер тізімі (ресми жиынтықтар: bankPaymentSum = Итого к оплате)."""
    return wb("POST", f"{FIN}/api/finance/v1/sales-reports/list",
              json={"dateFrom": d1.isoformat(), "dateTo": d2.isoformat(),
                    "period": period, "limit": 1000}, tries=3) or []


def fin_detail(d1, d2, period):
    """Есеп детализациясы, бөліктерімен (rrdId). Лимит: минутына 1 сұраныс."""
    rows, rrd = [], 0
    while True:
        part = wb("POST", f"{FIN}/api/finance/v1/sales-reports/detailed",
                  json={"dateFrom": d1.isoformat(), "dateTo": d2.isoformat(),
                        "period": period, "limit": 100000, "rrdId": rrd}, tries=3)
        if not part:
            break
        rows.extend(part)
        if len(part) < 100000:
            break
        rrd = part[-1]["rrdId"]
        time.sleep(62)
    return rows


AD_WORDS = ("продвижен", "реклам", "вб.продв")


def build_finance(title, d1, d2, reports, rows, ords, ad_nm, costs):
    cur = (reports[0].get("currency") if reports else None) or (rows[0].get("currency") if rows else "KZT")
    k = 1.0 if cur == "KZT" else rub_rate()   # бәрін ₸-ге келтіреміз

    def card(nm, r):
        return P.setdefault(nm, {"art": r.get("vendorCode", ""), "name": r.get("subjectName", ""),
                                 "sold": 0, "ret": 0, "otkaz": 0, "retail": 0, "price": 0, "pay": 0,
                                 "logi": 0, "stor": 0, "pen": 0, "ded": 0, "acc": 0, "add": 0})

    P = {}
    ded_types = {}
    ad_ded = 0.0
    for r in rows:
        nm = r.get("nmId") or 0
        c = card(nm, r)
        doc = (r.get("docTypeName") or "").lower()
        q = r.get("quantity") or 0
        sign = -1 if doc.startswith("возврат") else 1
        if doc.startswith("продажа") and q:
            c["sold"] += q
        elif doc.startswith("возврат") and q:
            c["ret"] += q
        if doc:
            # WB кабинетіндегі «Продажа» = «Цена розничная с учётом согласованной скидки» × дана
            c["retail"] += sign * num(r.get("retailAmount")) * k        # кабинеттегі «Продажа» (WB реализовал, Пр)
            c["price"] += sign * num(r.get("retailPriceWithDisc")) * abs(q or 0) * k   # сіздің бағаңыз
            c["pay"] += sign * num(r.get("forPay")) * k
        c["otkaz"] += r.get("returnAmount") or 0
        c["logi"] += num(r.get("deliveryService")) * k   # rebillLogisticCost осының ішінде, қайта қоспаймыз
        c["stor"] += num(r.get("paidStorage")) * k
        c["pen"] += num(r.get("penalty")) * k
        c["acc"] += num(r.get("paidAcceptance")) * k
        c["add"] += num(r.get("additionalPayment")) * k
        d = num(r.get("deduction")) * k
        if d:
            c["ded"] += d
            bt = (r.get("bonusTypeName") or r.get("sellerOperName") or "Удержание").strip()
            ded_types[bt] = ded_types.get(bt, 0) + d
            if any(w in bt.lower() for w in AD_WORDS):
                ad_ded += d

    # Тапсырыстар (статистика API) — дана саны
    o_cnt, o_cancel = {}, {}
    for o in ords or []:
        if not (d1 <= order_dt(o).date() <= d2):
            continue
        tgt = o_cancel if o.get("isCancel") else o_cnt
        tgt[o["nmId"]] = tgt.get(o["nmId"], 0) + 1

    T = {x: 0.0 for x in ("sold", "ret", "otkaz", "retail", "price", "pay", "logi", "stor", "pen",
                          "ded", "acc", "add", "cost", "ad", "profit")}
    no_cost, lines = set(), []
    all_nm = set(P) | set(o_cnt) | set(nm for nm, a in ad_nm.items() if a["sum"] > 0)
    for nm in all_nm:
        c = P.get(nm) or {"art": "", "name": "", "sold": 0, "ret": 0, "otkaz": 0, "retail": 0, "price": 0,
                          "pay": 0, "logi": 0, "stor": 0, "pen": 0, "ded": 0, "acc": 0, "add": 0}
        cost_u = costs.get(str(nm)) if nm else None
        net_q = c["sold"] - c["ret"]
        if nm and net_q and cost_u is None:
            no_cost.add(str(nm))
        cost = (cost_u or 0) * net_q
        ad = ad_nm.get(nm, {}).get("sum", 0)
        itog = c["pay"] - c["logi"] - c["stor"] - c["pen"] - c["ded"] - c["acc"] + c["add"]
        p0 = itog - cost - ad
        profit = p0 - max(p0, 0) * TAX_K    # пайдадан 16% (пайда оң болса)
        # заказдан болжам: 1 сатылымның таза түсімі × тапсырыс саны
        n_ord = o_cnt.get(nm, 0)
        forecast = None
        if n_ord and c["sold"]:
            per_u = (c["pay"] - c["logi"]) / max(c["sold"], 1) - (cost_u or 0)
            f0 = per_u * n_ord - ad
            forecast = f0 - max(f0, 0) * TAX_K
        for x in ("sold", "ret", "otkaz", "retail", "price", "pay", "logi", "stor", "pen", "ded", "acc", "add"):
            T[x] += c[x]
        T["cost"] += cost
        T["ad"] += ad
        T["profit"] += profit
        lines.append((nm, c, n_ord, o_cancel.get(nm, 0), itog, cost, ad, profit, forecast, cost_u))

    itog_calc = T["pay"] - T["logi"] - T["stor"] - T["pen"] - T["ded"] - T["acc"] + T["add"]
    bank = sum(num(r.get("bankPaymentSum")) for r in reports) * k if reports else None
    itog = bank if bank is not None else itog_calc
    # жарнама ұстаулармен қосарланбауы үшін
    profit_before = itog - T["cost"] - T["ad"] + ad_ded
    vat_total = max(profit_before, 0) * TAX_K
    profit_total = profit_before - vat_total

    period = f"{d1:%d.%m.%Y}" if d1 == d2 else f"{d1:%d.%m}–{d2:%d.%m.%Y}"
    L = [f"<b>{escape(title)}</b>", f"🗓 {period} · WB қаржы есебі бойынша", ""]
    L += [f"🛒 Тапсырыс: <b>{sum(o_cnt.values())} дана</b> · ❌ отмена {sum(o_cancel.values())}",
          f"✅ Выкуп (сатылды): <b>{int(T['sold'])} дана</b> · ↩️ қайтару {int(T['ret'])} · "
          f"🚫 отказ {int(T['otkaz'])}",
          "",
          "<b>💵 Ақша (WB-мен бірдей):</b>",
          f"Сатылым (кабинеттегі «Продажа»): {m(T['retail'])}",
          f"Сіздің бағаңызбен сатылым: {m(T['price'])}",
          f"WB комиссиясы + эквайринг (сіздің бағаңыздан): −{m(T['price'] - T['pay'])}"
          + (f" ({pct(T['price'] - T['pay'], T['price'])})" if T['price'] else ""),
          f"К перечислению: {m(T['pay'])}",
          f"Логистика: −{m(T['logi'])}",
          f"Сақтау (хранение): −{m(T['stor'])}",
          f"Айыппұл (штраф): −{m(T['pen'])}",
          f"Ұстаулар (удержания): −{m(T['ded'])}",
          f"Платная приёмка: −{m(T['acc'])}"]
    if T["add"]:
        L.append(f"Қосымша төлем (доплата): +{m(T['add'])}")
    L.append(f"<b>Итого к оплате: {m(itog)}</b>")
    if bank is not None and abs(bank - itog_calc) > 1:
        L.append(f"<i>(жолдар бойынша есептегенде {m(itog_calc)}, айырма WB есебіндегі басқа операциялар)</i>")
    if ded_types:
        L.append("Ұстаулар ішінде: " + "; ".join(f"{escape(n[:40])} {m(v)}" for n, v in
                                                  sorted(ded_types.items(), key=lambda x: -x[1])[:5]))
    L += ["",
          f"📦 Себестоимость: −{m(T['cost'])}",
          f"📢 Жарнама: −{m(T['ad'])}" + (f" (оның {m(ad_ded)} ұстауларда бар, қайта алынбады)" if ad_ded else ""),
          f"💰 Пайда (салыққа дейін): {m(profit_before)}",
          f"🧾 Салық {VAT_RATE:g}% (пайдадан): −{m(vat_total)}",

          f"💰 <b>Таза пайда (16%-дан кейін): {m(profit_total)}</b>"]
    if T["retail"]:
        L.append(f"📊 Маржа: {pct(profit_total, T['retail'])}")
    fc = sum(x[8] for x in lines if x[8] is not None)
    if fc:
        L.append(f"🔮 Заказдан күтілетін пайда (болжам): {m(fc)}")

    lines.sort(key=lambda x: (-(x[1]["sold"]), -x[2]))
    L += ["", "<b>📦 Артикул бойынша:</b>"]
    top = CFG.get("top_cards_sales", 25)
    for nm, c, n_ord, n_can, it, cost, ad, profit, fcst, cost_u in lines[:top]:
        if not nm:
            continue
        name = NAMES.get(nm) or c["art"] or c["name"] or str(nm)
        L.append(f"\n<b>{escape(name[:32])}</b> (арт. {nm})")
        L.append(f"   🛒 заказ {n_ord} · ❌ отмена {n_can} · ✅ выкуп {c['sold']} · ↩️ қайтару {c['ret']}"
                 + (f" · 🚫 отказ {c['otkaz']}" if c["otkaz"] else ""))
        if c["retail"]:
            L.append(f"   💵 Сатылым {m(c['retail'])} → WB аударады {m(c['pay'])}")
        costs_line = []
        if c["logi"]: costs_line.append(f"логистика {m(c['logi'])}")
        if c["stor"]: costs_line.append(f"сақтау {m(c['stor'])}")
        if c["pen"]: costs_line.append(f"штраф {m(c['pen'])}")
        if c["ded"]: costs_line.append(f"ұстау {m(c['ded'])}")
        if cost: costs_line.append(f"себест. {m(cost)}")
        if ad: costs_line.append(f"жарнама {m(ad)}")
        if costs_line:
            L.append("   ➖ " + " · ".join(costs_line))
        L.append(f"   💰 Таза пайда (16%-дан кейін): <b>{m(profit)}</b>" + (" ⚠️себест. жоқ" if cost_u is None and c["sold"] else "")
                 + (f" · заказдан болжам {m(fcst)}" if fcst is not None else ""))
    rest = [x for x in lines[top:] if x[0]]
    if rest:
        L.append(f"\n…тағы {len(rest)} артикул, пайдасы {m(sum(x[7] for x in rest))}")
    gen = P.get(0)
    if gen and (gen["stor"] or gen["pen"] or gen["ded"] or gen["acc"] or gen["logi"]):
        L.append(f"\n🏷 Артикулсыз шығындар: сақтау {m(gen['stor'])}, штраф {m(gen['pen'])}, "
                 f"ұстау {m(gen['ded'])}, логистика {m(gen['logi'])}, приёмка {m(gen['acc'])}")
    if no_cost:
        L.append(f"\n⚠️ Себестоимость жоқ артикулдар ({len(no_cost)}): "
                 + ", ".join(sorted(no_cost)[:15]) + (" …" if len(no_cost) > 15 else "")
                 + " — пайда себестоимостьсыз есептелді")
    if cur != "KZT":
        L.append(f"<i>WB есебі {escape(cur)} валютасында, ₸-ге {k:.2f} курсымен айналдырылды</i>")
    return "\n".join(L)


def finance_job(md, d1, d2, camps, ords, costs, force=False):
    """Қаржы есебі WB-да ТОЛЫҚ дайын болса ғана жібереді. True — жіберілді.
    Дайын болмаса ештеңе жібермейді, келесі сағатта қайта тексереді."""
    if ords is None:
        print(f"{md}: тапсырыс деректері жоқ (WB лимиті) — келесі сағатта")
        return False
    period = "daily" if md == "daily" else "weekly"
    reports = fin_list(d1, d2, period)
    reports = [r for r in reports if str(r.get("dateFrom", ""))[:10] >= d1.isoformat()
               and str(r.get("dateTo", ""))[:10] <= d2.isoformat()]
    if not reports:
        print(f"{md}: WB қаржы есебі әлі дайын емес")
        return False
    time.sleep(62)
    rows = fin_detail(d1, d2, period)
    if not rows:
        print(f"{md}: детализация әлі бос")
        return False
    try:
        stats = fullstats(list(camps), d1, d2) if camps else []
    except Exception:
        stats = []
    _, ad_nm = agg_ads(stats, camps, d1, d2)
    title = "🧾 Күндік қаржы есебі" if md == "daily" else "🧾 Апталық қаржы есебі (Дс–Жс)"
    tg(build_finance(title, d1, d2, reports, rows, ords, ad_nm, costs))
    return True



def commission_report(weeks=4):
    """Соңғы N апталық есеп бойынша WB комиссиясы: предмет және схема (FBW/FBS) бойынша."""
    today = datetime.now(MSK).date()
    last_sun = today - timedelta(days=today.weekday() + 1)
    first_mon = last_sun - timedelta(days=7 * weeks - 1)
    rows = fin_detail(first_mon, last_sun, "weekly")
    G = {}
    for r in rows:
        doc = (r.get("docTypeName") or "").lower()
        q = abs(r.get("quantity") or 0)
        if not q or not (doc.startswith("продажа") or doc.startswith("возврат")):
            continue
        sign = -1 if doc.startswith("возврат") else 1
        dm = (r.get("deliveryMethod") or "").upper()
        scheme = "FBS" if dm.startswith("FBS") else ("FBW" if dm.startswith("FBW") else (dm.split(",")[0] or "?"))
        key = (r.get("subjectName") or "?", scheme)
        g = G.setdefault(key, {"q": 0, "price": 0.0, "pay": 0.0, "acq": 0.0, "kvw_w": 0.0, "spp_w": 0.0})
        price = num(r.get("retailPriceWithDisc")) * q
        g["q"] += sign * q
        g["price"] += sign * price
        g["pay"] += sign * num(r.get("forPay"))
        g["acq"] += sign * num(r.get("acquiringFee"))
        if sign > 0:
            g["kvw_w"] += num(r.get("commissionPercent")) * price
            g["spp_w"] += num(r.get("spp")) * price
    L = [f"<b>📊 WB комиссиясы — {first_mon:%d.%m}–{last_sun:%d.%m.%Y} ({weeks} апта)</b>",
         "Тариф = WB-ның есептегі комиссия ставкасы (кВВ)",
         "Нақты = (сатылым − WB аударған) ÷ сатылым, эквайрингпен, СПП өтемақысынан кейін", ""]
    tp = tpay = 0.0
    for (subj, sch), g in sorted(G.items(), key=lambda x: -x[1]["price"]):
        if g["price"] <= 0:
            continue
        pos = g["price"] if g["price"] else 1
        kvw = g["kvw_w"] / pos
        eff = (g["price"] - g["pay"]) / g["price"] * 100
        L.append(f"<b>{escape(subj)}</b> · {sch} · {g['q']} дана")
        L.append(f"   тариф {kvw:.2f}% · СПП {g['spp_w'] / pos:.1f}% · эквайринг {g['acq'] / g['price'] * 100:.2f}%"
                 f" · <b>нақты {eff:.2f}%</b>")
        L.append(f"   сатылым {m(g['price'])} → WB аударды {m(g['pay'])}")
        tp += g["price"]; tpay += g["pay"]
    if tp:
        L += ["", f"<b>Барлығы: нақты комиссия {(tp - tpay) / tp * 100:.2f}%</b> ({m(tp - tpay)} / {m(tp)})"]
    tg("\n".join(L))

# ---------------------------------------------------------------- main
def target_time(now):
    """Есеп қай :00-ге арналған. GitHub cron уақытты дәл сақтамайды
    (іс жүзінде :30–:55 аралығында іске қосылады), сондықтан:
    :15–:59 → келесі сағат (сол :00-ге дейін күтеміз);
    :00–:14 → GitHub кешікті, осы сағаттың есебін бірден жібереміз."""
    base = now.replace(minute=0, second=0, microsecond=0)
    if now.minute >= 15:
        return base + timedelta(hours=1)
    return base


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "auto" else None
    if arg == "commission":
        commission_report(int(sys.argv[2]) if len(sys.argv) > 2 else 4)
        return
    now = datetime.now(TZ)
    if arg:
        modes, send_at = [arg], now
    else:
        send_at = target_time(now)
        # Сағаттық есеп әр EVERY сағат сайын (жұп сағаттарда: 10:00, 12:00 ...)
        modes = ["hourly"] if send_at.hour % EVERY == 0 else []
        if send_at.hour == CFG.get("daily_hour", 9):
            modes.append("ads_daily")
            if send_at.weekday() == 0:
                modes.append("ads_weekly")

    today = send_at.astimezone(MSK).date()          # WB күні
    yday = today - timedelta(days=1)
    last_mon = today - timedelta(days=today.weekday() + 7)
    last_sun = last_mon + timedelta(days=6)
    # Қолмен басқа кезеңді алу: "weekly 2026-11-02" (сол аптаның дүйсенбісі) немесе "daily 2026-11-10"
    if arg and len(sys.argv) > 2:
        dd = date.fromisoformat(sys.argv[2])
        if arg == "weekly":
            last_mon = dd - timedelta(days=dd.weekday())
            last_sun = last_mon + timedelta(days=6)
        elif arg == "daily":
            yday = dd

    # --- Жарнама есептері (бұрынғыдай)
    ad_periods = {}
    for md in modes:
        if md == "hourly":
            ad_periods[md] = (today, today, f"⏱ Сағаттық есеп — {send_at:%H:%M}")
        elif md in ("ads_daily", "daily"):
            ad_periods[md] = (yday, yday, "📅 Күндік жарнама есебі (кеше)")
        elif md in ("ads_weekly", "weekly"):
            ad_periods[md] = (last_mon, last_sun, "📈 Апталық жарнама есебі (Дс–Жс)")
        else:
            raise SystemExit(f"Белгісіз режим: {md}")

    # Қаржы есебі қажет пе? (күніне/аптасына бір рет, state арқылы)
    st = load_state()
    fin_jobs = []
    if arg in ("daily", "weekly"):
        fin_jobs.append((arg, *((yday, yday) if arg == "daily" else (last_mon, last_sun)), True))
    elif not arg and CFG.get("daily_hour", 9) <= send_at.hour <= 23:
        if st.get("daily_done") != yday.isoformat():
            fin_jobs.append(("daily", yday, yday, False))
        if send_at.weekday() <= 2 and st.get("weekly_done") != last_mon.isoformat():
            fin_jobs.append(("weekly", last_mon, last_sun, False))

    if not ad_periods and not fin_jobs:
        print(f"{send_at:%H:%M} — бұл сағатта есеп жоқ (әр {EVERY} сағат сайын)")
        return
    d_from = min([p[0] for p in ad_periods.values()] + [j[1] for j in fin_jobs])
    d_to = max([p[1] for p in ad_periods.values()] + [j[2] for j in fin_jobs])
    camps = campaigns()
    try:
        stats = fullstats(list(camps), d_from, d_to) if camps else []
    except Exception:
        stats = []
    bal = balance()
    ords = orders_since(d_from)
    rate = rub_rate()

    wait = (send_at - datetime.now(TZ)).total_seconds()
    if wait > 0:
        print(f"Деректер дайын, {send_at:%H:%M}-ге дейін {wait:.0f} с күтеміз")
        time.sleep(wait)

    for md, (d1, d2, label) in ad_periods.items():
        kind = "hourly" if md == "hourly" else "daily"
        tg(build(kind, label, d1, d2, camps, stats, ords, rate, bal, send_at))

    # --- Қаржы есебі (:00-ден кейін, WB дайын болса)
    if fin_jobs:
        costs = load_costs()
        for md, d1, d2, force in fin_jobs:
            try:
                if finance_job(md, d1, d2, camps, ords, costs, force):
                    st[f"{md}_done"] = d1.isoformat()
                    save_state(st)
            except WBLimit:
                print(f"{md}: WB лимиті, келесі сағатта қайталаймыз")
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                if code in (401, 403):
                    if st.get("fin_token_warned") != today.isoformat():
                        tg("⚠️ Қаржы есебі үшін WB токенінде «Финансы» рұқсаты жоқ. "
                           "WB кабинетінде Финансы санаты бар жаңа токен жасап, GitHub-тағы WB_TOKEN-ді ауыстырыңыз.")
                        st["fin_token_warned"] = today.isoformat()
                        save_state(st)
                else:
                    print(f"{md}: HTTP {code}")
            time.sleep(62)


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
