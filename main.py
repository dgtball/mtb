import os
import sys
import logging
import datetime
import asyncio
import hashlib
import hmac
import aiohttp
from urllib.parse import parse_qs
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION, TINKOFF_TOKEN, SECTOR_NAMES
import db
from moex_api import load_instrument_names, ticker_to_sector
from handlers import register_handlers, set_http_session, set_bot
import scheduler

# ---------- ЛОГИРОВАНИЕ ----------
from logging.handlers import TimedRotatingFileHandler

log_dir = os.path.join(os.getenv('DATA_DIR', '/app/data'), 'logs')
os.makedirs(log_dir, exist_ok=True)

file_handler = TimedRotatingFileHandler(
    os.path.join(log_dir, 'bot.log'),
    when='midnight',
    interval=1,
    backupCount=7,
    encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)

logging.getLogger('aiogram.event').setLevel(logging.WARNING)
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
logging.getLogger('aiohttp.client').setLevel(logging.WARNING)

# ---------- ИНИЦИАЛИЗАЦИЯ ----------
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- ПРОВЕРКА ПОДПИСИ TELEGRAM ----------
def verify_telegram_data(init_data: str) -> bool:
    try:
        params = parse_qs(init_data)
        logging.info(f"Init data keys: {list(params.keys())}")
        if "hash" not in params:
            logging.error("No 'hash' in init data")
            return False
        received_hash = params.pop("hash")[0]
        sorted_keys = sorted(params.keys())
        data_check_arr = [f"{k}={params[k][0]}" for k in sorted_keys]
        data_check_string = "\n".join(data_check_arr)
        secret_key = hmac.new("WebAppData".encode(), API_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        logging.info(f"Received hash: {received_hash}, calculated: {calculated_hash}")
        return calculated_hash == received_hash
    except Exception as e:
        logging.error(f"Verification error: {e}")
        return False

def get_user_id_from_init_data(init_data: str) -> int:
    params = parse_qs(init_data)
    user = params.get("user")
    if user:
        import json
        user_data = json.loads(user[0])
        return user_data.get("id")
    return 0

# ---------- FASTAPI РОУТЫ ----------
@app.get("/")
async def root():
    return {"status": "ok", "version": VERSION}
    
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/mini-app")
async def mini_app(request: Request):
    logging.info(f"Full URL: {str(request.url)}")
    init_data = request.query_params.get("tgWebAppData", "")
    logging.info(f"Init data: {init_data[:200]}")
    # Временно разрешаем без проверки? Замени на if False: для быстрого теста
    if not verify_telegram_data(init_data):
        raise HTTPException(status_code=403, detail="Invalid init data")
    user_id = get_user_id_from_init_data(init_data)
    if user_id != MY_CHAT_ID:
        raise HTTPException(status_code=403, detail="Access denied")
    with open("mini_app.html", "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(content=html)

@app.get("/api/portfolio")
async def api_portfolio(request: Request):
    init_data = request.query_params.get("tgWebAppData", "")
    if not verify_telegram_data(init_data):
        raise HTTPException(status_code=403, detail="Invalid init data")
    user_id = get_user_id_from_init_data(init_data)
    if user_id != MY_CHAT_ID:
        raise HTTPException(status_code=403, detail="Access denied")
    try:
        from tinkoff_api import get_portfolio_summary
        data = await get_portfolio_summary(bot_session)
        if not data:
            return JSONResponse({"error": "Нет данных"}, status_code=404)
        positions = []
        for pos in data["positions"]:
            value = pos["quantity"] * pos["price"]
            positions.append({
                "ticker": pos["ticker"],
                "name": pos["name"],
                "value": value,
                "yield_pct": pos["pos_yield_pct"],
                "sector": pos["sector_name"],
            })
        sectors = {}
        for p in positions:
            sec = p["sector"]
            sectors[sec] = sectors.get(sec, 0) + p["value"]
        sector_list = [{"name": k, "value": v} for k, v in sectors.items()]
        daily_change_pct = None
        today = datetime.date.today().isoformat()
        snapshot = db.get_daily_snapshot(today)
        if snapshot is not None and snapshot > 0:
            daily_change_pct = (data["total_amount"] - snapshot) / snapshot * 100
        return JSONResponse({
            "total_amount": data["total_amount"],
            "daily_change_pct": daily_change_pct,
            "positions": positions,
            "sectors": sector_list,
        })
    except Exception as e:
        logging.error(f"API portfolio: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/overrides")
async def api_overrides(request: Request):
    init_data = request.query_params.get("tgWebAppData", "")
    if not verify_telegram_data(init_data):
        raise HTTPException(status_code=403, detail="Invalid init data")
    user_id = get_user_id_from_init_data(init_data)
    if user_id != MY_CHAT_ID:
        raise HTTPException(status_code=403, detail="Access denied")
    try:
        from config import NAME_OVERRIDES
        overrides = [{"ticker": k, "name": v} for k, v in NAME_OVERRIDES.items()]
        return JSONResponse(overrides)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/override")
async def api_override(request: Request):
    init_data = request.query_params.get("tgWebAppData", "")
    if not verify_telegram_data(init_data):
        raise HTTPException(status_code=403, detail="Invalid init data")
    user_id = get_user_id_from_init_data(init_data)
    if user_id != MY_CHAT_ID:
        raise HTTPException(status_code=403, detail="Access denied")
    try:
        body = await request.json()
        action = body.get("action")
        ticker = body.get("ticker")
        if action == "add":
            display_name = body.get("display_name")
            db.set_name_override(ticker, display_name)
        elif action == "remove":
            db.remove_name_override(ticker)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ---------- ФОНОВЫЙ ОБНОВИТЕЛЬ ПОРТФЕЛЯ ----------
async def portfolio_updater(http_session):
    import scheduler as sched
    await asyncio.sleep(10)
    while True:
        try:
            if sched.is_portfolio_update_allowed():
                from tinkoff_api import get_portfolio_summary
                data = await get_portfolio_summary(http_session)
                if data:
                    total = data['total_amount']
                    db.set_portfolio_value(total)
                    logging.debug(f"Портфель автообновлён: {total:.2f}")
                await asyncio.sleep(300)
            else:
                await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"Ошибка автообновления портфеля: {e}")
            await asyncio.sleep(60)

# ---------- ГЛАВНАЯ ФУНКЦИЯ ----------
async def main():
    global bot_session
    db.init_db()
    db.load_name_overrides()

    bot_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
    set_http_session(bot_session)
    set_bot(bot)
    scheduler.set_bot(bot)
    scheduler.set_http_session(bot_session)

    await load_instrument_names(bot_session)

    register_handlers(dp)

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("✅ Вебхук удалён")

    try:
        await bot.send_message(MY_CHAT_ID, f"🚀 Бот перезапущен и готов к работе! ver: {VERSION}")
    except Exception as e:
        logging.error(f"❌ Не удалось отправить уведомление о запуске: {e}")

    asyncio.create_task(scheduler.scheduler_loop())
    if TINKOFF_TOKEN:
        asyncio.create_task(portfolio_updater(bot_session))

    # Запускаем FastAPI и поллинг параллельно
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    loop = asyncio.get_event_loop()
    loop.create_task(server.serve())
    logging.info(f"✅ FastAPI сервер запущен на порту {PORT}")

    logging.info("✅ Запускаем polling...")
    await dp.start_polling(bot)

    await bot_session.close()
    logging.info("✅ HTTP сессия закрыта")

if __name__ == "__main__":
    asyncio.run(main())