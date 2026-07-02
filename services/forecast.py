import logging
import statistics
import db
from datetime import date, timedelta
from collections import defaultdict

STABLE_SECTORS = {"Нефтегаз", "Финансы", "Телеком", "Энергетика", "Транспорт"}
GROWTH_SECTORS = {"ИТ", "Товары", "Медицина", "Стройка"}
MAX_HISTORY_DAYS = 365 * 6


async def calculate_and_update_forecasts(http_session):
    from tinkoff_api import get_portfolio_summary

    portfolio = await get_portfolio_summary(http_session)
    if not portfolio:
        logging.warning("Не удалось получить портфель для расчёта прогнозов")
        return

    tickers = [pos["ticker"] for pos in portfolio.get("positions", []) if pos.get("type_display") == "Акции"]

    for ticker in tickers:
        try:
            forecasts = await calculate_forecast_for_ticker(ticker)
            await db.upsert_dividend_forecasts(ticker, forecasts)
            for f in forecasts:
                logging.info(
                    f"Прогноз {ticker}: {f['amount']:.2f} ₽ в {f['month']}/{f['year']} "
                    f"(conf={f['confidence']:.1f}, {f['method']})"
                )
        except Exception as e:
            logging.error(f"Ошибка расчёта прогноза для {ticker}: {e}", exc_info=True)


async def calculate_forecast_for_ticker(ticker: str) -> list[dict]:
    dividend_db = await db.get_db()
    c = await dividend_db.execute(
        """
        SELECT payment_date, dividend_net
        FROM dividend_calendar
        WHERE ticker = ? AND dividend_net > 0
        ORDER BY payment_date
        """,
        (ticker,),
    )
    rows = await c.fetchall()
    if not rows:
        return []

    today = date.today()

    past: list[tuple[str, float]] = []
    future: list[tuple[str, float]] = []
    for date_str, amount in rows:
        year, month_str = int(date_str[:4]), int(date_str[5:7])
        day = int(date_str[8:10]) if len(date_str) >= 10 else 1
        if date(year, month_str, day) < today:
            past.append((date_str, amount))
        else:
            future.append((date_str, amount))

    result = []
    if future:
        for date_str, amount in future:
            result.append({
                "amount": amount,
                "month": int(date_str[5:7]),
                "year": int(date_str[:4]),
                "confidence": 1.0,
                "method": "declared",
            })

    cutoff = today - timedelta(days=MAX_HISTORY_DAYS)
    past = [(ds, amt) for ds, amt in past if _parse_date(ds) >= cutoff]

    if len(past) < 3:
        return result

    freq = classify_frequency(past)
    if freq == "unknown":
        return result

    sector = await db.get_sector(ticker)

    slots = group_by_slots(past, freq)

    strategy = choose_strategy(ticker, sector, freq, slots)

    all_entries = past + future
    periods_per_year = {"annual": 1, "semi_annual": 2, "quarterly": 4}
    count = periods_per_year.get(freq, 1)

    for period_idx in range(1, count + 1):
        slot_key = build_slot_key(freq, period_idx)
        hist_values = slots.get(slot_key, [])
        if not hist_values:
            continue

        month = estimate_month_for_period(freq, period_idx, past)

        slot_years = []
        for date_str, _ in all_entries:
            dt = _parse_date(date_str)
            if _slot_key_for_date(freq, dt) == slot_key:
                slot_years.append(dt.year)
        if not slot_years:
            continue
        target_year = max(slot_years) + 1

        already_declared = any(
            f["month"] == month and f["year"] == target_year for f in result
        )
        if already_declared:
            continue

        period_forecast = forecast_period_by_strategy(
            hist_values, strategy, target_year, month, sector, ticker
        )
        if period_forecast and period_forecast["amount"] > 0:
            result.append(period_forecast)

    return result


def classify_frequency(past: list[tuple[str, float]]) -> str:
    dates = sorted(past, key=lambda x: x[0])
    if len(dates) < 3:
        return "unknown"

    intervals = []
    prev = _parse_date(dates[0][0])
    for date_str, _ in dates[1:]:
        curr = _parse_date(date_str)
        diff = (curr - prev).days
        if diff > 0:
            intervals.append(diff)
        prev = curr

    if not intervals:
        return "unknown"

    median_interval = statistics.median(intervals)

    if 340 <= median_interval <= 395:
        return "annual"
    elif 150 <= median_interval <= 210:
        return "semi_annual"
    elif 75 <= median_interval <= 110:
        return "quarterly"
    elif median_interval < 45:
        return "monthly"
    else:
        return "unknown"


def _parse_date(date_str: str) -> date:
    parts = date_str.split("-")
    return date(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 1)


