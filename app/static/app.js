const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = (x='') => String(x).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const api = async (url, opt={}) => {
  opt.headers = {...(opt.headers||{}), 'X-Requested-With':'NASPDFReader'};
  if (opt.body && typeof opt.body !== 'string') {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(opt.body);
  }
  const r = await fetch(url, opt);
  if (r.status === 401) {
    showAuth(false);
    throw new Error('Login required');
  }
  if (!r.ok) {
    let x;
    try { x = await r.json(); } catch { x = {detail: await r.text()}; }
    throw new Error(typeof x.detail === 'string' ? x.detail : JSON.stringify(x.detail || x));
  }
  return r.json();
};

// Thai-aware, numeric collation for all user-visible book/series ordering.
// Intl.Collator follows CLDR Thai collation rules (including leading vowels such as เ แ โ ใ ไ)
// and numeric:true keeps 2 before 10.
const THAI_COLLATOR = new Intl.Collator('th-TH', {numeric:true, sensitivity:'base'});
const compareText = (a='', b='') => THAI_COLLATOR.compare(String(a ?? ''), String(b ?? ''));
function compareBooksForSort(a,b,sort=state.sort){
  if(sort==='series'){
    return compareText(a.series,b.series) || compareText(a.volume || a.title,b.volume || b.title) || compareText(a.title,b.title);
  }
  if(sort==='title') return compareText(a.title,b.title);
  return 0;
}
function sortBooksForUi(books,sort=state.sort){
  if(sort==='title' || sort==='series') books.sort((a,b)=>compareBooksForSort(a,b,sort));
  return books;
}

const initialParams = new URLSearchParams(location.search);
const initialSeries = initialParams.get('series') || '';
const urlCategory = initialParams.get('category') || '';
const initialSeriesCategory = initialSeries ? urlCategory : '';
const initialCategory = initialSeries ? (localStorage.getItem('nasreader.category') || '') : (initialParams.has('category') ? urlCategory : (localStorage.getItem('nasreader.category') || ''));
const initialView = initialSeries ? 'books' : (initialParams.get('view') || localStorage.getItem('nasreader.view') || 'books');

let state = {
  books: [], allSeries: [], categories: [], selected: new Set(), selection: false,
  category: initialCategory, q: '', series: initialSeries, seriesCategory: initialSeriesCategory,
  sort: localStorage.getItem('nasreader.sort') || 'title',
  view: initialView,
  allowDelete: false, longPress: null, lastSelected: null, suppressClick: null,
  missingCount: 0, missingRetention: 14, username: ''
};
let loadController = null;
let loadSeq = 0;

const toast = m => {
  const t = $('#toast');
  t.textContent = m;
  t.classList.remove('hidden');
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.add('hidden'), 2400);
};

function libraryUrlForCurrentState() {
  const p = new URLSearchParams();
  if (state.series) {
    p.set('series', state.series);
    if (state.seriesCategory || state.category) p.set('category', state.seriesCategory || state.category);
    p.set('view', 'books');
  }
  return '/' + (p.toString() ? `?${p.toString()}` : '');
}

function syncLibraryUrl() {
  history.replaceState({nasreader:true, series:state.series || ''}, '', libraryUrlForCurrentState());
}

function saveReaderContext(id) {
  const b = state.books.find(x => x.id === +id);
  const fromSeries = !!state.series;
  const seriesCategory = fromSeries ? (state.seriesCategory || b?.category || state.category || '') : '';
  const returnParams = new URLSearchParams();
  if (fromSeries) { returnParams.set('series', state.series); if (seriesCategory) returnParams.set('category', seriesCategory); returnParams.set('view','books'); }
  const ctx = {
    from: fromSeries ? 'series' : 'library',
    series: fromSeries ? state.series : (b?.series || ''),
    category: fromSeries ? seriesCategory : (b?.category || ''),
    orderedIds: fromSeries ? state.books.map(x => x.id) : [],
    returnUrl: fromSeries ? `/?${returnParams.toString()}` : (location.pathname + location.search),
    scrollY: window.scrollY || 0,
    openedAt: Date.now()
  };
  try { sessionStorage.setItem('nasreader.readerContext', JSON.stringify(ctx)); } catch {}
}

function openReader(id) {
  saveReaderContext(id);
  location.href = `/reader/${id}`;
}

