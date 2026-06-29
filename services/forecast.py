# services/forecast.py
import logging
import datetime
import statistics
import sqlite3
import db
from config import DB_PATH, NAME_OVERRIDES, ticker_to_name

async def calculate_and_update_forecasts(http_session):
    """Рассчитывает прогнозы дивидендов для всех акций в портфеле и сохраняет в БД."""
    from tinkoff_api import get_portfolio_summary
    
    portfolio = await get_portfolio_summary(http_session)
    if not portfolio:
        logging.warning("Не удалось получить портфель для расчёта прогнозов")
        return
    
    # Берём только акции (не облигации, не фонды)
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
                logging.info(f"Прогноз для {ticker}: {forecast['amount']:.2f} ₽ на акцию в {forecast['month']}/{forecast['year']}")
            else:
                # Если прогноз не удалось рассчитать, удаляем старую запись
                db.upsert_dividend_forecast(ticker, 0, 0, 0, 0, "none")
        except Exception as e:
            logging.error(f"Ошибка расчёта прогноза для {ticker}: {e}")

def calculate_forecast_for_ticker(ticker: str):
    """
    Рассчитывает прогноз дивидендов на акцию на основе исторических выплат.
    Использует данные из dividend_calendar (объявленные дивиденды на акцию).
    Возвращает словарь с полями: amount (на акцию), month, year, confidence.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Берём все записи из dividend_calendar для этого тикера, сортируем по дате
    c.execute("""
        SELECT payment_date, dividend_net
        FROM dividend_calendar
        WHERE ticker = ? AND dividend_net > 0
        ORDER BY payment_date
    """, (ticker,))
    rows = c.fetchall()
    conn.close()
    
    if len(rows) < 2:
        return None  # недостаточно данных
    
    # Группируем по году (суммируем все выплаты за год, если их несколько)
    yearly = {}
    for date_str, amount in rows:
        year = int(date_str[:4])
        if year not in yearly:
            yearly[year] = 0.0
        yearly[year] += amount
    
    years = sorted(yearly.keys())
    if len(years) < 2:
        return None
    
    # Берём последние 3 года
    recent_years = years[-3:]
    if len(recent_years) < 2:
        return None
    
    first_year = recent_years[0]
    last_year = recent_years[-1]
    first_amount = yearly[first_year]
    last_amount = yearly[last_year]
    
    if first_amount == 0:
        return None
    
    # CAGR
    cagr = (last_amount / first_amount) ** (1 / (len(recent_years) - 1)) - 1
    next_year = last_year + 1
    forecast_amount = last_amount * (1 + cagr)
    
    # Определяем медианный месяц выплат
    months = []
    for date_str, _ in rows:
        month = int(date_str[5:7])
        months.append(month)
    median_month = int(statistics.median(months))
    
    confidence = 1.0 if len(recent_years) >= 3 else 0.8
    
    return {
        "amount": max(0, forecast_amount),
        "month": median_month,
        "year": next_year,
        "confidence": confidence
    }