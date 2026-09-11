const $ = s => document.querySelector(s);
const THAI_COLLATOR = new Intl.Collator('th-TH', {numeric:true, sensitivity:'base'});
const compareText = (a='', b='') => THAI_COLLATOR.compare(String(a ?? ''), String(b ?? ''));
const $$ = s => [...document.querySelectorAll(s)];
const id = +location.pathname.split('/').pop();
const FILM_SLOT = 82;
const PDF_WIDTH = 1800;

let book;
let isEpub = false;
let mode = localStorage.getItem('nasreader.reader.mode') || 'single';
let fit = localStorage.getItem('nasreader.reader.fit') || 'page';
let rtl = localStorage.getItem('nasreader.reader.rtl') === '1';
let current = 1;
let saveTimer = null;
let saveChain = Promise.resolve();
let progressDirty = false;
let observer = null;
let verticalLoadObserver = null;
let pointerStart = null;
let dragging = false;
let activeTrack = null;
let suppressClickUntil = 0;
let filmRAF = null;
let sliderPreview = null;
let activityTimer = null;
let pendingSeconds = 0;
let sessionActiveSeconds = 0;
let readingInteractions = 0;
let lastReadingActivity = Date.now();
let lastCacheTouch = 0;
let cacheQualified = false;
let fullPdfCacheRunning = false;
let fullPdfCacheStop = false;
let pageInflight = new Map();
let objectUrls = new Set();
let readerContext = null;
let nextSeriesBook = null;
let endPromptOpen = false;

// EPUB state
let epubZip = null;
let epubOpfPath = '';
let epubManifest = new Map();
let epubManifestByPath = new Map();
let epubSpine = [];
let epubToc = [];
let epubChapterIndex = 0;
let epubScrollRatio = 0;
let epubIframe = null;
let epubPendingFragment = null;
let epubResourceUrls = new Map();
let epubFont = Number(localStorage.getItem('nasreader.epub.font') || 108);
let epubLine = Number(localStorage.getItem('nasreader.epub.line') || 1.65);
let epubTheme = localStorage.getItem('nasreader.epub.theme') || 'paper';

const api = async (url,opt={}) => {
  opt.headers = {...(opt.headers||{}),'X-Requested-With':'NASPDFReader'};
  if(opt.body && typeof opt.body !== 'string'){
    opt.headers['Content-Type']='application/json';
    opt.body=JSON.stringify(opt.body);
  }
  const r=await fetch(url,opt);
  if(!r.ok) throw new Error(await r.text());
  return r.json();
};

const sleep = ms => new Promise(r=>setTimeout(r,ms));
const clamp = (n,a,b) => Math.max(a,Math.min(b,n));


function readReaderContext(){
  try{
    const x=JSON.parse(sessionStorage.getItem('nasreader.readerContext')||'null');
    if(!x||!x.openedAt||Date.now()-x.openedAt>48*60*60*1000)return null;
    return x;
  }catch{return null;}
}
function fallbackSeriesUrl(){
  if(!book?.series)return '/';
  const p=new URLSearchParams();p.set('series',book.series);if(book.category)p.set('category',book.category);p.set('view','books');
  return `/?${p.toString()}`;
}
function setupBackNavigation(){
  const b=$('#readerBackBtn');if(!b)return;
  const fromSeries=readerContext?.from==='series'&&readerContext?.series===book.series;
  b.textContent=fromSeries?'‹ Series':'‹ Library';
  b.href=fromSeries?(readerContext.returnUrl||fallbackSeriesUrl()):'/';
  b.onclick=async e=>{
    e.preventDefault();
    await save();fullPdfCacheStop=true;
    let sameOriginRef=false;
    try{sameOriginRef=!!document.referrer&&new URL(document.referrer).origin===location.origin;}catch{}
    if(sameOriginRef&&history.length>1)history.back();
    else location.href=b.href;
  };
}
async function availableBookFromIds(ids,start){
  for(let i=start;i<ids.length;i++){
    const nextId=Number(ids[i]);if(!nextId||nextId===id)continue;
    try{const b=await api(`/api/books/${nextId}`);if(!b.missing)return b;}catch{}
  }
  return null;
}
async function resolveSeriesNavigation(){
  nextSeriesBook=null;
  if(!book?.series)return;
  let ids=[];
  if(readerContext?.from==='series'&&readerContext.series===book.series&&(readerContext.category||'')===(book.category||'')&&Array.isArray(readerContext.orderedIds)){
    ids=readerContext.orderedIds.map(Number).filter(Number.isFinite);
  }
  let pos=ids.indexOf(id);
  if(pos>=0){nextSeriesBook=await availableBookFromIds(ids,pos+1);return;}
  try{
    const p=new URLSearchParams();p.set('series',book.series);p.set('sort','series');
    if(book.category)p.set('category',book.category);
    const d=await api(`/api/library?${p.toString()}`);
    const live=(d.books||[])
      .filter(x=>(x.series||'')===book.series&&(x.category||'')===(book.category||''))
      .sort((a,b)=>compareText(a.volume||a.title,b.volume||b.title)||compareText(a.title,b.title));
    ids=live.map(x=>x.id);pos=ids.indexOf(id);
    if(pos>=0&&pos<ids.length-1)nextSeriesBook=live[pos+1];
  }catch{}
}
function hideNextVolumePrompt(){
  endPromptOpen=false;$('#nextVolumePrompt')?.classList.add('hidden');
}
function maybePromptNextVolume(){
  if(!nextSeriesBook||endPromptOpen)return false;
  endPromptOpen=true;
  $('#nextVolumeName').textContent=nextSeriesBook.title||'';
  $('#nextVolumePrompt').classList.remove('hidden');
  return true;
}
async function goNextVolume(){
  if(!nextSeriesBook)return;
  await save();fullPdfCacheStop=true;
  if(readerContext){readerContext.openedAt=Date.now();try{sessionStorage.setItem('nasreader.readerContext',JSON.stringify(readerContext));}catch{}}
  location.replace(`/reader/${nextSeriesBook.id}`);
}

