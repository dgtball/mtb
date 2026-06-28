const API_TOKEN = 'MINI_APP_TOKEN_PLACEHOLDER';
let sectorChart, dividendChart;
let allPositions = [];
let allTickersList = [];
let sectorsList = [];

// ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
async function apiCall(endpoint, method = 'GET', body = null) {
  const options = { method, headers: { 'Content-Type': 'application/json', 'X-Mini-App-Token': API_TOKEN } };
  if (body) options.body = JSON.stringify(body);
  const res = await fetch(endpoint, options);
  if (!res.ok) throw new Error(await res.text() || 'Ошибка запроса');
  return res.json();
}

function showToast(message, type = 'info') {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.className = 'toast ' + type;
  toast.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove('show'), 3000);
}

// ===== ПЕРЕКЛЮЧЕНИЕ ВКЛАДОК =====
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  const map = { portfolio: 0, dividends: 1, settings: 2 };
  document.querySelectorAll('.tab')[map[tab]].classList.add('active');
  document.getElementById(tab).classList.add('active');
  if (tab === 'portfolio') loadPortfolio();
  else if (tab === 'dividends') loadDividends();
  else if (tab === 'settings') {
    // Показываем первую подвкладку (активна по умолчанию)
    loadOverrides();
  }
}

function switchSettingsTab(tab) {
  document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.settings-content').forEach(c => c.classList.remove('active'));
  document.querySelector(`.settings-tab[onclick="switchSettingsTab('${tab}')"]`).classList.add('active');
  document.getElementById(`settings-${tab}`).classList.add('active');
  if (tab === 'overrides') loadOverrides();
  else if (tab === 'link') loadUntickedOperations();
  else if (tab === 'sectors') loadSectors();
}

// ===== ПОРТФЕЛЬ =====
async function loadPortfolio() {
  try {
    const data = await apiCall('/api/portfolio');
    document.getElementById('totalAmount').textContent = data.total_amount.toLocaleString() + ' ₽';
    const change = data.daily_change_pct;
    const changeEl = document.getElementById('dailyChange');
    if (change !== null && change !== undefined) {
      changeEl.textContent = (change >= 0 ? '+' : '') + change.toFixed(2) + '%';
      changeEl.style.color = change >= 0 ? 'var(--positive)' : 'var(--negative)';
    } else {
      changeEl.textContent = '—';
      changeEl.style.color = 'var(--text-secondary)';
    }
    allPositions = data.positions || [];
    // Сектора
    if (data.sectors && data.sectors.length > 0) {
      const totalValue = data.sectors.reduce((sum, s) => sum + s.value, 0);
      const labels = data.sectors.map(s => s.name);
      const values = data.sectors.map(s => s.value);
      const percentages = data.sectors.map(s => totalValue > 0 ? (s.value / totalValue * 100) : 0);
      const ctx = document.getElementById('sectorChart').getContext('2d');
      if (sectorChart) sectorChart.destroy();
      sectorChart = new Chart(ctx, {
        type: 'bar',
        data: { labels, datasets: [{ data: values, backgroundColor: '#2196F3', borderRadius: 4 }] },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: { enabled: false },
            datalabels: {
              anchor: 'end',
              align: 'top',
              color: '#e0e0e0',
              font: { weight: 'bold', size: 10 },
              formatter: (v, ctx) => percentages[ctx.dataIndex].toFixed(1) + '%'
            }
          },
          scales: {
            x: { ticks: { color: '#9e9e9e' }, grid: { display: false } },
            y: { ticks: { color: '#9e9e9e', callback: v => v.toLocaleString() + ' ₽' }, grid: { color: '#333' } }
          },
          onClick: (event, elements, chart) => {
            let index = null;
            if (elements.length > 0) index = elements[0].index;
            else {
              const xScale = chart.scales.x;
              const xPixel = event.x;
              const labelIndex = xScale.getValueForPixel(xPixel);
              if (typeof labelIndex === 'number' && labelIndex >= 0 && labelIndex < chart.data.labels.length) {
                index = Math.round(labelIndex);
              }
            }
            if (index !== null) showSectorDetails(chart.data.labels[index]);
          }
        },
        plugins: [ChartDataLabels]
      });
    }
    // Лидеры
    const gainersBody = document.getElementById('gainersTable');
    const losersBody = document.getElementById('losersTable');
    gainersBody.innerHTML = '';
    losersBody.innerHTML = '';
    if (data.portfolio_gainers) {
      data.portfolio_gainers.forEach(item => {
        const row = gainersBody.insertRow();
        row.innerHTML = `<td>${item.name}</td><td>${item.price_formatted}</td><td style="color:var(--positive)">+${item.change_pct.toFixed(2)}%</td>`;
      });
    }
    if (data.portfolio_losers) {
      data.portfolio_losers.forEach(item => {
        const row = losersBody.insertRow();
        row.innerHTML = `<td>${item.name}</td><td>${item.price_formatted}</td><td style="color:var(--negative)">${item.change_pct.toFixed(2)}%</td>`;
      });
    }
  } catch(e) {
    console.error(e);
    showToast('Ошибка загрузки портфеля', 'error');
  }
}

