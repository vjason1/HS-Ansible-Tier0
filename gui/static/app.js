/* Tier 0 Configuration GUI — single-page app, no dependencies.
 * All content from files, logs and the API is inserted with textContent
 * (never innerHTML) so nothing in a config file can execute in the page. */
'use strict';

// ------------------------------------------------------------------ state
const S = {
  token: '',
  meta: null,
  forms: {},        // sectionId -> { data, edits: {key: {action, value}} }
  issues: [],
  jobs: [],
  vaultPw: null,    // held in memory only, never stored
  pollers: [],
  inv: null,
};

// ------------------------------------------------------------------ helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (sel, root) => (root || document).querySelector(sel);
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
const jeq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

async function api(path, opts) {
  opts = opts || {};
  const headers = { 'X-Auth-Token': S.token };
  let body;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    headers['X-Requested-With'] = 'tier0-gui';
    body = JSON.stringify(opts.body);
  } else if (opts.method === 'POST') {
    headers['X-Requested-With'] = 'tier0-gui';
  }
  const res = await fetch(path, { method: opts.method || (body ? 'POST' : 'GET'), headers, body });
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const err = new Error(data.error || ('HTTP ' + res.status));
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

function toast(msg, isErr) {
  const t = h('div', { class: isErr ? 'err' : '', role: isErr ? 'alert' : 'status', text: msg });
  $('#toast').append(t);
  setTimeout(() => t.remove(), isErr ? 9000 : 4000);
}

function modal(title, bodyNodes, buttons) {
  const dlg = $('#modal');
  clear(dlg);
  return new Promise((resolve) => {
    const foot = h('footer');
    for (const b of buttons) {
      foot.append(h('button', {
        type: 'button', class: 'btn ' + (b.cls || ''), text: b.label,
        onclick: async () => {
          if (b.onClick) { const ok = await b.onClick(); if (ok === false) return; }
          dlg.close(); resolve(b.value);
        },
      }));
    }
    dlg.append(h('header', {}, h('h2', { text: title })), h('div', { class: 'body' }, bodyNodes), foot);
    dlg.addEventListener('cancel', () => resolve(undefined), { once: true });
    dlg.showModal();
  });
}

function diffView(lines) {
  if (!lines || !lines.length) return h('p', { class: 'muted', text: 'No textual change.' });
  const pre = h('pre', { class: 'diff' });
  for (const l of lines) {
    let cls = '';
    if (l.startsWith('+++') || l.startsWith('---') || l.startsWith('@@')) cls = 'hdr';
    else if (l.startsWith('+')) cls = 'add';
    else if (l.startsWith('-')) cls = 'del';
    pre.append(h('span', { class: cls, text: l || ' ' }));
  }
  return pre;
}

function fmtTime(t) {
  if (!t) return '';
  const d = new Date(t * 1000);
  return d.toLocaleString();
}

function stopPollers() { S.pollers.forEach(clearInterval); S.pollers = []; }

// ------------------------------------------------------------------ boot
async function boot() {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    sessionStorage.setItem('tier0-token', decodeURIComponent(m[1]));
    history.replaceState(null, '', location.pathname + '#/overview');
  }
  S.token = sessionStorage.getItem('tier0-token') || '';
  $('#menu-toggle').onclick = () => $('#rack').classList.toggle('open');
  tickClock(); setInterval(tickClock, 15000);
  try {
    S.meta = await api('/api/meta');
  } catch (e) {
    clear($('#view')).append(
      h('div', { class: 'page' },
        h('div', { class: 'page-head' }, h('h1', { text: 'Sign-in link needed' })),
        h('div', { class: 'notice red' },
          h('p', { text: e.status === 401
            ? 'This page needs the access token from the URL printed by gui/tier0_gui.py. Open that full URL (it ends in #token=…).'
            : 'Cannot reach the GUI server: ' + e.message }))));
    return;
  }
  $('#repo-path').textContent = S.meta.repo;
  $('#foot-info').textContent = 'GUI v' + S.meta.version + (S.meta.read_only ? ' (read-only)' : '');
  const repoName = S.meta.repo.split('/').filter(Boolean).pop();
  $('#repo-name').textContent = repoName + (S.meta.git && S.meta.git.branch ? ' @ ' + S.meta.git.branch : '');
  $('#top-repo').title = S.meta.repo;
  setupSavebar();
  window.addEventListener('hashchange', route);
  window.addEventListener('beforeunload', (ev) => {
    if (Object.values(S.forms).some((f) => Object.keys(f.edits).length)) { ev.preventDefault(); ev.returnValue = ''; }
  });
  refreshIssues();
  route();
  setInterval(refreshJobsLed, 4000);
}

// Same format as the Hammerspace UI header: "Wednesday, 14 Oct, 7:50 PM EDT"
function tickClock() {
  const d = new Date();
  const day = d.toLocaleDateString('en-US', { weekday: 'long' });
  const mon = d.toLocaleDateString('en-US', { month: 'short' });
  const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
  $('#top-clock').textContent = day + ', ' + d.getDate() + ' ' + mon + ', ' + time;
}

async function refreshIssues() {
  try { S.issues = (await api('/api/validate')).issues; } catch (e) { S.issues = []; }
  renderNav();
}
async function refreshJobsLed() {
  try { S.jobs = (await api('/api/jobs')).jobs; } catch (e) { return; }
  renderNav();
}

