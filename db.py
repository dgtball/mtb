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
    initial = [
        # Базовые переопределения
        ("MDMG-ао", "Мать и Дитя"),
        ("iАстра ао", "Астра"),
        ("iСофтлайн", "Софтлайн"),
        ('МКПАО "ВК"', "ВК"),
        ("iВУШХолдинг", "ВУШ"),
        ("iКаршеринг", "Делимобиль"),
        ("СевСт-ао", "Северсталь"),
        ("Роснефть", "Роснефть"),
        ("Газпотреб", "Газпром нефть"),
        ("ГАЗПРОМ ао", "Газпром"),
        ("Ростел -ао", "Ростелеком"),
        ("Т-Техно ао", "Т-Технологии"),
        ("КЦ ИКС 5", "X5"),
        ("Самолет ао", "Самолет"),
        # Дополнительные переименования
        ("Татнфт 3ао", "Татнефть"),
        ("Татнфт 3ап", "Татнефть п."),
        ("Татнфт Зап", "Татнефть п."),  # если встречается старый вариант
        ("Татнфт Зао", "Татнефть"),     # аналогично
        ("Сургнфгз-п", "Сургутнефтегаз п."),
        ("Сургнфгз", "Сургутнефтегаз"),
        ("Новатэк ао", "Новатэк"),
        ("Магнит ао", "Магнит"),
        ("АЛРОСА ао", "АЛРОСА"),
        ("Полюс", "Полюс"),
        ("ЯНДЕКС", "Яндекс"),
        ("OZON", "Ozon"),
        ("ФСК ЕЭС ао", "ФСК"),
        #("МКБ ао", "МКБ"),
        ("РусГидро", "РусГидро"),
        ("МосБиржа ао", "МосБиржа"),
        ("Сбербанк", "Сбер"),
        ("iПРОМОМЕД", "ПРОМОМЕД"),
        ("iПозитив", "Позитив"),
        ("iАренадата", "Аренадата"),
        ("iПРОМОМЕД", "ПРОМОМЕД"),
    ]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM name_overrides")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT OR IGNORE INTO name_overrides (ticker, display_name) VALUES (?, ?)", initial)
        conn.commit()
        logging.info("✅ Начальные переопределения названий добавлены в БД")
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
    """Сохраняет последнюю известную стоимость портфеля."""
    _set_state("last_total_value", value)

def get_portfolio_value() -> float | None:
    """Возвращает последнюю сохранённую стоимость портфеля."""
    return _get_state("last_total_value")

def set_daily_snapshot(date_str: str, value: float):
    """Сохраняет снэпшот портфеля на указанную дату (YYYY-MM-DD)."""
    _set_state(f"snapshot_{date_str}", value)

def get_daily_snapshot(date_str: str) -> float | None:
    """Возвращает снэпшот портфеля на дату или None."""
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