function showSectorDetails(sectorName) {
  const filtered = allPositions.filter(p => p.sector === sectorName);
  const container = document.getElementById('sectorDetails');
  if (filtered.length === 0) { container.classList.remove('visible'); return; }
  let html = `<h4>${sectorName}</h4><div class="table-wrap"><table><thead><tr><th>Название</th><th>Средняя</th><th>Доходность</th><th>Доля</th></tr></thead><tbody>`;
  filtered.forEach(p => {
    const sign = p.yield_pct >= 0 ? '+' : '';
    html += `<tr><td>${p.name}</td><td>${p.avg_price_formatted}</td><td style="color:${p.yield_pct >= 0 ? 'var(--positive)' : 'var(--negative)'}">${sign}${p.yield_pct.toFixed(2)}%</td><td>${p.share}%</td></tr>`;
  });
  html += '</tbody></table></div>';
  container.innerHTML = html;
  container.classList.add('visible');
}

// ===== ВЫПЛАТЫ =====
async function loadDividends() {
  try {
    const data = await apiCall('/api/dividends-yearly');
    const ctx = document.getElementById('dividendChart').getContext('2d');
    if (dividendChart) dividendChart.destroy();

    if (!data.years || data.years.length === 0) {
      // Пустой график
      return;
    }

    const years = data.years;
    const assetSet = new Set();
    data.datasets.forEach(ds => assetSet.add(ds.label));
    const assets = Array.from(assetSet).sort();

    const datasets = years.map((year, yearIndex) => {
      const yearData = assets.map(asset => {
        const ds = data.datasets.find(d => d.label === asset);
        return ds ? ds.data[yearIndex] : 0;
      });
      return {
        label: year,
        data: yearData,
        backgroundColor: ['#2196F3','#FF9800','#4CAF50','#F44336','#9C27B0','#00BCD4','#FFEB3B','#795548','#607D8B','#E91E63'][yearIndex % 10],
        borderRadius: 4,
      };
    });

    dividendChart = new Chart(ctx, {
      type: 'bar',
      data: { labels: assets, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: 'bottom', labels: { color: '#e0e0e0', font: { size: 10 } } },
          tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.raw.toLocaleString()}` } }
        },
        scales: {
          x: { stacked: true, ticks: { color: '#9e9e9e', font: { size: 9 } }, grid: { display: false } },
          y: { stacked: true, ticks: { color: '#9e9e9e', callback: v => v.toLocaleString(), font: { size: 9 } }, grid: { color: '#333' } }
        },
        barPercentage: 0.9,
        categoryPercentage: 0.9,
        onClick: (event, elements, chart) => {
          const xScale = chart.scales.x;
          const xPixel = event.x;
          const xIndex = Math.round(xScale.getValueForPixel(xPixel));
          if (xIndex >= 0 && xIndex < chart.data.labels.length) {
            const asset = chart.data.labels[xIndex];
            // Находим год с первой ненулевой выплатой
            let year = null;
            for (let ds of chart.data.datasets) {
              if (ds.data[xIndex] > 0) {
                year = ds.label;
                break;
              }
            }
            if (year) showDividendDetails(year, asset);
          }
        }
      }
    });

    // Синхронизация
    const syncStatus = await apiCall('/api/sync-status');
    if (syncStatus.last_sync) {
      document.getElementById('lastSync').textContent = new Date(syncStatus.last_sync).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' });
    }
  } catch(e) {
    console.error(e);
    showToast('Ошибка загрузки выплат', 'error');
  }
}

async function showDividendDetails(year, asset) {
  const url = `/api/dividends-yearly?year=${year}&ticker=${encodeURIComponent(asset)}`;
  const data = await apiCall(url);
  const container = document.getElementById('dividendDetails');
  if (!data.details || data.details.length === 0) { container.classList.remove('visible'); return; }
  let html = `<h4>${asset} за ${year}</h4><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Название</th><th>Сумма</th></tr></thead><tbody>`;
  data.details.forEach(d => {
    html += `<tr><td>${new Date(d.date).toLocaleDateString()}</td><td>${d.name}</td><td>${d.amount.toLocaleString()}</td></tr>`;
  });
  html += '</tbody></table></div>';
  container.innerHTML = html;
  container.classList.add('visible');
}

async function syncOperations() {
  try {
    const res = await apiCall('/api/sync', 'POST');
    if (res.last_sync) {
      document.getElementById('lastSync').textContent = new Date(res.last_sync).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' });
    }
    loadDividends();
    showToast('Синхронизация выполнена успешно', 'success');
  } catch(e) {
    showToast('Ошибка синхронизации', 'error');
  }
}

// ===== НАСТРОЙКИ: Переименования =====
async function loadOverrides() {
  try {
    const data = await apiCall('/api/overrides');
    const tbody = document.getElementById('overridesTable');
    tbody.innerHTML = '';
    data.forEach(o => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${o.ticker}</td>
        <td>${o.name}</td>
        <td><button onclick="removeOverride('${o.ticker}')">Удалить</button></td>
      `;
      tbody.appendChild(tr);
    });
  } catch(e) {
    console.error(e);
    showToast('Ошибка загрузки переименований', 'error');
  }
}

