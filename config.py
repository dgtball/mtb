import os

# ---------- ВЕРСИЯ ----------
VERSION = "3.5.1"
# ---------- ТОКЕНЫ ----------
API_TOKEN = os.getenv("BOT_TOKEN")
TINKOFF_TOKEN = os.getenv("TITN")
MINI_APP_SECRET = os.getenv("MINI_APP_SECRET", "fallback_default")
MY_CHAT_ID = os.getenv("MY_CHAT_ID")
if not MY_CHAT_ID:
    raise ValueError("MY_CHAT_ID не задан")
try:
    MY_CHAT_ID = int(MY_CHAT_ID)
except ValueError:
    raise ValueError("MY_CHAT_ID должен быть числом")
DOMAIN = os.getenv("DOMAIN")
if not DOMAIN:
    raise ValueError("DOMAIN не задан")
# Формируем webhook_url автоматически
WEBHOOK_URL = f"{DOMAIN}/webhook"

# ---------- ПАРАМЕТРЫ ----------
TOP_N = 10
DATA_DIR = os.getenv('DATA_DIR', '/app/data')
DB_PATH = os.path.join(DATA_DIR, 'favorites.db')
PORT = int(os.getenv('PORT', 3000))
TINKOFF_API_URL = os.getenv("TINKOFF_API_URL", "https://invest-public-api.tbank.ru/rest/")

# ---------- ПЕРЕОПРЕДЕЛЕНИЕ НАЗВАНИЙ (загружается из БД при старте) ----------
NAME_OVERRIDES = {}  # будет заполнено через db.load_name_overrides()

# ---------- СПИСОК НЕТОРГОВЫХ ВЫХОДНЫХ 2026 ----------
NO_TRADING_WEEKENDS_2026 = [
    ("2026-01-03", "2026-01-04"),
    ("2026-01-10", "2026-01-11"),
    ("2026-02-14", "2026-02-15"),
    ("2026-03-07", "2026-03-08"),
    ("2026-03-21", "2026-03-22"),
    ("2026-05-09", "2026-05-10"),
    ("2026-06-20", "2026-06-21"),
    ("2026-08-01", "2026-08-02"),
    ("2026-08-15", "2026-08-16"),
    ("2026-09-12", "2026-09-13"),
    ("2026-10-24", "2026-10-25"),
    ("2026-12-05", "2026-12-06"),
]

# Глобальный словарь имён инструментов из MOEX (загружается в runtime)
ticker_to_name = {}
ticker_to_sector = {}

# ---------- СПРАВОЧНИК СЕКТОРОВ MOEX ----------
SECTOR_NAMES = {
    "1": "Нефть и газ",
    "2": "Металлы и добыча",
    "3": "Химия и нефтехимия",
    "4": "Электроэнергетика",
    "5": "Машиностроение",
    "6": "Транспорт",
    "7": "Связь и телекоммуникации",
    "8": "Торговля и потребительские товары",
    "9": "Финансы и банки",
    "10": "Информационные технологии",
    "11": "Строительство и недвижимость",
    "12": "Здравоохранение",
    "13": "Прочие",
}