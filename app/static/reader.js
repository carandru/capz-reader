const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const id = +location.pathname.split('/').pop();
let book;
let mode = localStorage.getItem('nasreader.reader.mode') || 'single';
let fit = localStorage.getItem('nasreader.reader.fit') || 'page';
let rtl = localStorage.getItem('nasreader.reader.rtl') === '1';
let current = 1, saveTimer = null, started = Date.now(), controlsTimer = null, observer = null;
let pointerStart = null;

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

async function init(){
  book=await api(`/api/books/${id}`);
  if(book.missing) throw new Error('PDF file is currently missing from the NAS.');
  current=book.last_page||1;
  $('#readerTitle').textContent=book.title;
  $('#downloadBtn').href=`/api/books/${id}/file?download=true`;
  $('#pageSlider').max=book.page_count;
  $('#pageSlider').value=current;
  bind(); syncControls(); render(); save(); showControls();
}

function pageImg(p){
  const img=new Image();
  img.className='page-image';
  img.alt=`Page ${p}`;
  img.dataset.page=p;
  img.decoding='async';
  img.src=`/api/books/${id}/page/${p}`;
  return img;
}

function spreadPages(){
  if(current<=1) return [1];
  const first=current%2===0?current:current-1;
  return [first,first+1].filter(p=>p<=book.page_count);
}

function setLoading(v){ $('#loadingPage').classList.toggle('hidden',!v); }

function attachLoadTracking(images){
  if(!images.length){setLoading(false);return;}
  setLoading(true);
  let left=images.length;
  const done=()=>{left--;if(left<=0)setLoading(false)};
  images.forEach(img=>{if(img.complete)done();else{img.addEventListener('load',done,{once:true});img.addEventListener('error',done,{once:true});}});
}

function render(){
  if(observer){observer.disconnect();observer=null;}
  const r=$('#reader');
  r.className=`reader ${mode} fit-${fit}${rtl?' rtl':''}`;
  r.innerHTML='';
  let images=[];
  if(mode==='single'){
    const img=pageImg(current); r.append(img); images=[img];
  } else if(mode==='double'){
    const spread=document.createElement('div'); spread.className='spread';
    const pages=spreadPages();
    images=pages.map(pageImg);
    images.forEach(img=>spread.append(img));
    r.append(spread);
  } else {
    const frag=document.createDocumentFragment();
    for(let p=1;p<=book.page_count;p++){
      const img=pageImg(p); img.loading='lazy'; frag.append(img); images.push(img);
    }
    r.append(frag);
    setTimeout(()=>document.querySelector(`[data-page="${current}"]`)?.scrollIntoView({block:'start'}),60);
    observeVertical();
  }
  attachLoadTracking(mode==='vertical'?images.slice(0,Math.min(3,images.length)):images);
  updateText();
  syncControls();
  preloadNearby();
}

function observeVertical(){
  observer=new IntersectionObserver(entries=>{
    const v=entries.filter(x=>x.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];
    if(v){current=+v.target.dataset.page;updateText();scheduleSave();}
  },{threshold:[.35,.6,.8]});
  document.querySelectorAll('.page-image').forEach(x=>observer.observe(x));
}

function updateText(){
  $('#pageText').textContent=`${current} / ${book.page_count}`;
  $('#pageSlider').value=current;
}

function prevTarget(){
  if(mode!=='double') return current-1;
  if(current<=2) return 1;
  const first=current%2===0?current:current-1;
  return first-2;
}
function nextTarget(){
  if(mode!=='double') return current+1;
  if(current<=1) return Math.min(2,book.page_count);
  const first=current%2===0?current:current-1;
  return Math.min(first+2,book.page_count);
}

function go(p){
  current=Math.max(1,Math.min(book.page_count,p));
  if(mode==='vertical') document.querySelector(`[data-page="${current}"]`)?.scrollIntoView({behavior:'smooth',block:'start'});
  else render();
  updateText();scheduleSave();showControls();
}

