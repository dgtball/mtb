import aiosqlite
import logging
from contextlib import closing
from config import DB_PATH, NAME_OVERRIDES, ticker_to_name

# ---------- ИНИЦИАЛИЗАЦИЯ ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS name_overrides
                                (ticker TEXT PRIMARY KEY, display_name TEXT)''')
        async with conn:
            await conn.execute('''CREATE TABLE IF NOT EXISTS portfolio_state
                                    (key TEXT PRIMARY KEY, value REAL)''')
            async with conn:
                await conn.execute('''CREATE TABLE IF NOT EXISTS sectors
                                        (ticker TEXT PRIMARY KEY, sector_name TEXT)''')
                async with conn:
                    await conn.execute('''CREATE TABLE IF NOT EXISTS operations
                                            (id TEXT PRIMARY KEY, date TEXT NOT NULL,
                                             type TEXT NOT NULL, ticker TEXT, figi TEXT,
                                             instrument_type TEXT, quantity INTEGER, payment REAL,
                                             currency TEXT, commission REAL, name TEXT)''')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_operations_date ON operations(date)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_operations_type ON operations(type)')
                    await conn.commit()
                async with conn:
                    await conn.execute('''CREATE TABLE IF NOT EXISTS instruments 
                                            (ticker TEXT PRIMARY KEY, name TEXT, sector TEXT,
                                             figi TEXT, instrument_type TEXT, updated_at TIMESTAMP,
                                             maturity_date TEXT, coupon_period INTEGER)''')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_instruments_figi ON instruments(figi)')
                    await conn.commit()
                async with conn:
                    await conn.execute('''CREATE TABLE IF NOT EXISTS dividend_calendar (
                                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                                            ticker TEXT NOT NULL, figi TEXT NOT NULL,
                                            declared_date TEXT, record_date TEXT,
                                            payment_date TEXT, dividend_net REAL, dividend_type TEXT,
                                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                            UNIQUE(ticker, declared_date, payment_date))''')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_dividend_calendar_ticker ON dividend_calendar(ticker)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_dividend_calendar_payment_date ON dividend_calendar(payment_date)')
                    await conn.commit()
                async with conn:
                    await conn.execute('''CREATE TABLE IF NOT EXISTS coupon_calendar (
                                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                                            ticker TEXT NOT NULL, figi TEXT NOT NULL,
                                            coupon_date TEXT, coupon_value REAL,
                                            coupon_currency TEXT, record_date TEXT,
                                            is_redemption BOOLEAN DEFAULT 0,
                                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                            UNIQUE(ticker, coupon_date))''')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_coupon_calendar_ticker ON coupon_calendar(ticker)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_coupon_calendar_coupon_date ON coupon_calendar(coupon_date)')
                    await conn.commit()
                async with conn:
                    await conn.execute('''CREATE TABLE IF NOT EXISTS dividend_forecast 
                                            (ticker TEXT PRIMARY KEY, forecast_amount REAL, forecast_month INTEGER, forecast_year INTEGER, confidence_score REAL DEFAULT 1.0, method TEXT DEFAULT 'historical_cagr', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_forecast_ticker ON dividend_forecast(ticker)')
                    await conn.commit()
    logging.info(f"✅ База данных инициализирована: {DB_PATH}")
    await seed_overrides()
    await seed_sectors()
    await migrate_sectors_to_instruments()

async def seed_overrides():
    initial = [
        ("WUSH", "ВУШ"),
        ("DELI", "Делимобиль"),
    ]
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT COUNT(*) FROM name_overrides") as c:
            count = (await c.fetchone())[0]
        if count == 0:
            await conn.execute("INSERT OR IGNORE INTO name_overrides (ticker, display_name) VALUES (?, ?)", initial)
            await conn.commit()
            logging.info("✅ Начальные переопределения названий добавлены в БД")
        else:
            for ticker, display_name in initial:
                await conn.execute("INSERT OR IGNORE INTO name_overrides (ticker, display_name) VALUES (?, ?)", (ticker, display_name))
            await conn.commit()
            logging.info("✅ Новые переопределения добавлены в БД")

async def seed_sectors():
    initial_sectors = {
        "SBER": "Финансы",
        "ASTR": "ИТ",
        "CHMF": "Металл",
        "DELI": "Транспорт",
        "FIXR": "Товары",
        "FLOT": "Транспорт",
        "GAZP": "Нефтегаз",
        "HNFG": "Товары",
        "LKOH": "Нефтегаз",
        "MGNT": "Товары",
        "MTLR": "Металл",
        "NVTK": "Нефтегаз",
        "ROSN": "Нефтегаз",
        "RTKM": "ИТ",
        "SMLT": "Стройка",
        "SOFL": "ИТ",
        "WUSH": "Транспорт",
        "TATNP": "Нефтегаз",
        "TRNFP": "Нефтегаз",
        "MDMG": "Медицина",
        "T": "Финансы",
        "VKCO": "ИТ",
        "YDEX": "ИТ",
        "SU26233RMFS5": "Облигации",
        "SU26238RMFS4": "Облигации",
        "SU26240RMFS0": "Облигации",
        "SU26245RMFS9": "Облигации",
        "SU26246RMFS7": "Облигации",
        "SU26247RMFS5": "Облигации",
        "SU26248RMFS3": "Облигации",
        "RU000A106UW3": "Облигации",
        "LQDT": "Фонд",
        "TDIV": "Фонд",
        "TGLD": "Фонд",
        "TGLD@": "Фонд",
        "VTBR": "Финансы",
        "X5": "Товары",
        "TPAY": "Фонд",
        "SU26254RMFS1": "Облигации",
        "NLMK": "Металл",
        "MGKL": "Финансы",
        "SIBN": "Нефтегаз",
        "GLRX": "Стройка",
        "AFLT": "Транспорт"
    }
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("INSERT OR REPLACE INTO sectors (ticker, sector_name) VALUES (?, ?)", (
            "SBER", "Финансы"
        ))
        await conn.commit()
        logging.info(f"✅ Сектора обновлены ({len(initial_sectors)} шт.)")

async def migrate_sectors_to_instruments():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT ticker, sector_name FROM sectors") as c:
            rows = await c.fetchall()
        
        insert_count = 0
        for ticker, sector in rows:
            await conn.execute("INSERT OR IGNORE INTO instruments (ticker, sector, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (ticker, sector))
            insert_count += 1
        await conn.commit()
        logging.info(f"✅ Перенесено {insert_count} секторов в instruments")

async def insert_operation(op: dict):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute('''INSERT OR REPLACE INTO operations
                                    (id, date, type, ticker, figi, instrument_type, quantity, payment, currency, commission, name)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (op.get('id'), op.get('date'), op.get('type'), op.get('ticker'),
                     op.get('figi'), op.get('instrument_type'), op.get('quantity'),
                     op.get('payment'), op.get('currency'), op.get('commission'),
                     op.get('name')))
        await conn.commit()

async def get_personal_dividends():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT date, ticker, payment FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date") as c:
            rows = await c.fetchall()
        result = []
        for r in rows:
            ticker = r[1]
            if ticker is None:
                ticker = "Прочие"
            elif ticker == "Прочие":
                name = "Прочие"
            else:
                name = NAME_OVERRIDES.get(ticker)
                if name is None:
                    name = ticker_to_name.get(ticker, ticker)
            result.append({"date": r[0], "ticker": name, "amount": r[2]})
        return result

async def get_last_dividends(limit=10):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT date, ticker, payment FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date DESC LIMIT ?", (limit,)) as c:
            rows = await c.fetchall()
        return [{"date": r[0], "ticker": r[1], "amount": r[2]} for r in rows]

async def get_last_operation_date():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT MAX(date) FROM operations") as c:
            row = await c.fetchone()
        return row[0] if row else None

async def _get_state(key: str) -> float | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT value FROM portfolio_state WHERE key = ?", (key,)) as c:
            row = await c.fetchone()
        return float(row[0]) if row else None

async def _set_state(key: str, value: float):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("INSERT OR REPLACE INTO portfolio_state (key, value) VALUES (?, ?)", (key, value))
        await conn.commit()

async def set_portfolio_value(value: float):
    await _set_state("last_total_value", value)

async def get_portfolio_value() -> float | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT value FROM portfolio_state WHERE key = ?", ("last_total_value",)) as c:
            row = await c.fetchone()
        return float(row[0]) if row else None

async def set_daily_snapshot(date_str: str, value: float):
    await _set_state(f"snapshot_{date_str}", value)

async def get_daily_snapshot(date_str: str) -> float | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT value FROM portfolio_state WHERE key = ?", (f"snapshot_{date_str}",)) as c:
            row = await c.fetchone()
        return float(row[0]) if row else None

async def load_name_overrides():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT ticker, display_name FROM name_overrides") as c:
            rows = await c.fetchall()
    NAME_OVERRIDES.clear()
    for ticker, display_name in rows:
        NAME_OVERRIDES[ticker] = display_name
    logging.info(f"✅ Загружено {len(NAME_OVERRIDES)} переопределений названий")

async def set_name_override(ticker: str, display_name: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("INSERT OR REPLACE INTO name_overrides (ticker, display_name) VALUES (?, ?)", (ticker, display_name))
        await conn.commit()
        await load_name_overrides()

async def remove_name_override(ticker: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("DELETE FROM name_overrides WHERE ticker = ?", (ticker,))
        await conn.commit()
        await load_name_overrides()

async def get_instrument(ticker: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT ticker, name, sector, figi, instrument_type, updated_at FROM instruments WHERE ticker = ?", (ticker,)) as c:
            row = await c.fetchone()
        if row:
            return {"ticker": row[0], "name": row[1], "sector": row[2], "figi": row[3], "instrument_type": row[4], "updated_at": row[5]}
        return None

async def get_all_instruments():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT ticker, name, sector, figi, instrument_type, updated_at FROM instruments") as c:
            rows = await c.fetchall()
        return [{"ticker": r[0], "name": r[1], "sector": r[2], "figi": r[3], "instrument_type": r[4], "updated_at": r[5]} for r in rows]

async def upsert_instrument(ticker: str, name: str = None, sector: str = None, figi: str = None,
                        instrument_type: str = None, maturity_date: str = None, coupon_period: int = None):
    """
    Вставляет или обновляет запись в таблице instruments.
    Если параметр не указан, оставляет существующее значение (если оно есть).
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT name, sector, figi, instrument_type, maturity_date, coupon_period FROM instruments WHERE ticker = ?", (ticker,)) as c:
            row = await c.fetchone()
            existing = {
                "name": row[0] if row else None,
                "sector": row[1] if row else None,
                "figi": row[2] if row else None,
                "instrument_type": row[3] if row else None,
                "maturity_date": row[4] if row else None,
                "coupon_period": row[5] if row else None
            }
        
        # Если сектор не указан, пробуем взять из старой таблицы sectors (для обратной совместимости)
        if sector is None:
            async with conn.execute("SELECT sector_name FROM sectors WHERE ticker = ?", (ticker,)) as c:
                row_sector = await c.fetchone()
                if row_sector:
                    sector = row_sector[0]
                else:
                    sector = existing["sector"] or "Прочие"
        
        # Если имя не указано, оставляем существующее или ставим тикер
        if name is None:
            name = existing["name"] or ticker
        
        # Если figi не указан, оставляем существующий
        if figi is None:
            figi = existing["figi"]
        
        # Если instrument_type не указан, оставляем существующий
        if instrument_type is None:
            instrument_type = existing["instrument_type"]
        
        # Если maturity_date не указана, оставляем существующую
        if maturity_date is None:
            maturity_date = existing["maturity_date"]
        
        # Если coupon_period не указан, оставляем существующий
        if coupon_period is None:
            coupon_period = existing["coupon_period"]
        
        # Выполняем INSERT OR REPLACE
        await conn.execute('''INSERT OR REPLACE INTO instruments 
                                (ticker, name, sector, figi, instrument_type, updated_at, maturity_date, coupon_period)
                                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)''',
                    (ticker, name, sector, figi, instrument_type, maturity_date, coupon_period))
        await conn.commit()

async def update_instrument_sector(ticker: str, new_sector: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("UPDATE instruments SET sector = ?, updated_at = CURRENT_TIMESTAMP WHERE ticker = ?", (new_sector, ticker))
        async with conn.execute("INSERT OR REPLACE INTO sectors (ticker, sector_name) VALUES (?, ?)", (ticker, new_sector))
        await conn.commit()

async def upsert_dividend_calendar(ticker: str, figi: str, dividend_data: dict):
    # Извлекаем сумму дивиденда (units + nano)
    dividend_net_obj = dividend_data.get("dividendNet", {})
    units = float(dividend_net_obj.get("units", 0))
    nano = float(dividend_net_obj.get("nano", 0)) / 1e9
    dividend_net = units + nano
    
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute('''INSERT OR REPLACE INTO dividend_calendar 
                                    (ticker, figi, declared_date, record_date, payment_date, dividend_net, dividend_type)
                                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (
                        ticker,
                        figi,
                        dividend_data.get("declaredDate"),
                        dividend_data.get("recordDate"),
                        dividend_data.get("paymentDate"),
                        dividend_net,
                        dividend_data.get("dividendType")
                    ))
        await conn.commit()

async def get_dividend_calendar(ticker=None, year=None, month=None):
    """Получить календарь дивидендов с фильтрацией."""
    query = "SELECT ticker, figi, declared_date, record_date, payment_date, dividend_net, dividend_type FROM dividend_calendar"
    params = []
    conditions = []
    if ticker:
        conditions.append("ticker = ?")
        params.append(ticker)
    if year and month:
        conditions.append("strftime('%Y', payment_date) = ? AND strftime('%m', payment_date) = ?")
        params.extend([str(year), f"{month:02d}"])
    elif year:
        conditions.append("strftime('%Y', payment_date) = ?")
        params.append(str(year))
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY payment_date DESC"
    
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(query, params) as c:
            rows = await c.fetchall()
        return [{
            "ticker": r[0],
            "figi": r[1],
            "declared_date": r[2],
            "record_date": r[3],
            "payment_date": r[4],
            "dividend_net": r[5],
            "dividend_type": r[6]
        } for r in rows]

async def upsert_coupon_calendar(ticker: str, figi: str, coupon_data: dict):
    pay_obj = coupon_data.get("payOneBond", {})
    units = float(pay_obj.get("units", 0))
    nano = float(pay_obj.get("nano", 0)) / 1e9
    coupon_value = units + nano
    
    is_redemption = coupon_data.get("is_redemption", False)
    
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute('''INSERT OR REPLACE INTO coupon_calendar 
                                    (ticker, figi, coupon_date, coupon_value, coupon_currency, record_date, is_redemption)
                                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (
                        ticker,
                        figi,
                        coupon_data.get("couponDate"),
                        coupon_value,
                        coupon_data.get("currency", "RUB"),
                        coupon_data.get("fixDate"),
                        1 if is_redemption else 0
                    ))
        await conn.commit()

async def get_coupon_calendar(ticker=None, year=None, month=None):
    """Получить календарь купонов с фильтрацией."""
    query = "SELECT ticker, figi, coupon_date, coupon_value, coupon_currency, record_date FROM coupon_calendar"
    params = []
    conditions = []
    if ticker:
        conditions.append("ticker = ?")
        params.append(ticker)
    if year and month:
        conditions.append("strftime('%Y', coupon_date) = ? AND strftime('%m', coupon_date) = ?")
        params.extend([str(year), f"{month:02d}"])
    elif year:
        conditions.append("strftime('%Y', coupon_date) = ?")
        params.append(str(year))
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY coupon_date DESC"
    
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(query, params) as c:
            rows = await c.fetchall()
        return [{
            "ticker": r[0],
            "figi": r[1],
            "coupon_date": r[2],
            "coupon_value": r[3],
            "coupon_currency": r[4],
            "record_date": r[5]
        } for r in rows]

async def get_sector(ticker: str) -> str:
    inst = await get_instrument(ticker)
    if inst and inst['sector']:
        return inst['sector']
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT sector_name FROM sectors WHERE ticker = ?", (ticker,)) as c:
            row = await c.fetchone()
    return row[0] if row else "Прочие"

async def get_dividend_forecast(ticker: str = None):
    """Получить прогноз для одного или всех тикеров."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT ticker, forecast_amount, forecast_month, forecast_year, confidence_score, method, updated_at FROM dividend_forecast") as c:
            rows = await c.fetchall()
        if ticker:
            async with conn.execute("SELECT ticker, forecast_amount, forecast_month, forecast_year, confidence_score, method, updated_at FROM dividend_forecast WHERE ticker = ?", (ticker,)) as c:
                row = await c.fetchone()
                if row:
                    return {"ticker": row[0], "amount": row[1], "month": row[2], "year": row[3], "confidence": row[4], "method": row[5], "updated_at": row[6]}
            return None
        else:
            return [{"ticker": r[0], "amount": r[1], "month": r[2], "year": r[3], "confidence": r[4], "method": r[5], "updated_at": r[6]} for r in rows]

async def set_last_sync_time(timestamp: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("INSERT OR REPLACE INTO portfolio_state (key, value) VALUES (?, ?)", ("last_sync_time", timestamp))
        await conn.commit()

async def get_last_sync_time() -> str | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT value FROM portfolio_state WHERE key = ?", ("last_sync_time",)) as c:
            row = await c.fetchone()
        return row[0] if row else None
</write_to_file>
<task_progress>
- [x] Рефакторинг БД для Асинхронности (использование aiosqlite)
</task_progress>
</write_to_file>