function registerObjectUrl(blob){
  const u=URL.createObjectURL(blob);objectUrls.add(u);return u;
}
function revokeObjectUrl(u){
  if(!u)return;
  try{URL.revokeObjectURL(u)}catch{}
  objectUrls.delete(u);
}
function revokeAllObjectUrls(){
  for(const u of objectUrls)revokeObjectUrl(u);
  objectUrls.clear();
}

async function init(){
  book=await api(`/api/books/${id}`);
  if(book.missing) throw new Error('Book file is currently missing from the NAS.');
  isEpub=book.file_type==='epub';
  readerContext=readReaderContext();
  setupBackNavigation();
  await resolveSeriesNavigation();
  $('#readerTitle').textContent=book.title;
  $('#downloadBtn').href=`/api/books/${id}/file?download=true`;
  $('#downloadBtn').textContent=isEpub?'Download EPUB':'Download PDF';
  document.body.classList.toggle('epub-mode',isEpub);
  $('#pdfControls').classList.toggle('hidden',isEpub);
  $('#epubControls').classList.toggle('hidden',!isEpub);
  $('#filmstrip').classList.toggle('hidden',isEpub);
  document.body.classList.add('controls-hidden');

  try{
    const meta=await window.ReaderCache?.touchBook(book);
    cacheQualified=!!meta?.qualified;
  }catch{}

  bindCommon();
  if(isEpub) await initEpub();
  else initPdf();
  syncUiToggle();
  startActivityClock();
  if(cacheQualified && !isEpub) cacheWholePdf();
  window.ReaderCache?.cleanupExpired?.();
  setInterval(()=>window.ReaderCache?.cleanupExpired?.(),30*60*1000);
}

function noteReadingActivity(){
  lastReadingActivity=Date.now();readingInteractions++;
  if(Date.now()-lastCacheTouch>10*60*1000){lastCacheTouch=Date.now();window.ReaderCache?.touchBook?.(book).catch?.(()=>{});}
}
function startActivityClock(){
  activityTimer=setInterval(()=>{
    if(document.hidden)return;
    // Count reading time only while the reader has seen recent interaction.
    if(Date.now()-lastReadingActivity>120000)return;
    pendingSeconds+=5;
    sessionActiveSeconds+=5;
    if(!cacheQualified && sessionActiveSeconds>=180 && readingInteractions>=2) qualifyReadingCache();
  },5000);
}

async function qualifyReadingCache(){
  if(cacheQualified||!book)return;
  cacheQualified=true;
  try{await window.ReaderCache?.qualify(book)}catch{}
  if(!isEpub) cacheWholePdf();
}

function scheduleSave(){
  progressDirty=true;
  if(saveTimer)return;
  saveTimer=setTimeout(()=>{saveTimer=null;save();},6000);
}
function buildProgressPayload(){
  const sec=Math.min(pendingSeconds,3600);pendingSeconds=Math.max(0,pendingSeconds-sec);
  if(isEpub){
    const progress=epubProgressPercent();
    const location=JSON.stringify({chapter:epubChapterIndex,ratio:epubScrollRatio});
    return {sec, body:{page:epubChapterIndex+1,seconds:sec,epub_location:location,epub_progress:progress}};
  }
  return {sec, body:{page:current,seconds:sec}};
}
function save({keepalive=false,force=false}={}){
  if(!book)return Promise.resolve();
  if(saveTimer){clearTimeout(saveTimer);saveTimer=null;}
  if(!progressDirty && pendingSeconds<=0 && !force)return saveChain;
  progressDirty=false;
  const snapshot=buildProgressPayload();
  saveChain=saveChain.catch(()=>{}).then(async()=>{
    try{
      await api(`/api/books/${id}/progress`,{method:'POST',body:snapshot.body,keepalive});
      if(cacheQualified){try{await window.ReaderCache?.touchBook(book)}catch{}}
    }catch{
      pendingSeconds=Math.min(3600,pendingSeconds+snapshot.sec);progressDirty=true;
    }
  });
  return saveChain;
}

function setLoading(v,text){
  $('#loadingPage')?.classList.toggle('hidden',!v);
  if(text)$('#loadingText').textContent=text;
}

function uiVisible(){return !document.body.classList.contains('controls-hidden');}
function syncUiToggle(){
  const b=$('#uiToggleBtn');
  const visible=uiVisible();
  b.classList.toggle('ui-on',visible);
  b.setAttribute('aria-pressed',visible?'true':'false');
  b.setAttribute('aria-label',visible?'Hide reader UI':'Show reader UI');
  b.title=visible?'Hide UI':'Show UI';
}
function showUi(){
  document.body.classList.remove('controls-hidden');syncUiToggle();
  if(!isEpub)syncFilmstrip(true);
}
function hideUi(){
  document.body.classList.add('controls-hidden');
  $('#readerMoreMenu').classList.add('hidden');
  $('#epubToc').classList.add('hidden');
  syncUiToggle();
}
function toggleUi(){uiVisible()?hideUi():showUi();}

function bindCommon(){
  $('#uiToggleBtn').onclick=e=>{e.stopPropagation();toggleUi();};
  $('#nextVolumeStayBtn').onclick=hideNextVolumePrompt;
  $('#nextVolumeGoBtn').onclick=goNextVolume;
  $('#nextVolumePrompt').onclick=e=>{if(e.target===$('#nextVolumePrompt'))hideNextVolumePrompt();};
  $('#readerMoreBtn').onclick=e=>{e.stopPropagation();$('#readerMoreMenu').classList.toggle('hidden');};
  document.addEventListener('pointerdown',e=>{
    if(!e.target.closest('#readerMoreMenu')&&!e.target.closest('#readerMoreBtn'))$('#readerMoreMenu').classList.add('hidden');
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden)save({keepalive:true,force:true});});
  window.addEventListener('beforeunload',()=>{save({keepalive:true,force:true});fullPdfCacheStop=true;revokeAllObjectUrls();});
  window.addEventListener('pagehide',()=>{save({keepalive:true,force:true});fullPdfCacheStop=true;revokeAllObjectUrls();});
}

