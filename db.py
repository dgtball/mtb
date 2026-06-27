"""
db.py — работа с SQLite (единое соединение, параметризованные запросы)
"""

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import List, Dict, Any, Optional, Union

DB_PATH = "data/bot.db"   # путь к файлу БД

# Глобальное соединение (создаётся один раз)
_conn = None

def get_conn() -> sqlite3.Connection:
    """
    Возвращает глобальное соединение с БД.
    Если соединение отсутствует или закрыто — пересоздаёт.
    """
    global _conn
    if _conn is None:
        try:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as e:
            logging.error(f"Ошибка подключения к БД: {e}")
            raise
    # Проверяем живо ли соединение
    try:
        _conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        # Соединение закрыто, открываем заново
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
    return _conn

def close_db() -> None:
    """Закрывает глобальное соединение с БД."""
    global _conn
    if _conn:
        _conn.close()
        _conn = None

@contextmanager
def transaction():
    """Контекстный менеджер для транзакций."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Транзакция откачена: {e}")
        raise

# -------------------- Инициализация таблиц --------------------
def init_db() -> None:
    """Создаёт все необходимые таблицы и начальные данные, если их нет."""
    conn = get_conn()
    with transaction():
        # Таблица для кеша инструментов (MOEX)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS instruments_cache (
                ticker TEXT PRIMARY KEY,
                name TEXT,
                sector TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Таблица для маппинга (переименования, figi)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS instrument_mapping (
                ticker TEXT PRIMARY KEY,
                custom_name TEXT,
                figi TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Сектора
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE
            )
        """)
        # Портфель
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                ticker TEXT PRIMARY KEY,
                sector_id INTEGER,
                quantity REAL,
                avg_price REAL,
                last_snapshot_price REAL,
                FOREIGN KEY(sector_id) REFERENCES sectors(id)
            )
        """)
        # Операции (дивиденды, купоны, сделки, пополнения)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                figi TEXT,
                operation_type TEXT,
                quantity REAL,
                price REAL,
                date TIMESTAMP,
                payment REAL,
                FOREIGN KEY(ticker) REFERENCES portfolio(ticker)
            )
        """)
        # Снэпшоты (ежедневные цены)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                price REAL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(ticker) REFERENCES portfolio(ticker)
            )
        """)
        # Мета-информация (ключ-значение)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Индексы для ускорения
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operations_date ON operations(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operations_ticker ON operations(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_ticker ON snapshots(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON snapshots(timestamp)")

    # Добавляем начальные сектора, если их нет
    sectors = conn.execute("SELECT name FROM sectors").fetchall()
    if not sectors:
        default_sectors = ['Акции', 'Облигации', 'ETF', 'Деньги', 'Прочее']
        conn.executemany("INSERT OR IGNORE INTO sectors (name) VALUES (?)", [(s,) for s in default_sectors])
        conn.commit()

# -------------------- Работа с кешем инструментов --------------------
def update_instruments_cache(instruments: List[Dict[str, str]]) -> None:
    """
    Обновляет кеш инструментов (тикер, название, сектор).
    :param instruments: список словарей с ключами ticker, name, sector
    """
    conn = get_conn()
    with transaction():
        for inst in instruments:
            conn.execute(
                "INSERT OR REPLACE INTO instruments_cache (ticker, name, sector, last_updated) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (inst['ticker'], inst['name'], inst['sector'])
            )

def get_all_instruments() -> List[Dict[str, Any]]:
    """Возвращает все инструменты из кеша."""
    conn = get_conn()
    rows = conn.execute("SELECT ticker, name, sector FROM instruments_cache").fetchall()
    return [dict(row) for row in rows]

def get_instrument(ticker: str) -> Optional[Dict[str, Any]]:
    """Возвращает данные одного инструмента."""
    conn = get_conn()
    row = conn.execute("SELECT ticker, name, sector FROM instruments_cache WHERE ticker = ?", (ticker,)).fetchone()
    return dict(row) if row else None

def get_instruments_by_sector(sector: str) -> List[Dict[str, Any]]:
    """Возвращает инструменты по сектору."""
    conn = get_conn()
    rows = conn.execute("SELECT ticker, name FROM instruments_cache WHERE sector = ?", (sector,)).fetchall()
    return [dict(row) for row in rows]

# -------------------- Маппинг (переименования, figi) --------------------
def set_instrument_mapping(ticker: str, custom_name: Optional[str] = None, figi: Optional[str] = None) -> None:
    """
    Сохраняет кастомное имя или figi для тикера.
    Если оба параметра None — удаляет запись.
    """
    conn = get_conn()
    with transaction():
        if custom_name is None and figi is None:
            conn.execute("DELETE FROM instrument_mapping WHERE ticker = ?", (ticker,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO instrument_mapping (ticker, custom_name, figi, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (ticker, custom_name, figi)
            )

def get_instrument_mapping(ticker: Optional[str] = None) -> Union[List[Dict], Optional[Dict]]:
    """Возвращает все маппинги или один по тикеру."""
    conn = get_conn()
    if ticker:
        row = conn.execute("SELECT ticker, custom_name, figi FROM instrument_mapping WHERE ticker = ?", (ticker,)).fetchone()
        return dict(row) if row else None
    else:
        rows = conn.execute("SELECT ticker, custom_name, figi FROM instrument_mapping").fetchall()
        return [dict(row) for row in rows]

def get_custom_name(ticker: str) -> Optional[str]:
    """Возвращает кастомное имя для тикера, если есть."""
    row = get_instrument_mapping(ticker)
    return row['custom_name'] if row else None

def get_figi(ticker: str) -> Optional[str]:
    """Возвращает figi для тикера."""
    row = get_instrument_mapping(ticker)
    return row['figi'] if row else None

# -------------------- Сектора --------------------
def get_or_create_sector(sector_name: str) -> int:
    """Возвращает ID сектора, создаёт новый, если не существует."""
    conn = get_conn()
    row = conn.execute("SELECT id FROM sectors WHERE name = ?", (sector_name,)).fetchone()
    if row:
        return row['id']
    with transaction():
        conn.execute("INSERT INTO sectors (name) VALUES (?)", (sector_name,))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

def get_all_sectors() -> List[Dict[str, Any]]:
    """Возвращает все сектора."""
    conn = get_conn()
    rows = conn.execute("SELECT id, name FROM sectors ORDER BY name").fetchall()
    return [dict(row) for row in rows]

# -------------------- Портфель --------------------
def update_portfolio(ticker: str, sector_id: int, quantity: float, avg_price: float, snapshot_price: Optional[float] = None) -> None:
    """Обновляет или вставляет запись в портфель."""
    conn = get_conn()
    with transaction():
        conn.execute(
            "INSERT OR REPLACE INTO portfolio (ticker, sector_id, quantity, avg_price, last_snapshot_price) VALUES (?, ?, ?, ?, ?)",
            (ticker, sector_id, quantity, avg_price, snapshot_price)
        )

def get_portfolio() -> List[Dict[str, Any]]:
    """Возвращает полный портфель с названиями секторов и кастомными именами."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT p.ticker, p.quantity, p.avg_price, p.last_snapshot_price,
               s.name as sector_name, p.sector_id,
               m.custom_name
        FROM portfolio p
        LEFT JOIN sectors s ON p.sector_id = s.id
        LEFT JOIN instrument_mapping m ON p.ticker = m.ticker
    """).fetchall()
    return [dict(row) for row in rows]

def get_portfolio_tickers() -> List[str]:
    """Возвращает список тикеров в портфеле."""
    conn = get_conn()
    return [row['ticker'] for row in conn.execute("SELECT ticker FROM portfolio")]

def get_portfolio_value() -> float:
    """Суммарная стоимость портфеля по последним снэпшотам (если есть) или по средней цене."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT ticker, quantity, avg_price, last_snapshot_price
        FROM portfolio
    """).fetchall()
    total = 0.0
    for row in rows:
        price = row['last_snapshot_price'] if row['last_snapshot_price'] is not None else row['avg_price']
        total += row['quantity'] * price
    return total

# -------------------- Операции --------------------
def add_operation(ticker: str, figi: str, op_type: str, quantity: float, price: float, date: str, payment: Optional[float] = None) -> None:
    """Добавляет операцию (дивиденд, купон, сделка, пополнение)."""
    conn = get_conn()
    with transaction():
        conn.execute(
            "INSERT INTO operations (ticker, figi, operation_type, quantity, price, date, payment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticker, figi, op_type, quantity, price, date, payment)
        )

def get_operations_by_ticker(ticker: str) -> List[Dict[str, Any]]:
    """Все операции по тикеру, сортировка по дате (новые сверху)."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM operations WHERE ticker = ? ORDER BY date DESC", (ticker,)).fetchall()
    return [dict(row) for row in rows]

