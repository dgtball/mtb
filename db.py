import sqlite3
import logging
from config import DB_PATH

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favorites
                 (chat_id INTEGER, ticker TEXT, 
                  PRIMARY KEY (chat_id, ticker))''')
    conn.commit()
    conn.close()
    logging.info(f"✅ База данных инициализирована: {DB_PATH}")

def add_favorite(chat_id: int, ticker: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO favorites (chat_id, ticker) VALUES (?, ?)", (chat_id, ticker.upper()))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        logging.error(f"Ошибка добавления в SQLite: {e}")
        return False

def remove_favorite(chat_id: int, ticker: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM favorites WHERE chat_id = ? AND ticker = ?", (chat_id, ticker.upper()))
        conn.commit()
        deleted = c.rowcount > 0
        conn.close()
        return deleted
    except Exception as e:
        logging.error(f"Ошибка удаления из SQLite: {e}")
        return False

def get_favorites(chat_id: int) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT ticker FROM favorites WHERE chat_id = ?", (chat_id,))
        rows = c.fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception as e:
        logging.error(f"Ошибка получения избранного из SQLite: {e}")
        return []
