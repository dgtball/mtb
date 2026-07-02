import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from routers.auth import require_token
import state

router = APIRouter()

@router.post("/api/refresh-forecasts")
async def refresh_forecasts_manual(request: Request):
    require_token(request)
    try:
        from services.forecast import calculate_and_update_forecasts
        await calculate_and_update_forecasts(state.bot_session)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"Forecast refresh error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/debug-fetch-dividends")
async def debug_fetch_dividends(request: Request):
    require_token(request)
    try:
        from tinkoff_api import fetch_all_dividends, fetch_all_coupons
        logging.info("Debug: fetching dividends...")
        dividends_data = await fetch_all_dividends(state.bot_session)
        logging.info(f"Debug: dividends_data = {dividends_data}")
        coupons_data = await fetch_all_coupons(state.bot_session)
        logging.info(f"Debug: coupons_data = {coupons_data}")
        return JSONResponse({"dividends": dividends_data, "coupons": coupons_data})
    except Exception as e:
        logging.error(f"Debug error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