function restoreSeriesScrollOnce() {
  if (state.__scrollRestored || !state.series) return;
  let ctx = null;
  try { ctx = JSON.parse(sessionStorage.getItem('nasreader.readerContext') || 'null'); } catch {}
  if (!ctx || ctx.from !== 'series' || ctx.series !== state.series || (ctx.category || '') !== (state.seriesCategory || state.category || '')) return;
  state.__scrollRestored = true;
  const y = Math.max(0, Number(ctx.scrollY) || 0);
  requestAnimationFrame(() => window.scrollTo(0, y));
  setTimeout(() => window.scrollTo(0, y), 180);
}

async function authInit() {
  const s = await fetch('/api/auth/status').then(r => r.json());
  state.username = s.username || '';
  if (s.authenticated) {
    $('#auth').classList.add('hidden');
    $('#app').classList.remove('hidden');
    restoreControls();
    load();
  } else showAuth(s.setup_required);
}

function showAuth(setup) {
  $('#app').classList.add('hidden');
  $('#auth').classList.remove('hidden');
  $('#authHint').textContent = setup ? 'First run: create the only admin account.' : 'Login to your private library.';
  $('#authBtn').textContent = setup ? 'Create account' : 'Login';
  $('#authBtn').onclick = async () => {
    try {
      $('#authError').textContent = '';
      const d = await api(setup ? '/api/auth/setup' : '/api/auth/login', {
        method:'POST', body:{username:$('#username').value, password:$('#password').value}
      });
      state.username = d.username || $('#username').value;
      $('#auth').classList.add('hidden');
      $('#app').classList.remove('hidden');
      restoreControls();
      load();
    } catch(e) { $('#authError').textContent = e.message; }
  };
}

function restoreControls() {
  $('#sort').value = state.sort;
  syncViewButtons();
}

function query() {
  const p = new URLSearchParams();
  if (state.q) p.set('q', state.q);
  const effectiveCategory = state.series ? (state.seriesCategory || state.category) : state.category;
  if (effectiveCategory) p.set('category', effectiveCategory);
  if (state.series) p.set('series', state.series);
  p.set('sort', state.sort);
  if ($('#showArchived').checked) p.set('show_archived','true');
  if ($('#favorites').checked) p.set('favorites','true');
  return p.toString();
}

async function load() {
  const seq = ++loadSeq;
  if (loadController) loadController.abort();
  const controller = new AbortController();
  loadController = controller;
  let d;
  try {
    d = await api('/api/library?' + query(), {signal: controller.signal});
  } catch (e) {
    if (e?.name === 'AbortError') return;
    throw e;
  }
  if (seq !== loadSeq) return;
  state.books = sortBooksForUi(d.books || [], state.sort);
  state.allSeries = d.series;
  state.categories = d.categories || [];
  state.allowDelete = d.allow_delete_files;
  state.missingCount = d.missing_count || 0;
  state.missingRetention = d.missing_retention_days || 14;
  if (state.category && !state.categories.some(c => c.name === state.category)) {
    state.category = '';
    localStorage.setItem('nasreader.category','');
  }
  $('#physicalDelete').classList.toggle('hidden', !state.allowDelete);
  $('#countBadge').textContent = `${d.count} books`;
  renderCategoryTabs();
  renderSeriesFilter(d.series);
  renderContinue();
  render();
  renderScan(d.scan);
  restoreSeriesScrollOnce();
}

function renderCategoryTabs() {
  const total = state.categories.reduce((s,c)=>s+c.count,0);
  const items = [{name:'', label:'All', count:total}, ...state.categories.filter(c=>c.visible).map(c=>({name:c.name,label:c.name,count:c.count}))];
  $('#categoryTabs').innerHTML = items.map(c => `<button data-category="${esc(c.name)}" class="${state.category===c.name?'active':''}">${esc(c.label)} <span>${c.count}</span></button>`).join('');
  $$('#categoryTabs button').forEach(b => b.onclick = () => {
    state.category = b.dataset.category;
    localStorage.setItem('nasreader.category', state.category);
    state.series = ''; state.seriesCategory = '';
    syncLibraryUrl();
    load();
  });
}

function seriesOptionValue(x) { return `${encodeURIComponent(x.category)}|${encodeURIComponent(x.series)}`; }
function renderSeriesFilter(list) {
  const s = $('#seriesFilter');
  const filtered = list.filter(x => !state.category || x.category === state.category).sort((a,b)=>compareText(a.series,b.series)||compareText(a.category,b.category));
  s.innerHTML = '<option value="">All series</option>' + filtered.map(x => {
    const label = state.category ? `${x.series} (${x.count})` : `${x.series} · ${x.category} (${x.count})`;
    return `<option value="${esc(seriesOptionValue(x))}">${esc(label)}</option>`;
  }).join('');
  if (state.series) {
    const wanted = seriesOptionValue({category:state.seriesCategory || state.category,series:state.series});
    if ([...s.options].some(o => o.value === wanted)) s.value = wanted;
    else { state.series = ''; state.seriesCategory=''; s.value = ''; }
  } else s.value = '';
}

