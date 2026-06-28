# services/tops.py
import logging
import datetime
import pandas as pd
from moex_api import get_market_data, get_historical_shares, calc_period_change, get_moex_index_info, get_top_movers
from utils import get_moscow_time, get_session_status, smart_price, build_table_universal, get_portfolio_change_str
from config import TOP_N

async def get_top_data(period: str, http_session):
    """
    Возвращает данные для топа (лидеры роста/падения) за указанный период.
    period: 'day', 'week', 'month'
    Возвращает кортеж (gainers, losers, index_info, session_status, update_time, portfolio_line)
    """
    if period == 'day':
        shares_df = await get_market_data(http_session)
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        index_info = await get_moex_index_info(http_session)
        session_status = get_session_status(time_offset=1)
        update_time = datetime.datetime.now().strftime("%d/%m/%y %H:%M:%S")
        portfolio_line = get_portfolio_change_str()
        return gainers, losers, index_info, session_status, update_time, portfolio_line

    else:
        now = get_moscow_time()
        if period == 'week':
            start = now - datetime.timedelta(days=now.weekday())
        else:  # month
            start = now.replace(day=1)
        from_date_str = start.strftime("%Y-%m-%d")
        till_date_str = now.strftime("%Y-%m-%d")
        df = await get_historical_shares(http_session, from_date_str, till_date_str)
        if df.empty:
            return pd.DataFrame(), pd.DataFrame(), None, None, None, None

        changes = calc_period_change(df)
        shares_all = await get_market_data(http_session)
        if not shares_all.empty:
            mask = (shares_all['LISTLEVEL'] < 3) & (shares_all['SECTYPE'].isin(['1', '2']))
            allowed_tickers = shares_all[mask]['SECID'].unique()
            changes = changes[changes['SECID'].isin(allowed_tickers)]
            names = shares_all[mask][['SECID', 'SHORTNAME']].drop_duplicates('SECID')
            changes = changes.merge(names, on='SECID', how='left')
        positive = changes[changes['CHANGE_PCT'] > 0]
        negative = changes[changes['CHANGE_PCT'] < 0]
        gainers = positive.nlargest(TOP_N, 'CHANGE_PCT') if not positive.empty else pd.DataFrame()
        losers = negative.nsmallest(TOP_N, 'CHANGE_PCT') if not negative.empty else pd.DataFrame()
        return gainers, losers, None, None, None, None