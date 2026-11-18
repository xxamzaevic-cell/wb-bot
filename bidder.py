"""WB биддер: таңдалған іздеу кластерлерінде 1-орынды ұстап тұрады.

Сағатына бір рет іске қосылады. Әр кластер үшін:
  1. Соңғы сағаттағы орташа позицияны есептейді (WB күндік жинақтан айырма алады).
  2. 1-орында болмасақ, ставканы көтереді. Бәсекелестің «біз ұтылған» ставкасын есте
     сақтайды да, келесі жолы содан 1 ₸ жоғары қояды.
  3. 1-орында тұрсақ (қатарынан 2 рет), бәсекелестің ставкасынан 1 ₸ жоғарыға дейін
     ставканы ақырындап түсіреді.
  4. Ставкаға шек жоқ. Бірақ соңғы 7 күндегі CPO 1000 ₸-ден асса, бот ставканы
     КӨТЕРМЕЙДІ, Telegram-ға ескерту жібереді (түсіруге рұқсат).

dry_run=true кезінде ештеңе өзгертпейді, тек «не істер едім» деп жазады.
Қолмен: python bidder.py [dry|live]
"""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html import escape

import requests

ADV = "https://advert-api.wildberries.ru"
WB_TOKEN = os.environ.get("WB_BID_TOKEN") or os.environ.get("WB_TOKEN", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "bidder_config.json"), encoding="utf-8") as f:
    C = json.load(f)

MSK = timezone(timedelta(hours=3))      # WB күні Мәскеу уақытымен
TZ = timezone(timedelta(hours=5))       # Шымкент
STATE_FILE = os.path.join(HERE, "state", "bidder.json")
U = float(C.get("bid_units_per_tg", 100))   # API бірлігі → ₸
AID, NM = int(C["advert_id"]), int(C["nm_id"])


def norm(s):
    return " ".join(str(s).lower().replace("ё", "е").split())


CLUSTERS = [norm(c) for c in C["clusters"]]


# ---------------------------------------------------------------- HTTP
def wb(method, path, tries=3, **kw):
    url = path if path.startswith("http") else ADV + path
    for attempt in range(tries):
        try:
            r = requests.request(method, url, headers={"Authorization": WB_TOKEN},
                                 timeout=60, **kw)
        except requests.RequestException:
            time.sleep(10)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            print(f"{path} → {r.status_code} {dict((k, v) for k, v in r.headers.items() if 'atelimit' in k or k == 'Retry-After')}",
                  flush=True)
            wait = r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After")
            try:
                wait = float(wait) + 2
            except (TypeError, ValueError):
                wait = 15 * (attempt + 1)
            if wait > 280:               # ұзақ айып — күтпейміз, келесі сағатта
                raise RuntimeError(f"{path}: WB лимиті, {int(wait)} сек күту керек")
            time.sleep(wait)
            continue
        time.sleep(7)                   # WB лимиті: сұраныстар арасы ≥6 сек
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return r.text
    raise RuntimeError(f"{method} {path}: WB жауап бермеді (лимит)")


def tg(text):
    if not TG_TOKEN:
        print(text)
        return
    for i in range(0, len(text), 3900):
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text[i:i + 3900],
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=30)


def walk(obj, key):
    """JSON ішінен `key` өрісі бар барлық dict-терді табады (жауап пішіні өзгерсе де)."""
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for v in obj.values():
            yield from walk(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, key)


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- WB data
def cluster_stats(d1, d2):
    body = {"from": d1.isoformat(), "to": d2.isoformat(),
            "items": [{"advert_id": AID, "nm_id": NM}]}
    data = wb("POST", "/adv/v0/normquery/stats", json=body)
    out = {}
    for s in walk(data, "norm_query"):
        q = norm(s["norm_query"])
        views = f(s.get("views"))
        if "sum" in s or "spend" in s:
            spend = f(s.get("sum", s.get("spend")))
        else:
            spend = views * f(s.get("cpm")) / 1000
        out[q] = {"views": views, "orders": f(s.get("orders")),
                  "pos": f(s.get("avg_pos")), "spend": spend, "raw": s}
    return out


