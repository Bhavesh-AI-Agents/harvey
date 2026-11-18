// Discover > People: find people through SalesQL.
//
// Left: the audience (who to look for). Every list has an include side and an
// exclude side. Right: the people a search found, sorted into Qualified,
// Unsure, Rejected and Revealed. Searching is free; revealing spends credits
// and only happens for people ticked here.
//
// Plain functions and string templates, like app.js. State lives in SQ.

const SQ = {
  data: null,        // last /api/salesql answer
  aud: null,         // the audience being edited (a working copy)
  audId: '',         // id of the saved audience it came from ('' = not saved yet)
  dirty: false,
  open: new Set(['job_titles']),
  tab: 'qualified',
  picks: new Set(),
  catalog: null,     // {lists: {kind: {values, complete}}, collecting, note}
  browse: {},        // field -> text typed in its list filter
  offered: {},       // field -> names SalesQL offered for typed text
  busy: '',          // 'search' | 'reveal' | 'save' | ''
  notes: [],
  describe: '', describeNote: '',
  timer: null, catalogTimer: null,
};

// The audience form, top to bottom. kind: SalesQL list the names come from.
const SQ_FIELDS = [
  {key: 'job_titles', group: 'Person', label: 'Job title', hint: 'Type a title and press Enter',
   help: 'SalesQL has no fixed list of titles. It finds people whose title contains your words, so "Owner" also finds "Co-Owner".'},
  {key: 'past_titles', group: 'Person', label: 'Past job title', hint: 'A title they held before',
   help: 'Include: only people who once held such a title. Exclude: leave out anyone who ever did.'},
  {key: 'profile_words', group: 'Person', label: 'Words in title or headline', hint: 'A word or phrase',
   help: 'Checked by Harvey against each person\'s title and headline after the search.'},
  {key: 'countries', group: 'Location', label: 'Country', kind: 'country'},
  {key: 'states', group: 'Location', label: 'State or region', kind: 'region',
   help: 'SalesQL lists states and regions, not cities. Both the person and the company must be there.'},
  {key: 'cities', group: 'Location', label: 'City', hint: 'Boston',
   help: 'Checked by Harvey against each person\'s location. Pick the state too, so the search is narrowed first.'},
  {key: 'industries', group: 'Company', label: 'Industry', kind: 'industry'},
  {key: 'company_types', group: 'Company', label: 'Company type', kind: 'company_type'},
  {key: 'company_keywords', group: 'Company', label: 'Words in company name', hint: 'merchant services',
   help: 'Include: Harvey finds companies with these words in their name first, then their people (used in place of Industry). Exclude: checked after the search.'},
];
const SQ_TITLE_IDEAS = ['Founder', 'Co-Founder', 'Owner', 'Co-Owner', 'CEO', 'President',
  'Managing Director', 'Managing Partner', 'Partner', 'Principal', 'Proprietor', 'General Manager'];
const SQ_DEV_TITLES = ['Software Engineer', 'Software Developer', 'Developer', 'Programmer', 'CTO',
  'Engineering Manager', 'DevOps Engineer', 'QA Engineer', 'Web Developer', 'Full Stack Developer'];
const SQ_RANGES = [
  ['experience', 'Years of experience'], ['in_role', 'Years in current job'],
  ['founded', 'Company founded (year)'],
];
const SQ_FLAGS = [
  ['has_phone', 'Has a phone number'], ['has_work_phone', 'Has a work phone number'],
  ['has_verified_email', 'Has an email SalesQL verified'],
];
const SQ_TABS = [['qualified', 'Qualified'], ['unsure', 'Unsure'], ['rejected', 'Rejected'],
  ['done', 'Done']];

const sqEl = id => document.getElementById(id);
const sqPost = (path, body) => api(path, {
  method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
});

// ── Discover's two views ──

function showDiscover(view) {
  if (view !== 'people') view = 'biz';
  try { sessionStorage.setItem('harvey-discover-view', view); } catch { /* private window */ }
  const biz = sqEl('discover-biz'), people = sqEl('discover-people');
  if (!biz || !people) return;
  biz.hidden = view !== 'biz';
  people.hidden = view !== 'people';
  [['biz', 'disc-seg-biz'], ['people', 'disc-seg-people']].forEach(([v, id]) => {
    const btn = sqEl(id);
    if (btn) { btn.classList.toggle('on', v === view); btn.setAttribute('aria-selected', v === view); }
  });
  if (view === 'people') loadSalesql();
}

function sqInitialView() {
  let view = 'biz';
  try { view = sessionStorage.getItem('harvey-discover-view') || 'biz'; } catch { /* ignore */ }
  showDiscover(view);
}

// ── Loading ──

async function loadSalesql() {
  const root = sqEl('salesql-root');
  if (!root) return;
  const people = sqEl('discover-people');
  if (people && people.hidden) { if (!SQ.data) sqInitialView(); return; }
  const data = await api('/api/salesql');
  if (!data) { root.innerHTML = offlineState(); return; }
  SQ.data = data;
  if (!data.connected) { root.innerHTML = sqConnectCard(data); return; }
  const shown = data.audiences || {audience: null, active: '', items: [], saved: false};
  // Keep what is being edited across a reload, unless a different audience is now active.
  if (!SQ.aud || !SQ.dirty || SQ.audId !== shown.active) {
    SQ.aud = JSON.parse(JSON.stringify(shown.audience || sqBlankAudience()));
    SQ.audId = shown.active || '';
    SQ.dirty = false;
  }
  const known = new Set((data.candidates || []).map(r => r.id));
  SQ.picks = new Set([...SQ.picks].filter(id => known.has(id)));
  sqRender();
  if (!SQ.catalog) sqLoadCatalog();
}

