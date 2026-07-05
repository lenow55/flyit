#!/usr/bin/env python3
"""
Мониторинг цен на авиабилеты → Тбилиси (TBS).

Показывает только ПРЯМЫЕ рейсы и рейсы с ОДНОЙ пересадкой (без self-connect).
Цены в ₽ и € (ЦБ РФ), история в data/history.csv, отчёт в docs/index.html.

Главное отличие от "среднего" мониторинга — аналитика:
  1) Тепловая карта по месяцу: какие даты вылета дешевле/дороже.
  2) Статистика по дням недели: когда лететь выгоднее.
  3) История запусков: как менялся минимум цены на ближайшие N дней.

Запуск по расписанию через GitHub Actions (см. README).
"""

import csv
import datetime as dt
import html
import json
import os
import pathlib
import sys
from collections import defaultdict
from statistics import mean
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ---------------------------- Настройки ------------------------------------

TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()

# Месяц, по которому строим аналитику и тепловую карту (YYYY-MM).
# Можно поменять на любой будущий месяц — скрипт сам подтянет свежие цены.
DEPART_MONTH = "2026-08"

# Горизонт «лучшей цены на ближайшие дни» (для топ-карточек):
HORIZON_DAYS = 14

# Что учитываем: 0 = прямые, 1 = с одной пересадкой.
MAX_TRANSFERS = 1

CURRENCY = "rub"

ORIGINS = [
    ("GOJ", "Нижний Новгород"),
]

DESTINATIONS = [
    ("TBS", "Тбилиси"),
]

BASE = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
DOCS_DIR = BASE / "docs"
HISTORY_CSV = DATA_DIR / "history.csv"

API_HOST = "https://api.travelpayouts.com"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

MONTHS_RU = {
    "01": "январь",
    "02": "февраль",
    "03": "март",
    "04": "апрель",
    "05": "май",
    "06": "июнь",
    "07": "июль",
    "08": "август",
    "09": "сентябрь",
    "10": "октябрь",
    "11": "ноябрь",
    "12": "декабрь",
}
WD_LONG = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]
WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# ----------------------------- API ----------------------------------------


def api_get(path: str, params: dict[str, str]) -> dict[str, object]:
    params = {**params, "token": TOKEN}
    url = f"{API_HOST}{path}?{urlencode(params)}"
    req = Request(
        url, headers={"Accept-Encoding": "identity", "User-Agent": "tbs-monitor/1.0"}
    )
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    parsed: dict[str, object] = json.loads(raw)
    return parsed


def get_eur_rate() -> float | None:
    """Курс EUR→RUB от ЦБ РФ (₽ за 1 €). None при сбое."""
    try:
        req = Request(CBR_URL, headers={"User-Agent": "tbs-monitor/1.0"})
        with urlopen(req, timeout=30) as r:
            j: dict[str, object] = json.loads(r.read().decode("utf-8"))
        valute = j.get("Valute")
        if not isinstance(valute, dict):
            return None
        eur = valute.get("EUR")
        if not isinstance(eur, dict):
            return None
        value = eur.get("Value")
        nominal = eur.get("Nominal", 1)
        if not isinstance(value, (int, float)):
            return None
        return float(value) / float(nominal)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] CBR недоступен: {e}", file=sys.stderr)
        return None


# ----------------------- Сбор цен с API -----------------------------------


