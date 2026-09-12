(() => {
  const $ = (s) => document.querySelector(s);
  const fmt = (n, d = 2) => (n == null ? "—" : Number(n).toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const sign = (n) => (n > 0 ? "+" : "") + fmt(n);
  const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "muted");
  const time = (ts) => new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const STATUS = { active: "работает", paused: "пауза", fired: "уволен", intern: "стажёр", dropped: "отчислен" };
  const KIND = { fire: "увольнение", hire: "найм", intern: "стажёры", drop: "отчисление", pause: "пауза", halt: "стоп", report: "отчёт", lesson: "урок", retune: "настройка", research: "исследование", start: "старт", error: "ошибка", approval: "решение" };

  let state = null;

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  function renderSummary(s) {
    const d = s.department;
    $("#summary").innerHTML = `
      <div class="stat"><div class="label">Капитал</div><div class="value">${fmt(d.equity)} $</div></div>
      <div class="stat"><div class="label">Результат</div><div class="value ${cls(d.pnl)}">${sign(d.pnl)} $</div></div>
      <div class="stat"><div class="label">BTC</div><div class="value">${fmt(s.price, 0)} $</div></div>
      <div class="stat"><div class="label">Агентов</div><div class="value">${d.agents_active}<span class="muted" style="font-size:13px"> из ${s.team_size}${d.agents_paused ? `, ${d.agents_paused} на паузе` : ""}</span></div></div>
      <div class="stat"><div class="label">Нейросеть</div><div class="value" style="font-size:15px">${s.llm ? "подключена" : "нет ключа, 2 агента ждут"}</div></div>`;
    $("#meta").textContent = `${s.symbol} ${s.timeframe} · ${s.market} · последняя свеча ${s.last_tick_ts ? time(s.last_tick_ts) : "—"}` + (s.error ? ` · ошибка: ${s.error}` : "");
    $("#dot").className = "dot " + (s.error ? "err" : "ok");
    if (d.halted) $("#meta").textContent += " · ОТДЕЛ ОСТАНОВЛЕН ДО КОНЦА ДНЯ";
  }

  function renderAgents(s) {
    $("#agents").innerHTML = s.agents
      .filter((a) => a.status !== "fired")
      .concat(s.agents.filter((a) => a.status === "fired"))
      .map((a) => `
      <div class="card agent" data-name="${a.name}">
        <div class="head"><span class="name">${a.name}</span><span class="badge ${a.status}">${STATUS[a.status] || a.status}</span></div>
        <div class="strategy">${a.strategy}</div>
        <div class="row"><span>Капитал</span><b>${fmt(a.equity)} $</b></div>
        <div class="row"><span>Всего</span><b class="${cls(a.pnl_total)}">${sign(a.pnl_total)} $</b></div>
        <div class="row"><span>Сегодня</span><b class="${cls(a.pnl_day)}">${sign(a.pnl_day)} $</b></div>
        <div class="row"><span>Просадка</span><span>${fmt(a.drawdown * 100, 1)}%</span></div>
        <div class="row"><span>Сделок · побед</span><span>${a.trades} · ${fmt(a.win_rate * 100, 0)}%</span></div>
        <div class="row"><span>В BTC</span><span>${fmt(a.exposure * 100, 0)}%</span></div>
        <div class="bar"><i style="width:${Math.round(a.exposure * 100)}%"></i></div>
        <div class="reason" title="${a.last_reason}">${a.last_reason || ""}</div>
      </div>`).join("");
    document.querySelectorAll(".agent").forEach((el) => el.addEventListener("click", () => openAgent(el.dataset.name)));
  }

  function renderApprovals(s) {
    const block = $("#approvals-block");
    if (!s.approvals.length) { block.hidden = true; return; }
    block.hidden = false;
    $("#approvals").innerHTML = s.approvals.map((p) => `
      <div class="card approval">
        <div><b>${p.title}</b><div class="note">${time(p.ts)} · ${JSON.stringify(p.details).slice(0, 160)}</div></div>
        <div class="actions"><button class="primary" data-id="${p.id}" data-d="approve">Одобрить</button><button data-id="${p.id}" data-d="reject">Отклонить</button></div>
      </div>`).join("");
    document.querySelectorAll("#approvals button").forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/approvals/${b.dataset.id}/${b.dataset.d}`, { method: "POST" });
      refresh();
    }));
  }

  function renderInterns(s) {
    $("#interns-count").textContent = `${s.interns.length} из ${s.intern_count}`;
    if (!s.interns.length) { $("#interns").innerHTML = `<span class="muted">Стажёры появятся после ближайшего часа.</span>`; return; }
    $("#interns").innerHTML = `<div style="overflow-x:auto"><table><tr><th>Стажёр</th><th>Стратегия</th><th>Дней</th><th>Всего</th><th>Просадка</th><th>Сделок</th><th>В BTC</th></tr>` +
      s.interns.map((a) => `<tr class="agent-row" data-name="${a.name}" style="cursor:pointer"><td>${a.name}</td><td class="muted">${a.strategy}</td><td>${a.days}</td>
        <td class="${cls(a.pnl_total)}">${sign(a.pnl_total)} $</td><td>${fmt(a.drawdown * 100, 1)}%</td><td>${a.trades}</td><td>${fmt(a.exposure * 100, 0)}%</td></tr>`).join("") + "</table></div>";
    document.querySelectorAll(".agent-row").forEach((el) => el.addEventListener("click", () => openAgent(el.dataset.name)));
  }

  function renderBench(s) {
    if (!s.bench.length) { $("#bench").innerHTML = `<span class="muted">Скамейка пуста. Нажмите «Исследование», чтобы подобрать кандидатов.</span>`; return; }
    $("#bench").innerHTML = `<table><tr><th>Стратегия</th><th>Параметры</th><th>Доходность</th><th>Просадка</th><th>Sharpe</th><th>Оценка</th></tr>` +
      s.bench.map((b) => `<tr><td>${b.strategy}</td><td>${Object.entries(b.params).map(([k, v]) => `<span class="tag">${k}=${v}</span>`).join("")}</td>
        <td class="${cls(b.stats.return_pct)}">${sign(b.stats.return_pct)}%</td><td>${fmt(b.stats.max_drawdown_pct, 1)}%</td><td>${fmt(b.stats.sharpe, 2)}</td><td>${fmt(b.score, 2)}</td></tr>`).join("") + "</table>";
  }

  function renderEvents(s) {
    $("#events").innerHTML = s.events.map((e) => `<li><time>${time(e.ts)}</time><span class="kind ${e.kind}">${KIND[e.kind] || e.kind}</span><span>${e.message}</span></li>`).join("");
  }

  async function drawChart() {
    const data = await api("/api/equity");
    const canvas = $("#chart");
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);
    // Суммарный капитал отдела по времени (только живые агенты на каждый момент).
    const byTs = new Map();
    Object.values(data).forEach((series) => series.forEach((p) => byTs.set(p.ts, (byTs.get(p.ts) || 0) + p.equity)));
    const pts = [...byTs.entries()].sort((a, b) => a[0] - b[0]);
    if (pts.length < 2) { $("#chart-note").textContent = "График появится после нескольких часов работы."; return; }
    const ys = pts.map((p) => p[1]);
    const min = Math.min(...ys), max = Math.max(...ys), pad = (max - min) * 0.1 || 1;
    const x = (i) => 8 + (i / (pts.length - 1)) * (W - 16);
    const y = (v) => H - 8 - ((v - (min - pad)) / (max - min + 2 * pad)) * (H - 16);
    ctx.strokeStyle = "#262b36"; ctx.lineWidth = 1;
    [0.25, 0.5, 0.75].forEach((f) => { ctx.beginPath(); ctx.moveTo(0, H * f); ctx.lineTo(W, H * f); ctx.stroke(); });
    ctx.beginPath(); ctx.strokeStyle = ys[ys.length - 1] >= ys[0] ? "#2ecc71" : "#ff5c5c"; ctx.lineWidth = 2;
    pts.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1]))));
    ctx.stroke();
    $("#chart-note").textContent = `${time(pts[0][0])} → ${time(pts[pts.length - 1][0])} · мин ${fmt(min)} · макс ${fmt(max)}`;
  }

  async function openAgent(name) {
    const d = await api(`/api/agents/${encodeURIComponent(name)}`);
    const a = d.agent;
    $("#modal-box").innerHTML = `
      <div class="head" style="display:flex;justify-content:space-between;align-items:center"><h1 style="margin:0">${a.name}</h1><button id="modal-close">Закрыть</button></div>
      <div class="note">${a.strategy} · ${d.description}</div>
      <div class="note">Параметры: ${Object.entries(a.params).map(([k, v]) => `<span class="tag">${k}=${v}</span>`).join("")}</div>
      <p>Капитал <b>${fmt(a.equity)} $</b> · всего <b class="${cls(a.pnl_total)}">${sign(a.pnl_total)} $</b> · просадка ${fmt(a.drawdown * 100, 1)}% · сделок ${a.trades}</p>
      ${d.lessons.length ? `<h2>Уроки из журнала</h2><ul>${d.lessons.map((l) => `<li>${l}</li>`).join("")}</ul>` : ""}
      <h2>Последние решения</h2>
      <table><tr><th>Время</th><th>Решение</th><th>Доля</th><th>Цена</th><th>Через 4ч</th><th>Обоснование</th></tr>
      ${d.decisions.map((x) => `<tr><td>${time(x.ts)}</td><td>${x.action}${x.executed ? "" : ` <span class="tag">${x.blocked_by || "не исполнено"}</span>`}</td><td>${fmt(x.target_exposure * 100, 0)}%</td><td>${fmt(x.price, 0)}</td><td class="${cls(x.outcome_pct)}">${x.outcome_pct == null ? "—" : sign(x.outcome_pct) + "%"}</td><td>${x.reason}</td></tr>`).join("")}</table>
      ${a.status !== "fired" && a.status !== "dropped" ? `<p><button class="danger" id="modal-fire">${a.status === "intern" ? "Отчислить стажёра" : "Уволить агента"}</button></p>` : ""}`;
    $("#modal").classList.add("open");
    $("#modal-close").onclick = () => $("#modal").classList.remove("open");
    const f = $("#modal-fire");
    if (f) f.onclick = async () => { if (confirm(`Уволить ${a.name}?`)) { await api(`/api/agents/${encodeURIComponent(name)}/fire`, { method: "POST" }); $("#modal").classList.remove("open"); refresh(); } };
  }

  async function refresh() {
    try {
      state = await api("/api/state");
      renderSummary(state); renderAgents(state); renderApprovals(state); renderInterns(state); renderBench(state); renderEvents(state);
      drawChart();
    } catch (e) {
      $("#meta").textContent = "нет связи с сервером: " + e.message;
      $("#dot").className = "dot err";
    }
  }

  $("#btn-tick").onclick = async () => { $("#btn-tick").disabled = true; try { await api("/api/tick", { method: "POST" }); } finally { $("#btn-tick").disabled = false; refresh(); } };
  $("#btn-research").onclick = async () => { $("#btn-research").disabled = true; $("#btn-research").textContent = "Считаю…"; try { await api("/api/research", { method: "POST" }); } finally { $("#btn-research").disabled = false; $("#btn-research").textContent = "Исследование"; refresh(); } };
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").classList.remove("open"); });

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  refresh();
  setInterval(refresh, 60000);
})();