// ---------------- PDF reader ----------------
function initPdf(){
  current=Math.max(1,Math.min(book.page_count||1,book.last_page||1));
  $('#pageSlider').max=book.page_count||1;
  $('#pageSlider').value=current;
  bindPdf();
  syncPdfControls();
  setupFilmstrip();
  renderPdf();
  progressDirty=true; save({force:true});
}

async function fetchPdfPageBlob(p,width=PDF_WIDTH){
  if(!p||p<1||p>book.page_count)return null;
  const k=`${p}:${width}`;
  if(pageInflight.has(k))return pageInflight.get(k);
  const task=(async()=>{
    let blob=null;
    try{blob=await window.ReaderCache?.getPage(book,p,width)}catch{}
    if(blob)return blob;
    const r=await fetch(`/api/books/${id}/page/${p}?width=${width}&fv=${encodeURIComponent(book.mtime)}`,{cache:'no-store'});
    if(!r.ok)throw new Error(`Unable to load page ${p}`);
    blob=await r.blob();
    try{await window.ReaderCache?.putPage(book,p,width,blob)}catch{}
    return blob;
  })();
  pageInflight.set(k,task);
  try{return await task}finally{pageInflight.delete(k)}
}

function pageImg(p,{strip=false}={}){
  const img=new Image();
  img.className=strip?'film-image':'page-image';
  img.alt=`Page ${p}`;
  img.dataset.page=p;
  img.decoding='async';
  img.draggable=false;
  if(strip){
    img.loading='lazy';
    img.src=`/api/books/${id}/preview/${p}?width=140&fv=${encodeURIComponent(book.mtime)}`;
  }else{
    (async()=>{
      try{
        const blob=await fetchPdfPageBlob(p,PDF_WIDTH);
        if(!blob)return;
        const u=registerObjectUrl(blob);
        img.addEventListener('load',()=>revokeObjectUrl(u),{once:true});
        img.addEventListener('error',()=>revokeObjectUrl(u),{once:true});
        img.src=u;
      }catch{img.alt=`Unable to load page ${p}`;}
    })();
  }
  return img;
}

function spreadPagesFor(p){
  if(p<=1)return [1];
  const first=p%2===0?p:p-1;
  return [first,first+1].filter(x=>x<=book.page_count);
}
function prevTargetFor(p=current){
  if(mode!=='double')return p>1?p-1:null;
  if(p<=1)return null;
  const first=p%2===0?p:p-1;
  return first<=2?1:Math.max(1,first-2);
}
function nextTargetFor(p=current){
  if(mode!=='double')return p<book.page_count?p+1:null;
  if(p<=1)return book.page_count>=2?2:null;
  const first=p%2===0?p:p-1;
  const next=first+2;
  return next<=book.page_count?next:null;
}
function visualLeftTarget(){return rtl?nextTargetFor():prevTargetFor();}
function visualRightTarget(){return rtl?prevTargetFor():nextTargetFor();}

function attachLoadTracking(images){
  if(!images.length){setLoading(false);return;}
  setLoading(true,'Loading page…');
  let left=images.length;
  const done=()=>{left--;if(left<=0)setLoading(false)};
  images.forEach(img=>{
    if(img.complete&&img.src)done();
    else{
      img.addEventListener('load',done,{once:true});
      img.addEventListener('error',done,{once:true});
    }
  });
}
function makePagedContent(p){
  if(!p)return {node:null,images:[]};
  if(mode==='single'){
    const img=pageImg(p);return {node:img,images:[img]};
  }
  const spread=document.createElement('div');spread.className='spread';
  const images=spreadPagesFor(p).map(x=>pageImg(x));
  images.forEach(img=>spread.append(img));
  return {node:spread,images};
}
function makeSlide(p,position){
  const slide=document.createElement('div');slide.className=`pager-slide pager-${position}`;
  if(!p){slide.classList.add('pager-edge');return {slide,images:[]};}
  slide.dataset.target=p;
  const {node,images}=makePagedContent(p);if(node)slide.append(node);
  return {slide,images};
}
function renderPaged(){
  const r=$('#reader');
  r.className=`reader paged ${mode} fit-${fit}${rtl?' rtl':''}`;
  r.innerHTML='';
  const track=document.createElement('div');track.className='pager-track';
  const left=makeSlide(visualLeftTarget(),'left');
  const center=makeSlide(current,'center');
  const right=makeSlide(visualRightTarget(),'right');
  track.append(left.slide,center.slide,right.slide);r.append(track);activeTrack=track;
  attachLoadTracking(center.images);
}
function loadVerticalSlot(slot){
  if(!slot||slot.dataset.loaded==='1'||slot.dataset.loading==='1')return null;
  slot.dataset.loading='1';
  const p=+slot.dataset.page,img=pageImg(p);
  img.addEventListener('load',()=>{slot.dataset.loaded='1';slot.dataset.loading='0';slot.classList.add('loaded');},{once:true});
  img.addEventListener('error',()=>{slot.dataset.loading='0';},{once:true});
  slot.append(img);return img;
}
function renderVertical(){
  const r=$('#reader');r.className=`reader vertical fit-${fit}${rtl?' rtl':''}`;r.innerHTML='';activeTrack=null;
  const frag=document.createDocumentFragment();
  for(let p=1;p<=book.page_count;p++){
    const slot=document.createElement('div');slot.className='vertical-page-shell';slot.dataset.page=p;
    const n=document.createElement('span');n.className='vertical-page-number';n.textContent=p;slot.append(n);frag.append(slot);
  }
  r.append(frag);
  verticalLoadObserver=new IntersectionObserver(entries=>{
    entries.forEach(e=>{if(e.isIntersecting)loadVerticalSlot(e.target);});
  },{root:r,rootMargin:'1400px 0px',threshold:.01});
  r.querySelectorAll('.vertical-page-shell').forEach(x=>verticalLoadObserver.observe(x));
  const target=r.querySelector(`.vertical-page-shell[data-page="${current}"]`);
  const img=loadVerticalSlot(target);if(img)attachLoadTracking([img]);else setLoading(false);
  requestAnimationFrame(()=>target?.scrollIntoView({block:'start'}));
  observeVertical();
}
function renderPdf(){
  if(observer){observer.disconnect();observer=null;}
  if(verticalLoadObserver){verticalLoadObserver.disconnect();verticalLoadObserver=null;}
  if(mode==='vertical')renderVertical();else renderPaged();
  updatePdfText();syncPdfControls();preloadNearby();syncFilmstrip(true);
}
function observeVertical(){
  const r=$('#reader');
  observer=new IntersectionObserver(entries=>{
    const v=entries.filter(x=>x.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];
    if(v&&+v.target.dataset.page!==current){current=+v.target.dataset.page;noteReadingActivity();updatePdfText();syncFilmstrip(false);scheduleSave();}
  },{root:r,threshold:[.35,.6,.8]});
  r.querySelectorAll('.vertical-page-shell').forEach(x=>observer.observe(x));
}

