#!/usr/bin/env python3
"""
Мониторинг цен на авиабилеты Москва -> Венеция / Тревизо.

Тянет свежие цены из Travelpayouts (Aviasales) Data API, дописывает историю
в data/history.csv и пересобирает docs/index.html (график + таблица).
Запускается по расписанию через GitHub Actions (см. .github/workflows/monitor.yml).

ВАЖНО (честная оговорка): Data API отдаёт цены из кеша поисков Aviasales
за последние ~48 часов, а не живые котировки. Для отслеживания тренда
"дорожает / дешевеет" этого достаточно; финальную цену перед покупкой
всегда проверяй на самом aviasales.ru по ссылке из таблицы.
"""

import csv
import datetime as dt
import html
import os
import pathlib
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json

# ----------------------------- Настройки -----------------------------------

TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()

# Маршруты для мониторинга: (origin_iata, destination_iata, человекочитаемое имя)
ROUTES = [
    ("MOW", "VCE", "Москва → Венеция (Марко Поло)"),
    ("MOW", "TSF", "Москва → Тревизо (~20 км до Венеции)"),
]

CURRENCY = "rub"
# Смотрим ближайшие N месяцев вылета. Если нужна конкретная дата — см. DEPART_MONTHS ниже.
LOOKAHEAD_MONTHS = 6

BASE = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
DOCS_DIR = BASE / "docs"
HISTORY_CSV = DATA_DIR / "history.csv"

API_HOST = "https://api.travelpayouts.com"

# --------------------------- Работа с API ----------------------------------


def api_get(path: str, params: dict) -> dict:
    params = {**params, "token": TOKEN}
    url = f"{API_HOST}{path}?{urlencode(params)}"
    req = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "fare-monitor/1.0"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_cheapest(origin: str, destination: str) -> dict | None:
    """
    Самый дешёвый билет по маршруту на ближайший период.
    Использует endpoint aviasales/v3/prices_for_dates (сортировка по цене).
    Возвращает нормализованный dict или None.
    """
    try:
        data = api_get(
            "/aviasales/v3/prices_for_dates",
            {
                "origin": origin,
                "destination": destination,
                "currency": CURRENCY,
                "sorting": "price",
                "one_way": "true",
                "limit": 1,
                "page": 1,
            },
        )
    except (HTTPError, URLError, ValueError) as e:
        print(f"[warn] {origin}->{destination}: запрос не удался: {e}", file=sys.stderr)
        return None

    items = data.get("data") or []
    if not items:
        print(f"[info] {origin}->{destination}: цен в кеше нет сейчас", file=sys.stderr)
        return None

    it = items[0]
    link = it.get("link", "")
    full_link = f"https://www.aviasales.ru{link}" if link.startswith("/") else link
    return {
        "price": it.get("price"),
        "airline": it.get("airline", ""),
        "transfers": it.get("transfers", ""),
        "depart_date": it.get("departure_at", "")[:10],
        "duration": it.get("duration", ""),
        "link": full_link,
    }


# --------------------------- История (CSV) ---------------------------------

FIELDS = ["checked_at", "route", "origin", "destination", "price",
          "airline", "transfers", "depart_date", "duration", "link"]


def append_history(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def read_history() -> list[dict]:
    if not HISTORY_CSV.exists():
        return []
    with HISTORY_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --------------------------- Рендер страницы -------------------------------


def sparkline(points: list[float], w: int = 560, h: int = 120, pad: int = 8) -> str:
    """Простой SVG-график истории цен, без внешних зависимостей."""
    pts = [p for p in points if p is not None]
    if len(pts) < 2:
        return '<p class="muted">Пока мало точек для графика — появится после нескольких запусков.</p>'
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    n = len(pts)
    coords = []
    for i, p in enumerate(pts):
        x = pad + (w - 2 * pad) * (i / (n - 1))
        y = pad + (h - 2 * pad) * (1 - (p - lo) / span)
        coords.append((x, y))
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    last_x, last_y = coords[-1]
    return (
        f'<svg viewBox="0 0 {w} {h}" class="spark" role="img" '
        f'aria-label="История минимальной цены">'
        f'<path d="{path}" fill="none" stroke="var(--accent)" stroke-width="2"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.5" fill="var(--accent)"/>'
        f'</svg>'
    )


def render(latest: list[dict], history: list[dict]) -> str:
    now = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=3)))
    stamp = now.strftime("%d.%m.%Y %H:%M МСК")

    cards = []
    for row in latest:
        route = row["route"]
        hist_prices = [
            float(h["price"]) for h in history
            if h["route"] == route and h.get("price") not in ("", None)
        ]
        if row["price"] is None:
            price_block = '<div class="price na">нет данных</div>'
            meta = '<div class="meta">В кеше сейчас нет цен по этому маршруту</div>'
            link = ""
        else:
            price = int(row["price"])
            prev = hist_prices[-2] if len(hist_prices) >= 2 else None
            delta_html = ""
            if prev is not None:
                d = price - prev
                if d < 0:
                    delta_html = f'<span class="delta down">▼ {abs(int(d)):,} ₽</span>'.replace(",", " ")
                elif d > 0:
                    delta_html = f'<span class="delta up">▲ {int(d):,} ₽</span>'.replace(",", " ")
                else:
                    delta_html = '<span class="delta flat">без изменений</span>'
            lo = int(min(hist_prices)) if hist_prices else price
            price_str = f"{price:,} ₽".replace(",", " ")
            price_block = f'<div class="price">{price_str} {delta_html}</div>'
            t = row["transfers"]
            t_str = "прямой" if str(t) == "0" else f"{t} пересадк." if t != "" else ""
            dur = row["duration"]
            dur_str = f"{int(dur)//60} ч {int(dur)%60} мин" if str(dur).isdigit() else ""
            meta = (
                f'<div class="meta">вылет {html.escape(str(row["depart_date"]))} · '
                f'{html.escape(t_str)} · {html.escape(dur_str)} · '
                f'a/к {html.escape(str(row["airline"]))} · '
                f'минимум за всё время: {lo:,} ₽</div>'.replace(",", " ")
            )
            link = (
                f'<a class="btn" href="{html.escape(row["link"])}" target="_blank" '
                f'rel="noopener">Проверить на Aviasales →</a>' if row["link"] else ""
            )
        cards.append(f"""
        <article class="card">
          <h2>{html.escape(route)}</h2>
          {price_block}
          {meta}
          {sparkline(hist_prices)}
          {link}
        </article>""")

    # Таблица последних записей истории (свежие сверху)
    rows_html = ""
    for h in reversed(history[-40:]):
        if h.get("price") in ("", None):
            continue
        p = f"{int(float(h['price'])):,} ₽".replace(",", " ")
        rows_html += (
            f"<tr><td>{html.escape(h['checked_at'])}</td>"
            f"<td>{html.escape(h['route'])}</td>"
            f"<td class='num'>{p}</td>"
            f"<td>{html.escape(str(h.get('depart_date','')))}</td>"
            f"<td>{html.escape(str(h.get('airline','')))}</td></tr>"
        )

    return TEMPLATE.format(stamp=stamp, cards="".join(cards), rows=rows_html)


TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Мониторинг цен · Москва → Венеция</title>
<style>
  :root {{
    --bg:#12151c; --panel:#1a1f29; --ink:#e8eaed; --muted:#8b93a3;
    --line:#2a313d; --accent:#e0a03e; --down:#5fbf7f; --up:#e06a5c;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    line-height:1.5; padding:32px 20px 64px;
  }}
  .wrap {{ max-width:760px; margin:0 auto; }}
  header {{ border-bottom:1px solid var(--line); padding-bottom:16px; margin-bottom:28px; }}
  .eyebrow {{ font-family:var(--mono); font-size:12px; letter-spacing:.14em;
    text-transform:uppercase; color:var(--muted); }}
  h1 {{ font-size:26px; margin:6px 0 4px; font-weight:650; }}
  .stamp {{ font-family:var(--mono); font-size:13px; color:var(--muted); }}
  .grid {{ display:grid; gap:16px; grid-template-columns:1fr; }}
  @media(min-width:640px){{ .grid {{ grid-template-columns:1fr 1fr; }} }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:18px 18px 20px; }}
  .card h2 {{ font-size:15px; font-weight:600; margin:0 0 10px; color:var(--ink); }}
  .price {{ font-family:var(--mono); font-size:28px; font-weight:600; }}
  .price.na {{ color:var(--muted); font-size:20px; }}
  .delta {{ font-size:13px; font-family:var(--mono); margin-left:6px; }}
  .delta.down {{ color:var(--down); }} .delta.up {{ color:var(--up); }}
  .delta.flat {{ color:var(--muted); }}
  .meta {{ color:var(--muted); font-size:13px; margin:8px 0 12px; }}
  .spark {{ width:100%; height:auto; display:block; margin:6px 0 14px; }}
  .btn {{ display:inline-block; font-size:13px; color:var(--accent);
    text-decoration:none; border:1px solid var(--line); border-radius:8px;
    padding:6px 12px; }}
  .btn:hover {{ border-color:var(--accent); }}
  .muted {{ color:var(--muted); font-size:13px; }}
  h3 {{ font-size:14px; text-transform:uppercase; letter-spacing:.08em;
    color:var(--muted); margin:36px 0 10px; font-weight:600; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }}
  th {{ color:var(--muted); font-weight:500; font-family:var(--mono); font-size:11px;
    text-transform:uppercase; letter-spacing:.06em; }}
  td.num, th.num {{ text-align:right; font-family:var(--mono); }}
  footer {{ margin-top:40px; color:var(--muted); font-size:12px;
    border-top:1px solid var(--line); padding-top:16px; }}
  a {{ color:var(--accent); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">Fare monitor · обновление каждые 8 ч</div>
    <h1>Москва → Венеция</h1>
    <div class="stamp">Последняя проверка: {stamp}</div>
  </header>

  <div class="grid">{cards}</div>

  <h3>История проверок</h3>
  <table>
    <thead><tr><th>Проверено (UTC)</th><th>Маршрут</th><th class="num">Цена</th>
    <th>Вылет</th><th>A/к</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>

  <footer>
    Данные: Travelpayouts (Aviasales) Data API — цены из кеша поисков за ~48 ч,
    это ориентир по тренду, а не финальная цена. Прямых рейсов Москва → Венеция
    сейчас нет; варианты идут с пересадкой (обычно Белград или Ереван).
    Перед покупкой сверяйся на aviasales.ru.
  </footer>
</div>
</body>
</html>
"""


# ------------------------------- main --------------------------------------


def main() -> int:
    if not TOKEN:
        print("ОШИБКА: не задан TRAVELPAYOUTS_TOKEN (переменная окружения / секрет GitHub).",
              file=sys.stderr)
        return 1

    checked_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    latest, new_rows = [], []

    for origin, destination, name in ROUTES:
        res = fetch_cheapest(origin, destination)
        record = {
            "checked_at": checked_at, "route": name,
            "origin": origin, "destination": destination,
            "price": None, "airline": "", "transfers": "",
            "depart_date": "", "duration": "", "link": "",
        }
        if res:
            record.update(res)
            new_rows.append(record)
        latest.append(record)

    if new_rows:
        append_history(new_rows)

    history = read_history()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(render(latest, history), encoding="utf-8")
    print(f"Готово: {len(new_rows)} новых цен, всего в истории {len(history)} записей.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
