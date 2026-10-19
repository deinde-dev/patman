'use strict';

// Token handling: a ?token=... in the URL is remembered, then removed
// from the address bar so it does not end up in screenshots or history
const TOKEN_KEY = 'patman-token';
function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
}
function setToken(tok) {
  try { localStorage.setItem(TOKEN_KEY, tok); } catch (e) { /* ignore */ }
}
(function () {
  const params = new URLSearchParams(location.search);
  if (params.has('token')) {
    setToken(params.get('token'));
    history.replaceState(null, '', location.pathname + location.hash);
  }
})();

class AuthError extends Error {}

async function api(path, opts = {}) {
  const headers = {};
  const tok = getToken();
  if (tok) headers.Authorization = 'Bearer ' + tok;
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    opts = { method: 'POST', ...opts, body: JSON.stringify(opts.json) };
    delete opts.json;
  }
  const resp = await fetch('api/' + path, { ...opts, headers });
  if (resp.status === 401) throw new AuthError('Token needed');
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok && resp.status !== 409) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
  return data;
}

// Small DOM helper: h('div', {class: 'x'}, child, 'text', ...)
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

// Parse a database time (local, 'YYYY-MM-DD HH:MM:SS') or a patchwork
// time (UTC, 'YYYY-MM-DDTHH:MM:SS')
function parseTime(str, utc) {
  if (!str) return null;
  const iso = str.replace(' ', 'T');
  return new Date(utc && !/[zZ+]/.test(iso.slice(10)) ? iso + 'Z' : iso);
}

function ago(date) {
  if (!date || isNaN(date)) return '';
  const secs = (Date.now() - date.getTime()) / 1000;
  const future = secs < 0;
  const s = Math.abs(secs);
  let txt;
  if (s < 90) txt = 'just now';
  else if (s < 3600) txt = `${Math.round(s / 60)}m`;
  else if (s < 86400) txt = `${Math.round(s / 3600)}h`;
  else if (s < 86400 * 14) txt = `${Math.round(s / 86400)}d`;
  else if (s < 86400 * 60) txt = `${Math.round(s / 86400 / 7)}w`;
  else txt = date.toLocaleDateString();
  if (txt === 'just now' || txt.includes('/') || txt.includes('.')) return txt;
  return future ? `in ${txt}` : `${txt} ago`;
}

// Order and colours for patch states
const STATE_ORDER = ['accepted', 'changes-requested', 'under-review',
                     'needs-review-ack', 'rfc', 'new', 'deferred',
                     'superseded', 'rejected', 'not-applicable', 'unknown'];
const KNOWN_STATES = new Set(['accepted', 'new', 'under-review',
  'needs-review-ack', 'rfc', 'changes-requested', 'rejected',
  'not-applicable']);
function stClass(state) {
  return 'st-' + (KNOWN_STATES.has(state) ? state : 'other');
}
function sortedStates(states) {
  return Object.entries(states || {}).sort(
    (a, b) => STATE_ORDER.indexOf(a[0]) - STATE_ORDER.indexOf(b[0]));
}
function stateBar(states, count) {
  if (!count) return null;
  return h('div', { class: 'bar' }, sortedStates(states).map(([st, n]) =>
    h('span', { class: stClass(st), title: `${n} ${st}`,
                style: `width:${100 * n / count}%;background:currentColor` })));
}
function stateChips(states) {
  return sortedStates(states).map(([st, n]) =>
    h('span', { class: `chip ${stClass(st)}` }, `${n} ${st}`));
}
function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }
function npatches(n) { return `${n} patch${n === 1 ? '' : 'es'}`; }

// Render an email body, with quoted lines dimmed and long runs of
// quoted text folded away
// Show an email, collapsing long quotes. If onChunk is given, each comment
// which follows a quote gets a button which calls onChunk() with the quote
// and the comment
//
// onChunk(part) is called when 'Revise this' is tapped under a comment, with
// the comment and the end of the quote it is about.
//
// onEdit(comment, splice, cancel) is called when 'Edit' is tapped under a
// comment. It returns a form to show in place of the comment; splice(text)
// gives the whole mail with the comment changed to text (or dropped, along
// with its quote, if text is empty) and cancel() puts the comment back.
function renderMail(text, onChunk, onEdit) {
  const out = h('div', { class: 'mail' });
  const lines = (text || '').replace(/\s+$/, '').split('\n');
  let i = 0;
  let lastQuote = null;
  let quoteStart = 0;
  while (i < lines.length) {
    if (lines[i].startsWith('>')) {
      let j = i;
      while (j < lines.length && (lines[j].startsWith('>') ||
             (lines[j] === '' && j + 1 < lines.length &&
              lines[j + 1].startsWith('>')))) j++;
      const run = lines.slice(i, j);
      const quote = (ls) => h('span', { class: 'q' }, ls.join('\n'));
      if (run.length > 6) {
        // Keep the last few lines visible since they give the context
        // for the reply that follows
        const hidden = run.slice(0, -3);
        out.append(h('details', {},
          h('summary', { title: 'Show the rest of the quote' },
            `▸ ${hidden.length} quoted lines`), quote(hidden)));
        out.append(quote(run.slice(-3)));
      } else {
        out.append(quote(run));
      }
      lastQuote = run;
      quoteStart = i;
      i = j;
    } else {
      let j = i;
      while (j < lines.length && !lines[j].startsWith('>')) j++;
      // Split off the tag and sign-off, which are not part of the comment
      let k = i;
      while (k < j && !/^(Reviewed-by:|Regards,)/.test(lines[k])) k++;
      const comment = lines.slice(i, k).join('\n');
      const textEl = h('span', {}, comment + '\n');
      out.append(textEl);
      if ((onChunk || onEdit) && lastQuote && comment.trim()) {
        // Send the end of the quote, which is what the comment is about
        const part = [...lastQuote.slice(-8), '', comment.trim()].join('\n');
        const btns = h('div', { class: 'chunk-btns' },
          onChunk && h('button', { class: 'chunk-btn', onclick: () => onChunk(part),
                                   title: 'Ask the agent to change this comment' },
                       '✎ Revise this'),
          onEdit && h('button', { class: 'chunk-btn', onclick: () => edit(),
                                  title: 'Change this comment yourself' }, 'Edit'));
        out.append(btns);
        const [start, end, qstart] = [i, k, quoteStart];
        const splice = (newText) => {
          const body = newText.trim();
          let res;
          if (body) {
            // Keep the blank lines around the comment
            const old = lines.slice(start, end);
            const lead = old.findIndex((l) => l.trim());
            const trail = old.length - 1 - old.findLastIndex((l) => l.trim());
            res = [...lines.slice(0, start), ...old.slice(0, lead), ...body.split('\n'),
                   ...old.slice(old.length - trail), ...lines.slice(end)];
          } else {
            res = [...lines.slice(0, qstart), ...lines.slice(end)];
          }
          return res.join('\n') + '\n';
        };
        const edit = () => {
          const form = onEdit(comment.trim(), splice, () => form.replaceWith(textEl, btns));
          textEl.replaceWith(form);
          btns.remove();
        };
      }
      if (k < j) out.append(lines.slice(k, j).join('\n') + '\n');
      lastQuote = null;
      i = j;
    }
  }
  return out;
}

const main = document.getElementById('main');
const titleEl = document.getElementById('title');

// patman's version, shown below each screen once the server has given it
const versionEl = h('div', { class: 'version' });
let versionAsked = false;
function showVersion() {
  if (versionAsked) return;
  versionAsked = true;
  api('version').then((v) => {
    versionEl.textContent = v.version ? `patman ${v.version}` : '';
  }).catch(() => { versionAsked = false; });
}
const backBtn = document.getElementById('back');
backBtn.addEventListener('click', () => history.back());

// The last screen shown in each tab, and the scroll position of each screen,
// so that going back to a tab or a screen finds it as it was left. These
// are kept for the browser tab, if the browser allows
function loadPlaces(key) {
  try {
    return new Map(JSON.parse(sessionStorage.getItem(key) || '[]'));
  } catch (err) {
    return new Map();
  }
}
function savePlaces(key, map) {
  try {
    sessionStorage.setItem(key, JSON.stringify([...map].slice(-100)));
  } catch (err) {
    // Not available, e.g. in a private window
  }
}
const tabScreens = loadPlaces('patman-tabs');
const scrollPlaces = loadPlaces('patman-scroll');
let currentTab = null;

function setView(title, tab, back, ...content) {
  titleEl.textContent = title;
  backBtn.hidden = !back;
  for (const a of document.querySelectorAll('nav a')) {
    a.classList.toggle('active', a.dataset.tab === tab);
  }
  if (tab) {
    currentTab = tab;
    tabScreens.set(tab, location.hash);
    savePlaces('patman-tabs', tabScreens);
  }
  main.replaceChildren(...content.flat().filter((c) => c !== null && c !== undefined && c !== false),
                       versionEl);
  showVersion();
}

function showError(err, tab) {
  if (err instanceof AuthError) {
    const input = h('input', { type: 'password', placeholder: 'Token',
                               autocomplete: 'current-password' });
    const save = () => { setToken(input.value.trim()); route(); refreshSync(); };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
    setView('patman', tab, false, h('div', { class: 'card' },
      h('div', { class: 'name' }, 'This server needs a token'),
      h('div', { class: 'desc' },
        "Enter the token given to 'patman serve --token'"),
      input, h('button', { class: 'btn', onclick: save,
                           title: 'Use this token for the server' }, 'Save')));
    return;
  }
  setView('patman', tab, false, h('div', { class: 'error' }, String(err.message || err)));
}

// --- Series ---

