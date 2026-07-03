#!/usr/bin/env python3
"""
Мониторинг цен на авиабилеты Москва -> Венеция / Тревизо, вылет в СЕНТЯБРЕ.
Фокус на пересадках через ЕРЕВАН и ТУРЦИЮ. Цены в рублях и евро (курс ЦБ РФ на день).

Тянет цены из Travelpayouts (Aviasales) Data API, дописывает историю в
data/history.csv и пересобирает docs/index.html (карточки + график + таблица).
Запуск по расписанию через GitHub Actions (.github/workflows/monitor.yml).

ЧЕСТНЫЕ ОГОВОРКИ:
- Data API отдаёт цены из кеша поисков Aviasales за ~48 ч, а не живые котировки.
  Это ориентир по тренду; финальную цену проверяй на aviasales.ru по ссылке.
- Карточки "через Ереван"/"через Турцию" = СУММА ДВУХ ОТДЕЛЬНЫХ билетов
  (Москва->хаб + хаб->пункт) за сентябрь. Это self-connect: обычно дешевле,
  но это две разные брони, риск стыковки на тебе. Карточка "любой маршрут" —
  это цельный билет с одной стыковкой (какой найдёт кеш).
- Так как месяц задаётся с точностью до сентября, сумма по хабу — индикативная
  оценка бюджета маршрута, а не гарантированная стыковка в конкретный день.
"""

import csv
import datetime as dt
import html
import json
import os
import pathlib
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ----------------------------- Настройки -----------------------------------

TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()

DEPART_MONTH = "2026-09"           # сентябрь 2026 (формат YYYY-MM), анализ по всему месяцу
CURRENCY = "rub"                   # базовая валюта запроса к API

# Откуда летим (IATA, человекочитаемое имя)
ORIGINS = [
    ("MOW", "Москва"),
    ("LED", "Санкт-Петербург"),
]

# Куда летим (IATA, человекочитаемое имя)
DESTINATIONS = [
    ("VCE", "Венеция (Марко Поло)"),
    ("TSF", "Тревизо (~20 км до Венеции)"),
]

# Хабы пересадки, сгруппированные. Для группы берём самый дешёвый аэропорт.
VIA_GROUPS = [
    ("Ереван", [("EVN", "Ереван")]),
    ("Турция", [("IST", "Стамбул IST"), ("SAW", "Стамбул SAW")]),
    ("Белград", [("BEG", "Белград")]),
]
# Винительный падеж для подписи «Через …»
VIA_ACC = {"Ереван": "Ереван", "Турция": "Турцию", "Белград": "Белград"}

BASE = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
DOCS_DIR = BASE / "docs"
HISTORY_CSV = DATA_DIR / "history.csv"

API_HOST = "https://api.travelpayouts.com"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

MONTHS_RU = {
    "01": "январь", "02": "февраль", "03": "март", "04": "апрель",
    "05": "май", "06": "июнь", "07": "июль", "08": "август",
    "09": "сентябрь", "10": "октябрь", "11": "ноябрь", "12": "декабрь",
}

# --------------------------- Работа с API ----------------------------------


def api_get(path: str, params: dict) -> dict:
    params = {**params, "token": TOKEN}
    url = f"{API_HOST}{path}?{urlencode(params)}"
    req = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "fare-monitor/2.0"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cheapest_leg(origin: str, destination: str, month: str) -> dict | None:
    """Самый дешёвый билет origin->destination с вылетом в заданном месяце."""
    try:
        data = api_get(
            "/aviasales/v3/prices_for_dates",
            {
                "origin": origin, "destination": destination,
                "departure_at": month, "currency": CURRENCY,
                "sorting": "price", "one_way": "true", "limit": 1, "page": 1,
            },
        )
    except (HTTPError, URLError, ValueError) as e:
        print(f"[warn] {origin}->{destination} {month}: {e}", file=sys.stderr)
        return None
    items = data.get("data") or []
    if not items:
        return None
    it = items[0]
    link = it.get("link", "")
    full = f"https://www.aviasales.ru{link}" if link.startswith("/") else link
    return {
        "price": it.get("price"),
        "airline": it.get("airline", ""),
        "transfers": it.get("transfers", ""),
        "depart_date": (it.get("departure_at", "") or "")[:10],
        "duration": it.get("duration", ""),
        "link": full,
    }