async function addOverride() {
  const ticker = document.getElementById('newTicker').value.trim();
  const name = document.getElementById('newName').value.trim();
  if (!ticker || !name) { showToast('Заполните оба поля', 'error'); return; }
  try {
    await apiCall('/api/override', 'POST', { action: 'add', ticker, display_name: name });
    document.getElementById('newTicker').value = '';
    document.getElementById('newName').value = '';
    loadOverrides();
    showToast('Переименование добавлено', 'success');
  } catch(e) {
    showToast('Ошибка добавления', 'error');
  }
}

async function removeOverride(ticker) {
  if (!confirm(`Удалить переименование для ${ticker}?`)) return;
  try {
    await apiCall('/api/override', 'POST', { action: 'remove', ticker });
    loadOverrides();
    showToast('Переименование удалено', 'success');
  } catch(e) {
    showToast('Ошибка удаления', 'error');
  }
}

// ===== НАСТРОЙКИ: Привязка тикеров =====
async function loadUntickedOperations() {
  try {
    const data = await apiCall('/api/operations/unticked');
    const tbody = document.getElementById('untickedTable');
    tbody.innerHTML = '';
    const operations = data.operations || [];
    if (operations.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5">Нет операций без тикера</td></tr>';
      return;
    }
    const tickers = data.available_tickers || [];
    allTickersList = tickers;

    operations.forEach(op => {
      const tr = document.createElement('tr');
      const tdDate = document.createElement('td'); tdDate.textContent = new Date(op.date).toLocaleDateString(); tr.appendChild(tdDate);
      const tdPayment = document.createElement('td'); tdPayment.textContent = op.payment.toLocaleString(); tr.appendChild(tdPayment);
      const tdCurrent = document.createElement('td'); tdCurrent.textContent = op.ticker || '—'; tr.appendChild(tdCurrent);
      const tdSelect = document.createElement('td');
      const select = document.createElement('select');
      select.className = 'ticker-select';
      select.dataset.opid = op.id;
      const defaultOpt = document.createElement('option'); defaultOpt.value = ''; defaultOpt.textContent = 'Выберите'; select.appendChild(defaultOpt);
      tickers.forEach(t => {
        const opt = document.createElement('option'); opt.value = t; opt.textContent = t; select.appendChild(opt);
      });
      tdSelect.appendChild(select);
      tr.appendChild(tdSelect);
      const tdAction = document.createElement('td');
      const btn = document.createElement('button');
      btn.className = 'btn-success';
      btn.textContent = 'Привязать';
      btn.style.padding = '4px 10px';
      btn.style.border = 'none';
      btn.style.borderRadius = '6px';
      btn.style.cursor = 'pointer';
      btn.style.fontSize = '10px';
      btn.addEventListener('click', () => linkTicker(op.id));
      tdAction.appendChild(btn);
      tr.appendChild(tdAction);
      tbody.appendChild(tr);
    });
  } catch(e) {
    console.error(e);
    showToast('Ошибка загрузки операций без тикера', 'error');
  }
}

