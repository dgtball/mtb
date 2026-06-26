import os
import sys
import logging
import time
import datetime
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION, TINKOFF_TOKEN
import db
from moex_api import load_instrument_names
from handlers import register_handlers, set_http_session, set_bot
import scheduler

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

    logging.info("✅ Запускаем polling...")
    await dp.start_polling(bot)

    await bot_session.close()
    logging.info("✅ HTTP сессия закрыта")

if __name__ == "__main__":
    asyncio.run(main())