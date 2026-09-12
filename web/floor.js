/* Офис отдела: вид сверху, мягкая графика, без панорамирования.
   window.startFloor(canvas, state, equityPoints, onAgentClick) -> { stop, layout } */
(() => {
  const C = {
    floor1: "#1d1a30", floor2: "#181628", carpet: "rgba(255,255,255,.025)",
    room: "rgba(255,255,255,.045)", roomEdge: "rgba(255,255,255,.10)", rug: "rgba(255,255,255,.05)",
    wood: "#5b4f8a", woodDark: "#4a4073", desk: "#6f65b3", deskEdge: "#8b82cf", chair: "#3b3560",
    monitor: "#0f0d1f", glowIdle: "rgba(124,200,255,.35)", glowBtc: "rgba(247,147,26,.85)",
    text: "#f2efff", muted: "#a49dcf", dim: "#6f689a",
    up: "#4ade80", down: "#f87171", warn: "#fbbf24", btc: "#f7931a", violet: "#a99cff", teal: "#7cd8ff",
    plant: ["#3f8f5a", "#4faa6b", "#2f7449"], sofa: "#8d6fb8",
    avatar: ["#ff8a65", "#64b5f6", "#ba9cff", "#ffd54f", "#4dd0a1", "#f48fb1", "#90a4ae", "#ffab91", "#80cbc4", "#c5e1a5", "#b39ddb"],
  };
  const hash = (s) => { let h = 0; for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return h; };
  const short = (n) => n.replace(/\s*\(.*\)/, "").replace("Нейро-", "Н-");
  const money = (v) => (v > 0 ? "+" : "") + v.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " $";
  const initial = (n) => short(n).replace(/[^A-Za-zА-Яа-яЁё0-9]/g, "").slice(0, 1).toUpperCase() || "•";
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const FONT = "-apple-system, Segoe UI, Roboto, Inter, sans-serif";

  window.startFloor = function startFloor(canvas, state, equityPoints, onAgentClick) {
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const team = state.agents.filter((a) => a.status !== "fired");
    const interns = (state.interns || []).slice(0, 20);
    const now = state.now;
    let W = 0, H = 0, raf = 0, stopped = false;
    const L = {};           // раскладка
    const desks = [];       // {a, x, y, w, h, kind, color}
    const bubbles = [];
    const couriers = [];
    let nextBubble = 0, nextCourier = 0;

    const lastEvent = (kinds) => (state.events || []).find((e) => kinds.includes(e.kind));
    const say = {
      head: () => { const e = lastEvent(["report", "hire", "fire", "approval"]); return e ? e.message : "Слежу за отделом"; },
      risk: () => { const p = team.filter((a) => a.status === "paused").length; return state.department.halted ? "Отдел остановлен до завтра" : p ? `${p} на паузе до конца дня` : "Лимиты в норме, торгуем"; },
      lab: () => { const e = lastEvent(["research", "retune", "lesson", "intern", "drop"]); return e ? e.message : `${interns.length} стажёров на испытании`; },
    };

    // ---------- раскладка ----------
    function layout() {
      W = canvas.clientWidth;
      const mobile = W < 640;
      const pad = 12, gap = 10;
      const HUD = 116;
      const roomH = mobile ? 118 : 140;
      const teamCols = mobile ? 4 : 6, internCols = mobile ? 5 : 10;
      const teamRows = Math.ceil(team.length / teamCols), internRows = Math.ceil(interns.length / internCols);
      const deskW = Math.floor((W - pad * 2 - gap * (teamCols - 1)) / teamCols), deskH = mobile ? 92 : 100;
      const ideskW = Math.floor((W - pad * 2 - gap * (internCols - 1)) / internCols), ideskH = mobile ? 66 : 72;
      let y = HUD + 12;
      L.hud = { x: 0, y: 0, w: W, h: HUD };
      const rw = Math.floor((W - pad * 2 - gap * 2) / 3);
      L.rooms = [
        { key: "head", label: "Руководитель", x: pad, y, w: rw, h: roomH, color: C.text },
        { key: "risk", label: "Риск-менеджер", x: pad + rw + gap, y, w: rw, h: roomH, color: C.warn },
        { key: "lab", label: "Исследования", x: pad + (rw + gap) * 2, y, w: rw, h: roomH, color: C.violet },
      ];
      y += roomH + 26;
      L.teamLabel = { x: pad, y: y - 8 };
      desks.length = 0;
      team.forEach((a, i) => {
        const r = Math.floor(i / teamCols), c = i % teamCols;
        desks.push({ a, kind: "team", x: pad + c * (deskW + gap), y: y + r * (deskH + gap), w: deskW, h: deskH, color: C.avatar[hash(a.name) % C.avatar.length], phase: (hash(a.name) % 100) / 16 });
      });
      y += teamRows * (deskH + gap) + 18;
      L.internLabel = { x: pad, y: y - 2 };
      L.internZone = { x: pad - 4, y: y + 6, w: W - pad * 2 + 8, h: internRows * (ideskH + gap) + 12 };
      interns.forEach((a, i) => {
        const r = Math.floor(i / internCols), c = i % internCols;
        desks.push({ a, kind: "intern", x: pad + c * (ideskW + gap), y: y + 12 + r * (ideskH + gap), w: ideskW, h: ideskH, color: C.avatar[hash(a.name) % C.avatar.length], phase: (hash(a.name) % 100) / 16 });
      });
      y += L.internZone.h + 22;
      H = Math.max(y, 360);
      canvas.style.height = H + "px";
      canvas.width = W * dpr; canvas.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    // ---------- примитивы ----------
    function rr(x, y, w, h, r, fill, stroke, lw = 1) {
      ctx.beginPath(); ctx.roundRect(x, y, w, h, r);
      if (fill) { ctx.fillStyle = fill; ctx.fill(); }
      if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = lw; ctx.stroke(); }
    }
    function text(t, x, y, size, color, weight = 400, align = "left", base = "alphabetic") {
      ctx.font = `${weight} ${size}px ${FONT}`; ctx.fillStyle = color; ctx.textAlign = align; ctx.textBaseline = base; ctx.fillText(t, x, y);
    }
    function pill(x, y, t, bg, fg, size = 10, padX = 6, align = "center") {
      ctx.font = `600 ${size}px ${FONT}`;
      const w = ctx.measureText(t).width + padX * 2, h = size + 7;
      const bx = align === "center" ? x - w / 2 : x;
      rr(bx, y - h / 2, w, h, h / 2, bg);
      ctx.fillStyle = fg; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(t, bx + w / 2, y + 0.5);
      return w;
    }
    function shadow(fn, blur = 10, color = "rgba(0,0,0,.35)", oy = 3) { ctx.save(); ctx.shadowBlur = blur; ctx.shadowColor = color; ctx.shadowOffsetY = oy; fn(); ctx.restore(); }
    function plant(x, y, s, t) {
      const sway = Math.sin(t / 900 + x) * 1.2;
      rr(x - s * 0.45, y + s * 0.2, s * 0.9, s * 0.5, 4, "#4a3f6b");
      [[0, -0.3, 0.55], [-0.35, -0.05, 0.42], [0.35, -0.05, 0.42], [0, 0.1, 0.36]].forEach(([dx, dy, r], i) => {
        ctx.beginPath(); ctx.arc(x + dx * s + sway * (i ? 0.5 : 1), y + dy * s, r * s, 0, Math.PI * 2); ctx.fillStyle = C.plant[i % 3]; ctx.fill();
      });
    }
    function avatar(x, y, r, color, letter, t, phase, ring) {
      if (ring) { const p = 0.5 + 0.5 * Math.sin(t / 600 + phase); ctx.beginPath(); ctx.arc(x, y, r + 4 + p * 2, 0, Math.PI * 2); ctx.fillStyle = `rgba(247,147,26,${0.18 + 0.22 * p})`; ctx.fill(); }
      shadow(() => { ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); const g = ctx.createRadialGradient(x - r * 0.4, y - r * 0.5, r * 0.2, x, y, r); g.addColorStop(0, "#ffffff"); g.addColorStop(0.08, color); g.addColorStop(1, color); ctx.fillStyle = g; ctx.fill(); }, 8, "rgba(0,0,0,.4)", 2);
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.strokeStyle = "rgba(255,255,255,.35)"; ctx.lineWidth = 1.2; ctx.stroke();
      text(letter, x, y + 0.5, r * 1.05, "#1a1432", 700, "center", "middle");
    }
    function bubble(x, y, msg, maxW, alpha) {
      ctx.save(); ctx.globalAlpha = alpha;
      ctx.font = `11px ${FONT}`;
      const words = msg.split(" "), lines = []; let cur = "";
      for (const w of words) { const tt = cur ? cur + " " + w : w; if (ctx.measureText(tt).width > maxW && cur) { lines.push(cur); cur = w; } else cur = tt; if (lines.length === 3) break; }
      if (cur && lines.length < 3) lines.push(cur);
      if (lines.length === 3 && lines.join(" ").length < msg.length) lines[2] = lines[2].replace(/\s?\S*$/, "…");
      const w = Math.min(maxW, Math.max(...lines.map((l) => ctx.measureText(l).width))) + 18, h = lines.length * 14 + 12;
      const bx = clamp(x - w / 2, 6, W - w - 6), by = y - h - 12;
      shadow(() => rr(bx, by, w, h, 9, "#f7f5ff"), 14, "rgba(0,0,0,.45)", 4);
      ctx.beginPath(); ctx.moveTo(x - 5, by + h - 0.5); ctx.lineTo(x + 5, by + h - 0.5); ctx.lineTo(x, by + h + 7); ctx.closePath(); ctx.fillStyle = "#f7f5ff"; ctx.fill();
      ctx.fillStyle = "#241b45"; ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.font = `11px ${FONT}`;
      lines.forEach((l, i) => ctx.fillText(l, bx + 9, by + 6 + i * 14));
      ctx.restore();
    }

    // ---------- элементы офиса ----------
    function drawFloor(t) {
      const g = ctx.createLinearGradient(0, 0, 0, H); g.addColorStop(0, C.floor1); g.addColorStop(1, C.floor2);
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
      for (let x = 0; x < W; x += 48) for (let y = L.hud.h; y < H; y += 48) if (((x / 48 + y / 48) | 0) % 2) { ctx.fillStyle = C.carpet; ctx.fillRect(x, y, 48, 48); }
      // мягкий свет от ламп
      [[W * 0.25, L.hud.h + 60], [W * 0.75, L.hud.h + 60], [W * 0.5, H * 0.65]].forEach(([x, y]) => {
        const r = Math.max(W, H) * 0.35, lg = ctx.createRadialGradient(x, y, 0, x, y, r); lg.addColorStop(0, "rgba(255,230,190,.07)"); lg.addColorStop(1, "rgba(255,230,190,0)"); ctx.fillStyle = lg; ctx.fillRect(x - r, y - r, r * 2, r * 2);
      });
    }
    function drawHud(t) {
      const { w, h } = L.hud;
      const g = ctx.createLinearGradient(0, 0, 0, h); g.addColorStop(0, "#151228"); g.addColorStop(1, "#1b1730");
      rr(8, 8, w - 16, h - 12, 14, g, "rgba(255,255,255,.08)");
      const d = state.department, dayPnl = team.reduce((x, a) => x + a.pnl_day, 0);
      text("ТАБЛО ОТДЕЛА · ДЕМОСЧЁТ", 20, 26, 9, C.dim, 600);
      text(d.equity.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " $", 20, 52, 24, C.text, 700);
      text(money(dayPnl) + " сегодня", 20, 69, 12, dayPnl > 0 ? C.up : dayPnl < 0 ? C.down : C.muted, 600);
      text(money(d.pnl) + " всего", 20, 84, 12, d.pnl > 0 ? C.up : d.pnl < 0 ? C.down : C.muted, 600);
      const gx0 = Math.max(190, w * 0.45), gw = w - gx0 - 22, gy0 = 20, gh = h - 40;
      rr(gx0, gy0, gw, gh, 8, "rgba(255,255,255,.03)");
      const pts = equityPoints || [];
      if (pts.length > 1 && gw > 40) {
        const ys = pts.map((p) => p[1]), mn = Math.min(...ys), mx = Math.max(...ys), sp = mx - mn || 1;
        const X = (i) => gx0 + 6 + (i / (pts.length - 1)) * (gw - 12), Y = (v) => gy0 + gh - 6 - ((v - mn) / sp) * (gh - 22);
        const col = ys[ys.length - 1] >= ys[0] ? C.teal : C.down;
        const fg = ctx.createLinearGradient(0, gy0, 0, gy0 + gh); fg.addColorStop(0, col.replace(")", ",.25)").replace("rgb", "rgba").replace("#7cd8ff", "rgba(124,216,255").replace("#f87171", "rgba(248,113,113")); fg.addColorStop(1, "rgba(0,0,0,0)");
        ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(X(i), Y(p[1])) : ctx.moveTo(X(i), Y(p[1])))); ctx.lineTo(X(pts.length - 1), gy0 + gh); ctx.lineTo(X(0), gy0 + gh); ctx.closePath(); ctx.fillStyle = fg; ctx.fill();
        ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(X(i), Y(p[1])) : ctx.moveTo(X(i), Y(p[1])))); ctx.strokeStyle = col; ctx.lineWidth = 1.8; ctx.stroke();
        const lp = pts[pts.length - 1]; const pulse = 0.5 + 0.5 * Math.sin(t / 500);
        ctx.beginPath(); ctx.arc(X(pts.length - 1), Y(lp[1]), 3 + pulse * 2, 0, Math.PI * 2); ctx.fillStyle = col.startsWith("#") ? col + "55" : col; ctx.fill();
        ctx.beginPath(); ctx.arc(X(pts.length - 1), Y(lp[1]), 2.5, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill();
        text("результат отдела, $", gx0 + 8, gy0 + 12, 9, C.dim, 600);
      } else text("график накапливается", gx0 + 10, gy0 + gh / 2 + 4, 10, C.dim);
      const clock = window.astanaClock ? window.astanaClock() : "";
      text(`${clock} · BTC ${state.price.toLocaleString("ru-RU", { maximumFractionDigits: 0 })} $`, w - 20, h - 12, 9, C.dim, 500, "right");
      const up = (state.upcoming || []).slice(0, 2).map((u) => `${short(u.name)} в ${new Date(u.ts * 1000).toLocaleTimeString("ru-RU", { timeZone: "Asia/Almaty", hour: "2-digit", minute: "2-digit" })}`).join(", ");
      if (up) text("далее решают: " + up, 20, h - 11, 9, C.dim, 500);
    }
    const roomPeople = {};
    function drawRoom(r, t) {
      shadow(() => rr(r.x, r.y, r.w, r.h, 14, C.room, C.roomEdge), 18, "rgba(0,0,0,.25)", 4);
      rr(r.x + 10, r.y + r.h * 0.45, r.w - 20, r.h * 0.42, 10, C.rug);                       // ковёр
      pill(r.x + 10, r.y + 14, r.label, r.color, "#1a1432", 10, 7, "left");
      const cx = r.x + r.w * 0.5, cy = r.y + r.h * 0.62;
      const dw = Math.min(r.w * 0.5, 64), dh = 20;
      shadow(() => rr(cx - dw / 2, cy - dh / 2, dw, dh, 5, C.wood, C.woodDark), 8);          // стол
      rr(cx - 6, cy - 5, 12, 9, 2, C.monitor); ctx.fillStyle = "rgba(124,216,255,.55)"; ctx.fillRect(cx - 5, cy - 4, 10, 6);
      plant(r.x + r.w - 16, r.y + 30, 12, t);
      rr(r.x + 8, r.y + r.h - 22, 30, 12, 6, C.sofa);                                       // диванчик
      const who = { head: "Р", risk: "!", lab: "λ" }[r.key];
      const col = { head: "#e8e2ff", risk: C.warn, lab: C.violet }[r.key];
      const py = cy + dh / 2 + 12;
      avatar(cx, py, 10, col, who, t, r.x / 50, false);
      roomPeople[r.key] = { x: cx, y: py - 12 };
      if (r.key === "lab") { for (let i = 0; i < Math.min(3, interns.length); i++) { const a = t / 3000 + i * 2.1; avatar(cx + Math.cos(a) * 24, py - 4 + Math.sin(a) * 8, 5, C.violet, "", t, i, false); } }
    }
    function drawDesk(d, t) {
      const { a, x, y, w, h, kind } = d;
      const inPos = a.exposure > 0, mini = kind === "intern";
      const deskH = mini ? 16 : 22, deskY = y + (mini ? 14 : 20), deskW = w - 12, deskX = x + 6;
      // рабочая ячейка
      rr(x, y, w, h, 10, "rgba(255,255,255,.03)");
      // кресло + сотрудник (сверху от стола)
      const px = x + w / 2, py = deskY - (mini ? 8 : 11);
      ctx.beginPath(); ctx.arc(px, py, mini ? 9 : 12, 0, Math.PI * 2); ctx.fillStyle = C.chair; ctx.fill();
      avatar(px, py, mini ? 6.5 : 9, d.color, mini ? "" : initial(a.name), t, d.phase, inPos && !mini);
      // стол
      shadow(() => rr(deskX, deskY, deskW, deskH, 6, C.desk, C.deskEdge), 8, "rgba(0,0,0,.3)", 3);
      // монитор с подсветкой
      const mw = mini ? 14 : 20, mh = mini ? 9 : 12, mx = px - mw / 2, my = deskY + (deskH - mh) / 2;
      const blink = 0.6 + 0.4 * Math.sin(t / 800 + d.phase * 2);
      ctx.save(); ctx.shadowBlur = inPos ? 14 : 6; ctx.shadowColor = inPos ? C.glowBtc : C.glowIdle;
      rr(mx, my, mw, mh, 2, inPos ? `rgba(247,147,26,${0.55 + 0.45 * blink})` : `rgba(124,216,255,${0.25 + 0.3 * blink})`); ctx.restore();
      rr(mx - 1, my - 1, mw + 2, mh + 2, 2.5, null, C.monitor, 1.5);
      // мелочи на столе: чашка, блокнот
      if (!mini) { ctx.beginPath(); ctx.arc(deskX + 10, deskY + deskH / 2, 3, 0, Math.PI * 2); ctx.fillStyle = "#e8d8c0"; ctx.fill(); rr(deskX + deskW - 16, deskY + 5, 10, 12, 1.5, "#d8d2f0"); }
      // подписи
      const pnl = a.pnl_total, col = pnl > 0 ? C.up : pnl < 0 ? C.down : C.muted;
      const name = short(a.name);
      const ny = deskY + deskH + (mini ? 9 : 12);
      text(name.length > (mini ? 10 : 13) ? name.slice(0, mini ? 9 : 12) + "…" : name, px, ny, mini ? 9 : 11, C.text, 600, "center", "middle");
      if (!mini) text(money(pnl), px, ny + 14, 10, col, 600, "center", "middle");
      else { ctx.beginPath(); ctx.arc(px + Math.min(w / 2 - 6, 26), ny, 2.5, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill(); }
      if (a.status === "paused") pill(x + w - 16, y + 10, "⏸", "rgba(251,191,36,.25)", C.warn, 8, 3);
      d.head = { x: px, y: py - (mini ? 8 : 12) };
    }
    function drawCourier(c, t) {
      const k = clamp((t - c.t0) / c.dur, 0, 1), e = k < 0.5 ? 2 * k * k : -1 + (4 - 2 * k) * k;
      const mx = (c.from.x + c.to.x) / 2, my = Math.min(c.from.y, c.to.y) - 40;
      const x = (1 - e) * (1 - e) * c.from.x + 2 * (1 - e) * e * mx + e * e * c.to.x;
      const y = (1 - e) * (1 - e) * c.from.y + 2 * (1 - e) * e * my + e * e * c.to.y;
      ctx.setLineDash([3, 5]); ctx.beginPath(); ctx.moveTo(c.from.x, c.from.y); ctx.quadraticCurveTo(mx, my, c.to.x, c.to.y); ctx.strokeStyle = "rgba(247,147,26,.35)"; ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]);
      shadow(() => rr(x - 7, y - 9, 14, 18, 2, "#fff8e8"), 6);
      ctx.fillStyle = C.btc; ctx.fillRect(x - 4, y - 5, 8, 2); ctx.fillRect(x - 4, y - 1, 8, 2); ctx.fillRect(x - 4, y + 3, 5, 2);
      pill(x, y - 16, c.label, C.btc, "#1a1432", 9, 5);
    }

    // ---------- события анимации ----------
    function schedule(t) {
      if (t >= nextBubble) {
        nextBubble = t + 3000;
        for (let i = bubbles.length - 1; i >= 0; i--) if (bubbles[i].until < t) bubbles.splice(i, 1);
        if (Math.random() < 0.5) { const keys = ["head", "risk", "lab"]; const k = keys[Math.floor(Math.random() * 3)]; bubbles.push({ key: k, text: say[k](), t0: t, until: t + 4500 }); }
        else if (team.length) { const d = desks.filter((q) => q.kind === "team")[Math.floor(Math.random() * team.length)]; if (d && d.a.last_reason) bubbles.push({ desk: d, text: d.a.last_reason, t0: t, until: t + 4500 }); }
        if (bubbles.length > 2) bubbles.shift();
      }
      if (t >= nextCourier) {
        nextCourier = t + 6000;
        const recent = desks.filter((q) => q.kind === "team" && q.a.last_trade_ts && now - q.a.last_trade_ts < 7200);
        if (recent.length && roomPeople.risk) { const d = recent[Math.floor(Math.random() * recent.length)]; couriers.push({ from: { x: d.x + d.w / 2, y: d.y + 20 }, to: { x: roomPeople.risk.x, y: roomPeople.risk.y + 30 }, t0: t, dur: 3500, label: d.a.last_action === "SELL" ? "продажа" : "покупка" }); }
        for (let i = couriers.length - 1; i >= 0; i--) if (t - couriers[i].t0 > couriers[i].dur + 600) couriers.splice(i, 1);
      }
    }

    function draw(t) {
      if (stopped) return;
      drawFloor(t);
      drawHud(t);
      L.rooms.forEach((r) => drawRoom(r, t));
      text("КОМАНДА", L.teamLabel.x, L.teamLabel.y, 9, C.dim, 700);
      rr(L.internZone.x, L.internZone.y, L.internZone.w, L.internZone.h, 12, "rgba(169,156,255,.05)", "rgba(169,156,255,.18)");
      text(`СТАЖЁРЫ · ${interns.length}`, L.internLabel.x, L.internLabel.y, 9, C.dim, 700);
      desks.forEach((d) => drawDesk(d, t));
      couriers.forEach((c) => drawCourier(c, t));
      bubbles.forEach((b) => {
        const life = (t - b.t0) / (b.until - b.t0), alpha = life < 0.1 ? life / 0.1 : life > 0.85 ? (1 - life) / 0.15 : 1;
        const p = b.desk ? b.desk.head : roomPeople[b.key];
        if (p) bubble(p.x, p.y - 4, b.text, Math.min(220, W * 0.6), clamp(alpha, 0, 1));
      });
      schedule(t);
      raf = requestAnimationFrame(draw);
    }

    canvas.onclick = (ev) => {
      const b = canvas.getBoundingClientRect(); const x = ev.clientX - b.left, y = ev.clientY - b.top;
      const hit = desks.find((d) => x >= d.x && x <= d.x + d.w && y >= d.y && y <= d.y + d.h);
      if (hit && onAgentClick) onAgentClick(hit.a.name);
    };
    canvas.style.touchAction = "";
    layout();
    raf = requestAnimationFrame(draw);
    return { stop() { stopped = true; cancelAnimationFrame(raf); }, layout, fit: layout };
  };
})();