function sqBlankAudience() {
  const aud = {name: 'My audience', company_sizes: [], staff: [], page_size: 100};
  SQ_FIELDS.forEach(f => { aud[f.key] = {include: [], exclude: []}; });
  SQ_FLAGS.forEach(([k]) => { aud[k] = false; });
  SQ_RANGES.forEach(([k]) => { aud[k] = {min: null, max: null}; });
  return aud;
}

async function sqLoadCatalog() {
  clearTimeout(SQ.catalogTimer);
  const data = await api('/api/salesql/catalog');
  if (!data) return;
  SQ.catalog = data;
  SQ_FIELDS.filter(f => f.kind && SQ.open.has(f.key)).forEach(f => sqRenderBrowse(f.key));
  const people = sqEl('discover-people');
  if (data.collecting && people && !people.hidden) SQ.catalogTimer = setTimeout(sqLoadCatalog, 6000);
}

// ── Whole screen ──

function sqConnectCard(data) {
  return '<div class="card sq-connect"><h3>Find people through SalesQL</h3>' +
    '<p class="desc">Search by job title, location, company size and industry, and check who a company ' +
    'employs. Searching is free. Revealing a contact spends one SalesQL credit, and only for people you tick.</p>' +
    '<div class="form-group"><label class="form-label" for="salesql-key">SalesQL MCP key</label>' +
    '<input class="form-input" id="salesql-key" type="password" autocomplete="off" placeholder="Paste the key"></div>' +
    '<p class="desc">In SalesQL: Connectors, then MCP, then New MCP key. Tick Account, Search, Enrich and Verify.</p>' +
    '<div class="btn-group"><button class="btn btn-primary" onclick="sqConnect()">Connect</button>' +
    '<a class="btn btn-secondary" href="' + safeHref(data.key_url) + '" target="_blank" rel="noopener noreferrer">Open SalesQL</a></div></div>';
}

function sqRender() {
  const root = sqEl('salesql-root');
  if (!root || !SQ.data) return;
  root.innerHTML = sqAccountBar() + sqDescribeCard() +
    '<div class="sq-grid"><aside class="sq-panel" id="sq-panel">' + sqPanel() + '</aside>' +
    '<section class="sq-results" id="sq-results">' + sqResults() + '</section></div>';
}

function sqAccountBar() {
  const d = SQ.data, a = d.account;
  const stat = (label, value) => '<div class="sq-stat"><span class="sq-stat-label">' + label +
    '</span><span class="sq-stat-value">' + value + '</span></div>';
  return '<div class="card sq-account">' +
    (a ? stat('Plan', escHtml(a.plan || 'SalesQL')) +
         stat('Credits left', Number(a.credits).toLocaleString()) +
         stat('Credits refresh', escHtml(a.credits_refresh_date || 'unknown'))
       : stat('SalesQL', escHtml(d.error || 'Could not read the account'))) +
    stat('Reveals left today', d.left_today + ' of ' + d.cap) +
    '<div class="sq-stat sq-cap"><label class="sq-stat-label" for="sq-cap">Daily reveal cap</label>' +
    '<span><input class="form-input" id="sq-cap" type="number" min="0" max="1000" value="' + d.cap + '">' +
    '<button class="btn btn-secondary btn-sm" onclick="sqSaveCap()">Save</button></span></div></div>';
}

// ── Describe it in words ──

function sqDescribeCard() {
  return '<div class="card sq-describe"><label class="sq-panel-title" for="sq-describe">Describe who you want to find</label>' +
    '<p class="desc">Write it in your own words and Harvey\'s model fills in the filters below. ' +
    'Nothing is saved or searched until you have looked them over.</p>' +
    '<textarea class="form-input" id="sq-describe" rows="3" maxlength="1500" placeholder="' +
    'Owners and founders of small non-software businesses in California, 1 to 10 employees, with no developers on staff. Not agencies or consultants.' +
    '" oninput="SQ.describe = this.value">' + escHtml(SQ.describe || '') + '</textarea>' +
    '<div class="sq-row" style="margin-top:10px"><button class="btn btn-primary" id="sq-describe-btn" onclick="sqDescribe()"' +
    (SQ.busy ? ' disabled' : '') + '>' + (SQ.busy === 'describe' ? 'Working it out…' : 'Set the filters') + '</button>' +
    '<span class="muted" id="sq-describe-note">' + escHtml(SQ.describeNote || '') + '</span></div></div>';
}

