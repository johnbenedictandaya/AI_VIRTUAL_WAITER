/* floor_plan.js - live dining-room map for the Perpetual Bistro dashboard.
 * Self-contained: it injects its own styles, a Kiosk | Floor switch into the header, and the Floor view.
 * Reuses the dashboard's CSS variables (--red, --yellow, --card, ...) so light/dark themes just work.
 * Data comes from GET /api/floor (see floor_plan.py) and is polled every 2s while the Floor view is open.
 */
(() => {
  'use strict';
  const API = '/api/floor';
  const STATUS = {
    free:      {label: 'Free',       icon: '', c: 'var(--st-free)'},
    reserved:  {label: 'Reserved',   icon: '', c: 'var(--st-reserved)'},
    seated:    {label: 'Seated',     icon: '', c: 'var(--st-seated)'},
    ordering:  {label: 'Ordering',   icon: '', c: 'var(--st-ordering)'},
    preparing: {label: 'In kitchen', icon: '', c: 'var(--st-preparing)'},
    dining:    {label: 'Dining',     icon: '', c: 'var(--st-dining)'},
    cleaning:  {label: 'Cleaning',   icon: '', c: 'var(--st-cleaning)'},
    blocked:   {label: 'Blocked',    icon: '', c: 'var(--st-blocked)'},
  };
  const OCCUPIED = ['seated', 'ordering', 'preparing', 'dining'];
  const KITCHEN = {QUEUED: 'Queued', PREPARING: 'Cooking', READY: 'Ready to serve', SERVED: 'Served'};
  const NEXT = {QUEUED: ['PREPARING', 'Start cooking'], PREPARING: ['READY', 'Mark ready'], READY: ['SERVED', 'Mark served']};

  let snap = null, selected = null, edit = false, zone = 'All', kioskTable = null;
  let drag = null, pending = false, offset = 0, view = 'kiosk', embedded = false;
  const keys = {};

  /* ------------------------------------------------------------ tiny helpers */
  const h = (tag, props, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'style') e.style.cssText = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? '' : v);
    }
    kids.flat(Infinity).forEach(c => { if (c != null && c !== false) e.append(c); });
    return e;
  };
  const $ = id => document.getElementById(id);
  const php = n => Number(n || 0).toLocaleString('en-PH', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' PHP';
  const php0 = n => Number(n || 0).toLocaleString('en-PH', {maximumFractionDigits: 0});
  const pad = n => String(n).padStart(2, '0');
  const nowMs = () => Date.now() + offset;
  function ago(iso) {
    if (!iso) return '';
    const m = Math.max(0, Math.floor((nowMs() - Date.parse(iso)) / 60000));
    return m < 1 ? '<1m' : m < 60 ? m + 'm' : Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
  }
  const clock = hhmm => { const [H, M] = hhmm.split(':').map(Number); return ((H + 11) % 12 + 1) + ':' + pad(M) + (H < 12 ? ' AM' : ' PM'); };
  const sig = o => JSON.stringify(o);
  function section(name, key, fn) { const k = sig(key); if (keys[name] === k) return; keys[name] = k; fn(); }

  let toastTimer = 0;
  function toast(msg) {
    const t = $('fpToast'); t.textContent = msg; t.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 4500);
  }
  async function call(path, method, body) {
    const r = await fetch(path, {method: method || 'GET', headers: {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)});
    return r.json();
  }
  async function act(path, method, body) {                  // mutate, then re-render from the fresh snapshot
    pending = true;
    try {
      const res = await call(path, method, body);
      if (!res.ok) toast(res.error || 'Something went wrong');
      if (res.floor) accept(res.floor);
      return res;
    } catch (e) { toast('Server unreachable.'); return {ok: false}; }
    finally { pending = false; }
  }
  function accept(s) {
    snap = s; offset = Date.parse(s.server_now) - Date.now();
    if (selected != null && !s.tables.some(t => t.table_no === selected)) selected = null;
    renderAll();
  }
  async function refresh() {
    if ((view !== 'floor' && !embedded) || document.hidden || drag || pending) return;
    try { accept(await call(API)); } catch (e) { /* offline: keep last picture */ }
  }

  /* ------------------------------------------------------------ styles */
  const CSS = `
  .fp-switch{display:flex;gap:2px;background:rgba(255,255,255,.08);padding:3px;border-radius:11px}
  .fp-switch button{border:0;background:transparent;color:var(--on-board-2);padding:6px 16px;border-radius:8px;font-weight:600}
  .fp-switch button:hover:not(.on){background:rgba(255,255,255,.1);color:var(--on-board)}
  .fp-switch button.on{background:var(--yellow);color:var(--on-yellow)}
  #floorView{display:none;flex:1;min-height:0;flex-direction:column;gap:14px;padding:18px 24px;overflow-y:auto}
  body.fp-on #floorView,#floorView.fp-embedded{display:flex}#floorView.fp-embedded{padding:0;overflow:visible;flex:none}body.fp-on main,body.fp-on #banner{display:none!important}
  #floorView .card{overflow:visible}
  .fp-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .fp-stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 16px;min-width:0}
  .fp-stat small{display:block;font-size:13px;font-weight:600;color:var(--muted)}
  .fp-stat b{display:block;margin:2px 0 1px;font:600 30px/1.1 var(--f-display);font-variant-numeric:tabular-nums;color:var(--ink)}
  .fp-stat span{font-size:12.5px;color:var(--muted)}
  .fp-bar{height:6px;border-radius:99px;background:var(--line);margin-top:8px;overflow:hidden}.fp-bar i{display:block;height:100%;background:var(--data);border-radius:99px}
  .fp-body{display:grid;grid-template-columns:minmax(0,1fr) 400px;gap:18px;align-items:start}
  @media (max-width:1100px){.fp-body{grid-template-columns:1fr}}
  .fp-side{display:flex;flex-direction:column;gap:14px;min-width:0}
  .fp-bar-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:6px 16px 0}
  .fp-bar-row .sp{flex:1}
  .fp-bar-row button{padding:5px 14px;font-size:13px;border-radius:99px}
  .fp-legend{display:flex;gap:6px 14px;flex-wrap:wrap;padding:12px 16px 0;font-size:12.5px;color:var(--muted);font-weight:600}
  .fp-legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px;background:var(--c)}
  .fp-canvas{position:relative;aspect-ratio:16/10;container-type:inline-size;margin:12px 16px 14px;border-radius:14px;border:1px solid var(--line-2);overflow:hidden;
    background-color:var(--soft);background-image:radial-gradient(color-mix(in srgb,var(--ink) 14%,transparent) 1px,transparent 1.3px);background-size:26px 26px}
  .fp-canvas.edit{outline:3px dashed var(--ink);outline-offset:-3px}
  .fp-zone{position:absolute;border:1.5px dashed color-mix(in srgb,var(--ink) 24%,transparent);border-radius:16px;pointer-events:none;transition:opacity .2s}
  .fp-zone span{position:absolute;top:-1px;left:12px;transform:translateY(-50%);background:var(--soft);padding:0 8px;font:600 max(11px,1.3cqw)/1.4 var(--f-body);color:var(--muted)}
  .fp-deco{position:absolute;display:grid;place-items:center;font:600 max(11px,1.25cqw)/1.2 var(--f-body);border-radius:10px;pointer-events:none}
  .fp-kitchen{left:30%;top:0;width:40%;height:7.5%;background:var(--board);color:var(--on-board);border-radius:0 0 12px 12px}
  .fp-door{left:2%;bottom:0;width:14%;height:6%;background:var(--yellow);color:var(--on-yellow);border-radius:10px 10px 0 0}
  .fp-table{position:absolute;transform:translate(-50%,-50%);border:2.5px solid var(--c);background:color-mix(in srgb,var(--c) 14%,var(--card));color:var(--ink);
    display:flex;flex-direction:column;align-items:center;justify-content:center;gap:.08em;line-height:1.1;font-size:max(10.5px,1.2cqw);cursor:pointer;user-select:none;touch-action:none;
    box-shadow:0 1px 0 rgba(0,0,0,.12);transition:background .25s,border-color .25s,opacity .2s}
  .fp-table.round{border-radius:50%}.fp-table.square,.fp-table.rect{border-radius:14%}
  .fp-table:hover{filter:brightness(1.04)}.fp-table:focus-visible{outline:3px solid var(--focus);outline-offset:3px}
  .fp-table.sel{outline:3px solid var(--ink);outline-offset:3px;z-index:3}
  .fp-table.dim{opacity:.28}.fp-table.drag{z-index:5;cursor:grabbing;opacity:.9}
  .fp-table.kiosk::after{content:"Kiosk";position:absolute;top:-1.9em;background:var(--red);color:var(--on-red);font-size:.78em;font-weight:700;padding:1px 8px;border-radius:99px;animation:fpPulse 1.8s ease-in-out infinite}
  @keyframes fpPulse{50%{transform:scale(1.1)}}
  .fp-table .n{font:600 1.95em/1 var(--f-display)}
  .fp-table .st{font-weight:700;white-space:nowrap;color:color-mix(in srgb,var(--c) 72%,var(--ink))}
  .fp-table .tm{font:500 .86em var(--f-mono);color:var(--muted);white-space:nowrap}
  .fp-table .tot{font-weight:600;color:var(--muted);font-size:.86em;white-space:nowrap;font-variant-numeric:tabular-nums}
  .fp-chair{position:absolute;width:1.7cqw;height:1.7cqw;border-radius:50%;background:var(--card);border:2px solid var(--c);transform:translate(-50%,-50%)}.fp-chair.on{background:var(--c)}
  .fp-hint{padding:0 18px 14px;font-size:12.5px;color:var(--muted)}
  .fp-pad{padding:6px 18px 16px}.fp-empty{color:var(--muted);text-align:center;padding:14px 0}
  .fp-pills{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
  .fp-pill{display:inline-flex;gap:5px;align-items:center;padding:3px 10px;border-radius:99px;font-size:12.5px;font-weight:600;background:var(--soft);border:1px solid var(--line)}
  .fp-pill.st{background:color-mix(in srgb,var(--c) 16%,var(--card));color:color-mix(in srgb,var(--c) 75%,var(--ink));border-color:color-mix(in srgb,var(--c) 45%,transparent)}
  .fp-lines .row{display:grid;grid-template-columns:1fr auto;gap:8px;padding:7px 0;border-bottom:1px solid var(--line)}
  .fp-lines .note{font-size:12px;color:var(--muted)}.fp-tot{display:flex;justify-content:space-between;align-items:baseline;padding-top:8px;font:600 20px var(--f-display)}
  .fp-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.fp-actions button{font-size:13.5px}
  .fp-actions label{display:flex;align-items:center;gap:6px;font-weight:600;font-size:13.5px}
  .fp-in{padding:6px 10px;border-radius:8px;border:1px solid var(--line-2);background:var(--card);color:var(--ink);font:inherit;min-width:0}
  .fp-in:focus-visible{border-color:var(--ink);outline-offset:1px}input.fp-in[type=number]{width:72px}
  .fp-steps{display:flex;gap:4px;margin:10px 0 2px}.fp-steps i{flex:1;text-align:center;font-style:normal;font-size:12px;font-weight:600;padding:4px 2px;border-radius:8px;background:var(--soft);color:var(--muted);border:1px solid var(--line)}
  .fp-steps i.on{background:color-mix(in srgb,var(--st-preparing) 18%,var(--card));color:color-mix(in srgb,var(--st-preparing) 75%,var(--ink));border-color:var(--st-preparing)}
  .fp-steps i.done{background:var(--ok-t);color:var(--ok);border-color:transparent}
  .fp-res,.fp-tk{display:grid;grid-template-columns:1fr auto;gap:2px 8px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line)}
  .fp-res small,.fp-tk small{color:var(--muted);grid-column:1}.fp-res .btns,.fp-tk .btns{grid-column:2;grid-row:1/3;display:flex;gap:6px}
  .fp-res button,.fp-tk button{padding:5px 11px;font-size:12.5px}
  .fp-res.soon b{color:var(--red)}
  .fp-form{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px 0 2px}.fp-form .wide{grid-column:1/-1}.fp-form .fp-in,.fp-form select{width:100%}
  details.fp-det summary{cursor:pointer;font-weight:700;padding:8px 0}
  .fp-low{display:flex;justify-content:space-between;padding:5px 0;font-size:13.5px}.fp-low b{color:var(--red)}
  .fp-top{display:grid;grid-template-columns:1fr auto;gap:2px 8px;font-size:13.5px;padding:4px 0}.fp-top .bar{grid-column:1/-1;height:5px;border-radius:9px;background:var(--line)}.fp-top .bar i{display:block;height:100%;border-radius:9px;background:var(--data)}
  .fp-h3{margin:14px 0 4px;font:600 15px var(--f-display);color:var(--ink)}
  #fpToast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--board);color:var(--on-board);padding:10px 18px;border-radius:12px;font-weight:600;display:none;z-index:6;max-width:min(92vw,560px);box-shadow:0 10px 30px rgba(0,0,0,.35)}
  #fpToast.show{display:block}`;

  /* ------------------------------------------------------------ mount */
  function mount() {
    document.head.append(h('style', {}, CSS));
    const kBtn = h('button', {class: 'on', onclick: () => setView('kiosk')}, 'Kiosk');
    const fBtn = h('button', {onclick: () => setView('floor')}, 'Floor');
    const top = document.querySelector('.top');
    top.insertBefore(h('div', {class: 'fp-switch', id: 'fpSwitch'}, kBtn, fBtn), top.querySelector('.steps') || top.lastChild);

    const view = h('section', {id: 'floorView', 'aria-label': 'Restaurant floor'},
      h('div', {class: 'fp-stats', id: 'fpStats'}),
      h('div', {class: 'fp-body'},
        h('section', {class: 'card'}, h('h2', {}, 'Dining room'),
          h('div', {class: 'fp-bar-row', id: 'fpZones'}), h('div', {class: 'fp-legend', id: 'fpLegend'}),
          h('div', {class: 'fp-canvas', id: 'fpCanvas'}), h('div', {class: 'fp-hint', id: 'fpHint'})),
        h('aside', {class: 'fp-side'},
          h('section', {class: 'card'}, h('h2', {id: 'fpDrawerH'}, 'Table'), h('div', {class: 'fp-pad', id: 'fpDrawer'})),
          h('section', {class: 'card'}, h('h2', {}, 'Kitchen queue'), h('div', {class: 'fp-pad', id: 'fpKitchen'})),
          h('section', {class: 'card'}, h('h2', {}, 'Reservations'), h('div', {class: 'fp-pad'}, h('div', {id: 'fpRes'}), buildResForm())),
          h('section', {class: 'card'}, h('h2', {}, 'Insights'), h('div', {class: 'fp-pad', id: 'fpInsights'})))),
      h('div', {id: 'fpToast', role: 'status'}));
    document.body.append(view);
    fBtn.dataset.k = 'floor'; kBtn.dataset.k = 'kiosk';

    /* follow the kiosk's current table so the map can highlight it */
    const chip = $('table');
    if (chip) new MutationObserver(readKiosk).observe(chip, {childList: true, characterData: true, subtree: true});
    readKiosk();
    window.addEventListener('hashchange', () => { if (location.hash !== '#manager') setView(location.hash === '#floor' ? 'floor' : 'kiosk', true); });
    document.addEventListener('visibilitychange', refresh);
    setInterval(refresh, 2000);
    setInterval(tickTimers, 15000);
    setView(location.hash === '#floor' ? 'floor' : 'kiosk', true);
  }
  function readKiosk() {
    const m = /(\d+)/.exec(($('table') || {}).textContent || '');
    const n = m ? +m[1] : null;
    if (n !== kioskTable) { kioskTable = n; if (snap) renderAll(); }
  }
  function setView(v, fromHash) {
    view = v;
    document.body.classList.toggle('fp-on', v === 'floor');
    document.querySelectorAll('#fpSwitch button').forEach(b => b.classList.toggle('on', b.dataset.k === v));
    if (!fromHash) { try { history.replaceState(null, '', v === 'floor' ? '#floor' : location.pathname + location.search); } catch (e) {} }
    if (v === 'floor') refresh();
  }
  function tickTimers() { document.querySelectorAll('[data-since]').forEach(e => { e.textContent = ago(e.dataset.since); }); }

  /* ------------------------------------------------------------ render */
  function renderAll() {
    if (!snap) return;
    renderStats(); renderZones(); renderCanvas(); renderDrawer(); renderKitchen(); renderRes(); renderInsights();
  }
  function renderStats() {
    const s = snap.stats;
    section('stats', s, () => {
      const card = (label, val, sub, bar) => h('div', {class: 'fp-stat'}, h('small', {}, label), h('b', {}, val), h('span', {}, sub),
        bar != null && h('div', {class: 'fp-bar'}, h('i', {style: `width:${bar}%`})));
      $('fpStats').replaceChildren(
        card('Occupancy', s.occupancy_pct + '%', `${s.occupied} of ${s.usable} tables`, s.occupancy_pct),
        card('Free now', s.free, `${s.reserved} reserved · ${s.cleaning} cleaning`),
        card('Seats in use', `${s.seats_in_use}/${s.seats_total}`, s.guests ? `${s.guests} guests counted` : 'by table capacity'),
        card('Kitchen queue', s.kitchen_queue, `${s.open_orders} guest(s) still ordering`),
        card("Today's sales", php0(s.sales_today_php), `PHP · ${s.orders_today} confirmed order(s)`),
        card('Avg. ticket', php0(s.avg_ticket_php), 'PHP per order'));
    });
  }
  function renderZones() {
    section('zones', [snap.zones, zone, edit], () => {
      if (zone !== 'All' && !snap.zones.includes(zone)) zone = 'All';
      const chip = z => h('button', {class: z === zone ? 'active' : '', onclick: () => { zone = z; renderAll(); }}, z);
      $('fpZones').replaceChildren(...[chip('All'), ...snap.zones.map(chip), h('span', {class: 'sp'}),
        edit ? h('button', {onclick: addTable}, '＋ Add table') : null,
        h('button', {class: edit ? 'active' : '', onclick: () => { edit = !edit; renderAll(); }}, edit ? 'Done editing' : 'Edit layout')].filter(Boolean));
      $('fpLegend').replaceChildren(...Object.entries(STATUS).map(([k, v]) =>
        h('span', {style: `--c:${v.c}`}, h('i'), v.label)));
    });
  }
  function tableBox(t) {                                     // table size in cqw (1cqw = 1% of the canvas width)
    if (t.shape === 'round') { const d = 7 + t.seats * .6; return [d, d]; }
    if (t.shape === 'rect') return [5 + t.seats * 2, 9];
    return [7.2 + t.seats * .4, 7.2 + t.seats * .4];
  }
  function chairs(t, w, hgt, on) {
    const off = 1.55, out = [];
    const mk = (l, tp, i) => h('i', {class: 'fp-chair' + (i < on ? ' on' : ''), style: `left:${l}%;top:${tp}%`});
    if (t.shape === 'round') {
      for (let i = 0; i < t.seats; i++) {
        const a = (-90 + 360 * i / t.seats) * Math.PI / 180, r = 50 + off / w * 100;
        out.push(mk(50 + r * Math.cos(a), 50 + r * Math.sin(a), i));
      }
    } else {
      const top = Math.ceil(t.seats / 2), bot = t.seats - top, dy = off / hgt * 100;
      for (let i = 0; i < top; i++) out.push(mk((i + .5) / top * 100, -dy, i));
      for (let i = 0; i < bot; i++) out.push(mk((i + .5) / bot * 100, 100 + dy, top + i));
    }
    return out;
  }
  function zoneRects(tables) {
    const g = {};
    tables.forEach(t => {
      const [w, hg] = tableBox(t), hh = hg * 1.6 / 2 + 3, ww = w / 2 + 2.6;   // 16:10 canvas -> cqw to % of height
      const b = g[t.zone] || (g[t.zone] = {x0: 1e9, y0: 1e9, x1: -1e9, y1: -1e9});
      b.x0 = Math.min(b.x0, t.x - ww); b.x1 = Math.max(b.x1, t.x + ww); b.y0 = Math.min(b.y0, t.y - hh); b.y1 = Math.max(b.y1, t.y + hh);
    });
    return Object.entries(g).map(([name, b]) => {
      const x0 = Math.max(.5, b.x0 - 1), y0 = Math.max(.5, b.y0 - 1), x1 = Math.min(99.5, b.x1 + 1), y1 = Math.min(99.5, b.y1 + 1);
      return h('div', {class: 'fp-zone', style: `left:${x0}%;top:${y0}%;width:${x1 - x0}%;height:${y1 - y0}%;opacity:${zone === 'All' || zone === name ? 1 : .25}`}, h('span', {}, name));
    });
  }
  function renderCanvas() {
    section('canvas', [snap.tables, snap.stats.kitchen_queue, selected, edit, zone, kioskTable], () => {
      const cv = $('fpCanvas'); cv.classList.toggle('edit', edit);
      const els = snap.tables.map(t => {
        const st = STATUS[t.status], [w, hg] = tableBox(t);
        const used = OCCUPIED.includes(t.status) ? (t.party_size || t.seats) : 0;
        const label = t.status === 'free' ? 'Free' : st.label;
        const when = t.status === 'reserved' ? clock(t.reservation.time)
          : ['free', 'cleaning', 'blocked'].includes(t.status) ? null
          : h('span', {'data-since': t.since}, ago(t.since));
        const el = h('div', {class: `fp-table ${t.shape} s-${t.status}` + (selected === t.table_no ? ' sel' : '') +
            (kioskTable === t.table_no ? ' kiosk' : '') + (zone !== 'All' && t.zone !== zone ? ' dim' : ''),
          role: 'button', tabindex: 0, 'data-no': t.table_no, style: `left:${t.x}%;top:${t.y}%;width:${w}cqw;height:${hg}cqw;--c:${st.c}`,
          'aria-label': `Table ${t.table_no}, ${t.seats} seats, ${st.label}`,
          title: `Table ${t.table_no} · ${t.zone} · ${t.seats} seats · ${st.label}` + (t.kitchen_status ? ' · ' + KITCHEN[t.kitchen_status] : ''),
          onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(t.table_no); } }},
          ...chairs(t, w, hg, used),
          h('span', {class: 'n'}, t.table_no),
          h('span', {class: 'st'}, label),
          when && h('span', {class: 'tm'}, when),
          t.total_php > 0 && h('span', {class: 'tot'}, php0(t.total_php) + ' PHP'));
        el.addEventListener('pointerdown', e => onDown(e, t, el));
        return el;
      });
      cv.replaceChildren(...zoneRects(snap.tables),
        h('div', {class: 'fp-deco fp-kitchen'}, `Kitchen: ${snap.stats.kitchen_queue} active`),
        h('div', {class: 'fp-deco fp-door'}, 'Entrance'), ...els);
      $('fpHint').textContent = edit ? 'Edit mode: drag tables to rearrange; tap one to change seats, zone or shape.'
        : 'Tap a table for its order and actions. Filled chairs = seats in use.';
    });
  }

  /* ---- drag to rearrange (edit mode) */
  let moved = false;
  function onDown(e, t, el) {
    moved = false;
    if (!edit) return;
    const rect = $('fpCanvas').getBoundingClientRect();
    drag = {t, el, rect, id: e.pointerId, x: t.x, y: t.y};
    el.setPointerCapture(e.pointerId); el.classList.add('drag'); e.preventDefault();
    el.onpointermove = ev => {
      if (!drag) return; moved = true;
      drag.x = Math.max(4, Math.min(96, (ev.clientX - rect.left) / rect.width * 100));
      drag.y = Math.max(4, Math.min(96, (ev.clientY - rect.top) / rect.height * 100));
      el.style.left = drag.x + '%'; el.style.top = drag.y + '%';
    };
    el.onpointerup = el.onpointercancel = async () => {
      const d = drag; drag = null; el.classList.remove('drag'); el.onpointermove = el.onpointerup = el.onpointercancel = null;
      if (moved && d) await act(`${API}/tables/${d.t.table_no}`, 'PUT', {x: d.x, y: d.y});
      else select(d.t.table_no);
    };
  }
  document.addEventListener('click', e => {                  // tap/click selects (drag path handles its own)
    const el = e.target.closest && e.target.closest('.fp-table');
    if (el && !edit) select(+el.dataset.no);
  });
  function select(no) { selected = selected === no && !edit ? null : no; renderAll(); }

  /* ---- drawer: the selected table */
  function renderDrawer() {
    const t = snap.tables.find(x => x.table_no === selected);
    section('drawer', [t, edit, snap.zones, kioskReady()], () => {
      const box = $('fpDrawer'), head = $('fpDrawerH');
      if (!t) { head.textContent = 'Table'; box.replaceChildren(h('div', {class: 'fp-empty'}, 'Tap a table on the map to see its order, timers and actions.')); return; }
      const st = STATUS[t.status];
      head.textContent = `Table ${pad(t.table_no)}`;
      const kids = [h('div', {class: 'fp-pills'},
        h('span', {class: 'fp-pill st', style: `--c:${st.c}`}, st.label + (t.kitchen_status && t.status === 'preparing' ? ' · ' + KITCHEN[t.kitchen_status] : '')),
        h('span', {class: 'fp-pill'}, '' + t.zone), h('span', {class: 'fp-pill'}, '' + t.seats + ' seats'),
        t.party_size && h('span', {class: 'fp-pill'}, 'party of ' + t.party_size),
        t.since && h('span', {class: 'fp-pill'}, '⏱ ', h('span', {'data-since': t.since}, ago(t.since))))];
      if (t.reservation) kids.push(h('div', {class: 'fp-pill', style: 'background:color-mix(in srgb,#5b5fc7 22%,var(--card))'},
        `${t.reservation.guest_name} · party of ${t.reservation.party_size} · ${clock(t.reservation.time)}`));
      if (t.lines.length) {
        kids.push(h('div', {class: 'fp-lines', style: 'margin-top:10px'},
          t.lines.map(l => h('div', {class: 'row'}, h('div', {}, `${l.qty} × ${l.item}`, l.notes && h('div', {class: 'note'}, l.notes)), h('div', {}, php(l.line_total_php)))),
          h('div', {class: 'fp-tot'}, h('span', {}, 'Total incl. service fee'), h('span', {}, php(t.total_php)))));
      } else if (t.status === 'ordering') kids.push(h('div', {class: 'fp-empty'}, 'Nothing on the tray yet.'));
      if (t.kitchen_status) {
        const i = Object.keys(KITCHEN).indexOf(t.kitchen_status);
        kids.push(h('div', {class: 'fp-steps'}, Object.values(KITCHEN).map((n, k) => h('i', {class: k < i ? 'done' : k === i ? 'on' : ''}, n))));
      }
      kids.push(edit ? editPanel(t) : actions(t));
      box.replaceChildren(...kids);
    });
  }
  function kioskReady() { const b = $('banner'); return !!(b && b.classList.contains('show')); }
  function actions(t) {
    const no = t.table_no, a = [];
    const btn = (txt, fn, cta) => h('button', {class: cta ? 'cta' : '', onclick: fn}, txt);
    if (['free', 'reserved', 'seated'].includes(t.status)) {
      const party = h('input', {class: 'fp-in', type: 'number', min: 1, max: t.seats, value: t.party_size || t.reservation?.party_size || Math.min(2, t.seats), 'aria-label': 'Party size'});
      a.push(h('label', {}, 'Party', party), btn(t.status === 'seated' ? 'Update party' : 'Seat guests',
        () => act(`${API}/seat`, 'POST', {table_no: no, party_size: party.value, res_id: t.reservation?.res_id}), t.status !== 'seated'));
      a.push(btn('Take order in waiter', () => openInWaiter(no)));
    }
    if (t.status === 'free') {
      a.push(btn('Reserve', () => prefillRes(no)), btn('Block', () => act(`${API}/state`, 'POST', {table_no: no, state: 'blocked'})));
    }
    if (t.status === 'reserved') {
      a.push(btn('Cancel booking', () => act(`${API}/reservations/${t.reservation.res_id}/status`, 'POST', {status: 'CANCELLED'})),
        btn('No-show', () => act(`${API}/reservations/${t.reservation.res_id}/status`, 'POST', {status: 'NO_SHOW'})));
    }
    if (t.status === 'seated') a.push(btn('↩ Cancel seating', () => act(`${API}/state`, 'POST', {table_no: no, state: 'free'})));
    if (t.status === 'ordering') {
      if (kioskTable === no) a.push(btn('Show in waiter', () => setView('kiosk'), true));
      a.push(btn('Cancel order & clear', () => confirm(`Cancel table ${no}'s open order?`) && act(`${API}/clear`, 'POST', {table_no: no})));
    }
    if (t.status === 'preparing') {
      const nx = NEXT[t.kitchen_status];
      if (nx) a.push(btn(nx[1], () => act(`${API}/kitchen`, 'POST', {session_id: t.session_id, status: nx[0]}), true));
    }
    if (t.status === 'dining') a.push(btn('Guests left - clear table', () => act(`${API}/clear`, 'POST', {table_no: no}), true));
    if (t.status === 'cleaning') a.push(btn('Mark clean & free', () => act(`${API}/state`, 'POST', {table_no: no, state: 'free'}), true));
    if (t.status === 'blocked') a.push(btn('Unblock', () => act(`${API}/state`, 'POST', {table_no: no, state: 'free'}), true));
    return h('div', {class: 'fp-actions'}, a);
  }
  function openInWaiter(no) {
    if (!kioskReady() || typeof window.send !== 'function') { toast('The kiosk is busy with another table. Press "New table" on the Kiosk tab first.'); return; }
    setView('kiosk'); window.send('table ' + no);
  }
  function editPanel(t) {
    const seats = h('input', {class: 'fp-in', type: 'number', min: 1, max: 20, value: t.seats});
    const zn = h('input', {class: 'fp-in', list: 'fpZoneList', value: t.zone, maxlength: 24});
    const shape = h('select', {class: 'fp-in'}, ['round', 'square', 'rect'].map(s => h('option', {value: s, selected: s === t.shape}, s)));
    return h('div', {}, h('div', {class: 'fp-form'},
      h('label', {}, 'Seats', seats), h('label', {}, 'Shape', shape),
      h('label', {class: 'wide'}, 'Zone', zn, h('datalist', {id: 'fpZoneList'}, snap.zones.map(z => h('option', {value: z}))))),
      h('div', {class: 'fp-actions'},
        h('button', {class: 'cta', onclick: () => act(`${API}/tables/${t.table_no}`, 'PUT', {seats: seats.value, zone: zn.value, shape: shape.value})}, 'Save table'),
        h('button', {onclick: () => confirm(`Delete table ${t.table_no}?`) && act(`${API}/tables/${t.table_no}`, 'DELETE')}, 'Delete')));
  }
  async function addTable() {
    const res = await act(`${API}/tables`, 'POST', {seats: 4, shape: 'square', zone: zone === 'All' ? 'Main Hall' : zone, x: 50, y: 50});
    if (res.ok) { const mx = Math.max(...snap.tables.map(t => t.table_no)); selected = mx; renderAll(); }
  }

  /* ---- kitchen queue */
  function renderKitchen() {
    section('kitchen', snap.kitchen, () => {
      const box = $('fpKitchen');
      if (!snap.kitchen.length) { box.replaceChildren(h('div', {class: 'fp-empty'}, 'Kitchen is clear. Confirmed orders show up here.')); return; }
      box.replaceChildren(...snap.kitchen.map(k => {
        const nx = NEXT[k.status];
        return h('div', {class: 'fp-tk'},
          h('b', {}, `Table ${k.table_no} · `, KITCHEN[k.status], ' · ', h('span', {'data-since': k.confirmed_at}, ago(k.confirmed_at))),
          h('small', {}, k.items.map(l => `${l.qty}× ${l.item}` + (l.notes ? ` (${l.notes})` : '')).join(', ')),
          h('div', {class: 'btns'}, nx && h('button', {class: 'cta', onclick: () => act(`${API}/kitchen`, 'POST', {session_id: k.session_id, status: nx[0]})}, nx[1])));
      }));
    });
  }

  /* ---- reservations */
  let resForm = {};
  function buildResForm() {
    const name = h('input', {class: 'fp-in wide', placeholder: 'Guest name', maxlength: 60, 'aria-label': 'Guest name'});
    const party = h('input', {class: 'fp-in', type: 'number', min: 1, max: 20, value: 2, 'aria-label': 'Party size'});
    const time = h('input', {class: 'fp-in', type: 'time', 'aria-label': 'Time'});
    const tbl = h('select', {class: 'fp-in', 'aria-label': 'Table'});
    const d = new Date(Date.now() + 45 * 60000); time.value = pad(d.getHours()) + ':' + pad(Math.ceil(d.getMinutes() / 5) * 5 % 60);
    resForm = {name, party, time, tbl};
    const det = h('details', {class: 'fp-det', id: 'fpResDet'}, h('summary', {}, '＋ New reservation'),
      h('div', {class: 'fp-form'}, name, h('label', {}, 'Party', party), h('label', {}, 'Time', time),
        h('label', {class: 'wide'}, 'Table', tbl),
        h('button', {class: 'cta wide', onclick: async () => {
          const r = await act(`${API}/reservations`, 'POST', {table_no: tbl.value, guest_name: name.value, party_size: party.value, time: time.value});
          if (r.ok) { name.value = ''; det.open = false; }
        }}, 'Book table')));
    party.addEventListener('input', fillTables);
    return det;
  }
  function fillTables() {
    if (!snap) return; const keep = resForm.tbl.value, need = +resForm.party.value || 1;
    resForm.tbl.replaceChildren(...snap.tables.filter(t => t.seats >= need).map(t =>
      h('option', {value: t.table_no, selected: String(t.table_no) === keep}, `Table ${t.table_no} · ${t.seats} seats · ${t.zone}`)));
  }
  function prefillRes(no) {
    const det = $('fpResDet'); det.open = true; fillTables(); resForm.tbl.value = no; resForm.name.focus();
    det.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  }
  function renderRes() {
    section('res', [snap.reservations, snap.tables.map(t => [t.table_no, t.seats, t.zone])], () => {
      fillTables();
      const box = $('fpRes'), today = new Date().toISOString().slice(0, 10);
      if (!snap.reservations.length) { box.replaceChildren(h('div', {class: 'fp-empty'}, 'No upcoming bookings.')); return; }
      box.replaceChildren(...snap.reservations.slice(0, 8).map(r => {
        const t = snap.tables.find(x => x.table_no === r.table_no), canSeat = t && ['free', 'reserved'].includes(t.status);
        const when = r.in_min < 0 ? `${-r.in_min} min late` : r.in_min < 90 ? `in ${r.in_min} min` : clock(r.time);
        return h('div', {class: 'fp-res' + (r.in_min <= 30 ? ' soon' : '')},
          h('b', {}, `${clock(r.time)} · ${r.guest_name}`),
          h('small', {}, `Table ${r.table_no} · party of ${r.party_size} · ${when}`),
          h('div', {class: 'btns'},
            canSeat && h('button', {class: 'cta', onclick: () => act(`${API}/seat`, 'POST', {table_no: r.table_no, party_size: r.party_size, res_id: r.res_id})}, 'Seat'),
            h('button', {title: 'Cancel booking', 'aria-label': 'Cancel booking', onclick: () => act(`${API}/reservations/${r.res_id}/status`, 'POST', {status: 'CANCELLED'})}, '✕')));
      }));
    });
  }

  /* ---- insights */
  function renderInsights() {
    section('insights', [snap.low_stock, snap.top_sellers], () => {
      const max = Math.max(1, ...snap.top_sellers.map(t => t.qty));
      $('fpInsights').replaceChildren(
        h('div', {class: 'fp-h3', style: 'margin-top:0'}, 'Low stock (≤ ' + snap.low_stock_threshold + ')'),
        snap.low_stock.length ? snap.low_stock.map(i => h('div', {class: 'fp-low'}, h('span', {}, i.name), h('b', {}, i.stock ? i.stock + ' left' : 'Sold out')))
          : h('div', {class: 'fp-empty'}, 'Everything is well stocked.'),
        h('div', {class: 'fp-h3'}, 'Top sellers today'),
        snap.top_sellers.length ? snap.top_sellers.map(i => h('div', {class: 'fp-top'}, h('span', {}, i.name), h('b', {}, i.qty),
          h('div', {class: 'bar'}, h('i', {style: `width:${i.qty / max * 100}%`}))))
          : h('div', {class: 'fp-empty'}, 'No confirmed orders yet today.'));
    });
  }

  function embed(el) { const v = $('floorView'); v.classList.add('fp-embedded'); el.append(v); embedded = true; refresh(); }
  function release() { const v = $('floorView'); v.classList.remove('fp-embedded'); document.body.append(v); embedded = false; }
  window.BistroFloor = {embed, release, refresh, select: no => { setView('floor'); selected = no; renderAll(); }, show: () => setView('floor'), hide: () => setView('kiosk')};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
