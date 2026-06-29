import os
import sys
import logging
import time
import datetime
import asyncio
import aiohttp
import sqlite3
import uvicorn
import db

import scheduler
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage

from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION, TINKOFF_TOKEN, SECTOR_NAMES, MINI_APP_SECRET, NAME_OVERRIDES, ticker_to_name, DB_PATH, WEBHOOK_URL, DATA_DIR
from moex_api import load_instrument_names, ticker_to_sector
from handlers import register_handlers, set_http_session, set_bot
from services.portfolio import get_portfolio_with_details
from utils import retry


# Глобальная переменная для HTTP-сессии (будет установлена в main())
bot_session = None

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

# ---------- ПРОВЕРКА DATA_DIR ----------
if not os.path.exists(os.getenv('DATA_DIR', '/app/data')):
    os.makedirs(os.getenv('DATA_DIR', '/app/data'), exist_ok=True)
    logging.info(f"📁 Создана директория данных: {os.getenv('DATA_DIR', '/app/data')}")

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
    try:
        with open("mini_app.html", "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("MINI_APP_TOKEN_PLACEHOLDER", MINI_APP_SECRET)
        return HTMLResponse(content=html)
    except FileNotFoundError:
        logging.error("❌ mini_app.html не найден")
        return HTMLResponse("<h1>Ошибка: mini_app.html не найден</h1>", status_code=404)

@app.get("/api/portfolio")
async def api_portfolio(request: Request):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        data = await get_portfolio_with_details(bot_session)
        if not data:
            return JSONResponse({"error": "Нет данных"}, status_code=404)
        return JSONResponse(data)
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

@app.get("/api/dividends-monthly")
async def api_dividends_monthly(request: Request, year: int = None):
    logging.info(f"Запрос /api/dividends-monthly для года {year}")
    if not check_token(request):
        raise HTTPException(403)
    try:
        if year is None:
            year = datetime.datetime.now().year

        # ---------- 1. Получаем портфель с количествами ----------
        from tinkoff_api import get_portfolio_summary
        portfolio = await get_portfolio_summary(bot_session)
        if not portfolio:
            portfolio_positions = []
        else:
            portfolio_positions = portfolio.get("positions", [])

        # Строим словарь {ticker: quantity}
        portfolio_quantities = {}
        for pos in portfolio_positions:
            ticker = pos["ticker"]
            quantity = pos["quantity"]
            if ticker and quantity > 0:
                portfolio_quantities[ticker] = quantity

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # ---------- 2. Фактические выплаты (из операций) ----------
        c.execute("""SELECT date, ticker, payment, name 
                     FROM operations 
                     WHERE type IN ('Выплата дивидендов', 'Выплата купонов') 
                       AND currency = 'RUB' 
                       AND date LIKE ? 
                     ORDER BY date""", (f"{year}%",))
        rows = c.fetchall()
        
        actual_by_month = {m: {"total": 0.0, "details": []} for m in range(1, 13)}
        for r in rows:
            date_str = r[0]
            month = int(date_str[5:7])
            amount = r[2]
            ticker = r[1]
            name_display = r[3] or ticker
            if ticker is None or ticker == "Прочие":
                display_name = "Прочие"
            else:
                display_name = NAME_OVERRIDES.get(ticker) or ticker_to_name.get(ticker, ticker)
            actual_by_month[month]["total"] += amount
            actual_by_month[month]["details"].append({
                "date": date_str,
                "ticker": ticker,
                "name": display_name,
                "amount": amount,
                "type": "actual"
            })

        # ---------- 3. Объявленные дивиденды (из календаря) ----------
        # Только будущие выплаты (payment_date > сегодня)
        c.execute("""
            SELECT ticker, payment_date, record_date, dividend_net
            FROM dividend_calendar
            WHERE strftime('%Y', payment_date) = ?
              AND payment_date > date('now')
        """, (str(year),))
        declared_dividends = c.fetchall()

        declared_before_record = {m: {"total": 0.0, "details": []} for m in range(1, 13)}
        declared_after_record = {m: {"total": 0.0, "details": []} for m in range(1, 13)}

        for row in declared_dividends:
            ticker = row[0]
            payment_date = row[1]
            record_date = row[2]
            dividend_per_share = row[3]
            if not payment_date or not dividend_per_share:
                continue
            # Получаем количество акций из портфеля
            quantity = portfolio_quantities.get(ticker, 0)
            if quantity == 0:
                # Если акции нет в портфеле – пропускаем
                continue
            amount = dividend_per_share * quantity
            month = int(record_date[5:7])  # группируем по record_date
            name = NAME_OVERRIDES.get(ticker) or ticker_to_name.get(ticker, ticker)
            # Определяем статус
            if record_date and record_date >= datetime.date.today().isoformat():
                # реестр открыт – объявлены
                declared_before_record[month]["total"] += amount
                declared_before_record[month]["details"].append({
                    "date": record_date,  # показываем дату закрытия в деталях
                    "ticker": ticker,
                    "name": name,
                    "amount": amount,
                    "type": "declared_dividend_before",
                    "record_date": record_date,
                    "payment_date": payment_date
                })
            else:
                # реестр закрыт – ожидаемые
                declared_after_record[month]["total"] += amount
                declared_after_record[month]["details"].append({
                    "date": payment_date,
                    "ticker": ticker,
                    "name": name,
                    "amount": amount,
                    "type": "declared_dividend_after",
                    "record_date": record_date,
                    "payment_date": payment_date
                })

        # ---------- 4. Объявленные купоны (из календаря купонов) ----------
        c.execute("""
            SELECT ticker, coupon_date, coupon_value, record_date
            FROM coupon_calendar
            WHERE strftime('%Y', coupon_date) = ?
              AND coupon_date > date('now')
        """, (str(year),))
        declared_coupons = c.fetchall()

        for row in declared_coupons:
            ticker = row[0]
            coupon_date = row[1]      # payment_date (дата выплаты)
            coupon_per_bond = row[2]
            record_date = row[3]      # fixDate – дата фиксации реестра
            if not coupon_date or not coupon_per_bond:
                continue
            quantity = portfolio_quantities.get(ticker, 0)
            if quantity == 0:
                continue
            amount = coupon_per_bond * quantity
            month = int(record_date[5:7]) if record_date else int(coupon_date[5:7])
            name = NAME_OVERRIDES.get(ticker) or ticker_to_name.get(ticker, ticker)
            # Купоны показываем как "Объявлены" (жёлтый), так как у них нет разделения по реестру (или используем record_date)
            # Для единообразия будем считать, что если record_date >= сегодня – объявлены, иначе ожидаемые
            if record_date and record_date >= datetime.date.today().isoformat():
                declared_before_record[month]["total"] += amount
                declared_before_record[month]["details"].append({
                    "date": coupon_date,
                    "ticker": ticker,
                    "name": name,
                    "amount": amount,
                    "type": "declared_coupon_before",
                    "record_date": record_date,
                    "payment_date": coupon_date
                })
            else:
                declared_after_record[month]["total"] += amount
                declared_after_record[month]["details"].append({
                    "date": coupon_date,
                    "ticker": ticker,
                    "name": name,
                    "amount": amount,
                    "type": "declared_coupon_after",
                    "record_date": record_date,
                    "payment_date": coupon_date
                })

        # ---------- 5. Доступные годы (из операций) ----------
        c.execute("SELECT DISTINCT substr(date, 1, 4) FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date DESC")
        years_rows = c.fetchall()
        years = [int(row[0]) for row in years_rows if row[0] is not None]
        conn.close()

        months_labels = ['Янв','Фев','Мар','Апр','Май','Июн','Июл','Авг','Сен','Окт','Ноя','Дек']
        actual_data = [actual_by_month[m]["total"] for m in range(1, 13)]
        before_data = [declared_before_record[m]["total"] for m in range(1, 13)]
        after_data = [declared_after_record[m]["total"] for m in range(1, 13)]

        total_actual = sum(actual_data)
        total_before = sum(before_data)
        total_after = sum(after_data)

        return JSONResponse({
            "year": year,
            "months": months_labels,
            "actual": actual_data,
            "declared_before_record": before_data,
            "declared_after_record": after_data,
            "total_actual": total_actual,
            "total_before": total_before,
            "total_after": total_after,
            "details_actual": {m: actual_by_month[m]["details"] for m in range(1, 13)},
            "details_declared_before": {m: declared_before_record[m]["details"] for m in range(1, 13)},
            "details_declared_after": {m: declared_after_record[m]["details"] for m in range(1, 13)},
            "available_years": years
        })
    except Exception as e:
        logging.error(f"Error in /api/dividends-monthly: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
        
@app.get("/api/sync-status")
async def api_sync_status(request: Request):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    last_date = db.get_last_operation_date()
    return JSONResponse({"last_sync": last_date})

@app.post("/api/sync")
async def api_sync(request: Request):
    if not check_token(request):
        raise HTTPException(403)
    try:
        from tinkoff_api import sync_operations
        full = request.query_params.get("full") == "true"
        new_count = await sync_operations(bot_session, force_full=full)
        now_moscow = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)).isoformat()
        return JSONResponse({
            "status": "ok",
            "new_operations": new_count,
            "last_sync": now_moscow,
            "full": full
        })
    except Exception as e:
        logging.error(f"Sync error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/sync-full")
