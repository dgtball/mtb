import logging
import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from routers.auth import require_token
import db
import state

router = APIRouter()

@router.get("/api/sync-status")
async def api_sync_status(request: Request):
    require_token(request)
    last_sync = await db.get_last_sync_time()
    return JSONResponse({"last_sync": last_sync})

@router.post("/api/sync")
async def api_sync(request: Request):
    require_token(request)
    try:
        from tinkoff_api import sync_operations
        full = request.query_params.get("full") == "true"
        new_count = await sync_operations(state.bot_session, force_full=full)
        now_moscow = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)).isoformat()
        await db.set_last_sync_time(now_moscow)
        return JSONResponse({
            "status": "ok", "new_operations": new_count,
            "last_sync": now_moscow, "full": full
        })
    except Exception as e:
        logging.error(f"Sync error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/sync-full")
async def api_sync_full(request: Request):
    require_token(request)
    try:
        from tinkoff_api import sync_operations
        new_count = await sync_operations(state.bot_session, force_full=True)
        now_moscow = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)).isoformat()
        await db.set_last_sync_time(now_moscow)
        return JSONResponse({
            "status": "ok", "new_operations": new_count,
            "last_sync": now_moscow, "full": True
        })
    except Exception as e:
        logging.error(f"Sync-full error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/sync-calendars")
async def sync_calendars(request: Request):
    require_token(request)
    try:
        from tinkoff_api import fetch_all_dividends, fetch_all_coupons
        dividends_data = await fetch_all_dividends(state.bot_session)
        coupons_data = await fetch_all_coupons(state.bot_session)
        return JSONResponse({
            "status": "ok",
            "dividends_updated": len(dividends_data),
            "coupons_updated": len(coupons_data)
        })
    except Exception as e:
        logging.error(f"Calendar sync error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
