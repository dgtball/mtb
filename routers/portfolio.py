import logging
import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from services.portfolio import get_portfolio_with_details
from routers.auth import require_token
import db
from config import NAME_OVERRIDES
import state

router = APIRouter()

@router.get("/api/portfolio")
async def api_portfolio(request: Request):
    require_token(request)
    try:
        data = await get_portfolio_with_details(state.bot_session)
        if not data:
            return JSONResponse({"error": "Нет данных"}, status_code=404)
        return JSONResponse(data)
    except Exception as e:
        logging.error(f"API portfolio: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/overrides")
async def api_overrides(request: Request):
    require_token(request)
    try:
        overrides = [{"ticker": k, "name": v} for k, v in NAME_OVERRIDES.items()]
        return JSONResponse(overrides)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/override")
async def api_override(request: Request):
    require_token(request)
    try:
        body = await request.json()
        action = body.get("action")
        ticker = body.get("ticker")
        if action == "add":
            display_name = body.get("display_name")
            await db.set_name_override(ticker, display_name)
        elif action == "remove":
            await db.remove_name_override(ticker)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/portfolio-performance")
async def api_portfolio_performance(request: Request, range: str = "30d"):
    require_token(request)
    try:
        today = datetime.date.today()
        range_map = {
            "7d": today - datetime.timedelta(days=7),
            "30d": today - datetime.timedelta(days=30),
            "90d": today - datetime.timedelta(days=90),
            "180d": today - datetime.timedelta(days=180),
            "1y": today - datetime.timedelta(days=365),
            "all": today - datetime.timedelta(days=3650),
        }
        from_date = range_map.get(range, today - datetime.timedelta(days=30))

        snapshots = await db.get_daily_snapshots(from_date.isoformat(), today.isoformat())

        from moex_api import get_imoex_history
        imoex_data = await get_imoex_history(state.bot_session, from_date.isoformat(), today.isoformat())
        imoex_by_date = {r["date"][:10]: r["close"] for r in imoex_data if r.get("close")}

        dates_set = set()
        for s in snapshots:
            dates_set.add(s["date"])
        for d in imoex_by_date:
            dates_set.add(d)
        all_dates = sorted(dates_set)

        if not all_dates:
            return JSONResponse({"dates": [], "portfolio": [], "imoex": []})

        portfolio_map = {s["date"]: s["portfolio_value"] for s in snapshots}
        first_pv = None
        for d in all_dates:
            if d in portfolio_map and portfolio_map[d] is not None:
                first_pv = portfolio_map[d]
                break

        portfolio_series = []
        imoex_series = []
        for d in all_dates:
            pv = portfolio_map.get(d)
            portfolio_series.append(pv)

            iv = imoex_by_date.get(d)
            if iv is not None and first_pv is not None:
                base_iv = imoex_by_date.get(all_dates[0])
                if base_iv and base_iv > 0:
                    imoex_series.append(round(iv / base_iv * first_pv, 2))
                else:
                    imoex_series.append(None)
            else:
                imoex_series.append(None)

        return JSONResponse({
            "dates": all_dates,
            "portfolio": portfolio_series,
            "imoex": imoex_series,
        })
    except Exception as e:
        logging.error(f"Error in /api/portfolio-performance: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
