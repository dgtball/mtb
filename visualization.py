import io
import logging
import pandas as pd
from collections import defaultdict
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio
pio.kaleido.scope.default_format = "png"
from utils import smart_price
from config import NAME_OVERRIDES

def generate_portfolio_image(portfolio_data) -> io.BytesIO:
    if not portfolio_data or not portfolio_data["positions"]:
        logging.warning("Нет позиций для отображения портфеля")
        return None

    groups = defaultdict(list)
    for pos in portfolio_data["positions"]:
        groups[pos["type_display"]].append(pos)

    order = ["Акции", "Облигации", "Фонды"]
    ordered_groups = [(key, groups.pop(key)) for key in order if key in groups]
    for key, vals in groups.items():
        ordered_groups.append((key, vals))

    # Диагностика: выводим количество и первые 3 тикера
    for group_name, positions in ordered_groups:
        sample_tickers = [p['ticker'] for p in positions[:3]]
        logging.info(f"Группа {group_name}: {len(positions)} позиций. Примеры: {sample_tickers}")

    if not ordered_groups:
        return None

    total_amount = portfolio_data["total_amount"]
    total_cost = portfolio_data["total_cost"]
    total_yield = portfolio_data["total_yield_pct"]
    balance = portfolio_data.get("balance", 0.0)

    rows = len(ordered_groups)
    # Высота строки на одну позицию, плюс заголовок таблицы (30px), плюс небольшой запас
    row_heights = [30 * len(positions) + 50 for _, positions in ordered_groups]
    total_height = sum(row_heights) + 100  # 100px на общий заголовок и поля

    specs = [[{"type": "table"} for _ in range(1)] for _ in range(rows)]
    fig = make_subplots(
        rows=rows, cols=1,
        row_heights=row_heights,
        shared_xaxes=False,
        vertical_spacing=0.01,          # <-- минимальный зазор между подтаблицами
        subplot_titles=[g[0] for g in ordered_groups],
        specs=specs
    )

    col_labels = ["Название", "Кол-во", "Цена", "Средняя", "Доходность"]

    for idx, (group_name, positions) in enumerate(ordered_groups, start=1):
        table_data = []
        for pos in positions:
            display_name = pos["name"][:30] if pos["name"] else pos["ticker"]
            table_data.append([
                display_name,
                f"{pos['quantity']:.0f}",
                smart_price(pos['price']),
                smart_price(pos['avg_price']),
                f"{pos['pos_yield_pct']:+.2f}%"
            ])

        table_trace = go.Table(
            header=dict(
                values=col_labels,
                fill_color='#f0f0f0',
                align='center',
                font=dict(size=12, color='black', family='Arial')
            ),
            cells=dict(
                values=[list(col) for col in zip(*table_data)] if table_data else [[]],
                fill_color=[[
                    '#e6f9e6' if float(row[4].replace('%', '').replace('+', '')) > 0 else '#fce4e4'
                    for row in table_data
                ]],
                align='center',
                font=dict(size=11, color='black', family='Arial')
            ),
            name=group_name
        )
        fig.add_trace(table_trace, row=idx, col=1)

    fig.update_layout(
        title=dict(
            text=f"Портфель<br>Сумма: {total_amount:.2f} ₽   Вложено: {total_cost:.2f} ₽   "
                 f"Доходность: {total_yield:+.2f}%   Баланс: {balance:.2f} ₽",
            x=0.5, xanchor='center',
            font=dict(size=14, family='Arial', color='black')
        ),
        width=800,
        height=total_height,
        margin=dict(l=20, r=20, t=80, b=20),
        paper_bgcolor='white',
        showlegend=False
    )

    try:
        img_bytes = pio.to_image(fig, format='png', engine='kaleido')
        return io.BytesIO(img_bytes)
    except Exception as e:
        logging.error(f"Ошибка экспорта портфеля в PNG: {e}")
        return None

def generate_favorites_image(fav_df) -> io.BytesIO:
    if fav_df.empty:
        return None
    table_data = []
    for _, row in fav_df.iterrows():
        name = row.get('SHORTNAME', row['SECID'])
        secid = row['SECID']                           # <-- тикер
        # Применяем переопределение: сначала по тикеру, потом по названию
        display = NAME_OVERRIDES.get(secid, name)
        if display == name:
            display = NAME_OVERRIDES.get(name, name)
        if len(display) > 20:
            display = display[:17] + "…"
        price = smart_price(row['LAST']) if isinstance(row['LAST'], (int, float)) else str(row['LAST'])
        day = f"{row['CHANGEPERCENT']:+.2f}%" if pd.notna(row['CHANGEPERCENT']) else "—"
        week = f"{row['change_week']:+.2f}%" if pd.notna(row['change_week']) else "—"
        month = f"{row['change_month']:+.2f}%" if pd.notna(row['change_month']) else "—"
        table_data.append([display, price, day, week, month])
    headers = ["Название", "Цена", "День", "Неделя", "Месяц"]
    fig, ax = plt.subplots(figsize=(8, max(3, len(table_data) * 0.4 + 1)))
    ax.axis('off')
    table = ax.table(cellText=table_data, colLabels=headers, loc='center', cellLoc='center',
                     colColours=['#f0f0f0']*5, bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.5)
    for i, row in enumerate(table_data):
        for j, cell in enumerate(row):
            if j >= 2:
                val = row[j]
                if val != "—" and val.startswith('+'):
                    table[(i+1, j)].set_facecolor('lightgreen')
                elif val != "—" and val.startswith('-'):
                    table[(i+1, j)].set_facecolor('lightcoral')
    ax.set_title("Избранные акции", fontsize=14, fontweight='bold', pad=20)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.2)
    buf.seek(0)
    plt.close()
    return buf