import sqlite3
import logging
from config import DB_PATH, NAME_OVERRIDES

# ---------- ИНИЦИАЛИЗАЦИЯ И МИГРАЦИЯ ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Удаляем старую таблицу избранного (если была)
    c.execute("DROP TABLE IF EXISTS favorites")
    # Таблица переопределений названий
    c.execute('''CREATE TABLE IF NOT EXISTS name_overrides
                 (ticker TEXT PRIMARY KEY, display_name TEXT)''')
    # Таблица состояния портфеля
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio_state
                 (key TEXT PRIMARY KEY, value REAL)''')
    conn.commit()
    conn.close()
    logging.info(f"✅ База данных инициализирована: {DB_PATH}")
    seed_overrides()

def seed_overrides():
    """Однократно добавляет стартовые переопределения, удаляя устаревшие."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Удаляем старые записи, которые могли вызвать конфликт
    c.execute("DELETE FROM name_overrides WHERE ticker IN ('iКаршеринг', 'Каршеринг', 'Делимобиль')")
    c.execute("DELETE FROM name_overrides WHERE ticker = 'DELI' AND display_name != 'Делимобиль'")
    initial = [
        # Тикеры акций
        ("WUSH", "ВУШ"),
        ("DELI", "Делимобиль"),
        ("X5", "X5"),
        ("FLOT", "Совкомфлот"),
        ("SNGSP", "СургутНГ-п"),
        ("SNGS", "СургутНГ"),
        ("ENPG", "ЭН+"),
        ("MDMG", "Мать и Дитя"),
        ("TRNFP", "Транснефть-п"),
        ("T", "Т-Технологии"),
        ("GAZP", "Газпром"),
        ("SIBN", "Газпромнефть"),
        ("VKCO", "ВК"),
        ("TATNP", "Татнефть п"),
        ("TATN", "Татнефть"),
        ("CHMF", "Северсталь"),
        ("ETLN", "Эталон"),
        ("RNFT", "Руснефть"),
        ("RTKM", "Ростелеком"),
        ("MSRS", "РСетиМСК"),
        ("MRKV", "РСетиВолга"),
        ("MRKC", "РСетиЦентр"),
        ("GEMC", "ЕМЦ"),
        ("AFKS", "АФК Система"),
        # Облигации (по желанию)
        ("SU26247RMFS5", "ОФЗ 26247"),
        ("SU26248RMFS3", "ОФЗ 26248"),
        ("SU26246RMFS7", "ОФЗ 26246"),
        ("SU26254RMFS1", "ОФЗ 26254"),
        ("SU26233RMFS5", "ОФЗ 26233"),
        ("SU26245RMFS9", "ОФЗ 26245"),
        ("SU26240RMFS0", "ОФЗ 26240"),
        ("SU26238RMFS4", "ОФЗ 26238"),
        ("RU000A106UW3", "Дели P3"),
        # Фонды
        ("LQDT", "ВИМ Ликвидность"),
        ("TGLD@", "TGLD"),
    ]

    c.execute("SELECT COUNT(*) FROM name_overrides")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT OR IGNORE INTO name_overrides (ticker, display_name) VALUES (?, ?)", initial)
        conn.commit()
        logging.info("✅ Начальные переопределения названий добавлены в БД")
    else:
        # Если таблица не пуста, всё равно добавляем отсутствующие ключи
        for ticker, display_name in initial:
            c.execute("INSERT OR IGNORE INTO name_overrides (ticker, display_name) VALUES (?, ?)", (ticker, display_name))
        conn.commit()
        logging.info("✅ Новые переопределения добавлены в БД")

    conn.close()

# ---------- НОВЫЕ ФУНКЦИИ СОСТОЯНИЯ ПОРТФЕЛЯ ----------
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