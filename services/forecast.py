import logging
import datetime
import statistics
import db
from config import NAME_OVERRIDES, ticker_to_name

async def calculate_and_update_forecasts(http_session):
    """Рассчитывает прогнозы дивидендов для всех тикеров в портфеле и сохраняет в БД."""
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
                # Если прогноз не удалось рассчитать, удаляем старую запись (если была)
                db.upsert_dividend_forecast(ticker, 0, 0, 0, 0, "none")  # можно не сохранять
        except Exception as e:
            logging.error(f"Ошибка расчёта прогноза для {ticker}: {e}")

def calculate_forecast_for_ticker(ticker: str):
    """
    Рассчитывает прогноз дивидендов на основе исторических выплат.
    Возвращает словарь с полями: amount, month, year, confidence.
    """
    # Получаем все выплаты дивидендов по тикеру
    conn = db.get_db_connection()  # используем существующее соединение
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
        return None  # недостаточно данных для прогноза
    
    # Группируем по году
    yearly = {}
    for date_str, payment in rows:
        year = int(date_str[:4])
        if year not in yearly:
            yearly[year] = 0.0
        yearly[year] += payment
    
    # Сортируем годы
    years = sorted(yearly.keys())
    if len(years) < 2:
        return None
    
    # Берём последние 3 года (или все, если меньше)
    recent_years = years[-3:]
    if len(recent_years) < 2:
        return None
    
    # Вычисляем CAGR
    first_year = recent_years[0]
    last_year = recent_years[-1]
    first_amount = yearly[first_year]
    last_amount = yearly[last_year]
    
    if first_amount == 0:
        return None
    
    cagr = (last_amount / first_amount) ** (1 / (len(recent_years) - 1)) - 1
    
    # Прогноз на следующий год
    next_year = last_year + 1
    forecast_amount = last_amount * (1 + cagr)
    
    # Определяем медианный месяц выплат
    months = []
    for date_str, _ in rows:
        month = int(date_str[5:7])
        months.append(month)
    median_month = int(statistics.median(months))
    
    # Confidence score
    if len(recent_years) >= 3:
        confidence = 1.0
    elif len(recent_years) == 2:
        confidence = 0.8
    else:
        confidence = 0.5
    
    return {
        "amount": max(0, forecast_amount),  # не может быть отрицательным
        "month": median_month,
        "year": next_year,
        "confidence": confidence
    }