def current_bids():
    data = wb("POST", "/adv/v0/normquery/get-bids",
              json={"items": [{"advert_id": AID, "nm_id": NM}]})
    return {norm(b["norm_query"]): f(b.get("bid")) for b in walk(data, "norm_query")
            if b.get("bid") is not None}, data


def leader_hints():
    """WB ұсынған «максимум охват» ставкасы — бәсекелес деңгейінің бағдары (₸)."""
    try:
        data = wb("GET", "/api/advert/v0/bids/recommendations",
                  params={"advertId": AID, "nmId": NM}, tries=2)
    except Exception:
        return {}
    out = {}
    for q in walk(data, "normQuery"):
        v = (q.get("reachMax") or {}).get("bidKopecks")
        if v:
            out[norm(q["normQuery"])] = f(v) / 100
    return out


def set_bids(changes):
    body = {"bids": [{"advert_id": AID, "nm_id": NM, "norm_query": q,
                      "bid": int(round(tg_ * U))} for q, tg_ in changes.items()]}
    return wb("POST", "/adv/v0/normquery/bids", json=body)


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)


def hour_pos(prev, today, day):
    """WB күндік орташа позициясынан соңғы сағаттың позициясын шығарады."""
    v, p = today["views"], today["pos"]
    if prev and prev.get("day") == day and v > prev["views"]:
        dv = v - prev["views"]
        hp = (p * v - prev["pos"] * prev["views"]) / dv
        return max(hp, 1.0) if hp > 0 else p, dv
    if prev and prev.get("day") == day:
        return None, 0          # жаңа показ жоқ
    return (p if v else None), v


