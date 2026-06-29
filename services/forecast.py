import logging
import statistics
import sqlite3
import db
from config import DB_PATH

async def calculate_and_update_forecasts(http_session):
    from tinkoff_api import get_portfolio_summary
    portfolio = await get_portfolio_summary(http_session)
    if not portfolio:
        logging.warning("Не удалось получить портфель для расчёта прогнозов")
        return
    tickers = [pos["ticker"] for pos in portfolio.get("positions", []) if pos.get("type_display") == "Акции"]
    for ticker in tickers:
        try:
            forecast = calculate_forecast_for_ticker(ticker)
            if forecast:
                db.upsert_dividend_forecast(
                    ticker,
                    forecast["amount"],
                    forecast["month"],
                    forecast["year"],
                    forecast["confidence"],
                    "historical_cagr"
                )
                logging.info(f"Прогноз для {ticker}: {forecast['amount']:.2f} ₽ в {forecast['month']}/{forecast['year']}")
            else:
                db.upsert_dividend_forecast(ticker, 0, 0, 0, 0, "none")
        except Exception as e:
            logging.error(f"Ошибка расчёта прогноза для {ticker}: {e}")

def calculate_forecast_for_ticker(ticker: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT date, payment 
        FROM operations 
        WHERE ticker = ? AND type = 'Выплата дивидендов' AND currency = 'RUB'
        ORDER BY date
    """, (ticker,))
    rows = c.fetchall()
    conn.close()
    if len(rows) < 2:
        return None
    yearly = {}
    for date_str, payment in rows:
        year = int(date_str[:4])
        if year not in yearly:
            yearly[year] = 0.0
        yearly[year] += payment
    years = sorted(yearly.keys())
    if len(years) < 2:
        return None
    recent_years = years[-3:]
    if len(recent_years) < 2:
        return None
    first_year = recent_years[0]
    last_year = recent_years[-1]
    first_amount = yearly[first_year]
    last_amount = yearly[last_year]
    if first_amount == 0:
        return None
    cagr = (last_amount / first_amount) ** (1 / (len(recent_years) - 1)) - 1
    next_year = last_year + 1
    forecast_amount = last_amount * (1 + cagr)
    months = []
    for date_str, _ in rows:
        month = int(date_str[5:7])
        months.append(month)
    median_month = int(statistics.median(months))
    if len(recent_years) >= 3:
        confidence = 1.0
    elif len(recent_years) == 2:
        confidence = 0.8
    else:
        confidence = 0.5
    return {
        "amount": max(0, forecast_amount),
        "month": median_month,
        "year": next_year,
        "confidence": confidence
    }