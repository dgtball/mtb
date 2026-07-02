import logging
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
