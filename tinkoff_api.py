import logging
import datetime
import aiohttp
import db
import sqlite3

from config import TINKOFF_TOKEN, TINKOFF_API_URL, NAME_OVERRIDES, ticker_to_name
from utils import retry

# Глобальный словарь FIGI → Ticker, заполняется при старте
portfolio_figi_to_ticker = {}

@retry(max_attempts=3, delay=2, backoff=2)
async def tinkoff_api_request(http_session, method: str, endpoint: str, params: dict = None) -> dict:
    if not TINKOFF_TOKEN:
        raise ValueError("Токен TITN не задан")
    url = f"{TINKOFF_API_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {TINKOFF_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    async with http_session.request(method, url, headers=headers, json=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            text = await resp.text()
            logging.error(f"Ошибка API: статус {resp.status}, тело: {text[:500]}")
            raise Exception(f"API вернул ошибку {resp.status}: {text[:200]}")
        data = await resp.json()
        return data

@retry(max_attempts=2, delay=1, backoff=1.5)
async def get_accounts(http_session) -> list:
    params = {"status": "ACCOUNT_STATUS_OPEN"}
    data = await tinkoff_api_request(http_session, "POST", "tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts", params=params)
    return data.get("accounts", [])

@retry(max_attempts=2, delay=1, backoff=1.5)
async def get_portfolio_data(http_session, account_id: str) -> dict:
    params = {"accountId": account_id}
    data = await tinkoff_api_request(http_session, "POST", "tinkoff.public.invest.api.contract.v1.OperationsService/GetPortfolio", params=params)
    return data
    
@retry(max_attempts=3, delay=2, backoff=2)
async def get_portfolio_summary(http_session):
    try:
        accounts = await get_accounts(http_session)
        if not accounts:
            return None
        account_id = accounts[0].get("id")
        if not account_id:
            return None

        data = await get_portfolio_data(http_session, account_id)
        positions = data.get("positions", [])

        # Общая стоимость портфеля (с копейками)
        total_amount_obj = data.get("totalAmountPortfolio", {})
        total = float(total_amount_obj.get("units", 0)) + float(total_amount_obj.get("nano", 0)) / 1e9
        total_currency = total_amount_obj.get("currency", "RUB")

        total_cost = 0.0
        total_value = 0.0
        balance = 0.0

        # Определяем баланс (деньги на счёте)
        for pos in positions:
            ticker = pos.get("ticker", "")
            if ticker == "RUB000UTSTOM" or pos.get("instrumentType") == "INSTRUMENT_TYPE_CURRENCY":
                quantity_obj = pos.get("quantity", {})
                qty = float(quantity_obj.get("units", 0)) + float(quantity_obj.get("nano", 0)) / 1e9
                price_obj = pos.get("currentPrice", {})
                price = float(price_obj.get("units", 0)) + float(price_obj.get("nano", 0)) / 1e9
                balance = qty * price
                break

        filtered_positions = []
        for pos in positions:
            ticker = pos.get("ticker", "")
            if ticker == "RUB000UTSTOM" or pos.get("instrumentType") == "INSTRUMENT_TYPE_CURRENCY":
                continue
            filtered_positions.append(pos)

        # Суммируем стоимость и затраты
        for pos in filtered_positions:
            quantity_obj = pos.get("quantity", {})
            quantity = float(quantity_obj.get("units", 0)) + float(quantity_obj.get("nano", 0)) / 1e9
            avg_obj = pos.get("averagePositionPrice", {})
            avg_price = float(avg_obj.get("units", 0)) + float(avg_obj.get("nano", 0)) / 1e9
            price_obj = pos.get("currentPrice", {})
            price = float(price_obj.get("units", 0)) + float(price_obj.get("nano", 0)) / 1e9
            total_cost += quantity * avg_price
            total_value += quantity * price

        if total_cost > 0:
            total_yield_pct = (total_value - total_cost) / total_cost * 100
        else:
            total_yield_pct = 0.0

        result = {
            "total_amount": total_value,
            "currency": total_currency,
            "total_cost": total_cost,
            "total_yield_pct": total_yield_pct,
            "balance": balance,
            "positions": [],
            "expected_dividends": float(data.get("expectedDividends", 0))
        }

        type_map = {
            "INSTRUMENT_TYPE_SHARE": "Акции",
            "INSTRUMENT_TYPE_BOND": "Облигации",
            "INSTRUMENT_TYPE_ETF": "Фонды",
            "INSTRUMENT_TYPE_CURRENCY": "Валюта",
            "SHARE": "Акции",
            "BOND": "Облигации",
            "ETF": "Фонды",
            "CURRENCY": "Валюта",
        }

        # Обрабатываем каждую позицию
        for pos in filtered_positions:
            figi = pos.get("figi")
            ticker = pos.get("ticker") or figi
            raw_name = ticker_to_name.get(ticker)
            if raw_name is None:
                raw_name = ticker
            name = NAME_OVERRIDES.get(ticker)
            if name is None:
                name = NAME_OVERRIDES.get(raw_name, raw_name)

            quantity_obj = pos.get("quantity", {})
            quantity = float(quantity_obj.get("units", 0)) + float(quantity_obj.get("nano", 0)) / 1e9
            price_obj = pos.get("currentPrice", {})
            price = float(price_obj.get("units", 0)) + float(price_obj.get("nano", 0)) / 1e9
            avg_obj = pos.get("averagePositionPrice", {})
            avg_price = float(avg_obj.get("units", 0)) + float(avg_obj.get("nano", 0)) / 1e9
            expected_yield_obj = pos.get("expectedYield", {})
            expected_yield = float(expected_yield_obj.get("units", 0)) + float(expected_yield_obj.get("nano", 0)) / 1e9

            if avg_price and quantity:
                pos_yield_pct = (expected_yield / (avg_price * quantity)) * 100
            else:
                pos_yield_pct = 0.0

            instrument_type = pos.get("instrumentType", "").upper()

            if instrument_type in type_map:
                type_display = type_map[instrument_type]
            else:
                name_lower = name.lower()
                if "офз" in name_lower or "облиг" in name_lower:
                    type_display = "Облигации"
                elif ticker.startswith(("SU", "RU")):
                    type_display = "Облигации"
                elif "ETF" in name or ticker in ("LQDT", "TGLD", "TGLD@"):
                    type_display = "Фонды"
                elif "фонд" in name_lower or "etf" in name_lower:
                    type_display = "Фонды"
                else:
                    type_display = "Акции"

            result["positions"].append({
                "figi": figi,
                "ticker": ticker,
                "name": name or ticker,
                "instrument_type": instrument_type,
                "type_display": type_display,
                "quantity": quantity,
                "price": price,
                "avg_price": avg_price,
                "pos_yield_pct": pos_yield_pct,
            })

        return result
    except Exception as e:
        logging.error(f"Ошибка портфеля: {e}")
        return None