function updatePdfText(){
  const shown=sliderPreview||current;
  $('#pageText').textContent=`${shown} / ${book.page_count}`;
  if(!sliderPreview)$('#pageSlider').value=current;
}
function syncPdfControls(){
  $$('#modeSegment button').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
  $$('#fitSegment button').forEach(b=>b.classList.toggle('active',b.dataset.fit===fit));
  $$('#dirSegment button').forEach(b=>b.classList.toggle('active',(b.dataset.dir==='rtl')===rtl));
}
function preloadPage(p){if(p&&p>=1&&p<=book.page_count)fetchPdfPageBlob(p,PDF_WIDTH).catch(()=>{});}
function preloadNearby(){
  if(mode==='vertical')return;
  if(mode==='double'){
    const pages=spreadPagesFor(current),before=prevTargetFor(),after=nextTargetFor();
    if(before)spreadPagesFor(before).forEach(preloadPage);
    if(after)spreadPagesFor(after).forEach(preloadPage);
    pages.forEach(preloadPage);
  }else{preloadPage(current-1);preloadPage(current+1);preloadPage(current+2);}
}
function goPdf(p,{smoothVertical=true}={}){
  if(p==null)return;
  const next=Math.max(1,Math.min(book.page_count,p));
  if(next===current){updatePdfText();return;}
  current=next;sliderPreview=null;noteReadingActivity();
  if(mode==='vertical')document.querySelector(`[data-page="${current}"]`)?.scrollIntoView({behavior:smoothVertical?'smooth':'auto',block:'start'});
  else renderPdf();
  updatePdfText();scheduleSave();syncFilmstrip(true);
}
function handleReaderTap(e){
  if(Date.now()<suppressClickUntil||dragging||mode==='vertical'||endPromptOpen)return;
  const x=e.clientX/window.innerWidth;
  if(x<.25){
    const target=rtl?nextTargetFor():prevTargetFor();
    if(target==null&&rtl){maybePromptNextVolume();return;}
    goPdf(target);
  }else if(x>.75){
    const target=rtl?prevTargetFor():nextTargetFor();
    if(target==null&&!rtl){maybePromptNextVolume();return;}
    goPdf(target);
  }
}
function resetTrack(animate=true){
  if(!activeTrack)return;
  activeTrack.style.transition=animate?'transform 180ms cubic-bezier(.2,.8,.2,1)':'none';
  activeTrack.style.transform='translate3d(0,0,0)';
}
function finishSwipe(dx,velocity){
  if(!activeTrack)return;
  const threshold=Math.min(120,window.innerWidth*.16);
  const complete=Math.abs(dx)>threshold||Math.abs(velocity)>.55;
  const movingVisualRight=dx<0;
  const target=movingVisualRight?visualRightTarget():visualLeftTarget();
  const forward=(movingVisualRight&&!rtl)||(!movingVisualRight&&rtl);
  if(!complete){resetTrack(true);return;}
  if(!target){resetTrack(true);if(forward)setTimeout(()=>maybePromptNextVolume(),190);return;}
  const sign=dx<0?-1:1;
  activeTrack.style.transition='transform 180ms cubic-bezier(.2,.8,.2,1)';
  activeTrack.style.transform=`translate3d(${sign*window.innerWidth}px,0,0)`;
  const next=target;let finished=false;
  const done=()=>{if(finished)return;finished=true;current=next;sliderPreview=null;noteReadingActivity();renderPdf();scheduleSave();};
  activeTrack.addEventListener('transitionend',done,{once:true});setTimeout(done,260);
}
function pointerDown(e){
  if(mode==='vertical'||e.button>0||e.target.closest('a,button,input'))return;
  noteReadingActivity();
  pointerStart={x:e.clientX,y:e.clientY,time:performance.now(),lastX:e.clientX,lastT:performance.now(),velocity:0};dragging=false;
  try{$('#reader').setPointerCapture(e.pointerId)}catch{}
}
function pointerMove(e){
  if(!pointerStart||mode==='vertical'||!activeTrack)return;
  const dx=e.clientX-pointerStart.x,dy=e.clientY-pointerStart.y;
  if(!dragging){
    if(Math.abs(dx)<8&&Math.abs(dy)<8)return;
    if(Math.abs(dy)>Math.abs(dx)*1.15){pointerStart=null;return;}
    dragging=true;activeTrack.style.transition='none';
  }
  const now=performance.now(),dt=Math.max(1,now-pointerStart.lastT);
  pointerStart.velocity=(e.clientX-pointerStart.lastX)/dt;pointerStart.lastX=e.clientX;pointerStart.lastT=now;
  let x=dx;if((dx>0&&!visualLeftTarget())||(dx<0&&!visualRightTarget()))x*=.28;
  activeTrack.style.transform=`translate3d(${x}px,0,0)`;e.preventDefault();
}
function pointerUp(e){
  if(!pointerStart)return;
  const dx=e.clientX-pointerStart.x,velocity=pointerStart.velocity||0,wasDragging=dragging;
  pointerStart=null;dragging=false;
  if(wasDragging){suppressClickUntil=Date.now()+350;e.preventDefault();finishSwipe(dx,velocity);}
}
function setupFilmstrip(){
  const inner=$('#filmstripInner');inner.style.width=`${book.page_count*FILM_SLOT}px`;
  $('#filmstripScroller').addEventListener('scroll',()=>{
    if(filmRAF)return;filmRAF=requestAnimationFrame(()=>{filmRAF=null;renderFilmstripWindow();});
  },{passive:true});renderFilmstripWindow();
}
function renderFilmstripWindow(){
  if(!book||isEpub)return;
  const sc=$('#filmstripScroller'),inner=$('#filmstripInner');
  const start=Math.max(1,Math.floor(sc.scrollLeft/FILM_SLOT)-5);
  const visible=Math.ceil((sc.clientWidth||window.innerWidth)/FILM_SLOT)+10;
  const end=Math.min(book.page_count,start+visible);const wanted=new Set();
  for(let p=start;p<=end;p++)wanted.add(String(p));
  [...inner.children].forEach(el=>{if(!wanted.has(el.dataset.page))el.remove();});
  for(let p=start;p<=end;p++){
    let b=inner.querySelector(`[data-page="${p}"]`);
    if(!b){
      b=document.createElement('button');b.type='button';b.className='film-thumb';b.dataset.page=p;b.style.left=`${(p-1)*FILM_SLOT+5}px`;
      const img=pageImg(p,{strip:true}),n=document.createElement('span');n.textContent=p;b.append(img,n);b.onclick=()=>goPdf(p);inner.append(b);
    }
    b.classList.toggle('active',p===current);
  }
}
function syncFilmstrip(center=false){
  if(!book||isEpub)return;
  const sc=$('#filmstripScroller');
  if(center&&uiVisible()){
    const target=Math.max(0,(current-1)*FILM_SLOT-(sc.clientWidth-FILM_SLOT)/2);sc.scrollTo({left:target,behavior:'smooth'});
  }
  renderFilmstripWindow();
}
function pdfControlStep(which){
  let target,forward=false;
  if(mode==='vertical'){
    target=which==='next'?nextTargetFor():prevTargetFor();forward=which==='next';
  }else if(which==='next'){
    target=rtl?prevTargetFor():nextTargetFor();forward=!rtl;
  }else{
    target=rtl?nextTargetFor():prevTargetFor();forward=rtl;
  }
  if(target==null&&forward){maybePromptNextVolume();return;}
  goPdf(target);
}
function bindPdf(){
  $('#prevBtn').onclick=()=>pdfControlStep('prev');
  $('#nextBtn').onclick=()=>pdfControlStep('next');
  $('#pageSlider').oninput=e=>{sliderPreview=+e.target.value;updatePdfText();};
  $('#pageSlider').onchange=e=>{const p=+e.target.value;sliderPreview=null;goPdf(p);};
  $$('#modeSegment button').forEach(b=>b.onclick=()=>{mode=b.dataset.mode;localStorage.setItem('nasreader.reader.mode',mode);renderPdf();});
  $$('#fitSegment button').forEach(b=>b.onclick=()=>{fit=b.dataset.fit;localStorage.setItem('nasreader.reader.fit',fit);renderPdf();});
  $$('#dirSegment button').forEach(b=>b.onclick=()=>{rtl=b.dataset.dir==='rtl';localStorage.setItem('nasreader.reader.rtl',rtl?'1':'0');renderPdf();});
  $('#reader').onclick=handleReaderTap;
  $('#reader').addEventListener('pointerdown',pointerDown);
  $('#reader').addEventListener('pointermove',pointerMove,{passive:false});
  $('#reader').addEventListener('pointerup',pointerUp,{passive:false});
  $('#reader').addEventListener('pointercancel',()=>{pointerStart=null;dragging=false;resetTrack(true);});
  window.addEventListener('resize',()=>{if(mode!=='vertical')requestAnimationFrame(()=>renderPdf());syncFilmstrip(true);});
}