// A search box and optional filter chips above a list of cards. Each item
// is {card, text, groups} where text is searched and groups lists the
// filters the item belongs to. Each filter is [id, label, tooltip]
function filteredList(key, items, filters) {
  let current = filters ? filters[0][0] : null;
  try {
    const saved = localStorage.getItem(`patman-filter-${key}`);
    if (filters && filters.some(([id]) => id === saved)) current = saved;
  } catch (e) { /* ignore */ }
  const search = h('input', { type: 'search', class: 'search',
                              placeholder: 'Search', 'aria-label': 'Search' });
  const count = h('div', { class: 'section' });
  const empty = h('div', { class: 'empty', hidden: true }, 'Nothing matches');
  const chips = filters ? h('div', { class: 'tabs' }) : null;
  const update = () => {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const it of items) {
      const ok = (!current || it.groups.includes(current)) &&
                 words.every((w) => it.text.includes(w));
      it.card.hidden = !ok;
      shown += ok;
    }
    count.textContent = `${shown} of ${items.length}`;
    empty.hidden = shown > 0;
    if (chips) {
      chips.replaceChildren(...filters.map(([id, label, tip]) => h('button', {
        class: id === current ? 'on' : '', title: tip,
        onclick: () => {
          current = id;
          try { localStorage.setItem(`patman-filter-${key}`, id); }
          catch (e) { /* ignore */ }
          update();
        } }, label)));
    }
  };
  for (const it of items) it.text = it.text.toLowerCase();
  search.addEventListener('input', update);
  update();
  return [search, chips, count, h('div', { class: 'list' }, items.map((it) => it.card)),
          empty];
}

// Patchwork series IDs increase over time, so give a good recency order
function linkNum(link) { return parseInt(link, 10) || 0; }

const FINAL_STATES = new Set(['accepted', 'rejected', 'superseded',
                              'not-applicable']);

async function viewSeriesList() {
  const list = await api('series');
  const items = list.filter((s) => s.version !== null);
  if (!items.length) {
    return setView('Series', 'series', false,
                   h('div', { class: 'empty' }, 'No series yet'));
  }
  items.sort((a, b) => (b.sent || '').localeCompare(a.sent || '') ||
                       linkNum(b.link) - linkNum(a.link));
  setView('Series', 'series', false, ...filteredList('series', items.map((s) => {
    const comments = s.num_comments + s.cover_num_comments;
    const final = Object.entries(s.states).every(([st]) => FINAL_STATES.has(st));
    const card = h('a', { class: 'card', href: `#/series/${s.id}`,
                          title: 'Show the patches in this series' },
      h('div', { class: 'row' },
        h('div', { class: 'grow name' }, s.name),
        h('div', { class: 'ver' }, `v${s.version}`)),
      s.desc && h('div', { class: 'desc' }, s.desc),
      stateBar(s.states, s.count),
      h('div', { class: 'meta' },
        h('span', {}, `${s.states.accepted || 0}/${s.count} accepted`),
        comments ? h('span', { class: 'ncomments some' },
                     plural(comments, 'comment')) : null,
        s.sent && h('span', {}, `sent ${ago(parseTime(s.sent))}`),
        !s.link && h('span', {}, 'not sent')));
    const groups = ['all'];
    if (s.link && s.count && !final) groups.push('active');
    return { card, text: `${s.name} ${s.desc || ''}`, groups };
  }), [['active', 'Active', 'Sent series with patches still under review'],
        ['all', 'All', 'All your series']]));
}

function commentToggle(card, kind, itemId, ups) {
  let box = null;
  return async (ev) => {
    if (ev.target.closest('a, .comments')) return;
    if (box) { box.remove(); box = null; return; }
    box = h('div', { class: 'comments' }, h('div', { class: 'ver' }, 'Loading…'));
    card.append(box);
    try {
      const q = ups ? `?ups=${encodeURIComponent(ups)}` : '';
      const comments = await api(`comments/${kind}/${itemId}${q}`);
      box.replaceChildren(...(comments.length ? comments.map((c) =>
        h('div', {},
          h('div', { class: 'who' }, c.name || c.email || 'Unknown',
            h('span', { class: 'when' }, ago(parseTime(c.date, true)))),
          renderMail(c.content))) : [h('div', { class: 'ver' }, 'No comments')]));
    } catch (err) {
      box.replaceChildren(h('div', { class: 'error' }, err.message));
    }
  };
}

async function viewSeries(id, wantVer) {
  const ser = await api(`series/${id}`);
  const pw = ser.patchwork_url;
  const versions = ser.versions;
  const sv = versions.find((v) => v.version === wantVer) || versions[0];
  const tabs = versions.length > 1 ? h('div', { class: 'tabs' }, versions.map((v) =>
    h('button', { class: v === sv ? 'on' : '', title: `Show version ${v.version}`,
                  onclick: () => { location.replace(`#/series/${id}/${v.version}`); } },
      `v${v.version}`))) : null;

  const cards = [];
  if (sv.cover_id) {
    const card = h('div', { class: 'card patch', title: 'Tap to show the comments' },
      h('div', { class: 'row' },
        h('div', { class: 'grow name' }, 'Cover letter'),
        h('span', { class: 'ncomments' + (sv.cover_num_comments ? ' some' : '') },
          plural(sv.cover_num_comments, 'comment'))),
      sv.cover_name && h('div', { class: 'desc' }, sv.cover_name),
      h('div', { class: 'meta' },
        h('a', { class: 'extlink', href: mailHref('cover', sv.cover_id, ser.upstream),
                 title: 'Read the cover letter and its comments' }, 'Read'),
        pw && h('a', { class: 'extlink', href: `${pw}/cover/${sv.cover_id}/`,
                       target: '_blank', rel: 'noopener',
                       title: 'Open the cover letter on patchwork, in a new tab' },
                'Open in patchwork ↗')));
    card.addEventListener('click', commentToggle(card, 'cover', sv.cover_id, ser.upstream));
    cards.push(card);
  }
  for (const p of sv.patches) {
    const card = h('div', { class: 'card patch',
                            title: p.patch_id ? 'Tap to show the comments' : null },
      h('div', { class: 'row' },
        h('span', { class: 'seq' }, p.seq),
        h('div', { class: 'grow' }, p.subject),
        h('span', { class: 'ncomments' + (p.num_comments ? ' some' : '') },
          p.num_comments ? `💬 ${p.num_comments}` : '')),
      h('div', { class: 'meta' },
        p.state && h('span', { class: `chip ${stClass(p.state)}` }, p.state),
        p.patch_id && h('a', { class: 'extlink',
          href: mailHref('patch', p.patch_id, ser.upstream),
          title: 'Read the patch and its comments' }, 'Read'),
        pw && p.patch_id && h('a', { class: 'extlink',
          href: `${pw}/patch/${p.patch_id}/`, target: '_blank', rel: 'noopener',
          title: 'Open the patch on patchwork, in a new tab' }, 'patchwork ↗')));
    if (p.patch_id) {
      card.addEventListener('click', commentToggle(card, 'patch', p.patch_id, ser.upstream));
    }
    cards.push(card);
  }
  setView(ser.name, 'series', true,
    h('div', { class: 'card' },
      h('div', { class: 'name' }, ser.desc || ser.name),
      h('div', { class: 'meta' },
        h('span', {}, `v${sv.version}`),
        ser.upstream && h('span', {}, ser.upstream),
        sv.link ? h('span', {}, `series ${sv.link}`) : h('span', {}, 'no link'),
        sv.archive_tag && h('span', {}, 'archived')),
      h('div', { class: 'meta' }, stateChips(sv.states)),
      sv.link && seriesSync(id, sv.version)),
    tabs,
    h('div', { class: 'section' },
      `${sv.patches.length} patch${sv.patches.length === 1 ? '' : 'es'}`),
    h('div', { class: 'list' }, cards));
}

// A button which syncs one version of a series from patchwork, showing the
// series again once the sync is done
function seriesSync(id, version) {
  const hash = location.hash;
  const msg = h('span', { class: 'meta' });
  const btn = h('button', { class: 'btn light',
                            title: 'Gather the patch states and review tags for this version ' +
                                   'from patchwork' }, 'Sync');
  const poll = async (jobId) => {
    if (location.hash !== hash) return;
    let st;
    try {
      st = await api(`jobs/${jobId}`);
    } catch (err) {
      msg.textContent = err.message;
      return;
    }
    if (!st.finished) {
      setTimeout(() => poll(jobId), 1500);
      return;
    }
    refreshJobCount();
    if (jobStatus(st)[0] === 'ok') {
      route();
      return;
    }
    btn.disabled = false;
    btn.textContent = 'Sync';
    msg.replaceChildren('Sync failed: ', h('a', { href: `#/jobs/${jobId}`,
                                                  title: 'Show the job and its output' },
                                          'see the output'));
  };
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Syncing…';
    msg.textContent = '';
    try {
      const job = await api(`series/${id}/sync`, { json: { version } });
      refreshJobCount();
      poll(job.id);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = 'Sync';
      msg.textContent = err.message;
    }
  });
  return h('div', { class: 'actions' }, btn, msg);
}

// --- Patches ---

// Number of diff lines to show before asking
const DIFF_LINES = 1500;

function diffLine(line) {
  let cls = null;
  if (line.startsWith('diff --git')) cls = 'file';
  else if (line.startsWith('+++') || line.startsWith('---')) cls = null;
  else if (line.startsWith('+')) cls = 'add';
  else if (line.startsWith('-')) cls = 'del';
  else if (line.startsWith('@@')) cls = 'hunk';
  return h('span', { class: cls }, line || ' ');
}

// Number of lines of source to show each time the gap next to a chunk of
// a diff is widened
const CONTEXT_LINES = 5;

// A blob hash from a diff, or null if it is all zeros (no file)
function realBlob(oid) { return /[^0]/.test(oid) ? oid : null; }

// Parse a chunk's '@@' line, giving the first and last line on each side
function chunkRange(line) {
  const m = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
  if (!m) return null;
  const side = (start, count) => {
    const n = count === undefined ? 1 : +count;
    const first = n ? +start : +start + 1;
    return { first, last: first + n - 1 };
  };
  return { old: side(m[1], m[2]), new: side(m[3], m[4]) };
}

// Pick out each file in a diff: its name, the lines added and removed, its
// blobs before and after the change and where its first chunk starts. The
// result is keyed by the index of the file's 'diff --git' line
function diffFiles(lines) {
  const files = new Map();
  let cur = null;
  lines.forEach((line, idx) => {
    const m = line.match(/^diff --git a\/.* b\/(.*)/);
    if (m) {
      cur = { name: m[1], add: 0, del: 0, old: null, new: null, at: null };
      files.set(idx, cur);
      return;
    }
    if (!cur) return;
    const ix = !cur.at && line.match(/^index ([0-9a-f]+)\.\.([0-9a-f]+)/);
    if (ix) {
      cur.old = realBlob(ix[1]);
      cur.new = realBlob(ix[2]);
    } else if (line.startsWith('@@')) {
      const r = chunkRange(line);
      if (r && !cur.at) cur.at = { old: r.old.first, new: r.new.first };
    } else if (line.startsWith('+') && !line.startsWith('+++')) cur.add++;
    else if (line.startsWith('-') && !line.startsWith('---')) cur.del++;
  });
  return files;
}