def group_by_slots(past: list[tuple[str, float]], freq: str) -> dict[str, list[float]]:
    slots: dict[str, list[float]] = defaultdict(list)
    for date_str, amount in past:
        dt = _parse_date(date_str)
        key = _slot_key_for_date(freq, dt)
        slots[key].append(amount)
    return dict(slots)


def _slot_key_for_date(freq: str, dt: date) -> str:
    if freq == "annual":
        return "Y"
    elif freq == "semi_annual":
        return "H1" if dt.month <= 6 else "H2"
    elif freq == "quarterly":
        q = (dt.month - 1) // 3 + 1
        return f"Q{q}"
    else:
        return f"M{dt.month:02d}"


def build_slot_key(freq: str, period_idx: int) -> str:
    if freq == "annual":
        return "Y"
    elif freq == "semi_annual":
        return f"H{period_idx}"
    elif freq == "quarterly":
        return f"Q{period_idx}"
    else:
        return f"M{period_idx:02d}"


def estimate_month_for_period(freq: str, period_idx: int, past: list[tuple[str, float]]) -> int:
    if freq == "annual":
        months = [int(ds[5:7]) for ds, _ in past]
        return int(statistics.median(months)) if months else 6
    elif freq == "semi_annual":
        if period_idx == 1:
            months = [int(ds[5:7]) for ds, _ in past if 1 <= int(ds[5:7]) <= 6]
            return int(statistics.median(months)) if months else 5
        else:
            months = [int(ds[5:7]) for ds, _ in past if 7 <= int(ds[5:7]) <= 12]
            return int(statistics.median(months)) if months else 11
    elif freq == "quarterly":
        return period_idx * 3
    else:
        return period_idx


def choose_strategy(ticker: str, sector: str, freq: str, slots: dict[str, list[float]]) -> str:
    all_values = [v for vals in slots.values() for v in vals]
    if len(all_values) < 2:
        return "sector_average"

    if sector in STABLE_SECTORS:
        return "stable"
    if sector in GROWTH_SECTORS:
        return "growth"
    return "default_cagr"


def forecast_period_by_strategy(
    hist_values: list[float],
    strategy: str,
    target_year: int,
    month: int,
    sector: str,
    ticker: str,
) -> dict | None:
    if len(hist_values) == 0:
        return None

    sorted_vals = sorted(hist_values)
    median_val = statistics.median(sorted_vals)

    if len(sorted_vals) == 1:
        return {
            "amount": max(0, median_val),
            "month": month,
            "year": target_year,
            "confidence": 0.5,
            "method": "single_year",
        }

    if strategy == "stable":
        forecast = median_val
        if len(sorted_vals) >= 2:
            growth = (sorted_vals[-1] / sorted_vals[-2] - 1) if sorted_vals[-2] > 0 else 0
            if 0 < growth < 0.15:
                forecast = median_val * (1 + 0.07)
        confidence = min(0.5 + len(sorted_vals) * 0.1, 0.95)
        return _capped_forecast(forecast, median_val, month, target_year, confidence, "stable")

    if strategy == "growth":
        forecast = _linear_trend(sorted_vals)
        forecast = min(forecast, sorted_vals[-1] * 1.20)
        forecast = max(forecast, sorted_vals[-1] * 0.80)
        if forecast < 0:
            forecast = median_val
        confidence = min(0.4 + len(sorted_vals) * 0.15, 0.90)
        return _capped_forecast(forecast, median_val, month, target_year, confidence, "growth")

    if strategy == "default_cagr":
        if sorted_vals[0] > 0:
            cagr = (sorted_vals[-1] / sorted_vals[0]) ** (1 / (len(sorted_vals) - 1)) - 1
        else:
            cagr = 0
        forecast = sorted_vals[-1] * (1 + cagr)
        upper_cap = median_val * 2.0
        lower_cap = median_val * 0.5
        forecast = max(min(forecast, upper_cap), lower_cap)
        confidence = min(0.4 + len(sorted_vals) * 0.15, 0.90)
        return _capped_forecast(forecast, median_val, month, target_year, confidence, "default_cagr")

    return _capped_forecast(statistics.median(sorted_vals), statistics.median(sorted_vals), month, target_year, 0.5, "median_fallback")


def _linear_trend(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return values[-1] if values else 0
    xs = list(range(n))
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    num = sum((x - mean_x) * v for x, v in zip(xs, values))
    den = sum((x - mean_x) ** 2 for x in xs)
    slope = num / den if den != 0 else 0
    return mean_y + slope * n


def _capped_forecast(
    forecast: float,
    median_val: float,
    month: int,
    year: int,
    confidence: float,
    method: str,
) -> dict:
    result_forecast = max(0, forecast)
    return {
        "amount": round(result_forecast, 4),
        "month": month,
        "year": year,
        "confidence": round(confidence, 2),
        "method": method,
    }