function renderContinue() {
  const books = state.books
    .filter(b => b.read_state === 'reading' || (b.last_page > 1 && b.read_state !== 'read'))
    .sort((a,b)=>(b.last_opened||'').localeCompare(a.last_opened||''))
    .slice(0,12);
  const sec = $('#continueSection');
  if (!books.length || state.selection) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  $('#continueRow').innerHTML = books.map(b => `<button class="continue-card" data-open-reader="${b.id}"><img src="/api/books/${b.id}/thumb" loading="lazy"><span><strong>${esc(b.title)}</strong><small>${b.file_type==='epub'?`EPUB · ${b.progress}%`:`${b.last_page}/${b.page_count} · ${b.progress}%`}</small></span></button>`).join('');
  $$('[data-open-reader]').forEach(x => x.onclick = () => openReader(+x.dataset.openReader));
}

function syncViewButtons() {
  $$('#viewToggle button').forEach(b => b.classList.toggle('active', b.dataset.view === state.view));
}
function setView(v) {
  state.view = v;
  localStorage.setItem('nasreader.view', v);
  syncViewButtons();
  render();
}

function render() {
  const g = $('#grid');
  g.classList.toggle('select-mode', state.selection);
  $('#seriesBack').classList.toggle('hidden', !state.series);
  $('#seriesTitle').textContent = state.series || '';
  if (state.series || state.selection || state.view === 'books') renderBooks(g);
  else renderSeriesCards(g);
  $('#empty').classList.toggle('hidden', state.books.length > 0);
  updateBulk();
}