def get_operations_by_year(year: int, op_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Операции за год, опционально по типу (dividend, coupon, trade, deposit)."""
    conn = get_conn()
    query = "SELECT * FROM operations WHERE strftime('%Y', date) = ?"
    params = [str(year)]
    if op_type:
        query += " AND operation_type = ?"
        params.append(op_type)
    query += " ORDER BY date DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]

def get_operations_by_ticker_and_year(ticker: str, year: int) -> List[Dict[str, Any]]:
    """Операции по конкретному тикеру и году."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM operations WHERE ticker = ? AND strftime('%Y', date) = ? ORDER BY date DESC",
        (ticker, str(year))
    ).fetchall()
    return [dict(row) for row in rows]

def get_operations_sum_by_year_and_type(year: int, op_type: str) -> float:
    """Суммарная сумма выплат (payment) по году и типу операции."""
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(payment), 0) as total FROM operations WHERE strftime('%Y', date) = ? AND operation_type = ?",
        (str(year), op_type)
    ).fetchone()
    return row['total'] if row else 0.0

# -------------------- Снэпшоты --------------------
def add_snapshot(ticker: str, price: float) -> None:
    """Сохраняет снэпшот цены для тикера."""
    conn = get_conn()
    with transaction():
        conn.execute("INSERT INTO snapshots (ticker, price, timestamp) VALUES (?, ?, CURRENT_TIMESTAMP)", (ticker, price))