async def build_figi_map(http_session):
    """Запрашивает текущий портфель и строит словарь FIGI → ticker."""
    global portfolio_figi_to_ticker
    try:
        summary = await get_portfolio_summary(http_session)
        if summary:
            for pos in summary["positions"]:
                figi = pos["figi"]
                ticker = pos["ticker"]
                if figi and ticker and figi not in portfolio_figi_to_ticker:
                    portfolio_figi_to_ticker[figi] = ticker
            logging.info(f"build_figi_map: загружено {len(portfolio_figi_to_ticker)} FIGI из портфеля")
    except Exception as e:
        logging.error(f"Ошибка build_figi_map: {e}")

@retry(max_attempts=3, delay=2, backoff=2)
async def sync_operations(http_session, from_date=None, force_full=False):
    logging.info("sync_operations started")
    try:
        accounts = await get_accounts(http_session)
        if not accounts:
            logging.warning("sync_operations: нет аккаунтов")
            return 0
        account_id = accounts[0].get("id")
        if not account_id:
            logging.warning("sync_operations: не найден account_id")
            return 0

        # Определяем диапазон дат
        if force_full:
            # При полной синхронизации игнорируем последнюю дату в БД
            from_date = datetime.datetime.now() - datetime.timedelta(days=365*5)
        else:
            if from_date is None:
                last_date = db.get_last_operation_date()
                if last_date:
                    from_date = datetime.datetime.fromisoformat(last_date) + datetime.timedelta(seconds=1)
                else:
                    from_date = datetime.datetime.now() - datetime.timedelta(days=365*5)

        to_date = datetime.datetime.now()
        logging.info(f"Запрос операций с {from_date} по {to_date}")

        params = {
            "accountId": account_id,
            "from": from_date.strftime("%Y-%m-%dT%H:%M:%S+03:00"),
            "to": to_date.strftime("%Y-%m-%dT%H:%M:%S+03:00"),
            "state": "OPERATION_STATE_EXECUTED"
        }

        data = await tinkoff_api_request(http_session, "POST", "tinkoff.public.invest.api.contract.v1.OperationsService/GetOperations", params=params)
        operations = data.get("operations", [])
        logging.info(f"sync_operations: получено {len(operations)} операций от API")

        new_count = 0
        updated_count = 0
        for op in operations:
            ticker = op.get("ticker")
            if not ticker:
                figi = op.get("figi")
                if figi:
                    ticker = portfolio_figi_to_ticker.get(figi)
                    if not ticker:
                        from moex_api import figi_to_ticker as moex_figi_to_ticker
                        ticker = moex_figi_to_ticker.get(figi)
                if not ticker:
                    ticker = "Прочие"

            if op.get("currency", "RUB").upper() != "RUB":
                continue

            payment_obj = op.get("payment", {})
            units = float(payment_obj.get("units", 0))
            nano = float(payment_obj.get("nano", 0)) / 1e9
            payment_rub = units + nano

            commission_obj = op.get("commission", {})
            comm_units = float(commission_obj.get("units", 0))
            comm_nano = float(commission_obj.get("nano", 0)) / 1e9
            commission_rub = comm_units + comm_nano

            # Проверяем, существует ли уже запись
            with sqlite3.connect(db.DB_PATH) as conn:
                c = conn.cursor()
                c.execute("SELECT id FROM operations WHERE id = ?", (op.get("id"),))
                exists = c.fetchone() is not None

            db.insert_operation({
                "id": op.get("id"),
                "date": op.get("date"),
                "type": op.get("type"),
                "ticker": ticker,
                "figi": op.get("figi"),
                "instrument_type": op.get("instrumentType"),
                "quantity": op.get("quantity"),
                "payment": payment_rub,
                "currency": op.get("currency", "RUB").upper(),
                "commission": commission_rub,
                "name": op.get("name"),
            })
            if exists:
                updated_count += 1
            else:
                new_count += 1

        logging.info(f"sync_operations finished: добавлено {new_count}, обновлено {updated_count} записей")
        return new_count
    except Exception as e:
        logging.error(f"Ошибка в sync_operations: {e}", exc_info=True)
        return 0