async function linkTicker(opId) {
  const select = document.querySelector(`.ticker-select[data-opid="${opId}"]`);
  const newTicker = select.value;
  if (!newTicker) { showToast('Выберите тикер', 'error'); return; }
  try {
    await apiCall('/api/operations/link', 'POST', { id: opId, ticker: newTicker });
    loadUntickedOperations();
    loadDividends();
    showToast('Тикер привязан', 'success');
  } catch(e) {
    showToast('Ошибка привязки', 'error');
  }
}

// ===== НАСТРОЙКИ: Сектора =====
async function loadSectors() {
  try {
    // Загружаем список секторов
    if (sectorsList.length === 0) {
      sectorsList = await apiCall('/api/sectors/list');
    }
    const data = await apiCall('/api/instruments');
    const tbody = document.getElementById('sectorsTable');
    tbody.innerHTML = '';
    data.forEach(inst => {
      const tr = document.createElement('tr');
      const tdTicker = document.createElement('td'); tdTicker.textContent = inst.ticker; tr.appendChild(tdTicker);
      const tdName = document.createElement('td'); tdName.textContent = inst.name || inst.ticker; tr.appendChild(tdName);
      const tdSector = document.createElement('td');
      const select = document.createElement('select');
      select.className = 'sector-select';
      select.dataset.ticker = inst.ticker;
      sectorsList.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s;
        opt.textContent = s;
        if (s === (inst.sector || 'Прочие')) opt.selected = true;
        select.appendChild(opt);
      });
      tdSector.appendChild(select);
      tr.appendChild(tdSector);
      const tdAction = document.createElement('td');
      const btn = document.createElement('button');
      btn.className = 'btn-success';
      btn.textContent = 'Сохранить';
      btn.style.padding = '4px 10px';
      btn.style.border = 'none';
      btn.style.borderRadius = '6px';
      btn.style.cursor = 'pointer';
      btn.style.fontSize = '10px';
      btn.addEventListener('click', () => updateSector(inst.ticker));
      tdAction.appendChild(btn);
      tr.appendChild(tdAction);
      tbody.appendChild(tr);
    });
  } catch(e) {
    console.error(e);
    showToast('Ошибка загрузки секторов', 'error');
  }
}

async function updateSector(ticker) {
  const select = document.querySelector(`.sector-select[data-ticker="${ticker}"]`);
  const newSector = select.value;
  if (!newSector) { showToast('Выберите сектор', 'error'); return; }
  try {
    await apiCall('/api/sector', 'POST', { ticker, sector: newSector });
    showToast('Сектор обновлён', 'success');
    loadPortfolio();
  } catch(e) {
    showToast('Ошибка обновления сектора', 'error');
  }
}

// ===== ИНИЦИАЛИЗАЦИЯ =====
window.onload = function() {
  loadPortfolio();
  apiCall('/api/sync-status').then(data => {
    if (data.last_sync) {
      document.getElementById('lastSync').textContent = new Date(data.last_sync).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' });
    }
  }).catch(() => {});
};