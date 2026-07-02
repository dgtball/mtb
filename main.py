import os
import logging
import time
import asyncio
import aiohttp
import uvicorn
import db
import scheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage

from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION, TINKOFF_TOKEN, MINI_APP_SECRET, WEBHOOK_URL
from moex_api import load_instrument_names
from handlers import register_handlers, set_http_session, set_bot
from background import portfolio_updater
from routers.portfolio import router as portfolio_router
from routers.dividends import router as dividends_router
from routers.sync import router as sync_router
from routers.settings import router as settings_router
from routers.payments import router as payments_router
from routers.debug import router as debug_router
import state
from logging.handlers import TimedRotatingFileHandler

os.environ['TZ'] = 'Europe/Moscow'
time.tzset()

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

if not os.path.exists(os.getenv('DATA_DIR', '/app/data')):
    os.makedirs(os.getenv('DATA_DIR', '/app/data'), exist_ok=True)
    logging.info(f"Создана директория данных: {os.getenv('DATA_DIR', '/app/data')}")

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

app.include_router(portfolio_router)
app.include_router(dividends_router)
app.include_router(sync_router)
app.include_router(settings_router)
app.include_router(payments_router)
app.include_router(debug_router)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        telegram_update = types.Update(**update)
        await dp.feed_update(bot, telegram_update)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

@app.get("/")
async def root():
    return {"status": "ok", "version": VERSION}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/mini-app")
async def mini_app(request: Request):
    try:
        with open("mini_app.html", "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("MINI_APP_TOKEN_PLACEHOLDER", MINI_APP_SECRET)
        return HTMLResponse(content=html)
    except FileNotFoundError:
        logging.error("mini_app.html не найден")
        return HTMLResponse("<h1>Ошибка: mini_app.html не найден</h1>", status_code=404)

async def main():
    await db.init_db()
    await db.load_name_overrides()

    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
    state.bot_session = session
    set_http_session(session)
    set_bot(bot)
    scheduler.set_bot(bot)
    scheduler.set_http_session(session)

    await load_instrument_names(state.bot_session)
    register_handlers(dp)

    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Вебхук установлен: {WEBHOOK_URL}")

    try:
        await bot.send_message(MY_CHAT_ID, f"Бот перезапущен и готов к работе! ver: {VERSION}")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление о запуске: {e}")

    if TINKOFF_TOKEN:
        from tinkoff_api import build_figi_map
        await build_figi_map(state.bot_session)

    if TINKOFF_TOKEN:
        try:
            from tinkoff_api import sync_operations
            asyncio.create_task(sync_operations(state.bot_session))
        except Exception as e:
            logging.error(f"Ошибка запуска первой синхронизации: {e}")

    asyncio.create_task(scheduler.scheduler_loop())
    if TINKOFF_TOKEN:
        asyncio.create_task(portfolio_updater(state.bot_session))

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    logging.info(f"FastAPI сервер запущен на порту {PORT}")
    try:
        await server.serve()
    finally:
        await state.bot_session.close()
        logging.info("HTTP сессия закрыта")

if __name__ == "__main__":
    asyncio.run(main())