def fetch_prices_month(origin: str, dest: str, month: str) -> list[dict[str, object]]:
    """
    Тянем ВСЕ цены на месяц одним запросом (limit=1000).
    Возвращаем [{date, price, transfers, airline, duration, link}, ...].
    Фильтр по пересадкам — на клиенте.
    """
    try:
        data = api_get(
            "/aviasales/v3/prices_for_dates",
            {
                "origin": origin,
                "destination": dest,
                "departure_at": month,
                "currency": CURRENCY,
                "sorting": "price",
                "one_way": "true",
                "limit": "1000",
                "page": "1",
            },
        )
    except (HTTPError, URLError, ValueError) as e:
        print(f"[warn] {origin}->{dest}: {e}", file=sys.stderr)
        return []

    items = data.get("data")
    if not isinstance(items, list):
        return []

    out: list[dict[str, object]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        date = str(it.get("departure_at") or "")[:10]
        if not date:
            continue
        out.append(
            {
                "date": date,
                "price": it.get("price"),
                "transfers": it.get("transfers"),
                "airline": it.get("airline", ""),
                "duration": it.get("duration", ""),
                "link": it.get("link", ""),
            }
        )
    return out


# ----------------------- Аналитика ----------------------------------------


def _as_float(v: object) -> float | None:
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def split_by_route(
    prices: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Делим на прямые и с одной пересадкой (с валидной ценой)."""
    direct: list[dict[str, object]] = []
    onestop: list[dict[str, object]] = []
    for p in prices:
        if _as_float(p.get("price")) is None:
            continue
        try:
            t = int(str(p.get("transfers")))
        except (TypeError, ValueError):
            continue
        if t == 0:
            direct.append(p)
        elif t == 1:
            onestop.append(p)
    return direct, onestop


def best_in_window(
    prices: list[dict[str, object]], from_date: dt.date, days: int
) -> dict[str, object] | None:
    """Минимальная цена за окно [from_date, from_date + days)."""
    end = from_date + dt.timedelta(days=days)
    in_win: list[dict[str, object]] = []
    for p in prices:
        try:
            d = dt.date.fromisoformat(str(p["date"]))
        except (TypeError, ValueError):
            continue
        if from_date <= d < end and _as_float(p.get("price")) is not None:
            in_win.append(p)
    if not in_win:
        return None
    return min(in_win, key=lambda x: float(str(x["price"])))  # type: ignore[arg-type]


def weekday_breakdown(prices: list[dict[str, object]]) -> list[dict[str, object]]:
    """
    Для каждого дня недели — min/avg/max/count по ценам
    (любой тип маршрута до MAX_TRANSFERS).
    """
    by_wd: dict[int, list[float]] = defaultdict(list)
    for p in prices:
        price = _as_float(p.get("price"))
        if price is None:
            continue
        try:
            t = int(str(p.get("transfers")))
        except (TypeError, ValueError):
            continue
        if t > MAX_TRANSFERS:
            continue
        try:
            d = dt.date.fromisoformat(str(p["date"]))
        except (TypeError, ValueError):
            continue
        by_wd[d.weekday()].append(price)

    result: list[dict[str, object]] = []
    for wd in range(7):
        vals = by_wd.get(wd, [])
        if not vals:
            continue
        result.append(
            {
                "wd": wd,
                "count": len(vals),
                "min": min(vals),
                "avg": mean(vals),
                "max": max(vals),
            }
        )
    return result


# ----------------------- История (CSV) ------------------------------------

FIELDS = [
    "checked_at",
    "origin",
    "dest",
    "route_type",
    "depart_date",
    "weekday",
    "price_rub",
    "price_eur",
    "eur_rate",
    "airline",
    "transfers",
    "duration",
    "link",
]


def append_history(rows: list[dict[str, object]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def read_history() -> list[dict[str, object]]:
    if not HISTORY_CSV.exists():
        return []
    with HISTORY_CSV.open(encoding="utf-8") as f:
        # csv.DictReader возвращает list[dict[str | Any, str | Any]],
        # приводим к заявленному типу результата.
        rows: list[dict[str, object]] = []
        for r in csv.DictReader(f):
            rows.append({k: v for k, v in r.items() if isinstance(k, str)})
        return rows


# ---------------------- Форматирование ------------------------------------


def rub(n: object) -> str:
    v = _as_float(n)
    if v is None:
        return "— ₽"
    return f"{int(round(v)):,} ₽".replace(",", " ")


def eur(n: object) -> str:
    v = _as_float(n)
    if v is None:
        return "— €"
    return f"~{int(round(v)):,} €".replace(",", " ")


def dur_str(minutes: object) -> str:
    v = _as_float(minutes)
    if v is None:
        return ""
    m = int(v)
    return f"{m // 60} ч {m % 60} мин"


def aviasales_link(origin: str, dest: str, date_str: str, raw_link: object) -> str:
    if isinstance(raw_link, str) and raw_link:
        if raw_link.startswith("/"):
            url = f"https://www.aviasales.ru{raw_link}"
        else:
            url = raw_link
        return (
            f'<a class="btn" href="{html.escape(url)}" target="_blank" '
            f'rel="noopener">Aviasales →</a>'
        )
    if len(date_str) == 10:
        search = f"{origin}{date_str[5:7]}{date_str[8:10]}{dest}1"
    else:
        search = f"{origin}{dest}"
    return (
        f'<a class="btn" href="https://www.aviasales.ru/search/{search}" '
        f'target="_blank" rel="noopener">Aviasales →</a>'
    )


# ----------------------------- Рендер -------------------------------------


def heat_color(price: float, lo: float, hi: float) -> str:
    if hi <= lo:
        return "rgb(60,180,120)"
    t = max(0.0, min(1.0, (price - lo) / (hi - lo)))
    # зелёный → жёлтый → красный
    if t < 0.5:
        k = t / 0.5
        r = int(60 + (224 - 60) * k)
        g = int(180 + (170 - 180) * k)
        b = int(120 + (60 - 120) * k)
    else:
        k = (t - 0.5) / 0.5
        r = int(224 + (224 - 224) * k)
        g = int(170 + (90 - 170) * k)
        b = int(60 + (80 - 60) * k)
    return f"rgb({r},{g},{b})"


def month_dates(month: str) -> list[dt.date]:
    y, m = month.split("-")
    y_i, m_i = int(y), int(m)
    if m_i == 12:
        nxt = dt.date(y_i + 1, 1, 1)
    else:
        nxt = dt.date(y_i, m_i + 1, 1)
    days = (nxt - dt.timedelta(days=1)).day
    return [dt.date(y_i, m_i, d) for d in range(1, days + 1)]


def render_calendar(prices: list[dict[str, object]], month: str) -> str:
    """Сетка: строки=типы маршрута, столбцы=даты, ячейка=цена."""
    # собираем минимальную цену на каждую (route_type, date)
    by_key: dict[tuple[str, str], dict[str, object]] = {}
    for p in prices:
        try:
            t = int(str(p.get("transfers")))
        except (TypeError, ValueError):
            continue
        if t > MAX_TRANSFERS:
            continue
        if _as_float(p.get("price")) is None:
            continue
        rt = "direct" if t == 0 else "onestop"
        key = (rt, str(p["date"]))
        cur = by_key.get(key)
        if cur is None or _as_float(p.get("price")) < _as_float(cur.get("price")):
            by_key[key] = p

    dates = month_dates(month)
    if not dates:
        return ""

    all_prices = [
        float(str(p["price"]))  # type: ignore[arg-type]
        for p in by_key.values()
    ]
    lo = min(all_prices) if all_prices else 0.0
    hi = max(all_prices) if all_prices else 1.0

    # шапка: каждую неделю между датами рисуем разделитель
    head_cells = ""
    last_w = None
    for d in dates:
        w = d.isocalendar().week
        sep = "sep-l" if w != last_w else ""
        last_w = w
        wd_cls = "we" if d.weekday() >= 5 else "wd"
        head_cells += (
            f'<div class="cal-head {wd_cls} {sep}" '
            f'title="{d.isoformat()} ({WD_LONG[d.weekday()]})">'
            f"{d.day:02d}</div>"
        )

    rows = ""
    for rt_label, rt_key in [("прямой", "direct"), ("1 пересадка", "onestop")]:
        cells = ""
        last_w = None
        for d in dates:
            p = by_key.get((rt_key, d.isoformat()))
            w = d.isocalendar().week
            sep = "sep-l" if w != last_w else ""
            last_w = w
            wd_cls = "we" if d.weekday() >= 5 else "wd"
            if p is None:
                cell = (
                    f'<div class="cal-cell empty {wd_cls} {sep}" '
                    f'title="{d.isoformat()} — нет данных">—</div>'
                )
            else:
                price = _as_float(p.get("price")) or 0.0
                color = heat_color(price, lo, hi)
                title = (
                    f"{d.isoformat()} ({WD_LONG[d.weekday()]})\n"
                    f"{rub(price)}\n"
                    f"a/к {p.get('airline', '')} · "
                    f"{int(str(p['transfers']))} перес."
                )
                label = f"{int(round(price)) // 1000}k"
                if price < 10000:
                    label = f"{int(round(price))}"
                cell = (
                    f'<a class="cal-cell {wd_cls} {sep}" '
                    f'style="background:{color}" title="{title}" '
                    f'href="#{rt_key}-{d.isoformat()}">'
                    f"{label}</a>"
                )
            cells += cell
        rows += (
            f'<div class="cal-row">'
            f'<div class="cal-tag">{rt_label}</div>'
            f'<div class="cal-cells">{cells}</div>'
            f"</div>"
        )

    return (
        f'<div class="calendar" id="cal-{month}">'
        f'<div class="cal-row">'
        f'<div class="cal-tag cal-tag-h">Дата</div>'
        f'<div class="cal-cells cal-cells-h">{head_cells}</div>'
        f"</div>"
        f"{rows}</div>"
    )


def render_weekday_table(by_wd: list[dict[str, object]], eur_rate: float | None) -> str:
    if not by_wd:
        return '<p class="muted">нет данных для статистики.</p>'
    best_wd = min(by_wd, key=lambda s: float(str(s["min"])))  # type: ignore[arg-type]
    rows = ""
    for s in by_wd:
        wd_i = int(str(s["wd"]))
        cls = "best" if wd_i == int(str(best_wd["wd"])) else ""
        wd_name = WD_LONG[wd_i]
        rows += (
            f'<tr class="{cls}"><td>{wd_name}</td>'
            f'<td class="num">{s["count"]}</td>'
            f'<td class="num">{rub(s["min"])}</td>'
            f'<td class="num">{eur(s["min"] / eur_rate) if eur_rate else "—"}</td>'
            f'<td class="num">{rub(s["avg"])}</td>'
            f'<td class="num">{rub(s["max"])}</td></tr>'
        )
    return (
        f'<table class="wd-table"><thead><tr>'
        f'<th>День недели</th><th class="num">дат</th>'
        f'<th class="num">мин ₽</th><th class="num">мин €</th>'
        f'<th class="num">средн</th><th class="num">макс</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def render_topcards(
    top_by_origin: dict[str, dict[str, object | None]], eur_rate: float | None
) -> str:
    out = ""
    for origin_code, origin_label in ORIGINS:
        bucket = top_by_origin.get(origin_code, {})
        direct = bucket.get("direct") if isinstance(bucket, dict) else None
        onestop = bucket.get("onestop") if isinstance(bucket, dict) else None
        out += f'<h3 class="sec">{html.escape(origin_label)}</h3><div class="grid">'
        for label, p, tag in [
            ("Прямой (мин. за период)", direct, "direct"),
            ("С 1 пересадкой (мин. за период)", onestop, "onestop"),
        ]:
            if not isinstance(p, dict):
                out += (
                    f'<article class="card {tag} na">'
                    f'<div class="opt">{label}</div>'
                    f'<div class="price na">нет данных</div>'
                    f'<div class="muted">— нет рейсов на ближайшие {HORIZON_DAYS} дн —</div>'
                    f"</article>"
                )
                continue
            price = _as_float(p.get("price")) or 0.0
            eur_price = price / eur_rate if eur_rate else None
            detail_parts = [
                f"вылет {p.get('date', '')}",
                f"{int(str(p['transfers']))} перес."
                if p.get("transfers") != ""
                else "",
                dur_str(p.get("duration")),
                f"a/к {p.get('airline', '')}",
            ]
            detail = " · ".join(x for x in detail_parts if x)
            price_eur_html = (
                f'<div class="eur">{eur(eur_price)}</div>'
                if eur_price is not None
                else ""
            )
            link = aviasales_link(
                origin_code, "TBS", str(p.get("date", "")), p.get("link")
            )
            out += (
                f'<article class="card {tag}">'
                f'<div class="opt">{label}</div>'
                f'<div class="price">{rub(price)}</div>'
                f"{price_eur_html}"
                f'<div class="meta">{detail}</div>'
                f"{link}"
                f"</article>"
            )
        out += "</div>"
    return out


def render_history_spark(history: list[dict[str, object]]) -> str:
    pts: list[tuple[str, float]] = []
    for row in history:
        try:
            price = float(str(row["price_rub"]))
        except (TypeError, ValueError, KeyError):
            continue
        ts = str(row.get("checked_at", ""))
        pts.append((ts, price))
    if len(pts) < 2:
        return (
            '<p class="muted">График появится после второго запуска '
            "(минимум 2 точки истории).</p>"
        )
    prices = [p for _, p in pts]
    lo, hi = min(prices), max(prices)
    span = (hi - lo) or 1.0
    w_, h_, pad = 760, 150, 14
    n = len(pts)
    coords = [
        (
            pad + (w_ - 2 * pad) * i / (n - 1),
            pad + (h_ - 2 * pad) * (1 - (p - lo) / span),
        )
        for i, (_, p) in enumerate(pts)
    ]
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords)
    )
    lx, ly = coords[-1]
    first_ts = pts[0][0]
    last_ts = pts[-1][0]
    grid_y = [pad + (h_ - 2 * pad) * i / 4 for i in range(5)]
    grid_lines = "".join(
        f'<line x1="{pad}" x2="{w_ - pad}" y1="{y:.1f}" y2="{y:.1f}" '
        f'stroke="var(--line)" stroke-dasharray="2,4"/>'
        for y in grid_y
    )
    label_lo = (
        f'<text x="{w_ - pad}" y="{pad + 8}" class="hi-lo">max {int(hi):,}</text>'
    )
    label_hi = (
        f'<text x="{w_ - pad}" y="{h_ - pad}" class="hi-lo">min {int(lo):,}</text>'
    )
    return (
        f'<svg viewBox="0 0 {w_} {h_}" class="spark" '
        f'aria-label="История цен">'
        f"{grid_lines}"
        f'<path d="{path}" fill="none" stroke="var(--accent)" '
        f'stroke-width="2"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" '
        f'fill="var(--accent)"/>'
        f"{label_lo}{label_hi}</svg>"
        f'<div class="muted">per-run min, {len(pts)} точек: '
        f"{first_ts[:16]} → {last_ts[:16]}</div>"
    )


def render_history_table(history: list[dict[str, object]]) -> str:
    rows = ""
    count = 0
    for h in reversed(history):
        try:
            price = float(str(h["price_rub"]))
        except (TypeError, ValueError, KeyError):
            continue
        if h.get("depart_date") not in ("", None):
            date_label = (
                f"{h.get('depart_date', '')} "
                f"({WD_SHORT[int(dt.date.fromisoformat(str(h['depart_date'])).weekday())]})"
            )
        else:
            date_label = ""
        rt = "прямой" if h.get("route_type") == "direct" else "1 перес."
        eur_val = ""
        if h.get("eur_rate") not in ("", None) and h.get("price_eur") not in ("", None):
            eur_val = str(h["price_eur"])
        # Простой показ в ₽ (price_eur в CSV — это цена в евро, не отформатированная)
        try:
            eur_int = int(float(str(h.get("price_eur", ""))))
            eur_label = f"~{eur_int:,} €".replace(",", " ")
        except (TypeError, ValueError):
            eur_label = "—"
        rows += (
            f'<tr><td class="ts">{html.escape(str(h.get("checked_at", "")))}</td>'
            f"<td>{html.escape(str(h.get('origin', '')))}</td>"
            f"<td>{html.escape(str(h.get('route_type', '')))}</td>"
            f"<td>{html.escape(date_label)}</td>"
            f'<td class="num">{rub(price)}</td>'
            f'<td class="num">{eur_label}</td></tr>'
        )
        count += 1
        if count >= 60:
            break
    return (
        f'<table class="hist-table"><thead><tr>'
        f"<th>Проверено (UTC)</th><th>Из</th><th>Тип</th>"
        f'<th>Вылет</th><th class="num">₽</th>'
        f'<th class="num">€</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


# ---------------------------- main ----------------------------------------


def to_eur(rub_val: object, rate: float | None) -> float | None:
    f = _as_float(rub_val)
    if f is None or rate is None:
        return None
    return f / rate


def build_topcards(
    prices_by_origin: dict[str, list[dict[str, object]]], today: dt.date
) -> dict[str, dict[str, dict[str, object] | None]]:
    """
    Для каждого origin -> {"direct": лучший за горизонт, "onestop": ...}.
    """
    result: dict[str, dict[str, dict[str, object] | None]] = {}
    for origin_code, _ in ORIGINS:
        prices = prices_by_origin.get(origin_code, [])
        direct, onestop = split_by_route(prices)
        result[origin_code] = {
            "direct": best_in_window(direct, today, HORIZON_DAYS),
            "onestop": best_in_window(onestop, today, HORIZON_DAYS),
        }
    return result


def main() -> int:
    if not TOKEN:
        print("ОШИБКА: TRAVELPAYOUTS_TOKEN не задан.", file=sys.stderr)
        return 1

    today = dt.date.today()
    eur_rate = get_eur_rate()

    print(f"[info] месяц={DEPART_MONTH}, сегодня={today}, курс EUR={eur_rate}")
    prices_by_origin: dict[str, list[dict[str, object]]] = {}
    for origin_code, _ in ORIGINS:
        for dest_code, _ in DESTINATIONS:
            prices_by_origin[origin_code] = fetch_prices_month(
                origin_code, dest_code, DEPART_MONTH
            )

    # 1) пишем в историю
    checked_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    hist_rows: list[dict[str, object]] = []
    for origin_code, _ in ORIGINS:
        for p in prices_by_origin.get(origin_code, []):
            if _as_float(p.get("price")) is None:
                continue
            try:
                t = int(str(p.get("transfers")))
            except (TypeError, ValueError):
                continue
            if t > MAX_TRANSFERS:
                continue
            rt = "direct" if t == 0 else "onestop"
            depart_date = str(p.get("date", ""))
            try:
                wd = dt.date.fromisoformat(depart_date).weekday()
                wd_str = WD_SHORT[wd]
            except ValueError:
                wd_str = ""
            eur_val = to_eur(p.get("price"), eur_rate)
            hist_rows.append(
                {
                    "checked_at": checked_at,
                    "origin": origin_code,
                    "dest": DESTINATIONS[0][0],
                    "route_type": rt,
                    "depart_date": depart_date,
                    "weekday": wd_str,
                    "price_rub": int(round(float(str(p["price"])))),  # type: ignore[arg-type]
                    "price_eur": int(round(eur_val)) if eur_val is not None else "",
                    "eur_rate": f"{eur_rate:.4f}" if eur_rate else "",
                    "airline": p.get("airline", ""),
                    "transfers": t,
                    "duration": p.get("duration", ""),
                    "link": p.get("link", ""),
                }
            )
    if hist_rows:
        append_history(hist_rows)

    # 2) собираем аналитику
    history = read_history()
    top_by_origin = build_topcards(prices_by_origin, today)

    # объединяем для календаря/недельной статистики (без фильтра по origins)
    all_prices: list[dict[str, object]] = []
    for _, lst in prices_by_origin.items():
        all_prices.extend(lst)
    wd_stats = weekday_breakdown(all_prices)

    # шаблон
    now = dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=3))
    )
    stamp = now.strftime("%d.%m.%Y %H:%M МСК")
    y, m = DEPART_MONTH.split("-")
    month_ru = f"{MONTHS_RU.get(m, m)} {y}"
    rate_str = (
        f"курс ЦБ: 1 € = {eur_rate:.2f} ₽"
        if eur_rate
        else "курс ЦБ недоступен — цены только в ₽"
    )

    html_doc = render_report(
        top_by_origin=top_by_origin,
        prices=all_prices,
        wd_stats=wd_stats,
        history=history,
        eur_rate=eur_rate,
        month_ru=month_ru,
        month_iso=DEPART_MONTH,
        stamp=stamp,
        rate_str=rate_str,
    )

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html_doc, encoding="utf-8")

    priced = sum(
        1
        for p in all_prices
        if _as_float(p.get("price")) is not None
        and (lambda x: x <= MAX_TRANSFERS)(  # type: ignore[arg-type]
            int(str(p.get("transfers")))
            if str(p.get("transfers", "")).lstrip("-").isdigit()
            else 99
        )
    )
    print(
        f"[ok] карточек с ценой: {priced} (≤{MAX_TRANSFERS} перес.), "
        f"история: {len(history)} строк"
    )
    return 0


TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Мониторинг цен · → Тбилиси · {month_ru}</title>
<style>
  :root {{
    --bg:#101216; --panel:#181c24; --ink:#e8eaed; --muted:#8b93a3;
    --line:#262b36; --accent:#e0a03e; --down:#5fbf7f; --up:#e06a5c;
    --direct:#6dc3a5; --onestop:#e0a03e;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    line-height:1.5; padding:28px 18px 64px; }}
  .wrap {{ max-width:960px; margin:0 auto; }}
  header {{ border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:6px; }}
  .eyebrow {{ font-family:var(--mono); font-size:12px; letter-spacing:.14em;
    text-transform:uppercase; color:var(--muted); }}
  h1 {{ font-size:24px; margin:6px 0 4px; font-weight:650; }}
  h2 {{ font-size:18px; margin:34px 0 8px; font-weight:600; }}
  h3.sec {{ font-size:15px; margin:22px 0 8px; font-weight:600;
    color:var(--muted); letter-spacing:.04em; }}
  .stamp {{ font-family:var(--mono); font-size:13px; color:var(--muted); }}
  .grid {{ display:grid; gap:12px; grid-template-columns:1fr; margin-bottom:4px; }}
  @media(min-width:560px){{ .grid {{ grid-template-columns:1fr 1fr; }} }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:16px; border-top:3px solid var(--line); position:relative; }}
  .card.direct {{ border-top-color:var(--direct); }}
  .card.onestop {{ border-top-color:var(--onestop); }}
  .card.na {{ opacity:.7; }}
  .opt {{ font-size:11px; font-family:var(--mono); text-transform:uppercase;
    letter-spacing:.06em; color:var(--muted); margin-bottom:10px; }}
  .price {{ font-family:var(--mono); font-size:22px; font-weight:600; }}
  .price.na {{ color:var(--muted); font-size:15px; font-weight:500; }}
  .eur {{ font-family:var(--mono); font-size:13px; color:var(--muted); margin-top:2px; }}
  .meta {{ color:var(--muted); font-size:12px; margin:8px 0 10px; }}
  .btn {{ display:inline-block; font-size:12px; color:var(--accent); text-decoration:none;
    border:1px solid var(--line); border-radius:7px; padding:5px 10px; margin:2px 4px 2px 0; }}
  .btn:hover {{ border-color:var(--accent); }}
  .muted {{ color:var(--muted); font-size:12px; }}

  /* Календарь */
  .calendar {{ background:var(--panel); border:1px solid var(--line);
    border-radius:12px; padding:14px; overflow-x:auto; }}
  .cal-row {{ display:flex; align-items:stretch; min-width:fit-content; }}
  .cal-tag {{ flex:0 0 110px; font-family:var(--mono); font-size:11px;
    color:var(--muted); text-transform:uppercase; letter-spacing:.06em;
    display:flex; align-items:center; padding-right:10px; }}
  .cal-tag-h {{ color:var(--ink); font-weight:600; }}
  .cal-cells {{ display:grid; grid-auto-flow:column;
    grid-auto-columns:34px; gap:2px; flex:1; }}
  .cal-head {{ height:24px; display:flex; align-items:center; justify-content:center;
    font-family:var(--mono); font-size:11px; color:var(--muted);
    border-top:1px solid var(--line); padding-top:2px; }}
  .cal-head.we {{ color:var(--accent); opacity:.7; }}
  .cal-head.sep-l {{ border-left:1px dashed var(--line); padding-left:4px; }}
  .cal-cell {{ height:34px; display:flex; align-items:center; justify-content:center;
    font-family:var(--mono); font-size:11px; color:#101216;
    text-decoration:none; border-radius:4px; font-weight:600; }}
  .cal-cell.we {{ box-shadow:inset 0 0 0 1px #00000022; }}
  .cal-cell.sep-l {{ margin-left:3px; }}
  .cal-cell.empty {{ background:transparent; color:var(--muted); font-weight:400;
    border:1px dashed var(--line); }}
  .cal-cell.empty.we {{ color:var(--muted); }}

  /* Таблицы */
  table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:6px; }}
  th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }}
  th {{ color:var(--muted); font-weight:500; font-family:var(--mono); font-size:11px;
    text-transform:uppercase; letter-spacing:.06em; }}
  td.num,th.num {{ text-align:right; font-family:var(--mono); }}
  tr.best td {{ color:var(--down); }}
  .hist-table td.ts {{ font-family:var(--mono); color:var(--muted); font-size:12px; }}
  .wd-table {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
    overflow:hidden; }}

  .spark {{ width:100%; height:auto; display:block; background:var(--panel);
    border:1px solid var(--line); border-radius:12px; padding:10px; }}
  .spark text.hi-lo {{ fill:var(--muted); font:600 10px var(--mono); text-anchor:end; }}

  footer {{ margin-top:36px; color:var(--muted); font-size:12px;
    border-top:1px solid var(--line); padding-top:14px; }}
  a {{ color:var(--accent); }}
  .legend {{ display:flex; gap:14px; flex-wrap:wrap; font-size:12px;
    color:var(--muted); margin-top:6px; }}
  .swatch {{ display:inline-block; width:14px; height:14px; border-radius:3px;
    vertical-align:middle; margin-right:4px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">Fare monitor · → Тбилиси · {month_ru}</div>
    <h1>Прямые и с одной пересадкой — в Тбилиси</h1>
    <div class="stamp">Проверено: {stamp} · {rate_str}</div>
    <div class="legend">
      <span><span class="swatch" style="background:rgb(60,180,120)"></span>дешевле</span>
      <span><span class="swatch" style="background:rgb(224,170,60)"></span>средне</span>
      <span><span class="swatch" style="background:rgb(224,90,80)"></span>дороже</span>
      <span><span class="swatch" style="background:transparent;border:1px dashed var(--line)"></span>нет данных</span>
      <span>· граница между неделями — пунктир</span>
    </div>
  </header>

  <h2>Лучшие цены на ближайшие {horizon} дней</h2>
  {topcards}

  <h2>Календарь цен — {month_ru}</h2>
  <p class="muted">Чем зеленее — тем дешевле (по всему столбцу за месяц).
    Кликните на ячейку, чтобы получить ссылку на Aviasales.</p>
  {calendar}

  <h2>Статистика по дням недели</h2>
  <p class="muted">Лучший день подсвечен. Анализ по всем рейсам ≤ {max_t} пересадок
    за выбранный месяц.</p>
  {weekday_table}

  <h2>История проверок — динамика минимума</h2>
  {history_chart}

  <h2>Таблица последних замеров</h2>
  {history_table}

  <footer>
    Цены: Travelpayouts (Aviasales) Data API — кеш поисков за ~48 ч, ориентир
    по тренду, не финальная цена. Учитываются только прямые рейсы и рейсы с
    одной пересадкой (без self-connect). Евро — по официальному курсу ЦБ РФ.
    Перед покупкой сверяйся на aviasales.ru.
  </footer>
</div>
</body>
</html>
"""


def render_report(
    *,
    top_by_origin: dict[str, dict[str, dict[str, object] | None]],
    prices: list[dict[str, object]],
    wd_stats: list[dict[str, object]],
    history: list[dict[str, object]],
    eur_rate: float | None,
    month_ru: str,
    month_iso: str,
    stamp: str,
    rate_str: str,
) -> str:
    topcards = render_topcards(top_by_origin, eur_rate)
    calendar = render_calendar(prices, month_iso)
    weekday_table = render_weekday_table(wd_stats, eur_rate)
    history_chart = render_history_spark(history)
    history_table = render_history_table(history)
    return TEMPLATE.format(
        month_ru=month_ru,
        stamp=stamp,
        rate_str=rate_str,
        horizon=HORIZON_DAYS,
        max_t=MAX_TRANSFERS,
        topcards=topcards,
        calendar=calendar,
        weekday_table=weekday_table,
        history_chart=history_chart,
        history_table=history_table,
    )


if __name__ == "__main__":
    raise SystemExit(main())
