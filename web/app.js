(() => {
  // ---------- утилиты ----------
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const fmt = (n, d = 2) => (n == null || isNaN(n) ? "—" : Number(n).toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const sign = (n, d = 2) => (n > 0 ? "+" : "") + fmt(n, d);
  const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "muted");
  const time = (ts) => new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const date = (ts) => new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const STATUS = { active: "работает", paused: "пауза", fired: "уволен", intern: "стажёр", dropped: "отчислен" };
  const ACTION = { BUY: "войти в BTC", SELL: "выйти в USDT", HOLD: "держать" };
  function tradeCell(x) {
    if (x.trade_side) return `<span class="${x.trade_side === "BUY" ? "up" : "down"}">${x.trade_side === "BUY" ? "купил" : "продал"} ${fmt(x.trade_qty, 5)} BTC</span>`;
    if (!x.executed) return `<span class="warn">${esc(x.blocked_by || "заблокировано")}</span>`;
    const was = x.exposure_before == null ? null : Math.round(x.exposure_before * 100);
    return `<span class="dim">без сделки${was != null ? `, уже ${was}% в BTC` : ""}</span>`;
  }
  const KIND = { fire: "увольнение", hire: "найм", intern: "стажёры", drop: "отчисление", stop: "стоп-лосс", demote: "в стажёры", weekly: "ротация", live_ready: "к реальным торгам", head: "руководитель", pause: "пауза", halt: "стоп", report: "отчёт", lesson: "урок", retune: "настройка", research: "исследование", start: "старт", error: "ошибка", approval: "решение" };

  const TABS = ["home", "team", "interns", "lab", "reports"];
  let state = null, tab = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "home", equityData = null, summary = null, families = null, range = "all";
  let chartPoints = [];
  const openRows = new Set();
  let showInternTrades = false;
  const TZ = "Asia/Almaty";   // Астана, UTC+5
  const astana = (d = new Date()) => d.toLocaleString("ru-RU", { timeZone: TZ, day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }).replace(",", "");
  const astanaTime = (d) => d.toLocaleTimeString("ru-RU", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
  window.astanaClock = () => "Астана " + astana();
  function nextCandleInfo() {
    const now = Date.now() / 1000;
    const up = (state && state.upcoming || []).filter((u) => u.ts > now - 60);
    if (!up.length) return "ждём первую свечу";
    const first = up[0];
    const left = Math.max(0, first.ts - now);
    const m = Math.floor(left / 60), sec = Math.floor(left % 60);
    const others = up.slice(1, 3).map((u) => `${u.name.replace(/\s*\(.*\)/, "")} в ${astanaTime(new Date(u.ts * 1000))}`).join(", ");
    const head = left < 60 ? `${first.name.replace(/\s*\(.*\)/, "")} проверяет рынок вот-вот` : `${first.name.replace(/\s*\(.*\)/, "")} проверит рынок через ${m} мин ${String(sec).padStart(2, "0")} с, в ${astanaTime(new Date(first.ts * 1000))}`;
    return head + (others ? `; далее ${others}` : "");
  }
  function tickClock() {
    const el = $("#clock"); if (!el) return;
    const halted = state && state.department.halted;
    el.innerHTML = `<span class="num">${astana()}</span> по Астане · ${halted ? "отдел остановлен до завтра" : nextCandleInfo()}`;
  }
  setInterval(tickClock, 1000);

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error((await r.text()) || r.status);
    return r.json();
  }

  // ---------- шапка ----------
  function renderTop(s) {
    const d = s.department;
    $("#meta").textContent = `${s.market} · BTC ${fmt(s.price, 0)} $ · обновлено ${s.last_poll_ts ? astanaTime(new Date(s.last_poll_ts * 1000)) : "—"}` + (s.error ? ` · ${s.error}` : "") + (d.halted ? " · ОТДЕЛ ОСТАНОВЛЕН" : "");
    $("#dot").className = "dot " + (s.error ? "err" : "ok");
  }

  // ---------- главная ----------
  function heroHTML(s) {
    const d = s.department;
    const pct = d.start ? (d.pnl / d.start) * 100 : 0;
    const team = s.agents.filter((a) => a.status !== "fired");
    const day = team.reduce((x, a) => x + a.pnl_day, 0);
    return `<section class="hero fade">
      <div class="label">Капитал отдела · демосчёт</div>
      <div class="big num">${fmt(d.equity)} <small>$</small></div>
      <div class="delta num ${cls(day)}">${sign(day)} $ <span>за сегодня</span></div>
      <div class="delta num ${cls(d.pnl)}">${sign(d.pnl)} $ (${sign(pct, 2)}%) <span>за всё время</span></div>
    </section>`;
  }

  // ---------- лог сделок за 24 часа (текстом) ----------
  const btc = (q) => Number(q).toLocaleString("ru-RU", { minimumFractionDigits: 5, maximumFractionDigits: 5 }) + " BTC";
  function tradeLine(t) {
    const who = `<b>${esc(t.agent)}</b>${t.kind === "intern" ? ' <span class="dim">(котёнок)</span>' : ""}`;
    const stop = /стоп-лосс/i.test(t.reason || "") ? ' <span class="tag sell">стоп-лосс</span>' : "";
    if (t.side === "BUY") return `<li><time>${hhmm(t.ts)}</time><span>${who} <span class="up">купил</span> <span class="num">${btc(t.qty)}</span> по <span class="num">${fmt(t.price, 0)} $</span></span></li>`;
    const res = t.pnl == null ? ' · <span class="dim">итог не записан</span>' : ` · итог <b class="num ${cls(t.pnl)}">${sign(t.pnl)} $${t.pnl_pct != null ? ` (${sign(t.pnl_pct, 2)}%)` : ""}</b>`;
    return `<li><time>${hhmm(t.ts)}</time><span>${who} <span class="down">продал</span> <span class="num">${btc(t.qty)}</span> по <span class="num">${fmt(t.price, 0)} $</span>${res}${stop}</span></li>`;
  }
  function tradesHTML(s) {
    const all = s.trades_24h || [];
    const list = showInternTrades ? all : all.filter((t) => t.kind !== "intern");
    const buys = list.filter((t) => t.side === "BUY").length, sells = list.length - buys;
    const closed = list.filter((t) => t.side === "SELL" && t.pnl != null);
    const total = closed.reduce((x, t) => x + t.pnl, 0);
    const wins = closed.filter((t) => t.pnl > 0).length;
    return `<div class="card fade"><div class="cardhead"><h3>Сделки за 24 часа</h3>
        <label class="toggle"><input type="checkbox" id="toggle-interns" ${showInternTrades ? "checked" : ""}> показывать котят</label></div>
      <div class="note num">${list.length ? `${buys} покупок · ${sells} продаж${closed.length ? ` · закрыто ${closed.length}, в плюсе ${wins} · итог закрытых <b class="${cls(total)}">${sign(total)} $</b>` : ""}` : "за последние сутки сделок не было: коты ждут сигнала"}</div>
      ${list.length ? `<ul class="tlog">${list.slice(0, 60).map(tradeLine).join("")}</ul>` : ""}</div>`;
  }

  function allocHTML(s) {
    const team = s.agents.filter((a) => a.status !== "fired");
    const btcv = team.reduce((x, a) => x + a.equity * a.exposure, 0);
    const total = team.reduce((x, a) => x + a.equity, 0) || 1;
    const usdt = total - btcv;
    const r = 52, c = 2 * Math.PI * r, pb = btcv / total;
    const rows = [...team].sort((a, b) => b.equity - a.equity).map((a) => `
      <div class="row link" data-name="${esc(a.name)}"><i style="background:${a.exposure > 0 ? "var(--btc)" : "var(--usdt)"}"></i>
        <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(a.name)}</span>
        <b class="num">${fmt(a.equity, 0)} $</b><span class="pct num">${fmt(a.exposure * 100, 0)}% в BTC</span></div>`).join("");
    return `<div class="card fade"><h3>Распределение капитала</h3>
      <div class="alloc">
        <svg viewBox="0 0 130 130" role="img" aria-label="Доля BTC и USDT">
          <circle cx="65" cy="65" r="${r}" fill="none" stroke="var(--card2)" stroke-width="14"/>
          <circle cx="65" cy="65" r="${r}" fill="none" stroke="var(--btc)" stroke-width="14" stroke-dasharray="${c * pb} ${c}" stroke-dashoffset="${c / 4}" stroke-linecap="butt"/>
          <text x="65" y="61" text-anchor="middle" fill="var(--text)" font-size="15" font-weight="700">${fmt(pb * 100, 0)}%</text>
          <text x="65" y="78" text-anchor="middle" fill="var(--muted)" font-size="10">в BTC</text>
        </svg>
        <div class="rows">
          <div class="row"><i style="background:var(--btc)"></i><span>В биткоине</span><b class="num">${fmt(btcv, 0)} $</b><span class="pct num">${fmt(pb * 100, 1)}%</span></div>
          <div class="row"><i style="background:var(--usdt)"></i><span>В долларах (USDT)</span><b class="num">${fmt(usdt, 0)} $</b><span class="pct num">${fmt((1 - pb) * 100, 1)}%</span></div>
          <div class="note" style="margin-top:4px">По котам (оранжевая точка: сейчас держит BTC):</div>
          ${rows}
        </div></div></div>`;
  }

  function homeHTML(s) {
    return heroHTML(s) + approvalsHTML(s) + tradesHTML(s) + `
      <div class="card chart fade"><h3>Результат отдела, $</h3>
        <div class="range"><button data-r="day" class="${range === "day" ? "active" : ""}">День</button><button data-r="week" class="${range === "week" ? "active" : ""}">Неделя</button><button data-r="all" class="${range === "all" ? "active" : ""}">Всё время</button></div>
        <canvas id="chart"></canvas><div class="tip" id="tip"></div><div class="note" id="chart-note"></div></div>
      ${allocHTML(s)}
      <div class="card fade"><h3>Последние события</h3><ul class="events">${eventsHTML(s.events.slice(0, 8))}</ul></div>`;
  }

  // ---------- раскрывающаяся строка агента ----------
  const mm = (m) => ":" + String(m).padStart(2, "0");
  const hhmm = (ts) => (ts ? astanaTime(new Date(ts * 1000)) : "—");
  const llmBroken = (a) => /LLM недоступен|нет ключа|бюджет/i.test(a.last_reason || "");
  function agentRow(a, opts = {}) {
    const isIntern = a.status === "intern";
    const nameShort = esc(a.name);
    const nextIn = a.next_decision_ts ? Math.max(0, Math.round((a.next_decision_ts - Date.now() / 1000) / 60)) : null;
    return `<details class="acc ${a.status}" data-name="${nameShort}">
      <summary>
        <div class="acc-name">${catSVG(a.name, isIntern ? "intern" : "team", 36)}<div><b>${nameShort}</b><span class="state ${llmBroken(a) ? "fired" : a.status !== "active" && a.status !== "intern" ? a.status : a.exposure > 0 ? "inpos" : "wait"}">${llmBroken(a) ? "ошибка нейросети" : a.status === "paused" ? "пауза" : a.status === "fired" ? "уволен" : a.status === "dropped" ? "отчислен" : a.exposure > 0 ? `в BTC ${fmt(a.exposure * 100, 0)}%` : a.decided_at ? `ждёт сигнала · ${hhmm(a.decided_at)}` : "ещё не решал"}</span><span class="reason">${esc(a.last_reason || "")}</span></div></div>
        <div class="acc-badges">${a.live_ready ? '<span class="pill gold">готов к реальным</span>' : ""}${a.trial_weeks ? `<span class="pill">испытание · ${a.trial_weeks}-я нед.</span>` : ""}${!isIntern && a.streak_weeks > 0 ? `<span class="pill green">серия ${a.streak_weeks} нед.</span>` : ""}</div>
        <div class="acc-time num" title="Как часто агент смотрит на рынок">${a.strategy.startsWith("llm_") ? "сам" : a.cadence_minutes >= 60 ? "1 ч" : a.cadence_minutes + " м"}</div>
        <div class="acc-pnl num ${cls(a.pnl_24h)}">${sign(a.pnl_24h)} $<small>24 ч</small></div>
        <div class="acc-pnl num ${cls(a.pnl_total)}">${sign(a.pnl_total)} $<small>всего</small></div>
        <div class="acc-arrow">›</div>
      </summary>
      <div class="acc-body">
        <div class="note">${esc(a.description || a.strategy)}</div>
        <div class="kv">
          <div><span>Капитал</span><b class="num">${fmt(a.equity)} $</b></div>
          <div><span>Сегодня</span><b class="num ${cls(a.pnl_day)}">${sign(a.pnl_day)} $</b></div>
          <div><span>В BTC</span><b class="num">${fmt(a.exposure * 100, 0)}%</b></div>
          <div><span>Просадка</span><b class="num">${fmt(a.drawdown * 100, 1)}%</b></div>
          <div><span>Сделок · побед</span><b class="num">${a.trades} · ${fmt(a.win_rate * 100, 0)}%</b></div>
          <div><span>${isIntern ? "На стажировке" : "В команде"}</span><b class="num">${a.days} дн.</b></div>
          <div><span>За эту неделю</span><b class="num ${cls(a.pnl_week)}">${sign(a.pnl_week)} $</b></div>
          <div><span>Недель в плюсе подряд</span><b class="num">${isIntern ? "—" : a.streak_weeks}</b></div>
          <div><span>Смотрит на рынок</span><b class="num">${a.strategy.startsWith("llm_") ? "сам решает когда" : a.cadence_minutes >= 60 ? "раз в час" : `каждые ${a.cadence_minutes} мин`}</b></div>
          <div><span>Последняя проверка</span><b class="num">${hhmm(a.decided_at)}</b></div>
          <div><span>Следующая</span><b class="num">${hhmm(a.next_decision_ts)}${nextIn != null ? ` <span class="muted">(через ${nextIn} мин)</span>` : ""}</b></div>
          ${a.stop_price ? `<div><span>Стоп-лосс</span><b class="num down">${fmt(a.stop_price, 0)} $</b></div>` : ""}
          ${a.alert_above || a.alert_below ? `<div><span>Будильники по цене</span><b class="num">${a.alert_above ? "выше " + fmt(a.alert_above, 0) : ""}${a.alert_above && a.alert_below ? " · " : ""}${a.alert_below ? "ниже " + fmt(a.alert_below, 0) : ""}</b></div>` : ""}
        </div>
        <div class="bar"><i style="width:${Math.round(a.exposure * 100)}%"></i></div>
        <div class="last"><span class="tag">${ACTION[a.last_action] || "—"}</span> ${esc(a.last_reason || "решений ещё не было")}</div>
        <div style="margin-top:8px"><button class="btn open-agent" data-name="${nameShort}">Открыть карточку и журнал</button></div>
      </div>
    </details>`;
  }

  // ---------- команда ----------
  function agentCard(a) {
    return `<div class="card agent" data-name="${esc(a.name)}">
      <div class="head"><span class="name">${esc(a.name)}</span><span class="badge ${a.status}">${STATUS[a.status] || a.status}</span></div>
      <div class="strategy">${esc(a.description || a.strategy)}</div>
      <div class="row"><span>Капитал</span><b class="num">${fmt(a.equity)} $</b></div>
      <div class="row"><span>Всего</span><b class="num ${cls(a.pnl_total)}">${sign(a.pnl_total)} $</b></div>
      <div class="row"><span>Сегодня</span><b class="num ${cls(a.pnl_day)}">${sign(a.pnl_day)} $</b></div>
      <div class="row"><span>Просадка · сделок</span><span class="num">${fmt(a.drawdown * 100, 1)}% · ${a.trades}</span></div>
      <div class="row"><span>В BTC</span><span class="num">${fmt(a.exposure * 100, 0)}%</span></div>
      <div class="bar"><i style="width:${Math.round(a.exposure * 100)}%"></i></div>
      <div class="reason" title="${esc(a.last_reason)}">${esc(a.last_reason || "")}</div></div>`;
  }
  function teamHTML(s) {
    const alive = s.agents.filter((a) => a.status !== "fired");
    const fired = s.agents.filter((a) => a.status === "fired");
    const sorted = [...alive].sort((a, b) => b.pnl_total - a.pnl_total);
    return `<h2 class="sec">Коты · основная команда · ${alive.length} из ${s.team_size}</h2>
      <div class="note" style="margin-bottom:8px">Каждый кот торгует своими 1000 $ по своей теории и сам решает, как часто смотреть на рынок: колонка «Ритм». По понедельникам ротация: минус за неделю отправляет в котята, три недели в плюсе подряд дают статус «готов к реальным». ${(s.risk_per_trade || 1) < 1 ? `Размер позиции считается от риска: на одной сделке кот может потерять не больше ${fmt(s.risk_per_trade * 100, 0)}% капитала до стоп-лосса.` : "Размер позиции задаёт сама стратегия, потолок " + fmt((s.max_exposure || 1) * 100, 0) + "% капитала."} У каждой позиции есть стоп-лосс, он проверяется каждую минуту.</div>
      <div class="card list"><div class="acc-head"><span>Агент</span><span>Ритм</span><span>За 24 ч</span><span>За всё время</span><span></span></div>${sorted.map((a) => agentRow(a)).join("")}</div>
      ${fired.length ? `<h2 class="sec">Уволенные · ${fired.length}</h2><div class="card list">${fired.map((a) => agentRow(a)).join("")}</div>` : ""}`;
  }

  // ---------- стажёры ----------
  function internsHTML(s) {
    const names = new Set(s.interns.map((a) => a.name));
    const feed = (s.decisions || []).filter((d) => names.has(d.agent)).slice(0, 25);
    const ready = s.interns.filter((a) => a.days >= 14).length;
    const inBtc = s.interns.filter((a) => a.exposure > 0).length;
    const traded = s.interns.filter((a) => a.trades > 0).length;
    const best = s.interns[0];
    const nextUp = [...s.interns].filter((a) => a.next_decision_ts).sort((a, b) => a.next_decision_ts - b.next_decision_ts)[0];
    return `<h2 class="sec">Котята · стажёры · ${s.interns.length} из ${s.intern_count}</h2>
      <div class="card"><div class="stat">
        <div><div class="k">В позиции (в BTC)</div><div class="v num">${inBtc} <span class="muted" style="font-size:13px">из ${s.interns.length}</span></div></div>
        <div><div class="k">Уже торговали</div><div class="v num">${traded}</div></div>
        <div><div class="k">Прошли 14 дней</div><div class="v num">${ready}</div></div>
        <div><div class="k">Лучший</div><div class="v num ${cls(best?.pnl_total)}">${best ? sign(best.pnl_total) + " $" : "—"}</div><div class="note">${best ? esc(best.name) : ""}</div></div>
        <div><div class="k">Следующий решает</div><div class="v" style="font-size:14px">${nextUp ? `${esc(nextUp.name)} в ${hhmm(nextUp.next_decision_ts)}` : "—"}</div></div>
      </div>
      <div class="note" style="margin-top:10px">Котята торгуют в тени на своих демосчетах по 1000 $. Котёнок без единой сделки три дня отчисляется, его место занимает кандидат другого семейства. Каждый понедельник ротация: коты из команды с минусом за неделю (до трёх худших) уходят в котята на испытательный срок, а лучшие котята с плюсом за неделю занимают их места со свежим счётом. Котёнок с минусом две недели подряд отчисляется. Кот, три недели подряд в плюсе, становится кандидатом на реальный счёт.</div></div>
      <div class="card list"><h3>Рейтинг · нажмите на строку</h3><div class="acc-head"><span>Стажёр</span><span>Ритм</span><span>За 24 ч</span><span>За всё время</span><span></span></div>${s.interns.map((a) => agentRow(a)).join("")}</div>
      <div class="card"><h3>Что они делают прямо сейчас · последние решения</h3>${feed.length ? `<ul class="feed">${feed.map((d) => `<li><time>${hhmm(d.ts)}</time><b>${esc(d.agent)}</b><span class="tag ${d.action === "BUY" ? "buy" : d.action === "SELL" ? "sell" : ""}">${ACTION[d.action] || d.action}${d.trade_side ? (d.trade_side === "BUY" ? " · купил" : " · продал") : ""}</span><span class="muted">${esc(d.reason)}</span></li>`).join("")}</ul>` : '<div class="note">Первые решения появятся в ближайший час, у каждого стажёра в свою минуту.</div>'}</div>`;
  }

  // ---------- наука ----------
  function labHTML(s) {
    const bench = s.bench || [];
    const fam = families || [];
    const ev = s.events.filter((e) => ["retune", "lesson", "research", "intern", "drop"].includes(e.kind)).slice(0, 15);
    return `<h2 class="sec">Отдел исследований и обучения</h2>
      <div class="card"><div class="stat">
        <div><div class="k">Семейств стратегий</div><div class="v num">${fam.length}</div></div>
        <div><div class="k">На правилах</div><div class="v num">${fam.filter((f) => !f.llm).length}</div></div>
        <div><div class="k">С нейросетью</div><div class="v num">${fam.filter((f) => f.llm).length}</div></div>
        <div><div class="k">Кандидатов</div><div class="v num">${bench.length}</div></div>
      </div>
      <div class="note" style="margin-top:10px">Что делает отдел: перебирает семейства и их параметры на реальной истории за 30 дней, отбирает кандидатов в стажёры, раз в неделю перепроверяет параметры команды, а из ошибок нейро-агентов раз в сутки формулирует уроки.</div>
      <div style="margin-top:10px"><button class="btn" id="btn-research">Запустить исследование</button></div></div>
      <div class="card"><h3>Кандидаты (лучшие по бэктесту)</h3>${bench.length ? `<div class="tbl"><table><tr><th>Семейство</th><th>Параметры</th><th class="r">Доход</th><th class="r">Просадка</th><th class="r">Оценка</th></tr>` +
        bench.map((b) => `<tr><td>${esc(b.strategy)}</td><td>${Object.entries(b.params).map(([k, v]) => `<span class="tag">${k}=${v}</span>`).join("")}</td><td class="r num ${cls(b.stats.return_pct)}">${sign(b.stats.return_pct, 1)}%</td><td class="r num">${fmt(b.stats.max_drawdown_pct, 1)}%</td><td class="r num">${fmt(b.score, 1)}</td></tr>`).join("") + "</table></div>" : `<div class="note">Кандидаты появятся после исследования.</div>`}</div>
      <div class="card"><h3>Библиотека стратегий</h3><div class="tbl"><table>${fam.map((f) => `<tr><td><b>${esc(f.label)}</b>${f.llm ? ' <span class="badge intern">нейросеть</span>' : ""}<div class="note">${esc(f.description)}</div></td></tr>`).join("")}</table></div></div>
      <div class="card"><h3>Журнал обучения</h3><ul class="events">${eventsHTML(ev) || '<li class="muted">пока пусто</li>'}</ul></div>`;
  }

  // ---------- отчёты ----------
  function reportsHTML(s) {
    const sm = summary || { daily: [], reports: [] };
    const team = s.agents.filter((a) => a.status !== "fired");
    const day = team.reduce((x, a) => x + a.pnl_day, 0);
    const bestDay = [...team].sort((a, b) => b.pnl_day - a.pnl_day)[0];
    const worstDay = [...team].sort((a, b) => a.pnl_day - b.pnl_day)[0];
    const allRows = [...s.agents].sort((a, b) => b.pnl_total - a.pnl_total).map((a) => `<tr class="link" data-name="${esc(a.name)}"><td>${esc(a.name)} <span class="badge ${a.status}">${STATUS[a.status]}</span></td><td class="r num ${cls(a.pnl_total)}">${sign(a.pnl_total)} $</td><td class="r num ${cls(a.pnl_day)}">${sign(a.pnl_day)} $</td><td class="r num">${a.trades}</td><td class="r num">${fmt(a.win_rate * 100, 0)}%</td><td class="r num">${fmt(a.drawdown * 100, 1)}%</td></tr>`).join("");
    const daily = [...sm.daily].reverse().map((d) => `<tr><td>${d.day.slice(5).split("-").reverse().join(".")}</td><td class="r num">${fmt(d.equity)} $</td><td class="r num ${cls(d.change)}">${d.change == null ? "—" : sign(d.change) + " $"}</td></tr>`).join("");
    const reports = sm.reports.map((e) => { let data = {}; try { data = JSON.parse(e.data || "{}"); } catch (_) {} return `<div class="report"><div class="t">${time(e.ts)}</div><div>${esc(e.message)}</div>${(data.recommendations || []).length ? `<ul>${data.recommendations.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}</div>`; }).join("");
    const h = s.head || {};
    const REG = { up: "рост", flat: "боковик", down: "падение", off: "выключено" };
    const weeks = (h.weeks || []).slice(-6).reverse();
    return `<h2 class="sec">Отчёты</h2>
      ${approvalsHTML(s)}
      <div class="card"><h3>Руководитель отдела</h3><div class="stat">
        <div><div class="k">Подход</div><div class="v" style="font-size:15px">${h.mode === "defensive" ? "защитный" : "обычный"}</div></div>
        <div><div class="k">Режим рынка</div><div class="v" style="font-size:15px">${REG[h.regime] || "—"}</div></div>
        <div><div class="k">Потолок доли</div><div class="v num">${fmt((h.cap ?? 1) * 100, 0)}%</div></div>
        <div><div class="k">Порог сделки</div><div class="v num">${fmt((h.min_rebalance ?? 0.05) * 100, 0)}%</div></div>
        <div><div class="k">Недель хуже долларов подряд</div><div class="v num ${h.fail_weeks ? "down" : ""}">${h.fail_weeks ?? 0}</div></div>
      </div>
      <div class="note" style="margin-top:8px">Руководитель отвечает за результат: каждый день оценивает рынок и выставляет потолок доли, каждую неделю сравнивает отдел с «просто держать доллары» и «просто держать биткоин». Две недели подряд хуже долларов, и он переходит на защитный подход и переобучает всех.</div>
      ${weeks.length ? `<div class="tbl" style="margin-top:8px"><table><tr><th>Неделя</th><th class="r">Отдел</th><th class="r">Биткоин</th><th class="r">Комиссии</th></tr>${weeks.map((w) => `<tr><td>${date(w.ts)}</td><td class="r num ${cls(w.dept)}">${sign(w.dept, 2)}%</td><td class="r num ${cls(w.btc)}">${sign(w.btc, 2)}%</td><td class="r num">${fmt(w.fees)} $</td></tr>`).join("")}</table></div>` : '<div class="note" style="margin-top:6px">Первое недельное сравнение появится в понедельник.</div>'}</div>
      <div class="card"><h3>Нейросеть</h3><div class="stat">
        <div><div class="k">Модель</div><div class="v" style="font-size:14px">${s.llm ? esc(s.llm_model) : "нет ключа"}</div></div>
        <div><div class="k">Расход сегодня</div><div class="v num">${fmt(s.llm_spend?.usd ?? 0, 2)} $ <span class="muted" style="font-size:12px">из ${fmt(s.llm_spend?.budget ?? 0, 2)} $</span></div></div>
        <div><div class="k">Вызовов сегодня</div><div class="v num">${s.llm_spend?.calls ?? 0}</div></div>
        <div><div class="k">Агентов и котят</div><div class="v num">${s.agents.filter((a) => a.status !== "fired").length} · ${s.interns.length}</div></div>
      </div>${s.llm_error ? `<div class="note down" style="margin-top:8px">Последняя ошибка (${hhmm(s.llm_error.ts)}): ${esc(s.llm_error.text)}</div>` : ""}${s.llm && !(s.llm_spend?.calls) ? '<div class="note warn" style="margin-top:8px">Сегодня ни одного вызова нейросети. Если это не начало суток, проверьте ключ и баланс в консоли Anthropic.</div>' : ""}</div>
      <div class="card"><h3>Сегодня</h3><div class="stat">
        <div><div class="k">Результат дня</div><div class="v num ${cls(day)}">${sign(day)} $</div></div>
        <div><div class="k">Лучший сегодня</div><div class="v" style="font-size:14px">${bestDay ? `${esc(bestDay.name)} <span class="num ${cls(bestDay.pnl_day)}">${sign(bestDay.pnl_day)}</span>` : "—"}</div></div>
        <div><div class="k">Худший сегодня</div><div class="v" style="font-size:14px">${worstDay ? `${esc(worstDay.name)} <span class="num ${cls(worstDay.pnl_day)}">${sign(worstDay.pnl_day)}</span>` : "—"}</div></div>
      </div></div>
      <div class="card"><h3>Отчёты руководителя</h3>${reports || '<div class="note">Первый отчёт появится в конце дня.</div>'}</div>
      <div class="card"><h3>Капитал по дням</h3>${daily ? `<div class="tbl"><table><tr><th>День</th><th class="r">Капитал</th><th class="r">Изменение</th></tr>${daily}</table></div>` : '<div class="note">Появится после первого дня.</div>'}</div>
      <div class="card"><h3>За всё время по агентам</h3><div class="tbl"><table><tr><th>Агент</th><th class="r">Всего</th><th class="r">Сегодня</th><th class="r">Сделок</th><th class="r">Побед</th><th class="r">Просадка</th></tr>${allRows}</table></div></div>
      <div class="card"><h3>Все события</h3><ul class="events">${eventsHTML(s.events)}</ul></div>`;
  }

  // ---------- общие куски ----------
  function eventsHTML(list) {
    return list.map((e) => `<li><time>${time(e.ts)}</time><span class="kind ${e.kind}">${KIND[e.kind] || e.kind}</span><span>${esc(e.message)}</span></li>`).join("");
  }
  function approvalsHTML(s) {
    if (!s.approvals.length) return "";
    return s.approvals.map((p) => `<div class="card approval fade"><div><b>Нужно ваше решение</b><div>${esc(p.title)}</div><div class="note">${time(p.ts)}${p.details.agent_pnl != null ? ` · агент ${sign(p.details.agent_pnl)} $, стажёр ${sign(p.details.intern_pnl)} $` : p.details.pnl != null ? ` · ${sign(p.details.pnl)} $` : ""}</div></div>
      <div class="actions"><button class="btn primary" data-id="${p.id}" data-d="approve">Одобрить</button><button class="btn" data-id="${p.id}" data-d="reject">Отклонить</button></div></div>`).join("");
  }

  // ---------- график капитала ----------
  async function drawChart() {
    const canvas = $("#chart"); if (!canvas) return;
    if (!equityData) equityData = await api("/api/curve");
    let pts = equityData.map((p) => [p.ts, p.pnl]);
    chartPoints = pts.slice(-168);
    const last = pts.length ? pts[pts.length - 1][0] : 0;
    if (range === "day") pts = pts.filter((p) => p[0] >= last - 86400);
    if (range === "week") pts = pts.filter((p) => p[0] >= last - 7 * 86400);
    const note = $("#chart-note");
    const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth, H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr); ctx.clearRect(0, 0, W, H);
    if (pts.length < 2) { note.textContent = "График появится после нескольких часов работы."; return; }
    const ys = pts.map((p) => p[1]); const min = Math.min(...ys), max = Math.max(...ys), pad = (max - min) * 0.15 || 1;
    const L = 8, R = 8, T = 8, B = 18;
    const x = (i) => L + (i / (pts.length - 1)) * (W - L - R);
    const y = (v) => T + (1 - (v - (min - pad)) / (max - min + 2 * pad)) * (H - T - B);
    ctx.strokeStyle = "#232a3a"; ctx.lineWidth = 1;
    [0.25, 0.5, 0.75].forEach((f) => { ctx.beginPath(); ctx.moveTo(L, T + (H - T - B) * f); ctx.lineTo(W - R, T + (H - T - B) * f); ctx.stroke(); });
    const upc = ys[ys.length - 1] >= ys[0];
    const grad = ctx.createLinearGradient(0, T, 0, H - B); grad.addColorStop(0, upc ? "rgba(52,210,123,.25)" : "rgba(255,107,107,.25)"); grad.addColorStop(1, "rgba(0,0,0,0)");
    ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1])))); ctx.lineTo(x(pts.length - 1), H - B); ctx.lineTo(x(0), H - B); ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
    ctx.beginPath(); ctx.strokeStyle = upc ? "#34d27b" : "#ff6b6b"; ctx.lineWidth = 2; pts.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1])))); ctx.stroke();
    ctx.fillStyle = "#8a93a8"; ctx.font = "10px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.textAlign = "left"; ctx.fillText(time(pts[0][0]), L, H - 4); ctx.textAlign = "right"; ctx.fillText(time(pts[pts.length - 1][0]), W - R, H - 4);
    note.textContent = `результат отдела: мин ${sign(min)} $ · макс ${sign(max)} $ · за период ${sign(ys[ys.length - 1] - ys[0])} $`;
    const tip = $("#tip");
    const onMove = (ev) => {
      const b = canvas.getBoundingClientRect(); const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - b.left;
      const i = Math.max(0, Math.min(pts.length - 1, Math.round(((px - L) / (W - L - R)) * (pts.length - 1))));
      tip.style.display = "block"; tip.innerHTML = `${time(pts[i][0])}<br><b class="num">${sign(pts[i][1])} $</b>`;
      tip.style.left = Math.min(W - 130, Math.max(0, x(i) - 60)) + "px"; tip.style.top = Math.max(0, y(pts[i][1]) - 44) + "px";
    };
    canvas.onmousemove = onMove; canvas.ontouchstart = onMove; canvas.ontouchmove = onMove;
    canvas.onmouseleave = () => (tip.style.display = "none");
  }

  // ---------- карточка агента ----------
  async function openAgent(name) {
    const d = await api(`/api/agents/${encodeURIComponent(name)}`);
    const a = d.agent;
    $("#modal-box").innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><div style="display:flex;align-items:center;gap:10px">${catSVG(a.name, a.status === "intern" ? "intern" : "team", 48)}<div><b style="font-size:18px">${esc(a.name)}</b> <span class="badge ${a.status}">${STATUS[a.status]}</span></div></div><button class="btn" id="modal-close">Закрыть</button></div>
      <div class="note" style="margin-top:6px">${esc(d.description)}</div>
      <div class="note">Параметры: ${Object.entries(a.params).map(([k, v]) => `<span class="tag">${k}=${v}</span>`).join("")}</div>
      <div class="stat" style="margin-top:12px">
        <div><div class="k">Капитал</div><div class="v num">${fmt(a.equity)} $</div></div>
        <div><div class="k">Всего</div><div class="v num ${cls(a.pnl_total)}">${sign(a.pnl_total)} $</div></div>
        <div><div class="k">Сегодня</div><div class="v num ${cls(a.pnl_day)}">${sign(a.pnl_day)} $</div></div>
        <div><div class="k">Просадка</div><div class="v num">${fmt(a.drawdown * 100, 1)}%</div></div>
        <div><div class="k">Сделок · побед</div><div class="v num">${a.trades} · ${fmt(a.win_rate * 100, 0)}%</div></div>
        <div><div class="k">Смотрит на рынок</div><div class="v num" style="font-size:14px">${a.strategy.startsWith("llm_") ? "сам решает когда" : a.cadence_minutes >= 60 ? "раз в час" : `каждые ${a.cadence_minutes} мин`}</div></div>
      </div>
      ${d.lessons.length ? `<h3 style="margin:14px 0 6px;font-size:13px;color:var(--muted);text-transform:uppercase">Уроки из журнала</h3><ul style="padding-left:18px;font-size:13px">${d.lessons.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}
      <h3 style="margin:14px 0 6px;font-size:13px;color:var(--muted);text-transform:uppercase">Последние решения</h3>
      <div class="note" style="margin-bottom:6px">«Цель» это какую долю капитала агент хочет держать в BTC. Сделка происходит только если цель отличается от текущего состояния. В журнал попадают сделки, смена цели и контрольная запись раз в час; промежуточные проверки без изменений не записываются. «Цена через 4 ч» заполняется с задержкой.</div>
      <div class="tbl"><table><tr><th>Время</th><th>Цель</th><th>Сделка</th><th class="r">Цена BTC</th><th class="r">Цена через 4 ч</th><th>Обоснование</th></tr>
      ${d.decisions.map((x) => `<tr><td class="num">${time(x.ts)}</td><td>${ACTION[x.action] || x.action}<div class="dim">${fmt(x.target_exposure * 100, 0)}% в BTC</div></td><td>${tradeCell(x)}</td><td class="r num">${fmt(x.price, 0)}</td><td class="r num ${cls(x.outcome_pct)}">${x.outcome_pct == null ? "—" : sign(x.outcome_pct) + "%"}</td><td class="note">${esc(x.reason)}</td></tr>`).join("")}</table></div>
      ${a.status !== "fired" && a.status !== "dropped" ? `<p><button class="btn danger" id="modal-fire">${a.status === "intern" ? "Отчислить стажёра" : "Уволить агента"}</button></p>` : ""}`;
    $("#modal").classList.add("open");
    $("#modal-close").onclick = () => $("#modal").classList.remove("open");
    const f = $("#modal-fire");
    if (f) f.onclick = async () => { if (confirm(`${a.status === "intern" ? "Отчислить" : "Уволить"} ${a.name}?`)) { await api(`/api/agents/${encodeURIComponent(name)}/fire`, { method: "POST" }); $("#modal").classList.remove("open"); refresh(true); } };
  }

  // ---------- рендер вкладки ----------
  function render() {
    if (!state) return;
    const view = $("#view");
    const html = tab === "home" ? homeHTML(state) : tab === "team" ? teamHTML(state) : tab === "interns" ? internsHTML(state) : tab === "lab" ? labHTML(state) : reportsHTML(state);
    view.innerHTML = `<div class="fade">${html}</div>`;
    tickClock();
    $$("[data-name]", view).forEach((el) => { if (!el.classList.contains("acc")) el.addEventListener("click", (e) => { if (e.target.closest(".acc")) return; openAgent(el.dataset.name); }); });
    $$(".open-agent", view).forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); openAgent(b.dataset.name); }));
    // помнить, какие строки раскрыты, между обновлениями
    $$("details.acc", view).forEach((d) => { if (openRows.has(d.dataset.name)) d.open = true; d.addEventListener("toggle", () => { if (d.open) openRows.add(d.dataset.name); else openRows.delete(d.dataset.name); }); });
    $$(".approval button", view).forEach((b) => b.addEventListener("click", async () => { await api(`/api/approvals/${b.dataset.id}/${b.dataset.d}`, { method: "POST" }); refresh(true); }));
    $$(".range button", view).forEach((b) => b.addEventListener("click", () => { range = b.dataset.r; $$(".range button", view).forEach((x) => x.classList.toggle("active", x === b)); drawChart(); }));
    const rb = $("#btn-research", view);
    if (rb) rb.onclick = async () => { rb.disabled = true; rb.textContent = "Считаю, около минуты…"; try { await api("/api/research", { method: "POST" }); } finally { rb.disabled = false; rb.textContent = "Запустить исследование"; refresh(true); } };
    if (tab === "home") { drawChart(); const tg = $("#toggle-interns", view); if (tg) tg.onchange = () => { showInternTrades = tg.checked; render(); }; }
  }

  async function refresh(force) {
    try {
      const [st, sm, fam] = await Promise.all([api("/api/state"), (tab === "reports" || !summary || force) ? api("/api/summary") : summary, families || api("/api/families")]);
      state = st; summary = sm; families = fam; equityData = null;
      renderTop(state); render();
    } catch (e) {
      $("#meta").textContent = "нет связи с сервером: " + e.message; $("#dot").className = "dot err";
    }
  }

  $$("#tabs button").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.tab; history.replaceState(null, "", "#" + tab); $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b)); window.scrollTo(0, 0); render(); if (tab === "reports") refresh(true); }));
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $("#btn-tick").onclick = async () => { const b = $("#btn-tick"); b.disabled = true; try { await api("/api/tick", { method: "POST" }); } finally { b.disabled = false; refresh(true); } };
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").classList.remove("open"); });
  window.addEventListener("resize", () => { if (tab === "home") drawChart(); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  refresh();
  setInterval(() => refresh(false), 60000);
})();