async function sqDescribe() {
  const text = (SQ.describe || '').trim();
  if (SQ.busy) return;
  if (text.length < 8) { showToast('Describe who you want to find in a sentence or two.', 'error'); return; }
  if (SQ.dirty && !confirm('Leave the changes you have not saved?')) return;
  SQ.busy = 'describe';
  SQ.describeNote = 'Usually under a minute. A model running on your own machine can take a few minutes.';
  sqRender();
  const data = await sqPost('/api/salesql/describe', {text: text});
  SQ.busy = '';
  SQ.describeNote = '';
  if (!data || !data.success) {
    showToast((data && data.message) || 'Could not set the filters.', 'error');
    sqRender();
    return;
  }
  // A description starts a new audience with its own, empty list. The audience
  // that was open stays as it was, with the people it found.
  SQ.aud = data.audience;
  SQ.audId = '';
  SQ.dirty = true;
  SQ.picks = new Set(); SQ.tab = 'qualified';
  SQ.notes = data.notes || [];
  // Open every part the description filled in, so it can be checked at a glance.
  SQ.open = new Set(SQ_FIELDS.filter(f => data.audience[f.key].include.length || data.audience[f.key].exclude.length)
    .map(f => f.key));
  if (data.audience.company_sizes.length) SQ.open.add('sizes');
  if (data.audience.staff.length) SQ.open.add('staff');
  if (SQ_FLAGS.some(([k]) => data.audience[k]) ||
      SQ_RANGES.some(([k]) => data.audience[k].min !== null || data.audience[k].max !== null)) SQ.open.add('more');
  SQ.describeNote = 'A new audience was made from your description. Check its filters, then press Save and search.';
  sqRender();
  showToast('New audience made from your description. Check its filters, then press Save and search.', 'success');
}

// ── Audience panel ──

function sqPanel() {
  const shown = SQ.data.audiences || {items: []};
  const items = shown.items || [];
  let html = '<div class="sq-panel-head"><div class="sq-panel-title">Audience</div>' +
    '<p class="desc">Who to look for. Every list has an include side and an exclude side.</p>';
  if (items.length) {
    html += '<div class="sq-row"><select class="form-input" id="sq-aud-select" aria-label="Saved audiences" onchange="sqSelectAudience(this.value)">' +
      items.map(i => '<option value="' + escHtml(i.id) + '"' + (i.id === SQ.audId ? ' selected' : '') + '>' +
        escHtml(i.name) + '</option>').join('') +
      (SQ.audId ? '' : '<option value="" selected>New audience (not saved)</option>') + '</select>' +
      '<button class="btn btn-secondary btn-sm" onclick="sqNewAudience()" title="Start a new, empty audience with its own list of people">New</button>' +
      (SQ.audId ? '<button class="btn btn-danger btn-sm" onclick="sqDeleteAudience()">Delete</button>' : '') + '</div>';
  }
  html += '<label class="form-label" for="sq-name" style="margin-top:10px">Name</label>' +
    '<input class="form-input" id="sq-name" type="text" maxlength="60" value="' + escHtml(SQ.aud.name) +
    '" oninput="sqSet(\'name\', this.value)"></div>';
  if (SQ.notes.length) {
    html += '<div class="banner wait sq-notes"><div><p><b>Worth a look:</b></p>' +
      SQ.notes.map(n => '<p>' + escHtml(n) + '</p>').join('') + '</div></div>';
  }

  let group = '';
  SQ_FIELDS.forEach(f => {
    if (f.group !== group) { group = f.group; html += '<div class="sq-group">' + group + '</div>'; }
    html += '<div id="sq-f-' + f.key + '">' + sqField(f) + '</div>';
    if (f.key === 'company_keywords') html += '<div id="sq-f-sizes">' + sqSizes() + '</div>';
  });
  html += '<div id="sq-f-staff">' + sqStaff() + '</div>';
  html += '<div class="sq-group">More</div><div id="sq-f-more">' + sqMore() + '</div>';
  html += '<div class="sq-save" id="sq-save">' + sqSaveBar() + '</div>';
  return html;
}

function sqCount(pair) {
  const a = pair.include.length, b = pair.exclude.length;
  if (!a && !b) return '<span class="sq-count none">Any</span>';
  return (a ? '<span class="sq-count in">' + a + ' in</span>' : '') +
    (b ? '<span class="sq-count out">' + b + ' out</span>' : '');
}

function sqChips(key, side) {
  const names = SQ.aud[key][side];
  return names.map((name, i) => '<span class="sq-chip ' + (side === 'include' ? 'in' : 'out') + '">' +
    escHtml(name) + '<button type="button" aria-label="Remove ' + escHtml(name) + '" onclick="sqDrop(\'' + key +
    '\',\'' + side + '\',' + i + ')">&times;</button></span>').join('');
}

function sqZone(f, side) {
  const typed = !f.kind;
  return '<div class="sq-zone ' + (side === 'include' ? 'in' : 'out') + '">' +
    '<div class="sq-zone-label">' + (side === 'include' ? 'Include' : 'Exclude') + '</div>' +
    '<div class="sq-chips" id="sq-chips-' + f.key + '-' + side + '">' + sqChips(f.key, side) + '</div>' +
    (typed ? '<input class="form-input sq-add" type="text" autocomplete="off" placeholder="' + escHtml(f.hint || '') +
      '" aria-label="' + escHtml(f.label + ', ' + side) + '" onkeydown="sqTypeKey(event,\'' + f.key + '\',\'' + side +
      '\')" onblur="sqTypeAdd(this,\'' + f.key + '\',\'' + side + '\')">' : '') + '</div>';
}

