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
  const STATUS = { active: "работает", paused: "пауза", fired: "уволен", intern: "стажёр", dropped: "отчислен", experiment: "эксперимент" };
  const ACTION = { BUY: "войти в BTC", SELL: "выйти в USDT", HOLD: "держать" };
  const RANK = ["Стажёр", "Трейдер", "Старший трейдер", "Реальный счёт"];
  const DESK = { bulls: "Быки", bears: "Медведи", both: "Двусторонние" };
  const REG = { up: "рост", flat: "боковик", down: "падение", off: "выключено" };
  const KIND = { alert: "сигнал", briefing: "брифинг", capital: "капитал", live: "реальный счёт", owner: "владелец", knowledge: "база знаний", rule: "правило", reviser: "ревизор", strategist: "стратег", fire: "увольнение", hire: "найм", intern: "стажёры", drop: "отчисление", stop: "стоп-лосс", demote: "в стажёры", weekly: "ротация", live_ready: "к реальным торгам", head: "директор", rank: "звание", analytics: "аналитика", funding: "финансирование", liquidation: "ликвидация", pause: "пауза", halt: "стоп", report: "отчёт", lesson: "урок", retune: "настройка", research: "исследование", start: "старт", error: "ошибка", approval: "решение" };
  function tradeCell(x) {
    if (x.trade_side) return `<span class="${x.trade_side === "BUY" ? "up" : "down"}">${x.trade_side === "BUY" ? "купил" : "продал"} ${fmt(x.trade_qty, 5)} BTC</span>`;
    if (!x.executed) return `<span class="warn">${esc(x.blocked_by || "заблокировано")}</span>`;
    const was = x.exposure_before == null ? null : Math.round(x.exposure_before * 100);
    return `<span class="dim">без сделки${was != null ? `, уже ${was}% в BTC` : ""}</span>`;
  }

  const TABS = ["home", "desks", "interns", "company", "reports"];
  let state = null, tab = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "home", equityData = null, summary = null, families = null, range = "all";
  let candles = [], chartMode = "price", priceRange = 72, statsRange = "7d", deskSort = "pnl_total", deskOnlyPos = false, lastEventId = 0, notifyOn = false;
  let heatRange = "7d", corrData = null, labResult = null, labFamily = "", labParams = {}, labDays = 30, labBusy = false;
  try { chartMode = localStorage.getItem("botz.chart") || "price"; notifyOn = localStorage.getItem("botz.notify") === "1"; } catch (_) {}
  const openRows = new Set();
  let showInternTrades = false, showInternPos = false, showLibrary = false;
  const isShadow = (k) => k === "intern" || k === "experiment";
  const TZ = "Asia/Almaty";   // Астана, UTC+5
  const astanaTime = (d) => d.toLocaleTimeString("ru-RU", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
  const hhmm = (ts) => (ts ? astanaTime(new Date(ts * 1000)) : "—");

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error((await r.text()) || r.status);
    return r.json();
  }

  // аватар: кружок с инициалами в цвете деска
  function avatar(a, size = 36) {
    const words = String(a.name || "").replace(/\(.*?\)/g, "").trim().split(/\s+/);
    const ini = (words[0]?.[0] || "?") + (words[1]?.[0] || "");
    return `<span class="ava d-${a.desk || "bulls"}" style="width:${size}px;height:${size}px;font-size:${Math.round(size * 0.38)}px">${esc(ini.toUpperCase())}</span>`;
  }
  const rankPill = (a) => a.status === "fired" || a.status === "dropped" ? "" : a.status === "experiment" ? '<span class="pill gold">эксперимент</span>' : `<span class="pill rank-${a.rank ?? 1}">${RANK[a.rank ?? 1]}</span>`;
  const deskPill = (a) => `<span class="pill desk-${a.desk}">${DESK[a.desk] || a.desk}</span>`;

  // ---------- шапка ----------
  function renderTop(s) {
    const d = s.department;
    const h = s.head || {};
    const ago = candles.length > 25 ? candles[candles.length - 25].close : null;
    const ch = ago ? (s.price / ago - 1) * 100 : null;
    const team = s.agents.filter((a) => a.status !== "fired");
    const day = team.reduce((x, a) => x + a.pnl_day, 0);
    const risk = s.stress ? s.stress.at_risk_pct : null;
    $("#meta").innerHTML = `<b class="num">BTC ${fmt(s.price, 0)} $</b>${ch != null ? ` <span class="num ${cls(ch)}">${sign(ch, 2)}%<small> 24ч</small></span>` : ""} · <span class="pill reg-${h.regime}">${REG[h.regime] || "—"}</span> · <span class="num ${cls(day)}">${sign(day)} $ сегодня</span>${risk != null ? ` · <span class="num ${risk > 3 ? "down" : risk > 1.5 ? "warn" : "muted"}" title="Убыток до стопов при падении на 5%">под риском ${fmt(risk, 1)}%</span>` : ""} · <span class="dim">${s.market} ${s.last_poll_ts ? hhmm(s.last_poll_ts) : "—"}</span>` + (s.error ? ` · <span class="down">${esc(s.error)}</span>` : "") + (d.halted ? ' · <b class="down">КОМПАНИЯ ОСТАНОВЛЕНА</b>' : "");
    $("#dot").className = "dot " + (s.error ? "err" : "ok");
    document.title = `${sign(day)} $ · BTC ${fmt(s.price, 0)} · Botz`;
  }

  // ---------- главная ----------
  function heroHTML(s) {
    const d = s.department;
    const pct = d.start ? (d.pnl / d.start) * 100 : 0;
    const team = s.agents.filter((a) => a.status !== "fired");
    const day = team.reduce((x, a) => x + a.pnl_day, 0);
    return `<section class="hero">
      <div class="label">Botz · капитал компании · демосчёт</div>
      <div class="big num">${fmt(d.equity)} <small>$</small></div>
      <div class="delta num ${cls(day)}">${sign(day)} $ <span>за сегодня</span></div>
      <div class="delta num ${cls(d.pnl)}">${sign(d.pnl)} $ (${sign(pct, 2)}%) <span>за всё время</span></div>
    </section>`;
  }

  function desksSummaryHTML(s) {
    const h = s.head || {};
    return `<div class="desks">${(s.desks || []).map((d) => `
      <div class="card desk d-${d.key} link" data-go="desks">
        <div class="dhead"><b>${esc(d.label)}</b><span class="num muted">${d.agents} из ${d.size}</span></div>
        <div class="dval num">${fmt(d.equity, 0)} <small>$</small></div>
        <div class="drow num"><span class="${cls(d.pnl_day)}">${sign(d.pnl_day)} $</span><span class="muted">сегодня</span></div>
        <div class="drow num"><span class="${cls(d.pnl)}">${sign(d.pnl)} $</span><span class="muted">всего</span></div>
        <div class="capbar" title="Потолок доли капитала от директора"><i style="width:${Math.round(d.cap * 100)}%"></i></div>
        <div class="note num">директор выделил ${fmt(d.cap * 100, 0)}% · в позиции ${d.in_position}</div>
      </div>`).join("")}</div>
      <div class="note" style="margin-top:6px">Режим рынка по оценке директора: <b>${REG[h.regime] || "—"}</b>${h.source ? ` (${esc(h.source)})` : ""}${h.intraday ? ` · цена ушла на ${sign(h.intraday.move, 1)}% против режима «${REG[h.intraday.from]}», распределение пересмотрено в ${hhmm(h.intraday.ts)}` : ""}. Потолок показывает, какую долю своего капитала трейдеры деска могут держать в позиции. Если цена уходит против режима больше чем на 1,5%, директор пересматривает распределение сразу, не дожидаясь утра.</div>`;
  }

  // ---------- лог сделок за 24 часа (текстом) ----------
  const btc = (q) => Number(q).toLocaleString("ru-RU", { minimumFractionDigits: 5, maximumFractionDigits: 5 }) + " BTC";
  function tradeLine(t) {
    const who = `<b>${esc(t.agent)}</b>${t.kind === "intern" ? ' <span class="dim">(стажёр)</span>' : t.kind === "experiment" ? ' <span class="dim">(эксперимент)</span>' : ""}`;
    const stop = /подтянутый стоп/i.test(t.reason || "") ? ' <span class="tag buy">подтянутый стоп</span>' : /безубыток/i.test(t.reason || "") ? ' <span class="tag">стоп в безубыток</span>' : /фиксация/i.test(t.reason || "") ? ' <span class="tag buy">фиксация прибыли</span>' : /стоп-лосс/i.test(t.reason || "") ? ' <span class="tag sell">стоп-лосс</span>' : /ликвидац/i.test(t.reason || "") ? ' <span class="tag sell">ликвидация</span>' : "";
    const res = t.pnl == null ? "" : ` · итог <b class="num ${cls(t.pnl)}">${sign(t.pnl)} $${t.pnl_pct != null ? ` (${sign(t.pnl_pct, 2)}%)` : ""}</b>`;
    const after = t.pos_after || 0;
    let verb;
    if (t.side === "BUY") verb = t.pnl != null ? '<span class="up">закрыл шорт</span>' : '<span class="up">купил</span>';
    else verb = after < -1e-9 && t.pnl == null ? '<span class="short">открыл шорт</span>' : '<span class="down">продал</span>';
    const tail = t.side === "SELL" && t.pnl == null && after >= -1e-9 ? ' · <span class="dim">итог не записан</span>' : "";
    return `<li><time>${hhmm(t.ts)}</time><span>${who} ${verb} <span class="num">${btc(t.qty)}</span> по <span class="num">${fmt(t.price, 0)} $</span>${res}${tail}${stop}</span></li>`;
  }
  function tradesHTML(s) {
    const all = s.trades_24h || [];
    const list = showInternTrades ? all : all.filter((t) => !isShadow(t.kind));
    const buys = list.filter((t) => t.side === "BUY").length, sells = list.length - buys;
    const closed = list.filter((t) => t.pnl != null);
    const total = closed.reduce((x, t) => x + t.pnl, 0);
    const wins = closed.filter((t) => t.pnl > 0).length;
    return `<div class="card"><div class="cardhead"><h3>Сделки за 24 часа</h3>
        <label class="toggle"><input type="checkbox" id="toggle-interns" ${showInternTrades ? "checked" : ""}> стажёры и эксперименты</label></div>
      <div class="note num">${list.length ? `${buys} покупок · ${sells} продаж${closed.length ? ` · закрыто ${closed.length}, в плюсе ${wins} · итог закрытых <b class="${cls(total)}">${sign(total)} $</b>` : ""}` : "за последние сутки сделок не было: трейдеры ждут сигнала"}</div>
      ${list.length ? `<ul class="tlog">${list.slice(0, 60).map(tradeLine).join("")}</ul>` : ""}</div>`;
  }

  // ---------- монитор открытых позиций ----------
  const dur = (h) => (h == null ? "—" : h < 1 ? `${Math.round(h * 60)} мин` : h < 48 ? `${fmt(h, 1)} ч` : `${fmt(h / 24, 1)} дн.`);
  function positionLine(p) {
    const sideTag = p.side === "long" ? '<span class="pill desk-bulls">лонг</span>' : '<span class="pill desk-bears">шорт</span>';
    const SK = { trailing: "подтянутый стоп", breakeven: "стоп в безубытке", initial: "стоп" };
    const stop = p.stop ? `${SK[p.stop_kind] || "стоп"} <span class="num">${fmt(p.stop, 0)} $</span> <span class="dim num">(${sign(p.stop_pct, 1)}%)</span>${p.partial_taken ? ' · <span class="up">половина зафиксирована</span>' : ""}` : '<span class="dim">без стопа</span>';
    return `<li class="pos ${p.stop_kind === "trailing" ? "protected" : ""}" data-name="${esc(p.agent)}"><div class="phead">${avatar({ name: p.agent, desk: p.desk }, 30)}<div class="pname"><b>${esc(p.agent)}</b>${p.kind === "intern" ? ' <span class="dim">(стажёр)</span>' : p.kind === "experiment" ? ' <span class="dim">(эксперимент)</span>' : ""} ${sideTag}<div class="dim num">${fmt(p.exposure * 100, 0)}% капитала · ${btc(p.qty)} · ${fmt(p.notional, 0)} $</div></div>
        <div class="pres num"><b class="${cls(p.upnl)}">${sign(p.upnl)} $</b><small class="${cls(p.upnl_pct)}">${sign(p.upnl_pct, 2)}%</small></div></div>
      <div class="pbody num">вход <b>${fmt(p.entry, 0)} $</b> · сейчас <b>${fmt(p.price, 0)} $</b> · ${stop} · в сделке ${dur(p.hours)} <button class="btn mini act-close" data-name="${esc(p.agent)}">закрыть</button></div>
      ${p.reason ? `<div class="pbody dim">${esc(p.reason)}</div>` : ""}</li>`;
  }
  function positionsHTML(s) {
    const all = s.positions || [];
    const list = showInternPos ? all : all.filter((p) => !isShadow(p.kind));
    const longs = list.filter((p) => p.side === "long"), shorts = list.filter((p) => p.side === "short");
    const total = list.reduce((x, p) => x + p.upnl, 0);
    const inMarket = list.reduce((x, p) => x + p.notional, 0);
    const team = s.agents.filter((a) => a.status !== "fired").length;
    return `<div class="card"><div class="cardhead"><h3>Открытые позиции · ${list.length}</h3>
        <label class="toggle"><input type="checkbox" id="toggle-pos-interns" ${showInternPos ? "checked" : ""}> стажёры и эксперименты</label></div>
      <div class="note num">${list.length ? `лонгов ${longs.length} · шортов ${shorts.length} · в рынке ${fmt(inMarket, 0)} $ · на бумаге сейчас <b class="${cls(total)}">${sign(total)} $</b>${!showInternPos ? ` · вне рынка ${Math.max(0, team - list.length)} трейдеров` : ""}` : "сейчас все вне рынка: ждут сигнала"}</div>
      ${list.length ? `<ul class="plist">${list.map(positionLine).join("")}</ul>` : ""}
      <div class="note" style="margin-top:8px">Сопровождение позиций: при прибыли в 1·ATR стоп переносится в безубыток, дальше подтягивается за лучшей ценой на 2·ATR и назад не отступает. При прибыли в 3·ATR фиксируется половина позиции, остаток идёт с подтянутым стопом. После выхода в плюс пауза 2 часа, повторный вход в ту же сторону только после ухода цены на 0,5%, не больше 4 входов в день.</div></div>`;
  }

  function allocHTML(s) {
    const team = s.agents.filter((a) => a.status !== "fired");
    const btcv = team.reduce((x, a) => x + a.equity * Math.max(0, a.exposure), 0);
    const shortv = team.reduce((x, a) => x + a.equity * Math.max(0, -a.exposure), 0);
    const total = team.reduce((x, a) => x + a.equity, 0) || 1;
    const usdt = total - btcv;
    const r = 52, c = 2 * Math.PI * r, pb = btcv / total;
    const rows = [...team].sort((a, b) => b.equity - a.equity).map((a) => `
      <div class="row link" data-name="${esc(a.name)}"><i style="background:${a.exposure > 0 ? "var(--btc)" : a.exposure < 0 ? "var(--down)" : "var(--usdt)"}"></i>
        <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(a.name)}</span>
        <b class="num">${fmt(a.equity, 0)} $</b><span class="pct num">${a.exposure < 0 ? "шорт " + fmt(-a.exposure * 100, 0) + "%" : fmt(a.exposure * 100, 0) + "% в BTC"}</span></div>`).join("");
    return `<div class="card"><h3>Распределение капитала</h3>
      <div class="alloc">
        <svg viewBox="0 0 130 130" role="img" aria-label="Доля BTC и USDT">
          <circle cx="65" cy="65" r="${r}" fill="none" stroke="var(--card2)" stroke-width="14"/>
          <circle cx="65" cy="65" r="${r}" fill="none" stroke="var(--btc)" stroke-width="14" stroke-dasharray="${c * pb} ${c}" stroke-dashoffset="${c / 4}" stroke-linecap="butt"/>
          <text x="65" y="61" text-anchor="middle" fill="var(--text)" font-size="15" font-weight="700">${fmt(pb * 100, 0)}%</text>
          <text x="65" y="78" text-anchor="middle" fill="var(--muted)" font-size="10">в BTC</text>
        </svg>
        <div class="rows">
          <div class="row"><i style="background:var(--btc)"></i><span>В биткоине (лонг)</span><b class="num">${fmt(btcv, 0)} $</b><span class="pct num">${fmt(pb * 100, 1)}%</span></div>
          <div class="row"><i style="background:var(--usdt)"></i><span>В долларах (USDT)</span><b class="num">${fmt(usdt, 0)} $</b><span class="pct num">${fmt((1 - pb) * 100, 1)}%</span></div>
          ${shortv > 0 ? `<div class="row"><i style="background:var(--down)"></i><span>Открыто в шорт</span><b class="num">${fmt(shortv, 0)} $</b><span class="pct num">${fmt((shortv / total) * 100, 1)}%</span></div>` : ""}
          <div class="note" style="margin-top:4px">По трейдерам (оранжевая точка: держит BTC, красная: в шорте):</div>
          ${rows}
        </div></div></div>`;
  }

  function chartCardHTML() {
    const price = chartMode === "price";
    return `<div class="card chart o4"><div class="cardhead"><h3>${price ? "Цена BTC и позиции" : "Результат компании, $"}</h3>
        <div class="range"><button data-mode="price" class="${price ? "active" : ""}">Цена</button><button data-mode="pnl" class="${!price ? "active" : ""}">Результат</button></div></div>
      <div class="range" style="margin-top:6px">${price
        ? [[24, "24 ч"], [72, "3 дня"], [168, "7 дней"]].map(([n, l]) => `<button data-pr="${n}" class="${priceRange === n ? "active" : ""}">${l}</button>`).join("")
        : `<button data-r="day" class="${range === "day" ? "active" : ""}">День</button><button data-r="week" class="${range === "week" ? "active" : ""}">Неделя</button><button data-r="all" class="${range === "all" ? "active" : ""}">Всё время</button>`}</div>
      <canvas id="chart"></canvas><div class="tip" id="tip"></div><div class="note" id="chart-note"></div>
      ${price ? '<div class="legend"><i class="lg-long"></i>вход лонг <i class="lg-short"></i>вход шорт <i class="lg-stop"></i>стоп <span class="lg-buy">▲</span> покупка <span class="lg-sell">▼</span> продажа</div>' : ""}</div>`;
  }
  function briefingHTML(s) {
    const b = s.briefing;
    return `<div class="card o1b"><div class="cardhead"><h3>Утренний брифинг${b ? ` · ${esc(b.topic)}` : ""}</h3><button class="btn mini" id="btn-briefing">обновить</button></div>
      ${b ? `<div class="brief">${esc(b.text).split(/\n+/).map((p) => `<p>${p}</p>`).join("")}</div>` : '<div class="note">Директор пишет брифинг каждое утро в 06:00 по Астане: что было ночью, что делает компания, на что смотреть сегодня. Нажмите «обновить», чтобы получить его сейчас.</div>'}</div>`;
  }
  function stressHTML(s) {
    const st = s.stress || { moves: {}, equity: 0 };
    const mv = st.moves || {};
    const keys = Object.keys(mv);
    const risk = st.at_risk_pct || 0;
    return `<div class="card o6"><h3>Стресс-тест · под риском <span class="num ${risk > 3 ? "down" : risk > 1.5 ? "warn" : "up"}">${fmt(risk, 2)}%</span></h3>
      <div class="note">Убыток компании, если цена прямо сейчас сдвинется. Стопы учтены: позиция закрывается на стопе, дальше не теряет.${st.unprotected ? ` <b class="down">Без стопа: ${st.unprotected}.</b>` : ""} В рынке ${fmt(st.exposure_pct || 0, 0)}% капитала.</div>
      ${keys.length ? `<div class="tbl" style="margin-top:8px"><table><tr><th>Движение</th><th class="r">Компания</th><th class="r">Быки</th><th class="r">Медведи</th><th class="r">Двуст.</th></tr>
        ${keys.map((k) => `<tr><td class="num"><b>${k}%</b></td><td class="r num ${cls(mv[k].pnl)}">${sign(mv[k].pnl)} $ <span class="dim">(${sign(mv[k].pct, 2)}%)</span></td>${["bulls", "bears", "both"].map((d) => `<td class="r num ${cls(mv[k].desks[d])}">${sign(mv[k].desks[d])}</td>`).join("")}</tr>`).join("")}</table></div>` : ""}</div>`;
  }
  function directorMiniHTML(s) {
    const h = s.head || {}, d = h.director || {}, caps = h.caps || {};
    return `<div class="card o7 link" data-go="company"><h3>Директор · ${esc(d.name || "")}</h3>
      <div class="caps">${Object.keys(DESK).map((k) => `<div><span>${DESK[k]}</span><div class="capbar d-${k}"><i style="width:${Math.round((caps[k] ?? 1) * 100)}%"></i></div><b class="num">${fmt((caps[k] ?? 1) * 100, 0)}%</b></div>`).join("")}</div>
      <div class="note num" style="margin-top:6px">режим ${REG[h.regime] || "—"} (${esc(h.source || "правила")}) · рейтинг ${d.rating ?? 50} · премия ${sign(d.bonus || 0)} $${h.intraday ? ` · внутридневной пересмотр ${hhmm(h.intraday.ts)}` : ""}</div>
      ${macroHTML(s)}</div>`;
  }
  function macroHTML(s) {
    const m = s.macro; if (!m) return "";
    const fng = m.fng != null ? `<span class="pill ${m.fng <= 25 ? "desk-bears" : m.fng >= 75 ? "desk-bulls" : ""}">страх и жадность ${m.fng} · ${esc(m.fng_label || "")}</span>` : "";
    const oi = m.open_interest != null ? `<span class="pill">открытый интерес ${fmt(m.open_interest / 1000, 1)}k BTC${m.oi_change_pct != null ? ` (${sign(m.oi_change_pct, 1)}%)` : ""}</span>` : "";
    const vol = m.volume_24h_usd != null ? `<span class="pill">объём ${fmt(m.volume_24h_usd / 1e9, 2)} млрд $</span>` : "";
    return `<div class="macro">${fng}${oi}${vol}</div>`;
  }
  function statsHTML(s) {
    const st = (s.stats || {})[statsRange] || {};
    const pf = st.profit_factor == null ? "—" : st.profit_factor === Infinity || st.profit_factor > 99 ? "∞" : fmt(st.profit_factor, 2);
    const wr = st.win_rate == null ? "—" : fmt(st.win_rate * 100, 0) + "%";
    return `<div class="card o8"><div class="cardhead"><h3>Статистика сделок</h3><div class="range"><button data-sr="7d" class="${statsRange === "7d" ? "active" : ""}">7 дней</button><button data-sr="all" class="${statsRange === "all" ? "active" : ""}">Всё время</button></div></div>
      <div class="stat" style="margin-top:8px">
        <div><div class="k">Закрыто сделок</div><div class="v num">${st.closed ?? 0}</div><div class="note">всего сделок ${st.trades ?? 0}</div></div>
        <div><div class="k">Прибыльных</div><div class="v num ${st.win_rate != null ? (st.win_rate >= 0.5 ? "up" : "down") : ""}">${wr}</div></div>
        <div><div class="k">Профит-фактор</div><div class="v num ${st.profit_factor != null ? (st.profit_factor >= 1 ? "up" : "down") : ""}">${pf}</div><div class="note">выигрыш / проигрыш</div></div>
        <div><div class="k">Средняя прибыль · убыток</div><div class="v num" style="font-size:15px"><span class="up">${sign(st.avg_win || 0)}</span> · <span class="down">${sign(st.avg_loss || 0)}</span></div></div>
        <div><div class="k">Итог закрытых</div><div class="v num ${cls(st.pnl)}">${sign(st.pnl || 0)} $</div></div>
        <div><div class="k">Комиссии</div><div class="v num down">${fmt(st.fees || 0)} $</div><div class="note">${st.pnl && st.fees ? `${fmt(Math.abs(st.fees / (Math.abs(st.pnl) || 1)) * 100, 0)}% от итога` : ""}</div></div>
      </div>
      ${st.best ? `<div class="note num" style="margin-top:8px">Лучшая: ${esc(st.best.agent)} <b class="up">${sign(st.best.pnl)} $</b> (${time(st.best.ts)}) · худшая: ${esc(st.worst.agent)} <b class="down">${sign(st.worst.pnl)} $</b> (${time(st.worst.ts)})</div>` : ""}
      <div class="tbl" style="margin-top:8px"><table><tr><th>Деск</th><th class="r">Сделок</th><th class="r">Прибыльных</th><th class="r">Итог</th><th class="r">Комиссии</th></tr>
        ${Object.keys(DESK).map((k) => { const d = (st.desks || {})[k] || {}; return `<tr><td>${DESK[k]}</td><td class="r num">${d.closed ?? 0}</td><td class="r num">${d.win_rate == null ? "—" : fmt(d.win_rate * 100, 0) + "%"}</td><td class="r num ${cls(d.pnl)}">${sign(d.pnl || 0)} $</td><td class="r num">${fmt(d.fees || 0)} $</td></tr>`; }).join("")}</table></div></div>`;
  }
  function homeHTML(s) {
    return `<div class="term"><div class="col-main">
        <div class="o1">${heroHTML(s)}</div>
        ${briefingHTML(s)}
        ${chartCardHTML()}
        <div class="o3">${positionsHTML(s)}</div>
        <div class="o5">${tradesHTML(s)}</div>
        <div class="o9">${allocHTML(s)}</div>
      </div><div class="col-side">
        <div class="o0">${approvalsHTML(s)}</div>
        <div class="o2">${desksSummaryHTML(s)}</div>
        ${directorMiniHTML(s)}
        ${stressHTML(s)}
        ${statsHTML(s)}
        <div class="card o10"><h3>Последние события</h3><ul class="events">${eventsHTML(s.events.slice(0, 10))}</ul></div>
      </div></div>`;
  }

  // ---------- раскрывающаяся строка агента ----------
  function agentRow(a, opts = {}) {
    const isIntern = a.status === "intern";
    const nameShort = esc(a.name);
    const nextIn = a.next_decision_ts ? Math.max(0, Math.round((a.next_decision_ts - Date.now() / 1000) / 60)) : null;
    const stateTxt = a.status === "paused" ? "пауза" : a.status === "fired" ? "уволен" : a.status === "dropped" ? "отчислен" : a.exposure > 0 ? `лонг ${fmt(a.exposure * 100, 0)}%` : a.exposure < 0 ? `шорт ${fmt(-a.exposure * 100, 0)}%` : a.decided_at ? `ждёт сигнала · ${hhmm(a.decided_at)}` : "ещё не решал";
    const stateCls = a.status !== "active" && a.status !== "intern" ? a.status : a.exposure > 0 ? "inpos" : a.exposure < 0 ? "short" : "wait";
    return `<details class="acc ${a.status}" data-name="${nameShort}">
      <summary>
        <div class="acc-name">${avatar(a, 36)}<div><b>${nameShort}</b><span class="state ${stateCls}">${stateTxt}</span><span class="reason">${esc(a.last_reason || "")}</span></div></div>
        <div class="acc-badges">${rankPill(a)}${opts.desk ? deskPill(a) : ""}${a.live_ready && a.rank < 3 ? '<span class="pill gold">кандидат на реальный счёт</span>' : ""}${a.trial_weeks ? `<span class="pill">испытание · ${a.trial_weeks}-я нед.</span>` : ""}${!isIntern && a.streak_weeks > 0 ? `<span class="pill green">серия ${a.streak_weeks} нед.</span>` : ""}</div>
        <div class="acc-time num" title="Как часто агент смотрит на рынок">${a.cadence_minutes >= 60 ? "1 ч" : a.cadence_minutes + " м"}</div>
        <div class="acc-pnl num ${cls(a.pnl_24h)}">${sign(a.pnl_24h)} $<small>24 ч</small></div>
        <div class="acc-pnl num ${cls(a.pnl_total)}">${sign(a.pnl_total)} $<small>всего</small></div>
        <div class="acc-arrow">›</div>
      </summary>
      <div class="acc-body">
        <div class="note">${esc(a.description || a.strategy)}</div>
        <div class="kv">
          <div><span>Деск</span><b>${DESK[a.desk] || a.desk}</b></div>
          <div><span>Звание</span><b>${RANK[a.rank ?? 1]}</b></div>
          <div><span>Капитал</span><b class="num">${fmt(a.equity)} $${a.start_balance && Math.abs(a.start_balance - 1000) > 1 ? ` <span class="muted">(выделено ${fmt(a.start_balance, 0)})</span>` : ""}</b></div>
          <div><span>Сегодня</span><b class="num ${cls(a.pnl_day)}">${sign(a.pnl_day)} $</b></div>
          <div><span>Позиция</span><b class="num">${a.exposure < 0 ? "шорт " + fmt(-a.exposure * 100, 0) + "%" : fmt(a.exposure * 100, 0) + "% в BTC"}</b></div>
          <div><span>Просадка</span><b class="num">${fmt(a.drawdown * 100, 1)}%</b></div>
          <div><span>Сделок · побед</span><b class="num">${a.trades} · ${fmt(a.win_rate * 100, 0)}%</b></div>
          <div><span>${isIntern ? "На стажировке" : "В должности"}</span><b class="num">${a.days} дн.</b></div>
          <div><span>За эту неделю</span><b class="num ${cls(a.pnl_week)}">${sign(a.pnl_week)} $</b></div>
          <div><span>Недель в плюсе подряд</span><b class="num">${isIntern ? "—" : a.streak_weeks}</b></div>
          <div><span>Смотрит на рынок</span><b class="num">${a.cadence_minutes >= 60 ? "раз в час" : `каждые ${a.cadence_minutes} мин`}</b></div>
          <div><span>Последняя проверка</span><b class="num">${hhmm(a.decided_at)}</b></div>
          <div><span>Следующая</span><b class="num">${hhmm(a.next_decision_ts)}${nextIn != null ? ` <span class="muted">(через ${nextIn} мин)</span>` : ""}</b></div>
          ${a.stop_price ? `<div><span>Стоп-лосс</span><b class="num down">${fmt(a.stop_price, 0)} $</b></div>` : ""}
        </div>
        <div class="bar ${a.exposure < 0 ? "short" : ""}"><i style="width:${Math.round(Math.abs(a.exposure) * 100)}%"></i></div>
        <div class="last"><span class="tag">${ACTION[a.last_action] || "—"}</span> ${esc(a.last_reason || "решений ещё не было")}</div>
        <div style="margin-top:8px"><button class="btn open-agent" data-name="${nameShort}">Открыть карточку и журнал</button></div>
      </div>
    </details>`;
  }

  // ---------- дески ----------
  function desksHTML(s) {
    const alive = s.agents.filter((a) => a.status !== "fired");
    const fired = s.agents.filter((a) => a.status === "fired");
    const h = s.head || {};
    let html = `<h2 class="sec">Три деска · ${alive.length} из ${s.team_size} трейдеров</h2>
      <div class="note" style="margin-bottom:4px">Каждый трейдер торгует своими 1000 $ по своей теории и сам решает, как часто смотреть на рынок (колонка «Ритм»). Быки покупают только на рост (спот). Медведи ставят только на падение, двусторонние торгуют в обе стороны: у них фьючерсный демосчёт без плеча, со стопом и ставкой финансирования. Директор раз в день решает, какую долю капитала может использовать каждый деск. По понедельникам ротация внутри деска: минус за неделю отправляет в стажёры, ${s.head?.senior_weeks || 2} недели в плюсе подряд дают звание старшего трейдера, три недели подряд делают кандидатом на реальный счёт. У каждой позиции есть стоп-лосс, он проверяется каждую минуту.</div>`;
    html += `<div class="toolbar"><label>Сортировка <select id="desk-sort"><option value="pnl_total" ${deskSort === "pnl_total" ? "selected" : ""}>за всё время</option><option value="pnl_24h" ${deskSort === "pnl_24h" ? "selected" : ""}>за 24 часа</option><option value="pnl_day" ${deskSort === "pnl_day" ? "selected" : ""}>за сегодня</option><option value="drawdown" ${deskSort === "drawdown" ? "selected" : ""}>по просадке</option><option value="name" ${deskSort === "name" ? "selected" : ""}>по имени</option></select></label>
      <label class="toggle"><input type="checkbox" id="desk-onlypos" ${deskOnlyPos ? "checked" : ""}> только в позиции</label></div>`;
    const sorter = (a, b) => deskSort === "name" ? a.name.localeCompare(b.name, "ru") : deskSort === "drawdown" ? b.drawdown - a.drawdown : b[deskSort] - a[deskSort];
    for (const d of s.desks || []) {
      const rows = alive.filter((a) => a.desk === d.key && (!deskOnlyPos || Math.abs(a.exposure) > 1e-9)).sort(sorter);
      const wk = (h.weeks || []).slice(-1)[0]?.desks?.[d.key];
      html += `<div class="card list desk-list d-${d.key}"><div class="dtitle"><div><b>${esc(d.label)}</b> <span class="muted num">${d.agents} из ${d.size}</span><div class="note">${esc(d.description)}</div></div>
          <div class="dkpi num"><div><span>капитал</span><b>${fmt(d.equity, 0)} $</b></div><div><span>сегодня</span><b class="${cls(d.pnl_day)}">${sign(d.pnl_day)} $</b></div><div><span>неделя</span><b class="${cls(d.pnl_week)}">${sign(d.pnl_week)} $</b></div><div><span>потолок</span><b>${fmt(d.cap * 100, 0)}%</b></div></div></div>
        ${wk ? `<div class="note num" style="padding:0 4px 6px">Прошлая неделя: деск ${sign(wk.pct)}%, ориентир (${d.key === "bulls" ? "держать биткоин" : d.key === "bears" ? "шорт биткоина" : "держать доллары"}) ${sign(wk.bench)}%.</div>` : ""}
        <div class="acc-head"><span>Трейдер</span><span>Ритм</span><span>За 24 ч</span><span>За всё время</span><span></span></div>${rows.length ? rows.map((a) => agentRow(a)).join("") : '<div class="note" style="padding:8px 4px">Места пока свободны: директор нанимает из стажёров.</div>'}</div>`;
    }
    if (fired.length) html += `<h2 class="sec">Уволенные · ${fired.length}</h2><div class="card list">${fired.map((a) => agentRow(a, { desk: true })).join("")}</div>`;
    return html;
  }

  // ---------- стажёры ----------
  function internsHTML(s) {
    const names = new Set(s.interns.map((a) => a.name));
    const feed = (s.decisions || []).filter((d) => names.has(d.agent)).slice(0, 25);
    const inPos = s.interns.filter((a) => Math.abs(a.exposure) > 1e-9).length;
    const traded = s.interns.filter((a) => a.trades > 0).length;
    const best = s.interns[0];
    const byDesk = (s.desks || []).map((d) => `${d.label.toLowerCase()} ${d.interns}`).join(" · ");
    return `<h2 class="sec">Стажёры · ${s.interns.length} из ${s.intern_count}</h2>
      <div class="card"><div class="stat">
        <div><div class="k">В позиции</div><div class="v num">${inPos} <span class="muted" style="font-size:13px">из ${s.interns.length}</span></div></div>
        <div><div class="k">Уже торговали</div><div class="v num">${traded}</div></div>
        <div><div class="k">Лучший</div><div class="v num ${cls(best?.pnl_total)}">${best ? sign(best.pnl_total) + " $" : "—"}</div><div class="note">${best ? esc(best.name) : ""}</div></div>
        <div><div class="k">По дескам</div><div class="v" style="font-size:13px">${byDesk || "—"}</div></div>
      </div>
      <div class="ladder"><span class="pill">Кандидат</span>›<span class="pill rank-0">Стажёр</span>›<span class="pill rank-1">Трейдер</span>›<span class="pill rank-2">Старший трейдер</span>›<span class="pill rank-3">Реальный счёт</span></div>
      <div class="note">Карьерная лестница. Кандидатов находит отдел исследований по бэктесту. Стажёры торгуют в тени на своих демосчетах по 1000 $, распределены по дескам. Стажёр без единой сделки три дня отчисляется. Каждый понедельник лучшие стажёры деска с плюсом за неделю занимают места трейдеров с минусом (те уходят на испытательный срок в стажёры). Стажёр с минусом две недели подряд отчисляется. Трейдер с ${s.head?.senior_weeks || 2} неделями в плюсе подряд становится старшим, с тремя кандидатом на реальный счёт: переводите его вы.</div></div>
      <div class="card list"><h3>Рейтинг · нажмите на строку</h3><div class="acc-head"><span>Стажёр</span><span>Ритм</span><span>За 24 ч</span><span>За всё время</span><span></span></div>${s.interns.map((a) => agentRow(a, { desk: true })).join("")}</div>
      <div class="card"><h3>Что они делают прямо сейчас · последние решения</h3>${feed.length ? `<ul class="feed">${feed.map((d) => `<li><time>${hhmm(d.ts)}</time><b>${esc(d.agent)}</b><span class="tag ${d.action === "BUY" ? "buy" : d.action === "SELL" ? "sell" : ""}">${ACTION[d.action] || d.action}${d.trade_side ? (d.trade_side === "BUY" ? " · купил" : " · продал") : ""}</span><span class="muted">${esc(d.reason)}</span></li>`).join("")}</ul>` : '<div class="note">Первые решения появятся в ближайший час.</div>'}</div>`;
  }

  // ---------- компания: директор, аналитика, риск, наука, обучение ----------
  function directorHTML(s) {
    const h = s.head || {};
    const caps = h.caps || {};
    const weeks = (h.weeks || []).slice(-6).reverse();
    const c = s.consensus || {};
    const winRate = h.alloc_weeks ? Math.round((h.alloc_wins / h.alloc_weeks) * 100) : null;
    const d = h.director || {};
    const rating = d.rating ?? 50;
    const hist = s.directors_history || [];
    const tenure = d.since_ts ? Math.max(0, Math.floor((Date.now() / 1000 - d.since_ts) / 86400)) : 0;
    return `<div class="card"><h3>Директор</h3>
      <div class="dirhead"><div><b>${esc(d.name || "Директор")}</b> <span class="pill">${esc(d.style_ru || "сбалансированный")}</span><div class="note num">в должности ${tenure} дн. · ${d.weeks || 0} нед. оценено</div></div>
        <div class="dirbonus num"><span>заработал</span><b class="${cls(d.bonus)}">${sign(d.bonus || 0)} $</b>${d.bonus_week != null ? `<small class="${cls(d.bonus_week)}">${sign(d.bonus_week)} $ за неделю</small>` : ""}</div></div>
      <div class="rating"><div class="rbar"><i class="${rating >= 70 ? "good" : rating < 30 ? "bad" : ""}" style="width:${rating}%"></i></div><b class="num ${rating >= 70 ? "up" : rating < 30 ? "down" : ""}">${rating}</b><span class="note">рейтинг${d.rating_delta != null ? ` (${d.rating_delta > 0 ? "+" : ""}${d.rating_delta} за неделю)` : ""}${d.low_weeks ? ` · ниже 30 уже ${d.low_weeks} нед.` : ""}</span></div>
      <div class="note">Рейтинг от 0 до 100, старт 50. Каждую неделю: +10 если распределение капитала лучше равного (иначе −10), +10 если компания в плюсе (иначе −10), +5 если не хуже биткоина (иначе −5). Премия: ${fmt(h.bonus_pct ?? 10, 0)}% от прибыли недели и столько же от выигрыша над равным распределением, при убытке штраф в половину ставки. Премия виртуальная, из капитала не вычитается. Рейтинг ниже 30 две недели подряд: вам предлагают сменить директора, новый приходит со своим стилем, а база знаний, память и правила остаются в компании.</div>
      <div class="stat" style="margin-top:10px">
        <div><div class="k">Подход</div><div class="v" style="font-size:15px">${h.mode === "defensive" ? "защитный" : "обычный"}</div></div>
        <div><div class="k">Режим рынка</div><div class="v" style="font-size:15px">${REG[h.regime] || "—"}</div><div class="note">${h.source ? esc(h.source) : ""}${h.rule_regime && h.rule_regime !== h.regime ? ` · по правилам ${REG[h.rule_regime]}` : ""}</div></div>
        <div><div class="k">Голос аналитиков</div><div class="v" style="font-size:15px">${c.fresh ? `${REG[c.regime]} <span class="muted num" style="font-size:12px">${fmt((c.strength || 0) * 100, 0)}%</span>` : "нет свежих"}</div></div>
        <div><div class="k">KPI распределения</div><div class="v num ${winRate == null ? "" : winRate >= 50 ? "up" : "down"}">${winRate == null ? "—" : winRate + "%"}</div><div class="note">недель, когда его распределение было лучше равного</div></div>
        <div><div class="k">Недель хуже долларов подряд</div><div class="v num ${h.fail_weeks ? "down" : ""}">${h.fail_weeks ?? 0}</div></div>
        <div><div class="k">Порог сделки</div><div class="v num">${fmt((h.min_rebalance ?? 0.05) * 100, 0)}%</div></div>
      </div>
      <div class="caps">${Object.keys(DESK).map((k) => `<div><span>${DESK[k]}</span><div class="capbar d-${k}"><i style="width:${Math.round((caps[k] ?? 1) * 100)}%"></i></div><b class="num">${fmt((caps[k] ?? 1) * 100, 0)}%</b></div>`).join("")}</div>
      <div class="note" style="margin-top:8px">Директор отвечает за результат: каждый день оценивает рынок по своим правилам и голосам аналитиков и распределяет капитал между десками. Каждую неделю его распределение сравнивается с равным («если бы всем дали 100%»), а компания с «держать доллары» и «держать биткоин». Две недели подряд хуже долларов, и он переходит на защитный подход и переобучает всех.</div>
      ${weeks.length ? `<div class="tbl" style="margin-top:8px"><table><tr><th>Неделя</th><th class="r">Компания</th><th class="r">Равное</th><th class="r">Биткоин</th><th class="r">Быки</th><th class="r">Медведи</th><th class="r">Двуст.</th></tr>${weeks.map((w) => `<tr><td>${date(w.ts)}</td><td class="r num ${cls(w.dept)}">${sign(w.dept, 2)}%</td><td class="r num ${cls(w.equal)}">${w.equal == null ? "—" : sign(w.equal, 2) + "%"}</td><td class="r num ${cls(w.btc)}">${sign(w.btc, 2)}%</td>${["bulls", "bears", "both"].map((k) => `<td class="r num ${cls(w.desks?.[k]?.pct)}">${w.desks?.[k] ? sign(w.desks[k].pct, 2) + "%" : "—"}</td>`).join("")}</tr>`).join("")}</table></div>` : '<div class="note" style="margin-top:6px">Первое недельное сравнение появится в понедельник.</div>'}
      ${hist.length ? `<h3 style="margin-top:14px">Прежние директора</h3><ul class="kb">${hist.map((r) => `<li>${esc(r.text)}</li>`).join("")}</ul>` : ""}</div>`;
  }
  function analyticsHTML(s) {
    const list = s.analysts || [];
    const rows = list.map((a) => {
      const last = a.last;
      const acc = a.accuracy == null ? "—" : fmt(a.accuracy * 100, 0) + "%";
      return `<div class="analyst"><div class="ahead"><b>${esc(a.name)}</b><span class="num muted">точность ${acc}${a.scored ? ` <small>(${a.hits} из ${a.scored})</small>` : ""}</span></div>
        ${last ? `<div class="aview"><span class="pill reg-${last.regime}">${REG[last.regime]}</span><span class="num muted">уверенность ${fmt(last.confidence * 100, 0)}% · ${time(last.ts)}</span>${last.outcome_pct != null ? `<span class="num ${last.hit ? "up" : "down"}">${last.hit ? "сбылось" : "не сбылось"} (${sign(last.outcome_pct)}%)</span>` : ""}</div><div class="note">${esc(last.summary)}</div>` : '<div class="note">взгляда ещё нет</div>'}
        ${a.lessons?.length ? `<div class="note dim">урок: ${esc(a.lessons[a.lessons.length - 1])}</div>` : ""}
        <div class="note dim num">следующий взгляд ${a.next_ts ? hhmm(a.next_ts) : "—"}</div></div>`;
    }).join("");
    return `<div class="card"><h3>Аналитический отдел</h3>
      <div class="stat">
        <div><div class="k">Модель</div><div class="v" style="font-size:14px">${s.llm ? esc(s.llm_model) : "нет ключа"}</div></div>
        <div><div class="k">Расход сегодня</div><div class="v num">${fmt(s.llm_spend?.usd ?? 0, 2)} $ <span class="muted" style="font-size:12px">из ${fmt(s.llm_spend?.budget ?? 0, 2)} $</span></div></div>
        <div><div class="k">Вызовов сегодня</div><div class="v num">${s.llm_spend?.calls ?? 0}</div></div>
      </div>
      <div class="note" style="margin:8px 0">Нейросеть больше не торгует. Три аналитика несколько раз в день дают взгляд на сутки вперёд: рост, боковик или падение. Через сутки взгляд сверяется с ценой, так считается точность каждого. Директор взвешивает их голоса по точности.</div>
      ${s.llm ? rows : '<div class="note warn">Без ключа Claude API аналитический отдел молчит, директор работает только по своим правилам.</div>'}
      ${s.llm_error ? `<div class="note down" style="margin-top:8px">Последняя ошибка (${hhmm(s.llm_error.ts)}): ${esc(s.llm_error.text)}</div>` : ""}</div>`;
  }
  function riskHTML(s) {
    const r = s.risk || { limits: {}, week: {} };
    const L = r.limits, W = r.week;
    return `<div class="card"><h3>Риск-менеджер</h3><div class="stat">
        <div><div class="k">Стопов за неделю</div><div class="v num">${W.stops ?? 0}</div></div>
        <div><div class="k">Ликвидаций</div><div class="v num ${W.liquidations ? "down" : ""}">${W.liquidations ?? 0}</div></div>
        <div><div class="k">Пауз · увольнений</div><div class="v num">${W.pauses ?? 0} · ${W.fires ?? 0}</div></div>
        <div><div class="k">Остановок компании</div><div class="v num ${W.halts ? "down" : ""}">${W.halts ?? 0}</div></div>
        <div><div class="k">Худшая просадка сейчас</div><div class="v num">${fmt((r.max_drawdown || 0) * 100, 1)}%</div></div>
        <div><div class="k">В позиции</div><div class="v num">${r.in_position ?? 0}</div></div>
      </div>
      <div class="note" style="margin-top:8px">Лимиты: дневной убыток трейдера ${fmt((L.agent_daily_loss || 0) * 100, 0)}% → пауза до конца дня; просадка ${fmt((L.agent_max_drawdown || 0) * 100, 0)}% → увольнение; дневной убыток компании ${fmt((L.dept_daily_loss || 0) * 100, 0)}% → всё закрыть до завтра; стоп на расстоянии ${L.stop_atr_mult}·ATR; без плеча, позиция не больше ${fmt((L.max_exposure || 1) * 100, 0)}% капитала; ликвидация фьючерсов при марже ниже ${fmt((L.liquidation_ratio || 0) * 100, 0)}%. Ставка финансирования сейчас ${s.funding_rate == null ? "—" : sign(s.funding_rate * 100, 4) + "% за 8 ч"}.</div></div>`;
  }
  function scienceHTML(s) {
    const bench = s.bench || [];
    const fam = families || [];
    const sc = s.science || { week: {} };
    return `<div class="card"><h3>Отдел исследований</h3><div class="stat">
        <div><div class="k">Семейств стратегий</div><div class="v num">${sc.families ?? fam.length}</div></div>
        <div><div class="k">Кандидатов на скамейке</div><div class="v num">${bench.length}</div></div>
        <div><div class="k">За неделю в стажёры</div><div class="v num">${sc.week?.interns ?? 0}</div></div>
        <div><div class="k">За неделю в трейдеры · отчислено</div><div class="v num">${sc.week?.promoted ?? 0} · ${sc.week?.dropped ?? 0}</div></div>
      </div>
      <div class="note" style="margin-top:8px">Перебирает семейства и параметры на реальной истории за 30 дней, для каждого деска отдельно (спот, шорт, обе стороны). Первые две трети истории идут на подбор, последняя треть на проверку: оценка кандидата это худшая из двух, так отсеиваются случайные совпадения. Лучшие идут на скамейку кандидатов, оттуда набираются стажёры. Раз в неделю перепроверяет параметры трейдеров.</div>
      <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap"><button class="btn" id="btn-research">Запустить исследование</button><button class="btn" id="btn-library">${showLibrary ? "Скрыть библиотеку" : `Библиотека стратегий · ${fam.length}`}</button></div>
      ${bench.length ? `<h3 style="margin-top:14px">Кандидаты (лучшие по бэктесту)</h3><div class="tbl"><table><tr><th>Семейство</th><th>Параметры</th><th class="r">Подбор</th><th class="r">Проверка</th><th class="r">Оценка</th></tr>` +
        bench.map((b) => `<tr><td>${esc(b.strategy)}</td><td>${Object.entries(b.params).map(([k, v]) => `<span class="tag">${k}=${v}</span>`).join("")}</td><td class="r num ${cls(b.stats.return_pct)}">${sign(b.stats.return_pct, 1)}%</td><td class="r num ${cls(b.stats.oos_return_pct)}">${b.stats.oos_return_pct == null ? "—" : sign(b.stats.oos_return_pct, 1) + "%"}</td><td class="r num">${fmt(b.score, 1)}</td></tr>`).join("") + "</table></div>" : ""}
      ${showLibrary ? `<div class="tbl" style="margin-top:10px"><table>${fam.map((f) => `<tr><td><b>${esc(f.label)}</b>${f.side === "short" ? ' <span class="pill desk-bears">медведи</span>' : f.side === "both" ? ' <span class="pill desk-both">двусторонние</span>' : ' <span class="pill desk-bulls">быки</span>'}<div class="note">${esc(f.description)}</div></td></tr>`).join("")}</table></div>` : ""}</div>`;
  }
  function experimentsHTML(s) {
    const ex = s.experiments || [];
    if (!ex.length) return "";
    const tr = (s.trades_24h || []).filter((t) => t.kind === "experiment");
    const closed = tr.filter((t) => t.pnl != null), wins = closed.filter((t) => t.pnl > 0);
    const fees = tr.reduce((x, t) => x + (t.fee || 0), 0);
    return `<div class="card"><h3>Эксперименты · вне десков</h3>
      <div class="stat"><div><div class="k">Сделок за 24 ч</div><div class="v num">${tr.length}</div></div>
        <div><div class="k">Закрыто · в плюсе</div><div class="v num">${closed.length} · ${wins.length}</div></div>
        <div><div class="k">Итог за 24 ч</div><div class="v num ${cls(closed.reduce((x, t) => x + t.pnl, 0))}">${sign(closed.reduce((x, t) => x + t.pnl, 0))} $</div></div>
        <div><div class="k">Комиссии за 24 ч</div><div class="v num down">${fmt(fees)} $</div></div>
        <div><div class="k">Минутных свечей</div><div class="v num">${s.m1_count ?? 0}</div></div></div>
      <div class="note" style="margin:8px 0">Скальпер торгует на минутных свечах на своём счёте, в капитал компании не входит, лимиты входов на него не действуют. Он проверяет главный вопрос частой торговли: покрывает ли прибыль комиссию 0,1% за сделку. Если счёт проседает на 10%, он перезапускается с 1000 $. Сравнивайте «итог» и «комиссии»: пока комиссии больше, частая торговля не окупается.</div>
      <div class="list"><div class="acc-head"><span>Агент</span><span>Ритм</span><span>За 24 ч</span><span>За всё время</span><span></span></div>${ex.map((a) => agentRow(a)).join("")}</div></div>`;
  }
  function learningHTML(s) {
    const l = s.learning || { week: {}, events: [] };
    return `<div class="card"><h3>Обучение</h3><div class="stat">
        <div><div class="k">Уроков за неделю</div><div class="v num">${l.week?.lessons ?? 0}</div></div>
        <div><div class="k">Переобучений за неделю</div><div class="v num">${l.week?.retunes ?? 0}</div></div>
      </div>
      <div class="note" style="margin-top:8px">Каждому решению через 4 часа проставляется результат. Аналитики получают уроки из взглядов, которые не подтвердились; урок проверяется по точности до и после и уходит в архив, если не помог. Параметры трейдеров раз в неделю перепроверяются на свежей истории с проверкой на невиденных данных, трейдер после понижения переобучается перед стажировкой.</div>
      <ul class="events" style="margin-top:8px">${eventsHTML(l.events || []) || '<li class="muted">пока пусто</li>'}</ul></div>`;
  }
  function knowledgeHTML(s) {
    const k = s.knowledge || { counts: {}, memory: [], families: [], rules: [], lessons: [], insights: [], proposals: [], staff: {} };
    const c = k.counts || {};
    const cnt = (kind, st) => (c[kind] || {})[st] || 0;
    const PK = { strategy: "стратегия", risk: "риск", product: "приложение" };
    const PS = { pending: "ждёт решения", accepted: "принято", rejected: "отклонено", done: "сделано" };
    const memRow = (d) => ["up", "flat", "down"].map((r) => { const m = k.memory.find((x) => x.key === d && x.regime === r); return `<td class="r num ${m ? cls(m.avg) : ""}">${m ? `${sign(m.avg, 2)}%<div class="dim">${m.days} дн.</div>` : "—"}</td>`; }).join("");
    const fams = [...k.families].sort((a, b) => b.avg - a.avg);
    const top = fams.slice(0, 4), bottom = fams.slice(-3).reverse();
    const famLine = (m) => `<li><span class="pill desk-${/_short$/.test(m.key) ? "bears" : /_both$/.test(m.key) ? "both" : "bulls"}">${esc(m.key)}</span> в режиме «${REG[m.regime]}»: <b class="num ${cls(m.avg)}">${sign(m.avg, 2)}%</b> в день, ${m.days} дн.</li>`;
    return `<div class="card"><h3>База знаний компании</h3>
      <div class="stat">
        <div><div class="k">Дней в памяти</div><div class="v num">${k.memory.reduce((x, m) => x + m.days, 0) / 3 | 0}</div></div>
        <div><div class="k">Правил действует</div><div class="v num">${cnt("rule", "active")}</div></div>
        <div><div class="k">Уроков</div><div class="v num">${cnt("lesson", "active") + cnt("lesson", "verified")} <span class="muted" style="font-size:12px">в архиве ${cnt("lesson", "retired")}</span></div></div>
        <div><div class="k">Наблюдений стратега</div><div class="v num">${cnt("insight", "active")}</div></div>
        <div><div class="k">Предложений принято</div><div class="v num">${cnt("proposal", "accepted") + cnt("proposal", "done")} <span class="muted" style="font-size:12px">ждут ${cnt("proposal", "pending")}</span></div></div>
      </div>
      <div class="note" style="margin-top:8px">Знания принадлежат компании, а не людям. Каждый день итог каждого трейдера и стажёра записывается в память его семейства стратегий и деска под текущий режим рынка. Новый стажёр того же семейства наследует эту память, директор режет капитал дескам, которые в текущем режиме исторически теряют (после ${k.memory_min_days || 10} дней данных), и при найме предпочитает семейства с хорошей памятью.</div>
      <h3 style="margin-top:14px">Память десков по режимам · средний результат в день</h3>
      <div class="tbl"><table><tr><th>Деск</th><th class="r">Рост</th><th class="r">Боковик</th><th class="r">Падение</th></tr>
        ${["bulls", "bears", "both"].map((d) => `<tr><td>${DESK[d]}</td>${memRow(d)}</tr>`).join("")}</table></div>
      ${fams.length ? `<h3 style="margin-top:14px">Память семейств</h3><ul class="kb">${top.map(famLine).join("")}${bottom.length && fams.length > 4 ? `<li class="dim">…</li>${bottom.map(famLine).join("")}` : ""}</ul>` : ""}
      <h3 style="margin-top:14px">Правила риск-менеджера из опыта</h3>
      ${k.rules.length ? `<ul class="kb">${k.rules.map((r) => `<li><b>${esc(r.text)}</b><div class="note">${esc(r.data?.rationale || "")} · сработало ${r.uses} раз · ${date(r.ts)} <button class="btn mini kb-status" data-id="${r.id}" data-st="retired">убрать</button></div></li>`).join("")}</ul>` : '<div class="note">Пока нет. Ревизор предлагает правило по итогам недели, если видит повторяющуюся причину потерь; правило начинает действовать после вашего одобрения.</div>'}
      <h3 style="margin-top:14px">Наблюдения стратега развития</h3>
      ${k.insights.length ? `<ul class="kb">${k.insights.map((r) => `<li>${esc(r.text)}<div class="note">${time(r.ts)}</div></li>`).join("")}</ul>` : `<div class="note">${s.llm ? `Стратег смотрит на рынок и статистику компании раз в ${Math.round((s.head?.strategist_interval_h || 72) / 24)} дня; следующий раз ${k.staff?.strategist_next ? time(k.staff.strategist_next) : "скоро"}.` : "Стратег развития работает на нейросети: нужен ключ Claude API."}</div>`}
      <h3 style="margin-top:14px">Предложения по развитию</h3>
      ${k.proposals.length ? `<ul class="kb">${k.proposals.map((r) => `<li><span class="pill">${PK[r.topic] || r.topic}</span> <span class="pill ${r.status === "accepted" || r.status === "done" ? "green" : r.status === "rejected" ? "" : "gold"}">${PS[r.status] || r.status}</span> <b>${esc(r.data?.title || "")}</b><div class="note">${esc(r.data?.details || r.text)}${r.data?.expected_effect ? `<br>Ожидаемый эффект: ${esc(r.data.expected_effect)}` : ""}${r.status === "accepted" ? ` <button class="btn mini kb-status" data-id="${r.id}" data-st="done">сделано</button>` : ""}</div></li>`).join("")}</ul>` : '<div class="note">Принятые предложения копятся здесь как план развития: что менять в стратегиях, риске и самом приложении.</div>'}
      ${k.lessons.length ? `<h3 style="margin-top:14px">Уроки аналитикам</h3><ul class="kb">${k.lessons.map((r) => `<li><span class="pill ${r.status === "verified" ? "green" : r.status === "retired" ? "" : "gold"}">${r.status === "verified" ? "подтверждён" : r.status === "retired" ? "в архиве" : "проверяется"}</span> <b>${esc(r.topic)}</b>: ${esc(r.text)}</li>`).join("")}</ul>` : ""}
    </div>`;
  }
  function alertsHTML(s) {
    const kinds = s.alert_kinds || {};
    const list = s.alerts || [];
    const team = s.agents.filter((a) => a.status !== "fired");
    return `<div class="card"><h3>Мои сигналы</h3>
      <div class="note">Условие проверяется каждую минуту. Сработавший сигнал попадает в события и в push-уведомление. Одноразовый выключается после срабатывания, повторяющийся напоминает не чаще раза в час.</div>
      <div class="alertform">
        <select id="al-kind">${Object.entries(kinds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <input id="al-value" type="number" step="any" placeholder="значение">
        <select id="al-desk" hidden>${Object.entries(DESK).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <select id="al-agent" hidden>${team.map((a) => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("")}</select>
        <label class="toggle"><input type="checkbox" id="al-repeat"> повторять</label>
        <button class="btn" id="al-add">Добавить</button></div>
      ${list.length ? `<ul class="kb">${list.map((a) => `<li><span class="pill ${a.active ? "green" : ""}">${a.active ? "ждёт" : "сработал"}</span> ${esc(kinds[a.kind] || a.kind)} ${a.value ? `<b class="num">${fmt(a.value, a.kind.startsWith("price") ? 0 : 2)}</b>` : ""} ${a.target ? `<b>${esc(DESK[a.target] || a.target)}</b>` : ""}${a.repeat ? ' <span class="dim">· повторяется</span>' : ""}${a.fired_n ? ` <span class="dim num">· срабатывал ${a.fired_n} раз, последний ${time(a.fired_ts)}</span>` : ""} <button class="btn mini al-del" data-id="${a.id}">убрать</button></li>`).join("")}</ul>` : '<div class="note" style="margin-top:8px">Сигналов пока нет.</div>'}</div>`;
  }
  function labHTML(s) {
    const fam = families || [];
    const cur = fam.find((f) => f.family === labFamily) || fam[0];
    if (cur && !labFamily) { labFamily = cur.family; labParams = { ...cur.params }; }
    const r = labResult;
    return `<div class="card"><h3>Лаборатория · проверить идею</h3>
      <div class="note">Выберите стратегию и параметры, прогон идёт по реальной истории часовых свечей (до 30 дней) с комиссией 0,1%. Это тот же бэктест, которым пользуется отдел исследований.</div>
      <div class="labform">
        <select id="lab-family">${fam.map((f) => `<option value="${f.family}" ${f.family === labFamily ? "selected" : ""}>${esc(f.label)}${f.side === "short" ? " · медведь" : f.side === "both" ? " · двусторонний" : ""}</option>`).join("")}</select>
        <select id="lab-days">${[7, 14, 30].map((d) => `<option value="${d}" ${d === labDays ? "selected" : ""}>${d} дней</option>`).join("")}</select>
        <button class="btn primary" id="lab-run" ${labBusy ? "disabled" : ""}>${labBusy ? "Считаю…" : "Прогнать"}</button></div>
      <div class="labparams">${Object.entries(labParams).map(([k, v]) => `<label>${esc(k)}<input data-p="${esc(k)}" type="number" step="any" value="${v}"></label>`).join("")}</div>
      ${cur ? `<div class="note">${esc(cur.description)}</div>` : ""}
      ${r ? `<div class="stat" style="margin-top:10px">
          <div><div class="k">Доход</div><div class="v num ${cls(r.result.return_pct)}">${sign(r.result.return_pct, 2)}%</div></div>
          <div><div class="k">Просадка</div><div class="v num">${fmt(r.result.max_drawdown_pct, 2)}%</div></div>
          <div><div class="k">Sharpe</div><div class="v num">${fmt(r.result.sharpe, 2)}</div></div>
          <div><div class="k">Сделок</div><div class="v num">${r.result.trades}</div></div>
          <div><div class="k">Оценка</div><div class="v num ${cls(r.result.score)}">${fmt(r.result.score, 1)}</div></div>
        </div><div class="chart" style="margin-top:8px"><canvas id="lab-chart" style="height:160px"></canvas></div>` : ""}</div>`;
  }
  function drawLabChart() {
    const canvas = $("#lab-chart"); if (!canvas || !labResult) return;
    const pts = labResult.curve; if (pts.length < 2) return;
    const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth, H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
    const ys = pts.map((p) => p.equity), min = Math.min(...ys), max = Math.max(...ys), pad = (max - min) * 0.1 || 1;
    const x = (i) => 4 + (i / (pts.length - 1)) * (W - 8), y = (v) => 4 + (1 - (v - (min - pad)) / (max - min + 2 * pad)) * (H - 20);
    const css = getComputedStyle(document.documentElement), col = (n) => css.getPropertyValue(n).trim();
    ctx.strokeStyle = col("--line"); ctx.beginPath(); ctx.moveTo(4, y(1000)); ctx.lineTo(W - 4, y(1000)); ctx.stroke();
    ctx.strokeStyle = ys[ys.length - 1] >= 1000 ? col("--up") : col("--down"); ctx.lineWidth = 2; ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p.equity)) : ctx.moveTo(x(i), y(p.equity)))); ctx.stroke();
    ctx.fillStyle = col("--muted"); ctx.font = "10px sans-serif"; ctx.fillText(time(pts[0].ts), 4, H - 4); ctx.textAlign = "right"; ctx.fillText(time(pts[pts.length - 1].ts), W - 4, H - 4);
  }
  function correlationHTML(s) {
    const c = corrData;
    if (!c) return `<div class="card"><h3>Похожесть трейдеров</h3><div class="note">Считаю по часовым приращениям капитала за 7 дней…</div></div>`;
    if (c.names.length < 2) return `<div class="card"><h3>Похожесть трейдеров</h3><div class="note">Данных пока мало: нужно хотя бы сутки работы двух трейдеров.</div></div>`;
    const short = (n) => n.replace(/\s*\(.*\)/, "").replace("Двусторонний", "Дв.").replace("Медведь", "Мед.").slice(0, 12);
    const cell = (v) => { const a = Math.min(1, Math.abs(v)); const bg = v > 0 ? `rgba(52,210,123,${a * 0.85})` : `rgba(255,107,107,${a * 0.85})`; return `<td class="num" style="background:${bg};color:${a > 0.5 ? "#0b0d12" : "inherit"}">${v.toFixed(2)}</td>`; };
    return `<div class="card"><h3>Похожесть трейдеров · корреляция за 7 дней</h3>
      <div class="note">1,00 = двигаются одинаково (по сути один трейдер), 0 = независимы, отрицательное = в противофазе. Если весь деск красно-зелёный в одну сторону, разнообразия нет и просадка приходит ко всем сразу.</div>
      <div class="tbl corr" style="margin-top:8px"><table><tr><th></th>${c.names.map((n) => `<th title="${esc(n)}">${esc(short(n))}</th>`).join("")}</tr>
        ${c.names.map((n, i) => `<tr><th title="${esc(n)}"><span class="pill desk-${c.desks[i]}" style="padding:0 4px"></span> ${esc(short(n))}</th>${c.matrix[i].map(cell).join("")}</tr>`).join("")}</table></div>
      ${c.pairs.length ? `<div class="note" style="margin-top:8px">Самые похожие пары: ${c.pairs.slice(0, 4).map((p) => `${esc(short(p.a))} и ${esc(short(p.b))} <b class="num">${p.r.toFixed(2)}</b>`).join("; ")}.</div>` : ""}</div>`;
  }
  function liveHTML(s) {
    const l = s.live || {};
    const orders = l.orders || [];
    return `<div class="card"><h3>Реальный счёт · ${l.enabled ? (l.testnet ? "тестовая сеть Binance" : "<span class='down'>реальные деньги</span>") : "выключен"}</h3>
      ${l.enabled ? `<div class="stat"><div><div class="k">Капитал на трейдера</div><div class="v num">${fmt(l.capital_usd, 0)} $</div></div>
          <div><div class="k">Трейдеров со званием</div><div class="v num">${(l.agents || []).length}</div><div class="note">${(l.agents || []).map(esc).join(", ") || "пока никто"}</div></div>
          <div><div class="k">Позиции на бирже</div><div class="v num" style="font-size:14px">${Object.entries(l.positions || {}).filter(([, q]) => q > 0).map(([n, q]) => `${esc(n)} ${fmt(q, 5)} BTC`).join("<br>") || "нет"}</div></div></div>
        ${orders.length ? `<div class="tbl" style="margin-top:8px"><table><tr><th>Время</th><th>Трейдер</th><th>Сторона</th><th class="r">BTC</th><th class="r">$</th><th>Статус</th></tr>${orders.map((o) => `<tr><td class="num">${time(o.ts)}</td><td>${esc(o.agent)}</td><td class="${o.side === "BUY" ? "up" : "down"}">${o.side === "BUY" ? "покупка" : "продажа"}</td><td class="r num">${fmt(o.qty, 5)}</td><td class="r num">${fmt(o.quote)}</td><td>${o.error ? `<span class="down">${esc(o.error)}</span>` : esc(o.status)}</td></tr>`).join("")}</table></div>` : '<div class="note" style="margin-top:8px">Ордеров ещё не было. Они появятся, когда трейдер со званием «Реальный счёт» совершит сделку.</div>'}`
        : `<div class="note">Мост к бирже: сделки трейдеров со званием «Реальный счёт» повторяются на Binance пропорционально выделенной сумме. Сначала тестовая сеть (виртуальные деньги, настоящие ордера), потом реальный счёт. Включается в <b>.env</b>: LIVE_ENABLED=true, LIVE_API_KEY и LIVE_API_SECRET от testnet.binance.vision, LIVE_CAPITAL_USD. Пока зеркалятся только покупки и продажи на споте (деск быков).</div>`}</div>`;
  }
  function companyHTML(s) {
    return `<h2 class="sec">Компания Botz · отделы</h2>` + approvalsHTML(s) + directorHTML(s) + knowledgeHTML(s) + analyticsHTML(s) + riskHTML(s) + alertsHTML(s) + scienceHTML(s) + labHTML(s) + correlationHTML(s) + experimentsHTML(s) + liveHTML(s) + learningHTML(s);
  }

  // ---------- отчёты ----------
  function reportsHTML(s) {
    const sm = summary || { daily: [], reports: [] };
    const team = s.agents.filter((a) => a.status !== "fired");
    const day = team.reduce((x, a) => x + a.pnl_day, 0);
    const bestDay = [...team].sort((a, b) => b.pnl_day - a.pnl_day)[0];
    const worstDay = [...team].sort((a, b) => a.pnl_day - b.pnl_day)[0];
    const allRows = [...s.agents].sort((a, b) => b.pnl_total - a.pnl_total).map((a) => `<tr class="link" data-name="${esc(a.name)}"><td>${esc(a.name)}<div>${deskPill(a)} ${rankPill(a) || `<span class="badge ${a.status}">${STATUS[a.status]}</span>`}</div></td><td class="r num ${cls(a.pnl_total)}">${sign(a.pnl_total)} $</td><td class="r num ${cls(a.pnl_day)}">${sign(a.pnl_day)} $</td><td class="r num">${a.trades}</td><td class="r num">${fmt(a.win_rate * 100, 0)}%</td><td class="r num">${fmt(a.drawdown * 100, 1)}%</td></tr>`).join("");
    const daily = [...sm.daily].reverse().map((d) => `<tr><td>${d.day.slice(5).split("-").reverse().join(".")}</td><td class="r num">${fmt(d.equity)} $</td><td class="r num ${cls(d.change)}">${d.change == null ? "—" : sign(d.change) + " $"}</td></tr>`).join("");
    const reports = sm.reports.map((e) => { let data = {}; try { data = JSON.parse(e.data || "{}"); } catch (_) {} return `<div class="report"><div class="t">${time(e.ts)}</div><div>${esc(e.message)}</div>${(data.recommendations || []).length ? `<ul>${data.recommendations.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}</div>`; }).join("");
    return `<h2 class="sec">Отчёты</h2>
      ${approvalsHTML(s)}
      ${heatmapHTML(s)}
      <div class="card"><h3>Сегодня</h3><div class="stat">
        <div><div class="k">Результат дня</div><div class="v num ${cls(day)}">${sign(day)} $</div></div>
        <div><div class="k">Лучший сегодня</div><div class="v" style="font-size:14px">${bestDay ? `${esc(bestDay.name)} <span class="num ${cls(bestDay.pnl_day)}">${sign(bestDay.pnl_day)}</span>` : "—"}</div></div>
        <div><div class="k">Худший сегодня</div><div class="v" style="font-size:14px">${worstDay ? `${esc(worstDay.name)} <span class="num ${cls(worstDay.pnl_day)}">${sign(worstDay.pnl_day)}</span>` : "—"}</div></div>
      </div></div>
      <div class="card"><h3>Отчёты директора</h3>${reports || '<div class="note">Первый отчёт появится в конце дня.</div>'}</div>
      <div class="card"><h3>Капитал по дням</h3>${daily ? `<div class="tbl"><table><tr><th>День</th><th class="r">Капитал</th><th class="r">Изменение</th></tr>${daily}</table></div>` : '<div class="note">Появится после первого дня.</div>'}</div>
      <div class="card"><h3>За всё время по трейдерам</h3><div class="tbl"><table><tr><th>Трейдер</th><th class="r">Всего</th><th class="r">Сегодня</th><th class="r">Сделок</th><th class="r">Побед</th><th class="r">Просадка</th></tr>${allRows}</table></div></div>
      <div class="card"><h3>Все события</h3><ul class="events">${eventsHTML(s.events)}</ul></div>
      <div class="card"><h3>Экспорт</h3><div class="note">Все сделки компании в таблицу (CSV), открывается в Excel и Google Таблицах.</div><p><a class="btn" href="/api/trades.csv" download>Скачать сделки CSV</a></p></div>`;
  }

  function heatmapHTML(s) {
    const hm = (s.heatmap || {})[heatRange]; if (!hm) return "";
    const maxAbs = Math.max(1, ...hm.hours.map((c) => Math.abs(c.pnl)), ...hm.weekdays.map((c) => Math.abs(c.pnl)));
    const cellStyle = (c) => { const a = Math.min(1, Math.abs(c.pnl) / maxAbs); return c.n ? `background:${c.pnl >= 0 ? `rgba(52,210,123,${0.15 + a * 0.7})` : `rgba(255,107,107,${0.15 + a * 0.7})`}` : ""; };
    const hours = hm.hours.map((c, h) => ({ ...c, h: (h + 5) % 24 })).sort((a, b) => a.h - b.h);   // по Астане
    const WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
    return `<div class="card"><div class="cardhead"><h3>Тепловые карты · когда компания зарабатывает</h3><div class="range"><button data-hr="7d" class="${heatRange === "7d" ? "active" : ""}">7 дней</button><button data-hr="all" class="${heatRange === "all" ? "active" : ""}">Всё время</button></div></div>
      <div class="note">Итог закрытых сделок по часу закрытия (время Астаны) и по дню недели. Закрыто сделок: ${hm.closed}.</div>
      <div class="heat hours">${hours.map((c) => `<div style="${cellStyle(c)}" title="${String(c.h).padStart(2, "0")}:00 · ${c.n} сделок · ${sign(c.pnl)} $"><small>${String(c.h).padStart(2, "0")}</small><b class="num">${c.n ? sign(c.pnl, 0) : "·"}</b></div>`).join("")}</div>
      <div class="heat days">${hm.weekdays.map((c, i) => `<div style="${cellStyle(c)}" title="${WD[i]} · ${c.n} сделок"><small>${WD[i]}</small><b class="num">${c.n ? sign(c.pnl, 0) : "·"}</b><span class="dim">${c.n ? `${c.n} · ${Math.round((c.wins / c.n) * 100)}%` : ""}</span></div>`).join("")}</div></div>`;
  }

  // ---------- машина времени ----------
  async function openTimeMachine(ts) {
    const d = await api(`/api/at?ts=${ts}`);
    let head = null; try { head = d.head ? JSON.parse(d.head.data || "{}") : null; } catch (_) {}
    const caps = head && head.caps ? Object.entries(head.caps).map(([k, v]) => `${DESK[k]} ${fmt(v * 100, 0)}%`).join(" · ") : "";
    $("#modal-box").innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><div><b style="font-size:18px">Машина времени · ${time(ts)}</b><div class="note num">BTC ${fmt(d.price, 0)} $${d.candle ? ` · свеча О ${fmt(d.candle.open, 0)} В ${fmt(d.candle.high, 0)} Н ${fmt(d.candle.low, 0)} З ${fmt(d.candle.close, 0)}` : ""}</div></div><button class="btn" id="modal-close">Закрыть</button></div>
      <div class="note" style="margin-top:8px"><b>Директор:</b> ${d.head ? `${esc(d.head.message)}${caps ? `<br>потолки: ${caps}` : ""} <span class="dim">(${time(d.head.ts)})</span>` : "записей ещё не было"}</div>
      ${d.views.length ? `<div class="note"><b>Аналитики:</b> ${d.views.map((v) => `${esc(v.analyst)}: ${REG[v.regime]} (${fmt(v.confidence * 100, 0)}%)`).join("; ")}</div>` : ""}
      <h3 style="margin:14px 0 6px;font-size:13px;color:var(--muted);text-transform:uppercase">Открытые позиции в тот момент · ${d.positions.length}</h3>
      ${d.positions.length ? `<div class="tbl"><table><tr><th>Трейдер</th><th>Сторона</th><th class="r">Вход</th><th class="r">На бумаге</th></tr>${d.positions.map((p) => `<tr><td>${esc(p.agent)} ${deskPill(p)}</td><td class="${p.side === "long" ? "up" : "down"}">${p.side === "long" ? "лонг" : "шорт"}</td><td class="r num">${fmt(p.entry, 0)}</td><td class="r num ${cls(p.upnl)}">${sign(p.upnl)} $</td></tr>`).join("")}</table></div>` : '<div class="note">все были вне рынка</div>'}
      <h3 style="margin:14px 0 6px;font-size:13px;color:var(--muted);text-transform:uppercase">Что думали трейдеры (последний час)</h3>
      ${d.decisions.length ? `<ul class="feed">${d.decisions.map((x) => `<li><time>${hhmm(x.ts)}</time><b>${esc(x.agent)}</b><span class="tag ${x.action === "BUY" ? "buy" : x.action === "SELL" ? "sell" : ""}">${ACTION[x.action] || x.action}${x.trade_side ? (x.trade_side === "BUY" ? " · купил" : " · продал") : ""}</span><span class="muted">${esc(x.reason)}</span></li>`).join("")}</ul>` : '<div class="note">решений за этот час не записано</div>'}
      ${d.events.length ? `<h3 style="margin:14px 0 6px;font-size:13px;color:var(--muted);text-transform:uppercase">События часа</h3><ul class="events">${eventsHTML(d.events)}</ul>` : ""}`;
    $("#modal").classList.add("open");
    $("#modal-close").onclick = () => $("#modal").classList.remove("open");
  }

  // ---------- общие куски ----------
  function eventsHTML(list) {
    return list.map((e) => `<li><time>${time(e.ts)}</time><span class="kind ${e.kind}">${KIND[e.kind] || e.kind}</span><span>${esc(e.message)}</span></li>`).join("");
  }
  function approvalsHTML(s) {
    if (!s.approvals.length) return "";
    const PK = { strategy: "стратегия", risk: "риск", product: "приложение" };
    return s.approvals.map((p) => {
      const d = p.details || {};
      let extra = "";
      if (p.kind === "rule") extra = `<div class="note">${esc(d.rationale || "")}${d.expected_effect ? ` Ожидаемый эффект: ${esc(d.expected_effect)}` : ""}</div>`;
      if (p.kind === "proposal") extra = `<div class="note"><span class="pill">${PK[d.kind] || d.kind}</span> ${esc(d.details || "")}${d.expected_effect ? `<br>Ожидаемый эффект: ${esc(d.expected_effect)}` : ""}</div>`;
      if (p.kind === "director") extra = `<div class="note">Новый директор будет ${esc(d.next_style_ru || "")}. Память, правила и база знаний остаются. Отклонить = дать нынешнему ещё две недели.</div>`;
      return `<div class="card approval"><div style="min-width:0;flex:1"><b>Нужно ваше решение</b><div>${esc(p.title)}</div>${extra}<div class="note">${time(p.ts)}${d.intern ? ` · ${esc(d.intern)}` : ""}${d.pnl != null ? ` · ${sign(d.pnl)} $` : ""}${d.streak_weeks ? ` · ${d.streak_weeks} нед. в плюсе` : ""}</div></div>
      <div class="actions"><button class="btn primary" data-id="${p.id}" data-d="approve">${p.kind === "proposal" ? "Принять" : "Одобрить"}</button><button class="btn" data-id="${p.id}" data-d="reject">Отклонить</button></div></div>`;
    }).join("");
  }

  // ---------- график цены со свечами и позициями ----------
  function drawPriceChart() {
    const canvas = $("#chart"); if (!canvas || !candles.length) return;
    const pts = candles.slice(-priceRange);
    const note = $("#chart-note"), tip = $("#tip");
    const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth, H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr); ctx.clearRect(0, 0, W, H);
    const pos = (state.positions || []).filter((p) => showInternPos || !isShadow(p.kind));
    const trades = (state.trades_24h || []).filter((t) => (showInternTrades || !isShadow(t.kind)) && t.ts >= pts[0].ts);
    const lows = pts.map((c) => c.low), highs = pts.map((c) => c.high);
    let min = Math.min(...lows, ...pos.map((p) => p.stop || Infinity)), max = Math.max(...highs, ...pos.map((p) => p.entry));
    const pad = (max - min) * 0.06 || 1; min -= pad; max += pad;
    const L = 6, R = 58, T = 8, B = 18, step = pts[pts.length - 1].ts - pts[pts.length - 2].ts || 3600;
    const x = (ts) => L + ((ts - pts[0].ts) / (pts[pts.length - 1].ts + step - pts[0].ts)) * (W - L - R);
    const y = (v) => T + (1 - (v - min) / (max - min)) * (H - T - B);
    const cw = Math.max(1, ((W - L - R) / pts.length) * 0.65);
    const css = getComputedStyle(document.documentElement);
    const col = (n) => css.getPropertyValue(n).trim();
    ctx.strokeStyle = col("--line"); ctx.lineWidth = 1; ctx.fillStyle = col("--muted"); ctx.font = "10px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.textAlign = "left";
    for (let i = 0; i <= 4; i++) { const v = min + ((max - min) * i) / 4, yy = y(v); ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(W - R, yy); ctx.stroke(); ctx.fillText(fmt(v, 0), W - R + 4, yy + 3); }
    pts.forEach((c) => {
      const up = c.close >= c.open, cx = x(c.ts) + cw / 2;
      ctx.strokeStyle = ctx.fillStyle = up ? col("--up") : col("--down");
      ctx.beginPath(); ctx.moveTo(cx, y(c.high)); ctx.lineTo(cx, y(c.low)); ctx.stroke();
      const top = y(Math.max(c.open, c.close)), hgt = Math.max(1, Math.abs(y(c.open) - y(c.close)));
      ctx.fillRect(x(c.ts), top, cw, hgt);
    });
    // текущая цена
    ctx.setLineDash([3, 3]); ctx.strokeStyle = col("--text"); ctx.beginPath(); ctx.moveTo(L, y(state.price)); ctx.lineTo(W - R, y(state.price)); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = col("--text"); ctx.fillRect(W - R + 1, y(state.price) - 7, R - 2, 14); ctx.fillStyle = col("--bg"); ctx.font = "bold 10px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.fillText(fmt(state.price, 0), W - R + 4, y(state.price) + 3);
    // входы и стопы открытых позиций
    ctx.font = "9px -apple-system, Segoe UI, Roboto, sans-serif";
    const usedY = [];
    const labelY = (yy) => { let v = yy - 2; while (usedY.some((u) => Math.abs(u - v) < 10)) v -= 10; usedY.push(v); return v; };
    [...pos].sort((a, b) => b.entry - a.entry).forEach((p) => {
      const c = p.side === "long" ? col("--up") : col("--down");
      ctx.setLineDash([6, 3]); ctx.strokeStyle = c; ctx.globalAlpha = 0.8; ctx.beginPath(); ctx.moveTo(L, y(p.entry)); ctx.lineTo(W - R, y(p.entry)); ctx.stroke();
      if (p.stop) { ctx.setLineDash([2, 3]); ctx.strokeStyle = col("--warn"); ctx.beginPath(); ctx.moveTo(L, y(p.stop)); ctx.lineTo(W - R, y(p.stop)); ctx.stroke(); }
      ctx.setLineDash([]); ctx.globalAlpha = 1; ctx.fillStyle = c; ctx.fillText(p.agent.replace(/\s*\(.*\)/, "").slice(0, 18), L + 2, labelY(y(p.entry)));
    });
    // сделки
    trades.forEach((t) => {
      const tx = x(t.ts), ty = y(t.price); ctx.fillStyle = t.side === "BUY" ? col("--up") : col("--down"); ctx.beginPath();
      if (t.side === "BUY") { ctx.moveTo(tx, ty + 7); ctx.lineTo(tx - 4, ty + 13); ctx.lineTo(tx + 4, ty + 13); } else { ctx.moveTo(tx, ty - 7); ctx.lineTo(tx - 4, ty - 13); ctx.lineTo(tx + 4, ty - 13); }
      ctx.closePath(); ctx.fill();
    });
    ctx.fillStyle = col("--muted"); ctx.font = "10px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.textAlign = "left"; ctx.fillText(time(pts[0].ts), L, H - 4); ctx.textAlign = "right"; ctx.fillText(time(pts[pts.length - 1].ts), W - R, H - 4);
    const first = pts[0].open, last = pts[pts.length - 1].close;
    note.innerHTML = `за период <b class="num ${cls(last - first)}">${sign((last / first - 1) * 100, 2)}%</b> · мин ${fmt(Math.min(...lows), 0)} · макс ${fmt(Math.max(...highs), 0)} · открытых позиций ${pos.length}, сделок на графике ${trades.length}`;
    const onMove = (ev) => {
      const b = canvas.getBoundingClientRect(); const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - b.left;
      const i = Math.max(0, Math.min(pts.length - 1, Math.floor(((px - L) / (W - L - R)) * pts.length)));
      const c = pts[i]; if (!c) return;
      tip.style.display = "block"; tip.innerHTML = `${time(c.ts)}<br><span class="num">О ${fmt(c.open, 0)} · В ${fmt(c.high, 0)} · Н ${fmt(c.low, 0)} · З <b class="${cls(c.close - c.open)}">${fmt(c.close, 0)}</b></span>`;
      tip.style.left = Math.min(W - 190, Math.max(0, x(c.ts) - 80)) + "px"; tip.style.top = Math.max(0, y(c.high) - 44) + "px";
    };
    canvas.onmousemove = onMove; canvas.ontouchstart = onMove; canvas.ontouchmove = onMove;
    canvas.onmouseleave = () => (tip.style.display = "none");
    canvas.onclick = (ev) => {
      const b = canvas.getBoundingClientRect(); const px = ev.clientX - b.left;
      const i = Math.max(0, Math.min(pts.length - 1, Math.floor(((px - L) / (W - L - R)) * pts.length)));
      if (pts[i]) openTimeMachine(pts[i].ts + step - 1);
    };
    canvas.style.cursor = "pointer";
    note.innerHTML += ' · <span class="dim">клик по свече открывает машину времени</span>';
  }

  // ---------- график капитала ----------
  async function drawChart() {
    if (chartMode === "price") return drawPriceChart();
    const canvas = $("#chart"); if (!canvas) return;
    if (!equityData) equityData = await api("/api/curve");
    let pts = equityData.map((p) => [p.ts, p.pnl]);
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
    note.textContent = `результат компании: мин ${sign(min)} $ · макс ${sign(max)} $ · за период ${sign(ys[ys.length - 1] - ys[0])} $`;
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
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><div style="display:flex;align-items:center;gap:10px">${avatar(a, 48)}<div><b style="font-size:18px">${esc(a.name)}</b><div>${deskPill(a)} ${rankPill(a)} <span class="badge ${a.status}">${STATUS[a.status]}</span></div></div></div><button class="btn" id="modal-close">Закрыть</button></div>
      <div class="note" style="margin-top:6px">${esc(d.description)}</div>
      <div class="note">Параметры: ${Object.entries(a.params).map(([k, v]) => `<span class="tag">${k}=${v}</span>`).join("")}</div>
      ${(d.memory || []).length ? `<div class="note">Память компании о семействе: ${d.memory.map((m) => `${REG[m.regime]} <b class="num ${cls(m.avg)}">${sign(m.avg, 2)}%</b>/день (${m.days} дн.)`).join(" · ")}</div>` : ""}
      <div class="stat" style="margin-top:12px">
        <div><div class="k">Капитал</div><div class="v num">${fmt(a.equity)} $</div></div>
        <div><div class="k">Всего</div><div class="v num ${cls(a.pnl_total)}">${sign(a.pnl_total)} $</div></div>
        <div><div class="k">Сегодня</div><div class="v num ${cls(a.pnl_day)}">${sign(a.pnl_day)} $</div></div>
        <div><div class="k">Просадка</div><div class="v num">${fmt(a.drawdown * 100, 1)}%</div></div>
        <div><div class="k">Сделок · побед</div><div class="v num">${a.trades} · ${fmt(a.win_rate * 100, 0)}%</div></div>
        <div><div class="k">Смотрит на рынок</div><div class="v num" style="font-size:14px">${a.cadence_minutes >= 60 ? "раз в час" : `каждые ${a.cadence_minutes} мин`}</div></div>
      </div>
      <h3 style="margin:14px 0 6px;font-size:13px;color:var(--muted);text-transform:uppercase">Последние решения</h3>
      <div class="note" style="margin-bottom:6px">«Цель» это какую долю капитала агент хочет держать в позиции (минус = шорт). Сделка происходит только если цель отличается от текущего состояния. В журнал попадают сделки, смена цели и контрольная запись раз в час. «Цена через 4 ч» заполняется с задержкой.</div>
      <div class="tbl"><table><tr><th>Время</th><th>Цель</th><th>Сделка</th><th class="r">Цена BTC</th><th class="r">Цена через 4 ч</th><th>Обоснование</th></tr>
      ${d.decisions.map((x) => `<tr><td class="num">${time(x.ts)}</td><td>${ACTION[x.action] || x.action}<div class="dim">${x.target_exposure < 0 ? "шорт " + fmt(-x.target_exposure * 100, 0) + "%" : fmt(x.target_exposure * 100, 0) + "% в BTC"}</div></td><td>${tradeCell(x)}</td><td class="r num">${fmt(x.price, 0)}</td><td class="r num ${cls(x.outcome_pct)}">${x.outcome_pct == null ? "—" : sign(x.outcome_pct) + "%"}</td><td class="note">${esc(x.reason)}</td></tr>`).join("")}</table></div>
      ${a.status !== "fired" && a.status !== "dropped" ? `<p class="actions-row">${Math.abs(a.exposure) > 1e-9 ? '<button class="btn" id="modal-closepos">Закрыть позицию</button>' : ""}${a.status === "active" ? '<button class="btn" id="modal-pause">Пауза до завтра</button>' : a.status === "paused" ? '<button class="btn" id="modal-resume">Снять паузу</button>' : ""}<button class="btn danger" id="modal-fire">${a.status === "intern" ? "Отчислить стажёра" : a.status === "experiment" ? "Остановить эксперимент" : "Уволить трейдера"}</button></p>` : ""}`;
    $("#modal").classList.add("open");
    $("#modal-close").onclick = () => $("#modal").classList.remove("open");
    const act = (id, url, q) => { const b = $(id); if (b) b.onclick = async () => { if (confirm(q)) { await api(url, { method: "POST" }); $("#modal").classList.remove("open"); refresh(true); } }; };
    act("#modal-fire", `/api/agents/${encodeURIComponent(name)}/fire`, `${a.status === "intern" ? "Отчислить" : a.status === "experiment" ? "Остановить" : "Уволить"} ${a.name}?`);
    act("#modal-closepos", `/api/agents/${encodeURIComponent(name)}/close`, `Закрыть позицию ${a.name} по рынку?`);
    act("#modal-pause", `/api/agents/${encodeURIComponent(name)}/pause`, `Закрыть позицию и поставить ${a.name} на паузу до завтра?`);
    act("#modal-resume", `/api/agents/${encodeURIComponent(name)}/resume`, `Снять паузу с ${a.name}?`);
  }

  // ---------- рендер вкладки ----------
  function goTab(t) {
    tab = t; history.replaceState(null, "", "#" + tab);
    $$("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
    window.scrollTo(0, 0); render();
    if (tab === "reports") refresh(true);
  }
  function render() {
    if (!state) return;
    const view = $("#view");
    const html = tab === "home" ? homeHTML(state) : tab === "desks" ? desksHTML(state) : tab === "interns" ? internsHTML(state) : tab === "company" ? companyHTML(state) : reportsHTML(state);
    view.innerHTML = html;
    $$("[data-name]", view).forEach((el) => { if (!el.classList.contains("acc")) el.addEventListener("click", (e) => { if (e.target.closest(".acc") || e.target.closest("button")) return; openAgent(el.dataset.name); }); });
    $$("[data-go]", view).forEach((el) => el.addEventListener("click", () => goTab(el.dataset.go)));
    $$(".open-agent", view).forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); openAgent(b.dataset.name); }));
    $$("details.acc", view).forEach((d) => { if (openRows.has(d.dataset.name)) d.open = true; d.addEventListener("toggle", () => { if (d.open) openRows.add(d.dataset.name); else openRows.delete(d.dataset.name); }); });
    $$(".approval button", view).forEach((b) => b.addEventListener("click", async () => { await api(`/api/approvals/${b.dataset.id}/${b.dataset.d}`, { method: "POST" }); refresh(true); }));
    $$(".range button[data-r]", view).forEach((b) => b.addEventListener("click", () => { range = b.dataset.r; $$(".range button[data-r]", view).forEach((x) => x.classList.toggle("active", x === b)); drawChart(); }));
    $$(".range button[data-pr]", view).forEach((b) => b.addEventListener("click", () => { priceRange = Number(b.dataset.pr); render(); }));
    $$(".range button[data-mode]", view).forEach((b) => b.addEventListener("click", () => { chartMode = b.dataset.mode; try { localStorage.setItem("botz.chart", chartMode); } catch (_) {} render(); }));
    $$(".range button[data-sr]", view).forEach((b) => b.addEventListener("click", () => { statsRange = b.dataset.sr; render(); }));
    $$(".act-close", view).forEach((b) => b.addEventListener("click", async (e) => { e.stopPropagation(); e.preventDefault(); if (confirm(`Закрыть позицию ${b.dataset.name} по рынку?`)) { await api(`/api/agents/${encodeURIComponent(b.dataset.name)}/close`, { method: "POST" }); refresh(true); } }));
    const ds = $("#desk-sort", view); if (ds) ds.onchange = () => { deskSort = ds.value; render(); };
    $$(".range button[data-hr]", view).forEach((b) => b.addEventListener("click", () => { heatRange = b.dataset.hr; render(); }));
    const bb = $("#btn-briefing", view); if (bb) bb.onclick = async () => { bb.disabled = true; bb.textContent = "пишу…"; try { await api("/api/briefing", { method: "POST" }); } finally { refresh(true); } };
    // сигналы
    const alKind = $("#al-kind", view);
    if (alKind) {
      const sync = () => { const k = alKind.value; $("#al-desk", view).hidden = !k.startsWith("desk_"); $("#al-agent", view).hidden = !k.startsWith("agent_"); $("#al-value", view).hidden = k === "agent_entry" || k === "agent_exit"; $("#al-value", view).placeholder = k.startsWith("price") ? "цена, $" : "процент"; };
      alKind.onchange = sync; sync();
      $("#al-add", view).onclick = async () => {
        const k = alKind.value; const target = k.startsWith("desk_") ? $("#al-desk", view).value : k.startsWith("agent_") ? $("#al-agent", view).value : "";
        await api("/api/alerts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: k, value: Number($("#al-value", view).value || 0), target, repeat: $("#al-repeat", view).checked }) });
        refresh(true);
      };
      $$(".al-del", view).forEach((b) => b.addEventListener("click", async () => { await api(`/api/alerts/${b.dataset.id}`, { method: "DELETE" }); refresh(true); }));
    }
    // лаборатория
    const lf = $("#lab-family", view);
    if (lf) {
      lf.onchange = () => { labFamily = lf.value; const f = (families || []).find((x) => x.family === labFamily); labParams = f ? { ...f.params } : {}; labResult = null; render(); };
      $("#lab-days", view).onchange = (e) => { labDays = Number(e.target.value); };
      $$(".labparams input", view).forEach((inp) => inp.addEventListener("change", () => { labParams[inp.dataset.p] = Number(inp.value); }));
      $("#lab-run", view).onclick = async () => {
        labBusy = true; render();
        try { labResult = await api("/api/lab/backtest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ family: labFamily, params: labParams, days: labDays }) }); }
        catch (e) { alert("Не удалось прогнать: " + e.message); }
        finally { labBusy = false; render(); }
      };
      drawLabChart();
    }
    if (tab === "company" && !corrData) api("/api/correlation").then((c) => { corrData = c; if (tab === "company") render(); }).catch(() => {});
    const dp = $("#desk-onlypos", view); if (dp) dp.onchange = () => { deskOnlyPos = dp.checked; render(); };
    const rb = $("#btn-research", view);
    if (rb) rb.onclick = async () => { rb.disabled = true; rb.textContent = "Считаю, около минуты…"; try { await api("/api/research", { method: "POST" }); } finally { rb.disabled = false; rb.textContent = "Запустить исследование"; refresh(true); } };
    $$(".kb-status", view).forEach((b) => b.addEventListener("click", async (e) => { e.stopPropagation(); await api(`/api/knowledge/${b.dataset.id}/${b.dataset.st}`, { method: "POST" }); refresh(true); }));
    const lb = $("#btn-library", view);
    if (lb) lb.onclick = () => { showLibrary = !showLibrary; render(); };
    if (tab === "home") {
      drawChart();
      const tg = $("#toggle-interns", view); if (tg) tg.onchange = () => { showInternTrades = tg.checked; render(); };
      const tp = $("#toggle-pos-interns", view); if (tp) tp.onchange = () => { showInternPos = tp.checked; render(); };
    }
  }

  async function refresh(force) {
    try {
      const [st, sm, fam, cd] = await Promise.all([api("/api/state"), (tab === "reports" || !summary || force) ? api("/api/summary") : summary, families || api("/api/families"), api("/api/candles?limit=170")]);
      state = st; summary = sm; families = fam; candles = cd; equityData = null;
      renderTop(state); render(); notifyNew(state);
    } catch (e) {
      $("#meta").textContent = "нет связи с сервером: " + e.message; $("#dot").className = "dot err";
    }
  }

  // уведомления в браузере: новые заявки на решение, стопы, действия директора
  const NOTIFY_KINDS = new Set(["stop", "approval", "head", "fire", "halt", "liquidation", "live_ready", "weekly", "owner"]);
  function notifyNew(s) {
    const ev = s.events || [];
    const maxId = ev.reduce((m, e) => Math.max(m, e.id || 0), 0);
    if (!lastEventId) { lastEventId = maxId; return; }
    const fresh = ev.filter((e) => e.id > lastEventId && NOTIFY_KINDS.has(e.kind));
    lastEventId = Math.max(lastEventId, maxId);
    if (!notifyOn || !("Notification" in window) || Notification.permission !== "granted") return;
    fresh.slice(0, 3).forEach((e) => { try { new Notification(`Botz · ${KIND[e.kind] || e.kind}`, { body: e.message.slice(0, 160), tag: "botz-" + e.id }); } catch (_) {} });
    if (s.approvals.length && fresh.some((e) => e.kind === "live_ready" || e.kind === "weekly")) { try { new Notification("Botz · нужно ваше решение", { body: s.approvals[0].title }); } catch (_) {} }
  }
  function applyTheme() {
    let light = false; try { light = localStorage.getItem("botz.theme") === "light"; } catch (_) {}
    document.documentElement.classList.toggle("light", light);
    const b = $("#btn-theme"); if (b) b.title = light ? "Тёмная тема" : "Светлая тема";
    document.querySelector('meta[name="theme-color"]').setAttribute("content", light ? "#f4f6fb" : "#0b0d12");
  }
  applyTheme();
  $("#btn-theme").onclick = () => { let light = document.documentElement.classList.contains("light"); try { localStorage.setItem("botz.theme", light ? "dark" : "light"); } catch (_) {} applyTheme(); render(); };
  const bell = $("#btn-notify");
  const paintBell = () => { bell.classList.toggle("on", notifyOn); bell.title = notifyOn ? "Уведомления включены" : "Включить уведомления"; };
  paintBell();
  const urlB64ToU8 = (s) => { const b = atob((s + "=".repeat((4 - (s.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/")); return Uint8Array.from(b, (c) => c.charCodeAt(0)); };
  async function pushSubscribe() {
    const reg = await navigator.serviceWorker.ready;
    const { key } = await api("/api/push/key");
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToU8(key) });
    const label = /Android/i.test(navigator.userAgent) ? "Android" : /iPhone|iPad/i.test(navigator.userAgent) ? "iOS" : "компьютер";
    await api("/api/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ subscription: sub.toJSON(), label }) });
    await api("/api/push/test", { method: "POST" });
  }
  async function pushUnsubscribe() {
    const reg = await navigator.serviceWorker.ready; const sub = await reg.pushManager.getSubscription();
    if (sub) { await api("/api/push/unsubscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: sub.endpoint }) }); await sub.unsubscribe(); }
  }
  bell.onclick = async () => {
    if (!("Notification" in window)) { alert("Браузер не поддерживает уведомления"); return; }
    if (!window.isSecureContext) { alert("Уведомления работают только по https-адресу панели"); return; }
    if (!notifyOn) {
      const p = await Notification.requestPermission(); if (p !== "granted") { alert("Разрешите уведомления в настройках браузера"); return; }
      try { if ("PushManager" in window) await pushSubscribe(); } catch (e) { alert("Push не подключился: " + e.message + ". Уведомления будут приходить, пока панель открыта."); }
    } else { try { await pushUnsubscribe(); } catch (_) {} }
    notifyOn = !notifyOn; try { localStorage.setItem("botz.notify", notifyOn ? "1" : "0"); } catch (_) {} paintBell();
  };
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
    const idx = ["1", "2", "3", "4", "5"].indexOf(e.key);
    if (idx >= 0) goTab(TABS[idx]);
    else if (e.key === "r" || e.key === "к") refresh(true);
    else if (e.key === "t" || e.key === "е") $("#btn-theme").click();
    else if (e.key === "Escape") $("#modal").classList.remove("open");
  });
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => goTab(b.dataset.tab)));
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $("#btn-tick").onclick = async () => { const b = $("#btn-tick"); b.disabled = true; try { await api("/api/tick", { method: "POST" }); } finally { b.disabled = false; refresh(true); } };
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").classList.remove("open"); });
  window.addEventListener("resize", () => { if (tab === "home") drawChart(); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  refresh();
  setInterval(() => refresh(false), 60000);
})();