function bookCard(b) {
  return `<article class="book ${state.selected.has(b.id)?'selected':''}" data-id="${b.id}">
    <div class="cover-wrap">
      <img class="cover" loading="lazy" src="/api/books/${b.id}/thumb">
      <div class="select-dot">${state.selected.has(b.id)?'✓':''}</div>
      <button class="star" data-star="${b.id}" aria-label="Favorite">${b.favorite?'★':'☆'}</button>
      <button class="book-more" data-more="${b.id}" aria-label="Book details">•••</button>
      <div class="progress"><i style="width:${b.progress}%"></i></div>
    </div>
    <div class="book-info">
      <div class="book-title">${esc(b.title)}</div>
      <div class="meta"><span>${esc(b.series || b.category)}</span><span>${b.file_type==='epub'?'EPUB':`${b.page_count}p`}</span></div>
      <div class="chips">${b.volume?`<span class="chip">#${esc(b.volume)}</span>`:''}${b.tags.slice(0,2).map(t=>`<span class="chip">${esc(t)}</span>`).join('')}</div>
    </div>
  </article>`;
}

function renderBooks(g) {
  g.classList.remove('series-grid');
  g.innerHTML = state.books.map(bookCard).join('');
  bindCards();
}

function renderSeriesCards(g) {
  const grouped = new Map(), standalone = [];
  for (const b of state.books) {
    if (!b.series) { standalone.push(b); continue; }
    const key = `${b.category}\u0000${b.series}`;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(b);
  }
  const cards = [...grouped.values()].sort((a,b)=>compareText(a[0].series,b[0].series)||compareText(a[0].category,b[0].category)).map(items => {
    items.sort((a,b)=>compareText(a.volume||a.title,b.volume||b.title)||compareText(a.title,b.title));
    const first = items[0], read = items.filter(x=>x.read_state==='read').length;
    const avg = Math.round(items.reduce((s,x)=>s+x.progress,0)/items.length);
    return `<article class="series-card" data-series="${esc(first.series)}" data-category="${esc(first.category)}">
      <div class="series-covers"><img src="/api/books/${first.id}/thumb" loading="lazy"></div>
      <div class="series-info"><div class="book-title">${esc(first.series)}</div><div class="meta"><span>${items.length} books</span><span>${read}/${items.length} read</span></div><div class="progress"><i style="width:${avg}%"></i></div><small>Long-press to select the whole series</small></div>
    </article>`;
  }).join('');
  g.classList.add('series-grid');
  g.innerHTML = cards + (standalone.length ? `<div class="standalone-label">Standalone</div>${standalone.map(bookCard).join('')}` : '');
  bindSeriesCards();
  bindCards();
}
function bindSeriesCards() {
  $$('.series-card').forEach(card => {
    let lp = null, suppress = false;
    const name = card.dataset.series, category = card.dataset.category;
    card.addEventListener('pointerdown', e => {
      if (e.pointerType === 'touch') lp = setTimeout(() => {
        suppress = true; state.view='books'; setSelection(true);
        state.books.filter(b=>b.series===name && b.category===category).forEach(b=>state.selected.add(b.id));
        navigator.vibrate?.(20); render();
      }, 450);
    });
    ['pointerup','pointercancel','pointermove'].forEach(ev=>card.addEventListener(ev,()=>clearTimeout(lp)));
    card.onclick = () => {
      if (suppress) { suppress=false; return; }
      state.series = name; state.seriesCategory = category; $('#seriesFilter').value = seriesOptionValue({category,series:name});
      state.view='books'; syncViewButtons(); state.__scrollRestored=false; syncLibraryUrl(); load();
    };
  });
}

function bindCards() {
  $$('.book').forEach(card => {
    const id = +card.dataset.id;
    card.addEventListener('pointerdown', e => {
      if (e.pointerType === 'touch' && !state.selection && !e.target.closest('button')) {
        state.longPress = setTimeout(() => {
          state.suppressClick = id; state.view='books'; setSelection(true); toggle(id); navigator.vibrate?.(20);
        }, 450);
      }
    });
    ['pointerup','pointercancel','pointermove'].forEach(ev=>card.addEventListener(ev,()=>clearTimeout(state.longPress)));
    card.addEventListener('click', e => {
      if (state.suppressClick === id) { state.suppressClick = null; return; }
      if (e.target.closest('.star') || e.target.closest('.book-more')) return;
      if (state.selection) {
        if (e.shiftKey && state.lastSelected) {
          const ids=state.books.map(x=>x.id), a=ids.indexOf(state.lastSelected), b=ids.indexOf(id);
          ids.slice(Math.min(a,b),Math.max(a,b)+1).forEach(x=>state.selected.add(x)); render();
        } else toggle(id);
        state.lastSelected = id;
      } else openReader(id);
    });
  });
  $$('[data-star]').forEach(x => x.onclick = async e => {
    e.stopPropagation();
    const id=+x.dataset.star, b=state.books.find(z=>z.id===id); if(!b)return;
    const updated=await api(`/api/books/${id}`, {method:'PATCH', body:{favorite:!b.favorite}});
    b.favorite=!!updated.favorite; x.textContent=b.favorite?'★':'☆';
  });
  $$('[data-more]').forEach(x => x.onclick = e => { e.stopPropagation(); openDetails(+x.dataset.more); });
}

function setSelection(v) {
  state.selection = v;
  if (v) state.view='books';
  if (!v) state.selected.clear();
  $('#selectBtn').textContent = v ? 'Done' : 'Select';
  renderContinue(); render();
}
function toggle(id) { state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id); render(); }
function updateBulk() {
  $('#bulkBar').classList.toggle('hidden', !state.selection);
  $('#selectedCount').textContent = `${state.selected.size} selected`;
}

function categoryOptions(selected='') {
  return state.categories.map(c => `<option value="${esc(c.name)}" ${c.name===selected?'selected':''}>${esc(c.name)}</option>`).join('');
}

async function openDetails(id) {
  const b = await api(`/api/books/${id}`);
  const isEpub=b.file_type==='epub';
  const pages=isEpub?[]:[1,Math.min(2,b.page_count),Math.min(3,b.page_count)].filter((x,i,a)=>x&&a.indexOf(x)===i);
  const preview=isEpub?`<div class="detail-preview"><img loading="lazy" src="/api/books/${id}/thumb"></div>`:`<div class="detail-preview">${pages.map(p=>`<img loading="lazy" src="/api/books/${id}/preview/${p}">`).join('')}</div>`;
  const stats=isEpub?`<div class="detail-stats"><span>EPUB</span><span>${b.page_count||0} chapters</span><span>${b.progress}% read</span></div>`:`<div class="detail-stats"><span>${b.page_count} pages</span><span>${b.progress}% read</span><span>Page ${b.last_page}</span></div>`;
  $('#modalBody').innerHTML = `
    <div class="detail-head"><div><h2>${esc(b.title)}</h2><div class="detail-sub">${esc(b.series||'Standalone')}${b.volume?` · #${esc(b.volume)}`:''} · ${esc(b.category)}</div></div></div>
    ${preview}
    ${stats}
    <div class="detail-actions">
      <button id="detailRead" class="button-link primary">Read / Resume</button>
      <a class="button-link" href="/api/books/${id}/file?download=true">Download ${isEpub?'EPUB':'PDF'}</a>
      <button id="detailFav">${b.favorite?'★ Unfavorite':'☆ Favorite'}</button>
      <button id="detailEdit">Edit</button>
    </div>
    <div class="path-meta">${esc(b.rel_path)}</div>
    <div id="detailEditPane" class="hidden"></div>`;
  showModal();
  $('#detailRead').onclick = () => openReader(id);
  $('#detailFav').onclick = async () => { const updated=await api(`/api/books/${id}`,{method:'PATCH',body:{favorite:!b.favorite}}); const local=state.books.find(x=>x.id===id); if(local)local.favorite=!!updated.favorite; hideModal(); render(); };
  $('#detailEdit').onclick = () => showEditPane(id,b);
}


function showEditPane(id,b) {
  const pane = $('#detailEditPane');
  const isEpub=b.file_type==='epub';
  pane.classList.remove('hidden');
  pane.innerHTML = `<div class="form-grid detail-edit">
    <label>Title<input id="eTitle" value="${esc(b.title)}"></label>
    <label>Category<select id="eCategory">${categoryOptions(b.category)}</select></label>
    <label>Series<input id="eSeries" value="${esc(b.series||'')}"></label>
    <label>Volume / Part<input id="eVolume" value="${esc(b.volume||'')}"></label>
    ${isEpub?'':`<label>Cover page<input id="eCover" type="number" min="1" max="${b.page_count}" value="${b.cover_page}"></label>`}
    <label>Tags<input id="eTags" value="${esc(b.tags.join(', '))}"></label>
    <div class="row"><button id="saveEdit" class="primary">Save</button><button id="resetTitle">Reset title to filename</button></div>
  </div>`;
  $('#saveEdit').onclick = async () => {
    const body={title:$('#eTitle').value,category:$('#eCategory').value,series:$('#eSeries').value||null,volume:$('#eVolume').value||null,tags:$('#eTags').value.split(',').map(x=>x.trim()).filter(Boolean)};
    if(!isEpub)body.cover_page=+$('#eCover').value;
    await api(`/api/books/${id}`, {method:'PATCH', body});
    hideModal(); toast('Saved'); load();
  };
  $('#resetTitle').onclick = async () => { await api(`/api/books/${id}/reset-title`,{method:'POST'}); hideModal(); toast('Title reset'); load(); };
}


function showModal() { $('#modal').classList.remove('hidden'); }
function hideModal() { $('#modal').classList.add('hidden'); }
function actionSheet(title, html, onApply) {
  $('#modalBody').innerHTML = `<h2>${esc(title)}</h2>${html}<div class="row modal-actions"><button id="actionCancel">Cancel</button><button id="actionApply" class="primary">Apply</button></div>`;
  showModal(); $('#actionCancel').onclick=hideModal; $('#actionApply').onclick=onApply;
}

async function runBulk(action,value) {
  const ids=[...state.selected]; if(!ids.length) return toast('Select books first');
  await api('/api/bulk',{method:'POST',body:{ids,action,value}});
  toast(`Updated ${ids.length} books`); hideModal(); state.selected.clear(); setSelection(false); load();
}

function bulkPrompt(kind) {
  const n=state.selected.size; if(!n) return toast('Select books first');
  if(kind==='category') {
    actionSheet(`Move ${n} books to category`,`<div class="choice-grid">${state.categories.map(x=>`<button class="choice" data-value="${esc(x.name)}">${esc(x.name)}</button>`).join('')}</div>`,()=>{
      const v=$('.choice.selected')?.dataset.value; if(!v)return toast('Choose a category'); runBulk('category',v);
    }); bindChoiceSoon(); return;
  }
  if(kind==='series') {
    const opts=[...new Set(state.allSeries.filter(x=>!state.category||x.category===state.category).map(x=>x.series))].sort(compareText);
    actionSheet(`Move ${n} books to series`,`<label>Series<input id="bulkSeries" list="seriesNames" placeholder="Blank = Standalone"></label><datalist id="seriesNames">${opts.map(x=>`<option value="${esc(x)}">`).join('')}</datalist>`,()=>runBulk('series',$('#bulkSeries').value)); return;
  }
  if(kind==='tag') {
    actionSheet(`Tags for ${n} books`,`<label>Tag<input id="bulkTag" placeholder="Tag name"></label><div class="choice-grid"><button class="choice selected" data-value="add">Add</button><button class="choice" data-value="remove">Remove</button></div>`,()=>{const t=$('#bulkTag').value.trim();if(!t)return toast('Enter a tag');runBulk($('.choice.selected')?.dataset.value==='remove'?'remove_tag':'add_tag',t)});bindChoiceSoon();return;
  }
  if(kind==='read') {
    actionSheet(`Read state for ${n} books`,`<div class="choice-grid">${['unread','reading','read'].map(x=>`<button class="choice" data-value="${x}">${x}</button>`).join('')}</div>`,()=>{const v=$('.choice.selected')?.dataset.value;if(!v)return toast('Choose a state');runBulk('read_state',v)});bindChoiceSoon();return;
  }
  if(kind==='favorite') { actionSheet(`Favorite ${n} books`,`<div class="choice-grid"><button class="choice selected" data-value="1">Favorite</button><button class="choice" data-value="0">Unfavorite</button></div>`,()=>runBulk('favorite',$('.choice.selected')?.dataset.value==='1'));bindChoiceSoon();return; }
  if(kind==='archive') { actionSheet(`Archive ${n} books`,`<div class="choice-grid"><button class="choice selected" data-value="1">Archive</button><button class="choice" data-value="0">Unarchive</button></div>`,()=>runBulk('archive',$('.choice.selected')?.dataset.value==='1'));bindChoiceSoon();return; }
  if(kind==='preview') return actionSheet('Regenerate previews',`<p>Clear cached covers/previews for ${n} selected books and regenerate covers in the background.</p>`,()=>runBulk('regen_preview',true));
  if(kind==='remove') return actionSheet('Remove from library',`<div class="warning-box">Remove ${n} books from the index? <strong>Book files stay on the NAS.</strong> The paths will be ignored until you explicitly import them again.</div>`,()=>runBulk('remove_library',true));
  if(kind==='delete') return actionSheet('Delete book files',`<div class="danger-box"><strong>Permanent deletion.</strong><br>Delete ${n} selected book files from the NAS? This cannot be undone.</div><label class="confirm-line"><input id="deleteConfirm" type="checkbox"> I understand these files will be deleted from the NAS.</label>`,()=>{if(!$('#deleteConfirm').checked)return toast('Confirm permanent deletion first');runBulk('delete_files',true)});
}
function bindChoiceSoon(){setTimeout(()=>$$('.choice').forEach(b=>b.onclick=()=>{b.parentElement.querySelectorAll('.choice').forEach(x=>x.classList.remove('selected'));b.classList.add('selected')}),0)}

async function renderScan(s) {
  if(!s)return;
  const el=$('#scanStatus');
  if(s.running) {
    el.classList.remove('hidden');
    el.textContent=`Rescanning NAS… seen ${s.seen}, +${s.added}, updated ${s.updated}, missing ${s.missing||0}`;
    setTimeout(async()=>renderScan(await api('/api/scan/status')),1000);
  } else if(s.error) {
    el.classList.remove('hidden'); el.textContent='Rescan error: '+s.error;
  } else if((s.suspicious_sources||[]).length) {
    el.classList.remove('hidden'); el.textContent=`Rescan safety stop: ${(s.suspicious_sources||[]).map(x=>`${x.source} looked empty (${x.previous} indexed before)`).join(', ')}. Existing books were kept.`;
  } else if((s.unreadable||[]).length) {
    el.classList.remove('hidden'); el.textContent=`Rescan finished with ${(s.unreadable||[]).length} unreadable file(s). First: ${s.unreadable[0].path}`;
  } else if((s.sources_skipped||[]).length) {
    el.classList.remove('hidden'); el.textContent=`Rescan finished. Skipped source: ${(s.sources_skipped||[]).join(', ')}. Nothing from skipped sources was marked missing.`;
  } else el.classList.add('hidden');
}

async function openImport(path='') {
  const d = await api('/api/import/browse?path='+encodeURIComponent(path));
  const current = d.path || '/';
  const files=d.files||[];
  $('#modalBody').innerHTML = `<h2>Import from NAS</h2>
    <p class="muted">Register existing PDF / EPUB files only. Nothing is copied or duplicated.</p>
    <div class="import-path">📁 ${esc(current)}</div>
    <div class="import-browser">
      ${d.path ? `<button class="import-entry folder" data-import-dir="${esc(d.parent)}">↰ ..</button>` : ''}
      ${d.dirs.map(x=>`<button class="import-entry folder" data-import-dir="${esc(x.path)}">📁 <span>${esc(x.name)}</span></button>`).join('')}
      ${files.map(x=>`<label class="import-entry pdf ${x.indexed?'indexed':''}"><input type="checkbox" data-import-file="${esc(x.path)}" ${x.indexed?'disabled':''}><span>${x.file_type==='epub'?'📘':'📄'} ${esc(x.name)}</span><small>${x.indexed?'Already in library':(x.file_type||'PDF').toUpperCase()}</small></label>`).join('')}
      ${!d.dirs.length&&!files.length?'<div class="empty-mini">No PDF or EPUB files here.</div>':''}
    </div>
    <div class="row import-actions"><button id="importSelectAll">Select all books</button><button id="doImport" class="primary">Import selected</button></div>`;
  showModal();
  $$('[data-import-dir]').forEach(b=>b.onclick=()=>openImport(b.dataset.importDir));
  $('#importSelectAll').onclick=()=>$$('[data-import-file]:not(:disabled)').forEach(x=>x.checked=true);
  $('#doImport').onclick=async()=>{
    const paths=$$('[data-import-file]:checked').map(x=>x.dataset.importFile);
    if(!paths.length)return toast('Select book files first');
    const r=await api('/api/import',{method:'POST',body:{paths}});
    toast(`Imported ${r.added}, restored ${r.restored}, existing ${r.kept}`);
    hideModal(); load();
  };
}


async function showSettings() {
  const cats = await api('/api/categories');
  state.categories = cats;
  let cacheStats={books:0,bytes:0};try{cacheStats=await window.ReaderCache?.stats?.()||cacheStats}catch{}
  const cacheMb=(cacheStats.bytes/1024/1024).toFixed(cacheStats.bytes>100*1024*1024?0:1);
  $('#modalBody').innerHTML = `<h2>Settings</h2>
    <section class="settings-section"><h3>Categories</h3><div id="categoryManager"></div><div class="row"><input id="newCategory" placeholder="New category"><button id="addCategory" class="primary">Add</button></div></section>
    <section class="settings-section"><h3>Missing files</h3><p>${state.missingCount} hidden record(s). Missing paths are kept for ${state.missingRetention} days, then purged automatically.</p><button id="cleanMissing" ${state.missingCount?'':'disabled'}>Clean missing paths now</button></section>
    <section class="settings-section"><h3>Recent reading cache</h3><p>${cacheStats.books} cached book(s) · about ${cacheMb} MB. Short visits expire after 3 hours. Books read for 3+ minutes stay for 24 hours from the latest reading session and are cleaned automatically.</p><button id="clearReadingCache" ${cacheStats.books?'':'disabled'}>Clear Recent Reading Cache</button></section>
    <section class="settings-section"><h3>App cache</h3><p>Reload the current app shell and static assets without touching your library, reading progress, categories, account, or recent reading cache.</p><button id="refreshAppCache">Refresh App Cache</button></section>
    <section class="settings-section"><h3>Account · ${esc(state.username)}</h3><div class="form-grid"><label>Current password<input id="currentPw" type="password" autocomplete="current-password"></label><label>New password<input id="newPw" type="password" autocomplete="new-password" placeholder="10+ characters"></label><label>Confirm new password<input id="confirmPw" type="password" autocomplete="new-password"></label></div><div class="row"><button id="changePw" class="primary">Change password</button><button id="logoutBtn2">Logout</button></div></section>`;
  showModal();
  renderCategoryManager();
  $('#addCategory').onclick=async()=>{
    const name=$('#newCategory').value.trim(); if(!name)return;
    state.categories=await api('/api/categories',{method:'POST',body:{name}}); $('#newCategory').value=''; renderCategoryManager(); load();
  };
  $('#cleanMissing').onclick=async()=>{const r=await api('/api/missing/clean',{method:'POST'});toast(`Cleaned ${r.purged} missing record(s)`);hideModal();load();};
  $('#clearReadingCache').onclick=async()=>{await window.ReaderCache?.clearAll?.();toast('Recent reading cache cleared');hideModal();};
  $('#refreshAppCache').onclick=async()=>{try{if('serviceWorker' in navigator){const regs=await navigator.serviceWorker.getRegistrations();await Promise.all(regs.map(r=>r.unregister()));}if('caches' in window){const keys=await caches.keys();await Promise.all(keys.filter(k=>k.startsWith('nas-pdf-reader')||k.startsWith('nasreader')).map(k=>caches.delete(k)));}}catch{}location.replace('/?refresh='+Date.now());};
  $('#changePw').onclick=async()=>{
    const current=$('#currentPw').value, next=$('#newPw').value, confirm=$('#confirmPw').value;
    if(next!==confirm)return toast('New passwords do not match');
    await api('/api/auth/change-password',{method:'POST',body:{current_password:current,new_password:next}});
    toast('Password changed'); $('#currentPw').value=$('#newPw').value=$('#confirmPw').value='';
  };
  $('#logoutBtn2').onclick=async()=>{await api('/api/auth/logout',{method:'POST'});location.reload();};
}


function renderCategoryManager() {
  const wrap=$('#categoryManager'); if(!wrap)return;
  wrap.innerHTML=state.categories.map(c=>`<div class="category-row" data-cat-id="${c.id}"><input value="${esc(c.name)}" data-cat-name><span>${c.count} books</span><label class="mini-switch"><input type="checkbox" data-cat-visible ${c.visible?'checked':''}> Show</label><button data-cat-save>Save</button><button data-cat-delete class="danger-soft">Delete</button></div>`).join('');
  $$('.category-row').forEach(row=>{
    const id=+row.dataset.catId, cat=state.categories.find(c=>c.id===id);
    row.querySelector('[data-cat-save]').onclick=async()=>{
      const name=row.querySelector('[data-cat-name]').value.trim(), visible=row.querySelector('[data-cat-visible]').checked;
      state.categories=await api(`/api/categories/${id}`,{method:'PATCH',body:{name,visible}}); toast('Category saved'); renderCategoryManager(); load();
    };
    row.querySelector('[data-cat-delete]').onclick=async()=>{
      let move_to=null;
      if(cat.count){
        const choices=state.categories.filter(x=>x.id!==id).map(x=>x.name);
        if(!choices.length)return toast('Create another category first');
        move_to=prompt(`Move ${cat.count} book(s) to which category?\n${choices.join(', ')}`,choices[0]);
        if(!move_to)return;
      } else if(!confirm(`Delete category “${cat.name}”?`)) return;
      try{state.categories=await api(`/api/categories/${id}/delete`,{method:'POST',body:{move_to}});toast('Category deleted');renderCategoryManager();load();}catch(e){toast(e.message)}
    };
  });
}

$('#scanBtn').onclick=async()=>renderScan((await api('/api/scan',{method:'POST'})).scan);
$('#importBtn').onclick=()=>openImport('');
$('#settingsBtn').onclick=showSettings;
$('#selectBtn').onclick=()=>setSelection(!state.selection);
$('#bulkClose').onclick=()=>setSelection(false);
$('#selectVisible').onclick=()=>{state.books.forEach(b=>state.selected.add(b.id));render()};
$$('[data-bulk]').forEach(b=>b.onclick=()=>bulkPrompt(b.dataset.bulk));
$('#modalX').onclick=hideModal;
$('#modal').onclick=e=>{if(e.target===$('#modal'))hideModal()};
$('#search').oninput=debounce(()=>{state.q=$('#search').value;load()},250);
$('#seriesFilter').onchange=()=>{const raw=$('#seriesFilter').value;if(raw){const cut=raw.indexOf('|');state.seriesCategory=decodeURIComponent(cut>=0?raw.slice(0,cut):'');state.series=decodeURIComponent(cut>=0?raw.slice(cut+1):raw);state.view='books';}else{state.series='';state.seriesCategory='';}state.__scrollRestored=false;syncLibraryUrl();load()};
$('#sort').onchange=()=>{state.sort=$('#sort').value;localStorage.setItem('nasreader.sort',state.sort);load()};
$('#favorites').onchange=load;
$('#showArchived').onchange=load;
$('#seriesBackBtn').onclick=()=>{state.series='';state.seriesCategory='';$('#seriesFilter').value='';state.view='series';state.__scrollRestored=false;syncViewButtons();syncLibraryUrl();load()};
$$('#viewToggle button').forEach(b=>b.onclick=()=>setView(b.dataset.view));
function debounce(fn,ms){let t;return()=>{clearTimeout(t);t=setTimeout(fn,ms)}}
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});
authInit();
