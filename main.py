"""
main.py — FastAPI + вебхук Telegram + фоновые задачи
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
from aiogram import Bot, Dispatcher, types
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
import os
import json
from datetime import datetime
import pytz

# Импорты из наших модулей
import config
import db
import moex_api
import tinkoff_api
import handlers
import scheduler
import keyboards
import utils
from utils import get_moscow_time, smart_price

# Настройка логирования
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("data/logs/bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
BOT_TOKEN = config.BOT_TOKEN
MY_CHAT_ID = config.MY_CHAT_ID
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# FastAPI приложение
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Старт
    logger.info("Запуск приложения...")
    db.init_db()  # создаём таблицы, если нет
    # Загружаем кеш инструментов, если нужно (вызов из moex_api)
    await moex_api.update_instruments_cache_if_needed()
    # Запускаем планировщик (если он есть в scheduler)
    scheduler.start_scheduler(bot)
    # Устанавливаем вебхук
    webhook_url = f"{config.WEBHOOK_BASE_URL}/webhook"
    await bot.set_webhook(url=webhook_url)
    logger.info(f"Вебхук установлен: {webhook_url}")
    yield
    # Завершение
    logger.info("Остановка приложения...")
    scheduler.stop_scheduler()
    await bot.delete_webhook()
    db.close_db()
    logger.info("Соединение с БД закрыто.")

app = FastAPI(lifespan=lifespan)

# -------------------- Telegram Webhook --------------------
@app.post("/webhook")
async def webhook(request: Request):
    """Принимает обновления от Telegram."""
    try:
        data = await request.json()
        update = Update(**data)
        await dp.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Ошибка в вебхуке: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# -------------------- Mini App API --------------------
@app.get("/api/portfolio")
async def get_portfolio_data(request: Request):
    """Возвращает данные для Mini App: портфель с секторами, лидеры роста/падения."""
    # Проверка токена (если включена)
    token = request.headers.get("X-Mini-App-Token")
    if token != config.MINI_APP_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Получаем портфель из БД
    portfolio = db.get_portfolio()
    if not portfolio:
        return JSONResponse({"error": "Портфель пуст"})

    # Группировка по секторам для диаграммы
    sectors_data = {}
    for item in portfolio:
        sector = item.get('sector_name', 'Прочее')
        if sector not in sectors_data:
            sectors_data[sector] = {'total': 0.0, 'items': []}
        price = item.get('last_snapshot_price') or item.get('avg_price', 0)
        value = item['quantity'] * price
        sectors_data[sector]['total'] += value
        sectors_data[sector]['items'].append({
            'ticker': item['ticker'],
            'name': item.get('custom_name') or item['ticker'],
            'quantity': item['quantity'],
            'avg_price': item['avg_price'],
            'current_price': price,
            'sector': sector,
            'value': value
        })

    # Сортируем сектора по убыванию стоимости
    sectors_list = [
        {'sector': name, 'total': data['total']}
        for name, data in sectors_data.items()
    ]
    sectors_list.sort(key=lambda x: x['total'], reverse=True)

    # Получаем топ-5 лидеров роста и падения (только акции)
    # Для этого нам нужны текущие цены и цены закрытия вчера (или снэпшот)
    # Допустим, мы используем данные из moex_api для всех инструментов
    # Здесь можно использовать get_all_instruments() и текущие котировки
    # Для простоты примера я пропущу детальную реализацию, но покажу структуру
    # В реальности вы вызовете moex_api.get_prices_for_tickers() или что-то подобное

    # Пример: лидеры роста/падения (заглушка)
    leaders = {
        'gainers': [],  # список {ticker, name, change_percent}
        'losers': []
    }

    # Получаем все инструменты из кеша
    all_inst = db.get_all_instruments()
    # Здесь нужно запросить текущие цены у MOEX и вычислить изменения относительно вчерашнего закрытия
    # Это можно сделать через moex_api.get_market_data(tickers_list)
    # И затем отсортировать

    # Пока заглушка
    # ...

    return JSONResponse({
        'portfolio': portfolio,
        'sectors': sectors_list,
        'leaders': leaders
    })

@app.get("/api/dividends-yearly")
async def dividends_yearly(request: Request):
    """Возвращает сумму дивидендов и купонов по годам для столбчатого графика."""
    token = request.headers.get("X-Mini-App-Token")
    if token != config.MINI_APP_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Получаем все годы, за которые есть операции
    conn = db.get_conn()
    rows = conn.execute("SELECT DISTINCT strftime('%Y', date) as year FROM operations WHERE operation_type IN ('dividend', 'coupon') ORDER BY year").fetchall()
    years = [row['year'] for row in rows]
    if not years:
        return JSONResponse({'years': [], 'dividends': [], 'coupons': []})

    dividends = []
    coupons = []
    for year in years:
        div_sum = db.get_operations_sum_by_year_and_type(int(year), 'dividend')
        coup_sum = db.get_operations_sum_by_year_and_type(int(year), 'coupon')
        dividends.append(div_sum)
        coupons.append(coup_sum)

    return JSONResponse({
        'years': years,
        'dividends': dividends,
        'coupons': coupons
    })

@app.get("/api/dividends-details")
async def dividends_details(request: Request, year: int, ticker: Optional[str] = None):
    """Возвращает детализацию выплат по году и, опционально, по тикеру."""
    token = request.headers.get("X-Mini-App-Token")
    if token != config.MINI_APP_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    if ticker:
        ops = db.get_operations_by_ticker_and_year(ticker, year)
    else:
        # Все операции за год (дивиденды и купоны)
        ops = db.get_operations_by_year(year, None)  # без фильтра по типу
        # Отфильтруем только выплаты
        ops = [op for op in ops if op['operation_type'] in ('dividend', 'coupon')]

    return JSONResponse({'operations': ops})

@app.get("/api/instrument-mapping")
async def get_mapping(request: Request):
    """Возвращает все переименования."""
    token = request.headers.get("X-Mini-App-Token")
    if token != config.MINI_APP_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    mapping = db.get_instrument_mapping()
    return JSONResponse({'mapping': mapping})

@app.post("/api/instrument-mapping")
async def set_mapping(request: Request):
    """Устанавливает кастомное имя для тикера."""
    token = request.headers.get("X-Mini-App-Token")
    if token != config.MINI_APP_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    ticker = data.get('ticker')
    custom_name = data.get('custom_name')
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    db.set_instrument_mapping(ticker, custom_name=custom_name)
    return JSONResponse({'status': 'ok'})

@app.delete("/api/instrument-mapping")
async def delete_mapping(request: Request):
    """Удаляет переименование для тикера."""
    token = request.headers.get("X-Mini-App-Token")
    if token != config.MINI_APP_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    ticker = data.get('ticker')
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    db.set_instrument_mapping(ticker, custom_name=None, figi=None)  # удаляем
    return JSONResponse({'status': 'ok'})

# -------------------- Mini App HTML --------------------
@app.get("/mini-app", response_class=HTMLResponse)
async def mini_app(request: Request):
    """Отдаёт HTML Mini App."""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Мой портфель</title>
        <!-- Подключите Chart.js и Tailwind (или Bootstrap) -->
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            body { background: #f7fafc; }
            .container { max-width: 800px; margin: 0 auto; padding: 20px; }
            .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 20px; }
            h2 { font-size: 1.5rem; font-weight: bold; margin-bottom: 1rem; }
        </style>
    </head>
    <body>
        <div class="container" id="app">
            <div id="loading" class="text-center py-10">Загрузка...</div>
            <div id="content" style="display:none;">
                <!-- Портфель -->
                <div class="card">
                    <h2>Портфель по секторам</h2>
                    <canvas id="sectorChart" height="200"></canvas>
                </div>
                <!-- Лидеры -->
                <div class="card">
                    <h2>Топ-5 роста</h2>
                    <ul id="gainersList"></ul>
                    <h2>Топ-5 падения</h2>
                    <ul id="losersList"></ul>
                </div>
                <!-- Выплаты -->
                <div class="card">
                    <h2>Выплаты по годам</h2>
                    <canvas id="dividendsChart" height="200"></canvas>
                </div>
                <!-- Детализация по клику (пока пусто) -->
                <div id="details" class="card" style="display:none;">
                    <h2>Детали</h2>
                    <div id="detailsContent"></div>
                </div>
                <!-- Кнопка синхронизации -->
                <button id="syncBtn" class="bg-blue-500 text-white px-4 py-2 rounded">Синхронизировать с Т-Инвестициями</button>
            </div>
        </div>
        <script>
            const token = '{{ token }}';  // вставляется сервером

            async function fetchData(url) {
                const response = await fetch(url, { headers: { 'X-Mini-App-Token': token } });
                if (!response.ok) throw new Error('Ошибка загрузки');
                return response.json();
            }

            async function loadData() {
                try {
                    const [portfolioData, dividendsData] = await Promise.all([
                        fetchData('/api/portfolio'),
                        fetchData('/api/dividends-yearly')
                    ]);
                    renderPortfolio(portfolioData);
                    renderDividends(dividendsData);
                    document.getElementById('loading').style.display = 'none';
                    document.getElementById('content').style.display = 'block';
                } catch (e) {
                    document.getElementById('loading').textContent = 'Ошибка загрузки данных';
                    console.error(e);
                }
            }

            function renderPortfolio(data) {
                // Секторная диаграмма
                const ctx = document.getElementById('sectorChart').getContext('2d');
                const sectors = data.sectors || [];
                new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: sectors.map(s => s.sector),
                        datasets: [{
                            label: 'Стоимость (руб)',
                            data: sectors.map(s => s.total),
                            backgroundColor: 'rgba(54, 162, 235, 0.6)',
                            borderColor: 'rgba(54, 162, 235, 1)',
                            borderWidth: 1
                        }]
                    },
                    options: {
                        responsive: true,
                        plugins: {
                            legend: { display: false }
                        }
                    }
                });

                // Лидеры
                const gainers = data.leaders?.gainers || [];
                const losers = data.leaders?.losers || [];
                const gainersList = document.getElementById('gainersList');
                const losersList = document.getElementById('losersList');
                gainersList.innerHTML = gainers.map(g => `<li>${g.name} (${g.ticker}) ${g.change_percent}%</li>`).join('');
                losersList.innerHTML = losers.map(l => `<li>${l.name} (${l.ticker}) ${l.change_percent}%</li>`).join('');
            }

            function renderDividends(data) {
                const ctx = document.getElementById('dividendsChart').getContext('2d');
                const years = data.years || [];
                new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: years,
                        datasets: [
                            {
                                label: 'Дивиденды',
                                data: data.dividends || [],
                                backgroundColor: 'rgba(75, 192, 192, 0.6)',
                                borderColor: 'rgba(75, 192, 192, 1)',
                                borderWidth: 1
                            },
                            {
                                label: 'Купоны',
                                data: data.coupons || [],
                                backgroundColor: 'rgba(255, 206, 86, 0.6)',
                                borderColor: 'rgba(255, 206, 86, 1)',
                                borderWidth: 1
                            }
                        ]
                    },
                    options: {
                        responsive: true,
                        plugins: {
                            legend: { position: 'top' }
                        },
                        onClick: (e, item) => {
                            if (item.length > 0) {
                                const year = years[item[0].datasetIndex];
                                showDetails(year);
                            }
                        }
                    }
                });
            }

            async function showDetails(year) {
                // Запрос деталей по году
                try {
                    const data = await fetchData(`/api/dividends-details?year=${year}`);
                    const content = document.getElementById('detailsContent');
                    if (data.operations && data.operations.length) {
                        content.innerHTML = `<h3>Выплаты за ${year}</h3><ul>${data.operations.map(op => `<li>${op.ticker}: ${op.payment} руб (${op.date})</li>`).join('')}</ul>`;
                    } else {
                        content.innerHTML = `<h3>Выплаты за ${year}</h3><p>Нет данных</p>`;
                    }
                    document.getElementById('details').style.display = 'block';
                } catch (e) {
                    console.error(e);
                }
            }

            // Синхронизация
            document.getElementById('syncBtn').addEventListener('click', async () => {
                // Здесь вызывается ваш эндпоинт для синхронизации, например, /api/sync
                // Или просто отправка команды боту
                alert('Синхронизация запущена (заглушка)');
            });

            loadData();
        </script>
    </body>
    </html>
    """
    # Заменяем {{ token }} на реальный токен
    html_content = html_content.replace('{{ token }}', config.MINI_APP_SECRET)
    return HTMLResponse(content=html_content)

# -------------------- Дополнительные эндпоинты --------------------
# Можно добавить эндпоинт для синхронизации с Т-Инвестициями
@app.post("/api/sync")
async def sync_operations(request: Request):
    """Запускает синхронизацию операций из Т-Инвестиций."""
    token = request.headers.get("X-Mini-App-Token")
    if token != config.MINI_APP_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    # Запускаем синхронизацию в фоне
    asyncio.create_task(tinkoff_api.sync_all_operations())
    return JSONResponse({"status": "sync started"})

# -------------------- Регистрация обработчиков команд --------------------
# Подключаем handlers (если они определены)
# например, dp.message.register(handlers.start_command, Command("start"))
# dp.callback_query.register(handlers.button_callback)
# Но так как у вас handlers.py, вы должны зарегистрировать их там.

# Если handlers.py экспортирует функцию register_handlers(dp), то вызываем:
# handlers.register_handlers(dp)

# -------------------- Запуск (если файл запускается напрямую) --------------------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)