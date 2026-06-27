import sqlite3
import logging
from config import DB_PATH, NAME_OVERRIDES, ticker_to_name

# ---------- ИНИЦИАЛИЗАЦИЯ ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Таблица переопределений названий
    c.execute('''CREATE TABLE IF NOT EXISTS name_overrides
                 (ticker TEXT PRIMARY KEY, display_name TEXT)''')
    
    # Таблица состояния портфеля (снапшоты, текущая стоимость)
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio_state
                 (key TEXT PRIMARY KEY, value REAL)''')
    
    # Старая таблица секторов (для обратной совместимости)
    c.execute('''CREATE TABLE IF NOT EXISTS sectors
                 (ticker TEXT PRIMARY KEY, sector_name TEXT)''')
    
    # Таблица операций (выплаты, сделки)
    c.execute('''CREATE TABLE IF NOT EXISTS operations
                 (id TEXT PRIMARY KEY,
                  date TEXT NOT NULL,
                  type TEXT NOT NULL,
                  ticker TEXT,
                  figi TEXT,
                  instrument_type TEXT,
                  quantity INTEGER,
                  payment REAL,
                  currency TEXT,
                  commission REAL,
                  name TEXT)''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_operations_date ON operations(date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_operations_type ON operations(type)')
    
    # Новая таблица инструментов (кэш MOEX)
    c.execute('''CREATE TABLE IF NOT EXISTS instruments (
        ticker TEXT PRIMARY KEY,
        name TEXT,
        sector TEXT,
        figi TEXT,
        instrument_type TEXT,
        updated_at TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_instruments_figi ON instruments(figi)')
    
    conn.commit()
    conn.close()
    logging.info(f"✅ База данных инициализирована: {DB_PATH}")
    
    seed_overrides()
    seed_sectors()
    migrate_sectors_to_instruments()

# ---------- НАЧАЛЬНЫЕ ПЕРЕОПРЕДЕЛЕНИЯ ----------
def seed_overrides():
    initial = [
        ("WUSH", "ВУШ"),
        ("DELI", "Делимобиль"),
        # ... (ваш текущий список)
    ]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM name_overrides")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT OR IGNORE INTO name_overrides (ticker, display_name) VALUES (?, ?)", initial)
        conn.commit()
        logging.info("✅ Начальные переопределения названий добавлены в БД")
    else:
        for ticker, display_name in initial:
            c.execute("INSERT OR IGNORE INTO name_overrides (ticker, display_name) VALUES (?, ?)", (ticker, display_name))
        conn.commit()
        logging.info("✅ Новые переопределения добавлены в БД")
    conn.close()

# ---------- СЕКТОРА (старая таблица, для обратной совместимости) ----------
def seed_sectors():
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
        "AFLT": "Транспорт",
    }
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    inserted = 0
    for ticker, sector in initial_sectors.items():
        c.execute("INSERT OR REPLACE INTO sectors (ticker, sector_name) VALUES (?, ?)", (ticker, sector))
        inserted += 1
    conn.commit()
    conn.close()
    logging.info(f"✅ Сектора обновлены ({inserted} шт.)")

# ---------- МИГРАЦИЯ СТАРЫХ СЕКТОРОВ В НОВУЮ ТАБЛИЦУ ----------
def migrate_sectors_to_instruments():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Переносим все записи из sectors в instruments (если их там ещё нет)
    c.execute("SELECT ticker, sector_name FROM sectors")
    rows = c.fetchall()
    for ticker, sector in rows:
        c.execute("INSERT OR IGNORE INTO instruments (ticker, sector, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (ticker, sector))
    conn.commit()
    conn.close()
    logging.info(f"✅ Перенесено {len(rows)} секторов в instruments")

# ---------- ОПЕРАЦИИ ----------
def insert_operation(op):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO operations
                 (id, date, type, ticker, figi, instrument_type, quantity, payment, currency, commission, name)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (op.get('id'), op.get('date'), op.get('type'), op.get('ticker'),
               op.get('figi'), op.get('instrument_type'), op.get('quantity'),
               op.get('payment'), op.get('currency'), op.get('commission'),
               op.get('name')))
    conn.commit()
    conn.close()

def get_personal_dividends():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT date, ticker, payment FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date")
    rows = c.fetchall()
    conn.close()
    result = []
    for r in rows:
        ticker = r[1]
        if ticker == "Прочие":
            name = "Прочие"
        else:
            # Используем переопределение, если есть, иначе — название из MOEX
            name = NAME_OVERRIDES.get(ticker)
            if name is None:
                name = ticker_to_name.get(ticker, ticker)
        result.append({"date": r[0], "ticker": name, "amount": r[2]})
    return result
    
def get_last_dividends(limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT date, ticker, payment FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "ticker": r[1], "amount": r[2]} for r in rows]

def get_last_operation_date():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT MAX(date) FROM operations")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# ---------- СОСТОЯНИЕ ПОРТФЕЛЯ ----------
def _get_state(key: str) -> float | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM portfolio_state WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return float(row[0]) if row else None

def _set_state(key: str, value: float):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO portfolio_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def set_portfolio_value(value: float):
    _set_state("last_total_value", value)

def get_portfolio_value() -> float | None:
    return _get_state("last_total_value")

def set_daily_snapshot(date_str: str, value: float):
    _set_state(f"snapshot_{date_str}", value)

def get_daily_snapshot(date_str: str) -> float | None:
    return _get_state(f"snapshot_{date_str}")

# ---------- УПРАВЛЕНИЕ ПЕРЕОПРЕДЕЛЕНИЯМИ ----------
def load_name_overrides():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ticker, display_name FROM name_overrides")
    rows = c.fetchall()
    conn.close()
    NAME_OVERRIDES.clear()
    for ticker, display_name in rows:
        NAME_OVERRIDES[ticker] = display_name
    logging.info(f"✅ Загружено {len(NAME_OVERRIDES)} переопределений названий")

def set_name_override(ticker: str, display_name: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO name_overrides (ticker, display_name) VALUES (?, ?)", (ticker, display_name))
    conn.commit()
    conn.close()
    load_name_overrides()

def remove_name_override(ticker: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM name_overrides WHERE ticker = ?", (ticker,))
    conn.commit()
    conn.close()
    load_name_overrides()

# ---------- НОВАЯ ТАБЛИЦА INSTRUMENTS ----------
def get_instrument(ticker: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ticker, name, sector, figi, instrument_type, updated_at FROM instruments WHERE ticker = ?", (ticker,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"ticker": row[0], "name": row[1], "sector": row[2], "figi": row[3], "instrument_type": row[4], "updated_at": row[5]}
    return None

def get_all_instruments():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ticker, name, sector, figi, instrument_type, updated_at FROM instruments")
    rows = c.fetchall()
    conn.close()
    return [{"ticker": r[0], "name": r[1], "sector": r[2], "figi": r[3], "instrument_type": r[4], "updated_at": r[5]} for r in rows]

def upsert_instrument(ticker: str, name: str = None, sector: str = None, figi: str = None, instrument_type: str = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Если сектор не указан, пытаемся взять из старой таблицы sectors
    if sector is None:
        c.execute("SELECT sector_name FROM sectors WHERE ticker = ?", (ticker,))
        row = c.fetchone()
        if row:
            sector = row[0]
        else:
            sector = "Прочие"
    # Если имя не указано, оставляем существующее
    if name is None:
        c.execute("SELECT name FROM instruments WHERE ticker = ?", (ticker,))
        row = c.fetchone()
        if row:
            name = row[0]
    # Если figi не указан, оставляем существующий
    if figi is None:
        c.execute("SELECT figi FROM instruments WHERE ticker = ?", (ticker,))
        row = c.fetchone()
        if row:
            figi = row[0]
    # Если instrument_type не указан, оставляем существующий
    if instrument_type is None:
        c.execute("SELECT instrument_type FROM instruments WHERE ticker = ?", (ticker,))
        row = c.fetchone()
        if row:
            instrument_type = row[0]

    c.execute('''INSERT OR REPLACE INTO instruments (ticker, name, sector, figi, instrument_type, updated_at)
                 VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)''',
              (ticker, name, sector, figi, instrument_type))
    conn.commit()
    conn.close()

def update_instrument_sector(ticker: str, new_sector: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE instruments SET sector = ?, updated_at = CURRENT_TIMESTAMP WHERE ticker = ?", (new_sector, ticker))
    conn.commit()
    conn.close()
    # также обновляем в старой таблице sectors для совместимости
    c.execute("INSERT OR REPLACE INTO sectors (ticker, sector_name) VALUES (?, ?)", (ticker, new_sector))
    conn.commit()
    conn.close()

# ---------- СЕКТОРА (обновленная функция) ----------
def get_sector(ticker: str) -> str:
    inst = get_instrument(ticker)
    if inst and inst['sector']:
        return inst['sector']
    # fallback на старую таблицу sectors
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT sector_name FROM sectors WHERE ticker = ?", (ticker,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "Прочие"