// Files read from the git repo, keyed by the blob hashes asked for
const blobs = new Map();

// Read a file from the git repo, preferring its version after the change
function getBlob(file) {
  const hashes = [file.new, file.old].filter(Boolean);
  const key = hashes.join(',');
  if (!blobs.has(key)) {
    const got = api(`blob?${hashes.map((oid) => `hash=${oid}`).join('&')}`)
      .then((b) => ({ hash: b.hash, isNew: b.hash === file.new,
                      lines: b.text ? b.text.replace(/\n$/, '').split('\n') : [] }));
    got.catch(() => blobs.delete(key));
    blobs.set(key, got);
  }
  return blobs.get(key);
}

// Link to the page showing the whole of a file in a diff, at a line given
// for each side
function fileHref(file, at) {
  at = at || { old: 1, new: 1 };
  return `#/file/${file.new || '-'}/${file.old || '-'}/${at.new}/${at.old}/` +
    encodeURIComponent(file.name);
}

// The source lines in the gap before or after a chunk of a diff, shown a
// few at a time. The gap starts at line 'lo' (on each side) and has 'count'
// lines, or runs to the end of the file if that is null. 'after' is true if
// there is a chunk above the gap and 'before' if there is one below it
function diffGap(file, lo, count, after, before) {
  if (count === 0) return [];
  const top = h('div', {});
  const bottom = h('div', {});
  const bar = h('div', { class: 'expand' });
  let src = null;
  let ntop = 0;
  let nbottom = 0;
  const left = () => (src ? src.count - ntop - nbottom : count);
  // The line of the chunk below the gap, else the last one shown above it
  const near = () => {
    const n = before ? count : Math.max(ntop - 1, 0);
    return { old: lo.old + n, new: lo.new + n };
  };
  const srcLine = (n) => h('span', { class: 'ctx' }, ` ${src.lines[n - 1]}`);
  const draw = () => {
    const n = left();
    if (n === 0) { bar.remove(); return; }
    const btn = (label, add, sub, tip) => h('button', { onclick: () => widen(add, sub),
                                                        title: tip }, label);
    const first = src ? src.first : (file.new ? lo.new : lo.old);
    bar.replaceChildren(...[
      after && btn(`↓ ${CONTEXT_LINES} more`, CONTEXT_LINES, 0,
                   `Show ${CONTEXT_LINES} more lines below the chunk above`),
      before && btn(`↑ ${CONTEXT_LINES} more`, 0, CONTEXT_LINES,
                    `Show ${CONTEXT_LINES} more lines above the chunk below`),
      n !== null && n > CONTEXT_LINES && btn(`All ${n}`, n, 0,
                                             'Show all the lines in this gap'),
      h('a', { href: fileHref(file, near()),
               title: 'Show the whole file, with line numbers, at this point' },
        'View whole file'),
      n !== null && h('span', {}, `${plural(n, 'line')} from ${first + ntop}`),
    ].filter(Boolean));
  };
  // Show more lines below the chunk above (ntop) or above the one below
  const widen = async (addTop, addBottom) => {
    if (!src) {
      try {
        const b = await getBlob(file);
        const first = b.isNew ? lo.new : lo.old;
        src = { lines: b.lines, first,
                count: count ?? Math.max(0, b.lines.length - first + 1) };
      } catch (err) {
        bar.replaceChildren(h('span', { class: 'error' }, err.message));
        return;
      }
    }
    for (let i = Math.min(addTop, left()); i > 0; i--) {
      top.append(srcLine(src.first + ntop));
      ntop++;
    }
    for (let i = Math.min(addBottom, left()); i > 0; i--) {
      nbottom++;
      bottom.prepend(srcLine(src.first + src.count - nbottom));
    }
    draw();
  };
  draw();
  return [top, bar, bottom];
}

function renderDiff(diff) {
  const lines = diff.replace(/\n$/, '').split('\n');
  const files = diffFiles(lines);
  const box = h('div', { class: 'diff' });
  const show = (limit) => {
    const out = [];
    let file = null;
    let prev = null;  // last line on each side of the chunk above
    // Gaps are only shown for a changed file, since the diff of a new or
    // removed file holds all of it
    const gaps = () => file && file.old && file.new;
    const next = () => (prev ? { old: prev.old + 1, new: prev.new + 1 } : { old: 1, new: 1 });
    lines.slice(0, limit).forEach((line, idx) => {
      if (files.has(idx)) {
        if (gaps() && prev) out.push(diffGap(file, next(), null, true, false));
        file = files.get(idx);
        prev = null;
        out.push(h('span', { class: 'file' }, file.old || file.new
          ? [h('a', { class: 'view', href: fileHref(file, file.at),
                      title: 'Show the whole file, with line numbers' }, 'View whole file'),
             ' '] : null, line));
        return;
      }
      const r = file && line.startsWith('@@') && chunkRange(line);
      if (r) {
        const lo = next();
        if (gaps()) out.push(diffGap(file, lo, r.old.first - lo.old, !!prev, true));
        prev = { old: r.old.last, new: r.new.last };
      }
      out.push(diffLine(line));
    });
    if (limit >= lines.length && gaps() && prev) {
      out.push(diffGap(file, next(), null, true, false));
    }
    box.replaceChildren(...out.flat());
  };
  show(DIFF_LINES);
  const more = lines.length > DIFF_LINES && h('div', { class: 'actions' },
    h('button', { class: 'btn light small', title: 'Show the rest of the diff',
                  onclick: (ev) => {
      show(lines.length);
      ev.target.remove();
    } }, `Show all ${lines.length} lines`));
  const stats = [...files.values()];
  const add = stats.reduce((n, f) => n + f.add, 0);
  const del = stats.reduce((n, f) => n + f.del, 0);
  return [
    h('div', { class: 'section' }, `${plural(stats.length, 'file')}, +${add} -${del}`),
    stats.length > 1 && h('div', { class: 'card files' }, stats.map((f) =>
      h('div', { class: 'row' }, h('div', { class: 'grow' }, f.name),
        h('span', { class: 'n plus' }, `+${f.add}`),
        h('span', { class: 'n minus' }, `-${f.del}`)))),
    box, more];
}

// Show the whole of a file from a diff, from the git repo, at a line
async function viewFile(newOid, oldOid, newLine, oldLine, name) {
  const b = await getBlob({ new: newOid === '-' ? null : newOid,
                            old: oldOid === '-' ? null : oldOid });
  const target = b.isNew ? newLine : oldLine;
  const width = `${String(b.lines.length).length}ch`;
  const rows = b.lines.map((text, i) => h('span', { class: i + 1 === target ? 'target' : null },
    h('i', { class: 'ln', style: `width:${width}` }, i + 1), text));
  setView(name.split('/').pop(), null, true,
    h('div', { class: 'card' },
      h('div', { class: 'name' }, name),
      h('div', { class: 'meta' },
        h('span', {}, b.isNew ? 'with the patch applied' : 'before the patch'),
        h('span', {}, plural(b.lines.length, 'line')),
        h('span', {}, `blob ${b.hash}`))),
    h('div', { class: 'diff src' }, rows));
  if (rows[target - 1]) rows[target - 1].scrollIntoView({ block: 'center' });
}

function renderComments(comments) {
  return comments.length ? comments.map((c) =>
    h('div', { class: 'card' },
      h('div', { class: 'who' }, c.name || c.email || 'Unknown',
        h('span', { class: 'when' }, ago(parseTime(c.date, true)))),
      renderMail(c.content))) : [h('div', { class: 'empty' }, 'No comments')];
}

// Link to the page for a patch or cover letter
function mailHref(kind, id, ups) {
  return `#/${kind}/${id}${ups ? '/' + encodeURIComponent(ups) : ''}`;
}

async function viewMail(kind, id, ups) {
  const q = ups ? `?ups=${encodeURIComponent(ups)}` : '';
  const m = await api(`${kind}/${id}${q}`);
  const comments = h('div', { class: 'list' },
                     h('div', { class: 'spinner' }, 'Loading comments…'));
  const ser = m.series[0];
  setView(m.name, null, true,
    h('div', { class: 'card' },
      h('div', { class: 'name' }, m.name),
      h('div', { class: 'meta' },
        m.state && h('span', { class: `chip ${stClass(m.state)}` }, m.state),
        m.submitter && h('span', {}, m.submitter),
        m.date && h('span', {}, ago(parseTime(m.date, true))),
        ser && h('span', {}, `series ${ser.id} v${ser.version}`)),
      m.web_url && h('div', { class: 'actions' },
        h('a', { class: 'btn light small', href: m.web_url, target: '_blank',
                 rel: 'noopener', title: 'Open this on patchwork, in a new tab' },
          'patchwork ↗'))),
    h('div', { class: 'section' }, kind === 'patch' ? 'Commit message' : 'Cover letter'),
    h('div', { class: 'card' }, renderMail(m.content)),
    m.diff && renderDiff(m.diff),
    h('div', { class: 'section' }, 'Comments'),
    comments);
  try {
    comments.replaceChildren(...renderComments(await api(`comments/${kind}/${id}${q}`)));
  } catch (err) {
    comments.replaceChildren(h('div', { class: 'error' }, err.message));
  }
}

// Button which shows a patch or cover letter in the box it returns, so
// that it can be read next to its review
function mailToggle(kind, id, ups) {
  const what = kind === 'patch' ? 'patch' : 'cover letter';
  const box = h('div', { class: 'inline-mail' });
  let open = false;
  let mail = null;
  const btn = h('button', { class: 'btn light small',
                            title: `Show or hide the ${what} here, next to its review`,
                            onclick: async () => {
    open = !open;
    btn.textContent = `${open ? 'Hide' : 'Show'} the ${what}`;
    if (!open) { box.replaceChildren(); return; }
    box.replaceChildren(h('div', { class: 'ver' }, 'Loading…'));
    try {
      const q = ups ? `?ups=${encodeURIComponent(ups)}` : '';
      mail = mail || await api(`${kind}/${id}${q}`);
      if (open) {
        box.replaceChildren(h('div', {}, renderMail(mail.content),
                              mail.diff && renderDiff(mail.diff)));
      }
    } catch (err) {
      if (open) box.replaceChildren(h('div', { class: 'error' }, err.message));
    }
  } }, `Show the ${what}`);
  return [btn, box];
}

