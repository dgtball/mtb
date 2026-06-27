import os
import sys
import logging
import time
import datetime
import asyncio
import aiohttp
import sqlite3
import db
import uvicorn
import scheduler
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION, TINKOFF_TOKEN, SECTOR_NAMES, MINI_APP_SECRET, NAME_OVERRIDES, ticker_to_name, DB_PATH, WEBHOOK_URL, SECTOR_NAMES
from moex_api import load_instrument_names, ticker_to_sector
from handlers import register_handlers, set_http_session, set_bot

# ---------- ЛОГИРОВАНИЕ ----------
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
    token = request.headers.get("X-Mini-App-Token", "")
    return token == MINI_APP_SECRET

# ---------- ВЕБХУК ТЕЛЕГРАМ ----------
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

# ---------- FASTAPI РОУТЫ (без зависимостей от aiogram) ----------
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

        market_df = await get_market_data(bot_session)
        ticker_change = {}
        if not market_df.empty and 'SECID' in market_df.columns and 'LAST' in market_df.columns and 'OPEN' in market_df.columns:
            for _, row in market_df.iterrows():
                secid = row['SECID']
                last = row['LAST']
                open_price = row['OPEN']
                if isinstance(last, (int, float)) and isinstance(open_price, (int, float)) and open_price != 0:
                    change = ((last - open_price) / open_price) * 100
                    ticker_change[secid] = change

        total_amount = data["total_amount"]
        positions = []
        portfolio_equities = []

        for pos in data["positions"]:
            ticker = pos["ticker"]
            sector_name = db.get_sector(ticker)
            value = pos["quantity"] * pos["price"]
            share = (value / total_amount * 100) if total_amount > 0 else 0

            avg_formatted = smart_price(pos["avg_price"])

            positions.append({
                "ticker": ticker,
                "name": pos["name"] or pos["ticker"],  # если name None, подставляем тикер
                "price_formatted": smart_price(pos["price"]),
                "avg_price_formatted": avg_formatted,
                "value": value,
                "yield_pct": pos["pos_yield_pct"],
                "sector": sector_name,
                "share": round(share, 1),
            })

            if sector_name and sector_name not in ("Прочие", "Фонд", "Облигации"):
                change = ticker_change.get(ticker)
                pct = change if change is not None else pos["pos_yield_pct"]
                portfolio_equities.append({
                    "name": pos["name"],
                    "price_formatted": smart_price(pos["price"]),
                    "change_pct": pct,
                })

        gainers_list = [p for p in portfolio_equities if p["change_pct"] > 0]
        losers_list = [p for p in portfolio_equities if p["change_pct"] < 0]
        gainers_list.sort(key=lambda x: x["change_pct"], reverse=True)
        losers_list.sort(key=lambda x: x["change_pct"])

        portfolio_gainers = gainers_list[:5]
        portfolio_losers = losers_list[:5]

        sectors = {}
        for p in positions:
            sec = p["sector"]
            sectors[sec] = sectors.get(sec, 0) + p["value"]
        sector_list = [{"name": k, "value": v} for k, v in sectors.items()]

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

# ---------- НОВЫЕ ЭНДПОИНТЫ ДЛЯ ВЫПЛАТ ----------
@app.get("/api/my-dividends")
async def api_my_dividends(request: Request):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        dividends = db.get_personal_dividends()
        return JSONResponse(dividends)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/dividends-yearly")
async def api_dividends_yearly(request: Request, year: int = None, ticker: str = None):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Если передан ticker (это может быть отображаемое имя), пытаемся найти фактический тикер
        actual_ticker = None
        if ticker:
            # 1. Ищем в name_overrides, где display_name = ticker
            c.execute("SELECT ticker FROM name_overrides WHERE display_name = ?", (ticker,))
            row = c.fetchone()
            if row:
                actual_ticker = row[0]
            else:
                # 2. Ищем в ticker_to_name (глобальный словарь) по значению
                for t, name in ticker_to_name.items():
                    if name == ticker:
                        actual_ticker = t
                        break
                # 3. Если не нашли, считаем, что передан сам тикер
                if not actual_ticker:
                    actual_ticker = ticker

        if year and actual_ticker:
            # Детализация по году и тикеру
            c.execute("""SELECT date, ticker, payment FROM operations 
                         WHERE type IN ('Выплата дивидендов', 'Выплата купонов') 
                         AND currency = 'RUB' AND date LIKE ? 
                         AND ticker = ? 
                         ORDER BY date DESC""", 
                      (f"{year}%", actual_ticker))
            rows = c.fetchall()
            conn.close()
            details = []
            for r in rows:
                tick = r[1]
                if tick is None:
                    tick = "Прочие"
                if tick != "Прочие":
                    name = NAME_OVERRIDES.get(tick) or ticker_to_name.get(tick, tick)
                else:
                    name = "Прочие"
                details.append({"date": r[0], "name": name, "amount": r[2]})
            return JSONResponse({"year": year, "ticker": ticker, "details": details})
        
        elif actual_ticker:
            # Без года – возвращаем все выплаты по активу, сгруппированные по годам
            c.execute("""SELECT date, ticker, payment FROM operations 
                         WHERE type IN ('Выплата дивидендов', 'Выплата купонов') 
                         AND currency = 'RUB' 
                         AND ticker = ? 
                         ORDER BY date DESC""", 
                      (actual_ticker,))
            rows = c.fetchall()
            conn.close()
            details = []
            yearly_totals = {}
            for r in rows:
                tick = r[1]
                y = r[0][:4]
                if tick is None:
                    tick = "Прочие"
                if tick != "Прочие":
                    name = NAME_OVERRIDES.get(tick) or ticker_to_name.get(tick, tick)
                else:
                    name = "Прочие"
                details.append({"date": r[0], "name": name, "amount": r[2]})
                yearly_totals[y] = yearly_totals.get(y, 0) + r[2]
            return JSONResponse({"ticker": ticker, "details": details, "yearly_totals": yearly_totals})
        
        else:
            # Общий график (как было)
            c.execute("SELECT date, ticker, payment FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date")
            rows = c.fetchall()
            conn.close()
            yearly = {}
            for r in rows:
                y = r[0][:4]
                tick = r[1]
                if tick is None:
                    tick = "Прочие"
                if tick != "Прочие":
                    name = NAME_OVERRIDES.get(tick) or ticker_to_name.get(tick, tick)
                else:
                    name = "Прочие"
                if y not in yearly:
                    yearly[y] = {}
                if name not in yearly[y]:
                    yearly[y][name] = 0.0
                yearly[y][name] += r[2]
            years = sorted(yearly.keys())
            datasets = []
            for name in sorted(set(n for y in yearly.values() for n in y.keys())):
                datasets.append({
                    "label": name,
                    "data": [yearly[y].get(name, 0) for y in years]
                })
            return JSONResponse({"years": years, "datasets": datasets})
    except Exception as e:
        logging.error(f"Error in /api/dividends-yearly: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/sync")