function sqField(f) {
  const open = SQ.open.has(f.key);
  let html = '<details class="sq-field"' + (open ? ' open' : '') + ' ontoggle="sqToggle(\'' + f.key + '\', this.open)">' +
    '<summary><span class="sq-field-name">' + f.label + '</span><span class="sq-counts" id="sq-count-' + f.key + '">' +
    sqCount(SQ.aud[f.key]) + '</span></summary><div class="sq-field-body">';
  if (f.help) html += '<p class="desc">' + f.help + '</p>';
  html += sqZone(f, 'include') + sqZone(f, 'exclude');
  if (f.key === 'job_titles') html += sqTitleIdeas();
  if (f.kind) {
    html += '<input class="form-input sq-browse-filter" type="text" autocomplete="off" placeholder="Type to narrow SalesQL\'s list" ' +
      'aria-label="Narrow the ' + escHtml(f.label) + ' list" value="' + escHtml(SQ.browse[f.key] || '') +
      '" oninput="sqBrowseType(\'' + f.key + '\', this.value)">' +
      '<div class="sq-browse" id="sq-browse-' + f.key + '">' + sqBrowse(f) + '</div>';
  }
  return html + '</div></details>';
}

function sqTitleIdeas() {
  const have = new Set(SQ.aud.job_titles.include.concat(SQ.aud.job_titles.exclude).map(v => v.toLowerCase()));
  const seen = (SQ.data.titles_seen || []).filter(t => !SQ_TITLE_IDEAS.includes(t)).slice(0, 14);
  const tag = name => have.has(name.toLowerCase()) ? '' :
    '<button type="button" class="sq-idea" data-v="' + escHtml(name) + '" onclick="sqAdd(\'job_titles\',\'include\',this.dataset.v)">+ ' +
    escHtml(name) + '</button>';
  return '<div class="sq-ideas"><span class="sq-ideas-label">Common titles</span>' + SQ_TITLE_IDEAS.map(tag).join('') +
    (seen.length ? '<span class="sq-ideas-label">Seen on people your searches found</span>' + seen.map(tag).join('') : '') + '</div>';
}

function sqBrowse(f) {
  const list = SQ.catalog && SQ.catalog.lists ? SQ.catalog.lists[f.kind] : null;
  if (!list) return '<p class="muted">Loading SalesQL\'s list…</p>';
  const q = (SQ.browse[f.key] || '').trim().toLowerCase();
  const pair = SQ.aud[f.key];
  const names = list.values.filter(n => !q || n.toLowerCase().includes(q)).slice(0, 300);
  const offered = (SQ.offered[f.key] || []).filter(n => !names.includes(n));
  const row = name => {
    const inc = pair.include.includes(name), exc = pair.exclude.includes(name);
    return '<div class="sq-opt' + (inc ? ' in' : exc ? ' out' : '') + '"><span>' + escHtml(name) + '</span>' +
      '<button type="button" class="sq-pm in' + (inc ? ' on' : '') + '" data-v="' + escHtml(name) + '" title="Include" aria-label="Include ' +
      escHtml(name) + '" onclick="sqPick(\'' + f.key + '\',\'include\',this.dataset.v)">+</button>' +
      '<button type="button" class="sq-pm out' + (exc ? ' on' : '') + '" data-v="' + escHtml(name) + '" title="Exclude" aria-label="Exclude ' +
      escHtml(name) + '" onclick="sqPick(\'' + f.key + '\',\'exclude\',this.dataset.v)">&minus;</button></div>';
  };
  let html = names.concat(offered).map(row).join('');
  if (!html) html = '<p class="muted">' + (q ? 'Nothing in SalesQL\'s list matches that.' : 'The list is empty so far.') + '</p>';
  let status = list.values.length + ' from SalesQL';
  if (!list.complete) {
    status += SQ.catalog.collecting ? ' so far, still collecting'
      : ', not complete yet' + (SQ.catalog.note ? ' (' + SQ.catalog.note + ')' : '');
  }
  return html + '<p class="sq-browse-status">' + escHtml(status) + '.</p>';
}

function sqSizes() {
  const sizes = SQ.data.sizes || [];
  const chosen = SQ.aud.company_sizes;
  return '<details class="sq-field"' + (SQ.open.has('sizes') ? ' open' : '') + ' ontoggle="sqToggle(\'sizes\', this.open)">' +
    '<summary><span class="sq-field-name">Company size</span><span class="sq-counts">' +
    (chosen.length ? '<span class="sq-count in">' + escHtml(chosen.join(', ')) + '</span>' : '<span class="sq-count none">Any</span>') +
    '</span></summary><div class="sq-field-body"><p class="desc">Employees. Unticked sizes are left out.</p><div class="sq-sizes">' +
    sizes.map(size => '<label><input type="checkbox" value="' + escHtml(size) + '"' + (chosen.includes(size) ? ' checked' : '') +
      ' onchange="sqSize(this.value, this.checked)"> ' + escHtml(size) + '</label>').join('') + '</div></div></details>';
}