// --- Reviews ---

const REVIEW_STATUS = {
  new: 'not drafted', draft: 'draft', edited: 'draft out of date', sent: 'sent',
  replied: 'replied', deleted: 'deleted',
};

// Check whether a review has a Gmail draft. An edited review's draft is
// created again when it is sent
const hasDraft = (r) => r.status === 'draft' || r.status === 'edited';

// Start a job and show it
async function startJob(path, data, msg) {
  try {
    const job = await api(path, { json: data || {} });
    refreshJobCount();
    location.hash = `#/jobs/${job.id}`;
  } catch (err) {
    if (msg) msg.textContent = err.message;
    else alert(err.message);
  }
}

// A button which needs a second tap to confirm. 'tip' says what it does
function confirmButton(label, cls, action, tip) {
  let armed = null;
  const btn = h('button', { class: cls, title: tip && `${tip}; tap twice to confirm`,
                            onclick: () => {
    if (armed) { clearTimeout(armed); armed = null; btn.textContent = label; action(); return; }
    btn.textContent = 'Tap to confirm';
    armed = setTimeout(() => { armed = null; btn.textContent = label; }, 3000);
  } }, label);
  return btn;
}

function reviewActions() {
  return h('div', { class: 'actions', style: 'margin:0 0 12px' },
    h('a', { class: 'btn', href: '#/review/new',
             title: 'Have the agent review a series from patchwork' }, '+ Review a series'),
    confirmButton('Scan for new versions', 'btn light',
                  () => startJob('review/scan', { drafts: true }),
                  'Look on patchwork for new versions of series you reviewed, and ' +
                  'review them'));
}

async function viewReviewList() {
  const list = await api('reviews');
  if (!list.length) {
    return setView('Reviews', 'reviews', false, reviewActions(),
      h('div', { class: 'empty' }, 'No reviews yet'));
  }
  setView('Reviews', 'reviews', false, reviewActions(),
          ...filteredList('reviews', list.map((r) => ({
    text: `${r.name} ${r.desc || ''}`,
    groups: r.replies_waiting ? ['all', 'replies'] : ['all'],
    card: h('a', { class: 'card', href: `#/reviews/${r.id}`,
                   title: 'Show the reviews of this series' },
      h('div', { class: 'row' },
        h('div', { class: 'grow name' }, r.desc || r.name),
        r.version && h('div', { class: 'ver' }, `v${r.version}`)),
      h('div', { class: 'meta' },
        r.replies_waiting ? h('span', { class: 'chip st-changes-requested' },
          `${r.replies_waiting} ${r.replies_waiting === 1 ? 'reply' : 'replies'}`)
          : null,
        h('span', {}, `${r.approved}/${r.num_reviews} approved`),
        Object.entries(r.statuses).map(([st, n]) =>
          h('span', {}, `${n} ${REVIEW_STATUS[st] || st}`)),
        r.timestamp && h('span', {}, ago(parseTime(r.timestamp))))) })),
    [['all', 'All', 'All your reviews'],
     ['replies', 'Needs reply', 'Reviews with replies waiting for your response']]));
}

// Show a job which acts on some reviews, in a strip on each of their cards,
// rather than moving to the job's page. Each entry of 'targets' has a 'strip'
// element and a 'show' function; refresh() is called when the job finishes,
// to read the reviews again, and gives the new text of each, by review ID
async function trackJob(job, verb, targets, refresh) {
  const before = new Map(targets.map((t) => [t.r.id, t.r.body]));
  const output = () => h('a', { href: `#/jobs/${job.id}`,
                                title: 'Show the output of the job' }, 'View output');
  const say = (t, cls, ...kids) => {
    t.strip.className = `jobstrip ${cls}`;
    t.strip.replaceChildren(...kids.filter((kid) => kid !== null && kid !== false));
  };
  const progress = (st) => targets.forEach((t) => say(t, 'busy',
    h('span', {}, `${verb}… ${st.queued ? 'waiting' : 'running'}`), ' ', output()));
  // The reviews cannot be changed until the job finishes
  const lock = (busy) => targets.forEach((t) => {
    t.busy = busy;
    t.show();
  });
  lock(true);
  refreshJobCount();
  progress(job);
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    if (!targets.some((t) => t.strip.isConnected)) return;
    let st;
    try {
      st = await api(`jobs/${job.id}`);
    } catch (err) {
      continue;
    }
    if (!st.finished) {
      progress(st);
      continue;
    }
    const [status] = jobStatus(st);
    if (status !== 'ok') {
      lock(false);
      targets.forEach((t) => say(t, 'failed', h('span', {}, `${verb} ${status}`),
                                 ' ', output()));
      return;
    }
    const fresh = await refresh();
    targets.forEach((t) => {
      const old = before.get(t.r.id);
      const now = fresh.get(t.r.id);
      if (now) Object.assign(t.r, now);
      t.busy = false;
      t.show();
      const changed = now && now.body !== old;
      const was = h('div', { class: 'notes', hidden: true },
                    h('div', { class: 'hint' }, 'Before:'), renderMail(old));
      say(t, 'done', h('span', {}, changed ? `${verb} done` : `${verb} done, no change`),
        changed ? ' ' : null, changed ? h('button', { class: 'link',
          title: 'Show or hide the text from before the job', onclick: () => {
          was.hidden = !was.hidden;
        } }, 'Show changes') : null,
        ' ', output(), ' ',
        h('button', { class: 'link', onclick: () => t.strip.replaceChildren(),
                      title: 'Hide this message' }, 'Dismiss'), was);
    });
    return;
  }
}

async function viewReview(id, wantVer) {
  const ser = await api(`reviews/${id}`);
  const versions = ser.versions;
  const sv = versions.find((v) => v.version === wantVer) || versions[0];
  const tabs = versions.length > 1 ? h('div', { class: 'tabs' }, versions.map((v) =>
    h('button', { class: v === sv ? 'on' : '',
                  title: `Show the reviews of version ${v.version}`,
                  onclick: () => { location.replace(`#/reviews/${id}/${v.version}`); } },
      `v${v.version}`))) : null;
  const unsent = (r) => r.status !== 'sent' && r.status !== 'replied';
  // Each review's card, so that a job can be shown on the cards it acts on
  const entries = new Map();
  const refresh = async () => {
    const again = await api(`reviews/${id}`);
    const ver = again.versions.find((v) => v.version === sv.version);
    return new Map((ver ? ver.reviews : []).map((r) => [r.id, r]));
  };
  // Start a job on some reviews (seqs, or those which 'want' for all) and show
  // it on their cards, staying on this page
  const runHere = async (path, data, seqs, verb, want, msg) => {
    let job;
    try {
      job = await api(path, { json: { version: sv.version, seqs, ...data } });
    } catch (err) {
      if (msg) msg.textContent = err.message;
      else alert(err.message);
      return;
    }
    const targets = sv.reviews.filter((r) => (seqs.length ? seqs.includes(r.seq)
                                                          : want(r)))
      .map((r) => entries.get(r.id));
    trackJob(job, verb, targets, refresh);
  };
  const redraft = (seqs) => runHere(`reviews/${id}/redraft`, {}, seqs, 'Redraft',
                                    unsent);
  const revise = (seqs, part, note, drafts, msg) => runHere(`reviews/${id}/revise`,
    { part, note, drafts }, seqs, 'Revise', unsent, msg);
  const send = (seqs) => runHere(`reviews/${id}/send`, {}, seqs, 'Send', hasDraft);
  const reviewCard = (r) => {
    const card = h('div', { class: 'card review' });
    const strip = h('div', { class: 'jobstrip' });
    const pat = r.seq ? (sv.patches || []).find((p) => p.seq === r.seq) : null;
    const itemId = r.seq ? pat && pat.patch_id : sv.cover_id;
    const label = r.seq ? `Patch ${r.seq}` : 'Cover letter';
    const [mailBtn, mailBox] = itemId
      ? mailToggle(r.seq ? 'patch' : 'cover', itemId, ser.upstream) : [null, null];
    const [commentsBtn, commentsBox] = itemId && sv.link
      ? commentsToggle(ser, sv, r.seq, itemId) : [null, null];
    const responses = commentResponses(sv, r.seq).map(replyBox);
    const title = h('div', { class: 'row' },
      h('div', { class: 'grow name' }, itemId
        ? h('a', { href: mailHref(r.seq ? 'patch' : 'cover', itemId, ser.upstream),
                   title: `Read the ${r.seq ? 'patch' : 'cover letter'} and its comments` },
            label, pat ? h('span', { class: 'desc' }, ` ${pat.subject}`) : null)
        : label),
      h('span', { class: `chip ${r.approved ? 'st-accepted' : 'st-changes-requested'}` },
        r.approved ? 'Reviewed-by' : 'comments'),
      h('span', { class: 'ver' }, REVIEW_STATUS[r.status] || r.status || ''));
    const header = h('div', {}, title, strip);
    const ask = (part) => card.replaceChildren(header, reviseForm(
      part, hasDraft(r), show,
      (note, drafts, msg) => revise([r.seq], part, note, drafts, msg)));
    const editChunk = (comment, splice, cancel) => chunkForm(
      comment, hasDraft(r), cancel,
      (text, redraft) => saveReview(r, splice(text), redraft, entries.get(r.id),
                                    refresh));
    // A review can be changed if it has not been sent and no job is acting on it
    const free = () => unsent(r) && !(entries.get(r.id) || {}).busy;
    const show = () => card.replaceChildren(header,
      renderMail(r.body, free() ? ask : null, free() ? editChunk : null),
      (mailBtn || free()) && h('div', { class: 'actions' },
        mailBtn, commentsBtn,
        free() && [
          h('button', { class: 'btn light small', title: 'Change the review yourself',
                        onclick: () => editReview(card, r, header, show,
                                                  entries.get(r.id), refresh) },
            'Edit'),
          h('button', { class: 'btn light small', onclick: () => ask(null),
                        title: 'Ask the agent to change the review' }, 'Revise'),
          confirmButton('Redraft', 'btn light small', () => redraft([r.seq]),
                        'Create the Gmail draft of this review again'),
          hasDraft(r) && confirmButton('Send', 'btn small', () => send([r.seq]),
            r.status === 'edited'
              ? 'Create the Gmail draft of this review again, since it has changed, ' +
                'and send it'
              : 'Send the Gmail draft of this review')]),
      ...[mailBox, commentsBox].filter(Boolean),
      ...(r.replies || []).map(replyBox), ...responses);
    entries.set(r.id, { r, strip, show, busy: false });
    show();
    return card;
  };
  const reviews = sv.reviews.map(reviewCard);
  const cover = sv.cover_id && !sv.reviews.some((r) => r.seq === 0) &&
    unreviewedCover(ser, sv);
  // Show the jobs which were already acting on reviews, e.g. after a reload
  const byJob = new Map();
  sv.reviews.filter((r) => r.job).forEach((r) => {
    byJob.set(r.job, [...(byJob.get(r.job) || []), entries.get(r.id)]);
  });
  byJob.forEach(async (targets, jid) => {
    const st = await api(`jobs/${jid}`).catch(() => null);
    if (st) trackJob(st, st.title.split(' ')[0], targets, refresh);
  });
  const anyUnsent = sv.reviews.some(unsent);
  const drafts = sv.reviews.filter(hasDraft).length;
  const reviseAll = h('div', {});
  const askAll = () => reviseAll.replaceChildren(h('div', { class: 'card' },
    h('div', { class: 'name' }, 'Revise all the reviews'),
    reviseForm(null, sv.reviews.some(hasDraft),
               () => reviseAll.replaceChildren(),
               (note, drafts, msg) => revise([], null, note, drafts, msg))));
  // Start a job and show it
  const reviewAgain = async (context) => {
    const job = await api('review', { json: { target: sv.link, ups: ser.upstream,
                                              drafts: true, force: true, context } });
    refreshJobCount();
    location.hash = `#/jobs/${job.id}`;
  };
  const [againBtn, againBox] = reviewButton(
    'Review again', 'btn light small',
    'Have the agent review this version of the series again, replacing its reviews',
    reviewAgain);
  const actions = sv.link && h('div', { class: 'actions' },
    anyUnsent && confirmButton('Redraft all', 'btn light small', () => redraft([]),
                               'Create the Gmail drafts of all the unsent reviews again'),
    anyUnsent && h('button', { class: 'btn light small', onclick: askAll,
                               title: 'Ask the agent to change all the unsent reviews' },
                   'Revise all'),
    drafts > 0 && confirmButton(drafts === 1 ? 'Send the draft' : `Send ${drafts} drafts`,
                                'btn small', () => send([]),
                                'Send the Gmail drafts of the reviews, first drafting again ' +
                                'any which are out of date'),
    againBtn,
    ser.patchwork_url && h('a', { class: 'btn light small', target: '_blank',
      rel: 'noopener', title: 'Open the series on patchwork, in a new tab',
      href: ser.project ? `${ser.patchwork_url}/project/${ser.project}/list/?series=${sv.link}`
                      : ser.patchwork_url },
      'patchwork ↗'));
  setView(ser.desc || ser.name, 'reviews', true,
    h('div', { class: 'card' },
      h('div', { class: 'name' }, ser.desc || ser.name),
      h('div', { class: 'meta' },
        h('span', {}, `v${sv.version}`),
        ser.upstream && h('span', {}, ser.upstream),
        sv.link && h('span', {}, `series ${sv.link}`)),
      actions, sv.link && againBox),
    reviseAll,
    tabs,
    sv.notes && h('div', { class: 'section' }, 'Notes'),
    sv.notes && h('div', { class: 'notes' }, renderMail(sv.notes)),
    h('div', { class: 'section' }, plural(reviews.length, 'review')),
    cover && h('div', { class: 'list' }, cover),
    reviews.length ? h('div', { class: 'list' }, reviews)
                   : h('div', { class: 'empty' }, 'No reviews for this version'));
}