function scheduleSave(){clearTimeout(saveTimer);saveTimer=setTimeout(save,500)}
async function save(){
  if(!book)return;
  const sec=Math.min((Date.now()-started)/1000,300);started=Date.now();
  try{await api(`/api/books/${id}/progress`,{method:'POST',body:{page:current,seconds:sec}})}catch{}
}

function syncControls(){
  $$('#modeSegment button').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
  $$('#fitSegment button').forEach(b=>b.classList.toggle('active',b.dataset.fit===fit));
  $$('#dirSegment button').forEach(b=>b.classList.toggle('active',(b.dataset.dir==='rtl')===rtl));
}

function showControls(){
  document.body.classList.remove('controls-hidden');
  clearTimeout(controlsTimer);
  controlsTimer=setTimeout(()=>document.body.classList.add('controls-hidden'),2600);
}
function toggleControls(){
  if(document.body.classList.contains('controls-hidden')) showControls();
  else {clearTimeout(controlsTimer);document.body.classList.add('controls-hidden');}
}

function preloadPage(p){
  if(p<1||p>book.page_count)return;
  const img=new Image();img.src=`/api/books/${id}/page/${p}`;
}
function preloadNearby(){
  if(mode==='double'){
    const p=spreadPages();
    preloadPage(Math.max(1,p[0]-2));preloadPage(Math.min(book.page_count,p[p.length-1]+1));preloadPage(Math.min(book.page_count,p[p.length-1]+2));
  }else{
    preloadPage(current-1);preloadPage(current+1);preloadPage(current+2);
  }
}

function handleReaderTap(e){
  if(e.target.closest('.page-image')===null && mode==='vertical'){toggleControls();return;}
  const x=e.clientX/window.innerWidth;
  if(mode==='vertical'){toggleControls();return;}
  if(x<.25) go(rtl?nextTarget():prevTarget());
  else if(x>.75) go(rtl?prevTarget():nextTarget());
  else toggleControls();
}

function bind(){
  $('#prevBtn').onclick=()=>go(rtl?nextTarget():prevTarget());
  $('#nextBtn').onclick=()=>go(rtl?prevTarget():nextTarget());
  $('#pageSlider').oninput=e=>go(+e.target.value);

  $$('#modeSegment button').forEach(b=>b.onclick=()=>{
    mode=b.dataset.mode;localStorage.setItem('nasreader.reader.mode',mode);render();showControls();
  });
  $$('#fitSegment button').forEach(b=>b.onclick=()=>{
    fit=b.dataset.fit;localStorage.setItem('nasreader.reader.fit',fit);render();showControls();
  });
  $$('#dirSegment button').forEach(b=>b.onclick=()=>{
    rtl=b.dataset.dir==='rtl';localStorage.setItem('nasreader.reader.rtl',rtl?'1':'0');render();showControls();
  });

  $('#readerMoreBtn').onclick=e=>{e.stopPropagation();$('#readerMoreMenu').classList.toggle('hidden');showControls();};
  $('#reader').onclick=handleReaderTap;
  $('#reader').addEventListener('pointerdown',e=>{pointerStart={x:e.clientX,y:e.clientY,time:Date.now()};});
  $('#reader').addEventListener('pointerup',e=>{
    if(!pointerStart||mode==='vertical'){pointerStart=null;return;}
    const dx=e.clientX-pointerStart.x,dy=e.clientY-pointerStart.y,dt=Date.now()-pointerStart.time;pointerStart=null;
    if(dt<550&&Math.abs(dx)>70&&Math.abs(dx)>Math.abs(dy)*1.35){
      e.preventDefault();
      if(dx<0)go(rtl?prevTarget():nextTarget());else go(rtl?nextTarget():prevTarget());
    }
  });

  document.addEventListener('pointerdown',e=>{if(!e.target.closest('#readerMoreMenu')&&!e.target.closest('#readerMoreBtn'))$('#readerMoreMenu').classList.add('hidden');});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)save()});
  window.addEventListener('beforeunload',save);
  window.addEventListener('resize',()=>{if(mode!=='vertical'&&fit==='page')requestAnimationFrame(()=>render());});
}

init().catch(e=>{document.body.innerHTML=`<div style="padding:40px;color:white">${String(e)}</div>`});
