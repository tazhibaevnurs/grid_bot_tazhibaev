"use strict";

// Локальный дашборд: обычный polling через fetch, без WebSocket.
const REFRESH_MS = 7000;

const state = {
  page: 0,
  pageSize: 25,
  side: "",
  from: "",
  to: "",
  sortKey: "timestamp",
  sortDir: "desc",
  range: "all",
  total: 0,
};

let equityChart = null;

// --- утилиты форматирования ------------------------------------------------

function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString("ru-RU", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtSigned(v, digits = 2) {
  if (v === null || v === undefined) return "—";
  const s = fmtNum(v, digits);
  return v > 0 ? "+" + s : s;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ru-RU", { hour12: false });
}

function pnlClass(v) {
  if (v === null || v === undefined) return "";
  return v > 0 ? "pos" : v < 0 ? "neg" : "";
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

// --- статистика / карточки -------------------------------------------------

async function loadStats() {
  let s;
  try {
    s = await getJSON("/api/stats");
  } catch (e) {
    return;
  }

  const cur = s.quote_currency || "";
  document.getElementById("card-equity").textContent =
    s.current_equity === null ? "—" : `${fmtNum(s.current_equity)} ${cur}`;
  document.getElementById("card-equity-sub").textContent =
    `старт: ${s.start_equity === null ? "—" : fmtNum(s.start_equity)}`;

  const pnlEl = document.getElementById("card-pnl");
  pnlEl.textContent = s.pnl_abs === null ? "—" : `${fmtSigned(s.pnl_abs)} ${cur}`;
  pnlEl.className = "card-value mono " + pnlClass(s.pnl_abs);
  const pctEl = document.getElementById("card-pnl-pct");
  pctEl.textContent = s.pnl_pct === null ? "—" : `${fmtSigned(s.pnl_pct)} %`;
  pctEl.className = "card-sub mono " + pnlClass(s.pnl_pct);

  document.getElementById("card-winrate").textContent =
    s.win_rate === null ? "—" : `${fmtNum(s.win_rate, 1)} %`;
  document.getElementById("card-winrate-sub").textContent =
    `закрытых: ${s.closed_trades}`;

  document.getElementById("card-trades").textContent = s.total_trades;
  const realEl = document.getElementById("card-realized");
  realEl.textContent = `реализ. P&L: ${fmtSigned(s.realized_pnl_total)}`;
  realEl.className = "card-sub mono " + pnlClass(s.realized_pnl_total);

  const ddEl = document.getElementById("card-drawdown");
  ddEl.textContent = s.drawdown_pct === null ? "—" : `${fmtNum(s.drawdown_pct, 2)} %`;
  ddEl.className = "card-value mono " + (s.drawdown_pct > 0 ? "neg" : "");
  document.getElementById("card-peak").textContent =
    `пик: ${s.peak_equity === null ? "—" : fmtNum(s.peak_equity)}`;

  const modeTag = s.multi_symbol_mode ? "MULTI" : "SINGLE";
  document.getElementById("card-mode").textContent = s.market_type
    ? `${s.market_type.toUpperCase()} · ${modeTag}`
    : modeTag;
  const levTxt = s.market_type === "futures" && s.leverage ? `плечо ${s.leverage}x` : "без плеча";
  document.getElementById("card-leverage").textContent =
    `символов: ${s.active_symbol_count ?? 0} · ${levTxt}`;

  renderCategoryBreakdown(s.categories || []);

  // Статус-индикатор.
  const dot = document.getElementById("status-dot");
  const txt = document.getElementById("status-text");
  dot.className = "dot " + s.status;
  txt.textContent =
    s.status === "running"
      ? "RUNNING"
      : s.status === "killed"
        ? "KILLED"
        : s.status === "stale"
          ? "STALE"
          : "STOPPED";

  renderProcesses(s.processes || []);

  // Баннер демо-режима.
  const banner = document.getElementById("dry-run-banner");
  banner.classList.toggle("hidden", s.dry_run !== true);
}

const CATEGORY_LABELS = {
  volume: "объём",
  gainer: "рост",
  volatile: "волатильн.",
  manual: "ручной",
};

function renderCategoryBreakdown(categories) {
  const el = document.getElementById("cat-breakdown");
  if (!el) return;
  if (!categories.length) {
    el.textContent = "";
    return;
  }
  el.innerHTML = categories
    .map((c) => {
      const label = CATEGORY_LABELS[c.category] || c.category;
      const pnlCls = pnlClass(c.pnl_abs);
      return `<span class="cat-chip cat-${c.category}">${label}: ${c.count}</span>
        <span class="cat-eq mono">${fmtNum(c.equity)} <span class="${pnlCls}">(${fmtSigned(
        c.pnl_abs
      )})</span></span>`;
    })
    .join('<span class="cat-sep">·</span>');
}

async function loadActiveSymbols() {
  let data;
  try {
    data = await getJSON("/api/active-symbols");
  } catch (e) {
    return;
  }
  const body = document.getElementById("symbols-body");
  if (!body) return;
  body.innerHTML = "";
  if (!data.symbols.length) {
    body.innerHTML =
      '<tr><td colspan="8" class="empty-row">Нет активных тикеров</td></tr>';
    return;
  }
  for (const s of data.symbols) {
    const tr = document.createElement("tr");
    const catLabel = CATEGORY_LABELS[s.category] || s.category;
    const statusCls = s.status === "wind_down" ? "wind" : "active";
    const statusLabel = s.status === "wind_down" ? "wind-down" : "active";
    tr.innerHTML = `
      <td class="sym-name">${escapeHtml(s.symbol)}</td>
      <td><span class="cat-chip cat-${s.category}">${catLabel}</span></td>
      <td>${s.start_equity === null ? "—" : fmtNum(s.start_equity)}</td>
      <td>${s.current_equity === null ? "—" : fmtNum(s.current_equity)}</td>
      <td class="${pnlClass(s.pnl_abs)}">${s.pnl_abs === null ? "—" : fmtSigned(s.pnl_abs)}</td>
      <td class="${pnlClass(s.pnl_pct)}">${s.pnl_pct === null ? "—" : fmtSigned(s.pnl_pct) + " %"}</td>
      <td>${s.trades}</td>
      <td><span class="tag ${statusCls}">${statusLabel}</span></td>
    `;
    body.appendChild(tr);
  }
}

function processStatusLabel(status) {
  const map = {
    running: "RUNNING",
    stopped: "STOPPED",
    stale: "STALE",
    offline: "OFFLINE",
  };
  return map[status] || status.toUpperCase();
}

function renderProcesses(processes) {
  const grid = document.getElementById("processes-grid");
  if (!grid) return;
  grid.innerHTML = "";
  if (!processes.length) {
    grid.innerHTML = '<div class="process-empty">Нет данных о процессах</div>';
    return;
  }
  for (const p of processes) {
    const card = document.createElement("div");
    card.className = "process-card";
    card.innerHTML = `
      <div class="process-head">
        <span class="dot ${p.status}"></span>
        <span class="process-name">${escapeHtml(p.label || p.name)}</span>
        <span class="process-status mono">${processStatusLabel(p.status)}</span>
      </div>
      <div class="process-meta mono">
        <div>PID: ${p.pid ?? "—"}</div>
        <div>Старт: ${fmtTime(p.started_at)}</div>
        <div>Pulse: ${fmtTime(p.last_heartbeat)}${
          p.heartbeat_age_sec != null ? ` (${p.heartbeat_age_sec}s)` : ""
        }</div>
        <div>${p.detail ? escapeHtml(p.detail) : ""}</div>
      </div>
    `;
    grid.appendChild(card);
  }
}

// --- график equity ---------------------------------------------------------

function rangeToFrom(range) {
  if (range === "all") return null;
  const now = Date.now();
  const ms = range === "24h" ? 24 * 3600e3 : 7 * 24 * 3600e3;
  return new Date(now - ms).toISOString();
}

async function loadEquity() {
  let url = "/api/equity-history";
  const from = rangeToFrom(state.range);
  if (from) url += `?from=${encodeURIComponent(from)}`;
  let data;
  try {
    data = await getJSON(url);
  } catch (e) {
    return;
  }
  // Категориальная ось X (метки времени) — без зависимости от time-адаптера Chart.js.
  const labels = data.equity.map((r) => fmtTime(r.timestamp));
  const values = data.equity.map((r) => r.equity);
  const ctx = document.getElementById("equity-chart");

  if (!equityChart) {
    equityChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Equity",
            data: values,
            borderColor: "#2dd4bf",
            backgroundColor: "rgba(45,212,191,0.12)",
            borderWidth: 2,
            pointRadius: 0,
            fill: true,
            tension: 0.2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: {
          x: {
            ticks: { color: "#6b7c8c", maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
            grid: { color: "rgba(31,44,56,0.6)" },
          },
          y: {
            ticks: { color: "#6b7c8c" },
            grid: { color: "rgba(31,44,56,0.6)" },
          },
        },
        plugins: { legend: { display: false } },
      },
    });
  } else {
    equityChart.data.labels = labels;
    equityChart.data.datasets[0].data = values;
    equityChart.update("none");
  }
}

