import logging
import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from routers.auth import require_token
from config import DB_PATH, NAME_OVERRIDES, ticker_to_name
import db
import state

router = APIRouter()

@router.get("/api/my-dividends")
async def api_my_dividends(request: Request):
    require_token(request)
    try:
        dividends = await db.get_personal_dividends()
        return JSONResponse(dividends)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/dividends-yearly")
async def api_dividends_yearly(request: Request, year: int = None, ticker: str = None):
    require_token(request)
    try:
        conn = await db.get_db()

        actual_ticker = None
        if ticker:
            c = await conn.execute("SELECT ticker FROM name_overrides WHERE display_name = ?", (ticker,))
            row = await c.fetchone()
            if row:
                actual_ticker = row[0]
            else:
                for t, name in ticker_to_name.items():
                    if name == ticker:
                        actual_ticker = t
                        break
                if not actual_ticker:
                    actual_ticker = ticker

        if year and actual_ticker:
            c = await conn.execute("""SELECT date, ticker, payment FROM operations 
                         WHERE type IN ('Выплата дивидендов', 'Выплата купонов') 
                         AND currency = 'RUB' AND date LIKE ? 
                         AND ticker = ? 
                         ORDER BY date DESC""", (f"{year}%", actual_ticker))
            rows = await c.fetchall()
            details = []
            for r in rows:
                tick = r[1] if r[1] else "Прочие"
                if tick != "Прочие":
                    name = NAME_OVERRIDES.get(tick) or ticker_to_name.get(tick, tick)
                else:
                    name = "Прочие"
                details.append({"date": r[0], "name": name, "amount": r[2]})
            return JSONResponse({"year": year, "ticker": ticker, "details": details})

        elif actual_ticker:
            c = await conn.execute("""SELECT date, ticker, payment FROM operations 
                         WHERE type IN ('Выплата дивидендов', 'Выплата купонов') 
                         AND currency = 'RUB' 
                         AND ticker = ? 
                         ORDER BY date DESC""", (actual_ticker,))
            rows = await c.fetchall()
            details = []
            yearly_totals = {}
            for r in rows:
                tick = r[1] if r[1] else "Прочие"
                y = r[0][:4]
                if tick != "Прочие":
                    name = NAME_OVERRIDES.get(tick) or ticker_to_name.get(tick, tick)
                else:
                    name = "Прочие"
                details.append({"date": r[0], "name": name, "amount": r[2]})
                yearly_totals[y] = yearly_totals.get(y, 0) + r[2]
            return JSONResponse({"ticker": ticker, "details": details, "yearly_totals": yearly_totals})

        else:
            c = await conn.execute("SELECT date, ticker, payment FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date")
            rows = await c.fetchall()
            yearly = {}
            for r in rows:
                y = r[0][:4]
                tick = r[1] if r[1] else "Прочие"
                if tick != "Прочие":
                    name = NAME_OVERRIDES.get(tick) or ticker_to_name.get(tick, tick)
                else:
                    name = "Прочие"
                if y not in yearly:
                    yearly[y] = {}
                if name not in yearly[y]:
                    yearly[y][name] = 0.0
                yearly[y][name] += r[2]
            years = sorted(yearly.keys())
            datasets = []
            for name in sorted(set(n for y in yearly.values() for n in y.keys())):
                datasets.append({
                    "label": name,
                    "data": [yearly[y].get(name, 0) for y in years]
                })
            return JSONResponse({"years": years, "datasets": datasets})
    except Exception as e:
        logging.error(f"Error in /api/dividends-yearly: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/dividends-monthly")