// A card for the cover letter of a version which has no review of it, e.g.
// because the agent had nothing to say about the series as a whole. It shows
// the cover letter and can ask the agent to review just that
// A button which opens a form to start a review, asking for any context to
// give the agent, e.g. 'this is an RFC, so ignore style'. 'start(context)'
// starts it, throwing an error to show if it cannot. Returns the button and
// the box which holds the form, to go below the row of buttons
function reviewButton(label, cls, tip, start) {
  const box = h('div', {});
  const btn = h('button', { class: cls, title: tip, onclick: () => {
    btn.disabled = true;
    const text = h('textarea', { class: 'note', spellcheck: 'true',
                                 placeholder: 'Context for the agent (optional), e.g. ' +
                                              'this is an RFC, so ignore style' });
    const msg = h('div', { class: 'msg' });
    const close = () => {
      box.replaceChildren();
      btn.disabled = false;
    };
    const go = h('button', { class: 'btn small', title: tip, onclick: async () => {
      go.disabled = true;
      msg.textContent = '';
      try {
        await start(text.value.trim());
        close();
      } catch (err) {
        msg.textContent = err.message;
        go.disabled = false;
      }
    } }, 'Start the review');
    box.replaceChildren(h('div', { class: 'form' }, text,
      h('div', { class: 'actions' }, go,
        h('button', { class: 'btn light small', onclick: close,
                      title: 'Close this without starting a review' }, 'Cancel')),
      msg));
    text.focus();
  } }, label);
  return [btn, box];
}

// Show in 'strip' how a job is getting on, until it finishes. 'verb' says
// what it does, e.g. 'Reviewing'. Returns the job's final state, with the
// strip showing any failure, or null if the strip has left the page
async function followJob(job, strip, verb) {
  const say = (cls, text) => {
    strip.className = `jobstrip ${cls}`;
    strip.replaceChildren(h('span', {}, text), ' ',
      h('a', { href: `#/jobs/${job.id}`, title: 'Show the output of the job' },
        'View output'));
  };
  refreshJobCount();
  while (!job.finished) {
    say('busy', `${verb}… ${job.queued ? 'waiting' : 'running'}`);
    await new Promise((resolve) => setTimeout(resolve, 2000));
    if (!strip.isConnected) return null;
    try {
      job = await api(`jobs/${job.id}`);
    } catch (err) {
      // Try again
    }
  }
  refreshJobCount();
  const [status] = jobStatus(job);
  if (status === 'ok') say('', `${verb}: done`);
  else say('failed', `${verb}: ${status}`);
  return job;
}

// Get the comments on patchwork which have responses, for a patch (0 for the
// cover letter) of a version
function commentResponses(sv, seq) {
  return (sv.comments || []).filter((c) => c.seq === seq);
}

// A button showing the comments on patchwork about a patch (seq) or the cover
// letter (seq 0) of a reviewed version, each with 'Respond' to have the agent
// suggest a response to it. Returns the button and the box which holds them
function commentsToggle(ser, sv, seq, itemId) {
  const kind = seq ? 'patch' : 'cover';
  const box = h('div', {});
  const responded = new Set(commentResponses(sv, seq).map((c) => c.comment_id));
  const item = (c) => {
    const slot = h('div', {});
    const strip = h('div', { class: 'jobstrip' });
    const reset = () => slot.replaceChildren(responded.has(c.id)
      ? h('div', { class: 'ver' }, 'Your response to this is below')
      : h('div', { class: 'actions' },
        h('button', { class: 'btn light small', onclick: () => ask(),
                      title: 'Respond to this comment, with a suggestion from the agent' },
          'Respond')));
    const ask = () => {
      const note = h('textarea', { class: 'note', spellcheck: 'true',
                                   placeholder: 'What would you like to say? (optional)' });
      const msg = h('div', { class: 'msg' });
      const go = h('button', { class: 'btn small', onclick: async () => {
        go.disabled = true;
        msg.textContent = '';
        let job;
        try {
          job = await api(`reviews/${ser.id}/respond`, { json: {
            version: sv.version, seq, comment: c.id, note: note.value } });
        } catch (err) {
          msg.textContent = err.message;
          go.disabled = false;
          return;
        }
        // The note is no longer needed once the job has started
        slot.replaceChildren();
        const st = await followJob(job, strip, 'Suggesting a response');
        if (st && jobStatus(st)[0] === 'ok') route();
        else if (st) reset();
      }, title: 'Have the agent suggest a response, as a job, which you can then change, ' +
                'draft or send' }, 'Suggest a response');
      slot.replaceChildren(h('div', { class: 'form' }, note,
        h('div', { class: 'actions' }, go,
          h('button', { class: 'btn light small', onclick: reset,
                        title: 'Close this without responding' }, 'Cancel')), msg));
      note.focus();
    };
    reset();
    return h('div', { class: 'comments' },
      h('div', { class: 'who' }, c.name || c.email,
        h('span', { class: 'when' }, ago(parseTime(c.date, true)))),
      renderMail(c.content || ''), strip, slot);
  };
  let open = false;
  const btn = h('button', { class: 'btn light small',
                            title: 'Show the comments on patchwork, to respond to them',
                            onclick: async () => {
    open = !open;
    btn.textContent = open ? 'Hide comments' : 'Comments';
    if (!open) { box.replaceChildren(); return; }
    box.replaceChildren(h('div', { class: 'ver' }, 'Loading…'));
    try {
      const q = ser.upstream ? `?ups=${encodeURIComponent(ser.upstream)}` : '';
      const list = await api(`comments/${kind}/${itemId}${q}`);
      if (open) {
        box.replaceChildren(...(list.length ? list.map(item)
          : [h('div', { class: 'ver' }, 'No comments on patchwork')]));
      }
    } catch (err) {
      if (open) box.replaceChildren(h('div', { class: 'error' }, err.message));
    }
  } }, 'Comments');
  return [btn, box];
}

