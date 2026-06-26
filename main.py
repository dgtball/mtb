import os
import sys
import logging
import time
import datetime
import asyncio
import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION, TINKOFF_TOKEN, SECTOR_NAMES, MINI_APP_SECRET
import db
from moex_api import load_instrument_names, ticker_to_sector
from handlers import register_handlers, set_http_session, set_bot
import scheduler

os.environ['TZ'] = 'Europe/Moscow'
time.tzset()  # перезагружаем настройки времени с учётом TZ

# ---------- ЛОГИРОВАНИЕ С UTC+3 ----------
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

# ---------- ПРОВЕРКА ТОКЕНА ----------
def check_token(request: Request) -> bool:
    token = request.query_params.get("token", "")
    return token == MINI_APP_SECRET

# ---------- FASTAPI РОУТЫ ----------
@app.get("/")
async def root():
    return {"status": "ok", "version": VERSION}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/mini-app")
async def mini_app(request: Request):
    with open("mini_app.html", "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("MINI_APP_TOKEN_PLACEHOLDER", MINI_APP_SECRET)
    return HTMLResponse(content=html)

@app.get("/api/portfolio")
async def api_portfolio(request: Request):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        from tinkoff_api import get_portfolio_summary
        from moex_api import get_market_data
        from utils import smart_price

        data = await get_portfolio_summary(bot_session)
        if not data:
            return JSONResponse({"error": "Нет данных"}, status_code=404)

        logging.info("API portfolio: позиции из портфеля:")
        for pos in data["positions"]:
            logging.info(f"  {pos['ticker']} -> сектор из БД: {db.get_sector(pos['ticker'])}")

        market_df = await get_market_data(bot_session)
        ticker_change = {}
        if not market_df.empty and 'SECID' in market_df.columns and 'CHANGEPERCENT' in market_df.columns:
            for _, row in market_df.iterrows():
                ticker_change[row['SECID']] = row['CHANGEPERCENT']
        logging.info(f"ticker_change keys count: {len(ticker_change)}")
        # проверим первые 10 тикеров
        sample = list(ticker_change.keys())[:10]
        logging.info(f"ticker_change sample: {sample}")
        # тикеры портфеля для сравнения
        portfolio_tickers = [p["ticker"] for p in data["positions"]]
        logging.info(f"portfolio tickers: {portfolio_tickers}")

        total_amount = data["total_amount"]
        positions = []
        portfolio_equities = []

        for pos in data["positions"]:
            ticker = pos["ticker"]
            sector_name = db.get_sector(ticker)
            value = pos["quantity"] * pos["price"]
            share = (value / total_amount * 100) if total_amount > 0 else 0

            # Средняя цена через smart_price
            avg_formatted = smart_price(pos["avg_price"])
            if ticker == "ASTR":
                logging.info(f"ASTR avg_price raw: {pos['avg_price']}, type: {type(pos['avg_price'])}")

            positions.append({
                "ticker": ticker,
                "name": pos["name"],
                "price_formatted": smart_price(pos["price"]),
                "avg_price_formatted": avg_formatted,
                "value": value,
                "yield_pct": pos["pos_yield_pct"],
                "sector": sector_name,
                "share": round(share, 1),
            })

            # Собираем акции для лидеров: не Прочие, не Фонд, не Облигации
            if sector_name and sector_name not in ("Прочие", "Фонд", "Облигации"):
                portfolio_equities.append({
                    "name": pos["name"],
                    "price_formatted": smart_price(pos["price"]),
                    "change_pct": pos["pos_yield_pct"],   # общая доходность
                })

        logging.info(f"Собрано {len(portfolio_equities)} акций для лидеров")

        sectors = {}
        for p in positions:
            sec = p["sector"]
            sectors[sec] = sectors.get(sec, 0) + p["value"]
        sector_list = [{"name": k, "value": v} for k, v in sectors.items()]

        portfolio_equities.sort(key=lambda x: x["change_pct"], reverse=True)
        portfolio_gainers = portfolio_equities[:5]
        portfolio_losers = portfolio_equities[-5:][::-1] if len(portfolio_equities) >= 5 else portfolio_equities[::-1]

        daily_change_pct = None
        today = datetime.date.today().isoformat()
        snapshot = db.get_daily_snapshot(today)
        if snapshot is not None and snapshot > 0:
            daily_change_pct = (total_amount - snapshot) / snapshot * 100

        return JSONResponse({
            "total_amount": total_amount,
            "daily_change_pct": daily_change_pct,
            "positions": positions,
            "sectors": sector_list,
            "portfolio_gainers": portfolio_gainers,
            "portfolio_losers": portfolio_losers,
        })
    except Exception as e:
        logging.error(f"API portfolio: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/overrides")
async def api_overrides(request: Request):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        from config import NAME_OVERRIDES
        overrides = [{"ticker": k, "name": v} for k, v in NAME_OVERRIDES.items()]
        return JSONResponse(overrides)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/override")
async def api_override(request: Request):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
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

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    loop = asyncio.get_event_loop()
    loop.create_task(server.serve())
    logging.info(f"✅ FastAPI сервер запущен на порту {PORT}")

    await asyncio.sleep(2)
    logging.info("✅ Запускаем polling...")
    await dp.start_polling(bot)

    # Закрываем сессию после остановки поллинга
    await bot_session.close()
    logging.info("✅ HTTP сессия закрыта")

if __name__ == "__main__":
    asyncio.run(main())