def get_latest_snapshot(ticker: str) -> Optional[Dict[str, Any]]:
    """Последний снэпшот для тикера."""
    conn = get_conn()
    row = conn.execute(
        "SELECT price, timestamp FROM snapshots WHERE ticker = ? ORDER BY timestamp DESC LIMIT 1",
        (ticker,)
    ).fetchone()
    return dict(row) if row else None

def get_snapshots_for_day(date: str) -> List[Dict[str, Any]]:
    """Все снэпшоты за конкретную дату (в формате YYYY-MM-DD)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT ticker, price, timestamp FROM snapshots WHERE date(timestamp) = date(?) ORDER BY ticker",
        (date,)
    ).fetchall()
    return [dict(row) for row in rows]

def update_portfolio_snapshot_prices(snapshots: Dict[str, float]) -> None:
    """Обновляет last_snapshot_price в портфеле на основе переданного словаря {ticker: price}."""
    conn = get_conn()
    with transaction():
        for ticker, price in snapshots.items():
            conn.execute("UPDATE portfolio SET last_snapshot_price = ? WHERE ticker = ?", (price, ticker))

# -------------------- Мета-информация --------------------
def get_meta(key: str) -> Optional[str]:
    """Получить значение мета-ключа."""
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row['value'] if row else None

def set_meta(key: str, value: str) -> None:
    """Установить значение мета-ключа."""
    conn = get_conn()
    with transaction():
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

# -------------------- Дополнительные удобные функции --------------------
def clear_instruments_cache() -> None:
    """Очищает кеш инструментов (используется при полной перезагрузке)."""
    conn = get_conn()
    with transaction():
        conn.execute("DELETE FROM instruments_cache")
