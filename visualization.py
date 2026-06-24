import io
import logging
import os
import pandas as pd
from collections import defaultdict
from jinja2 import Environment, FileSystemLoader
import cairosvg
from utils import smart_price
from config import NAME_OVERRIDES

# Настройка Jinja2 для загрузки шаблона из текущей директории
env = Environment(loader=FileSystemLoader('.'))
template = env.get_template('portfolio_template.svg')

# ---------- НОВАЯ ВИЗУАЛИЗАЦИЯ ПОРТФЕЛЯ (SVG) ----------
def generate_portfolio_image(portfolio_data, daily_change_pct=None) -> io.BytesIO:
    if not portfolio_data or not portfolio_data["positions"]:
        logging.warning("Нет позиций для отображения портфеля")
        return None

    positions = portfolio_data["positions"]
    total_amount = portfolio_data["total_amount"]
    total_yield_pct = portfolio_data["total_yield_pct"]
    total_cost = portfolio_data["total_cost"]
    balance = portfolio_data.get("balance", 0.0)

    # Распределение по типам для кольцевой диаграммы
    type_values = defaultdict(float)
    for pos in positions:
        value = pos["quantity"] * pos["price"]
        type_values[pos["type_display"]] += value

    total_value = sum(type_values.values())
    type_percents = {}
    for t, v in type_values.items():
        type_percents[t] = (v / total_value * 100) if total_value > 0 else 0

    # Топ‑5 позиций по стоимости
    sorted_pos = sorted(positions, key=lambda p: p["quantity"] * p["price"], reverse=True)
    top5 = sorted_pos[:5]

    top5_data = []
    for pos in top5:
        ticker = pos['ticker']
        name = pos['name']
        display_name = NAME_OVERRIDES.get(name, name)
        if len(display_name) > 12:
            display_name = display_name[:10] + '…'

        value = pos['quantity'] * pos['price']
        value_formatted = f"{value:,.0f}"
        yield_pct = pos['pos_yield_pct']

        top5_data.append({
            'display_name': display_name,
            'ticker': ticker,
            'value': value,
            'value_formatted': value_formatted,
            'yield_pct': yield_pct
        })

    # Если топ-5 меньше 5, дополним пустыми, чтобы шаблон не сломался
    while len(top5_data) < 5:
        top5_data.append({
            'display_name': '—',
            'value': 0,
            'value_formatted': '0',
            'yield_pct': 0
        })

    # Форматирование итоговых сумм
    total_amount_f = f"{total_amount:,.2f}"
    total_cost_f = f"{total_cost:,.2f}"
    balance_f = f"{balance:,.2f}"

    # Время обновления (московское)
    from utils import get_moscow_time
    update_time = get_moscow_time().strftime("%d.%m.%Y %H:%M МСК")

    # Рендеринг SVG
    svg = template.render(
        update_time=update_time,
        total_amount_formatted=total_amount_f,
        daily_change_pct=daily_change_pct,
        type_percents=type_percents,
        top5=top5_data,
        total_cost_formatted=total_cost_f,
        total_yield_pct=total_yield_pct,
        balance_formatted=balance_f
    )

    # Конвертация в PNG
    try:
        png_bytes = cairosvg.svg2png(bytestring=svg.encode('utf-8'), output_width=800, output_height=600)
        return io.BytesIO(png_bytes)
    except Exception as e:
        logging.error(f"Ошибка рендеринга SVG портфеля: {e}")
        return None

# ---------- ИЗБРАННОЕ (БЕЗ ИЗМЕНЕНИЙ) ----------
def generate_favorites_image(fav_df) -> io.BytesIO:
    if fav_df.empty:
        return None
    # Импортируем matplotlib только здесь, чтобы не конфликтовать с CairoSVG
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    table_data = []
    for _, row in fav_df.iterrows():
        name = row.get('SHORTNAME', row['SECID'])
        secid = row['SECID']
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