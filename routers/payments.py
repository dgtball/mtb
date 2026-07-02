import logging
import datetime
import sqlite3
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from routers.auth import require_token
from config import DB_PATH

router = APIRouter()

@router.get("/api/upcoming-payments")
async def get_upcoming_payments(request: Request, year: int = None, month: int = None):
    require_token(request)
    try:
        if year is None:
            year = datetime.datetime.now().year
        if month is None:
            month = datetime.datetime.now().month

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute("""
            SELECT ticker, payment_date, dividend_net, figi
            FROM dividend_calendar
            WHERE strftime('%Y', payment_date) = ? AND strftime('%m', payment_date) = ?
        """, (str(year), f"{month:02d}"))
        dividends = [{"ticker": row[0], "date": row[1], "amount": row[2], "type": "dividend"} for row in c.fetchall()]

        c.execute("""
            SELECT ticker, coupon_date, coupon_value, figi
            FROM coupon_calendar
            WHERE strftime('%Y', coupon_date) = ? AND strftime('%m', coupon_date) = ?
        """, (str(year), f"{month:02d}"))
        coupons = [{"ticker": row[0], "date": row[1], "amount": row[2], "type": "coupon"} for row in c.fetchall()]

        conn.close()

        return JSONResponse({
            "year": year, "month": month,
            "dividends": dividends, "coupons": coupons
        })
    except Exception as e:
        logging.error(f"Error in /api/upcoming-payments: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
