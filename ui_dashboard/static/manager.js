/* manager.js - PIN-protected Manager mode for the Perpetual Bistro dashboard.
 * Self-contained like floor_plan.js: injects its own styles, a "Manager" button in the header switch and the Manager view.
 * Tabs: Overview | Live floor (the floor map, embedded) | Inventory | Sales | Orders | Team & costs | Settings.
 * Data comes from /api/manager/* (manager.py); every call carries the session token returned by the PIN login.
 */
(() => {
  'use strict';
  const API = '/api/manager';
  const TABS = [['overview', 'Overview'], ['floor', 'Live floor'], ['inventory', 'Inventory'], ['sales', 'Sales'],
    ['orders', 'Orders'], ['team', 'Team & costs'], ['settings', 'Settings']];

  let token = '', tab = 'overview', defaultPin = false, salesRange = '7d', teamRange = '7d', ordersFilter = 'ALL', invFilter = 'ALL', invCat = 'All', invQuery = '';
  let itemSort = {k: 'sales', dir: -1}, loading = false;
  try { token = sessionStorage.getItem('bistroMgr') || ''; } catch (e) {}

  /* ------------------------------------------------------------ helpers */
  const h = (tag, props, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v; else if (k === 'style') e.style.cssText = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? '' : v);
    }
    kids.flat(Infinity).forEach(c => { if (c != null && c !== false) e.append(c); });
    return e;
  };
  const NS = 'http://www.w3.org/2000/svg';
  const sv = (tag, attrs, ...kids) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v); kids.forEach(c => c != null && e.append(c)); return e; };
  const $ = id => document.getElementById(id);
  const fmt = (n, d = 0) => Number(n || 0).toLocaleString('en-PH', {minimumFractionDigits: d, maximumFractionDigits: d});
  const php = n => fmt(n, 2) + ' PHP';
  const php0 = n => '₱' + fmt(n, 0);
  const compact = n => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k' : String(Math.round(n));
  const pad = n => String(n).padStart(2, '0');
  const dayLabel = iso => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString('en', {weekday: 'short', day: 'numeric'}); };
  const hourLabel = x => x === 0 ? '12a' : x < 12 ? x + 'a' : x === 12 ? '12p' : (x - 12) + 'p';
  const timeAgo = iso => { const m = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 60000)); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.floor(m / 60) + ' h ago' : Math.floor(m / 1440) + ' d ago'; };
  const stamp = new Map();                                    // element -> last render key
  function put(el, key, fn) { const k = JSON.stringify(key); if (stamp.get(el) === k) return; stamp.set(el, k); fn(); }

  let toastTimer = 0;
  function toast(msg, bad) {
    const t = $('mgToast'); t.textContent = msg; t.className = 'show' + (bad ? ' bad' : '');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.className = ''; }, 4500);
  }
  async function api(path, method, body, raw) {
    const r = await fetch(API + path, {method: method || 'GET', headers: {'Content-Type': 'application/json', 'X-Manager-Token': token},
      body: body === undefined ? undefined : JSON.stringify(body)});
    if (r.status === 401 && path !== '/login') { lock(true); throw new Error('locked'); }
    if (raw) return r;
    const j = await r.json();
    if (!j.ok && j.error) toast(j.error, true);
    return j;
  }
  async function download(path, name) {
    try { const r = await api(path, 'GET', undefined, true); if (!r.ok) throw 0;
      const url = URL.createObjectURL(await r.blob()); const a = h('a', {href: url, download: name}); document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    } catch (e) { toast('Download failed', true); }
  }

  /* modal form: ask({title, fields, ok}) -> values | null */
  function ask({title, fields = [], ok = 'Save', note}) {
    return new Promise(res => {
      const inputs = {};
      const rows = fields.map(f => {
        const el = f.options ? h('select', {class: 'mg-in'}, f.options.map(o => h('option', {value: o.value ?? o, selected: (o.value ?? o) === f.value}, o.label ?? o)))
          : h('input', {class: 'mg-in', type: f.type || 'text', value: f.value ?? '', placeholder: f.placeholder || '', min: f.min, max: f.max, step: f.step, maxlength: f.maxlength || 80,
              autocomplete: 'off', inputmode: f.type === 'number' ? 'decimal' : null});
        inputs[f.name] = el; return h('label', {class: 'mg-fld'}, f.label, el);
      });
      const close = v => { box.remove(); res(v); };
      const submit = () => close(Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value])));
      const box = h('div', {class: 'mg-modal', onclick: e => e.target === box && close(null), onkeydown: e => { if (e.key === 'Escape') close(null); if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') submit(); }},
        h('div', {class: 'mg-dlg', role: 'dialog', 'aria-label': title}, h('h3', {}, title), note && h('p', {class: 'mg-muted'}, note), ...rows,
          h('div', {class: 'mg-row end'}, h('button', {onclick: () => close(null)}, 'Cancel'), h('button', {class: 'cta', onclick: submit}, ok))));
      document.body.append(box); (Object.values(inputs)[0] || box).focus();
    });
  }

  /* ------------------------------------------------------------ charts (single series, one hue, hover tips) */
  function tip(e, text) {
    const t = $('mgTip'); if (!text) { t.style.display = 'none'; return; }
    t.textContent = text; t.style.display = 'block';
    t.style.left = Math.min(e.clientX + 12, innerWidth - t.offsetWidth - 8) + 'px'; t.style.top = (e.clientY + 14) + 'px';
  }
  function bars(data, {label, value, fmtVal = php0, height = 190, title}) {
    const W = 600, H = height, L = 38, B = 24, T = 8, R = 6, max = Math.max(1, ...data.map(value));
    const nice = (() => { const p = Math.pow(10, Math.floor(Math.log10(max))), f = max / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p; })();
    const iw = W - L - R, ih = H - T - B, bw = iw / data.length, g = Math.min(8, bw * .3);
    const svg = sv('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': title || 'Bar chart', class: 'mg-chart'});
    [0, .5, 1].forEach(f => {
      const y = T + ih * (1 - f);
      svg.append(sv('line', {x1: L, x2: W - R, y1: y, y2: y, class: 'grid'}), Object.assign(sv('text', {x: L - 6, y: y + 3, 'text-anchor': 'end', class: 'ax'}), {textContent: compact(nice * f)}));
    });
    const every = Math.ceil(data.length / 12);
    data.forEach((d, i) => {
      const v = value(d), bh = Math.max(v > 0 ? 2 : 0, v / nice * ih), x = L + i * bw + g / 2, w = Math.max(2, bw - g), y = T + ih - bh, r = Math.min(4, w / 2, bh);
      const bar = sv('path', {d: bh ? `M${x},${T + ih} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${T + ih} Z` : '', class: 'bar'});
      const hit = sv('rect', {x: L + i * bw, y: T, width: bw, height: ih, fill: 'transparent'});
      const msg = `${label(d)}: ${fmtVal(v)}` + (d.orders != null ? ` · ${d.orders} order(s)` : '');
      hit.addEventListener('mousemove', e => tip(e, msg)); hit.addEventListener('mouseleave', e => tip(e, null));
      svg.append(bar, hit);
      if (i % every === 0) svg.append(Object.assign(sv('text', {x: x + w / 2, y: H - 7, 'text-anchor': 'middle', class: 'ax'}), {textContent: label(d, true)}));
    });
    return svg;
  }
  function hbars(rows, {name, value, fmtVal = php0}) {
    const max = Math.max(1, ...rows.map(value));
    return h('div', {}, rows.map(r => h('div', {class: 'mg-hb'}, h('span', {}, name(r)), h('b', {}, fmtVal(value(r))), h('div', {class: 'bar'}, h('i', {style: `width:${value(r) / max * 100}%`})))));
  }

  /* ------------------------------------------------------------ styles */
  const CSS = `
   #mgrView{display:none;flex:1;min-height:0;flex-direction:column;overflow:hidden;position:relative}
   body.mg-on #mgrView{display:flex}body.mg-on main,body.mg-on #banner{display:none!important}
   .mg-tabs{display:flex;gap:6px;overflow-x:auto;padding:14px 24px 10px;background:var(--bg);flex:none}
   .mg-tabs button{white-space:nowrap;border-color:transparent;background:transparent;color:var(--muted);border-radius:99px;padding:7px 16px}
   .mg-tabs button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
   .mg-tabs button:hover:not(.on){background:var(--card);color:var(--ink);border-color:var(--line)}
   #mgPanel{flex:1;min-height:0;overflow-y:auto;padding:6px 24px 28px}
   .mg-muted{color:var(--muted);font-size:13px}.mg-empty{color:var(--muted);text-align:center;padding:18px 0}
   .mg-grid{display:grid;gap:14px}.mg-kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-bottom:14px}
   .mg-2{grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));margin-bottom:14px;align-items:start}
   .mg-kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 16px;min-width:0}
   .mg-kpi small{display:block;font-size:13px;font-weight:600;color:var(--muted)}
   .mg-kpi b{display:block;margin:2px 0 1px;font:600 30px/1.1 var(--f-display);font-variant-numeric:tabular-nums}.mg-kpi span{font-size:12.5px;color:var(--muted)}
   .mg-kpi.good b{color:var(--ok)}.mg-kpi.bad b{color:var(--red)}.mg-kpi.plain b{color:var(--ink)}
   .mg-card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);min-width:0}
   .mg-card>h2{margin:0;font:600 19px/1.2 var(--f-display);padding:16px 18px 4px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
   .mg-card>h2 .sp{flex:1}.mg-card>h2 button{padding:4px 11px;font:600 12.5px var(--f-body)}
   .mg-body{padding:8px 18px 16px}
   .mg-chart{width:100%;height:auto;display:block}.mg-chart .grid{stroke:var(--line);stroke-width:1}.mg-chart .ax{fill:var(--muted);font-size:10.5px;font-family:inherit}.mg-chart .bar{fill:var(--data)}
   #mgTip{position:fixed;display:none;background:var(--board);color:var(--on-board);padding:5px 10px;border-radius:8px;font-size:12px;font-weight:600;pointer-events:none;z-index:30;white-space:nowrap}
   .mg-hb{display:grid;grid-template-columns:1fr auto;gap:2px 10px;font-size:13.5px;padding:4px 0}.mg-hb .bar{grid-column:1/-1;height:6px;border-radius:9px;background:var(--line)}.mg-hb .bar i{display:block;height:100%;border-radius:9px;background:var(--data)}
   .mg-alert{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--line);font-size:14px;cursor:pointer}.mg-alert:hover b{text-decoration:underline}
   .mg-alert .dot{flex:none;width:18px;text-align:center;font-size:12px;line-height:1.9}
   .mg-alert.bad .dot{color:var(--red)}.mg-alert.warn .dot{color:var(--st-preparing)}.mg-alert.info .dot{color:var(--st-reserved)}.mg-alert.bad b{color:var(--red)}
   .mg-tbl{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
   .mg-tbl th{position:sticky;top:0;background:var(--card);text-align:left;font-size:12.5px;font-weight:700;color:var(--muted);padding:8px 8px;border-bottom:1px solid var(--line-2);white-space:nowrap}
   .mg-tbl th.sort{cursor:pointer}.mg-tbl td{padding:8px 8px;border-bottom:1px solid var(--line);vertical-align:middle}.mg-tbl .num{text-align:right}.mg-tbl tr.out td{background:color-mix(in srgb,var(--red) 7%,transparent)}
   .mg-wrap{overflow-x:auto}
   .mg-in{padding:6px 10px;border-radius:8px;border:1px solid var(--line-2);background:var(--card);color:var(--ink);font:inherit;min-width:0;width:100%}
   .mg-in:focus-visible{border-color:var(--ink);outline-offset:1px}td .mg-in{width:84px;text-align:right}
   .mg-pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600;background:var(--soft);border:1px solid var(--line);white-space:nowrap}
   .mg-pill.ok,.mg-pill.CONFIRMED{background:var(--ok-t);color:var(--ok);border-color:transparent}.mg-pill.low,.mg-pill.OPEN{background:var(--yellow-t);border-color:color-mix(in srgb,var(--yellow-d) 40%,transparent)}
   .mg-pill.out,.mg-pill.VOIDED{background:var(--red);color:var(--on-red);border-color:transparent}.mg-pill.CANCELLED{background:var(--soft);color:var(--muted)}
   .mg-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.mg-row.end{justify-content:flex-end;margin-top:12px}.mg-row .sp{flex:1}
   .mg-row button,.mg-tbl button{padding:4px 11px;font-size:12.5px}
   .mg-chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}.mg-chips button{padding:5px 15px;border-radius:99px}
   .mg-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;align-items:end;margin-top:10px}
   .mg-fld{display:flex;flex-direction:column;gap:3px;font-size:12.5px;font-weight:600;color:var(--muted);text-align:left}
   .mg-order{border-bottom:1px solid var(--line);padding:9px 0}.mg-order summary{cursor:pointer;display:flex;gap:10px;align-items:center;flex-wrap:wrap;list-style:none}
   .mg-order summary::-webkit-details-marker{display:none}.mg-order .tot{margin-left:auto;font-weight:700;font-variant-numeric:tabular-nums}
   .mg-order .ln{display:flex;justify-content:space-between;padding:3px 0 3px 12px;font-size:13.5px}.mg-order .why{font-size:12.5px;color:var(--red);padding-left:12px}
   .mg-pl{width:100%;font-variant-numeric:tabular-nums}.mg-pl div{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--line)}
   .mg-pl .sum{font-weight:700;border-bottom:1.5px solid var(--ink)}.mg-pl .net{font:600 22px var(--f-display);border:0;padding-top:10px}.mg-pl .neg{color:var(--red)}.mg-pl .pos{color:var(--ok)}
   .mg-modal{position:fixed;inset:0;background:rgba(15,12,9,.6);display:flex;align-items:center;justify-content:center;padding:16px;z-index:40}
   .mg-dlg{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;width:min(100%,420px);display:flex;flex-direction:column;gap:10px;max-height:90vh;overflow:auto}.mg-dlg h3{margin:0 0 2px;font:600 20px var(--f-display)}
   .mg-dlg p{margin:0}
   #mgLock{position:absolute;inset:0;background:var(--bg);display:none;align-items:center;justify-content:center;z-index:8;padding:16px}
   #mgLock.show{display:flex}#mgLock .box{background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:28px;width:min(100%,360px);text-align:center;display:flex;flex-direction:column;gap:12px}
   #mgLock .box h2{margin:0;font:600 24px var(--f-display);justify-content:center}#mgLock input{text-align:center;font:500 26px var(--f-mono);letter-spacing:.4em;padding:10px;border-radius:10px;border:1px solid var(--line-2);background:var(--card);color:var(--ink)}#mgLock .err{color:var(--red);font-weight:700;min-height:1.2em}
   #mgToast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--board);color:var(--on-board);padding:10px 18px;border-radius:12px;font-weight:600;display:none;z-index:50;max-width:min(92vw,560px);box-shadow:0 10px 30px rgba(0,0,0,.35)}
   #mgToast.show{display:block}#mgToast.bad{background:var(--red);color:var(--on-red)}
   .mg-note{background:var(--yellow-t);border:1px solid color-mix(in srgb,var(--yellow-d) 40%,transparent);border-radius:10px;padding:8px 12px;font-size:13.5px;margin-bottom:10px}
   @media (max-width:700px){#mgPanel,.mg-tabs{padding-left:12px;padding-right:12px}}`;

  /* ------------------------------------------------------------ shell + login */
  function mount() {
    document.head.append(h('style', {}, CSS));
    let sw = $('fpSwitch');
    if (!sw) { sw = h('div', {class: 'fp-switch', id: 'fpSwitch'}); const top = document.querySelector('.top'); top.insertBefore(sw, top.querySelector('.steps') || top.lastChild); }
    sw.append(h('button', {id: 'mgBtn', 'data-k': 'manager', onclick: enter}, 'Manager'));
    sw.querySelectorAll('button:not(#mgBtn)').forEach(b => b.addEventListener('click', leave, true));   // Kiosk/Floor buttons leave manager mode

    const pin = h('input', {class: 'mg-in', id: 'mgPin', type: 'password', inputmode: 'numeric', maxlength: 12, autocomplete: 'off', 'aria-label': 'Manager PIN', placeholder: '••••'});
    const go = async () => {
      const r = await fetch(API + '/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pin: pin.value})}).then(x => x.json()).catch(() => ({error: 'Server unreachable'}));
      if (r.ok) { token = r.token; defaultPin = r.default_pin; try { sessionStorage.setItem('bistroMgr', token); } catch (e) {} pin.value = ''; $('mgErr').textContent = ''; $('mgLock').classList.remove('show'); showTab(defaultPin ? 'settings' : 'overview'); if (defaultPin) toast('Still on the default PIN 1234 - please change it below.', true); }
      else { $('mgErr').textContent = r.error || 'Wrong PIN'; pin.select(); }
    };
    pin.addEventListener('keydown', e => e.key === 'Enter' && go());
    const view = h('section', {id: 'mgrView', 'aria-label': 'Manager mode', style: 'position:relative'},
      h('nav', {class: 'mg-tabs', id: 'mgTabs', role: 'tablist'}, TABS.map(([k, t]) => h('button', {'data-t': k, role: 'tab', onclick: () => showTab(k)}, t))),
      h('div', {id: 'mgPanel'}),
      h('div', {id: 'mgLock'}, h('div', {class: 'box'}, h('h2', {}, 'Manager mode'), h('p', {class: 'mg-muted'}, 'Enter the manager PIN to see sales, stock and costs.'),
        pin, h('div', {class: 'err', id: 'mgErr', role: 'alert'}), h('button', {class: 'cta', onclick: go}, 'Unlock'),
        h('button', {onclick: () => { leave(); document.querySelector('#fpSwitch button').click(); }}, 'Back to kiosk'))));
    document.body.append(view, h('div', {id: 'mgToast', role: 'status'}), h('div', {id: 'mgTip'}));
    window.addEventListener('hashchange', () => { if (location.hash === '#manager') enter(); });
    setInterval(poll, 5000);
    if (location.hash === '#manager') enter();
  }
  function enter() {
    document.body.classList.remove('fp-on'); document.body.classList.add('mg-on');
    document.querySelectorAll('#fpSwitch button').forEach(b => b.classList.toggle('on', b.id === 'mgBtn'));
    try { history.replaceState(null, '', '#manager'); } catch (e) {}
    if (!token) { lock(false); return; }
    $('mgLock').classList.remove('show'); showTab(tab);
  }
  function leave() {
    if (!document.body.classList.contains('mg-on')) return;
    if (window.BistroFloor) BistroFloor.release();
    document.body.classList.remove('mg-on');
  }
  function lock(expired) {
    if (token && !expired) api('/logout', 'POST').catch(() => {});
    token = ''; try { sessionStorage.removeItem('bistroMgr'); } catch (e) {}
    if (window.BistroFloor) BistroFloor.release();
    $('mgLock').classList.add('show'); $('mgErr').textContent = expired ? 'Session expired - enter the PIN again.' : '';
    setTimeout(() => $('mgPin').focus(), 50);
  }
  function showTab(t) {
    if (window.BistroFloor) BistroFloor.release();           // never delete the floor view when the panel is rebuilt
    tab = t; stamp.clear();
    document.querySelectorAll('#mgTabs button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
    const panel = $('mgPanel'); panel.replaceChildren(h('div', {class: 'mg-empty'}, 'Loading...'));
    if (t === 'floor') {
      const host = h('div', {}); panel.replaceChildren(host);
      if (window.BistroFloor) BistroFloor.embed(host); else host.append(h('div', {class: 'mg-empty'}, 'floor_plan.js is not loaded.'));
      return;
    }
    refreshTab();
  }
  function poll() {
    if (!document.body.classList.contains('mg-on') || !token || document.hidden || tab === 'floor') return;
    const a = document.activeElement;
    if (a && $('mgPanel').contains(a) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(a.tagName)) return;     // never clobber typing
    if (!document.querySelector('.mg-modal')) refreshTab();
  }
  async function refreshTab() {
    if (loading) return; loading = true;
    try { await ({overview: viewOverview, inventory: viewInventory, sales: viewSales, orders: viewOrders, team: viewTeam, settings: viewSettings}[tab] || (() => 0))(); }
    catch (e) { if (e.message !== 'locked') console.error(e); }
    finally { loading = false; }
  }
  const panelSet = (key, nodes) => { const p = $('mgPanel'); put(p, key, () => { const sc = p.scrollTop; p.replaceChildren(...nodes()); p.scrollTop = sc; }); };
  const kpi = (label, value, sub, cls) => h('div', {class: 'mg-kpi ' + (cls || '')}, h('small', {}, label), h('b', {}, value), h('span', {}, sub));
  const card = (title, body, ...head) => h('section', {class: 'mg-card'}, h('h2', {}, title, h('span', {class: 'sp'}), ...head), h('div', {class: 'mg-body'}, body));
  const chips = (opts, cur, on) => h('div', {class: 'mg-chips'}, opts.map(([k, t]) => h('button', {class: k === cur ? 'active' : '', onclick: () => on(k)}, t)));

  /* ------------------------------------------------------------ overview */
  async function viewOverview() {
    const d = await api('/overview'); if (!d.ok) return;
    panelSet(d, () => {
      const t = d.today, f = d.floor, net = t.net_profit;
      const alertIcon = {bad: '●', warn: '●', info: '●'};
      const hrs = d.hours.map(x => x.orders).map((n, i) => n ? i : -1).filter(i => i >= 0);
      const lo = Math.min(8, ...(hrs.length ? hrs : [8])), hi = Math.max(20, ...(hrs.length ? hrs : [20]));
      return [h('div', {class: 'mg-grid mg-kpis'},
        kpi("Today's sales", php0(t.billed), `${t.orders} order(s) · avg ${php0(t.avg_ticket)}`, 'plain'),
        kpi('Gross profit', php0(t.gross_profit), `food ${php0(t.food_sales)} − cost ${php0(t.cogs)}` + (t.cost_coverage_pct < 100 ? ` (${t.cost_coverage_pct}% costed)` : ''), t.gross_profit >= 0 ? 'good' : 'bad'),
        kpi('Net today (est.)', php0(net), `after ${php0(t.labor)} wages & ${php0(t.expenses)} expenses`, net >= 0 ? 'good' : 'bad'),
        kpi('Occupancy', f.occupancy_pct + '%', `${f.occupied}/${f.usable} tables · ${f.free} free · ${f.reserved} reserved`, 'plain'),
        kpi('Kitchen queue', f.kitchen_queue, `${f.open_orders} guest(s) still ordering`, 'plain'),
        kpi('Stock', d.stock.out + ' out', `${d.stock.low} low of ${d.stock.items} items`, d.stock.out ? 'bad' : 'plain'),
        kpi('Team on shift', d.staff.on, `of ${d.staff.total} active`, 'plain'),
        kpi('Cancelled / voided', d.cancelled.orders + ' / ' + d.voided.orders, `${php0(d.cancelled.value)} abandoned · ${php0(d.voided.value)} voided`, d.voided.orders ? 'bad' : 'plain')),
      h('div', {class: 'mg-grid mg-2'},
        card('Needs attention', d.alerts.length ? d.alerts.map(a => h('div', {class: 'mg-alert ' + a.level, onclick: () => showTab(a.tab)}, h('span', {class: 'dot'}, alertIcon[a.level]), h('b', {style: 'font-weight:600'}, a.text))) : h('div', {class: 'mg-empty'}, 'Nothing needs attention right now.')),
        card('Top sellers today', d.top_sellers.length ? hbars(d.top_sellers, {name: x => x.name, value: x => x.qty, fmtVal: n => n + ' sold'}) : h('div', {class: 'mg-empty'}, 'No confirmed orders yet today.'))),
      h('div', {class: 'mg-grid mg-2'},
        card('Sales - last 7 days', bars(d.days, {label: (x, s) => dayLabel(x.date), value: x => x.billed, title: 'Sales per day, last 7 days'})),
        card("Today's sales by hour", bars(d.hours.slice(lo, hi + 1), {label: (x) => hourLabel(x.hour), value: x => x.billed, title: 'Sales per hour today'})))];
    });
  }

  /* ------------------------------------------------------------ inventory */
  async function stockDialog(it, mode) {
    const titles = {add: `Restock ${it.name}`, waste: `Write off ${it.name}`, set: `Count ${it.name}`};
    const v = await ask({title: titles[mode], ok: 'Save', note: `Currently ${it.stock} in stock.`, fields: [
      {name: 'qty', label: mode === 'set' ? 'Actual count' : 'Quantity', type: 'number', min: 0, value: mode === 'set' ? it.stock : '', step: 1},
      {name: 'note', label: 'Note (optional)', placeholder: mode === 'waste' ? 'spoiled, dropped...' : 'delivery, recount...'}]});
    if (!v) return;
    const r = await api('/stock', 'POST', {item_id: it.id, mode, qty: v.qty, note: v.note}); if (r.ok) { toast('Saved'); refreshTab(); }
  }
  async function saveItem(it, field, value) {
    const r = await api('/item', 'POST', {item_id: it.id, [field]: value}); if (r.ok) toast('Saved'); refreshTab();
  }
  async function viewInventory() {
    const d = await api('/inventory'); if (!d.ok) return;
    panelSet([d, invFilter, invCat, invQuery], () => {
      const s = d.summary, q = invQuery.trim().toLowerCase();
      const rows = d.items.filter(i => (invFilter === 'ALL' || (invFilter === 'LOW' ? i.status !== 'ok' : i.status === 'out')) && (invCat === 'All' || i.category === invCat) && (!q || i.name.toLowerCase().includes(q)));
      const num = (it, field, val, w) => h('input', {class: 'mg-in', type: 'number', min: 0, step: field === 'price' || field === 'unit_cost' ? '0.01' : '1', value: val ?? '', placeholder: '—', 'aria-label': `${it.name} ${field}`,
        style: w ? `width:${w}px` : null, onchange: e => saveItem(it, field, e.target.value)});
      const search = h('input', {class: 'mg-in', placeholder: 'Search items…', value: invQuery, style: 'max-width:200px', 'aria-label': 'Search items',
        oninput: e => { invQuery = e.target.value; const pos = e.target.selectionStart; stamp.clear(); viewInventory().then(() => { const n = $('mgPanel').querySelector('input[placeholder^="Search"]'); if (n) { n.focus(); n.setSelectionRange(pos, pos); } }); }});
      const reorder = d.items.filter(i => i.suggest);
      return [h('div', {class: 'mg-grid mg-kpis'},
        kpi('Items', d.items.length, `${d.categories.length} categories`, 'plain'),
        kpi('Out of stock', d.items.filter(i => i.status === 'out').length, 'cannot be ordered at the kiosk', d.items.some(i => i.status === 'out') ? 'bad' : 'plain'),
        kpi('Low stock', d.items.filter(i => i.status === 'low').length, 'at or below reorder level', 'plain'),
        kpi('Stock value (cost)', php0(s.stock_cost_value), `retail ${php0(s.stock_retail_value)}` + (s.uncosted ? ` · ${s.uncosted} item(s) w/o cost` : ''), 'plain'),
        kpi('Suggested reorder', php0(s.reorder_cost), `${reorder.length} item(s) to top up to par`, 'plain')),
      s.uncosted ? h('div', {class: 'mg-note'}, `${s.uncosted} item(s) have no unit cost, so profit figures are incomplete. `,
        h('button', {onclick: async () => { const v = await ask({title: 'Estimate missing costs', ok: 'Apply', note: 'Sets unit cost = price × this percent for items with no cost. You can edit each one afterwards.', fields: [{name: 'pct', label: 'Food cost % of price', type: 'number', value: 35, min: 5, max: 95}]}); if (v) { const r = await api('/items/estimate-costs', 'POST', {pct: v.pct}); if (r.ok) { toast(`Estimated ${r.updated} cost(s)`); refreshTab(); } } }}, 'Estimate costs…')) : null,
      h('div', {class: 'mg-grid mg-2', style: 'grid-template-columns:minmax(0,1fr);'},
        card('Stock & prices', [h('div', {class: 'mg-row', style: 'margin-bottom:10px'},
          chips([['ALL', 'All'], ['LOW', 'Low / out'], ['OUT', 'Out']], invFilter, k => { invFilter = k; refreshTab(); }),
          h('select', {class: 'mg-in', style: 'width:auto', 'aria-label': 'Category', onchange: e => { invCat = e.target.value; refreshTab(); }}, ['All', ...d.categories].map(c => h('option', {value: c, selected: c === invCat}, c))),
          search, h('span', {class: 'sp'}), h('button', {class: 'cta', onclick: addItem}, '＋ New item')),
        h('div', {class: 'mg-wrap'}, h('table', {class: 'mg-tbl'},
          h('thead', {}, h('tr', {}, ['Item', 'Stock', 'Sold 7d', 'Price', 'Unit cost', 'Margin', 'Reorder at', 'Actions'].map((t, i) => h('th', {class: i > 0 && i < 7 ? 'num' : ''}, t)))),
          h('tbody', {}, rows.length ? rows.map(it => h('tr', {class: it.status === 'out' ? 'out' : ''},
            h('td', {}, h('b', {}, it.name), h('div', {class: 'mg-muted'}, it.category)),
            h('td', {class: 'num'}, h('b', {style: 'font-size:17px'}, it.stock), ' ', h('span', {class: 'mg-pill ' + it.status}, it.status === 'ok' ? 'OK' : it.status === 'low' ? 'LOW' : 'OUT'),
              it.days_left != null && h('div', {class: 'mg-muted'}, `~${it.days_left} d left`)),
            h('td', {class: 'num'}, it.sold_7d, it.sold_today ? h('div', {class: 'mg-muted'}, it.sold_today + ' today') : null),
            h('td', {class: 'num'}, num(it, 'price', it.price)), h('td', {class: 'num'}, num(it, 'unit_cost', it.unit_cost)),
            h('td', {class: 'num'}, it.margin_pct != null ? it.margin_pct + '%' : '—'), h('td', {class: 'num'}, num(it, 'reorder_level', it.reorder_level, 64)),
            h('td', {}, h('div', {class: 'mg-row'}, h('button', {class: 'cta', onclick: () => stockDialog(it, 'add')}, '+ Restock'), h('button', {onclick: () => stockDialog(it, 'set')}, 'Count'),
              h('button', {onclick: () => stockDialog(it, 'waste'), disabled: !it.stock}, 'Waste'),
              h('button', {disabled: !it.stock, title: "Mark as sold out (86)", onclick: async () => { if (confirm(`86 ${it.name}? This sets its stock to 0.`)) { const r = await api('/stock', 'POST', {item_id: it.id, mode: 'eighty', note: '86'}); if (r.ok) { toast(it.name + ' is 86\'d'); refreshTab(); } } }}, '86'))))) : h('tr', {}, h('td', {colspan: 8, class: 'mg-empty'}, 'No items match.')))))],
        h('button', {onclick: () => download('/export/inventory.csv', 'inventory.csv')}, 'CSV'))),
      h('div', {class: 'mg-grid mg-2'},
        card('Suggested reorder list', reorder.length ? [h('div', {class: 'mg-wrap'}, h('table', {class: 'mg-tbl'}, h('thead', {}, h('tr', {}, ['Item', 'Stock', 'Order qty', 'Est. cost'].map((t, i) => h('th', {class: i ? 'num' : ''}, t)))),
          h('tbody', {}, reorder.map(i => h('tr', {}, h('td', {}, i.name), h('td', {class: 'num'}, i.stock), h('td', {class: 'num'}, h('b', {}, i.suggest)), h('td', {class: 'num'}, i.unit_cost != null ? php0(i.suggest * i.unit_cost) : '—')))))),
          h('div', {class: 'mg-muted', style: 'margin-top:8px'}, 'Order quantity tops each item up to its par level (default: 4 × reorder level).')] : h('div', {class: 'mg-empty'}, 'Nothing to reorder.'),
          h('button', {onclick: () => download('/export/reorder.csv', 'reorder-list.csv')}, 'CSV')),
        card('Stock history', d.log.length ? d.log.slice(0, 14).map(l => h('div', {class: 'mg-hb'}, h('span', {}, h('b', {}, l.name), ' · ', l.reason, l.note ? ` (${l.note})` : ''),
          h('b', {style: l.delta < 0 ? 'color:var(--red)' : 'color:var(--ok)'}, (l.delta > 0 ? '+' : '') + l.delta), h('small', {class: 'mg-muted', style: 'grid-column:1/-1'}, `${timeAgo(l.at.replace(' ', 'T') + 'Z')} · now ${l.stock_after}`)))
          : h('div', {class: 'mg-empty'}, 'No stock changes recorded yet.'), h('button', {onclick: () => download('/export/stock_log.csv', 'stock-history.csv')}, 'CSV')))];
    });
  }
  async function addItem() {
    const cats = await api('/inventory'); const v = await ask({title: 'New menu item', ok: 'Add item', fields: [
      {name: 'name', label: 'Name'}, {name: 'category', label: 'Category', value: (cats.categories || [])[0] || '', placeholder: 'Burgers, Drinks…'},
      {name: 'price', label: 'Price (PHP)', type: 'number', min: 0, step: '0.01'}, {name: 'unit_cost', label: 'Unit cost (optional)', type: 'number', min: 0, step: '0.01'},
      {name: 'stock', label: 'Opening stock', type: 'number', min: 0, value: 0}, {name: 'allergens', label: 'Allergens (comma separated)', placeholder: 'gluten, dairy'},
      {name: 'aliases', label: 'Nicknames customers say', placeholder: 'fries, french fries'}]});
    if (!v) return; const r = await api('/items', 'POST', v); if (r.ok) { toast('Item added - it appears on the kiosk menu now.'); refreshTab(); }
  }

  /* ------------------------------------------------------------ sales */
  async function viewSales() {
    const d = await api('/sales?range=' + salesRange); if (!d.ok) return;
    panelSet([d, itemSort], () => {
      const t = d.totals;
      const items = [...d.items].sort((a, b) => ((a[itemSort.k] ?? -1e9) - (b[itemSort.k] ?? -1e9)) * itemSort.dir || a.name.localeCompare(b.name));
      const th = (label, k, num) => h('th', {class: 'sort' + (num ? ' num' : ''), onclick: () => { itemSort = {k, dir: itemSort.k === k ? -itemSort.dir : -1}; stamp.clear(); viewSales(); }}, label + (itemSort.k === k ? (itemSort.dir < 0 ? ' ↓' : ' ↑') : ''));
      return [h('div', {class: 'mg-row', style: 'margin-bottom:10px'}, chips([['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['all', 'All time']], salesRange, k => { salesRange = k; refreshTab(); }),
        h('span', {class: 'sp'}), h('button', {onclick: () => download('/export/sales.csv', 'orders-and-lines.csv')}, 'Orders CSV')),
      h('div', {class: 'mg-grid mg-kpis'},
        kpi('Total billed', php0(t.billed), `incl. ${php0(t.service_fee)} service fee`, 'plain'), kpi('Orders', t.orders, `avg ticket ${php0(t.avg_ticket)}`, 'plain'),
        kpi('Gross profit', php0(t.gross_profit), `food ${php0(t.food_sales)} − cost ${php0(t.cogs)}`, t.gross_profit >= 0 ? 'good' : 'bad'),
        kpi('Gross margin', t.food_sales ? Math.round(100 * t.gross_profit / t.food_sales) + '%' : '—', t.cost_coverage_pct < 100 ? `only ${t.cost_coverage_pct}% of sales have a cost` : 'all items costed', 'plain'),
        kpi('Abandoned orders', d.cancelled.orders, php0(d.cancelled.value) + ' not billed', 'plain'), kpi('Voided orders', d.voided.orders, php0(d.voided.value) + ' refunded', d.voided.orders ? 'bad' : 'plain')),
      h('div', {class: 'mg-grid mg-2'},
        card('Sales per day', d.daily.length ? bars(d.daily, {label: (x) => dayLabel(x.date), value: x => x.billed, title: 'Sales per day'}) : h('div', {class: 'mg-empty'}, 'No sales in this period.')),
        card('Busiest hours', d.daily.length ? bars(d.hours.slice(7, 23), {label: x => hourLabel(x.hour), value: x => x.billed, title: 'Sales by hour of day'}) : h('div', {class: 'mg-empty'}, 'No sales in this period.'))),
      h('div', {class: 'mg-grid mg-2'},
        card('By category', d.categories.length ? hbars(d.categories, {name: c => `${c.name} · ${c.qty} sold`, value: c => c.sales}) : h('div', {class: 'mg-empty'}, '—')),
        card('By table', d.tables.length ? hbars(d.tables, {name: x => `Table ${x.table_no} · ${x.orders} order(s)`, value: x => x.billed}) : h('div', {class: 'mg-empty'}, '—'))),
      card('Item performance', d.items.length ? h('div', {class: 'mg-wrap'}, h('table', {class: 'mg-tbl'}, h('thead', {}, h('tr', {}, th('Item', 'name'), th('Qty', 'qty', 1), th('Sales', 'sales', 1), th('Cost', 'cost', 1), th('Profit', 'profit', 1))),
        h('tbody', {}, items.map(i => h('tr', {}, h('td', {}, i.name, h('div', {class: 'mg-muted'}, i.category)), h('td', {class: 'num'}, i.qty), h('td', {class: 'num'}, php0(i.sales)),
          h('td', {class: 'num'}, i.cost != null ? php0(i.cost) : '—'), h('td', {class: 'num'}, i.profit != null ? php0(i.profit) : '—')))))) : h('div', {class: 'mg-empty'}, 'No items sold in this period.'))];
    });
  }

  /* ------------------------------------------------------------ orders */
  async function viewOrders() {
    const d = await api('/orders?limit=80&status=' + (ordersFilter === 'VOIDED' ? 'CANCELLED' : ordersFilter)); if (!d.ok) return;
    const list = ordersFilter === 'VOIDED' ? d.orders.filter(o => o.status === 'VOIDED') : d.orders;
    const open = new Set([...document.querySelectorAll('.mg-order[open]')].map(e => e.dataset.id));
    panelSet([d, ordersFilter], () => [chips([['ALL', 'All'], ['OPEN', 'Open'], ['CONFIRMED', 'Confirmed'], ['CANCELLED', 'Cancelled'], ['VOIDED', 'Voided']], ordersFilter, k => { ordersFilter = k; refreshTab(); }),
      card('Orders', list.length ? list.map(o => h('details', {class: 'mg-order', 'data-id': o.session_id, open: open.has(String(o.session_id))},
        h('summary', {}, h('b', {}, '#' + o.session_id), h('span', {}, 'Table ' + o.table_no), h('span', {class: 'mg-pill ' + o.status}, o.status), h('span', {class: 'mg-muted'}, `${o.items} item(s) · ${timeAgo(o.created_at)}`), h('span', {class: 'tot'}, php(o.total))),
        o.lines.map(l => h('div', {class: 'ln'}, h('span', {}, `${l.qty} × ${l.item}` + (l.notes ? ` (${l.notes})` : '')), h('span', {}, php(l.total)))),
        o.void_reason != null && h('div', {class: 'why'}, 'Voided: ' + o.void_reason),
        o.status === 'CONFIRMED' && h('div', {class: 'mg-row end'}, h('button', {onclick: async e => { e.preventDefault(); const v = await ask({title: `Void order #${o.session_id}`, ok: 'Void order', note: 'Marks the order cancelled and puts its items back into stock.', fields: [{name: 'reason', label: 'Reason', placeholder: 'wrong order, customer refund…'}]});
          if (v) { const r = await api(`/orders/${o.session_id}/void`, 'POST', {reason: v.reason}); if (r.ok) { toast('Order voided and stock restored'); refreshTab(); } } }}, '↩ Void order')))) : h('div', {class: 'mg-empty'}, 'No orders here yet.'),
        h('button', {onclick: () => download('/export/orders.csv', 'orders.csv')}, 'CSV'))]);
  }

  /* ------------------------------------------------------------ team & costs */
  async function viewTeam() {
    const d = await api('/team?range=' + teamRange); if (!d.ok) return;
    panelSet(d, () => {
      const p = d.pnl, line = (label, val, cls) => h('div', {class: cls || ''}, h('span', {}, label), h('span', {}, val));
      const neg = n => n < 0 ? 'neg' : 'pos';
      return [h('div', {class: 'mg-row', style: 'margin-bottom:10px'}, chips([['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['all', 'All time']], teamRange, k => { teamRange = k; refreshTab(); })),
      h('div', {class: 'mg-grid mg-2'},
        card('Profit & loss (estimate)', [h('div', {class: 'mg-pl'}, line('Food & drink sales', php(p.food_sales)), line('− Cost of goods', php(p.cogs)), line('Gross profit', php(p.gross_profit), 'sum ' + neg(p.gross_profit)),
          line('− Wages', php(p.labor)), line('− Expenses', php(p.expenses)), line('Net profit', php(p.net_profit), 'net ' + neg(p.net_profit))),
          h('div', {class: 'mg-muted', style: 'margin-top:8px'}, `Service fee (${php0(p.service_fee)}) is excluded. Costs use each item's unit cost` + (p.cost_coverage_pct < 100 ? ` - only ${p.cost_coverage_pct}% of sales are costed, so profit is overstated.` : '.'))]),
        card('Expenses', [d.expense_categories.length ? hbars(d.expense_categories, {name: c => c.category, value: c => c.amount}) : null,
          h('div', {class: 'mg-form'}, ...(() => { const cat = h('input', {class: 'mg-in', placeholder: 'Rent, Utilities…', list: 'mgCats', maxlength: 30, 'aria-label': 'Category'}), amt = h('input', {class: 'mg-in', type: 'number', min: 0, step: '0.01', placeholder: '0.00', 'aria-label': 'Amount'}), note = h('input', {class: 'mg-in', placeholder: 'Note', maxlength: 80, 'aria-label': 'Note'});
            return [h('label', {class: 'mg-fld'}, 'Category', cat, h('datalist', {id: 'mgCats'}, ['Rent', 'Utilities', 'Supplies', 'Marketing', 'Maintenance', 'Other'].map(c => h('option', {value: c})))), h('label', {class: 'mg-fld'}, 'Amount', amt), h('label', {class: 'mg-fld'}, 'Note', note),
              h('button', {class: 'cta', onclick: async () => { const r = await api('/expenses', 'POST', {category: cat.value, amount: amt.value, note: note.value}); if (r.ok) { toast('Expense added'); refreshTab(); } }}, 'Add')]; })()),
          d.expenses.length ? h('div', {style: 'margin-top:10px'}, d.expenses.slice(0, 10).map(x => h('div', {class: 'mg-hb'}, h('span', {}, `${x.spent_on} · ${x.category}` + (x.note ? ` · ${x.note}` : '')),
            h('span', {}, h('b', {}, php(x.amount)), ' ', h('button', {'aria-label': 'Delete expense', title: 'Delete', style: 'padding:0 8px', onclick: async () => { if (confirm('Delete this expense?')) { await api('/expenses/' + x.exp_id, 'DELETE'); refreshTab(); } }}, '✕')))))
            : h('div', {class: 'mg-empty'}, 'No expenses in this period.')])),
      card('Team', [h('div', {class: 'mg-wrap'}, h('table', {class: 'mg-tbl'}, h('thead', {}, h('tr', {}, ['Name', 'Rate / h', 'Hours', 'Wages', 'Shift'].map((t, i) => h('th', {class: i && i < 4 ? 'num' : ''}, t)))),
        h('tbody', {}, d.staff.length ? d.staff.map(s => h('tr', {style: s.active ? '' : 'opacity:.5'}, h('td', {}, h('b', {}, s.name), h('div', {class: 'mg-muted'}, s.role + (s.active ? '' : ' · inactive'))),
          h('td', {class: 'num'}, php0(s.hourly_rate)), h('td', {class: 'num'}, s.hours), h('td', {class: 'num'}, php0(s.wages)),
          h('td', {}, h('div', {class: 'mg-row'}, s.on_shift ? [h('span', {class: 'mg-pill ok'}, 'On · ' + timeAgo(s.since).replace(' ago', '')), h('button', {onclick: async () => { await api(`/staff/${s.staff_id}/clock`, 'POST', {action: 'out'}); refreshTab(); }}, 'Clock out')]
            : s.active ? h('button', {class: 'cta', onclick: async () => { await api(`/staff/${s.staff_id}/clock`, 'POST', {action: 'in'}); refreshTab(); }}, 'Clock in') : null,
            h('button', {title: s.active ? 'Deactivate' : 'Reactivate', onclick: async () => { await api('/staff/' + s.staff_id, 'POST', {active: !s.active}); refreshTab(); }}, s.active ? '⏻' : '↺'))))) : h('tr', {}, h('td', {colspan: 5, class: 'mg-empty'}, 'No staff yet - add your first team member below.'))))),
        h('div', {class: 'mg-row', style: 'margin-top:10px'}, h('button', {onclick: async () => { const v = await ask({title: 'Add team member', ok: 'Add', fields: [{name: 'name', label: 'Name'}, {name: 'role', label: 'Role', value: 'Server', options: ['Manager', 'Server', 'Cook', 'Cashier', 'Runner', 'Dishwasher']}, {name: 'hourly_rate', label: 'Hourly rate (PHP)', type: 'number', min: 0, step: '0.01', value: 70}]}); if (v) { const r = await api('/staff', 'POST', v); if (r.ok) refreshTab(); } }}, '＋ Add staff')),
        h('div', {class: 'mg-muted', style: 'margin-top:6px'}, 'Hours and wages cover the selected period; clocked-in staff accrue until they clock out.')])];
    });
  }

  /* ------------------------------------------------------------ settings */
  async function viewSettings() {
    panelSet(['settings', defaultPin], () => [h('div', {class: 'mg-grid mg-2'},
      card('Manager PIN', [defaultPin ? h('div', {class: 'mg-note'}, 'You are still using the default PIN (1234). Please change it.') : null,
        h('div', {class: 'mg-form'}, ...(() => { const o = h('input', {class: 'mg-in', type: 'password', inputmode: 'numeric', 'aria-label': 'Current PIN', autocomplete: 'off'}), n = h('input', {class: 'mg-in', type: 'password', inputmode: 'numeric', maxlength: 12, 'aria-label': 'New PIN', autocomplete: 'off'});
          return [h('label', {class: 'mg-fld'}, 'Current PIN', o), h('label', {class: 'mg-fld'}, 'New PIN (4-12 digits)', n), h('button', {class: 'cta', onclick: async () => { const r = await api('/pin', 'POST', {old: o.value, new: n.value}); if (r.ok) { defaultPin = false; toast('PIN changed'); o.value = n.value = ''; stamp.clear(); viewSettings(); } }}, 'Change PIN')]; })()),
        h('div', {class: 'mg-row', style: 'margin-top:12px'}, h('button', {onclick: () => lock(false)}, 'Lock manager mode now'))]),
      card('About this data', h('div', {class: 'mg-muted', style: 'display:grid;gap:8px'},
        h('div', {}, 'Sales count orders confirmed at the kiosk. Prices are the current menu prices, so changing a price also changes how past sales are valued.'),
        h('div', {}, 'Stock drops when an order is confirmed and rises when you restock or void. Every change is logged under Inventory > Stock history.'),
        h('div', {}, 'Cost, profit and wage numbers are estimates from the unit costs, hourly rates and expenses you enter.'),
        h('div', {}, 'Heads-up: bumping PRAGMA user_version in init_restaurant_db.sql rebuilds the database and resets the menu, stock, manager edits and history.'))))]);
  }

  window.BistroManager = {enter, lock: () => lock(false)};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
