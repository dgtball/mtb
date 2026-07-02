import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from routers.auth import require_token
from config import SECTOR_NAMES, ticker_to_name
import db

router = APIRouter()

@router.post("/api/sector")
async def set_sector(request: Request):
    require_token(request)
    body = await request.json()
    ticker = body.get("ticker")
    sector = body.get("sector")
    if not ticker or not sector:
        raise HTTPException(400, "Missing ticker or sector")
    await db.update_instrument_sector(ticker, sector)
    from moex_api import ticker_to_sector
    ticker_to_sector[ticker] = sector
    return JSONResponse({"status": "ok"})

@router.get("/api/sectors/list")
async def get_sectors_list(request: Request):
    require_token(request)
    sectors = sorted(set(SECTOR_NAMES.values()))
    return JSONResponse(sectors)

@router.get("/api/instruments")
async def get_instruments(request: Request):
    require_token(request)
    try:
        instruments = await db.get_all_instruments()
        return JSONResponse(instruments)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/operations/unticked")
async def get_unticked_operations(request: Request):
    require_token(request)
    try:
        conn = await db.get_db()
        c = await conn.execute("""SELECT id, date, payment, ticker, name 
                     FROM operations 
                     WHERE (ticker IS NULL OR ticker = 'Прочие') 
                       AND type IN ('Выплата дивидендов', 'Выплата купонов')
                     ORDER BY date DESC""")
        rows = await c.fetchall()
        operations = []
        for r in rows:
            operations.append({
                "id": r[0], "date": r[1], "payment": r[2],
                "ticker": r[3], "name": r[4] or "Неизвестно"
            })

        tickers_set = set()
        c = await conn.execute("SELECT ticker FROM instruments")
        for row in await c.fetchall():
            if row[0]:
                tickers_set.add(row[0])
        c = await conn.execute("SELECT ticker FROM name_overrides")
        for row in await c.fetchall():
            if row[0]:
                tickers_set.add(row[0])
        c = await conn.execute("SELECT DISTINCT ticker FROM operations WHERE ticker IS NOT NULL AND ticker != 'Прочие'")
        for row in await c.fetchall():
            if row[0]:
                tickers_set.add(row[0])

        tickers_list = sorted(tickers_set)

        return JSONResponse({
            "operations": operations,
            "available_tickers": tickers_list
        })
    except Exception as e:
        logging.error(f"Error in /api/operations/unticked: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/operations/link")
async def link_ticker_to_operation(request: Request):
    require_token(request)
    try:
        body = await request.json()
        op_id = body.get("id")
        new_ticker = body.get("ticker")
        if not op_id or not new_ticker:
            raise HTTPException(400, "Missing id or ticker")
        conn = await db.get_db()
        await conn.execute("UPDATE operations SET ticker = ? WHERE id = ?", (new_ticker, op_id))
        await conn.commit()
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"Error in /api/operations/link: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/manual-forecasts")
async def get_manual_forecasts(request: Request):
    require_token(request)
    try:
        forecasts = await db.get_manual_forecasts()
        result = []
        for f in forecasts:
            name = ticker_to_name.get(f["ticker"], f["ticker"])
            result.append({**f, "name": name})
        return JSONResponse(result)
    except Exception as e:
        logging.error(f"Error in /api/manual-forecasts GET: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/manual-forecasts")
async def create_manual_forecast(request: Request):
    require_token(request)
    try:
        body = await request.json()
        ticker = body.get("ticker", "").strip().upper()
        amount = body.get("amount")
        month = body.get("month")
        year = body.get("year")
        if not ticker or amount is None or not month or not year:
            raise HTTPException(400, "Missing ticker, amount, month or year")
        amount = float(amount)
        month = int(month)
        year = int(year)
        if month < 1 or month > 12:
            raise HTTPException(400, "Month must be 1-12")
        if year < 2020 or year > 2035:
            raise HTTPException(400, "Year out of range")
        await db.upsert_manual_forecast(ticker, amount, month, year)
        return JSONResponse({"status": "ok"})
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error in /api/manual-forecasts POST: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.delete("/api/manual-forecasts/{forecast_id}")
async def delete_manual_forecast(forecast_id: int, request: Request):
    require_token(request)
    try:
        existing = await db.get_manual_forecast_by_id(forecast_id)
        if not existing:
            raise HTTPException(404, "Forecast not found")
        await db.delete_manual_forecast(forecast_id)
        return JSONResponse({"status": "ok"})
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error in /api/manual-forecasts DELETE: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.put("/api/manual-forecasts/{forecast_id}")
async def update_manual_forecast(forecast_id: int, request: Request):
    require_token(request)
    try:
        body = await request.json()
        amount = body.get("amount")
        month = body.get("month")
        year = body.get("year")
        existing = await db.get_manual_forecast_by_id(forecast_id)
        if not existing:
            raise HTTPException(404, "Forecast not found")
        amount = float(amount) if amount is not None else existing["amount"]
        month = int(month) if month is not None else existing["month"]
        year = int(year) if year is not None else existing["year"]
        if month < 1 or month > 12:
            raise HTTPException(400, "Month must be 1-12")
        await db.upsert_manual_forecast(existing["ticker"], amount, month, year)
        return JSONResponse({"status": "ok"})
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error in /api/manual-forecasts PUT: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
