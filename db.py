import sqlite3
import logging
from config import DB_PATH, NAME_OVERRIDES

# ---------- ИНИЦИАЛИЗАЦИЯ ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS name_overrides
                 (ticker TEXT PRIMARY KEY, display_name TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio_state
                 (key TEXT PRIMARY KEY, value REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS sectors
                 (ticker TEXT PRIMARY KEY, sector_name TEXT)''')
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
    conn.commit()
    conn.close()
    logging.info(f"✅ База данных инициализирована: {DB_PATH}")
    seed_overrides()
    seed_sectors()

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

# ---------- СЕКТОРА ----------
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
    # Используем русские названия типов
    c.execute("SELECT COUNT(*) FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB'")
    count = c.fetchone()[0]
    logging.info(f"get_personal_dividends: найдено {count} выплат в БД")
    c.execute("SELECT date, ticker, payment FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date")
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "ticker": r[1], "amount": r[2]} for r in rows]
    
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

# ---------- СЕКТОРА (дополнительные функции) ----------
def get_sector(ticker: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT sector_name FROM sectors WHERE ticker = ?", (ticker,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "Прочие"