async def api_sync_full(request: Request):
    if not check_token(request):
        raise HTTPException(403)
    try:
        from tinkoff_api import sync_operations
        new_count = await sync_operations(bot_session, force_full=True)
        now_moscow = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)).isoformat()
        return JSONResponse({
            "status": "ok",
            "new_operations": new_count,
            "last_sync": now_moscow,
            "full": True
        })
    except Exception as e:
        logging.error(f"Sync-full error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/sync-calendars")
async def sync_calendars(request: Request):
    if not check_token(request):
        raise HTTPException(403)
    try:
        from tinkoff_api import fetch_all_dividends, fetch_all_coupons
        dividends_data = await fetch_all_dividends(bot_session)
        coupons_data = await fetch_all_coupons(bot_session)
        return JSONResponse({
            "status": "ok",
            "dividends_updated": len(dividends_data),
            "coupons_updated": len(coupons_data)
        })
    except Exception as e:
        logging.error(f"Calendar sync error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

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
        # 1. Получаем операции без тикера
        c.execute("""SELECT id, date, payment, ticker, name 
                     FROM operations 
                     WHERE (ticker IS NULL OR ticker = 'Прочие') 
                       AND type IN ('Выплата дивидендов', 'Выплата купонов')
                     ORDER BY date DESC""")
        rows = c.fetchall()
        operations = []
        for r in rows:
            operations.append({
                "id": r[0],
                "date": r[1],
                "payment": r[2],
                "ticker": r[3],
                "name": r[4] or "Неизвестно"
            })
        
        # 2. Собираем все возможные тикеры для выпадающего списка:
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

# ---------- Объявленные дивиденды и купоны ----------

@app.post("/api/debug-fetch-dividends")
async def debug_fetch_dividends(request: Request):
    if not check_token(request):
        raise HTTPException(403)
    try:
        from tinkoff_api import fetch_all_dividends, fetch_all_coupons
        logging.info("Debug: fetching dividends...")
        dividends_data = await fetch_all_dividends(bot_session)
        logging.info(f"Debug: dividends_data = {dividends_data}")
        coupons_data = await fetch_all_coupons(bot_session)
        logging.info(f"Debug: coupons_data = {coupons_data}")
        return JSONResponse({"dividends": dividends_data, "coupons": coupons_data})
    except Exception as e:
        logging.error(f"Debug error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/upcoming-payments")
async def get_upcoming_payments(request: Request, year: int = None, month: int = None):
    if not check_token(request):
        raise HTTPException(403)
    try:
        if year is None:
            year = datetime.datetime.now().year
        if month is None:
            month = datetime.datetime.now().month
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Дивиденды
        c.execute("""
            SELECT ticker, payment_date, dividend_net, figi
            FROM dividend_calendar
            WHERE strftime('%Y', payment_date) = ? AND strftime('%m', payment_date) = ?
        """, (str(year), f"{month:02d}"))
        dividends = [{"ticker": row[0], "date": row[1], "amount": row[2], "type": "dividend"} for row in c.fetchall()]
        
        # Купоны
        c.execute("""
            SELECT ticker, coupon_date, coupon_value, figi
            FROM coupon_calendar
            WHERE strftime('%Y', coupon_date) = ? AND strftime('%m', coupon_date) = ?
        """, (str(year), f"{month:02d}"))
        coupons = [{"ticker": row[0], "date": row[1], "amount": row[2], "type": "coupon"} for row in c.fetchall()]
        
        conn.close()
        
        return JSONResponse({
            "year": year,
            "month": month,
            "dividends": dividends,
            "coupons": coupons
        })
    except Exception as e:
        logging.error(f"Error in /api/upcoming-payments: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
        
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

    # Загружаем инструменты из кэша или MOEX (с retry)
    await load_instrument_names(bot_session)
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
    try:
        await server.serve()
    finally:
        await bot_session.close()
        logging.info("✅ HTTP сессия закрыта")

if __name__ == "__main__":
    asyncio.run(main())