// ------------------------------------------------------------------ nav
function currentId() {
  const p = location.hash.replace(/^#\/?/, '').split('/');
  return p[0] || 'overview';
}

function renderNav() {
  const nav = clear($('#nav'));
  const cur = currentId();
  for (const g of S.meta.menus) {
    const grp = h('div', { class: 'nav-group' }, h('h3', { text: g.title }));
    for (const it of g.items) {
      let led = '';
      const f = S.forms[it.id];
      if (S.issues.some((i) => i.menu === it.id && i.level === 'error')) led = 'error';
      if (f && Object.keys(f.edits).length) led = 'dirty';
      if (it.id === 'inventory' && S.inv && S.inv.dirty) led = 'dirty';
      if (it.id === 'jobs' && S.jobs.some((j) => j.status === 'running')) led = 'busy';
      const label = led === 'dirty' ? ' (unsaved changes)' : led === 'error' ? ' (has errors)' : '';
      grp.append(h('a', {
        class: 'nav-item', href: '#/' + it.id, 'aria-current': it.id === cur ? 'page' : null,
        onclick: () => $('#rack').classList.remove('open'),
      }, h('span', { class: 'led ' + led, 'aria-hidden': 'true' }), it.title,
      label ? h('span', { class: 'sr-only', style: 'position:absolute;left:-9999px', text: label }) : null));
    }
    nav.append(grp);
  }
}

function menuItem(id) {
  for (const g of S.meta.menus) for (const it of g.items) if (it.id === id) return it;
  return null;
}

async function route() {
  stopPollers();
  const parts = location.hash.replace(/^#\/?/, '').split('/').map((x) => { try { return decodeURIComponent(x); } catch (e) { return x; } });
  const id = parts[0] || 'overview';
  renderNav();
  updateSavebar();
  const view = clear($('#view'));
  const item = menuItem(id);
  try {
    if (!item) { view.append(h('p', { text: 'Unknown page.' })); return; }
    const kind = item.kind;
    if (kind === 'overview') await pageOverview(view);
    else if (kind === 'form') await pageForm(view, id);
    else if (kind === 'vault') await pageVault(view);
    else if (kind === 'inventory') await pageInventory(view);
    else if (kind === 'raw') await pageRaw(view, parts.slice(1).join('/'));
    else if (kind === 'run') await pageRun(view, parts[1]);
    else if (kind === 'utilities') await pageUtilities(view);
    else if (kind === 'upgrade') await pageUpgrade(view, parts[1], parts[2]);
    else if (kind === 'jobs') await pageJobs(view, parts[1]);
    else if (kind === 'docs') await pageDocs(view, parts.slice(1).join('/'));
  } catch (e) {
    view.append(h('div', { class: 'notice red' }, h('p', { text: e.message })));
  }
  $('#main').focus({ preventScroll: true });
}

function pageHead(title, lede, file) {
  return h('div', { class: 'page-head' },
    h('h1', { text: title }),
    lede ? h('p', { class: 'lede', text: lede }) : null,
    file ? h('div', { class: 'file' }, 'Edits ', h('code', { text: file })) : null);
}

// ------------------------------------------------------------------ overview
async function pageOverview(view) {
  const o = await api('/api/overview');
  S.issues = o.issues; renderNav();
  const page = h('div', { class: 'page' });
  page.append(pageHead('Overview',
    'Configure the Tier 0 Ansible repository menu by menu. Each menu edits one file and shows you the exact change before it is written; a backup of every file is kept in .gui-backups/.'));
  const counts = o.static_inventory_counts || {};
  page.append(h('div', { class: 'facts cols3' },
    fact(o.git.branch ? o.git.branch + (o.git.dirty ? ' *' : '') : '—', 'Git branch' + (o.git.commit ? ' @ ' + o.git.commit : '')),
    fact(o.ansible ? o.ansible.replace(/^ansible(-playbook)? \[?core ?/, '').replace(/\]$/, '') : 'not installed', 'Ansible core'),
    fact(o.ansible_cfg_inventory || '—', 'Default inventory (ansible.cfg)'),
    fact(String(counts.storage_servers || 0), 'Storage servers in inventory.yml'),
    fact(String(counts.di_nodes || 0), 'DI nodes in inventory.yml'),
    fact(o.vault, 'vars/vault.yml'),
  ));
  page.append(h('h2', { text: 'Configuration check' }));
  if (!o.issues.length) page.append(h('div', { class: 'notice green' }, h('p', { text: 'No problems found in the current configuration.' })));
  else {
    const ul = h('ul', { class: 'issues' });
    const order = { error: 0, warning: 1, info: 2 };
    o.issues.sort((a, b) => order[a.level] - order[b.level]).forEach((i) => ul.append(h('li', {},
      h('span', { class: 'lvl ' + i.level, text: i.level === 'error' ? 'Error' : i.level === 'warning' ? 'Warning' : 'Note' }),
      h('span', { class: 'msg' }, h('b', { text: i.area + ': ' }), i.message),
      i.menu ? h('a', { href: '#/' + i.menu, text: 'Fix' }) : null)));
    page.append(ul);
  }
  page.append(h('h2', { text: 'Common tasks' }));
  page.append(h('div', { class: 'btn-row' },
    h('a', { class: 'btn', href: '#/run/precheck', text: 'Dry-run discovery and pre-checks' }),
    h('a', { class: 'btn', href: '#/upgrade', text: 'Check an OS upgrade' }),
    h('a', { class: 'btn', href: '#/inventory', text: 'Edit hosts' }),
    h('a', { class: 'btn', href: '#/docs', text: 'Read the docs' })));
  if (o.jobs.length) {
    page.append(h('h2', { text: 'Recent jobs' }), jobsTable(o.jobs));
  }
  view.append(page);
}
function fact(value, label) { return h('div', {}, h('b', { text: value }), h('span', { text: label })); }

// ------------------------------------------------------------------ schema forms
function fieldVisible(field, sec, getVal) {
  const cond = field.show_if || {};
  for (const [k, want] of Object.entries(cond)) {
    const v = getVal(k);
    const wants = Array.isArray(want) ? want : [want];
    if (!wants.some((w) => w === v || String(w) === String(v))) return false;
  }
  return true;
}

function formState(id) {
  return S.forms[id];
}

function effectiveValue(id, key) {
  const st = formState(id);
  if (!st) return undefined;
  const e = st.edits[key];
  if (e) return e.action === 'unset' ? undefined : e.value;
  const v = st.data.values[key];
  if (v && v.state === 'set') return v.value;
  const f = st.data.section.fields.find((x) => x.key === key);
  return f ? f.default : undefined;
}

// Look a key up in ANY loaded vars/main.yml section (for cross-menu show_if)
function crossValue(key) {
  for (const [id, st] of Object.entries(S.forms)) {
    if (st.data.file !== 'vars/main.yml') continue;
    if (st.data.section.fields.some((f) => f.key === key)) return effectiveValue(id, key);
  }
  return undefined;
}

async function loadSection(id, force) {
  if (!S.forms[id] || force) {
    const data = await api('/api/section/' + id);
    S.forms[id] = { data, edits: (S.forms[id] && !force) ? S.forms[id].edits : {} };
  }
  return S.forms[id];
}

async function ensureCrossSections(sec) {
  // show_if may reference keys from other menus of vars/main.yml
  const needed = new Set();
  for (const f of sec.fields) for (const k of Object.keys(f.show_if || {})) if (!sec.fields.some((x) => x.key === k)) needed.add(k);
  for (const k of Object.keys(sec.show_if || {})) needed.add(k);
  for (const k of needed) {
    for (const s of Object.values(S.meta.sections)) {
      if (s.file === sec.file && s.fields.some((f) => f.key === k)) await loadSection(s.id);
    }
  }
}

async function pageForm(view, id) {
  const st = await loadSection(id);
  const sec = st.data.section;
  await ensureCrossSections(sec);
  const page = h('div', { class: 'page' });
  page.append(pageHead(sec.title, sec.description, sec.file));
  if (!st.data.exists) page.append(h('div', { class: 'notice amber' }, h('p', { text: sec.file + ' does not exist yet; saving creates it.' })));
  if (sec.show_if) {
    const getV = (k) => crossValue(k);
    if (!fieldVisible({ show_if: sec.show_if }, sec, getV)) {
      page.append(h('div', { class: 'notice' }, h('p', { text: 'These settings only apply when ' +
        Object.entries(sec.show_if).map(([k, v]) => k + ' = ' + v).join(', ') + '. They are shown so you can prepare them.' })));
    }
  }
  const fieldsBox = h('div', { class: 'fields' });
  page.append(fieldsBox);
  view.append(page);
  const rerender = () => {
    clear(fieldsBox);
    const getVal = (k) => sec.fields.some((f) => f.key === k) ? effectiveValue(id, k) : crossValue(k);
    let shown = 0;
    for (const f of sec.fields) {
      if (!fieldVisible(f, sec, getVal)) continue;
      fieldsBox.append(renderField(id, f, rerender));
      shown++;
    }
    if (!shown) fieldsBox.append(h('div', { class: 'empty', text: 'Nothing to configure with the current settings.' }));
    renderNav(); updateSavebar();
  };
  rerender();
}

function toText(field, value) {
  if (value === undefined || value === null) return '';
  if (field.type === 'list') return (Array.isArray(value) ? value : [value]).join('\n');
  if (field.type === 'intlist') return (Array.isArray(value) ? value : [value]).join(', ');
  return String(value);
}

function renderField(id, f, rerender) {
  const st = formState(id);
  const cur = st.data.values[f.key] || { state: 'absent' };
  const edit = st.edits[f.key];
  const isYaml = f.type === 'yaml';
  const origText = isYaml ? (cur.yaml_text || '') : null;
  const state = edit ? (edit.action === 'unset' ? 'unset' : 'set') : cur.state;
  const row = h('div', { class: 'field' + (edit ? ' modified' : '') });
  const inputId = 'f-' + f.key.replace(/[^\w-]/g, '_');
  const label = h('div', { class: 'field-label' },
    h('label', { for: inputId, text: f.label }), h('span', { class: 'key', text: f.key }));
  const control = h('div', { class: 'field-control' });
  row.append(label, control);

  const setEdit = (value) => {
    // Compare with what is on disk; identical == no edit
    let same;
    if (isYaml) same = cur.state === 'set' && value === origText;
    else same = cur.state === 'set' && jeq(normalize(f, value), cur.value);
    if (same) delete st.edits[f.key];
    else st.edits[f.key] = { action: 'set', value };
    row.classList.toggle('modified', !!st.edits[f.key]);
    renderNav(); updateSavebar();
  };

  if (state === 'set' || (state === 'absent' && !f.optional)) {
    const value = edit && edit.action === 'set' ? edit.value
      : (cur.state === 'set' ? (isYaml ? origText : cur.value) : (isYaml ? '' : f.default));
    control.append(inputFor(f, inputId, value, (v) => { setEdit(v); if (affectsVisibility(id, f.key)) rerender(); }));
  } else {
    control.append(h('div', { class: 'unset-box', text: 'Not set — the role default applies' +
      (f.default !== undefined && f.default !== null ? ' (' + toText(f, f.default) + ')' : '') + '.' }));
  }

  if (f.danger && (effectiveValue(id, f.key) === true || (Array.isArray(effectiveValue(id, f.key)) && effectiveValue(id, f.key).length))) {
    row.classList.add('danger-on');
    control.append(h('div', { class: 'field-danger', text: f.danger }));
  }
  if (f.help) control.append(h('div', { class: 'field-help', text: f.help }));

  const meta = h('div', { class: 'field-state' });
  if (cur.state === 'absent' && !edit) meta.append(h('span', { class: 'chip', text: f.optional ? 'not in file' : 'not in file — default shown' }));
  if (cur.state === 'unset' && !edit) meta.append(h('span', { class: 'chip', text: 'commented out in file' }));
  if (edit) meta.append(h('span', { class: 'chip amber', text: edit.action === 'unset' ? 'will be commented out' : (cur.state === 'set' ? 'changed' : 'will be added') }));
  if (f.default !== undefined && f.default !== null && f.type !== 'yaml' && state === 'set') {
    meta.append(h('span', { class: 'muted', text: 'Default: ' + (toText(f, f.default) || '""') }));
  }
  if (f.optional && state === 'set') {
    meta.append(h('button', { type: 'button', class: 'linkbtn', text: 'Unset (use default)', onclick: () => {
      if (cur.state === 'set') st.edits[f.key] = { action: 'unset' }; else delete st.edits[f.key];
      rerender();
    } }));
  }
  if (state === 'unset' || (f.optional && state !== 'set')) {
    meta.append(h('button', { type: 'button', class: 'linkbtn', text: 'Set a value', onclick: () => {
      const start = isYaml ? (f.example || '') : (f.default !== undefined ? f.default : (f.type === 'bool' ? false : (f.type === 'list' || f.type === 'intlist') ? [] : ''));
      st.edits[f.key] = { action: 'set', value: start };
      rerender();
    } }));
  }
  if (isYaml && f.example && state === 'set') {
    meta.append(h('button', { type: 'button', class: 'linkbtn', text: 'Insert example', onclick: () => {
      const ta = $('#' + inputId);
      if (ta) { ta.value = f.example; ta.dispatchEvent(new Event('input')); }
    } }));
  }
  if (edit) {
    meta.append(h('button', { type: 'button', class: 'linkbtn', text: 'Revert', onclick: () => { delete st.edits[f.key]; rerender(); } }));
  }
  if (meta.childNodes.length) control.append(meta);
  return row;
}

function affectsVisibility(id, key) {
  for (const s of Object.values(S.meta.sections)) {
    if (s.show_if && key in s.show_if) return true;
    for (const f of s.fields) if (f.show_if && key in f.show_if) return true;
  }
  return false;
}

function normalize(f, v) {
  if (f.type === 'list') return String(v).split('\n').map((x) => x.trim()).filter(Boolean);
  if (f.type === 'intlist') return String(v).split(/[,\s]+/).filter((x) => x !== '').map(Number);
  if (f.type === 'int') return v === '' ? '' : Number(v);
  if (f.type === 'float') return v === '' ? '' : Number(v);
  if (f.type === 'enum') { const o = f.options.find((x) => String(x) === String(v)); return o === undefined ? v : o; }
  return v;
}

function inputFor(f, id, value, onChange) {
  const t = f.type;
  if (t === 'bool') {
    const cb = h('input', { type: 'checkbox', id, checked: !!value, onchange: () => onChange(cb.checked) });
    return h('label', { class: 'switch' + (f.danger ? ' danger' : '') }, cb, h('span', { class: 'track' }),
      h('span', { class: 'muted small', text: cb.checked ? 'Enabled' : 'Disabled' }));
  }
  if (t === 'enum') {
    const sel = h('select', { id, onchange: () => onChange(normalize(f, sel.value)) });
    const opts = f.options.slice();
    if (value !== undefined && value !== null && !opts.some((o) => String(o) === String(value))) opts.push(value);
    for (const o of opts) sel.append(h('option', { value: String(o), text: o === '' ? '(empty)' : String(o) }));
    sel.value = value === undefined || value === null ? '' : String(value);
    return sel;
  }
  if (t === 'int' || t === 'float') {
    const inp = h('input', { type: 'number', id, value: value === undefined || value === null ? '' : String(value),
      step: f.step || (t === 'int' ? '1' : 'any'), min: f.min, max: f.max, placeholder: f.placeholder,
      oninput: () => onChange(normalize(f, inp.value)) });
    return inp;
  }
  if (t === 'list' || t === 'intlist') {
    const ta = h('textarea', { id, rows: Math.max(2, Math.min(8, (toText(f, value).split('\n').length) + 1)),
      placeholder: f.placeholder || (t === 'list' ? 'One entry per line' : 'Comma-separated numbers'),
      oninput: () => onChange(normalize(f, ta.value)) });
    ta.value = toText(f, value);
    return h('div', {}, ta, h('div', { class: 'field-default', text: t === 'list' ? 'One entry per line. Empty = [] .' : 'Comma-separated.' }));
  }
  if (t === 'yaml') {
    const ta = h('textarea', { id, class: 'yaml', rows: Math.max(4, Math.min(18, String(value || '').split('\n').length + 1)),
      spellcheck: 'false', placeholder: f.example || 'YAML', oninput: () => onChange(ta.value) });
    ta.value = value || '';
    return ta;
  }
  if (t === 'secret') {
    const inp = h('input', { type: 'password', id, autocomplete: 'off', value: value || '',
      oninput: () => onChange(inp.value) });
    const btn = h('button', { type: 'button', class: 'linkbtn', text: 'Show', onclick: () => {
      inp.type = inp.type === 'password' ? 'text' : 'password'; btn.textContent = inp.type === 'password' ? 'Show' : 'Hide';
    } });
    return h('div', { class: 'row' }, h('div', { class: 'grow' }, inp), btn);
  }
  const inp = h('input', { type: 'text', id, class: /[{\/]/.test(String(value || '')) ? 'mono' : '',
    value: value === undefined || value === null ? '' : String(value), placeholder: f.placeholder || '',
    oninput: () => onChange(inp.value), spellcheck: 'false' });
  return inp;
}

// ------------------------------------------------------------------ save bar
function setupSavebar() {
  $('#savebar-discard').onclick = () => {
    const id = currentId();
    if (S.forms[id]) S.forms[id].edits = {};
    if (id === 'inventory' && S.inv) { S.inv = null; }
    route();
  };
  $('#savebar-preview').onclick = () => saveCurrent(true);
  $('#savebar-save').onclick = () => saveCurrent(false);
}

function updateSavebar() {
  const id = currentId();
  let n = 0;
  if (S.forms[id]) n = Object.keys(S.forms[id].edits).length;
  if (id === 'inventory' && S.inv && S.inv.dirty) n = 1;
  const bar = $('#savebar');
  bar.classList.toggle('show', n > 0 && !S.meta.read_only);
  const file = id === 'inventory' ? (S.inv && S.inv.file) : (S.forms[id] && S.forms[id].data.file);
  if (!n) return;
  $('#savebar-count').textContent = id === 'inventory'
    ? 'Unsaved host inventory changes in ' + file
    : n + ' unsaved change' + (n === 1 ? '' : 's') + ' to ' + file;
  // other menus with pending edits
  const others = Object.entries(S.forms).filter(([k, f]) => k !== id && Object.keys(f.edits).length).map(([k]) => menuItem(k) ? menuItem(k).title : k);
  if (others.length && n) $('#savebar-count').textContent += ' · also unsaved: ' + others.join(', ');
}

function changesPayload(id) {
  const st = S.forms[id];
  const changes = {};
  for (const [k, e] of Object.entries(st.edits)) changes[k] = e.action === 'unset' ? { action: 'unset' } : { action: 'set', value: e.value };
  const body = { changes, base_hash: st.data.hash };
  if (st.data.file === 'vars/vault.yml' && S.vaultPw) body.vault_password = S.vaultPw;
  return body;
}

async function saveCurrent(previewOnly) {
  const id = currentId();
  if (id === 'inventory') return saveInventory(previewOnly);
  const st = S.forms[id];
  if (!st || !Object.keys(st.edits).length) return;
  try {
    const pv = await api('/api/section/' + id + '/preview', { body: changesPayload(id) });
    const choice = await modal('Review changes to ' + pv.file,
      [h('p', { class: 'muted', text: 'Only the lines below change. Everything else in the file, including comments, stays exactly as it is. The previous version is backed up to .gui-backups/.' }), diffView(pv.diff)],
      previewOnly ? [{ label: 'Close', value: 'close' }, { label: 'Save to file', cls: 'primary', value: 'save' }]
        : [{ label: 'Cancel', value: 'cancel' }, { label: 'Save to file', cls: 'primary', value: 'save' }]);
    if (choice !== 'save') return;
    const res = await api('/api/section/' + id + '/save', { body: changesPayload(id) });
    toast('Saved ' + res.file + (res.backup ? ' (backup: ' + res.backup + ')' : ''));
    // Reload every loaded menu of that file: its hash changed. Unsaved edits
    // in OTHER menus of the same file are kept and re-applied on top.
    for (const [sid, f] of Object.entries(S.forms)) {
      if (f.data.file !== res.file) continue;
      let data = await api('/api/section/' + sid);
      if (data.locked && S.vaultPw) data = await api('/api/section/' + sid + '/read', { body: { vault_password: S.vaultPw } });
      S.forms[sid] = { data, edits: sid === id ? {} : f.edits };
    }
    await refreshIssues();
    route();
  } catch (e) {
    if (e.status === 409) toast(e.message, true);
    else toast('Not saved: ' + e.message, true);
  }
}

// ------------------------------------------------------------------ vault
async function pageVault(view) {
  const page = h('div', { class: 'page' });
  view.append(page);
  let st = await loadSection('credentials', true);
  if (st.data.locked && S.vaultPw) {
    try {
      st.data = await api('/api/section/credentials/read', { body: { vault_password: S.vaultPw } });
    } catch (e) { S.vaultPw = null; }
  }
  const sec = st.data.section;
  page.append(pageHead(sec.title, sec.description, sec.file));
  if (st.data.locked) {
    const pw = h('input', { type: 'password', id: 'vault-pw', autocomplete: 'off' });
    const err = h('div');
    page.append(h('div', { class: 'notice' }, h('p', { text: 'vars/vault.yml is encrypted with Ansible Vault. Enter the vault password to view and edit it. The password is kept in this browser tab\'s memory only and is used to re-encrypt the file when you save.' })),
      h('div', { class: 'form-grid' },
        h('div', {}, h('label', { for: 'vault-pw', text: 'Vault password' }), pw),
        h('div', { style: 'align-self:end' }, h('button', { class: 'btn primary', type: 'button', text: 'Unlock', onclick: async () => {
          try {
            const data = await api('/api/section/credentials/read', { body: { vault_password: pw.value } });
            S.vaultPw = pw.value;
            S.forms.credentials = { data, edits: {} };
            route();
          } catch (e) { clear(err).append(h('div', { class: 'notice red' }, h('p', { text: e.message }))); }
        } }))), err);
    pw.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') page.querySelector('.btn.primary').click(); });
    return;
  }
  if (!st.data.encrypted) {
    page.append(h('div', { class: 'notice amber' },
      h('p', { text: 'This file is stored in plaintext. Encrypt it before committing it or copying it to another machine. Playbooks then need the vault password (the Run menu can pass it).' }),
      S.meta.read_only ? null : h('button', { class: 'btn', type: 'button', text: 'Encrypt vars/vault.yml…', onclick: encryptVault })));
  } else {
    page.append(h('div', { class: 'notice green' }, h('p', { text: 'Encrypted. Changes are re-encrypted with the same vault password when you save.' })));
  }
  const box = h('div', { class: 'fields' });
  page.append(box);
  const rerender = () => {
    clear(box);
    for (const f of sec.fields) box.append(renderField('credentials', f, rerender));
    renderNav(); updateSavebar();
  };
  rerender();
}

async function encryptVault() {
  const a = h('input', { type: 'password', id: 'enc-a', autocomplete: 'new-password' });
  const b = h('input', { type: 'password', id: 'enc-b', autocomplete: 'new-password' });
  const msg = h('div');
  const ok = await modal('Encrypt vars/vault.yml', [
    h('p', { text: 'Choose a vault password (8+ characters). Keep it somewhere safe — without it nobody can read the file, including the playbooks.' }),
    h('div', { class: 'form-grid' }, h('div', {}, h('label', { for: 'enc-a', text: 'Vault password' }), a),
      h('div', {}, h('label', { for: 'enc-b', text: 'Repeat' }), b)), msg],
  [{ label: 'Cancel', value: false }, { label: 'Encrypt', cls: 'primary', value: true, onClick: async () => {
    if (a.value !== b.value) { clear(msg).append(h('p', { class: 'field-danger', text: 'The passwords do not match.' })); return false; }
    try {
      await api('/api/vault/encrypt', { body: { vault_password: a.value, base_hash: S.forms.credentials.data.hash } });
      S.vaultPw = a.value; return true;
    } catch (e) { clear(msg).append(h('p', { class: 'field-danger', text: e.message })); return false; }
  } }]);
  if (ok) { toast('vars/vault.yml is now encrypted'); delete S.forms.credentials; refreshIssues(); route(); }
}

// ------------------------------------------------------------------ inventory
const GROUP_PRESETS = [
  ['storage_servers', 'Tier 0 / LSS storage nodes'],
  ['di_nodes', 'Data Instantiator (mover) nodes'],
  ['nfs_clients', 'NFS clients for verify_nfs.yml'],
  ['di_k8s_build_hosts', 'DI image build hosts (k8s mode)'],
];

function varsToText(v) {
  return Object.entries(v || {}).map(([k, x]) => k + '=' + (typeof x === 'object' ? JSON.stringify(x) : String(x))).join('\n');
}
function textToVars(t) {
  const out = {};
  for (const line of String(t || '').split('\n')) {
    const s = line.trim(); if (!s || s.startsWith('#')) continue;
    const i = s.indexOf('='); if (i < 1) throw new Error('Variable lines must be key=value: ' + s);
    const k = s.slice(0, i).trim(); let v = s.slice(i + 1).trim();
    if (/^(true|false)$/i.test(v)) v = v.toLowerCase() === 'true';
    else if (/^-?\d+$/.test(v)) v = parseInt(v, 10);
    else if (/^[\[{]/.test(v)) { try { v = JSON.parse(v); } catch (e) { /* keep string */ } }
    out[k] = v;
  }
  return out;
}

async function pageInventory(view, file) {
  file = file || (S.inv && S.inv.file) || 'inventory.yml';
  if (!S.inv || S.inv.file !== file) {
    const model = await api('/api/inventory?file=' + encodeURIComponent(file));
    S.inv = { file, model, dirty: false };
  }
  const inv = S.inv;
  const m = inv.model;
  const page = h('div', { class: 'page wide' });
  page.append(pageHead('Static hosts',
    'Hosts for on-prem or manually listed nodes. Put Tier 0 nodes in storage_servers, movers in di_nodes. Nest hosts in AZ1/AZ2/… groups to get availability-zone prefixes automatically. Cloud deployments usually use a dynamic inventory instead (see the menus below).', file));
  const sel = h('select', { id: 'inv-file', onchange: () => { if (inv.dirty && !confirm('Discard unsaved inventory changes?')) { sel.value = file; return; } S.inv = null; clear(view); pageInventory(view, sel.value); } },
    h('option', { value: 'inventory.yml', text: 'inventory.yml' }), h('option', { value: 'inventory.az.yml', text: 'inventory.az.yml (AZ-grouped sample)' }));
  sel.value = file;
  page.append(h('div', { class: 'row' },
    h('label', { for: 'inv-file', text: 'File' }), h('div', { style: 'width:280px' }, sel), h('span', { class: 'grow' }),
    h('button', { class: 'btn', type: 'button', text: 'Show resolved inventory', onclick: () => startJob({ kind: 'inventory_graph', inventory: file }) }),
    h('a', { class: 'btn quiet', href: '#/files/' + file, text: 'Edit as YAML' })));
  if (m.complex && m.complex.length) {
    page.append(h('div', { class: 'notice red' }, h('p', { text: 'This file uses structure the host editor does not support (' + m.complex.join('; ') + '). Use “Edit as YAML” to avoid losing it.' })));
    view.append(page); return;
  }
  page.append(h('div', { class: 'notice amber' }, h('p', { text: 'Saving rewrites the host tree. The comment header at the top of the file is kept; comments inside the tree are not. A backup is written first.' })));
  const tree = h('div');
  page.append(h('div', { class: 'spacer' }), tree);
  const markDirty = () => { inv.dirty = true; renderNav(); updateSavebar(); };

  const hostsTable = (hosts) => {
    const tbl = h('table', { class: 'hosts' }, h('tr', {}, h('th', { text: 'Host name' }), h('th', { text: 'ansible_host (IP / DNS)' }), h('th', { text: 'Other variables (key=value per line)' }), h('th')));
    hosts.forEach((host, idx) => {
      const others = Object.assign({}, host.vars || {}); delete others.ansible_host;
      const name = h('input', { type: 'text', value: host.name, 'aria-label': 'Host name', oninput: () => { host.name = name.value.trim(); markDirty(); } });
      const ip = h('input', { type: 'text', class: 'mono', value: (host.vars || {}).ansible_host || '', 'aria-label': 'ansible_host',
        oninput: () => { host.vars = host.vars || {}; if (ip.value.trim()) host.vars.ansible_host = ip.value.trim(); else delete host.vars.ansible_host; markDirty(); } });
      const ta = h('textarea', { rows: Math.max(1, Object.keys(others).length), 'aria-label': 'Host variables', placeholder: 'di_node_ip=10.0.12.100',
        oninput: () => { try { const v = textToVars(ta.value); host.vars = Object.assign(v, ip.value.trim() ? { ansible_host: ip.value.trim() } : {}); ta.setCustomValidity(''); markDirty(); } catch (e) { ta.setCustomValidity(e.message); ta.reportValidity(); } } });
      ta.value = varsToText(others);
      tbl.append(h('tr', {}, h('td', {}, name), h('td', {}, ip), h('td', {}, ta),
        h('td', {}, h('button', { class: 'btn quiet', type: 'button', text: 'Remove', onclick: () => { hosts.splice(idx, 1); markDirty(); draw(); } }))));
    });
    return tbl;
  };
  const varsBox = (obj, holder, label) => {
    const ta = h('textarea', { rows: Math.max(2, Object.keys(obj || {}).length + 1), placeholder: 'ansible_user=ubuntu', 'aria-label': label,
      oninput: () => { try { holder.vars = textToVars(ta.value); ta.setCustomValidity(''); markDirty(); } catch (e) { ta.setCustomValidity(e.message); ta.reportValidity(); } } });
    ta.value = varsToText(obj);
    return h('div', { class: 'vars-box' }, h('div', { class: 'small muted', text: label }), ta);
  };
  const draw = () => {
    clear(tree);
    m.groups.forEach((g, gi) => {
      const nameIn = h('input', { type: 'text', value: g.name, 'aria-label': 'Group name', oninput: () => { g.name = nameIn.value.trim(); markDirty(); } });
      const preset = GROUP_PRESETS.find((p) => p[0] === g.name);
      const box = h('section', { class: 'group' }, h('header', {}, nameIn,
        h('span', { class: 'muted small grow', text: preset ? preset[1] : '' }),
        h('button', { class: 'btn', type: 'button', text: 'Add host', onclick: () => { g.hosts.push({ name: '', vars: {} }); markDirty(); draw(); } }),
        h('button', { class: 'btn', type: 'button', text: 'Add AZ subgroup', onclick: () => { g.children.push({ name: 'AZ' + (g.children.length + 1), hosts: [], vars: {} }); markDirty(); draw(); } }),
        h('button', { class: 'btn quiet', type: 'button', text: 'Remove group', onclick: () => { if (confirm('Remove group ' + g.name + ' and its hosts?')) { m.groups.splice(gi, 1); markDirty(); draw(); } } })));
      const body = h('div', { class: 'group-body' });
      if (g.hosts.length) body.append(hostsTable(g.hosts));
      g.children.forEach((c, ci) => {
        const cn = h('input', { type: 'text', value: c.name, 'aria-label': 'Subgroup name', style: 'max-width:180px', oninput: () => { c.name = cn.value.trim(); markDirty(); } });
        body.append(h('div', { class: 'sub' }, h('header', {}, cn,
          h('span', { class: 'muted small grow', text: /^AZ\d+$/.test(c.name) ? 'Availability zone ' + c.name.slice(2) : 'Child group' }),
          h('button', { class: 'btn', type: 'button', text: 'Add host', onclick: () => { c.hosts.push({ name: '', vars: {} }); markDirty(); draw(); } }),
          h('button', { class: 'btn quiet', type: 'button', text: 'Remove', onclick: () => { g.children.splice(ci, 1); markDirty(); draw(); } })),
        c.hosts.length ? hostsTable(c.hosts) : h('p', { class: 'muted small', style: 'padding:0 12px', text: 'No hosts yet.' }),
        varsBox(c.vars, c, 'Subgroup variables')));
      });
      if (!g.hosts.length && !g.children.length) body.append(h('p', { class: 'muted small', style: 'padding:10px 14px 0', text: 'No hosts yet.' }));
      body.append(varsBox(g.vars, g, 'Group variables (apply to every host in ' + (g.name || 'this group') + ')'));
      box.append(body);
      tree.append(box);
    });
    const missing = GROUP_PRESETS.filter((p) => !m.groups.some((g) => g.name === p[0]));
    tree.append(h('div', { class: 'btn-row' }, missing.map((p) => h('button', { class: 'btn', type: 'button', text: 'Add ' + p[0], onclick: () => {
      m.groups.push({ name: p[0], hosts: [], vars: {}, children: [] }); markDirty(); draw(); } })),
    h('button', { class: 'btn quiet', type: 'button', text: 'Add custom group', onclick: () => { m.groups.push({ name: 'new_group', hosts: [], vars: {}, children: [] }); markDirty(); draw(); } })));
  };
  draw();
  const allVars = varsBox(m.all_vars, { set vars(v) { m.all_vars = v; } }, 'Variables for every host (all.vars)');
  page.append(h('h2', { text: 'Global variables' }), h('div', { class: 'group' }, allVars));
  view.append(page);
}

async function saveInventory(previewOnly) {
  const inv = S.inv;
  const body = { file: inv.file, model: inv.model, base_hash: inv.model.hash };
  try {
    const pv = await api('/api/inventory/preview', { body });
    const choice = await modal('Review changes to ' + inv.file, [diffView(pv.diff)],
      [{ label: previewOnly ? 'Close' : 'Cancel', value: 'no' }, { label: 'Save to file', cls: 'primary', value: 'save' }]);
    if (choice !== 'save') return;
    const res = await api('/api/inventory/save', { body });
    toast('Saved ' + inv.file + (res.backup ? ' (backup: ' + res.backup + ')' : ''));
    S.inv = null; refreshIssues(); route();
  } catch (e) { toast('Not saved: ' + e.message, true); }
}

// ------------------------------------------------------------------ raw editor
async function pageRaw(view, path) {
  path = path || 'vars/main.yml';
  const page = h('div', { class: 'page wide' });
  page.append(pageHead('Raw file editor', 'Edit any configuration file directly. YAML and INI files are validated before they are written; a backup is kept in .gui-backups/. Prefer the menus for vars/main.yml — they keep comments intact automatically.'));
  const sel = h('select', { id: 'raw-file', onchange: () => { location.hash = '#/files/' + sel.value; } },
    S.meta.editable_files.map((f) => h('option', { value: f, text: f })));
  sel.value = path;
  page.append(h('div', { class: 'row' }, h('label', { for: 'raw-file', text: 'File' }), h('div', { style: 'width:320px' }, sel)));
  view.append(page);
  const d = await api('/api/file?path=' + encodeURIComponent(path));
  if (d.encrypted) {
    page.append(h('div', { class: 'spacer' }), h('div', { class: 'notice' }, h('p', { text: 'This file is vault-encrypted. Edit it from the Credentials menu.' })));
    return;
  }
  const ta = h('textarea', { id: 'raw-text', spellcheck: 'false', style: 'min-height:62vh', 'aria-label': path });
  ta.value = d.text;
  page.append(h('div', { class: 'spacer' }), ta, h('div', { class: 'spacer' }), h('div', { class: 'btn-row' },
    h('button', { class: 'btn', type: 'button', text: 'Revert', onclick: () => { ta.value = d.text; } }),
    S.meta.read_only ? null : h('button', { class: 'btn primary', type: 'button', text: 'Review and save', onclick: async () => {
      try {
        const pv = await api('/api/file/preview', { body: { path, text: ta.value, base_hash: d.hash } });
        const c = await modal('Review changes to ' + path, [diffView(pv.diff)], [{ label: 'Cancel', value: 0 }, { label: 'Save to file', cls: 'primary', value: 1 }]);
        if (!c) return;
        const r = await api('/api/file/save', { body: { path, text: ta.value, base_hash: d.hash } });
        toast('Saved ' + path); Object.keys(S.forms).forEach((k) => { if (!Object.keys(S.forms[k].edits).length) delete S.forms[k]; });
        refreshIssues(); route();
        return r;
      } catch (e) { toast('Not saved: ' + e.message, true); }
    } })));
}

// ------------------------------------------------------------------ run
const RUN_PRESETS = {
  precheck: { playbook: 'site.yml', tags: ['discovery', 'precheck'], check: true },
};

async function defaultInventory() {
  try {
    const st = await loadSection('ansible-cfg');
    const v = st.data.values['defaults.inventory'];
    return v && v.state === 'set' ? String(v.value).split(',').map((s) => s.trim()).filter(Boolean) : [];
  } catch (e) { return []; }
}

function vaultControls() {
  const pw = h('input', { type: 'password', id: 'run-vault', autocomplete: 'off', placeholder: S.vaultPw ? 'Using the password from Credentials' : '' });
  const pf = h('input', { type: 'text', id: 'run-vault-file', class: 'mono', placeholder: '~/.vault_pass' });
  return {
    nodes: [h('div', {}, h('label', { for: 'run-vault', text: 'Vault password (if vault.yml is encrypted)' }), pw),
      h('div', {}, h('label', { for: 'run-vault-file', text: '…or vault password file' }), pf)],
    read: () => (pw.value || S.vaultPw ? { vault_password: pw.value || S.vaultPw } : (pf.value.trim() ? { vault_password_file: pf.value.trim() } : {})),
  };
}

async function pageRun(view, preset) {
  const pre = RUN_PRESETS[preset] || {};
  const invDefault = await defaultInventory();
  const page = h('div', { class: 'page wide' });
  page.append(pageHead('Run playbooks', 'Build an ansible-playbook command from the options below and run it on this controller through scripts/run.sh (UTF-8 locale + log file in ~/logs). Output streams live; every run is kept in Job history.'));
  const pbSel = h('select', { id: 'pb' }, S.meta.playbooks.map((p) => h('option', { value: p.id, text: p.title })));
  pbSel.value = pre.playbook || 'site.yml';
  const desc = h('div');
  const invBox = h('div', { class: 'tagpick' });
  S.meta.inventories.forEach((f) => {
    const cb = h('input', { type: 'checkbox', value: f, checked: invDefault.includes(f) });
    invBox.append(h('label', {}, cb, f));
  });
  const limit = h('input', { type: 'text', id: 'limit', class: 'mono', placeholder: 'e.g. instance2026* or @.new_instances_limit' });
  const tagsBox = h('div', { class: 'tagpick' });
  const skipBox = h('div', { class: 'tagpick' });
  const check = h('input', { type: 'checkbox', id: 'chk', checked: pre.check !== undefined ? pre.check : true });
  const diff = h('input', { type: 'checkbox', id: 'dif' });
  const verb = h('select', { id: 'verb' }, [0, 1, 2, 3, 4].map((n) => h('option', { value: n, text: n ? '-' + 'v'.repeat(n) : 'normal' })));
  const extraBox = h('div', { class: 'form-grid full', style: 'border:0;padding:0' });
  const extraText = h('textarea', { id: 'ev', rows: 3, placeholder: 'key=value per line, e.g. hammerspace_serial=2' });
  const vault = vaultControls();
  const cmd = h('div', { class: 'cmd' });
  const out = h('div');
  const extraInputs = {};

  const drawPb = () => {
    const pb = S.meta.playbooks.find((p) => p.id === pbSel.value);
    clear(desc).append(h('p', { class: 'muted', text: pb.description }),
      pb.destructive ? h('div', { class: 'notice red' }, h('p', { text: 'Without Check mode: ' + pb.destructive })) : null);
    clear(tagsBox); clear(skipBox);
    if (!pb.tags.length) { tagsBox.append(h('span', { class: 'muted small', text: 'This playbook has no selectable tags.' })); }
    pb.tags.forEach((t) => {
      tagsBox.append(h('label', {}, h('input', { type: 'checkbox', value: t, checked: (pre.tags || []).includes(t) && pb.id === (pre.playbook || 'site.yml'), onchange: drawCmd }), t));
      skipBox.append(h('label', {}, h('input', { type: 'checkbox', value: t, onchange: drawCmd }), t));
    });
    clear(extraBox);
    for (const k in extraInputs) delete extraInputs[k];
    for (const o of pb.extra) {
      let inp;
      if (o.type === 'bool') inp = h('select', { onchange: drawCmd }, h('option', { value: '', text: '(from vars/main.yml)' }), h('option', { value: 'true', text: 'true' }), h('option', { value: 'false', text: 'false' }));
      else if (o.type === 'enum') inp = h('select', { onchange: drawCmd }, (o.required ? [] : [h('option', { value: '', text: '(from vars/main.yml)' })]).concat(o.options.filter((x) => x !== '').map((x) => h('option', { value: x, text: x }))));
      else inp = h('input', { type: o.type === 'int' ? 'number' : 'text', oninput: drawCmd });
      extraInputs[o.key] = { o, inp };
      extraBox.append(h('div', {}, h('label', { text: o.label + (o.required ? ' (required)' : '') }), inp));
    }
    drawCmd();
  };
  const collect = () => {
    const extra = {};
    for (const [k, { o, inp }] of Object.entries(extraInputs)) {
      if (inp.value === '') continue;
      extra[k] = o.type === 'bool' ? inp.value === 'true' : o.type === 'int' ? Number(inp.value) : inp.value;
    }
    return Object.assign({
      kind: 'playbook', playbook: pbSel.value,
      inventory: [...invBox.querySelectorAll('input:checked')].map((x) => x.value),
      limit: limit.value.trim(),
      tags: [...tagsBox.querySelectorAll('input:checked')].map((x) => x.value),
      skip_tags: [...skipBox.querySelectorAll('input:checked')].map((x) => x.value),
      check: check.checked, diff: diff.checked, verbosity: Number(verb.value),
      extra, extra_vars_text: extraText.value,
    }, vault.read());
  };
  function drawCmd() {
    const r = collect();
    let s = './scripts/run.sh playbook ' + r.playbook;
    r.inventory.forEach((i) => { s += ' -i ' + i; });
    if (r.limit) s += " --limit '" + r.limit + "'";
    if (r.tags.length) s += ' --tags ' + r.tags.join(',');
    if (r.skip_tags.length) s += ' --skip-tags ' + r.skip_tags.join(',');
    if (r.check) s += ' --check';
    if (r.diff) s += ' --diff';
    if (r.verbosity) s += ' -' + 'v'.repeat(r.verbosity);
    const ev = Object.assign({}, r.extra);
    for (const line of r.extra_vars_text.split('\n')) { const i = line.indexOf('='); if (i > 0) ev[line.slice(0, i).trim()] = line.slice(i + 1).trim(); }
    if (Object.keys(ev).length) s += " -e '" + JSON.stringify(ev) + "'";
    if (r.vault_password) s += ' --vault-password-file <temp file>';
    if (r.vault_password_file) s += ' --vault-password-file ' + r.vault_password_file;
    cmd.textContent = s;
  }
  [pbSel].forEach((x) => x.addEventListener('change', drawPb));
  [limit, extraText].forEach((x) => x.addEventListener('input', drawCmd));
  [check, diff, verb].forEach((x) => x.addEventListener('change', drawCmd));
  invBox.addEventListener('change', drawCmd);

  page.append(h('div', { class: 'form-grid' },
    h('div', { class: 'full' }, h('label', { for: 'pb', text: 'Playbook' }), pbSel, desc),
    h('div', { class: 'full' }, h('label', { text: 'Inventory (-i)' }), invBox),
    h('div', {}, h('label', { for: 'limit', text: 'Limit (--limit)' }), limit),
    h('div', {}, h('label', { for: 'verb', text: 'Verbosity' }), verb),
    h('div', { class: 'full row' },
      h('label', { class: 'switch' }, check, h('span', { class: 'track' }), 'Check mode (dry run, --check)'),
      h('label', { class: 'switch' }, diff, h('span', { class: 'track' }), 'Show diffs (--diff)')),
    h('div', { class: 'full' }, h('label', { text: 'Only these tags (--tags)' }), tagsBox),
    h('details', { class: 'full' }, h('summary', { text: 'Skip tags (--skip-tags)' }), h('div', { class: 'spacer' }), skipBox),
    extraBox,
    h('div', { class: 'full' }, h('label', { for: 'ev', text: 'Extra variables (-e)' }), extraText),
    vault.nodes,
  ));
  page.append(h('h2', { text: 'Command' }), cmd, h('div', { class: 'spacer' }),
    h('div', { class: 'btn-row' }, S.meta.read_only ? h('span', { class: 'muted', text: 'Read-only mode: copy the command and run it in a terminal.' })
      : h('button', { class: 'btn primary', type: 'button', text: 'Run playbook', onclick: () => startJob(collect(), out) })), out);
  view.append(page);
  drawPb();
}

async function startJob(req, outEl) {
  try {
    const job = await api('/api/jobs', { body: req });
    toast('Started: ' + job.title);
    location.hash = '#/jobs/' + job.id;
  } catch (e) {
    if (e.status === 412 && e.data && e.data.needs_confirm) {
      const word = h('input', { type: 'text', id: 'confirm-word', autocomplete: 'off' });
      const ok = await modal('Confirm a change to real systems', [
        h('div', { class: 'notice red' }, h('p', { text: e.message })),
        h('p', { text: 'This is not a dry run. Type RUN to continue.' }), word],
      [{ label: 'Cancel', value: false }, { label: 'Run it', cls: 'danger', value: true, onClick: () => word.value.trim() === 'RUN' }]);
      if (ok) return startJob(Object.assign({}, req, { confirm: true }), outEl);
    } else {
      toast('Not started: ' + e.message, true);
    }
  }
}

// ------------------------------------------------------------------ utilities
async function pageUtilities(view) {
  const api_ = await loadSection('hs-api').catch(() => null);
  const defaults = {};
  if (api_) for (const [k, v] of Object.entries(api_.data.values)) if (v.state === 'set') defaults[k] = v.value;
  const page = h('div', { class: 'page wide' });
  page.append(pageHead('Hammerspace utilities', 'The cluster-side Python tools from the repo root. They talk only to the Anvil API. Dry run is on by default; a real run asks for confirmation and passes --yes.'));
  const sel = h('select', { id: 'util' }, S.meta.utilities.map((u) => h('option', { value: u.id, text: u.title + ' (' + u.id + ')' })));
  const form = h('div');
  const pw = h('input', { type: 'password', id: 'hs-pw', autocomplete: 'off' });
  page.append(h('div', { class: 'form-grid' }, h('div', { class: 'full' }, h('label', { for: 'util', text: 'Tool' }), sel)), h('div', { class: 'spacer' }), form);
  const draw = () => {
    const u = S.meta.utilities.find((x) => x.id === sel.value);
    const inputs = {};
    const grid = h('div', { class: 'form-grid' }, h('p', { class: 'full muted', text: u.description }));
    for (const o of u.options) {
      let inp;
      let dv = o.default;
      if (o.default_from && defaults[o.default_from] && !String(defaults[o.default_from]).includes('{{')) dv = defaults[o.default_from];
      if (o.type === 'bool') {
        inp = h('input', { type: 'checkbox', checked: !!dv });
        grid.append(h('div', {}, h('label', { class: 'switch' }, inp, h('span', { class: 'track' }), o.label)));
      } else if (o.type === 'enum') {
        inp = h('select', {}, (o.required ? [] : [h('option', { value: '', text: '—' })]).concat(o.options.map((x) => h('option', { value: x, text: x }))));
        if (dv) inp.value = dv;
        grid.append(h('div', {}, h('label', { text: o.label + (o.required ? ' (required)' : '') }), inp));
      } else if (o.type === 'list') {
        inp = h('textarea', { rows: 2, placeholder: 'One per line' });
        grid.append(h('div', {}, h('label', { text: o.label }), inp));
      } else {
        inp = h('input', { type: o.type === 'int' ? 'number' : 'text', value: dv === undefined ? '' : String(dv), placeholder: o.placeholder || '' });
        grid.append(h('div', {}, h('label', { text: o.label + (o.required ? ' (required)' : '') }), inp));
      }
      inputs[o.key] = { o, inp };
    }
    grid.append(h('div', {}, h('label', { for: 'hs-pw', text: 'Hammerspace password (sent as HAMMERSPACE_PASSWORD)' }), pw),
      h('p', { class: 'full muted small', text: 'Give either a password file above or the password here. It is passed to the script through its environment, never on the command line.' }));
    clear(form).append(grid, h('div', { class: 'spacer' }),
      S.meta.read_only ? null : h('button', { class: 'btn primary', type: 'button', text: 'Run tool', onclick: () => {
        const options = {};
        for (const [k, { o, inp }] of Object.entries(inputs)) {
          if (o.type === 'bool') options[k] = inp.checked;
          else if (o.type === 'list') options[k] = inp.value.split('\n').map((x) => x.trim()).filter(Boolean);
          else if (inp.value !== '') options[k] = o.type === 'int' ? Number(inp.value) : inp.value;
        }
        startJob({ kind: 'utility', script: u.id, options, hs_password: pw.value || undefined });
      } }));
  };
  sel.onchange = draw;
  draw();
  view.append(page);
}

// ------------------------------------------------------------------ jobs
function jobsTable(jobs) {
  const t = h('table', { class: 'grid' }, h('tr', {}, h('th', { text: 'Job' }), h('th', { text: 'Status' }), h('th', { text: 'Started' }), h('th', { text: 'Duration' })));
  for (const j of jobs) {
    const dur = j.finished && j.started ? Math.round(j.finished - j.started) + ' s' : (j.status === 'running' ? 'running…' : '');
    const cls = j.status === 'succeeded' ? 'green' : j.status === 'running' ? 'teal' : (j.status === 'failed' || j.status === 'lost') ? 'red' : 'amber';
    t.append(h('tr', {}, h('td', {}, h('a', { href: '#/jobs/' + j.id, text: j.title })),
      h('td', {}, h('span', { class: 'chip ' + cls, text: j.status + (j.returncode !== null && j.returncode !== undefined && j.status !== 'running' ? ' (rc ' + j.returncode + ')' : '') })),
      h('td', { text: fmtTime(j.started) }), h('td', { text: dur })));
  }
  return t;
}

async function pageJobs(view, jobId) {
  if (!jobId) {
    const d = await api('/api/jobs');
    const page = h('div', { class: 'page wide' }, pageHead('Job history', 'Every playbook and tool run from this GUI. Logs are kept in .gui-jobs/.'));
    page.append(d.jobs.length ? jobsTable(d.jobs) : h('div', { class: 'empty', text: 'No jobs yet. Start one from Run playbooks, Hammerspace utilities or OS Upgrade Check.' }));
    view.append(page);
    return;
  }
  const page = h('div', { class: 'page wide' });
  const title = h('h1');
  const status = h('span', { class: 'chip' });
  const argv = h('div', { class: 'cmd' });
  const log = h('pre', { class: 'log', 'aria-live': 'off' });
  const stopBtn = h('button', { class: 'btn', type: 'button', text: 'Stop job' });
  const follow = h('input', { type: 'checkbox', checked: true });
  const after = h('div');
  page.append(h('div', { class: 'page-head' }, title, h('div', { class: 'row' }, status, h('span', { class: 'grow' }),
    h('label', { class: 'switch' }, follow, h('span', { class: 'track' }), 'Follow output'), stopBtn, h('a', { class: 'btn quiet', href: '#/jobs', text: 'All jobs' }))),
  argv, h('div', { class: 'spacer' }), log, after);
  view.append(page);
  let offset = 0;
  let done = false;
  const tick = async () => {
    if (done) return;
    let d;
    try { d = await api('/api/jobs/' + jobId + '?offset=' + offset); } catch (e) { clear(log).append(e.message); done = true; return; }
    title.textContent = d.title;
    status.textContent = d.status + (d.returncode !== null && d.returncode !== undefined ? ' (rc ' + d.returncode + ')' : '');
    status.className = 'chip ' + (d.status === 'succeeded' ? 'green' : d.status === 'running' ? 'teal' : d.status === 'failed' ? 'red' : 'amber');
    argv.textContent = '$ ' + d.argv.join(' ');
    if (d.text) { log.append(d.text); offset = d.offset; if (follow.checked) log.scrollTop = log.scrollHeight; }
    stopBtn.disabled = d.status !== 'running';
    if (d.status !== 'running' && d.status !== 'stopping' && d.status !== 'queued') {
      done = true; stopPollers(); refreshJobsLed(); refreshIssues();
      const ex = (d.meta && d.meta.extra) || {};
      if (d.meta && d.meta.playbook === 'os_upgrade_snapshot.yml') {
        after.append(h('div', { class: 'spacer' }), h('a', { class: 'btn primary', href: '#/upgrade/' + (ex.snapshot_run || 'os-upgrade'), text: 'Open the OS Upgrade Check results' }));
      }
    }
  };
  stopBtn.onclick = async () => { await api('/api/jobs/' + jobId + '/stop', { method: 'POST' }); toast('Stopping job'); };
  await tick();
  if (!done) S.pollers.push(setInterval(tick, 1000));
}

// ------------------------------------------------------------------ OS upgrade check
async function pageUpgrade(view, runName, host) {
  const runs = (await api('/api/upgrade/runs')).runs;
  if (runName && host) return upgradeReport(view, runName, host, runs);
  const page = h('div', { class: 'page wide' });
  page.append(pageHead('OS Upgrade Check',
    'Capture each node\'s configuration before an OS upgrade, upgrade and reboot, then capture it again and see exactly what changed — kernel and packages, RAID assembly, mounts, NFS exports and nfsd tuning, sunrpc slots, read-ahead, IRQ pinning, NIC names and drivers, services and firewall. Collection is read-only.'));
  const known = runs.map((r) => r.run);
  const today = new Date().toISOString().slice(0, 10);
  const runIn = h('input', { type: 'text', id: 'run-name', class: 'mono', value: runName || (known[0] || 'os-upgrade-' + today), list: 'run-names' });
  const dl = h('datalist', { id: 'run-names' }, known.map((r) => h('option', { value: r })));
  const hostsIn = h('input', { type: 'text', id: 'run-hosts', class: 'mono', value: 'storage_servers' });
  const limit = h('input', { type: 'text', id: 'run-limit', class: 'mono', placeholder: 'optional, e.g. node101' });
  const invDefault = await defaultInventory();
  const invBox = h('div', { class: 'tagpick' });
  S.meta.inventories.forEach((f) => invBox.append(h('label', {}, h('input', { type: 'checkbox', value: f, checked: invDefault.includes(f) }), f)));
  const vault = vaultControls();
  page.append(h('div', { class: 'form-grid' },
    h('div', {}, h('label', { for: 'run-name', text: 'Run name (one per maintenance window)' }), runIn, dl),
    h('div', {}, h('label', { for: 'run-hosts', text: 'Hosts (group or pattern)' }), hostsIn),
    h('div', {}, h('label', { for: 'run-limit', text: 'Limit' }), limit),
    h('div', { class: 'full' }, h('label', { text: 'Inventory' }), invBox), vault.nodes));
  const job = (phase) => Object.assign({
    kind: 'playbook', playbook: 'os_upgrade_snapshot.yml',
    inventory: [...invBox.querySelectorAll('input:checked')].map((x) => x.value), limit: limit.value.trim(),
    extra: { snapshot_phase: phase, snapshot_run: runIn.value.trim(), snapshot_hosts: hostsIn.value.trim() },
  }, vault.read());
  const cur = runs.find((r) => r.run === (runIn.value.trim()));
  const preDone = cur && cur.hosts.length && cur.hosts.every((x) => x.pre);
  const postDone = cur && cur.hosts.length && cur.hosts.every((x) => x.post);
  page.append(h('div', { class: 'spacer' }), h('ol', { class: 'steps' },
    h('li', { class: preDone ? 'done' : '' }, h('h3', { text: 'Capture the baseline' }),
      h('p', { text: 'Run before touching the OS. Writes snapshots/<run>/<host>/pre.txt. A second baseline for the same run is refused, so a post-upgrade state can never overwrite it.' }),
      S.meta.read_only ? null : h('button', { class: 'btn primary', type: 'button', text: 'Capture pre-upgrade snapshot', onclick: () => startJob(job('pre')) })),
    h('li', {}, h('h3', { text: 'Upgrade the operating system and reboot' }),
      h('p', { text: 'Do this outside the GUI with your normal process (e.g. dnf upgrade / apt full-upgrade, then reboot). Consider disabling availability-drop first for planned maintenance (Hammerspace utilities → Availability drop).' })),
    h('li', { class: postDone ? 'done' : '' }, h('h3', { text: 'Capture again and compare' }),
      h('p', { text: 'Writes post.txt and the diff reports for every host, plus a fleet summary.' }),
      S.meta.read_only ? null : h('div', { class: 'btn-row' },
        h('button', { class: 'btn primary', type: 'button', text: 'Capture post-upgrade snapshot and compare', onclick: () => startJob(job('post')) }),
        h('button', { class: 'btn', type: 'button', text: 'Rebuild reports only', onclick: () => startJob(job('diff')) }))),
    h('li', { class: postDone ? 'done' : '' }, h('h3', { text: 'Review the differences' }),
      h('p', { text: 'Critical sections (RAID, mounts, exports, nfsd runtime, NVMe, NIC names) are listed first; expected churn like package versions is kept separate.' }))));
  runIn.addEventListener('change', () => { location.hash = '#/upgrade/' + encodeURIComponent(runIn.value.trim()); });

  page.append(h('h2', { text: 'Snapshot runs' }));
  if (!runs.length) page.append(h('div', { class: 'empty', text: 'No snapshots yet. Capture a pre-upgrade snapshot to start.' }));
  for (const r of runs) {
    if (runName && r.run !== runName && runs.some((x) => x.run === runName)) continue;
    const t = h('table', { class: 'grid' }, h('tr', {}, h('th', { text: 'Host' }), h('th', { text: 'Before' }), h('th', { text: 'After' }), h('th', { text: 'Result' }), h('th')));
    for (const x of r.hosts) {
      let result = h('span', { class: 'chip', text: x.pre ? 'waiting for post-upgrade snapshot' : 'no baseline' });
      if (x.diff) {
        const c = x.diff.counts;
        result = x.diff.identical ? h('span', { class: 'chip green', text: 'no changes' })
          : h('span', {}, c.critical ? h('span', { class: 'chip red', text: c.critical + ' critical' }) : null, ' ',
            c.warning ? h('span', { class: 'chip amber', text: c.warning + ' warning' }) : null, ' ',
            c.info ? h('span', { class: 'chip', text: c.info + ' info' }) : null);
      }
      const snap = (m) => m ? h('span', {}, h('span', { text: (m.captured_utc || '').replace('T', ' ').replace('Z', ' UTC') }), h('br'), h('span', { class: 'muted small mono', text: m.kernel || '' })) : h('span', { class: 'muted', text: '—' });
      t.append(h('tr', {}, h('td', { text: x.host }), h('td', {}, snap(x.pre)), h('td', {}, snap(x.post)), h('td', {}, result),
        h('td', {}, x.diff || (x.archives && x.archives.length > 1) ? h('a', { href: '#/upgrade/' + encodeURIComponent(r.run) + '/' + encodeURIComponent(x.host), text: 'Open report' }) : null)));
    }
    page.append(h('h3', { text: r.run, style: 'margin:18px 0 8px' }), t);
  }
  view.append(page);
}

async function upgradeReport(view, run, host, runs) {
  const r = runs.find((x) => x.run === run);
  const hx = r && r.hosts.find((x) => x.host === host);
  const page = h('div', { class: 'page wide' });
  page.append(h('div', { class: 'page-head' }, h('a', { href: '#/upgrade/' + encodeURIComponent(run), text: 'OS Upgrade Check' }),
    h('h1', { text: host }), h('p', { class: 'lede', text: 'Run ' + run })));
  const q = 'run=' + encodeURIComponent(run) + '&host=' + encodeURIComponent(host) + '&token=' + encodeURIComponent(S.token);
  page.append(h('div', { class: 'btn-row' },
    h('a', { class: 'btn', href: '/api/upgrade/report?format=html&' + q, target: '_blank', rel: 'noopener', text: 'Open HTML report' }),
    h('a', { class: 'btn', href: '/api/upgrade/report?format=txt&' + q, target: '_blank', rel: 'noopener', text: 'Text report' }),
    h('a', { class: 'btn quiet', href: '/api/upgrade/report?format=pre&' + q, target: '_blank', rel: 'noopener', text: 'pre.txt' }),
    h('a', { class: 'btn quiet', href: '/api/upgrade/report?format=post&' + q, target: '_blank', rel: 'noopener', text: 'post.txt' })));
  // compare arbitrary archived captures
  const opts = ['pre.txt', 'post.txt'].concat(hx ? hx.archives : []);
  const a = h('select', { id: 'cmp-a' }, opts.map((o) => h('option', { value: o, text: o })));
  const b = h('select', { id: 'cmp-b' }, opts.map((o) => h('option', { value: o, text: o })));
  a.value = 'pre.txt'; b.value = 'post.txt';
  const body = h('div');
  page.append(h('div', { class: 'spacer' }), h('details', {}, h('summary', { text: 'Compare two other captures of this host' }),
    h('div', { class: 'form-grid', style: 'margin-top:10px' }, h('div', {}, h('label', { for: 'cmp-a', text: 'Before' }), a),
      h('div', {}, h('label', { for: 'cmp-b', text: 'After' }), b),
      h('div', { style: 'align-self:end' }, h('button', { class: 'btn', type: 'button', text: 'Compare', onclick: async () => {
        try { renderDiffReport(clear(body), await api('/api/upgrade/compare', { body: { run, host, before: a.value, after: b.value } })); }
        catch (e) { toast(e.message, true); }
      } })))), h('div', { class: 'spacer' }), body);
  view.append(page);
  try { renderDiffReport(body, await api('/api/upgrade/report?' + q)); }
  catch (e) { body.append(h('div', { class: 'notice amber' }, h('p', { text: e.message + ' — capture the post-upgrade snapshot, or compare two archived captures above.' }))); }
}

function renderDiffReport(el, res) {
  const c = res.counts;
  const bm = res.before.meta, am = res.after.meta;
  el.append(h('p', { class: 'muted', text: 'Before: ' + (bm.captured_utc || '?') + ' (kernel ' + (bm.kernel || '?') + ')   After: ' + (am.captured_utc || '?') + ' (kernel ' + (am.kernel || '?') + ')' }));
  el.append(h('div', { class: 'facts' },
    fact(res.identical ? 'None' : c.sections_changed + ' of ' + c.sections_total, 'Sections changed'),
    fact(String(c.critical), 'Critical'), fact(String(c.warning), 'Warning'), fact(String(c.info), 'Informational')));
  for (const w of res.warnings || []) el.append(h('div', { class: 'spacer' }), h('div', { class: 'notice amber' }, h('p', { text: w })));
  if (res.identical) el.append(h('div', { class: 'spacer' }), h('div', { class: 'notice green' }, h('p', { text: 'No configuration differences between the two snapshots.' })));
  if (res.attention && res.attention.length) {
    el.append(h('h2', { text: 'Needs attention' }));
    const ul = h('ul', { class: 'issues' });
    res.attention.forEach((a) => {
      const m = a.match(/^\[(\w+)\]\s*(.*)$/);
      const lvl = m ? m[1].toLowerCase() : 'info';
      ul.append(h('li', {}, h('span', { class: 'lvl ' + (lvl === 'critical' ? 'error' : lvl), text: lvl === 'critical' ? 'Critical' : 'Warning' }), h('span', { class: 'msg', text: m ? m[2] : a })));
    });
    el.append(ul);
  }
  if (res.key_facts && res.key_facts.length) {
    const factTable = (rows) => {
      const t = h('table', { class: 'grid' }, h('tr', {}, h('th', { text: 'Item' }), h('th', { text: 'Before' }), h('th', { text: 'After' })));
      rows.forEach((f) => t.append(h('tr', { class: f.changed ? 'changed' : '' }, h('td', {}, h('code', { text: f.key })), h('td', { text: f.before }), h('td', { text: f.after }))));
      return t;
    };
    const chg = res.key_facts.filter((f) => f.changed);
    const same = res.key_facts.filter((f) => !f.changed);
    el.append(h('h2', { text: 'Key facts' }));
    if (chg.length) el.append(factTable(chg));
    else el.append(h('p', { class: 'muted', text: 'None of the headline values changed.' }));
    if (same.length) el.append(h('details', { style: 'margin-top:8px' }, h('summary', { text: 'Show ' + same.length + ' unchanged values' }), h('div', { class: 'spacer' }), factTable(same)));
  }
  const changed = res.sections.filter((s) => s.status !== 'unchanged');
  const rank = { critical: 0, warning: 1, info: 2 };
  changed.sort((a, b) => rank[a.severity] - rank[b.severity] || a.name.localeCompare(b.name));
  if (changed.length) el.append(h('h2', { text: 'Changed sections' }));
  for (const s of changed) {
    const det = h('details', { class: 'section', open: s.severity === 'critical' });
    const sevCls = s.severity === 'critical' ? 'red' : s.severity === 'warning' ? 'amber' : '';
    det.append(h('summary', {}, h('span', { class: 'chip ' + sevCls, text: s.severity }), h('code', { text: s.name }), h('span', { text: s.status }), h('span', { class: 'desc', text: s.description })));
    const d = s.detail || {};
    if (s.kind === 'packages' && s.status === 'changed') {
      const t = h('table', { class: 'grid' }, h('tr', {}, h('th', { text: 'Change' }), h('th', { text: 'Package' }), h('th', { text: 'Before' }), h('th', { text: 'After' })));
      d.upgraded.forEach((p) => t.append(h('tr', {}, h('td', { text: 'upgraded' }), h('td', {}, h('code', { text: p.name })), h('td', { text: p.before }), h('td', { text: p.after }))));
      d.downgraded.forEach((p) => t.append(h('tr', { class: 'changed' }, h('td', { text: 'downgraded' }), h('td', {}, h('code', { text: p.name })), h('td', { text: p.before }), h('td', { text: p.after }))));
      d.added.forEach((p) => t.append(h('tr', {}, h('td', { text: 'added' }), h('td', {}, h('code', { text: p.name })), h('td'), h('td', { text: p.after }))));
      d.removed.forEach((p) => t.append(h('tr', { class: 'changed' }, h('td', { text: 'removed' }), h('td', {}, h('code', { text: p.name })), h('td', { text: p.before }), h('td'))));
      d.multi_version.forEach((p) => t.append(h('tr', {}, h('td', { text: 'versions' }), h('td', {}, h('code', { text: p.name })), h('td', { text: '− ' + (p.versions_removed.join(', ') || 'none') }), h('td', { text: '+ ' + (p.versions_added.join(', ') || 'none') }))));
      det.append(h('p', { class: 'muted small', text: d.total_before + ' packages before, ' + d.total_after + ' after.' }), t);
    } else if (s.kind === 'kv' && s.status === 'changed') {
      const t = h('table', { class: 'grid' }, h('tr', {}, h('th', { text: 'Key' }), h('th', { text: 'Before' }), h('th', { text: 'After' })));
      d.changed.forEach((x) => t.append(h('tr', { class: 'changed' }, h('td', {}, h('code', { text: x.key })), h('td', { text: x.before }), h('td', { text: x.after }))));
      d.added.forEach((x) => t.append(h('tr', {}, h('td', {}, h('code', { text: x.key })), h('td', { class: 'muted', text: 'absent' }), h('td', { text: x.after }))));
      d.removed.forEach((x) => t.append(h('tr', {}, h('td', {}, h('code', { text: x.key })), h('td', { text: x.before }), h('td', { class: 'muted', text: 'absent' }))));
      det.append(t);
    } else {
      det.append(diffView((d.diff || []).slice(0, 1500)));
    }
    el.append(det);
  }
  const same = res.sections.filter((s) => s.status === 'unchanged').map((s) => s.name);
  if (same.length) el.append(h('h2', { text: 'Unchanged (' + same.length + ')' }), h('p', { class: 'muted small', text: same.join(', ') }));
}

// ------------------------------------------------------------------ docs
function renderMarkdown(md) {
  const root = h('div', { class: 'doc' });
  const lines = md.replace(/\r/g, '').split('\n');
  let i = 0;
  const inline = (text) => {
    const frag = document.createDocumentFragment();
    const re = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
    let last = 0; let m;
    while ((m = re.exec(text))) {
      if (m.index > last) frag.append(text.slice(last, m.index));
      const tok = m[0];
      if (tok[0] === '`') frag.append(h('code', { text: tok.slice(1, -1) }));
      else if (tok.startsWith('**')) frag.append(h('strong', { text: tok.slice(2, -2) }));
      else {
        const mm = tok.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        const href = mm[2];
        if (/^https?:\/\//.test(href)) frag.append(h('a', { href, target: '_blank', rel: 'noopener noreferrer', text: mm[1] }));
        else if (/\.md(#.*)?$/.test(href)) frag.append(h('a', { href: '#/docs/' + href.replace(/#.*$/, ''), text: mm[1] }));
        else frag.append(h('span', { text: mm[1] }));
      }
      last = m.index + tok.length;
    }
    if (last < text.length) frag.append(text.slice(last));
    return frag;
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; root.append(h('pre', {}, h('code', { text: buf.join('\n') }))); continue;
    }
    const hm = line.match(/^(#{1,6})\s+(.*)$/);
    if (hm) { root.append(h('h' + Math.min(hm[1].length, 4), {}, inline(hm[2]))); i++; continue; }
    if (/^\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) rows.push(lines[i++]);
      const t = h('table');
      rows.forEach((r, ri) => {
        if (/^\|[\s:|-]+\|?$/.test(r)) return;
        const cells = r.replace(/^\||\|$/g, '').split('|');
        t.append(h('tr', {}, cells.map((c) => h(ri === 0 ? 'th' : 'td', {}, inline(c.trim())))));
      });
      root.append(t); continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ''));
      root.append(h('blockquote', {}, inline(buf.join(' ')))); continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const list = h(ordered ? 'ol' : 'ul');
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        list.append(h('li', {}, inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ''))));
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s+/.test(lines[i])) { list.lastChild.append(' ', inline(lines[i].trim())); i++; }
      }
      root.append(list); continue;
    }
    if (!line.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#|```|\||>|\s*([-*]|\d+\.)\s)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) { buf.push(lines[i++]); }
    root.append(h('p', {}, inline(buf.join(' '))));
  }
  return root;
}

async function pageDocs(view, name) {
  const list = (await api('/api/docs')).docs;
  name = name || 'gui/README.md';
  if (!list.includes(name)) name = list[0];
  const page = h('div', { class: 'page wide' });
  page.append(pageHead('Documentation', 'The repository\'s own guides, rendered here so they are available on air-gapped controllers.'));
  const ul = h('ul', { class: 'doc-list' }, list.map((d) => h('li', {}, h('button', { type: 'button', 'aria-current': d === name ? 'true' : 'false', text: d, onclick: () => { location.hash = '#/docs/' + d; } }))));
  const doc = h('div');
  page.append(h('div', { class: 'doc-layout' }, h('nav', { 'aria-label': 'Documents' }, ul), doc));
  view.append(page);
  const d = await api('/api/doc?name=' + encodeURIComponent(name));
  doc.append(renderMarkdown(d.text));
}

boot();
