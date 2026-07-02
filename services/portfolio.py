import logging
import datetime
import pandas as pd
from typing import List, Dict, Any, Optional
from db import get_sector, get_daily_snapshot
from moex_api import get_market_data
from tinkoff_api import get_portfolio_summary
from utils import smart_price

async def get_portfolio_with_details(http_session) -> Optional[Dict[str, Any]]:
    try:
        data = await get_portfolio_summary(http_session)
        if not data:
            return None

        market_df = await get_market_data(http_session)
        ticker_change = {}
        if not market_df.empty and 'SECID' in market_df.columns and 'LAST' in market_df.columns and 'OPEN' in market_df.columns:
            for _, row in market_df.iterrows():
                secid = row['SECID']
                last = row['LAST']
                open_price = row['OPEN']
                if isinstance(last, (int, float)) and isinstance(open_price, (int, float)):
                    if open_price != 0 and pd.notna(open_price) and pd.notna(last):
                        change = ((last - open_price) / open_price) * 100
                        ticker_change[secid] = change

        total_amount = data["total_amount"]
        positions = []
        portfolio_equities = []

        for pos in data["positions"]:
            ticker = pos["ticker"]
            sector_name = await get_sector(ticker)
            value = pos["quantity"] * pos["price"]
            share = (value / total_amount * 100) if total_amount > 0 else 0

            position = {
                "ticker": ticker,
                "name": pos["name"],
                "price_formatted": smart_price(pos["price"]),
                "avg_price_formatted": smart_price(pos["avg_price"]),
                "value": value,
                "yield_pct": pos["pos_yield_pct"],
                "sector": sector_name,
                "share": round(share, 1),
            }
            positions.append(position)

            if sector_name and sector_name not in ("Прочие", "Фонд", "Облигации"):
                change = ticker_change.get(ticker)
                pct = change if change is not None else pos["pos_yield_pct"]
                portfolio_equities.append({
                    "name": pos["name"],
                    "price_formatted": smart_price(pos["price"]),
                    "change_pct": pct,
                })

        gainers = [p for p in portfolio_equities if p["change_pct"] > 0]
        losers = [p for p in portfolio_equities if p["change_pct"] < 0]
        gainers.sort(key=lambda x: x["change_pct"], reverse=True)
        losers.sort(key=lambda x: x["change_pct"])

        sectors = {}
        for p in positions:
            sec = p["sector"]
            sectors[sec] = sectors.get(sec, 0) + p["value"]
        sector_list = [{"name": k, "value": v} for k, v in sectors.items()]

        daily_change_pct = None
        today = datetime.date.today().isoformat()
        snapshot = await get_daily_snapshot(today)
        if snapshot is not None and snapshot > 0:
            daily_change_pct = (total_amount - snapshot) / snapshot * 100

        return {
            "total_amount": total_amount,
            "daily_change_pct": daily_change_pct,
            "positions": positions,
            "sectors": sector_list,
            "portfolio_gainers": gainers[:5],
            "portfolio_losers": losers[:5],
        }
    except Exception as e:
        logging.error(f"Ошибка в get_portfolio_with_details: {e}", exc_info=True)
        return None
