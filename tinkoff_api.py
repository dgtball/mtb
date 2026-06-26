import logging
import datetime
import aiohttp
from config import TINKOFF_TOKEN, TINKOFF_API_URL, NAME_OVERRIDES, ticker_to_name
from moex_api import figi_to_ticker
import db

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

async def get_accounts(http_session) -> list:
    params = {"status": "ACCOUNT_STATUS_OPEN"}
    data = await tinkoff_api_request(http_session, "POST", "tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts", params=params)
    return data.get("accounts", [])

async def get_portfolio_data(http_session, account_id: str) -> dict:
    params = {"accountId": account_id}
    data = await tinkoff_api_request(http_session, "POST", "tinkoff.public.invest.api.contract.v1.OperationsService/GetPortfolio", params=params)
    return data

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

        total_amount = data.get("totalAmountPortfolio", {})
        total = float(total_amount.get("units", 0))
        total_currency = total_amount.get("currency", "RUB")

        total_cost = 0.0
        total_value = 0.0
        balance = 0.0

        for pos in positions:
            ticker = pos.get("ticker", "")
            if ticker == "RUB000UTSTOM" or pos.get("instrumentType") == "INSTRUMENT_TYPE_CURRENCY":
                balance = float(pos.get("quantity", {}).get("units", 0)) * float(pos.get("currentPrice", {}).get("units", 1))
                break

        filtered_positions = []
        for pos in positions:
            ticker = pos.get("ticker", "")
            if ticker == "RUB000UTSTOM" or pos.get("instrumentType") == "INSTRUMENT_TYPE_CURRENCY":
                continue
            filtered_positions.append(pos)

        for pos in filtered_positions:
            quantity = float(pos.get("quantity", {}).get("units", 0))
            avg_price = float(pos.get("averagePositionPrice", {}).get("units", 0))
            price = float(pos.get("currentPrice", {}).get("units", 0))
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

        for pos in filtered_positions:
            figi = pos.get("figi")
            ticker = pos.get("ticker") or figi
            raw_name = ticker_to_name.get(ticker, ticker)
            name = NAME_OVERRIDES.get(ticker)
            if name is None:
                name = NAME_OVERRIDES.get(raw_name, raw_name)

            quantity = float(pos.get("quantity", {}).get("units", 0))
            price = float(pos.get("currentPrice", {}).get("units", 0))
            avg_price = float(pos.get("averagePositionPrice", {}).get("units", 0))
            expected_yield = float(pos.get("expectedYield", {}).get("units", 0))
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
                "name": name,
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

async def sync_operations(http_session, from_date=None):
    """Синхронизирует операции из T-Invest API в БД. Возвращает количество новых операций."""
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

        if from_date is None:
            last_date = db.get_last_operation_date()
            if last_date:
                from_date = datetime.datetime.fromisoformat(last_date) + datetime.timedelta(seconds=1)
            else:
                from_date = datetime.datetime.now() - datetime.timedelta(days=365*5)

        to_date = datetime.datetime.now()
        params = {
            "accountId": account_id,
            "from": from_date.strftime("%Y-%m-%dT%H:%M:%S+03:00"),
            "to": to_date.strftime("%Y-%m-%dT%H:%M:%S+03:00"),
            "state": "OPERATION_STATE_EXECUTED"
        }
        data = await tinkoff_api_request(http_session, "POST", "tinkoff.public.invest.api.contract.v1.OperationsService/GetOperations", params=params)
        operations = data.get("operations", [])
        logging.info(f"sync_operations: получено {len(operations)} операций от API")

        unique_types = set()
        new_count = 0
        for op in operations:
            op_type = op.get("type", "")
            unique_types.add(op_type)
            ticker = op.get("ticker")
            if not ticker:
                figi = op.get("figi")
                if figi:
                    ticker = figi_to_ticker.get(figi)
            op["ticker"] = ticker

            # Исправлено: приводим валюту к верхнему регистру
            if op.get("currency", "RUB").upper() != "RUB":
                continue

            db.insert_operation({
                "id": op.get("id"),
                "date": op.get("date"),
                "type": op_type,
                "ticker": ticker,
                "figi": op.get("figi"),
                "instrument_type": op.get("instrumentType"),
                "quantity": op.get("quantity"),
                "payment": float(op.get("payment", {}).get("units", 0)) if op.get("payment") else 0,
                "currency": op.get("currency", "RUB").upper(),
                "commission": float(op.get("commission", {}).get("units", 0)) if op.get("commission") else 0,
                "name": op.get("name"),
            })
            new_count += 1

        logging.info(f"sync_operations: уникальные типы операций: {unique_types}")
        logging.info(f"sync_operations finished: добавлено {new_count} новых записей")
        return new_count
    except Exception as e:
        logging.error(f"Ошибка в sync_operations: {e}", exc_info=True)
        return 0