// --- таблица сделок --------------------------------------------------------

function buildTradesURL() {
  const p = new URLSearchParams();
  p.set("limit", state.pageSize);
  p.set("offset", state.page * state.pageSize);
  if (state.side) p.set("side", state.side);
  if (state.from) p.set("from", new Date(state.from).toISOString());
  if (state.to) {
    const d = new Date(state.to);
    d.setHours(23, 59, 59, 999);
    p.set("to", d.toISOString());
  }
  return "/api/trades?" + p.toString();
}

function sortRows(rows) {
  const { sortKey, sortDir } = state;
  const dir = sortDir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    let va = a[sortKey];
    let vb = b[sortKey];
    if (va === null || va === undefined) va = -Infinity;
    if (vb === null || vb === undefined) vb = -Infinity;
    if (typeof va === "string" && typeof vb === "string") {
      return va.localeCompare(vb) * dir;
    }
    return (va - vb) * dir;
  });
}

async function loadTrades() {
  let data;
  try {
    data = await getJSON(buildTradesURL());
  } catch (e) {
    return;
  }
  state.total = data.total;
  const rows = sortRows(data.trades);
  const body = document.getElementById("trades-body");
  body.innerHTML = "";

  for (const t of rows) {
    const tr = document.createElement("tr");
    const pnl = t.realized_pnl;
    tr.innerHTML = `
      <td>${fmtTime(t.timestamp)}</td>
      <td><span class="tag ${t.side}">${t.side}</span></td>
      <td>${fmtNum(t.price, 2)}</td>
      <td>${fmtNum(t.amount, 6)}</td>
      <td>${fmtNum(t.quote_value, 2)}</td>
      <td class="${pnlClass(pnl)}">${pnl === null || pnl === undefined ? "—" : fmtSigned(pnl, 4)}</td>
      <td><span class="tag ${t.dry_run ? "demo" : "live"}">${t.dry_run ? "demo" : "live"}</span></td>
    `;
    body.appendChild(tr);
  }

  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  document.getElementById("page-info").textContent =
    `стр. ${state.page + 1} / ${totalPages} · всего ${state.total}`;
  document.getElementById("page-prev").disabled = state.page <= 0;
  document.getElementById("page-next").disabled = state.page >= totalPages - 1;
}