function unreviewedCover(ser, sv) {
  const [showBtn, mailBox] = mailToggle('cover', sv.cover_id, ser.upstream);
  const [commentsBtn, commentsBox] = commentsToggle(ser, sv, 0, sv.cover_id);
  const strip = h('div', { class: 'jobstrip' });
  // Once the review has started, return so that its form closes, then follow it
  const review = async (context) => {
    const job = await api('review', { json: { target: String(sv.link), ups: ser.upstream,
                                              index: '0', drafts: true, context } });
    followJob(job, strip, 'Reviewing').then(async (st) => {
      if (!st || jobStatus(st)[0] !== 'ok') return;
      // Show the new review, if the agent wrote one
      const again = await api(`reviews/${ser.id}`);
      const ver = again.versions.find((v) => v.version === sv.version);
      if (ver && ver.reviews.some((r) => r.seq === 0)) {
        route();
        return;
      }
      strip.firstChild.textContent = 'The agent has no comments on the cover letter';
    });
  };
  const [reviewBtn, reviewBox] = sv.link ? reviewButton(
    'Review it', 'btn light small',
    'Have the agent review just the cover letter, as a job, creating a Gmail draft ' +
    'if it has comments', review) : [null, null];
  return h('div', { class: 'card review' },
    h('div', { class: 'row' },
      h('div', { class: 'grow name' },
        h('a', { href: mailHref('cover', sv.cover_id, ser.upstream),
                 title: 'Read the cover letter and its comments' },
          'Cover letter', sv.cover_name ? h('span', { class: 'desc' }, ` ${sv.cover_name}`)
                                        : null)),
      h('span', { class: 'ver' }, 'not reviewed')),
    strip,
    h('div', { class: 'actions' }, showBtn, commentsBtn, reviewBtn),
    reviewBox, mailBox, commentsBox, commentResponses(sv, 0).map(replyBox));
}

// Status of a reply to a review, as [text, chip class]
const REPLY_STATUS = {
  new: ['needs a reply', 'st-changes-requested'],
  suggested: ['response suggested', 'st-changes-requested'],
  skip: ['no reply needed', 'st-other'],
  draft: ['response drafted', 'st-under-review'],
  sent: ['response sent', 'st-accepted'],
};