function sqStaff() {
  const staff = SQ.aud.staff;
  let html = '<details class="sq-field"' + (SQ.open.has('staff') ? ' open' : '') + ' ontoggle="sqToggle(\'staff\', this.open)">' +
    '<summary><span class="sq-field-name">Who the company employs</span><span class="sq-counts">' +
    (staff.length ? '<span class="sq-count in">' + staff.length + (staff.length === 1 ? ' check' : ' checks') + '</span>'
                  : '<span class="sq-count none">Not checked</span>') + '</span></summary><div class="sq-field-body">' +
    '<p class="desc">Harvey looks up each company\'s staff by job title. Each check is one extra free search per ten companies.</p>';
  staff.forEach((c, i) => {
    html += '<div class="sq-staff"><div class="sq-row"><select class="form-input" aria-label="Must have" onchange="sqStaffSet(' + i + ',\'must\',this.value)">' +
      '<option value="none"' + (c.must === 'none' ? ' selected' : '') + '>Exclude companies that have</option>' +
      '<option value="some"' + (c.must === 'some' ? ' selected' : '') + '>Include only companies that have</option></select>' +
      '<button class="btn btn-secondary btn-sm" onclick="sqStaffRemove(' + i + ')">Remove</button></div>' +
      '<label class="form-label" for="sq-staff-label-' + i + '">People called (one word, shown in the reason)</label>' +
      '<input class="form-input" id="sq-staff-label-' + i + '" type="text" maxlength="30" value="' + escHtml(c.label) +
      '" oninput="sqStaffSet(' + i + ',\'label\',this.value)">' +
      '<label class="form-label" for="sq-staff-titles-' + i + '" style="margin-top:8px">With any of these job titles (commas between)</label>' +
      '<input class="form-input" id="sq-staff-titles-' + i + '" type="text" value="' + escHtml(c.titles.join(', ')) +
      '" oninput="sqStaffSet(' + i + ',\'titles\',this.value)"></div>';
  });
  html += '<div class="btn-group" style="margin-top:8px"><button class="btn btn-secondary btn-sm" onclick="sqStaffAdd(false)">Add a check</button>' +
    (staff.some(c => c.label === 'software') ? '' :
      '<button class="btn btn-secondary btn-sm" onclick="sqStaffAdd(true)">No software staff</button>') + '</div>';
  return html + '</div></details>';
}

function sqMore() {
  const aud = SQ.aud;
  const active = SQ_FLAGS.filter(([k]) => aud[k]).length +
    SQ_RANGES.filter(([k]) => aud[k].min !== null || aud[k].max !== null).length;
  let html = '<details class="sq-field"' + (SQ.open.has('more') ? ' open' : '') + ' ontoggle="sqToggle(\'more\', this.open)">' +
    '<summary><span class="sq-field-name">Contact data, experience, company age</span><span class="sq-counts">' +
    (active ? '<span class="sq-count in">' + active + ' set</span>' : '<span class="sq-count none">Any</span>') +
    '</span></summary><div class="sq-field-body">';
  SQ_FLAGS.forEach(([k, label]) => {
    html += '<label class="sq-flag"><input type="checkbox"' + (aud[k] ? ' checked' : '') +
      ' onchange="sqSet(\'' + k + '\', this.checked)"> ' + label + '</label>';
  });
  SQ_RANGES.forEach(([k, label]) => {
    const v = side => aud[k][side] === null ? '' : aud[k][side];
    html += '<div class="sq-range"><span class="form-label">' + label + '</span><div class="sq-row">' +
      '<input class="form-input" type="number" min="0" placeholder="From" aria-label="' + label + ', from" value="' + v('min') +
      '" oninput="sqRange(\'' + k + '\',\'min\',this.value)"><span class="muted">to</span>' +
      '<input class="form-input" type="number" min="0" placeholder="To" aria-label="' + label + ', to" value="' + v('max') +
      '" oninput="sqRange(\'' + k + '\',\'max\',this.value)"></div></div>';
  });
  html += '<div class="sq-range"><label class="form-label" for="sq-page">People per search (20 to 100)</label>' +
    '<input class="form-input" id="sq-page" type="number" min="20" max="100" style="width:110px" value="' + aud.page_size +
    '" oninput="sqSet(\'page_size\', parseInt(this.value, 10) || 0)"></div>';
  return html + '</div></details>';
}

function sqSaveBar() {
  const saved = !!SQ.audId;
  let html = '<div class="sq-row"><button class="btn btn-secondary" onclick="sqSave(false)"' +
    (SQ.busy ? ' disabled' : '') + '>' + (saved ? (SQ.dirty ? 'Save changes' : 'Saved') : 'Save audience') + '</button>' +
    (saved ? '<button class="btn btn-ghost" onclick="sqSave(true)"' + (SQ.busy ? ' disabled' : '') + '>Save as new</button>' : '') +
    (SQ.dirty ? '<span class="sq-dirty">Not saved yet</span>' : '') + '</div>';
  return html;
}

// ── Audience edits ──

function sqTouch() {
  const was = SQ.dirty;
  SQ.dirty = true;
  if (!was && SQ.audId) sqRenderResults();
  const bar = sqEl('sq-save');
  if (bar) bar.innerHTML = sqSaveBar();
  const head = sqEl('sq-search-btn');
  if (head) head.textContent = sqSearchLabel();
}

function sqSet(key, value) { SQ.aud[key] = value; sqTouch(); }

function sqToggle(key, open) {
  if (open) SQ.open.add(key); else SQ.open.delete(key);
  const f = SQ_FIELDS.find(x => x.key === key);
  if (open && f && f.kind) sqRenderBrowse(key);
}

function sqRefreshField(key) {
  ['include', 'exclude'].forEach(side => {
    const el = sqEl('sq-chips-' + key + '-' + side);
    if (el) el.innerHTML = sqChips(key, side);
  });
  const count = sqEl('sq-count-' + key);
  if (count) count.innerHTML = sqCount(SQ.aud[key]);
  sqRenderBrowse(key);
  if (key === 'job_titles') {
    const ideas = document.querySelector('#sq-f-job_titles .sq-ideas');
    if (ideas) ideas.outerHTML = sqTitleIdeas();
  }
}

