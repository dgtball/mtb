import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler
import asyncio
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION, TINKOFF_TOKEN
import db
from moex_api import load_instrument_names
from handlers import register_handlers, set_http_session, set_bot
import scheduler

# Убедимся, что папка logs существует
log_dir = os.path.join(os.getenv('DATA_DIR', '/app/data'), 'logs')
os.makedirs(log_dir, exist_ok=True)

# Основной файл (INFO и выше)
file_handler = TimedRotatingFileHandler(
    os.path.join(log_dir, 'bot.log'),
    when='midnight',
    interval=1,
    backupCount=7,  # хранить последние 7 дней
    encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# Консольный вывод (только WARNING и выше, чтобы не захламлять stdout)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))

# Настройка корневого логгера
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)

# Отключаем лишние логгеры
logging.getLogger('aiogram.event').setLevel(logging.WARNING)
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
logging.getLogger('aiohttp.client').setLevel(logging.WARNING)

try:
    from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION
    import db
    from moex_api import load_instrument_names
    from handlers import register_handlers, set_http_session, set_bot
except Exception as e:
    logging.critical("CRITICAL IMPORT ERROR", exc_info=True)
    sys.exit(1)

if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

async def health_handler(request):
    return web.Response(text="OK")

async def run_health_server():
    app = web.Application()
    app.router.add_get('/health', health_handler)
    app.router.add_get('/', health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"✅ Health‑сервер запущен на порту {PORT}")
    await asyncio.Event().wait()
    
    
   # ---------- ФОНОВОЕ ОБНОВЛЕНИЕ ПОРТФЕЛЯ ----------
async def portfolio_updater(http_session):
    """Периодически обновляет текущую стоимость портфеля, но только днём."""
    import scheduler
    await asyncio.sleep(10)  # первичная задержка после запуска
    while True:
        try:
            if scheduler.is_portfolio_update_allowed():
                from tinkoff_api import get_portfolio_summary
                data = await get_portfolio_summary(http_session)
                if data:
                    total = data['total_amount']
                    db.set_portfolio_value(total)
                    logging.debug(f"Портфель автообновлён: {total:.2f}")
                await asyncio.sleep(300)  # 5 минут
            else:
                await asyncio.sleep(60)  # ночью проверяем раз в минуту, не проснулись ли
        except Exception as e:
            logging.error(f"Ошибка автообновления портфеля: {e}")
            await asyncio.sleep(60)

async def main():
    db.init_db()
    db.load_name_overrides()

    http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
    set_http_session(http_session)
    set_bot(bot)
    scheduler.set_bot(bot)
    scheduler.set_http_session(http_session)

    await load_instrument_names(http_session)

    register_handlers(dp)

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("✅ Вебхук удалён")

    try:
        await bot.send_message(MY_CHAT_ID, f"🚀 Бот перезапущен и готов к работе! ver: {VERSION}")
    except Exception as e:
        logging.error(f"❌ Не удалось отправить уведомление о запуске: {e}")

    asyncio.create_task(scheduler.scheduler_loop())

    logging.info("✅ Запускаем polling...")
    polling_task = asyncio.create_task(dp.start_polling(bot))
    health_task = asyncio.create_task(run_health_server())

    done, pending = await asyncio.wait(
        [polling_task, health_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    await http_session.close()
    logging.info("✅ HTTP сессия закрыта")

if __name__ == "__main__":
    asyncio.run(main())