async def api_dividends_monthly(request: Request, year: int = None):
    logging.info(f"Запрос /api/dividends-monthly для года {year}")
    require_token(request)
    try:
        if year is None:
            year = datetime.datetime.now().year

        from tinkoff_api import get_portfolio_summary
        portfolio = await get_portfolio_summary(state.bot_session)
        if not portfolio:
            portfolio_positions = []
        else:
            portfolio_positions = portfolio.get("positions", [])

        portfolio_quantities = {}
        for pos in portfolio_positions:
            ticker = pos["ticker"]
            quantity = pos["quantity"]
            if ticker and quantity > 0:
                portfolio_quantities[ticker] = quantity

        conn = await db.get_db()

        c = await conn.execute("""SELECT date, ticker, payment, name 
                     FROM operations 
                     WHERE type IN ('Выплата дивидендов', 'Выплата купонов') 
                       AND currency = 'RUB' 
                       AND date LIKE ? 
                     ORDER BY date""", (f"{year}%",))
        rows = await c.fetchall()

        actual_by_month = {m: {"total": 0.0, "details": []} for m in range(1, 13)}
        for r in rows:
            date_str = r[0]
            month = int(date_str[5:7])
            amount = r[2]
            ticker = r[1]
            if ticker is None or ticker == "Прочие":
                display_name = "Прочие"
            else:
                display_name = NAME_OVERRIDES.get(ticker) or ticker_to_name.get(ticker, ticker)
            actual_by_month[month]["total"] += amount
            actual_by_month[month]["details"].append({
                "date": date_str, "ticker": ticker, "name": display_name,
                "amount": amount, "type": "actual"
            })

        c = await conn.execute("""
            SELECT ticker, payment_date, record_date, dividend_net
            FROM dividend_calendar
            WHERE strftime('%Y', payment_date) = ? AND payment_date > date('now')
        """, (str(year),))
        declared_dividends = await c.fetchall()

        declared_before_record = {m: {"total": 0.0, "details": []} for m in range(1, 13)}
        declared_after_record = {m: {"total": 0.0, "details": []} for m in range(1, 13)}

        for row in declared_dividends:
            ticker = row[0]
            payment_date = row[1]
            record_date = row[2]
            dividend_per_share = row[3]
            if not payment_date or not dividend_per_share:
                continue
            quantity = portfolio_quantities.get(ticker, 0)
            if quantity == 0:
                continue
            amount = dividend_per_share * quantity
            month = int(record_date[5:7]) if record_date else int(payment_date[5:7])
            name = NAME_OVERRIDES.get(ticker) or ticker_to_name.get(ticker, ticker)
            if record_date and record_date >= datetime.date.today().isoformat():
                declared_before_record[month]["total"] += amount
                declared_before_record[month]["details"].append({
                    "date": record_date, "ticker": ticker, "name": name,
                    "amount": amount, "type": "declared_dividend_before"
                })
            else:
                declared_after_record[month]["total"] += amount
                declared_after_record[month]["details"].append({
                    "date": record_date, "ticker": ticker, "name": name,
                    "amount": amount, "type": "declared_dividend_after"
                })

        c = await conn.execute("""
            SELECT ticker, coupon_date, coupon_value, record_date, is_redemption
            FROM coupon_calendar
            WHERE strftime('%Y', coupon_date) = ? AND coupon_date > date('now')
        """, (str(year),))
        declared_coupons = await c.fetchall()

        redemption_by_month = {m: {"total": 0.0, "details": []} for m in range(1, 13)}

        for row in declared_coupons:
            ticker = row[0]
            coupon_date = row[1]
            coupon_per_bond = row[2]
            record_date = row[3]
            is_redemption = row[4]
            if not coupon_date or not coupon_per_bond:
                continue
            quantity = portfolio_quantities.get(ticker, 0)
            if quantity == 0:
                continue
            amount = coupon_per_bond * quantity
            month = int(coupon_date[5:7])
            name = NAME_OVERRIDES.get(ticker) or ticker_to_name.get(ticker, ticker)

            if is_redemption:
                redemption_by_month[month]["total"] += amount
                redemption_by_month[month]["details"].append({
                    "date": coupon_date, "ticker": ticker, "name": name,
                    "amount": amount, "type": "redemption"
                })
            else:
                if (record_date and record_date >= datetime.date.today().isoformat()) or (not record_date and coupon_date > datetime.date.today().isoformat()):
                    declared_before_record[month]["total"] += amount
                    declared_before_record[month]["details"].append({
                        "date": coupon_date, "ticker": ticker, "name": name,
                        "amount": amount, "type": "declared_coupon_before"
                    })
                else:
                    declared_after_record[month]["total"] += amount
                    declared_after_record[month]["details"].append({
                        "date": coupon_date, "ticker": ticker, "name": name,
                        "amount": amount, "type": "declared_coupon_after"
                    })

        forecast_rows = await db.get_dividend_forecast(year=year)
        forecast_rows.sort(key=lambda r: (r["year"], r["month"], 0 if r.get("method") == "manual" else 1))

        seen_keys = set()
        forecast_by_month = {m: {"total": 0.0, "details": []} for m in range(1, 13)}

        for row in forecast_rows:
            if row["amount"] <= 0:
                continue
            ticker = row["ticker"]
            key = (ticker, row["year"], row["month"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            forecast_per_share = row["amount"]
            month = row["month"]
            if forecast_per_share > 0:
                quantity = portfolio_quantities.get(ticker, 0)
                if quantity == 0:
                    continue
                amount = forecast_per_share * quantity
                name = NAME_OVERRIDES.get(ticker) or ticker_to_name.get(ticker, ticker)
                forecast_by_month[month]["total"] += amount
                forecast_by_month[month]["details"].append({
                    "date": f"{year}-{month:02d}-01", "ticker": ticker, "name": name,
                    "amount": amount, "type": "forecast", "method": row.get("method"),
                    "id": row.get("id")
                })

        c = await conn.execute("SELECT DISTINCT substr(date, 1, 4) FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date DESC")
        years_from_ops = [int(row[0]) for row in await c.fetchall() if row[0] is not None]
        all_forecasts = await db.get_dividend_forecast()
        forecast_years = sorted(set(f["year"] for f in all_forecasts if f["amount"] > 0))
        all_years = sorted(set(years_from_ops + forecast_years), reverse=True)

        months_labels = ['Янв','Фев','Мар','Апр','Май','Июн','Июл','Авг','Сен','Окт','Ноя','Дек']
        actual_data = [actual_by_month[m]["total"] for m in range(1, 13)]
        before_data = [declared_before_record[m]["total"] for m in range(1, 13)]
        after_data = [declared_after_record[m]["total"] for m in range(1, 13)]
        forecast_data = [forecast_by_month[m]["total"] for m in range(1, 13)]
        redemption_data = [redemption_by_month[m]["total"] for m in range(1, 13)]

        return JSONResponse({
            "year": year, "months": months_labels,
            "actual": actual_data, "declared_before_record": before_data,
            "declared_after_record": after_data, "redemption": redemption_data,
            "forecast": forecast_data,
            "total_actual": sum(actual_data), "total_before": sum(before_data),
            "total_after": sum(after_data), "total_redemption": sum(redemption_data),
            "total_forecast": sum(forecast_data),
            "details_actual": {m: actual_by_month[m]["details"] for m in range(1, 13)},
            "details_declared_before": {m: declared_before_record[m]["details"] for m in range(1, 13)},
            "details_declared_after": {m: declared_after_record[m]["details"] for m in range(1, 13)},
            "details_redemption": {m: redemption_by_month[m]["details"] for m in range(1, 13)},
            "details_forecast": {m: forecast_by_month[m]["details"] for m in range(1, 13)},
            "available_years": all_years
        })
    except Exception as e:
        logging.error(f"Error in /api/dividends-monthly: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