function sqRenderBrowse(key) {
  const f = SQ_FIELDS.find(x => x.key === key);
  const el = sqEl('sq-browse-' + key);
  if (f && f.kind && el) el.innerHTML = sqBrowse(f);
}

function sqAdd(key, side, raw) {
  const pair = SQ.aud[key];
  const other = side === 'include' ? 'exclude' : 'include';
  let changed = false;
  String(raw).split(',').map(v => v.replace(/\s+/g, ' ').trim()).filter(Boolean).forEach(name => {
    const same = v => v.toLowerCase() === name.toLowerCase();
    if (pair[side].some(same)) return;
    pair[other] = pair[other].filter(v => !same(v));     // a name is on one side only
    pair[side].push(name.slice(0, 80));
    changed = true;
  });
  if (changed) { sqRefreshField(key); sqTouch(); }
}

function sqDrop(key, side, i) {
  SQ.aud[key][side].splice(i, 1);
  sqRefreshField(key);
  sqTouch();
}

function sqPick(key, side, name) {
  const at = SQ.aud[key][side].indexOf(name);
  if (at >= 0) { sqDrop(key, side, at); return; }       // pressing it again takes it off
  sqAdd(key, side, name);
}

function sqTypeKey(event, key, side) {
  if (event.key !== 'Enter' && event.key !== ',') return;
  event.preventDefault();
  sqTypeAdd(event.target, key, side);
}

function sqTypeAdd(input, key, side) {
  if (!input.value.trim()) return;
  const value = input.value;
  input.value = '';
  sqAdd(key, side, value);
}

function sqBrowseType(key, text) {
  SQ.browse[key] = text;
  SQ.offered[key] = [];
  sqRenderBrowse(key);
  const f = SQ_FIELDS.find(x => x.key === key);
  const q = text.trim();
  clearTimeout(SQ.timer);
  if (!f || f.kind === 'company_type' || q.length < 2) return;
  const list = SQ.catalog && SQ.catalog.lists ? SQ.catalog.lists[f.kind] : null;
  if (list && list.complete) return;
  // The list is still being collected: ask SalesQL directly for what was typed.
  SQ.timer = setTimeout(async () => {
    const data = await sqPost('/api/salesql/lookup', {kind: f.kind, q: q});
    if ((SQ.browse[key] || '').trim() !== q) return;
    SQ.offered[key] = (data && data.values) || [];
    sqRenderBrowse(key);
  }, 350);
}

function sqSize(size, on) {
  const all = SQ.data.sizes || [];
  const chosen = new Set(SQ.aud.company_sizes);
  if (on) chosen.add(size); else chosen.delete(size);
  SQ.aud.company_sizes = all.filter(s => chosen.has(s));
  const el = sqEl('sq-f-sizes');
  if (el) el.innerHTML = sqSizes();
  sqTouch();
}

function sqStaffAdd(software) {
  if (SQ.aud.staff.length >= 10) { showToast('Harvey keeps at most 10 staff checks.', 'error'); return; }
  SQ.aud.staff.push(software ? {label: 'software', must: 'none', titles: SQ_DEV_TITLES.slice()}
                             : {label: '', must: 'none', titles: []});
  SQ.open.add('staff');
  sqEl('sq-f-staff').innerHTML = sqStaff();
  sqTouch();
}

function sqStaffRemove(i) {
  SQ.aud.staff.splice(i, 1);
  sqEl('sq-f-staff').innerHTML = sqStaff();
  sqTouch();
}

function sqStaffSet(i, key, value) {
  const check = SQ.aud.staff[i];
  if (!check) return;
  check[key] = key === 'titles' ? value.split(',').map(v => v.trim()).filter(Boolean) : value;
  sqTouch();
}

function sqRange(key, side, value) {
  const n = parseInt(value, 10);
  SQ.aud[key][side] = Number.isInteger(n) && n >= 0 ? n : null;
  sqTouch();
}

// ── Saving and switching audiences ──

async function sqSave(asNew) {
  if (SQ.busy) return false;
  const problem = sqProblem();
  if (problem) { showToast(problem, 'error'); return false; }
  SQ.busy = 'save';
  const body = {audience: SQ.aud};
  if (!asNew && SQ.audId) body.id = SQ.audId;
  if (asNew || !SQ.audId) {      // a new audience never takes another one's name
    const taken = ((SQ.data.audiences || {}).items || []).map(i => i.name.toLowerCase());
    let name = SQ.aud.name, n = 2;
    while (taken.includes(name.toLowerCase())) name = SQ.aud.name + ' ' + n++;
    body.audience = {...SQ.aud, name: name};
  }
  const data = await sqPost('/api/salesql/audience', body);
  SQ.busy = '';
  if (!data || !data.success) {
    showToast((data && data.message) || 'Could not save the audience.', 'error');
    const bar = sqEl('sq-save');
    if (bar) bar.innerHTML = sqSaveBar();
    return false;
  }
  SQ.notes = data.notes || [];
  SQ.dirty = false;
  SQ.aud = null;
  showToast(SQ.notes.length ? 'Saved, with names to check.' : 'Audience saved.', SQ.notes.length ? 'error' : 'success');
  await loadSalesql();
  return true;
}

function sqProblem() {
  const aud = SQ.aud;
  if (!aud.job_titles.include.length) return 'Add at least one job title to include.';
  if (!(aud.page_size >= 20 && aud.page_size <= 100)) return 'People per search must be from 20 to 100.';
  const bad = aud.staff.findIndex(c => !c.titles.length);
  if (bad >= 0) return 'Staff check ' + (bad + 1) + ' needs at least one job title.';
  return '';
}

