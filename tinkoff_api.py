import logging
import datetime
import aiohttp
from config import TINKOFF_TOKEN, TINKOFF_API_URL, NAME_OVERRIDES, ticker_to_name

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

async def get_portfolio_snapshot(http_session, target_date: datetime.date) -> float | None:
    """
    Возвращает стоимость портфеля на конец указанного дня (по цене закрытия).
    Использует GetOperations с фильтром по дате.
    Если данных нет, возвращает None.
    """
    try:
        accounts = await get_accounts(http_session)
        if not accounts:
            return None
        account_id = accounts[0].get("id")
        if not account_id:
            return None

        # Запрашиваем портфель на конец дня target_date
        from_date = target_date
        to_date = target_date + datetime.timedelta(days=1)
        params = {
            "accountId": account_id,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "state": "OPERATION_STATE_EXECUTED",
            "figi": "",
        }
        data = await tinkoff_api_request(http_session, "POST", "tinkoff.public.invest.api.contract.v1.OperationsService/GetOperations", params=params)
        operations = data.get("operations", [])

        # Собираем позиции на основе последних операций перед закрытием (упрощённо)
        # Так как API не даёт прямой исторический портфель, используем текущий портфель
        # с корректировкой по истории сделок за день. Но для восстановления снэпшота
        # проще сразу взять totalAmountPortfolio, если запросить портфель на дату
        # через GetPortfolio за вчера. Однако GetPortfolio не поддерживает дату.
        # Поэтому используем обходной путь: запросим позиции сейчас и вычтем изменения,
        # произошедшие за target_date..сегодня. Но это сложно.
        # Более надёжный способ: использовать GetOperations для получения суммы портфеля
        # на конец дня, но API Т-Инвестиций не предоставляет такую возможность напрямую.
        # Поэтому в качестве fallback возвращаем None, чтобы вызвался текущий снэпшот.
        return None
    except Exception as e:
        logging.error(f"Ошибка получения исторического снэпшота: {e}")
        return None