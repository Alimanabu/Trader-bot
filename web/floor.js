/* Изометрический торговый зал. Рисуется из живого состояния отдела.
   window.startFloor(canvas, state, equityPoints, onAgentClick) -> { stop, fit } */
(() => {
  const C = {
    bg: "#1a1432", floorA: "#2a2150", floorB: "#2f2558", grid: "rgba(170,150,255,.10)",
    wall: "#221a44", wallEdge: "rgba(180,160,255,.25)",
    deskTop: "#c4b3ff", deskSide: "#8d78d8", deskFront: "#6c58bf", monitor: "#120d26", screenOn: "#7cf2ff", screenIdle: "#4a3f7a",
    tag: "#22193f", tagText: "#efe9ff", up: "#4ade80", down: "#f87171", warn: "#fbbf24", btc: "#f7931a",
    glass: "rgba(150,130,255,.16)", glassEdge: "rgba(200,185,255,.55)", bubble: "#f4f1ff", bubbleText: "#241b45",
    skin: ["#f2c9a0", "#d9a577", "#b07a4f", "#f7d7bd"], hair: ["#2b1d3a", "#5a3b2e", "#e0b04a", "#8b3a3a", "#1f2a5a"],
    shirt: ["#ff7b5c", "#5cb8ff", "#8a7bff", "#ffd166", "#4ade80", "#f472b6", "#94a3b8"],
  };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const short = (n) => n.replace(/\s*\(.*\)/, "").replace("Нейро-", "Н-");
  const money = (v) => (v > 0 ? "+" : "") + v.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " $";
  const hash = (s) => { let h = 0; for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return h; };

  window.startFloor = function startFloor(canvas, state, equityPoints, onAgentClick) {
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const team = state.agents.filter((a) => a.status !== "fired");
    const interns = state.interns || [];
    const now = state.now;
    // --- сетка ---
    const TW = 44, TH = 22;                          // размер клетки в мировых координатах
    const iso = (gx, gy) => ({ x: (gx - gy) * TW / 2, y: (gx + gy) * TH / 2 });
    const view = { x: 0, y: 0, s: 1 };
    let W = 0, H = 0, HUD = 0, raf = 0, stopped = false;

    // --- расстановка ---
    const slots = [];
    const teamRows = [[4, 9], [4, 11], [4, 13]];
    const GW = 20, GH = 16;  // размер пола в клетках
    team.forEach((a, i) => { const row = Math.floor(i / 4), col = i % 4; slots.push({ kind: "team", a, gx: 6 + col * 3.2, gy: 8 + row * 2.6 }); });
    interns.slice(0, 20).forEach((a, i) => { const row = Math.floor(i / 5), col = i % 5; slots.push({ kind: "intern", a, gx: 8 + col * 2.3, gy: 0.8 + row * 1.8 }); });
    const rooms = [
      { key: "head", label: "Руководитель", gx: 0.5, gy: 0.5, w: 3.2, h: 3.2, color: "#e8e2ff", people: 1 },
      { key: "risk", label: "Риск-менеджер", gx: 0.5, gy: 4.5, w: 3.2, h: 3.2, color: C.warn, people: 1 },
      { key: "lab", label: "Исследования", gx: 0.5, gy: 8.5, w: 3.2, h: 4, color: "#9085e9", people: 2 },
    ];
    // персонажи
    const people = [];
    const mkPerson = (name, gx, gy, extra = {}) => { const h = hash(name); return { name, gx, gy, skin: C.skin[h % 4], hair: C.hair[(h >> 3) % 5], shirt: C.shirt[(h >> 6) % 7], phase: (h % 100) / 16, ...extra }; };
    slots.forEach((s) => { s.p = mkPerson(s.a.name, s.gx - 0.42, s.gy - 0.42, { slot: s }); people.push(s.p); });
    rooms.forEach((r) => { for (let i = 0; i < r.people; i++) { const p = mkPerson(r.key + i, r.gx + 0.9 + i * 1.1, r.gy + 1.6 + (i % 2) * 0.5, { room: r, stand: true }); people.push(p); } });

    // --- реплики ---
    const lastEvent = (kinds) => (state.events || []).find((e) => kinds.includes(e.kind));
    const roomSay = {
      head: () => { const e = lastEvent(["report", "hire", "fire", "approval"]); return e ? e.message : "Слежу за отделом"; },
      risk: () => { const paused = team.filter((a) => a.status === "paused").length; return state.department.halted ? "Отдел остановлен до завтра" : paused ? `${paused} на паузе до конца дня` : "Лимиты в норме, торгуем"; },
      lab: () => { const e = lastEvent(["research", "retune", "lesson", "intern", "drop"]); return e ? e.message : `${interns.length} стажёров на испытании`; },
    };
    const bubbles = []; // {p, text, until}
    let nextBubble = 0;
    const walkers = [];
    let nextWalker = 0;

    // --- размещение по экрану ---
    function layout() {
      W = canvas.clientWidth; H = canvas.clientHeight;
      canvas.width = W * dpr; canvas.height = H * dpr;
      HUD = Math.min(120, Math.max(92, H * 0.26));
      fit();
    }
    function bounds() {
      // границы по содержимому, а не по всему полу
      const pts = [];
      rooms.forEach((r) => { pts.push(iso(r.gx, r.gy)); pts.push(iso(r.gx + r.w, r.gy + r.h)); pts.push(iso(r.gx, r.gy + r.h)); pts.push(iso(r.gx + r.w, r.gy)); });
      slots.forEach((q) => { pts.push(iso(q.gx - 1, q.gy - 1)); pts.push(iso(q.gx + 1, q.gy + 1)); });
      const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
      return { minx: Math.min(...xs), maxx: Math.max(...xs), miny: Math.min(...ys), maxy: Math.max(...ys) };
    }
    function fit() {
      const b = bounds();
      const s = Math.min((W - 24) / (b.maxx - b.minx), (H - HUD - 40) / (b.maxy - b.miny + 30));
      view.s = s; view.x = W / 2 - ((b.minx + b.maxx) / 2) * s; view.y = HUD + 46 - b.miny * s;
    }
    const P = (gx, gy) => { const p = iso(gx, gy); return { x: view.x + p.x * view.s, y: view.y + p.y * view.s }; };

    // --- примитивы ---
    function tile(gx, gy, fill, stroke) {
      const a = P(gx - 0.5, gy - 0.5), b = P(gx + 0.5, gy - 0.5), c = P(gx + 0.5, gy + 0.5), d = P(gx - 0.5, gy + 0.5);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.lineTo(c.x, c.y); ctx.lineTo(d.x, d.y); ctx.closePath();
      if (fill) { ctx.fillStyle = fill; ctx.fill(); } if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke(); }
    }
    function box(gx, gy, w, h, z, top, left, right, edge) {
      // параллелепипед: основание (gx,gy) размером w×h клеток, высота z (в мировых единицах)
      const s = view.s, zz = z * s;
      const a = P(gx, gy), b = P(gx + w, gy), c = P(gx + w, gy + h), d = P(gx, gy + h);
      ctx.beginPath(); ctx.moveTo(d.x, d.y); ctx.lineTo(c.x, c.y); ctx.lineTo(c.x, c.y - zz); ctx.lineTo(d.x, d.y - zz); ctx.closePath(); ctx.fillStyle = left; ctx.fill(); if (edge) { ctx.strokeStyle = edge; ctx.stroke(); }
      ctx.beginPath(); ctx.moveTo(c.x, c.y); ctx.lineTo(b.x, b.y); ctx.lineTo(b.x, b.y - zz); ctx.lineTo(c.x, c.y - zz); ctx.closePath(); ctx.fillStyle = right; ctx.fill(); if (edge) ctx.stroke();
      ctx.beginPath(); ctx.moveTo(a.x, a.y - zz); ctx.lineTo(b.x, b.y - zz); ctx.lineTo(c.x, c.y - zz); ctx.lineTo(d.x, d.y - zz); ctx.closePath(); ctx.fillStyle = top; ctx.fill(); if (edge) ctx.stroke();
    }
    function pill(x, y, text, bg, fg, size = 10, pad = 5) {
      ctx.font = `600 ${size}px -apple-system, Segoe UI, Roboto, sans-serif`;
      const w = ctx.measureText(text).width + pad * 2, h = size + 6;
      ctx.beginPath(); ctx.roundRect(x - w / 2, y - h / 2, w, h, h / 2); ctx.fillStyle = bg; ctx.fill();
      ctx.fillStyle = fg; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(text, x, y + 0.5);
      return w;
    }
    function bubble(x, y, text, maxW) {
      ctx.font = "10px -apple-system, Segoe UI, Roboto, sans-serif";
      const words = text.split(" "), lines = []; let cur = "";
      for (const w of words) { const t = cur ? cur + " " + w : w; if (ctx.measureText(t).width > maxW && cur) { lines.push(cur); cur = w; } else cur = t; if (lines.length === 3) break; }
      if (cur && lines.length < 3) lines.push(cur);
      if (lines.length === 3 && words.length > lines.join(" ").split(" ").length) lines[2] = lines[2].replace(/\s?\S*$/, "…");
      const w = Math.min(maxW, Math.max(...lines.map((l) => ctx.measureText(l).width))) + 14, h = lines.length * 13 + 10;
      const bx = clamp(x - w / 2, 4, W - w - 4), by = y - h - 10;
      ctx.beginPath(); ctx.roundRect(bx, by, w, h, 7); ctx.fillStyle = C.bubble; ctx.fill();
      ctx.beginPath(); ctx.moveTo(x - 4, by + h); ctx.lineTo(x + 4, by + h); ctx.lineTo(x, by + h + 6); ctx.closePath(); ctx.fill();
      ctx.fillStyle = C.bubbleText; ctx.textAlign = "left"; ctx.textBaseline = "top";
      lines.forEach((l, i) => ctx.fillText(l, bx + 7, by + 6 + i * 13));
    }
    function person(p, t, scale = 1) {
      const s = view.s * 0.95 * scale, pos = P(p.gx, p.gy);
      const bob = Math.sin(t / 500 + p.phase) * 1.0 * s;
      const x = pos.x, y = pos.y - bob;
      const stand = p.stand || p.up;
      const bodyH = (stand ? 14 : 11) * s, bodyW = 11 * s, headS = 9 * s;
      ctx.fillStyle = "rgba(0,0,0,.25)"; ctx.beginPath(); ctx.ellipse(x, pos.y + 1.5 * s, 7 * s, 3 * s, 0, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = p.shirt; ctx.fillRect(x - bodyW / 2, y - bodyH, bodyW, bodyH);
      ctx.fillStyle = p.skin; ctx.fillRect(x - headS / 2, y - bodyH - headS, headS, headS);
      ctx.fillStyle = p.hair; ctx.fillRect(x - headS / 2, y - bodyH - headS, headS, 3 * s);
      ctx.fillStyle = "#1a1432"; ctx.fillRect(x - 2.4 * s, y - bodyH - headS + 4.5 * s, 1.6 * s, 1.6 * s); ctx.fillRect(x + 0.8 * s, y - bodyH - headS + 4.5 * s, 1.6 * s, 1.6 * s);
      ctx.fillStyle = p.skin; ctx.fillRect(x - bodyW / 2 - 2.5 * s, y - bodyH + 1 * s, 2.5 * s, 6 * s); ctx.fillRect(x + bodyW / 2, y - bodyH + 1 * s, 2.5 * s, 6 * s);
      if (p.up) { ctx.fillStyle = p.skin; ctx.fillRect(x + bodyW / 2, y - bodyH - 3 * s + Math.sin(t / 150) * s, 2.5 * s, 7 * s); }
      return { x, y: y - bodyH - headS };
    }
    function desk(slot, t) {
      const { gx, gy, a, kind } = slot;
      const size = kind === "team" ? 1 : 0.8;
      const inPos = a.exposure > 0;
      box(gx - size * 0.55, gy - size * 0.3, size * 1.1, size * 0.6, 7, C.deskTop, C.deskFront, C.deskSide);
      // монитор
      const m = P(gx, gy - size * 0.05); const s = view.s;
      const mw = 11 * s * size, mh = 8 * s * size, my = m.y - 7 * s - mh - 2 * s;
      ctx.fillStyle = C.monitor; ctx.fillRect(m.x - mw / 2 - 1, my - 1, mw + 2, mh + 2);
      const blink = 0.55 + 0.45 * Math.sin(t / 700 + slot.p.phase * 3);
      ctx.fillStyle = inPos ? `rgba(247,147,26,${0.5 + 0.5 * blink})` : `rgba(124,242,255,${0.25 + 0.35 * blink})`;
      ctx.fillRect(m.x - mw / 2, my, mw, mh);
      if (inPos) { ctx.fillStyle = "#fff3"; ctx.fillRect(m.x - mw * 0.3, my + mh * 0.3, mw * 0.15, mh * 0.5); ctx.fillRect(m.x, my + mh * 0.15, mw * 0.15, mh * 0.6); }
      ctx.fillStyle = C.monitor; ctx.fillRect(m.x - 1.5 * s, my + mh + 1, 3 * s, 2 * s);
    }
    function tags(slot) {
      const { gx, gy, a, kind } = slot;
      const top = P(gx, gy - 0.9); const s = view.s;
      const y = top.y - 22 * s - (kind === "team" ? 12 : 8);
      const pnl = a.pnl_total, col = pnl > 0 ? C.up : pnl < 0 ? C.down : "#9d95c4";
      if (kind === "intern") { if (view.s < 0.7) return; pill(top.x, y, short(a.name).slice(0, 12), "rgba(34,25,63,.85)", "#c9c1e8", 8, 4); return; }
      const name = short(a.name).slice(0, 12);
      const w = pill(top.x, y, name, C.tag, C.tagText, 10, 6);
      ctx.beginPath(); ctx.arc(top.x + w / 2 + 2, y - 7, 4, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill();
      if (view.s >= 0.9) pill(top.x, y - 15, money(pnl), "rgba(19,15,38,.9)", col, 9, 4);
      if (a.status === "paused") pill(top.x - w / 2 - 2, y - 7, "⏸", "rgba(251,191,36,.25)", C.warn, 8, 3);
    }
    function room(r, t) {
      box(r.gx, r.gy, r.w, r.h, 26, "rgba(0,0,0,0)", C.glass, C.glass, C.glassEdge);
      for (let i = 0; i < r.w; i++) for (let j = 0; j < r.h; j++) tile(r.gx + i + 0.5, r.gy + j + 0.5, "rgba(150,130,255,.08)");
      const c = P(r.gx + r.w / 2, r.gy);
      pill(c.x, c.y - 26 * view.s - 12, r.label, r.color, "#1a1432", 10);
    }
    function hud(t) {
      const d = state.department, dayPnl = team.reduce((x, a) => x + a.pnl_day, 0);
      ctx.fillStyle = "#130f26"; ctx.fillRect(0, 0, W, HUD);
      ctx.fillStyle = "rgba(124,242,255,.06)"; ctx.fillRect(0, HUD - 2, W, 2);
      ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
      ctx.fillStyle = "#8e86b8"; ctx.font = "600 9px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.fillText("ТОРГОВЫЙ ЗАЛ · ТАБЛО ОТДЕЛА · ДЕМОСЧЁТ", 12, 16);
      ctx.fillStyle = "#f4f1ff"; ctx.font = "700 26px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.fillText(d.equity.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " $", 12, 44);
      ctx.font = "600 12px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.fillStyle = dayPnl > 0 ? C.up : dayPnl < 0 ? C.down : "#c9c1e8"; ctx.fillText(money(dayPnl) + " сегодня", 12, 62);
      ctx.fillStyle = d.pnl > 0 ? C.up : d.pnl < 0 ? C.down : "#c9c1e8"; ctx.fillText(money(d.pnl) + " всего", 12, 78);
      // мини-график
      const gx0 = Math.max(180, W * 0.42), gw = W - gx0 - 12, gy0 = 22, gh = HUD - 48;
      ctx.fillStyle = "rgba(255,255,255,.03)"; ctx.fillRect(gx0, gy0, gw, gh);
      const pts = equityPoints || [];
      if (pts.length > 1 && gw > 40) {
        const ys = pts.map((p) => p[1]), mn = Math.min(...ys), mx = Math.max(...ys), sp = mx - mn || 1;
        ctx.beginPath(); ctx.lineWidth = 1.5; ctx.strokeStyle = ys[ys.length - 1] >= ys[0] ? C.screenOn : C.down;
        pts.forEach((p, i) => { const x = gx0 + (i / (pts.length - 1)) * gw, y = gy0 + gh - ((p[1] - mn) / sp) * (gh - 6) - 3; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
        ctx.stroke();
        const bl = Math.floor(t / 600) % 2 === 0; if (bl) { const last = pts[pts.length - 1]; const x = gx0 + gw, y = gy0 + gh - ((last[1] - mn) / sp) * (gh - 6) - 3; ctx.fillStyle = ctx.strokeStyle; ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill(); }
      } else { ctx.fillStyle = "#5d5590"; ctx.font = "10px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.fillText("график накапливается", gx0 + 8, gy0 + gh / 2 + 4); }
      ctx.fillStyle = "#8e86b8"; ctx.font = "9px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.textAlign = "right";
      ctx.fillText(`${window.astanaClock ? window.astanaClock() : ""} · BTC ${state.price.toLocaleString("ru-RU", { maximumFractionDigits: 0 })} $`, W - 12, 16);
      // бегущая строка событий
      const line = (state.events || []).slice(0, 8).map((e) => `${e.message}`).join("     •     ") || "ждём первую свечу";
      ctx.save(); ctx.beginPath(); ctx.rect(0, HUD - 18, W, 16); ctx.clip();
      ctx.font = "10px -apple-system, Segoe UI, Roboto, sans-serif"; ctx.textAlign = "left"; ctx.fillStyle = "#b7aee0";
      const tw = ctx.measureText(line).width + 120; const off = (t / 40) % tw;
      ctx.fillText(line, W - off, HUD - 6); ctx.fillText(line, W - off + tw, HUD - 6);
      ctx.restore();
    }

    // --- анимационные события ---
    function scheduleBubbles(t) {
      if (t < nextBubble) return;
      nextBubble = t + 2600;
      const r = rooms[Math.floor(Math.random() * rooms.length)];
      if (Math.random() < 0.45) {
        const p = people.find((q) => q.room === r);
        bubbles.push({ p, text: roomSay[r.key](), until: t + 4200 });
      } else if (team.length) {
        const a = team[Math.floor(Math.random() * team.length)];
        const s = slots.find((q) => q.a === a);
        if (s && a.last_reason) bubbles.push({ p: s.p, text: a.last_reason, until: t + 4200 });
      }
      for (let i = bubbles.length - 1; i >= 0; i--) if (bubbles[i].until < t) bubbles.splice(i, 1);
      if (bubbles.length > 2) bubbles.shift();
    }
    function scheduleWalkers(t) {
      if (t < nextWalker) return;
      nextWalker = t + 5000;
      const recent = slots.filter((s) => s.kind === "team" && s.a.last_trade_ts && now - s.a.last_trade_ts < 7200);
      if (!recent.length) return;
      const s = recent[Math.floor(Math.random() * recent.length)];
      const risk = rooms[1];
      walkers.push({ p: mkPerson("w" + s.a.name + t, s.p.gx, s.p.gy, { stand: true }), from: { gx: s.p.gx, gy: s.p.gy }, to: { gx: risk.gx + 1.5, gy: risk.gy + 3.6 }, t0: t, dur: 3200, label: s.a.last_action === "SELL" ? "продажа" : "покупка" });
    }

    // --- кадр ---
    function draw(t) {
      if (stopped) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
      ctx.save(); ctx.beginPath(); ctx.rect(0, HUD, W, H - HUD); ctx.clip();
      // пол
      for (let gx = 0; gx < GW; gx++) for (let gy = 0; gy < GH; gy++) tile(gx, gy, (gx + gy) % 2 ? C.floorA : C.floorB, C.grid);
      // задние стены
      const wl = [P(-0.5, -0.5), P(GW - 0.5, -0.5)], wl2 = [P(-0.5, -0.5), P(-0.5, GH - 0.5)];
      const wh = 60 * view.s;
      ctx.fillStyle = C.wall; ctx.beginPath(); ctx.moveTo(wl[0].x, wl[0].y); ctx.lineTo(wl[1].x, wl[1].y); ctx.lineTo(wl[1].x, wl[1].y - wh); ctx.lineTo(wl[0].x, wl[0].y - wh); ctx.closePath(); ctx.fill(); ctx.strokeStyle = C.wallEdge; ctx.stroke();
      ctx.beginPath(); ctx.moveTo(wl2[0].x, wl2[0].y); ctx.lineTo(wl2[1].x, wl2[1].y); ctx.lineTo(wl2[1].x, wl2[1].y - wh); ctx.lineTo(wl2[0].x, wl2[0].y - wh); ctx.closePath(); ctx.fill(); ctx.stroke();
      // объекты по глубине
      const items = [];
      rooms.forEach((r) => items.push({ d: r.gx + r.gy + r.w + r.h - 2, f: () => room(r, t) }));
      slots.forEach((s) => { items.push({ d: s.p.gx + s.p.gy, f: () => { const h = person(s.p, t, s.kind === "team" ? 1 : 0.85); s.head = h; } }); items.push({ d: s.gx + s.gy + 0.01, f: () => desk(s, t) }); });
      people.filter((p) => p.room).forEach((p) => items.push({ d: p.gx + p.gy, f: () => { p.head = person(p, t); } }));
      walkers.forEach((w) => { const k = Math.min(1, (t - w.t0) / w.dur); const e = k < 0.5 ? 2 * k * k : -1 + (4 - 2 * k) * k; w.p.gx = w.from.gx + (w.to.gx - w.from.gx) * e; w.p.gy = w.from.gy + (w.to.gy - w.from.gy) * e; w.p.up = true; items.push({ d: w.p.gx + w.p.gy + 0.5, f: () => { const h = person(w.p, t); pill(h.x, h.y - 10, w.label, C.btc, "#1a1432", 9, 4); } }); });
      items.sort((a, b) => a.d - b.d).forEach((i) => i.f());
      for (let i = walkers.length - 1; i >= 0; i--) if (t - walkers[i].t0 > walkers[i].dur + 800) walkers.splice(i, 1);
      // бирки поверх
      slots.forEach(tags);
      bubbles.forEach((b) => { if (b.p.head) bubble(b.p.head.x, b.p.head.y - 6, b.text, Math.min(200, W * 0.5)); });
      ctx.restore();
      hud(t);
      scheduleBubbles(t); scheduleWalkers(t);
      raf = requestAnimationFrame(draw);
    }

    // --- управление: перетаскивание, зум, клик ---
    let drag = null, pinch = null, moved = false;
    const toLocal = (ev) => { const b = canvas.getBoundingClientRect(); const src = ev.touches ? ev.touches[0] : ev; return { x: src.clientX - b.left, y: src.clientY - b.top }; };
    const zoomAt = (px, py, k) => { const ns = clamp(view.s * k, 0.35, 4); view.x = px - (px - view.x) * (ns / view.s); view.y = py - (py - view.y) * (ns / view.s); view.s = ns; };
    canvas.addEventListener("pointerdown", (ev) => { drag = { x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y }; moved = false; canvas.setPointerCapture(ev.pointerId); });
    canvas.addEventListener("pointermove", (ev) => { if (!drag || pinch) return; const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y; if (Math.hypot(dx, dy) > 4) moved = true; view.x = drag.vx + dx; view.y = drag.vy + dy; });
    canvas.addEventListener("pointerup", (ev) => {
      if (drag && !moved) { const l = toLocal(ev); const hit = slots.find((s) => s.head && Math.hypot(s.head.x - l.x, s.head.y + 14 - l.y) < 22); if (hit && onAgentClick) onAgentClick(hit.a.name); }
      drag = null;
    });
    canvas.addEventListener("pointercancel", () => (drag = null));
    canvas.addEventListener("wheel", (ev) => { ev.preventDefault(); const l = toLocal(ev); zoomAt(l.x, l.y, ev.deltaY < 0 ? 1.12 : 0.9); }, { passive: false });
    canvas.addEventListener("touchstart", (ev) => { if (ev.touches.length === 2) { pinch = { d: Math.hypot(ev.touches[0].clientX - ev.touches[1].clientX, ev.touches[0].clientY - ev.touches[1].clientY) }; } }, { passive: true });
    canvas.addEventListener("touchmove", (ev) => { if (ev.touches.length === 2 && pinch) { ev.preventDefault(); const d = Math.hypot(ev.touches[0].clientX - ev.touches[1].clientX, ev.touches[0].clientY - ev.touches[1].clientY); const b = canvas.getBoundingClientRect(); zoomAt((ev.touches[0].clientX + ev.touches[1].clientX) / 2 - b.left, (ev.touches[0].clientY + ev.touches[1].clientY) / 2 - b.top, d / pinch.d); pinch.d = d; } }, { passive: false });
    canvas.addEventListener("touchend", () => (pinch = null));
    canvas.addEventListener("dblclick", fit);
    canvas.style.touchAction = "none";

    layout();
    raf = requestAnimationFrame(draw);
    return { stop() { stopped = true; cancelAnimationFrame(raf); }, fit, layout };
  };
})();