async function sqSelectAudience(id) {
  if (!id) return;
  if (SQ.dirty && !confirm('Leave this audience without saving your changes?')) {
    sqEl('sq-aud-select').value = SQ.audId;
    return;
  }
  const data = await sqPost('/api/salesql/audience/select', {id: id});
  if (!data || !data.success) { showToast((data && data.message) || 'Could not switch.', 'error'); return; }
  SQ.dirty = false; SQ.aud = null; SQ.notes = [];
  loadSalesql();
}

function sqNewAudience() {
  if (SQ.dirty && !confirm('Leave this audience without saving your changes?')) return;
  SQ.audId = '';
  SQ.aud = {...sqBlankAudience(), name: 'New audience'};   // a clean start; "Save as new" copies
  SQ.dirty = true; SQ.notes = []; SQ.picks = new Set(); SQ.tab = 'qualified';
  SQ.open = new Set(['job_titles']);
  sqRender();
  const name = sqEl('sq-name');
  if (name) { name.focus(); name.select(); }
}

async function sqDeleteAudience() {
  if (!SQ.audId || !confirm('Delete the audience "' + SQ.aud.name + '"? People already found stay on the list.')) return;
  const data = await sqPost('/api/salesql/audience/delete', {id: SQ.audId});
  if (!data || !data.success) { showToast((data && data.message) || 'Could not delete it.', 'error'); return; }
  SQ.dirty = false; SQ.aud = null; SQ.notes = [];
  loadSalesql();
}

// ── Results ──

function sqRows(tab) {
  // A new audience that isn't saved yet has found nobody.
  if (!SQ.audId && (SQ.data.audiences || {}).saved) return [];
  if (tab === 'done') return SQ.data.handled || [];
  return (SQ.data.candidates || []).filter(r => (r.grade || 'unsure') === tab);
}

function sqSearchLabel() {
  if (SQ.busy === 'search') return 'Searching…';
  return (SQ.dirty || !SQ.audId) ? 'Save and search' : 'Search';
}

function sqResults() {
  const rows = sqRows(SQ.tab);
  let html = '<div class="sq-results-head"><div><div class="sq-panel-title">People</div>' +
    '<p class="desc">The people <b>' + escHtml(SQ.aud.name) + '</b> has found. Searching spends nothing; each search ' +
    'brings its next ' + (SQ.aud.page_size || 100) + ' people. Each audience keeps its own list.</p></div>' +
    '<button class="btn btn-primary" id="sq-search-btn" onclick="sqSearch()"' + (SQ.busy ? ' disabled' : '') + '>' +
    sqSearchLabel() + '</button></div>';
  if (SQ.audId && SQ.dirty && (SQ.data.candidates || []).length) {
    html += '<div class="banner wait" style="margin-bottom:10px"><div><p style="margin:0">You have changed this audience. ' +
      'The people below were found with the filters as last saved. Press <b>Save and search</b>: new people follow the new ' +
      'filters, and anyone here who no longer fits moves out of Qualified.</p></div></div>';
  }
  html += '<div class="sq-tabs" role="tablist">' + SQ_TABS.map(([key, label]) =>
    '<button class="sq-tab' + (key === SQ.tab ? ' on' : '') + '" role="tab" aria-selected="' + (key === SQ.tab) +
    '" onclick="sqTab(\'' + key + '\')">' + label + ' <span class="sq-tab-n">' + sqRows(key).length + '</span></button>').join('') + '</div>';
  if (!rows.length) {
    const empty = {
      qualified: ['Nobody qualified yet', 'Set the audience on the left and press <b>Search</b>.'],
      unsure: ['Nobody to review', 'People Harvey could not decide on appear here, with the reason.'],
      rejected: ['Nobody rejected', 'People your excludes and checks ruled out appear here, with the reason.'],
      done: ['Nobody revealed yet', 'People you reveal move here and are added to Contacts.'],
    }[SQ.tab];
    return html + '<div class="card">' + emptyState('&#128269;', empty[0], empty[1]) + '</div>';
  }
  const pickable = SQ.tab !== 'done';
  const allOn = pickable && rows.every(r => SQ.picks.has(r.id));
  html += '<div class="table-card sq-table"><table><thead><tr>' +
    (pickable ? '<th class="sq-check"><input type="checkbox" aria-label="Select everyone shown"' + (allOn ? ' checked' : '') +
      ' onchange="sqPickAll(this.checked)"></th>' : '') +
    '<th>Person</th><th>Company</th><th>' + (pickable ? 'Why' : 'Result') + '</th></tr></thead><tbody>' +
    rows.map(r => '<tr>' +
      (pickable ? '<td class="sq-check"><input type="checkbox" aria-label="Select ' + escHtml(r.full_name) + '"' +
        (SQ.picks.has(r.id) ? ' checked' : '') + ' data-id="' + escHtml(r.id) + '" onchange="sqPickOne(this.dataset.id, this.checked)"></td>' : '') +
      '<td><div class="cell-main">' + (r.linkedin_url
        ? '<a href="' + safeHref(r.linkedin_url) + '" target="_blank" rel="noopener noreferrer">' + escHtml(r.full_name) + '</a>'
        : escHtml(r.full_name)) + '</div><div class="cell-sub">' + escHtml(r.title || '') +
        (r.location ? ' &middot; ' + escHtml(r.location) : '') + '</div></td>' +
      '<td><div class="cell-main">' + escHtml(r.org_name || '') + '</div><div class="cell-sub">' +
        escHtml([r.org_industry, r.org_size ? r.org_size + ' people' : ''].filter(Boolean).join(' · ')) + '</div></td>' +
      '<td class="sq-why">' + (pickable ? escHtml(r.reason || '')
        : '<span class="badge ' + ({revealed: 't-good', dismissed: 't-waiting'}[r.status] || 't-bad') + '">' +
          escHtml({revealed: 'Revealed', no_email: 'No work email', failed: 'Not found', skipped: 'Skipped',
                   dismissed: 'Taken off'}[r.status] || r.status) + '</span> ' + escHtml(r.note || '')) + '</td></tr>').join('') +
    '</tbody></table></div>';
  if (pickable) html += '<div class="sq-actions" id="sq-actions">' + sqActions() + '</div>';
  return html;
}

