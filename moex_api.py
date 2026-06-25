import logging
import asyncio
import datetime
import pandas as pd
import aiohttp
from config import ticker_to_name

ticker_to_sector = {}

async def load_instrument_names(http_session):
    global ticker_to_name
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    # Акции TQBR
    url_shares = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=securities"
    try:
        async with http_session.get(url_shares, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                json_data = await resp.json()
                if 'securities' in json_data:
                    columns = json_data['securities']['columns']
                    data_rows = json_data['securities']['data']
                    df = pd.DataFrame(data_rows, columns=columns)
                    if 'SECID' in df.columns and 'SHORTNAME' in df.columns:
                        for _, row in df.iterrows():
                            raw_name = row['SHORTNAME']
                            clean_name = raw_name.replace(' ао', '').replace(' ап', '')
                            if clean_name.startswith('i'):
                                clean_name = clean_name[1:]
                            ticker_to_name[row['SECID']] = clean_name
                            if 'SECTORID' in df.columns:
                                ticker_to_sector[row['SECID']] = row['SECTORID']
    except Exception as e:
        logging.error(f"Ошибка загрузки акций: {e}")

    # Облигации (две доски)
    for board in ['TQOB', 'TQCB']:
        url_bonds = f"https://iss.moex.com/iss/engines/stock/markets/bonds/boards/{board}/securities.json?iss.meta=off&iss.only=securities"
        try:
            async with http_session.get(url_bonds, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    json_data = await resp.json()
                    if 'securities' in json_data:
                        columns = json_data['securities']['columns']
                        data_rows = json_data['securities']['data']
                        df = pd.DataFrame(data_rows, columns=columns)
                        if 'SECID' in df.columns and 'SHORTNAME' in df.columns:
                            for _, row in df.iterrows():
                                raw_name = row['SHORTNAME']
                                clean_name = raw_name.replace(' ао', '').replace(' ап', '')
                                if clean_name.startswith('i'):
                                    clean_name = clean_name[1:]
                                ticker_to_name[row['SECID']] = clean_name
                                if 'SECTORID' in df.columns:
                                    ticker_to_sector[row['SECID']] = row['SECTORID']
        except Exception as e:
            logging.error(f"Ошибка загрузки облигаций {board}: {e}")

    # ETF
    url_etf = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQTF/securities.json?iss.meta=off&iss.only=securities"
    try:
        async with http_session.get(url_etf, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                json_data = await resp.json()
                if 'securities' in json_data:
                    columns = json_data['securities']['columns']
                    data_rows = json_data['securities']['data']
                    df = pd.DataFrame(data_rows, columns=columns)
                    if 'SECID' in df.columns and 'SHORTNAME' in df.columns:
                        for _, row in df.iterrows():
                            raw_name = row['SHORTNAME']
                            clean_name = raw_name.replace(' ао', '').replace(' ап', '')
                            if clean_name.startswith('i'):
                                clean_name = clean_name[1:]
                            ticker_to_name[row['SECID']] = clean_name
                            if 'SECTORID' in df.columns:
                                ticker_to_sector[row['SECID']] = row['SECTORID']
    except Exception as e:
        logging.error(f"Ошибка загрузки ETF: {e}")

    logging.info(f"✅ Загружено {len(ticker_to_name)} наименований и {len(ticker_to_sector)} секторов")

async def get_market_data(http_session):
    url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata,securities"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for attempt in range(3):
        try:
            async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                json_data = await resp.json()
                if 'marketdata' not in json_data or 'securities' not in json_data:
                    await asyncio.sleep(2)
                    continue
                md_columns = json_data['marketdata']['columns']
                md_rows = json_data['marketdata']['data']
                market_df = pd.DataFrame(md_rows, columns=md_columns)
                sec_columns = json_data['securities']['columns']
                sec_rows = json_data['securities']['data']
                sec_df = pd.DataFrame(sec_rows, columns=sec_columns)

                if not hasattr(get_market_data, '_logged'):
                    logging.info(f"Структура marketdata (однократно): колонки securities: {sec_columns}")
                    if 'SECTYPE' in sec_columns:
                        sample = sec_df['SECTYPE'].head(10).tolist()
                        logging.info(f"Примеры SECTYPE: {sample}")
                    get_market_data._logged = True

                available_cols = ['SECID', 'SHORTNAME', 'LISTLEVEL', 'SECTORID']
                if 'SECTYPE' in sec_columns:
                    available_cols.append('SECTYPE')
                if 'BOARDID' in sec_columns:
                    available_cols.append('BOARDID')
                sec_df = sec_df[available_cols].copy()
                merged = pd.merge(market_df, sec_df, on='SECID', how='left')
                return merged
        except Exception as e:
            logging.error(f"Ошибка в get_market_data: {e}")
            await asyncio.sleep(2)
    return pd.DataFrame()

async def get_historical_shares(http_session, from_date, till_date):
    url = f"https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities.json?from={from_date}&till={till_date}&iss.meta=off&iss.only=history"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for attempt in range(3):
        try:
            async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                json_data = await resp.json()
                if 'history' not in json_data:
                    return pd.DataFrame()
                columns = json_data['history']['columns']
                data_rows = json_data['history']['data']
                df = pd.DataFrame(data_rows, columns=columns)
                return df
        except Exception:
            await asyncio.sleep(2)
    return pd.DataFrame()

async def get_historical_close(http_session, ticker, target_date):
    from_date = (target_date - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
    till_date = target_date.strftime("%Y-%m-%d")
    df = await get_historical_shares(http_session, from_date, till_date)
    if df.empty:
        return None
    ticker_data = df[df['SECID'] == ticker].copy()
    if ticker_data.empty:
        return None
    ticker_data['TRADEDATE'] = pd.to_datetime(ticker_data['TRADEDATE'])
    ticker_data = ticker_data.sort_values('TRADEDATE')
    return ticker_data.iloc[-1]['CLOSE']

async def get_moex_index_info(http_session):
    url = "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json?iss.meta=off"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logging.warning(f"MOEX index returned status {resp.status}")
                return None
            json_data = await resp.json()
            if 'marketdata' in json_data:
                md_columns = json_data['marketdata']['columns']
                md_rows = json_data['marketdata']['data']
                if md_rows:
                    row = md_rows[0]
                    current_idx = md_columns.index('CURRENTVALUE') if 'CURRENTVALUE' in md_columns else None
                    if current_idx is None:
                        current_idx = md_columns.index('LASTVALUE') if 'LASTVALUE' in md_columns else None
                    change_idx = md_columns.index('LASTCHANGEPRC') if 'LASTCHANGEPRC' in md_columns else None
                    if change_idx is None:
                        change_idx = md_columns.index('LASTCHANGETOOPENPRC') if 'LASTCHANGETOOPENPRC' in md_columns else None
                    result = {}
                    if current_idx is not None:
                        result['last'] = float(row[current_idx])
                    if change_idx is not None:
                        result['change_percent'] = float(row[change_idx])
                    if result:
                        return result
            if 'securities' in json_data:
                columns = json_data['securities']['columns']
                data_rows = json_data['securities']['data']
                if data_rows:
                    row = data_rows[0]
                    last_idx = columns.index('LAST') if 'LAST' in columns else None
                    change_percent_idx = columns.index('CHANGEPERCENT') if 'CHANGEPERCENT' in columns else None
                    result = {}
                    if last_idx is not None:
                        result['last'] = float(row[last_idx])
                    if change_percent_idx is not None:
                        result['change_percent'] = float(row[change_percent_idx])
                    if result:
                        return result
            logging.warning("Не найдены данные индекса")
            return None
    except Exception as e:
        logging.error(f"Ошибка получения индекса: {e}")
        return None

async def get_moex_index(http_session):
    info = await get_moex_index_info(http_session)
    return info.get('last') if info else None

def get_top_movers(data, top_n=10, exclude_level3=True):
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    if 'SECTYPE' in data.columns:
        data = data[data['SECTYPE'].isin(['1', '2'])].copy()
    if 'BOARDID' in data.columns:
        data = data[data['BOARDID'] == 'TQBR'].copy()
    if exclude_level3 and 'LISTLEVEL' in data.columns:
        data = data[data['LISTLEVEL'] < 3].copy()
    data = data.copy()
    if 'CHANGEPERCENT' not in data.columns:
        if 'OPEN' in data.columns and 'LAST' in data.columns:
            data['CHANGEPERCENT'] = ((data['LAST'] - data['OPEN']) / data['OPEN']) * 100
        else:
            return pd.DataFrame(), pd.DataFrame()
    required = ['SECID', 'CHANGEPERCENT', 'LAST', 'SHORTNAME']
    for col in required:
        if col not in data.columns:
            if col == 'SHORTNAME':
                data['SHORTNAME'] = data['SECID']
            else:
                return pd.DataFrame(), pd.DataFrame()
    data = data.dropna(subset=['SECID', 'CHANGEPERCENT', 'LAST'])
    data['CHANGEPERCENT'] = pd.to_numeric(data['CHANGEPERCENT'], errors='coerce')
    data['LAST'] = pd.to_numeric(data['LAST'], errors='coerce')
    data = data.dropna(subset=['CHANGEPERCENT', 'LAST'])
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()

    positive = data[data['CHANGEPERCENT'] > 0].copy()
    negative = data[data['CHANGEPERCENT'] < 0].copy()
    gainers = positive.nlargest(top_n, 'CHANGEPERCENT') if not positive.empty else pd.DataFrame()
    losers = negative.nsmallest(top_n, 'CHANGEPERCENT') if not negative.empty else pd.DataFrame()
    return gainers, losers

def calc_period_change(df):
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df['TRADEDATE'] = pd.to_datetime(df['TRADEDATE'])
    df = df.sort_values('TRADEDATE')
    first = df.groupby('SECID').first()[['OPEN']]
    last = df.groupby('SECID').last()[['CLOSE']]
    combined = first.join(last, how='inner')
    combined['CHANGE_PCT'] = ((combined['CLOSE'] - combined['OPEN']) / combined['OPEN']) * 100
    return combined.reset_index()