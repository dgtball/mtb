import io
import logging
import pandas as pd
from collections import defaultdict
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from utils import smart_price
from config import NAME_OVERRIDES

plt.style.use('dark_background')

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
    labels = []
    sizes = []
    for t, v in type_values.items():
        labels.append(t)
        sizes.append(v)

    # Топ‑5 позиций по стоимости
    sorted_pos = sorted(positions, key=lambda p: p["quantity"] * p["price"], reverse=True)
    top5 = sorted_pos[:5]

    # Создание тёмной фигуры
    fig, (ax_donut, ax_bars) = plt.subplots(
        1, 2, figsize=(12, 6),
        gridspec_kw={'width_ratios': [1, 2]},
        facecolor='#121212'
    )
    ax_donut.set_facecolor('#121212')
    ax_bars.set_facecolor('#121212')

    # --- Кольцевая диаграмма ---
    donut_colors = {'Акции': '#2196F3', 'Облигации': '#FF9800', 'Фонды': '#4CAF50'}
    pie_colors = [donut_colors.get(l, '#9E9E9E') for l in labels]

    wedges, texts, autotexts = ax_donut.pie(
        sizes, labels=labels, autopct='%1.1f%%',
        startangle=90, pctdistance=0.85, colors=pie_colors,
        wedgeprops=dict(width=0.4, edgecolor='#121212'),
        textprops=dict(color='white')
    )
    for autotext in autotexts:
        autotext.set_fontsize(9)
        autotext.set_color('white')
    ax_donut.set_title('Структура портфеля', fontsize=12, fontweight='bold', color='white')

    # --- Горизонтальные бары (топ-5) ---
    names = []
    values = []
    pct_changes = []
    for pos in top5:
        ticker = pos['ticker']
        name = pos['name']
        display = NAME_OVERRIDES.get(name, name)
        if len(display) > 12:
            display = display[:10] + '…'
        names.append(display)
        val = pos['quantity'] * pos['price']
        values.append(val)
        pct_changes.append(pos['pos_yield_pct'])

    y_pos = range(len(names))
    bar_colors = ['#4CAF50' if p >= 0 else '#F44336' for p in pct_changes]
    ax_bars.barh(y_pos, values, color=bar_colors, height=0.6)
    ax_bars.set_yticks(y_pos)
    ax_bars.set_yticklabels(names, color='white')
    ax_bars.invert_yaxis()
    ax_bars.set_xlabel('Стоимость, ₽', color='white')
    ax_bars.set_title('Топ-5 позиций', fontsize=12, fontweight='bold', color='white')
    ax_bars.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'{x:,.0f}'))
    ax_bars.tick_params(colors='white')
    ax_bars.spines['bottom'].set_color('white')
    ax_bars.spines['top'].set_visible(False)
    ax_bars.spines['right'].set_visible(False)
    ax_bars.spines['left'].set_color('white')

    # Подписи доходности справа
    max_val = max(values) if values else 1
    for i, (val, pct) in enumerate(zip(values, pct_changes)):
        ax_bars.text(
            val + max_val * 0.02, i,
            f'{pct:+.1f}%', va='center', fontsize=8,
            color='#4CAF50' if pct >= 0 else '#F44336'
        )

    # --- Общая информация ---
    info_text = (
        f"Сумма: {total_amount:,.2f} ₽    "
        f"Вложено: {total_cost:,.2f} ₽    "
        f"Доходность: {total_yield_pct:+.2f}%"
    )
    if daily_change_pct is not None:
        info_text += f"    Изм. за день: {daily_change_pct:+.2f}%"
    if balance > 0:
        info_text += f"    Баланс: {balance:,.2f} ₽"
    fig.suptitle(info_text, fontsize=10, color='white', y=0.02)

    plt.tight_layout(rect=[0, 0.03, 1, 0.97])
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='#121212')
    buf.seek(0)
    plt.close()
    return buf