// Start a response to a reply, quoting it
function quoteReply(rp) {
  const name = rp.sender.replace(/<.*>/, '').replace(/"/g, '').trim();
  const first = name.split(/\s+/)[0] || '';
  const quoted = rp.body.replace(/\s+$/, '').split('\n')
    .map((line) => (line ? `> ${line}` : '>')).join('\n');
  return `Hi ${first},\n\nOn ${rp.date}, ${rp.sender} wrote:\n${quoted}\n\n`;
}

// Show a reply to one of our reviews, with our response to it
function replyBox(rp) {
  const box = h('div', { class: 'comments' });
  const show = () => {
    const [label, cls] = REPLY_STATUS[rp.status] || [rp.status, 'st-other'];
    const waiting = rp.status === 'new' || rp.status === 'suggested';
    box.replaceChildren(
      h('div', { class: 'who' }, `↩ ${rp.sender.replace(/\s*<.*>/, '') || rp.sender}`,
        h('span', { class: 'when' }, ago(new Date(rp.date))), ' ',
        h('span', { class: `chip ${cls}` }, label)),
      renderMail(rp.body),
      rp.response && h('div', { class: 'section' }, 'Your response'),
      rp.response && h('div', { class: 'notes' }, renderMail(rp.response)),
      rp.status !== 'sent' && h('div', { class: 'actions' },
        h('button', { class: 'btn light small', onclick: edit,
                      title: 'Write your response to this reply yourself' },
          rp.response ? 'Edit response' : 'Write a response'),
        rp.response && confirmButton('Send', 'btn small',
                                     () => startJob(`replies/${rp.id}/send`, {}),
                                     'Send your response'),
        rp.response && confirmButton(
          rp.status === 'draft' ? 'Update Gmail draft' : 'Create Gmail draft',
          'btn light small', () => startJob(`replies/${rp.id}`, { draft: true }),
          'Put your response in a Gmail draft, to check or send from Gmail'),
        confirmButton('Suggest a response', 'btn light small',
                      () => startJob(`replies/${rp.id}/suggest`, {}),
                      'Have the agent suggest a response to this reply'),
        waiting && h('button', { class: 'btn light small',
                                 title: 'Mark this reply as needing no response',
                                 onclick: async () => {
          Object.assign(rp, await api(`replies/${rp.id}`, { json: { skip: true } }));
          show();
        } }, 'No reply needed')));
  };
  const edit = () => {
    const text = h('textarea', { spellcheck: 'true' });
    text.value = rp.response || quoteReply(rp);
    const draft = h('input', { type: 'checkbox', checked: true });
    const msg = h('div', { class: 'msg' });
    const save = h('button', { class: 'btn light small', title: 'Save your response',
                               onclick: async () => {
      save.disabled = true;
      msg.textContent = '';
      try {
        const res = await api(`replies/${rp.id}`,
                              { json: { response: text.value, draft: draft.checked } });
        if (res.kind) {
          location.hash = `#/jobs/${res.id}`;
        } else {
          Object.assign(rp, res);
          show();
        }
      } catch (err) {
        msg.textContent = err.message;
        save.disabled = false;
      }
    } }, 'Save');
    // Sending creates the Gmail draft from the saved response first
    const send = confirmButton('Save and send', 'btn small', async () => {
      msg.textContent = '';
      try {
        await api(`replies/${rp.id}`, { json: { response: text.value } });
      } catch (err) {
        msg.textContent = err.message;
        return;
      }
      startJob(`replies/${rp.id}/send`, {}, msg);
    }, 'Save your response and send it');
    box.replaceChildren(h('div', { class: 'who' }, `Response to ${rp.sender}`),
      h('div', { class: 'form' }, text,
        h('label', { class: 'check', title: 'Also put the response in a Gmail draft' }, draft,
          rp.status === 'draft' ? 'Update the Gmail draft' : 'Create the Gmail draft'),
        h('div', { class: 'actions' }, send, save,
          h('button', { class: 'btn light small', onclick: show,
                        title: 'Close the response without saving it' }, 'Cancel')),
        msg));
    text.focus();
  };
  show();
  return box;
}

// A form asking the agent to change a review, or part of one, or all of them.
// Calls submit(note, drafts, msg) when the user sends it
function reviseForm(part, drafted, cancel, submit) {
  const text = h('textarea', { class: 'note', spellcheck: 'true',
    placeholder: part ? "e.g. 'Drop this', 'Suggest a timeout instead'"
                      : "e.g. 'Approve it', 'Drop the comments about style'" });
  const box = h('input', { type: 'checkbox', checked: drafted });
  const msg = h('div', { class: 'msg' });
  const send = h('button', { class: 'btn small', onclick: async () => {
    send.disabled = true;
    msg.textContent = '';
    await submit(text.value, box.checked, msg);
    send.disabled = false;
  }, title: 'Ask the agent to make this change' }, 'Revise');
  const form = h('div', { class: 'form' },
    part && h('div', { class: 'hint' }, 'About this part:'),
    part && h('div', { class: 'notes revise-part' }, renderMail(part)),
    h('label', {}, 'What should change?'), text,
    h('label', { class: 'check', title: 'Also update the Gmail draft of the review' },
      box, 'Update the Gmail draft'),
    h('div', { class: 'actions' }, send,
      h('button', { class: 'btn light small', onclick: cancel,
                    title: 'Close this without changing anything' }, 'Cancel')), msg);
  setTimeout(() => text.focus(), 0);
  return form;
}

// Save the text of a review which has not been sent. If its Gmail draft is
// to be updated, show that job on the review's card ('target'), using
// refresh() to read the review when it finishes
async function saveReview(r, body, redraft, target, refresh) {
  const res = await api(`review-items/${r.id}`, { json: { body, redraft } });
  if (res.kind) {
    r.body = body;
    target.show();
    trackJob(res, 'Update draft', [target], refresh);
  } else {
    Object.assign(r, res);
    target.show();
  }
}

// A form to change one comment of a review, shown in its place.
// Calls submit(text, redraft) when the user saves it
function chunkForm(comment, drafted, cancel, submit) {
  // Allow for long lines wrapping on a narrow screen
  const rows = comment.split('\n').reduce((n, l) => n + Math.ceil((l.length + 1) / 60), 1);
  const text = h('textarea', { spellcheck: 'true', rows: Math.min(rows, 20) });
  text.value = comment;
  const box = h('input', { type: 'checkbox', checked: drafted });
  const msg = h('div', { class: 'msg' });
  const save = h('button', { class: 'btn small', onclick: async () => {
    save.disabled = true;
    msg.textContent = '';
    try {
      await submit(text.value, box.checked);
    } catch (err) {
      msg.textContent = err.message;
      save.disabled = false;
    }
  }, title: 'Save the changed comment' }, 'Save');
  const form = h('div', { class: 'form' }, text,
    h('label', { class: 'check', title: 'Also update the Gmail draft of the review' },
      box, 'Update the Gmail draft'),
    h('div', { class: 'hint' }, 'Leave it empty to drop the comment and its quote'),
    h('div', { class: 'actions' }, save,
      h('button', { class: 'btn light small', onclick: cancel,
                    title: 'Close this without changing the comment' }, 'Cancel')), msg);
  setTimeout(() => text.focus(), 0);
  return form;
}

// Change the text of a review which has not been sent
function editReview(card, r, header, show, target, refresh) {
  const text = h('textarea', { spellcheck: 'true' });
  text.value = r.body;
  const box = h('input', { type: 'checkbox', checked: hasDraft(r) });
  const msg = h('div', { class: 'msg' });
  const save = h('button', { class: 'btn small', onclick: async () => {
    save.disabled = true;
    msg.textContent = '';
    try {
      await saveReview(r, text.value, box.checked, target, refresh);
    } catch (err) {
      msg.textContent = err.message;
      save.disabled = false;
    }
  }, title: 'Save the changed review' }, 'Save');
  card.replaceChildren(header, h('div', { class: 'form' }, text,
    h('label', { class: 'check', title: 'Also update the Gmail draft of the review' },
      box, 'Update the Gmail draft'),
    h('div', { class: 'hint' }, "Include a 'Reviewed-by:' line to approve the patch"),
    h('div', { class: 'actions' }, save,
      h('button', { class: 'btn light small', onclick: show,
                    title: 'Close this without changing the review' }, 'Cancel')), msg));
  text.focus();
  text.setSelectionRange(0, 0);
  text.scrollTop = 0;
}

async function viewReviewForm() {
  const upstreams = await api('upstreams').catch(() => []);
  const field = (label, input, hint) => [h('label', {}, label), input,
                                         hint && h('div', { class: 'hint' }, hint)];
  const check = (label, checked, tip) => {
    const box = h('input', { type: 'checkbox', checked });
    return [box, h('label', { class: 'check', title: tip }, box, label)];
  };
  const target = h('input', { type: 'text', inputmode: 'url',
                              placeholder: 'e.g. 471234 or a patchwork URL' });
  const title = h('input', { type: 'text', placeholder: 'Words from the title' });
  const version = h('input', { type: 'number', min: 1, placeholder: 'Latest' });
  const index = h('input', { type: 'text', placeholder: 'All', inputmode: 'numeric' });
  const ups = h('select', { title: 'The upstream whose patchwork has the series' },
                upstreams.map((u) =>
    h('option', { value: u.name, selected: u.default }, `${u.name} (${u.patchwork_url})`)));
  const [drafts, draftsRow] = check('Create Gmail drafts', true,
                                    'Put each review in a Gmail draft, ready to send');
  const [whole, wholeRow] = check('Whole series, for a patch URL', false,
                                  'Review the whole series which the patch is part of');
  const [force, forceRow] = check('Review again if already reviewed', false,
                                  'Review the series even if you have reviewed it already');
  const context = h('textarea', { class: 'note', spellcheck: 'true',
                                  placeholder: 'e.g. this is an RFC, so ignore style' });
  const msg = h('div', { class: 'msg' });
  const submit = h('button', { class: 'btn', onclick: async () => {
    submit.disabled = true;
    msg.textContent = '';
    await startJob('review', {
      target: target.value, title: title.value, version: version.value,
      index: index.value, ups: ups.value, drafts: drafts.checked,
      whole: whole.checked, force: force.checked, context: context.value }, msg);
    submit.disabled = false;
  }, title: 'Start a job in which the agent reviews the series' }, 'Start review');
  setView('New review', 'reviews', true, h('div', { class: 'card form' },
    field('Patchwork series or patch', target,
          'A series ID, or the URL of a series or patch'),
    field('Or search by cover-letter title', title),
    field('Version (with a title)', version),
    field('Patches to review', index, 'e.g. 1,3-5'),
    upstreams.length > 1 && field('Upstream', ups),
    field('Context for the agent (optional)', context),
    draftsRow, wholeRow, forceRow,
    h('div', { class: 'actions' }, submit), msg));
}

// --- Inbox ---

// Pages of the inbox read so far, so 'Load more' can add to them
let inbox = null;

// Jobs reviewing series in the inbox, so each series can show how its review
// is getting on, even after the inbox is shown again. The server says which
// series are being reviewed, in case the review was started elsewhere
//   key: patchwork series ID
//   value: job number
const inboxJobs = new Map();

// Numbers of the jobs in inboxJobs which have finished
const inboxDone = new Set();

// Show the state of a series' review job in 'box', checking every few seconds
// until it finishes, while the box is still on the page
function trackInboxJob(box, jobId) {
  const poll = async () => {
    if (!box.isConnected) return;
    let st;
    try {
      st = await api(`jobs/${jobId}`);
    } catch (err) {
      box.textContent = err.message;
      return;
    }
    const [text, cls] = jobStatus(st);
    box.replaceChildren(h('a', { class: `chip ${cls}`, href: `#/jobs/${jobId}`,
                                 title: 'Show the review job and its output' },
                          text === 'ok' ? 'review done' : `review ${text}`));
    if (st.finished) {
      inboxDone.add(jobId);
      refreshJobCount();
    } else {
      setTimeout(poll, 3000);
    }
  };
  // Wait until the box is on the page
  setTimeout(poll, 0);
}

function inboxDetails(s, ups, jobBox) {
  const cover = s.cover_id && h('div', { class: 'row' },
    h('span', { class: 'seq' }, 0),
    h('div', { class: 'grow' },
      h('a', { href: mailHref('cover', s.cover_id, ups),
               title: 'Read the cover letter and its comments' }, 'Cover letter')));
  // While the series is being reviewed, link to the job instead
  const busyLink = (jobId) => h('a', { class: 'btn light small', href: `#/jobs/${jobId}`,
                                       title: 'Show the review job and its output' },
                                'Being reviewed…');
  const jobId = inboxJobs.get(s.id);
  // Stay on the inbox, so that more reviews can be started
  const [reviewBtn, reviewBox] = reviewButton(
    s.reviewed ? 'Review again' : 'Review', 'btn small',
    'Have the agent review this series, as a job; the inbox stays open',
    async (context) => {
      const job = await api('review', { json: { target: String(s.id), ups, drafts: true,
                                                force: !!s.reviewed, context } });
      inboxJobs.set(s.id, job.id);
      refreshJobCount();
      trackInboxJob(jobBox, job.id);
      reviewBtn.replaceWith(busyLink(job.id));
    });
  return h('div', { class: 'comments' },
    h('div', { class: 'list' }, cover, s.patches.map((p, i) => h('div', { class: 'row' },
      h('span', { class: 'seq' }, i + 1),
      h('div', { class: 'grow' },
        h('a', { href: mailHref('patch', p.id, ups),
                 title: 'Read the patch and its comments' }, p.name))))),
    h('div', { class: 'actions' },
      jobId && !inboxDone.has(jobId) ? busyLink(jobId) : reviewBtn,
      s.web_url && h('a', { class: 'btn light small', href: s.web_url,
                            target: '_blank', rel: 'noopener',
                            title: 'Open the series on patchwork, in a new tab' },
        'patchwork ↗')),
    reviewBox);
}

function inboxCard(s, ups, pw) {
  const jobBox = h('span', {});
  const card = h('div', { class: 'card patch', title: 'Tap to show the patches' },
    h('div', { class: 'row' },
      h('div', { class: 'grow name' }, s.name),
      s.version && h('div', { class: 'ver' }, `v${s.version}`)),
    h('div', { class: 'meta' },
      h('span', {}, s.submitter || ''),
      h('span', {}, ago(parseTime(s.date, true))),
      h('span', {}, npatches(s.total || s.patches.length)),
      s.reviewed && h('a', { class: 'chip st-accepted', href: `#/reviews/${s.reviewed}`,
                             title: 'Show your reviews of this series' }, 'reviewed'),
      s.earlier && h('a', { class: 'chip st-under-review', href: `#/reviews/${s.earlier}`,
                            title: 'Show your reviews of an earlier version' },
                     'earlier version reviewed'),
      s.mine && h('span', { class: 'chip st-other' }, 'yours'),
      !s.received_all && h('span', { class: 'chip st-changes-requested' }, 'incomplete'),
      jobBox));
  if (s.job) inboxJobs.set(s.id, s.job);
  if (inboxJobs.has(s.id)) trackInboxJob(jobBox, inboxJobs.get(s.id));
  let box = null;
  card.addEventListener('click', (ev) => {
    if (ev.target.closest('a, button, .comments')) return;
    if (box) { box.remove(); box = null; return; }
    box = inboxDetails(s, ups, jobBox);
    card.append(box);
  });
  const groups = ['all'];
  if (!s.reviewed && !s.mine) groups.push('todo');
  if (s.earlier) groups.push('newver');
  return { card, text: `${s.name} ${s.submitter || ''}`, groups };
}

async function viewInbox(more) {
  const upstreams = await api('upstreams').catch(() => []);
  let ups = null;
  try { ups = localStorage.getItem('patman-inbox-ups'); } catch (e) { /* ignore */ }
  if (!upstreams.some((u) => u.name === ups)) {
    ups = (upstreams.find((u) => u.default) || upstreams[0] || {}).name || '';
  }
  const pw = (upstreams.find((u) => u.name === ups) || {}).patchwork_url;
  if (!more || !inbox || inbox.ups !== ups) {
    inbox = { ups, page: 0, series: [], done: false };
  }
  const data = await api(`inbox?ups=${encodeURIComponent(ups)}&page=${inbox.page + 1}`);
  inbox.page = data.page;
  inbox.series.push(...data.series);
  inbox.done = data.series.length < data.per_page;

  const upsTabs = upstreams.length > 1 && h('div', { class: 'tabs' },
    upstreams.map((u) => h('button', { class: u.name === ups ? 'on' : '',
                                       title: `Show the series on ${u.name}'s patchwork`,
                                       onclick: () => {
      try { localStorage.setItem('patman-inbox-ups', u.name); } catch (e) { /* ignore */ }
      route();
    } }, u.name)));
  const loadMore = !inbox.done && h('div', { class: 'actions' },
    h('button', { class: 'btn light', title: 'Read more series from patchwork',
                  onclick: (ev) => {
      ev.target.disabled = true;
      viewInbox(true).catch((err) => showError(err, 'inbox'));
    } }, 'Load more'));
  setView('Inbox', 'inbox', false, upsTabs,
    ...filteredList('inbox', inbox.series.map((s) => inboxCard(s, ups, pw)),
                    [['todo', 'To review',
                      'Series which you have not reviewed and are not yours'],
                     ['newver', 'New versions', 'New versions of series you reviewed earlier'],
                     ['all', 'All', 'All the series listed']]),
    loadMore);
}

// --- Todo ---

async function viewTodo() {
  const list = await api('todo');
  if (!list.length) {
    return setView('Todo', 'todo', false, h('div', { class: 'empty' }, 'Nothing scheduled'));
  }
  const due = list.filter((t) => t.due);
  const later = list.filter((t) => !t.due);
  const card = (t) => h('a', { class: 'card', href: `#/series/${t.series_id}`,
                               title: 'Show this series' },
    h('div', { class: 'row' },
      h('div', { class: 'grow name' }, t.name),
      h('span', { class: `chip ${t.due ? 'st-changes-requested' : 'st-other'}` },
        t.due ? `due ${ago(parseTime(t.timestamp))}`.replace('due just now', 'due now')
              : ago(parseTime(t.timestamp)))),
    t.desc && h('div', { class: 'desc' }, t.desc));
  setView('Todo', 'todo', false,
    due.length ? h('div', { class: 'section' }, 'Due') : null,
    due.length ? h('div', { class: 'list' }, due.map(card)) : null,
    later.length ? h('div', { class: 'section' }, 'Scheduled') : null,
    later.length ? h('div', { class: 'list' }, later.map(card)) : null);
}

// --- Sync ---

const syncBtn = document.getElementById('sync');
const syncText = document.getElementById('sync-text');
let syncState = null;
let syncTimer = null;

function syncOk(state) {
  return !state.cancelled && state.results.every((r) => r.returncode === 0);
}

// Show the number of jobs which are waiting or running on the Jobs tab,
// checking again every few seconds while there are any
let jobCountTimer = null;
async function refreshJobCount() {
  clearTimeout(jobCountTimer);
  let jobs;
  try {
    jobs = await api('jobs');
  } catch (err) {
    return;
  }
  const count = jobs.filter((job) => !job.finished).length;
  const tab = document.querySelector('nav a[data-tab="jobs"]');
  let badge = tab.querySelector('.badge');
  if (!badge) {
    badge = h('span', { class: 'badge' });
    tab.append(badge);
  }
  badge.textContent = count || '';
  if (count) jobCountTimer = setTimeout(refreshJobCount, 3000);
}

async function refreshSync() {
  const wasRunning = syncState && syncState.running;
  refreshJobCount();
  try {
    syncState = await api('sync');
  } catch (err) {
    syncBtn.className = 'bad';
    syncText.textContent = 'offline';
    return;
  }
  const st = syncState;
  syncBtn.className = st.running ? 'running' : !st.finished ? '' :
                      syncOk(st) ? 'ok' : 'bad';
  syncText.textContent = st.running ? 'syncing' :
    st.finished ? ago(parseTime(st.finished)) : 'sync';
  clearTimeout(syncTimer);
  syncTimer = setTimeout(refreshSync, st.running ? 1500 : 60000);
  if (location.hash === '#/sync') viewSync();
  else if (wasRunning && !st.running) route();
}

async function startSync() {
  await api('sync', { method: 'POST' });
  await refreshSync();
}

async function cancelSync() {
  await api('sync/cancel', { method: 'POST' });
  await refreshSync();
}

// Describe how a command in a job ended
function cmdStatus(r, st) {
  if (r.returncode === null) return 'running…';
  if (r.returncode === 0) return 'ok';
  if (st.cancelled && r === st.results[st.results.length - 1]) return 'cancelled';
  return 'exit ' + r.returncode;
}

// Get the status of a job as [text, chip class]
function jobStatus(st) {
  if (st.queued) return st.cancelled ? ['cancelled', 'st-other'] : ['waiting', 'st-other'];
  if (st.running) return ['running', 'st-under-review'];
  if (st.cancelled) return ['cancelled', 'st-changes-requested'];
  return st.results.every((r) => r.returncode === 0) ? ['ok', 'st-accepted']
                                                    : ['failed', 'st-rejected'];
}

// Render the output of each command in a job, following the one running.
// The page is redrawn every few seconds while the job runs, so the returned
// function puts the page and each log back where the user left them, and
// keeps a log scrolled to its end there as more output arrives
function jobOutput(st) {
  const key = (i) => `${location.hash}:${i}`;
  const old = new Map([...main.querySelectorAll('.log')].map((log) => [log.dataset.key, {
    top: log.scrollTop,
    end: log.scrollTop + log.clientHeight >= log.scrollHeight - 4 }]));
  const pageY = old.size ? window.scrollY : null;
  const logs = st.results.map((r, i) =>
    h('div', { class: 'log mail', 'data-key': key(i) }, r.output.trim() || '(no output yet)'));
  const follow = () => {
    if (pageY !== null) window.scrollTo(0, pageY);
    logs.forEach((log, i) => {
      const prev = old.get(key(i));
      if (prev && !prev.end) log.scrollTop = prev.top;
      else if (prev || (st.running && i === logs.length - 1)) log.scrollTop = log.scrollHeight;
    });
  };
  return [st.results.flatMap((r, i) => [
    h('div', { class: 'section cmd' }, `${r.cmd} — ${cmdStatus(r, st)}`), logs[i]]), follow];
}

// Show a number of bytes in a short form, e.g. '1.2 MB'
function fmtBytes(n) {
  if (n < 1000) return `${n} B`;
  if (n < 1e6) return `${(n / 1e3).toFixed(1)} KB`;
  return `${(n / 1e6).toFixed(1)} MB`;
}

// Show the requests which patman has sent to each patchwork server recently,
// so that it is easy to see whether it is sending too many
function trafficCard(list) {
  const periods = [['hour', 'This hour'], ['today', 'Today'], ['week', 'Last 7 days']];
  return h('div', { class: 'card' },
    h('div', { class: 'name' }, 'Patchwork traffic'),
    list.length ? list.map((item) => h('div', {},
      h('div', { class: 'meta', title: 'Patchwork server' }, item.url),
      h('div', { class: 'traffic',
                 title: 'Requests sent to the server and data received from it' },
        periods.flatMap(([key, label]) => [
          h('span', { class: 'label' }, label),
          h('span', {}, `${plural(item[key][0], 'request')}, ` +
                        fmtBytes(item[key][1]))]))))
      : h('div', { class: 'meta' }, 'No requests in the last 7 days'));
}

function viewSync() {
  const st = syncState || { results: [] };
  const active = st.running || st.queued;
  // Without a periodic sync, a sync is unusual, so make sure it is wanted
  const tip = `Gather the review tags for all ${st.num_series} series ` +
              'from patchwork now';
  const btn = active || st.interval
    ? h('button', { class: 'btn', disabled: active, title: tip,
                    onclick: () => { btn.disabled = true; startSync(); } },
        st.queued ? 'Waiting…' : st.running ? 'Syncing…' : 'Sync now')
    : confirmButton('Sync now', 'btn', startSync,
                    `${tip}, which sends a lot of requests; to sync one series, use ` +
                    'the Sync button on its page');
  const cancel = active && h('button', { class: 'btn danger', title: 'Stop the sync',
      onclick: () => { cancel.disabled = true; cancelSync(); } }, 'Cancel');
  const [output, follow] = jobOutput(st);
  setView('Sync', null, true,
    h('div', { class: 'card' },
      h('div', { class: 'meta' },
        h('span', {}, st.interval ? `Every ${st.interval} min` : 'Periodic sync off'),
        st.num_series !== undefined && h('span', {}, `${st.num_series} series`),
        st.review_sync && h('span', {}, 'with review --sync'),
        st.started && h('span', {}, `last started ${ago(parseTime(st.started))}`),
        st.cancelled && h('span', {}, 'cancelled')),
      h('div', { class: 'actions' }, btn, cancel)),
    st.traffic && trafficCard(st.traffic),
    output);
  follow();
}

let jobTimer = null;
function pollJobs(hash, active) {
  clearTimeout(jobTimer);
  if (active) {
    jobTimer = setTimeout(() => { if (location.hash === hash) route(); }, 1500);
  }
}

async function viewJobs() {
  const jobs = await api('jobs');
  pollJobs('#/jobs', jobs.some((j) => !j.finished));
  if (!jobs.length) {
    return setView('Jobs', 'jobs', false, reviewActions(),
                   h('div', { class: 'empty' }, 'No jobs since the server started'));
  }
  setView('Jobs', 'jobs', false, reviewActions(), h('div', { class: 'list' },
    jobs.map((j) => {
      const [text, cls] = jobStatus(j);
      return h('a', { class: 'card', href: `#/jobs/${j.id}`,
                      title: 'Show the job and its output' },
        h('div', { class: 'row' },
          h('div', { class: 'grow name' }, j.title),
          h('span', { class: `chip ${cls}` }, text)),
        h('div', { class: 'meta' },
          h('span', {}, `#${j.id}`),
          j.resumed > 0 && h('span', {}, 'resumed after a restart'),
          j.started && h('span', {}, `started ${ago(parseTime(j.started))}`),
          j.finished && h('span', {}, `finished ${ago(parseTime(j.finished))}`)));
    })));
}

async function viewJob(id) {
  const st = await api(`jobs/${id}`);
  const active = !st.finished;
  pollJobs(`#/jobs/${id}`, active);
  const [text, cls] = jobStatus(st);
  const cancel = active && h('button', { class: 'btn danger', title: 'Stop this job',
                                         onclick: async () => {
    cancel.disabled = true;
    await api(`jobs/${id}/cancel`, { method: 'POST' });
    route();
  } }, 'Cancel');
  const [output, follow] = jobOutput(st);
  setView(st.title, 'jobs', true,
    h('div', { class: 'card' },
      h('div', { class: 'row' },
        h('div', { class: 'grow name' }, st.title),
        h('span', { class: `chip ${cls}` }, text)),
      h('div', { class: 'meta' },
        st.started && h('span', {}, `started ${ago(parseTime(st.started))}`),
        st.finished && h('span', {}, `finished ${ago(parseTime(st.finished))}`)),
      cancel && h('div', { class: 'actions' }, cancel)),
    output);
  follow();
}
syncBtn.addEventListener('click', () => { location.hash = '#/sync'; });

// --- Routing ---

async function route() {
  const parts = (location.hash.replace(/^#\/?/, '') || 'series').split('/');
  const [page, id, ver] = parts;
  const tab = page === 'sync' ? null : page;
  try {
    if (page === 'series' && id) await viewSeries(+id, ver ? +ver : null);
    else if (page === 'reviews' && id) await viewReview(+id, ver ? +ver : null);
    else if (page === 'review' && id === 'new') await viewReviewForm();
    else if (page === 'inbox') await viewInbox();
    else if ((page === 'patch' || page === 'cover') && id) {
      await viewMail(page, +id, ver ? decodeURIComponent(ver) : null);
    }
    else if (page === 'file' && parts.length === 6) {
      await viewFile(parts[1], parts[2], +parts[3], +parts[4],
                     decodeURIComponent(parts[5]));
    }
    else if (page === 'jobs' && id) await viewJob(+id);
    else if (page === 'jobs') await viewJobs();
    else if (page === 'reviews') await viewReviewList();
    else if (page === 'todo') await viewTodo();
    else if (page === 'sync') viewSync();
    else await viewSeriesList();
  } catch (err) {
    showError(err, tab);
  }
}

// Tapping a tab goes back to the screen last shown in it; tapping the tab
// being shown goes to its top level
for (const a of document.querySelectorAll('nav a')) {
  a.addEventListener('click', (event) => {
    const tab = a.dataset.tab;
    const last = tabScreens.get(tab);
    if (tab !== currentTab && last && last !== location.hash) {
      event.preventDefault();
      location.hash = last;
    }
  });
}

// Remember where each screen is scrolled to, and go back there on returning
let scrollSave = null;
window.addEventListener('scroll', () => {
  clearTimeout(scrollSave);
  scrollSave = setTimeout(() => {
    scrollPlaces.delete(location.hash);
    scrollPlaces.set(location.hash, window.scrollY);
    savePlaces('patman-scroll', scrollPlaces);
  }, 200);
});

window.addEventListener('hashchange', async () => {
  clearTimeout(scrollSave);
  window.scrollTo(0, 0);
  await route();
  const y = scrollPlaces.get(location.hash);
  if (y) window.scrollTo(0, y);
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { refreshSync(); }
});
route();
refreshSync();