def via_group_total(origin: str, dest: str, month: str, airports: list) -> dict | None:
    """
    Для группы хабов считаем: min по хабам ( цена(origin->хаб) + цена(хаб->dest) ).
    Возвращает лучший вариант или None, если ни по одному хабу нет обеих ног.
    """
    best = None
    for hub_code, hub_name in airports:
        leg1 = cheapest_leg(origin, hub_code, month)
        leg2 = cheapest_leg(hub_code, dest, month)
        if not leg1 or not leg2 or leg1["price"] is None or leg2["price"] is None:
            continue
        total = leg1["price"] + leg2["price"]
        cand = {
            "price": total, "hub_code": hub_code, "hub_name": hub_name,
            "leg1": leg1, "leg2": leg2,
        }
        if best is None or total < best["price"]:
            best = cand
    return best


def get_eur_rate() -> float | None:
    """Курс EUR->RUB от ЦБ РФ на текущий день (сколько рублей за 1 евро)."""
    try:
        req = Request(CBR_URL, headers={"User-Agent": "fare-monitor/2.0"})
        with urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode("utf-8"))
        v = j["Valute"]["EUR"]
        return v["Value"] / v.get("Nominal", 1)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] курс ЦБ недоступен: {e}", file=sys.stderr)
        return None


# --------------------------- История (CSV) ---------------------------------

FIELDS = ["checked_at", "origin", "dest", "option", "hub", "price_rub", "price_eur",
          "eur_rate", "depart_month", "detail", "link1", "link2"]


