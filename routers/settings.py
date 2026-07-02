import logging
import sqlite3
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from routers.auth import require_token
from config import DB_PATH, SECTOR_NAMES
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
    db.update_instrument_sector(ticker, sector)
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
        instruments = db.get_all_instruments()
        return JSONResponse(instruments)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/operations/unticked")
async def get_unticked_operations(request: Request):
    require_token(request)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT id, date, payment, ticker, name 
                     FROM operations 
                     WHERE (ticker IS NULL OR ticker = 'Прочие') 
                       AND type IN ('Выплата дивидендов', 'Выплата купонов')
                     ORDER BY date DESC""")
        rows = c.fetchall()
        operations = []
        for r in rows:
            operations.append({
                "id": r[0], "date": r[1], "payment": r[2],
                "ticker": r[3], "name": r[4] or "Неизвестно"
            })

        tickers_set = set()
        c.execute("SELECT ticker FROM instruments")
        for row in c.fetchall():
            if row[0]:
                tickers_set.add(row[0])
        c.execute("SELECT ticker FROM name_overrides")
        for row in c.fetchall():
            if row[0]:
                tickers_set.add(row[0])
        c.execute("SELECT DISTINCT ticker FROM operations WHERE ticker IS NOT NULL AND ticker != 'Прочие'")
        for row in c.fetchall():
            if row[0]:
                tickers_set.add(row[0])

        tickers_list = sorted(tickers_set)
        conn.close()

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
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE operations SET ticker = ? WHERE id = ?", (new_ticker, op_id))
        conn.commit()
        conn.close()
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"Error in /api/operations/link: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