// --- события ----------------------------------------------------------------

async function loadEvents() {
  let data;
  try {
    data = await getJSON("/api/events?limit=80");
  } catch (e) {
    return;
  }
  const list = document.getElementById("events-list");
  list.innerHTML = "";
  for (const ev of data.events) {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="ev-level ${ev.level}">${ev.level}</span>
      <span class="ev-msg">${escapeHtml(ev.message)}</span>
      <span class="ev-time">${fmtTime(ev.timestamp)}</span>
    `;
    list.appendChild(li);
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

// --- пульт управления ------------------------------------------------------

// Пока пользователь крутит контролы, не перетираем их фоновым обновлением.
let controlDirty = false;
let controlStatusTimer = null;

async function postJSON(url) {
  const res = await fetch(url, { method: "POST" });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) data.ok = false;
  return data;
}

function showControlStatus(msg, ok = true) {
  const el = document.getElementById("control-status");
  if (!el) return;
  el.textContent = msg;
  el.style.color = ok ? "var(--accent)" : "var(--red)";
  if (controlStatusTimer) clearTimeout(controlStatusTimer);
  controlStatusTimer = setTimeout(() => { el.textContent = ""; }, 6000);
}

async function loadControl() {
  if (controlDirty) return; // не мешаем пользователю редактировать
  let data;
  try {
    data = await getJSON("/api/control");
  } catch (e) {
    return;
  }
  const s = data.settings;

  const symInput = document.getElementById("sym-count");
  if (document.activeElement !== symInput) symInput.value = s.max_symbols;

  const levInput = document.getElementById("lev-count");
  if (document.activeElement !== levInput) levInput.value = s.leverage > 1 ? s.leverage : 2;

  // Тумблер рынка.
  document.querySelectorAll("#market-toggle button").forEach((b) => {
    b.classList.toggle("active", b.dataset.market === s.market_type);
  });
  document.getElementById("leverage-row").style.display =
    s.market_type === "futures" ? "flex" : "none";

  // Профили стратегии.
  const optsEl = document.getElementById("strategy-options");
  if (!optsEl.dataset.rendered) {
    optsEl.innerHTML = data.profiles
      .map(
        (p) => `<button class="strategy-opt" data-profile="${p.id}">
          <div class="so-label">${escapeHtml(p.label)}</div>
          <div class="so-desc">${escapeHtml(p.description)}</div>
        </button>`
      )
      .join("");
    optsEl.dataset.rendered = "1";
    optsEl.querySelectorAll(".strategy-opt").forEach((btn) => {
      btn.addEventListener("click", () => {
        optsEl.querySelectorAll(".strategy-opt").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        controlDirty = true;
      });
    });
  }
  optsEl.querySelectorAll(".strategy-opt").forEach((b) => {
    b.classList.toggle("active", b.dataset.profile === s.risk_profile);
  });

  // Кнопки процесса по статусу бота.
  const running = s.bot_status === "running" || s.bot_status === "stale";
  document.getElementById("bot-start").disabled = running;
  document.getElementById("bot-stop").disabled = !running;
}

function setupControlPanel() {
  const symInput = document.getElementById("sym-count");
  document.getElementById("sym-minus").addEventListener("click", () => {
    symInput.value = Math.max(1, (parseInt(symInput.value, 10) || 8) - 1);
    controlDirty = true;
  });
  document.getElementById("sym-plus").addEventListener("click", () => {
    symInput.value = Math.min(30, (parseInt(symInput.value, 10) || 8) + 1);
    controlDirty = true;
  });
  symInput.addEventListener("input", () => { controlDirty = true; });

  document.getElementById("sym-apply").addEventListener("click", async () => {
    const n = Math.max(1, Math.min(30, parseInt(symInput.value, 10) || 8));
    const r = await postJSON(`/api/control/symbols?count=${n}`);
    showControlStatus(r.message || "Готово", r.ok !== false);
    controlDirty = false;
    refreshAll();
  });

  document.getElementById("rescan-now").addEventListener("click", async () => {
    const n = Math.max(1, Math.min(30, parseInt(symInput.value, 10) || 8));
    const r = await postJSON(`/api/control/symbols?count=${n}`);
    showControlStatus("Пересканирование запрошено.", r.ok !== false);
    controlDirty = false;
  });

  // Тумблер рынка.
  document.querySelectorAll("#market-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#market-toggle button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById("leverage-row").style.display =
        btn.dataset.market === "futures" ? "flex" : "none";
      controlDirty = true;
    });
  });
  document.getElementById("lev-count").addEventListener("input", () => { controlDirty = true; });

  document.getElementById("market-apply").addEventListener("click", async () => {
    const market = document.querySelector("#market-toggle button.active").dataset.market;
    const lev = Math.max(1, Math.min(50, parseInt(document.getElementById("lev-count").value, 10) || 2));
    showControlStatus("Переключение рынка, перезапуск бота...");
    const r = await postJSON(`/api/control/market?market_type=${market}&leverage=${lev}`);
    showControlStatus(r.message || "Готово", r.ok !== false);
    controlDirty = false;
    setTimeout(refreshAll, 1500);
  });

  document.getElementById("strategy-apply").addEventListener("click", async () => {
    const active = document.querySelector(".strategy-opt.active");
    if (!active) { showControlStatus("Выберите профиль", false); return; }
    showControlStatus("Смена стратегии, перезапуск бота...");
    const r = await postJSON(`/api/control/strategy?profile=${active.dataset.profile}`);
    showControlStatus(r.message || "Готово", r.ok !== false);
    controlDirty = false;
    setTimeout(refreshAll, 1500);
  });

  // Процесс бота.
  const botAction = async (action) => {
    showControlStatus("Выполняется: " + action + "...");
    const r = await postJSON(`/api/bot/${action}`);
    showControlStatus(r.message || "Готово", r.ok !== false);
    setTimeout(refreshAll, 1500);
  };
  document.getElementById("bot-start").addEventListener("click", () => botAction("start"));
  document.getElementById("bot-stop").addEventListener("click", () => botAction("stop"));
  document.getElementById("bot-restart").addEventListener("click", () => botAction("restart"));
}

// --- refresh / события UI ---------------------------------------------------

async function refreshAll() {
  await Promise.allSettled([
    loadStats(),
    loadEquity(),
    loadActiveSymbols(),
    loadControl(),
    loadTrades(),
    loadEvents(),
  ]);
  document.getElementById("last-update").textContent =
    "обновлено " + new Date().toLocaleTimeString("ru-RU", { hour12: false });
}

function setupUI() {
  document.getElementById("refresh-secs").textContent = String(REFRESH_MS / 1000);

  // Диапазон графика.
  document.querySelectorAll("#range-switch button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#range-switch button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.range = btn.dataset.range;
      loadEquity();
    });
  });

  // Сортировка таблицы.
  document.querySelectorAll("#trades-table thead th").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.sortKey === key) {
        state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
      } else {
        state.sortKey = key;
        state.sortDir = "desc";
      }
      document.querySelectorAll("#trades-table thead th").forEach((h) => {
        const base = h.textContent.replace(/[▲▼]\s*$/, "").trim();
        h.textContent = base;
      });
      th.textContent = th.textContent + (state.sortDir === "asc" ? " ▲" : " ▼");
      loadTrades();
    });
  });

  // Фильтры.
  document.getElementById("filter-apply").addEventListener("click", () => {
    state.side = document.getElementById("filter-side").value;
    state.from = document.getElementById("filter-from").value;
    state.to = document.getElementById("filter-to").value;
    state.page = 0;
    loadTrades();
  });
  document.getElementById("filter-reset").addEventListener("click", () => {
    state.side = state.from = state.to = "";
    document.getElementById("filter-side").value = "";
    document.getElementById("filter-from").value = "";
    document.getElementById("filter-to").value = "";
    state.page = 0;
    loadTrades();
  });

  // Пагинация.
  document.getElementById("page-prev").addEventListener("click", () => {
    if (state.page > 0) { state.page--; loadTrades(); }
  });
  document.getElementById("page-next").addEventListener("click", () => {
    const totalPages = Math.ceil(state.total / state.pageSize);
    if (state.page < totalPages - 1) { state.page++; loadTrades(); }
  });
}

setupUI();
setupControlPanel();
refreshAll();
setInterval(refreshAll, REFRESH_MS);