def append_history(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def read_history() -> list[dict]:
    if not HISTORY_CSV.exists():
        return []
    with HISTORY_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --------------------------- Форматирование --------------------------------


def rub(n) -> str:
    return f"{int(round(float(n))):,} ₽".replace(",", " ")


def eur(n) -> str:
    return f"~{int(round(float(n))):,} €".replace(",", " ")


def dur_str(minutes) -> str:
    if str(minutes).isdigit():
        m = int(minutes)
        return f"{m // 60} ч {m % 60} мин"
    return ""


# --------------------------- Рендер страницы -------------------------------


def sparkline(points: list, w: int = 560, h: int = 110, pad: int = 8) -> str:
    pts = [float(p) for p in points if p not in ("", None)]
    if len(pts) < 2:
        return '<p class="muted">Мало точек для графика — появится после нескольких запусков.</p>'
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    n = len(pts)
    coords = [(pad + (w - 2 * pad) * (i / (n - 1)),
               pad + (h - 2 * pad) * (1 - (p - lo) / span)) for i, p in enumerate(pts)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    lx, ly = coords[-1]
    return (f'<svg viewBox="0 0 {w} {h}" class="spark" role="img" aria-label="История цены">'
            f'<path d="{path}" fill="none" stroke="var(--accent)" stroke-width="2"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="var(--accent)"/></svg>')


def render(cards: list[dict], history: list[dict], eur_rate, month: str) -> str:
    now = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=3)))
    stamp = now.strftime("%d.%m.%Y %H:%M МСК")
    yyyy, mm = month.split("-")
    month_ru = f"{MONTHS_RU.get(mm, mm)} {yyyy}"
    rate_str = f"курс ЦБ: 1 € = {eur_rate:.2f} ₽" if eur_rate else "курс ЦБ недоступен — только ₽"

    # группируем карточки по секции «откуда → куда»
    by_section: dict[str, list] = {}
    for c in cards:
        by_section.setdefault(c["section"], []).append(c)

    sections = ""
    for section_label, group in by_section.items():
        cells = ""
        for c in group:
            hist_prices = [h["price_rub"] for h in history
                           if h.get("origin") == c["origin_code"]
                           and h.get("dest") == c["dest_code"]
                           and h.get("option") == c["option"]
                           and h.get("price_rub") not in ("", None)]
            if c["price_rub"] is None:
                body = '<div class="price na">нет данных в кеше</div>'
                links = ""
            else:
                prev = float(hist_prices[-2]) if len(hist_prices) >= 2 else None
                delta = ""
                if prev is not None:
                    d = c["price_rub"] - prev
                    if d < 0:
                        delta = f'<span class="delta down">▼ {rub(abs(d))}</span>'
                    elif d > 0:
                        delta = f'<span class="delta up">▲ {rub(d)}</span>'
                    else:
                        delta = '<span class="delta flat">= </span>'
                eur_line = (f'<div class="eur">{eur(c["price_eur"])}</div>'
                            if c["price_eur"] is not None else "")
                lo = min(float(x) for x in hist_prices) if hist_prices else c["price_rub"]
                body = (f'<div class="price">{rub(c["price_rub"])} {delta}</div>'
                        f'{eur_line}'
                        f'<div class="meta">{c["detail"]} · мин. за всё время: {rub(lo)}</div>')
                links = c["links_html"]
            cells += f"""
          <article class="card {c['tag']}">
            <div class="opt">{html.escape(c['option'])}</div>
            {body}
            {sparkline(hist_prices)}
            {links}
          </article>"""
        sections += f'<h3>{html.escape(section_label)}</h3><div class="grid">{cells}</div>'

    # таблица истории
    rows_html = ""
    for h in reversed(history[-72:]):
        if h.get("price_rub") in ("", None):
            continue
        pe = eur(h["price_eur"]) if h.get("price_eur") not in ("", None) else "—"
        rows_html += (f"<tr><td>{html.escape(h['checked_at'])}</td>"
                      f"<td>{html.escape(h.get('origin',''))}</td>"
                      f"<td>{html.escape(h.get('dest',''))}</td>"
                      f"<td>{html.escape(h.get('option',''))}</td>"
                      f"<td class='num'>{rub(h['price_rub'])}</td>"
                      f"<td class='num'>{pe}</td></tr>")

    return TEMPLATE.format(stamp=stamp, month_ru=month_ru, rate_str=rate_str,
                           sections=sections, rows=rows_html)


TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Мониторинг цен · Москва → Венеция · сентябрь</title>
<style>
  :root {{
    --bg:#12151c; --panel:#1a1f29; --ink:#e8eaed; --muted:#8b93a3;
    --line:#2a313d; --accent:#e0a03e; --down:#5fbf7f; --up:#e06a5c;
    --evn:#7fa8d0; --tur:#d08f7f; --beg:#9db07f;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    line-height:1.5; padding:32px 20px 64px; }}
  .wrap {{ max-width:820px; margin:0 auto; }}
  header {{ border-bottom:1px solid var(--line); padding-bottom:16px; margin-bottom:8px; }}
  .eyebrow {{ font-family:var(--mono); font-size:12px; letter-spacing:.14em;
    text-transform:uppercase; color:var(--muted); }}
  h1 {{ font-size:26px; margin:6px 0 4px; font-weight:650; }}
  .stamp {{ font-family:var(--mono); font-size:13px; color:var(--muted); }}
  h3 {{ font-size:15px; margin:30px 0 12px; font-weight:600; }}
  .grid {{ display:grid; gap:14px; grid-template-columns:1fr; }}
  @media(min-width:520px){{ .grid {{ grid-template-columns:1fr 1fr; }} }}
  @media(min-width:900px){{ .grid {{ grid-template-columns:repeat(4,1fr); }} }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:16px; border-top:3px solid var(--line); }}
  .card.evn {{ border-top-color:var(--evn); }}
  .card.tur {{ border-top-color:var(--tur); }}
  .card.beg {{ border-top-color:var(--beg); }}
  .card.any {{ border-top-color:var(--accent); }}
  .opt {{ font-size:12px; font-family:var(--mono); text-transform:uppercase;
    letter-spacing:.06em; color:var(--muted); margin-bottom:10px; }}
  .price {{ font-family:var(--mono); font-size:23px; font-weight:600; }}
  .price.na {{ color:var(--muted); font-size:16px; }}
  .eur {{ font-family:var(--mono); font-size:14px; color:var(--muted); margin-top:2px; }}
  .delta {{ font-size:12px; font-family:var(--mono); margin-left:4px; }}
  .delta.down {{ color:var(--down); }} .delta.up {{ color:var(--up); }} .delta.flat {{ color:var(--muted); }}
  .meta {{ color:var(--muted); font-size:12px; margin:8px 0 10px; }}
  .spark {{ width:100%; height:auto; display:block; margin:4px 0 12px; }}
  .btn {{ display:inline-block; font-size:12px; color:var(--accent); text-decoration:none;
    border:1px solid var(--line); border-radius:7px; padding:5px 10px; margin:2px 4px 2px 0; }}
  .btn:hover {{ border-color:var(--accent); }}
  .muted {{ color:var(--muted); font-size:12px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
  th,td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }}
  th {{ color:var(--muted); font-weight:500; font-family:var(--mono); font-size:11px;
    text-transform:uppercase; letter-spacing:.06em; }}
  td.num,th.num {{ text-align:right; font-family:var(--mono); }}
  footer {{ margin-top:36px; color:var(--muted); font-size:12px;
    border-top:1px solid var(--line); padding-top:16px; }}
  a {{ color:var(--accent); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">Fare monitor · обновление каждые 8 ч · вылет {month_ru}</div>
    <h1>Москва · СПб → Венеция / Тревизо</h1>
    <div class="stamp">Проверка: {stamp} · {rate_str}</div>
  </header>

  {sections}

  <h3>История проверок</h3>
  <table>
    <thead><tr><th>Проверено (UTC)</th><th>Откуда</th><th>Пункт</th><th>Маршрут</th>
    <th class="num">₽</th><th class="num">€</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>

  <footer>
    Цены: Travelpayouts (Aviasales) Data API — кеш поисков за ~48 ч, ориентир по
    тренду, не финальная цена. Карточки «через Ереван/Турцию» — сумма двух
    отдельных билетов (Москва→хаб + хаб→пункт) за {month_ru}, это self-connect:
    дешевле, но две брони и риск стыковки на пассажире. «Любой маршрут» — цельный
    билет с одной стыковкой. Евро — по официальному курсу ЦБ РФ на день проверки.
    Перед покупкой сверяйся на aviasales.ru.
  </footer>
</div>
</body>
</html>
"""


# ------------------------------- main --------------------------------------


def build_cards(eur_rate) -> list[dict]:
    def to_eur(rub_val):
        return None if (eur_rate is None or rub_val is None) else rub_val / eur_rate

    cards = []
    for origin_code, origin_label in ORIGINS:
        for dest_code, dest_label in DESTINATIONS:
            section = f"{origin_label} → {dest_label}"
            # 1) любой маршрут (цельный билет)
            any_r = cheapest_leg(origin_code, dest_code, DEPART_MONTH)
            if any_r and any_r["price"] is not None:
                t = any_r["transfers"]
                t_str = "прямой" if str(t) == "0" else f"{t} перес." if t != "" else ""
                detail = " · ".join(x for x in [f"вылет {any_r['depart_date']}", t_str,
                                                dur_str(any_r["duration"]),
                                                f"a/к {any_r['airline']}"] if x)
                links = (f'<a class="btn" href="{html.escape(any_r["link"])}" target="_blank" '
                         f'rel="noopener">Aviasales →</a>' if any_r["link"] else "")
                cards.append({
                    "origin_code": origin_code, "dest_code": dest_code,
                    "dest_label": dest_label, "section": section,
                    "option": "Любой маршрут (мин.)", "tag": "any",
                    "route_key": f"{origin_code}|{dest_code}|any",
                    "price_rub": any_r["price"], "price_eur": to_eur(any_r["price"]),
                    "detail": detail, "links_html": links,
                })
            else:
                cards.append({"origin_code": origin_code, "dest_code": dest_code,
                              "dest_label": dest_label, "section": section,
                              "option": "Любой маршрут (мин.)", "tag": "any",
                              "route_key": f"{origin_code}|{dest_code}|any", "price_rub": None,
                              "price_eur": None, "detail": "", "links_html": ""})

            # 2) через хабы (Ереван / Турция / Белград) — сумма двух билетов
            for group_name, airports in VIA_GROUPS:
                tag = {"Ереван": "evn", "Турция": "tur", "Белград": "beg"}.get(group_name, "any")
                best = via_group_total(origin_code, dest_code, DEPART_MONTH, airports)
                if best:
                    l1, l2 = best["leg1"], best["leg2"]
                    detail = f'{best["hub_name"]}: {rub(l1["price"])} + {rub(l2["price"])}'
                    links = ""
                    if l1["link"]:
                        links += (f'<a class="btn" href="{html.escape(l1["link"])}" '
                                  f'target="_blank" rel="noopener">'
                                  f'{origin_code}→{best["hub_code"]} →</a>')
                    if l2["link"]:
                        links += (f'<a class="btn" href="{html.escape(l2["link"])}" '
                                  f'target="_blank" rel="noopener">'
                                  f'{best["hub_code"]}→{dest_code} →</a>')
                    cards.append({
                        "origin_code": origin_code, "dest_code": dest_code,
                        "dest_label": dest_label, "section": section,
                        "option": f"Через {VIA_ACC.get(group_name, group_name)}", "tag": tag,
                        "route_key": f"{origin_code}|{dest_code}|{group_name}",
                        "hub": best["hub_code"],
                        "price_rub": best["price"], "price_eur": to_eur(best["price"]),
                        "detail": detail, "links_html": links,
                    })
                else:
                    cards.append({"origin_code": origin_code, "dest_code": dest_code,
                                  "dest_label": dest_label, "section": section,
                                  "option": f"Через {VIA_ACC.get(group_name, group_name)}",
                                  "tag": tag,
                                  "route_key": f"{origin_code}|{dest_code}|{group_name}",
                                  "hub": "", "price_rub": None, "price_eur": None,
                                  "detail": "", "links_html": ""})
    return cards


def main() -> int:
    if not TOKEN:
        print("ОШИБКА: не задан TRAVELPAYOUTS_TOKEN (секрет GitHub / переменная окружения).",
              file=sys.stderr)
        return 1

    eur_rate = get_eur_rate()
    cards = build_cards(eur_rate)

    checked_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    hist_rows = []
    for c in cards:
        if c["price_rub"] is None:
            continue
        hist_rows.append({
            "checked_at": checked_at, "origin": c["origin_code"], "dest": c["dest_code"],
            "option": c["option"], "hub": c.get("hub", ""),
            "price_rub": int(round(c["price_rub"])),
            "price_eur": int(round(c["price_eur"])) if c["price_eur"] is not None else "",
            "eur_rate": f"{eur_rate:.4f}" if eur_rate else "",
            "depart_month": DEPART_MONTH, "detail": c["detail"],
            "link1": "", "link2": "",
        })
    if hist_rows:
        append_history(hist_rows)

    history = read_history()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(
        render(cards, history, eur_rate, DEPART_MONTH), encoding="utf-8")
    priced = sum(1 for c in cards if c["price_rub"] is not None)
    print(f"Готово: {priced}/{len(cards)} карточек с ценой, история — {len(history)} строк, "
          f"курс EUR={eur_rate}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