function sqActions() {
  const n = SQ.picks.size;
  return '<span class="sq-actions-n">' + (n ? n + ' selected' : 'Tick people to act on them') + '</span>' +
    '<button class="btn btn-secondary" onclick="sqDismiss()"' + (n && !SQ.busy ? '' : ' disabled') + '>Take off the list</button>' +
    '<button class="btn btn-primary" onclick="sqReveal()"' + (n && !SQ.busy ? '' : ' disabled') + '>' +
    (SQ.busy === 'reveal' ? 'Revealing…' : n ? 'Reveal ' + n + ' (' + n + (n === 1 ? ' credit' : ' credits') + ' at most)' : 'Reveal') + '</button>';
}

function sqRenderResults() {
  const el = sqEl('sq-results');
  if (el) el.innerHTML = sqResults();
}

function sqTab(tab) { SQ.tab = tab; SQ.picks = new Set(); sqRenderResults(); }

function sqPickOne(id, on) {
  if (on) SQ.picks.add(id); else SQ.picks.delete(id);
  const bar = sqEl('sq-actions');
  if (bar) bar.innerHTML = sqActions();
}

function sqPickAll(on) {
  sqRows(SQ.tab).forEach(r => { if (on) SQ.picks.add(r.id); else SQ.picks.delete(r.id); });
  sqRenderResults();
}

// ── Actions ──

async function sqConnect() {
  const data = await sqPost('/api/salesql/connect', {key: (sqEl('salesql-key') || {}).value || ''});
  showToast(data && data.message ? data.message : 'Could not connect.', data && data.success ? 'success' : 'error');
  loadSalesql();
}

async function sqSaveCap() {
  const data = await sqPost('/api/salesql/cap', {cap: parseInt((sqEl('sq-cap') || {}).value, 10)});
  showToast(data && data.success ? 'Daily cap set to ' + data.cap + '.' : (data && data.message) || 'Could not save the cap.',
            data && data.success ? 'success' : 'error');
  if (data && data.success) loadSalesql();
}

async function sqSearch() {
  if (SQ.busy) return;
  if ((SQ.dirty || !SQ.audId) && !(await sqSave(false))) return;
  SQ.busy = 'search';
  sqRenderResults();
  const data = await sqPost('/api/salesql/search');
  SQ.busy = '';
  if (data && data.success) {
    showToast(data.found + ' found: ' + data.qualified + ' qualified, ' + data.unsure + ' unsure, ' + data.rejected +
      ' rejected. No credits spent.' + (data.notes && data.notes.length ? ' ' + data.notes.join(' ') : ''), 'success');
    SQ.tab = data.qualified ? 'qualified' : data.unsure ? 'unsure' : 'rejected';
  } else {
    showToast((data && data.message) || 'The search failed.', 'error');
  }
  await loadSalesql();
}

async function sqReveal() {
  const ids = [...SQ.picks];
  if (SQ.busy || !ids.length) return;
  if (!confirm('Reveal ' + ids.length + (ids.length === 1 ? ' person' : ' people') + '? This spends up to ' +
               ids.length + ' SalesQL credits.')) return;
  SQ.busy = 'reveal';
  const bar = sqEl('sq-actions');
  if (bar) bar.innerHTML = sqActions();
  const data = await sqPost('/api/salesql/reveal', {ids: ids});
  SQ.busy = '';
  showToast(data && data.success
    ? 'Revealed ' + data.revealed + ' · no work email ' + data.no_email + ' · skipped ' + data.skipped + ' · failed ' +
      data.failed + '. Credits used: ' + data.credits_used +
      (data.verifications_used ? ' · address checks: ' + data.verifications_used : '') + '.' +
      (data.messages && data.messages.length ? ' ' + data.messages.join(' ') : '')
    : (data && data.message) || 'The reveal failed.', data && data.success ? 'success' : 'error');
  SQ.picks = new Set();
  if (data && data.success && data.revealed) SQ.tab = 'done';
  await loadSalesql();
}

async function sqDismiss() {
  const ids = [...SQ.picks];
  if (SQ.busy || !ids.length) return;
  const data = await sqPost('/api/salesql/dismiss', {ids: ids});
  showToast(data && data.success ? data.dismissed + ' taken off the list.' : (data && data.message) || 'Could not do that.',
            data && data.success ? 'success' : 'error');
  SQ.picks = new Set();
  await loadSalesql();
}