async def api_sync(request: Request):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        from tinkoff_api import sync_operations
        new_count = await sync_operations(bot_session)
        now_moscow = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)).isoformat()
        return JSONResponse({
            "status": "ok",
            "new_operations": new_count,
            "last_sync": now_moscow
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/sync-status")
async def api_sync_status(request: Request):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    last_date = db.get_last_operation_date()
    return JSONResponse({"last_sync": last_date})
    
@app.post("/api/sector")
async def set_sector(request: Request):
    if not check_token(request):
        raise HTTPException(403)
    body = await request.json()
    ticker = body.get("ticker")
    sector = body.get("sector")
    if not ticker or not sector:
        raise HTTPException(400, "Missing ticker or sector")
    db.update_instrument_sector(ticker, sector)
    from moex_api import ticker_to_sector
    ticker_to_sector[ticker] = sector
    return JSONResponse({"status": "ok"})
    
@app.get("/api/sectors/list")
async def get_sectors_list(request: Request):
    if not check_token(request):
        raise HTTPException(403)
    # Возвращаем список уникальных секторов из справочника
    sectors = sorted(set(SECTOR_NAMES.values()))
    return JSONResponse(sectors)
    
@app.get("/api/instruments")
async def get_instruments(request: Request):
    if not check_token(request):
        raise HTTPException(403)
    try:
        instruments = db.get_all_instruments()
        # можно отфильтровать только те, что есть в портфеле, или все
        return JSONResponse(instruments)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    
@app.get("/api/operations/unticked")
async def get_unticked_operations(request: Request):
    if not check_token(request):
        raise HTTPException(403)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT id, date, payment, ticker, name 
                     FROM operations 
                     WHERE (ticker IS NULL OR ticker = 'Прочие') 
                       AND type IN ('Выплата дивидендов', 'Выплата купонов')
                     ORDER BY date DESC""")
        rows = c.fetchall()
        conn.close()
        result = []
        for r in rows:
            result.append({
                "id": r[0],
                "date": r[1],
                "payment": r[2],
                "ticker": r[3],
                "name": r[4] or "Неизвестно"
            })
        return JSONResponse(result)
    except Exception as e:
        logging.error(f"Error in /api/operations/unticked: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/operations/link")
async def link_ticker_to_operation(request: Request):
    if not check_token(request):
        raise HTTPException(403)
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

    await load_instrument_names(bot_session, force=True)
    register_handlers(dp)

    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"✅ Вебхук установлен: {WEBHOOK_URL}")

    try:
        await bot.send_message(MY_CHAT_ID, f"🚀 Бот перезапущен и готов к работе! ver: {VERSION}")
    except Exception as e:
        logging.error(f"❌ Не удалось отправить уведомление о запуске: {e}")

    # Строим карту FIGI → ticker из портфеля
    if TINKOFF_TOKEN:
        from tinkoff_api import build_figi_map
        await build_figi_map(bot_session)

    # Первая синхронизация операций
    if TINKOFF_TOKEN:
        try:
            from tinkoff_api import sync_operations
            asyncio.create_task(sync_operations(bot_session))
        except Exception as e:
            logging.error(f"Ошибка запуска первой синхронизации: {e}")

    asyncio.create_task(scheduler.scheduler_loop())
    if TINKOFF_TOKEN:
        asyncio.create_task(portfolio_updater(bot_session))

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    logging.info(f"✅ FastAPI сервер запущен на порту {PORT}")
    await server.serve()

    await bot_session.close()
    logging.info("✅ HTTP сессия закрыта")

if __name__ == "__main__":
    asyncio.run(main())