async function cacheWholePdf(){
  if(fullPdfCacheRunning||!cacheQualified||isEpub)return;
  fullPdfCacheRunning=true;fullPdfCacheStop=false;
  const order=[];
  for(let d=0;d<book.page_count;d++){
    const a=current+d,b=current-d;
    if(a>=1&&a<=book.page_count&&!order.includes(a))order.push(a);
    if(b>=1&&b<=book.page_count&&!order.includes(b))order.push(b);
  }
  try{
    for(const p of order){
      if(fullPdfCacheStop||!cacheQualified)break;
      if(document.hidden){await sleep(1000);continue;}
      // Interactive reading always wins over background whole-book caching.
      if(Date.now()-lastReadingActivity<1400){await sleep(500);continue;}
      let has=false;try{has=await window.ReaderCache?.hasPage(book,p,PDF_WIDTH)}catch{}
      if(!has){try{await fetchPdfPageBlob(p,PDF_WIDTH)}catch{}}
      await sleep(120);
    }
  }finally{fullPdfCacheRunning=false;}
}

// ---------------- EPUB reader ----------------
function epubNormalize(basePath,href){
  href=(href||'').split('#')[0].split('?')[0];
  try{href=decodeURIComponent(href)}catch{}
  if(!href)return basePath;
  if(/^[a-z]+:/i.test(href)||href.startsWith('//'))return href;
  const parts=basePath.split('/');parts.pop();
  for(const seg of href.replace(/\\/g,'/').split('/')){
    if(!seg||seg==='.')continue;
    if(seg==='..')parts.pop();else parts.push(seg);
  }
  return parts.join('/').replace(/^\//,'');
}
function epubHrefParts(basePath,href){
  const raw=href||'';const hash=raw.includes('#')?raw.slice(raw.indexOf('#')+1):'';
  return {path:epubNormalize(basePath,raw),fragment:hash};
}
function mimeForPath(path){
  const ext=(path.split('.').pop()||'').toLowerCase();
  const map={jpg:'image/jpeg',jpeg:'image/jpeg',png:'image/png',gif:'image/gif',webp:'image/webp',svg:'image/svg+xml',css:'text/css',woff:'font/woff',woff2:'font/woff2',ttf:'font/ttf',otf:'font/otf',mp3:'audio/mpeg',mp4:'video/mp4',xhtml:'application/xhtml+xml',html:'text/html'};
  return map[ext]||'application/octet-stream';
}
async function zipText(path){
  const f=epubZip.file(path);if(!f)throw new Error(`Missing EPUB resource: ${path}`);return f.async('text');
}
async function epubResourceUrl(path){
  if(!path||/^[a-z]+:/i.test(path))return path;
  if(epubResourceUrls.has(path))return epubResourceUrls.get(path);
  const f=epubZip.file(path);if(!f)return '';
  const bytes=await f.async('uint8array');
  const media=epubManifestByPath.get(path)?.media||mimeForPath(path);
  const url=registerObjectUrl(new Blob([bytes],{type:media}));
  epubResourceUrls.set(path,url);return url;
}
function releaseEpubChapterResources(){
  for(const u of epubResourceUrls.values())revokeObjectUrl(u);
  epubResourceUrls.clear();
}
async function rewriteCss(css,cssPath){
  const matches=[...css.matchAll(/url\(\s*(['"]?)([^)'"\s]+)\1\s*\)/gi)];
  const replacements=new Map();
  for(const m of matches){
    const raw=m[2];if(!raw||raw.startsWith('data:')||raw.startsWith('#')||/^[a-z]+:/i.test(raw))continue;
    const p=epubNormalize(cssPath,raw);const u=await epubResourceUrl(p);if(u)replacements.set(raw,u);
  }
  for(const [raw,u] of replacements)css=css.split(raw).join(u);
  return css;
}
async function parseEpubBlob(blob){
  if(typeof JSZip==='undefined')throw new Error('EPUB engine did not load.');
  epubZip=await JSZip.loadAsync(blob);
  const container=await zipText('META-INF/container.xml');
  const cdoc=new DOMParser().parseFromString(container,'application/xml');
  const rootfile=[...cdoc.getElementsByTagNameNS('*','rootfile')][0];
  if(!rootfile)throw new Error('Invalid EPUB: package file not found.');
  epubOpfPath=rootfile.getAttribute('full-path');
  const opfText=await zipText(epubOpfPath);
  const odoc=new DOMParser().parseFromString(opfText,'application/xml');
  epubManifest.clear();epubManifestByPath.clear();epubSpine=[];
  for(const item of [...odoc.getElementsByTagNameNS('*','item')]){
    const itemId=item.getAttribute('id'),href=item.getAttribute('href');if(!itemId||!href)continue;
    const rec={id:itemId,path:epubNormalize(epubOpfPath,href),media:item.getAttribute('media-type')||'',properties:item.getAttribute('properties')||''};
    epubManifest.set(itemId,rec);epubManifestByPath.set(rec.path,rec);
  }
  for(const ref of [...odoc.getElementsByTagNameNS('*','itemref')]){
    const item=epubManifest.get(ref.getAttribute('idref'));if(item)epubSpine.push(item.path);
  }
  if(!epubSpine.length)throw new Error('Invalid EPUB: no readable chapters found.');
  epubToc=await parseEpubToc();
}
async function parseEpubToc(){
  const out=[];
  const nav=[...epubManifest.values()].find(x=>x.properties.split(/\s+/).includes('nav'));
  if(nav){
    try{
      const txt=await zipText(nav.path),doc=new DOMParser().parseFromString(txt,'text/html');
      for(const a of [...doc.querySelectorAll('nav a[href], a[href]')]){
        const hp=epubHrefParts(nav.path,a.getAttribute('href'));const idx=epubSpine.indexOf(hp.path);
        if(idx>=0)out.push({label:(a.textContent||'').trim()||`Chapter ${idx+1}`,chapter:idx,fragment:hp.fragment});
      }
    }catch{}
  }
  if(out.length)return dedupeToc(out);
  const ncx=[...epubManifest.values()].find(x=>/dtbncx/i.test(x.media));
  if(ncx){
    try{
      const txt=await zipText(ncx.path),doc=new DOMParser().parseFromString(txt,'application/xml');
      for(const np of [...doc.getElementsByTagNameNS('*','navPoint')]){
        const label=[...np.getElementsByTagNameNS('*','text')][0]?.textContent?.trim();
        const src=[...np.getElementsByTagNameNS('*','content')][0]?.getAttribute('src');if(!src)continue;
        const hp=epubHrefParts(ncx.path,src),idx=epubSpine.indexOf(hp.path);
        if(idx>=0)out.push({label:label||`Chapter ${idx+1}`,chapter:idx,fragment:hp.fragment});
      }
    }catch{}
  }
  if(!out.length)epubSpine.forEach((_,i)=>out.push({label:`Chapter ${i+1}`,chapter:i,fragment:''}));
  return dedupeToc(out);
}
function dedupeToc(items){
  const seen=new Set();return items.filter(x=>{const k=`${x.chapter}|${x.fragment}|${x.label}`;if(seen.has(k))return false;seen.add(k);return true;});
}
function parseStoredEpubLocation(){
  try{
    const x=JSON.parse(book.epub_location||'{}');
    if(Number.isFinite(x.chapter))return {chapter:clamp(Number(x.chapter),0,epubSpine.length-1),ratio:clamp(Number(x.ratio)||0,0,1)};
  }catch{}
  return {chapter:clamp((book.last_page||1)-1,0,epubSpine.length-1),ratio:0};
}
async function loadEpubBlob(){
  let blob=null;try{blob=await window.ReaderCache?.getFile(book)}catch{}
  if(blob)return blob;
  const r=await fetch(`/api/books/${id}/file?fv=${encodeURIComponent(book.mtime)}`,{cache:'no-store'});
  if(!r.ok)throw new Error('Unable to load EPUB file.');
  blob=await r.blob();
  try{await window.ReaderCache?.putFile(book,blob)}catch{}
  return blob;
}
async function initEpub(){
  setLoading(true,'Loading EPUB…');
  const blob=await loadEpubBlob();
  await parseEpubBlob(blob);
  const loc=parseStoredEpubLocation();epubChapterIndex=loc.chapter;epubScrollRatio=loc.ratio;
  $('#pageSlider').min=1;$('#pageSlider').max=epubSpine.length;$('#pageSlider').value=epubChapterIndex+1;
  $('#epubTheme').value=epubTheme;
  bindEpub();renderToc();
  await renderEpubChapter(epubChapterIndex,epubScrollRatio);
  updateEpubText();progressDirty=true;save({force:true});
}
function epubThemeValues(){
  if(epubTheme==='dark')return {bg:'#101214',fg:'#e8e8e8',muted:'#aab0b7',link:'#8ab4f8'};
  if(epubTheme==='sepia')return {bg:'#f1e3c6',fg:'#352d22',muted:'#786a55',link:'#765b2a'};
  return {bg:'#f7f4ed',fg:'#202020',muted:'#77736c',link:'#315f9a'};
}
function epubCustomCss(){
  const t=epubThemeValues();
  return `
    :root{color-scheme:${epubTheme==='dark'?'dark':'light'};}
    html,body{background:${t.bg}!important;color:${t.fg}!important;}
    html{font-size:${epubFont}%!important;}
    body{box-sizing:border-box!important;margin:0 auto!important;padding:clamp(22px,5vw,64px)!important;max-width:920px!important;line-height:${epubLine}!important;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif!important;overflow-wrap:anywhere;}
    p,li,blockquote,div{line-height:${epubLine}!important;}
    img,svg,video{max-width:100%!important;height:auto!important;}
    a{color:${t.link}!important;} hr{border-color:${t.muted}!important;}
    .nasreader-epub-nav{display:flex!important;gap:16px!important;justify-content:space-between!important;margin:60px 0 20px!important;padding-top:24px!important;border-top:1px solid ${t.muted}55!important;}
    .nasreader-epub-nav button{font:inherit!important;padding:12px 18px!important;border-radius:12px!important;border:1px solid ${t.muted}77!important;background:${t.bg}!important;color:${t.fg}!important;}
  `;
}
async function prepareEpubDocument(chapterPath){
  const raw=await zipText(chapterPath);
  const doc=new DOMParser().parseFromString(raw,'text/html');
  doc.querySelectorAll('script,object,embed,iframe').forEach(x=>x.remove());
  for(const link of [...doc.querySelectorAll('link[rel~="stylesheet"][href]')]){
    const p=epubNormalize(chapterPath,link.getAttribute('href'));
    try{
      let css=await zipText(p);css=await rewriteCss(css,p);
      const st=doc.createElement('style');st.textContent=css;link.replaceWith(st);
    }catch{link.remove();}
  }
  for(const st of [...doc.querySelectorAll('style')]){
    try{st.textContent=await rewriteCss(st.textContent||'',chapterPath)}catch{}
  }
  const resourceAttrs=[['img','src'],['source','src'],['audio','src'],['video','src'],['video','poster'],['image','href']];
  for(const [sel,attr] of resourceAttrs){
    for(const el of [...doc.querySelectorAll(`${sel}[${attr}]`)]){
      const rawRef=el.getAttribute(attr);if(!rawRef||rawRef.startsWith('data:')||/^[a-z]+:/i.test(rawRef))continue;
      const u=await epubResourceUrl(epubNormalize(chapterPath,rawRef));if(u)el.setAttribute(attr,u);
    }
  }
  for(const el of [...doc.querySelectorAll('[style]')]){
    try{el.setAttribute('style',await rewriteCss(el.getAttribute('style')||'',chapterPath))}catch{}
  }
  let head=doc.head;if(!head){head=doc.createElement('head');doc.documentElement.prepend(head);}
  const csp=doc.createElement('meta');csp.httpEquiv='Content-Security-Policy';csp.content="default-src 'none'; img-src blob: data:; media-src blob: data:; font-src blob: data:; style-src 'unsafe-inline' blob:;";head.prepend(csp);
  const custom=doc.createElement('style');custom.id='nasreader-style';custom.textContent=epubCustomCss();head.append(custom);
  const nav=doc.createElement('div');nav.className='nasreader-epub-nav';
  const atLast=epubChapterIndex>=epubSpine.length-1;
  const nextControl=atLast?(nextSeriesBook?'<button type="button" data-nas-finish>Finish book ›</button>':''):'<button type="button" data-nas-next>Next chapter ›</button>';
  nav.innerHTML=`<button type="button" data-nas-prev ${epubChapterIndex<=0?'disabled':''}>‹ Previous chapter</button>${nextControl}`;
  doc.body.append(nav);
  return '<!doctype html>'+doc.documentElement.outerHTML;
}
async function renderEpubChapter(index,ratio=0,fragment=''){
  index=clamp(index,0,epubSpine.length-1);epubChapterIndex=index;epubScrollRatio=clamp(ratio||0,0,1);epubPendingFragment=fragment||null;
  setLoading(true,'Loading chapter…');noteReadingActivity();
  const reader=$('#reader');reader.className='reader epub-reader';reader.innerHTML='';releaseEpubChapterResources();
  const iframe=document.createElement('iframe');iframe.className='epub-frame';iframe.setAttribute('sandbox','allow-same-origin');iframe.setAttribute('title',`Chapter ${index+1}`);epubIframe=iframe;reader.append(iframe);
  const chapterPath=epubSpine[index];
  const html=await prepareEpubDocument(chapterPath);
  await new Promise(resolve=>{iframe.onload=resolve;iframe.srcdoc=html;});
  const win=iframe.contentWindow,doc=iframe.contentDocument;
  if(!win||!doc){setLoading(false);return;}
  const syncScroll=()=>{
    const de=doc.scrollingElement||doc.documentElement;const max=Math.max(1,de.scrollHeight-win.innerHeight);
    epubScrollRatio=clamp(win.scrollY/max,0,1);noteReadingActivity();updateEpubText();scheduleSave();
  };
  win.addEventListener('scroll',()=>{clearTimeout(win.__nasScrollTimer);win.__nasScrollTimer=setTimeout(syncScroll,160);},{passive:true});
  doc.querySelector('[data-nas-prev]')?.addEventListener('click',()=>goEpubChapter(index-1,1));
  doc.querySelector('[data-nas-next]')?.addEventListener('click',()=>goEpubChapter(index+1,0));
  doc.querySelector('[data-nas-finish]')?.addEventListener('click',()=>maybePromptNextVolume());
  doc.addEventListener('click',e=>{
    const a=e.target.closest?.('a[href]');if(!a)return;
    const href=a.getAttribute('href')||'';
    if(!href||/^(https?:|mailto:|tel:)/i.test(href))return;
    const hp=epubHrefParts(chapterPath,href),target=epubSpine.indexOf(hp.path);
    if(target>=0){e.preventDefault();if(target===epubChapterIndex&&hp.fragment){doc.getElementById(hp.fragment)?.scrollIntoView();}else goEpubChapter(target,0,hp.fragment);}
  });
  requestAnimationFrame(()=>{
    if(epubPendingFragment){doc.getElementById(epubPendingFragment)?.scrollIntoView();epubPendingFragment=null;}
    else{
      const de=doc.scrollingElement||doc.documentElement;const max=Math.max(0,de.scrollHeight-win.innerHeight);win.scrollTo(0,max*epubScrollRatio);
    }
    syncScroll();setLoading(false);
  });
  $('#pageSlider').value=index+1;updateEpubText();scheduleSave();
}
function epubProgressPercent(){
  if(!epubSpine.length)return 0;
  return clamp(((epubChapterIndex+epubScrollRatio)/epubSpine.length)*100,0,100);
}
function updateEpubText(){
  if(!epubSpine.length)return;
  const pct=epubProgressPercent();
  $('#pageText').textContent=`Chapter ${epubChapterIndex+1} / ${epubSpine.length} · ${pct.toFixed(0)}%`;
  $('#pageSlider').value=epubChapterIndex+1;
}
async function goEpubChapter(index,ratio=0,fragment=''){
  if(index<0||index>=epubSpine.length)return;
  await save();await renderEpubChapter(index,ratio,fragment);
}
function rerenderEpubKeepPosition(){
  const ratio=epubScrollRatio;renderEpubChapter(epubChapterIndex,ratio).catch(e=>console.error(e));
}
function renderToc(){
  const list=$('#epubTocList');list.innerHTML='';
  epubToc.forEach(item=>{
    const b=document.createElement('button');b.type='button';b.textContent=item.label;b.dataset.chapter=item.chapter;
    b.onclick=()=>{goEpubChapter(item.chapter,0,item.fragment);$('#epubToc').classList.add('hidden');};list.append(b);
  });
}
function bindEpub(){
  $('#prevBtn').onclick=()=>goEpubChapter(epubChapterIndex-1,1);
  $('#nextBtn').onclick=()=>{if(epubChapterIndex>=epubSpine.length-1){maybePromptNextVolume();return;}goEpubChapter(epubChapterIndex+1,0);};
  $('#pageSlider').oninput=e=>{$('#pageText').textContent=`Chapter ${+e.target.value} / ${epubSpine.length}`;};
  $('#pageSlider').onchange=e=>goEpubChapter(+e.target.value-1,0);
  $('#fontDownBtn').onclick=()=>{epubFont=clamp(epubFont-8,72,180);localStorage.setItem('nasreader.epub.font',epubFont);rerenderEpubKeepPosition();};
  $('#fontUpBtn').onclick=()=>{epubFont=clamp(epubFont+8,72,180);localStorage.setItem('nasreader.epub.font',epubFont);rerenderEpubKeepPosition();};
  $('#lineDownBtn').onclick=()=>{epubLine=clamp(Math.round((epubLine-.1)*10)/10,1.2,2.4);localStorage.setItem('nasreader.epub.line',epubLine);rerenderEpubKeepPosition();};
  $('#lineUpBtn').onclick=()=>{epubLine=clamp(Math.round((epubLine+.1)*10)/10,1.2,2.4);localStorage.setItem('nasreader.epub.line',epubLine);rerenderEpubKeepPosition();};
  $('#epubTheme').onchange=e=>{epubTheme=e.target.value;localStorage.setItem('nasreader.epub.theme',epubTheme);rerenderEpubKeepPosition();};
  $('#tocBtn').onclick=()=>$('#epubToc').classList.toggle('hidden');
  $('#tocCloseBtn').onclick=()=>$('#epubToc').classList.add('hidden');
  window.addEventListener('resize',()=>{if(epubIframe)updateEpubText();});
}

init().catch(e=>{console.error(e);document.body.innerHTML=`<div style="padding:40px;color:white;white-space:pre-wrap">${String(e)}</div>`});