# ---------------------------------------------------------------- logic
def decide(q, bid, pos, views, cpo, hint, cs, now):
    """Бір кластер үшін жаңа ставка (₸) және себеп. None = өзгертпеу."""
    first_max = C["first_place_max_pos"]
    out = C["outbid_tg"]
    lost = cs.get("lost_bid")            # біз 1-орыннан ұтылған ең жоғары ставка
    if lost and now - cs.get("lost_ts", 0) > C["forget_competitor_hours"] * 3600:
        lost = None                      # бәсекелес түсірген болуы мүмкін — ұмытамыз
        cs.pop("lost_bid", None)

    if pos is None or views < C["min_views_for_decision"]:
        return None, f"дерек аз ({int(views)} показ)"

    win = cs.get("win_bid")              # 1-орын берген ең соңғы ставка
    if pos > first_max:                  # 1-орында емеспіз
        cs["first_runs"] = 0
        if lost is None or bid > lost:
            cs["lost_bid"], cs["lost_ts"] = bid, now
        if win and win <= bid:           # бәсекелес көтерді — ескі «жеңіс» жарамсыз
            cs.pop("win_bid", None)
            win = None
        if cpo > C["max_cpo_tg"]:
            return None, f"⛔ CPO {cpo:.0f} ₸ > {C['max_cpo_tg']} ₸ — көтермеймін"
        if win:                          # тым төмен түсіп кеттік — жеңген ставкаға ораламыз
            return round(win), f"поз. {pos:.1f} → қайта көтеремін"
        step = max(bid * C["raise_pct"] / 100, C["raise_min_tg"])
        new = bid + step
        if hint and hint + out > new:
            new = hint + out             # WB көрсеткен лидер деңгейіне бірден секіреміз
        return round(new), f"поз. {pos:.1f} → көтеремін"

    # 1-орындамыз
    cs["win_bid"] = bid
    cs["first_runs"] = cs.get("first_runs", 0) + 1
    if cs["first_runs"] < C["lower_after_runs"]:
        return None, f"поз. {pos:.1f} ✅ ұстап тұр"
    floor = max(C["min_bid_tg"], (lost + out) if lost else 0)
    if lost and lost < bid:              # бәсекелес осы екеуінің арасында — ортасына түсеміз
        new = max((lost + bid) / 2, floor)
    else:
        new = max(bid * (1 - C["lower_pct"] / 100), floor)
    if bid - new < 1:
        return None, f"поз. {pos:.1f} ✅ бәсекелестен +{out} ₸"
    return round(new), f"поз. {pos:.1f} ✅ → үнемдеп түсіремін"


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    dry = C["dry_run"] if mode not in ("dry", "live") else mode == "dry"
    now = time.time()
    today = datetime.now(MSK).date()
    st = load_state()
    cl_state = st.setdefault("clusters", {})

    bids, raw_bids = current_bids()
    # WB normquery/stats лимиті қатаң: бір іске қосылуда тек БІР сұраныс жасаймыз.
    # 7 күндік CPO 6 сағатта бір рет жаңарады, қалған сағаттарда — бүгінгі позиция.
    cpo_cache = st.get("cpo_cache") or {}
    if not cpo_cache or now - st.get("cpo_ts", 0) > 6 * 3600:
        period = cluster_stats(today - timedelta(days=C["cpo_days"] - 1), today)
        st["cpo_cache"] = {q: {"orders": v["orders"], "spend": v["spend"]}
                           for q, v in period.items()}
        st["cpo_ts"] = now
        save_state(st)
        tg(f"🎯 <b>Биддер</b>: {C['cpo_days']} күндік CPO жаңартылды. "
           "Ставка келесі сағатта тексеріледі.") if mode else print("CPO updated")
        return
    stats_period = cpo_cache
    stats_today = cluster_stats(today, today)
    hints = leader_hints()

    lines, changes = [], {}
    for q in CLUSTERS:
        cs = cl_state.setdefault(q, {})
        t = stats_today.get(q, {"views": 0, "orders": 0, "pos": 0, "spend": 0})
        per = stats_period.get(q, {"orders": 0, "spend": 0})
        pos, views = hour_pos(cs.get("cum"), t, today.isoformat())
        cs["cum"] = {"day": today.isoformat(), "views": t["views"], "pos": t["pos"]}
        cpo = per["spend"] / per["orders"] if per["orders"] else (
            float("inf") if per["spend"] > C["max_cpo_tg"] else 0)
        bid_api = bids.get(q)
        if bid_api is None:
            lines.append(f"• <b>{escape(q)}</b>: ставка табылмады ❓")
            continue
        bid = bid_api / U
        new, why = decide(q, bid, pos, views, cpo, hints.get(q), cs, now)
        cpo_s = "—" if not per["orders"] and not per["spend"] else (
            "∞" if cpo == float("inf") else f"{cpo:.0f}")
        line = f"• <b>{escape(q)}</b>: {why}\n   ставка {bid:.0f} ₸"
        if new is not None and new != round(bid):
            changes[q] = new
            line += f" → <b>{new} ₸</b>"
        line += f" | CPO {C['cpo_days']}к: {cpo_s} ₸"
        if "⛔" in why:
            key = f"alert_{today}"
            if not cs.get(key):
                cs[key] = 1
                line += "\n   ⚠️ Бәсекелес ставканы қымбаттатты. Шешімді сізге қалдырамын."
        lines.append(line)

    ok = ""
    if changes and not dry:
        try:
            set_bids(changes)
            ok = "✅ ставкалар өзгертілді"
        except Exception as e:
            ok = f"❌ ставка қою қатесі: {escape(str(e))}"
    elif changes:
        ok = "🧪 СЫНАҚ РЕЖИМІ: ештеңе өзгертілген жоқ"

    head = f"🎯 <b>Биддер</b> {datetime.now(TZ):%d.%m %H:%M} · кампания {AID}"
    msg = "\n".join([head] + lines + ([ok] if ok else []))
    first_debug = dry and not st.get("dry_debug_sent")
    if first_debug:
        st["dry_debug_sent"] = 1
        msg += ("\n\n<b>Тексеру үшін (бір рет):</b>\nWB get-bids жауабы:\n<code>"
                + escape(json.dumps(raw_bids, ensure_ascii=False)[:1200]) + "</code>"
                + f"\nБірлік: 1 ₸ = {U:g} API бірлігі. Ставкалар кабинеттегімен сәйкес пе?")
    if changes or mode or first_debug or any("⚠️" in l for l in lines):
        tg(msg)                  # өзгеріс жоқ болса, Telegram-ды толтырмаймыз
    else:
        print(msg)
    save_state(st)


if __name__ == "__main__":
    main()
