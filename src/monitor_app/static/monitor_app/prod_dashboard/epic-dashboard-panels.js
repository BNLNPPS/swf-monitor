(() => {
  document.querySelectorAll('.site').forEach(panel => {
    const heading = panel.querySelector('.panelhead');
    const name = heading.querySelector('strong').textContent;
    const button = document.createElement('button');
    button.className = 'site-toggle';
    button.type = 'button';
    button.setAttribute('aria-expanded', String(!panel.classList.contains('closed')));
    button.setAttribute('aria-label', (panel.classList.contains('closed') ? 'Expand ' : 'Collapse ') + name);
    button.innerHTML = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M4 2L8 6L4 10"/></svg>';
    heading.prepend(button);
    button.addEventListener('click', () => {
      const closed = panel.classList.toggle('closed');
      button.setAttribute('aria-expanded', String(!closed));
      button.setAttribute('aria-label', (closed ? 'Expand ' : 'Collapse ') + name);
      if(name.trim()==='OSG'){document.querySelector('.main').classList.toggle('osg-closed',closed);}
    });
  });
})();

function fitFlight(){
 const stage=document.getElementById('stage');
 const scale=stage.getBoundingClientRect().width/1200;
 stage.style.setProperty('--screen-height',(innerHeight/scale)+'px');
}
addEventListener('resize',fitFlight);fitFlight();

const pausedStrip=document.querySelector('.tasks .pauses');
if(pausedStrip){pausedStrip.id='paused-top';document.getElementById('stage').appendChild(pausedStrip);}

if(pausedStrip){
 const tasks=Array.from(pausedStrip.querySelectorAll('.pausechip'));
 const heading=document.createElement('div');heading.className='panelhead';
 const title=document.createElement('strong');title.textContent='Paused tasks';heading.appendChild(title);
 const list=document.createElement('div');list.className='paused-list';tasks.forEach(task=>list.appendChild(task));
 pausedStrip.replaceChildren(heading,list);
}

const flightColumn=document.createElement('div');flightColumn.id='flight-column';document.getElementById('stage').appendChild(flightColumn);
for(const id of ['report','tasks']){const panel=document.getElementById(id);if(panel)flightColumn.appendChild(panel);}

if(pausedStrip)flightColumn.appendChild(pausedStrip);
const campaignStrip=document.createElement('div');
campaignStrip.id='campaign-summary';campaignStrip.className='flowcell';
campaignStrip.innerHTML='<div class="label campaign-title">Campaign —</div><div class="campaign-since small">delivered since —</div><div class="campaign-totals"><b class="green campaign-events">—</b> events <b class="green campaign-bytes">—</b> TB</div><div class="campaign-configs small">— physics configs</div>';
document.getElementById('flow').appendChild(campaignStrip);
document.getElementById('flow').appendChild(document.getElementById('daily-assessment'));

document.querySelectorAll('.plot').forEach(plot=>{
 const axis=plot.querySelector('svg text');
 if(!axis)return;
 const label=document.createElement('span');label.className='chart-y-label';label.textContent=axis.textContent;
 axis.remove();plot.appendChild(label);
});

function fitPausedTasks(){
 if(!pausedStrip)return;
 const list=pausedStrip.querySelector('.paused-list');
 const tasks=Array.from(list.querySelectorAll('.pausechip'));
 list.style.removeProperty('grid-template-columns');
 list.style.display='flex';list.style.flexWrap='wrap';list.style.width='100%';
 list.style.fontSize='11px';list.style.gap='.4em .65em';
 tasks.forEach(task=>{task.style.fontSize='inherit';task.style.lineHeight='1.3';task.style.whiteSpace='nowrap';});
 if(!tasks.length)return;
 const rows=()=>new Set(tasks.map(task=>task.offsetTop)).size;
 if(rows()>2){
  let low=1,high=11;
  for(let i=0;i<10;i++){
   const size=(low+high)/2;list.style.fontSize=size+'px';
   if(rows()>2)high=size;else low=size;
  }
  list.style.fontSize=low+'px';
 }
}
fitPausedTasks();
addEventListener('resize',fitPausedTasks);

const rightColumn=document.createElement('div');rightColumn.id='right-column';document.getElementById('stage').appendChild(rightColumn);
for(const id of ['events','storage'])rightColumn.appendChild(document.getElementById(id));

document.querySelectorAll('#site-controls [data-site-action]').forEach(link=>{
 link.addEventListener('click',event=>{
  event.preventDefault();
  const close=link.dataset.siteAction==='close';
  document.querySelectorAll('.site').forEach(panel=>{
   if(panel.classList.contains('closed')!==close)panel.querySelector('.site-toggle').click();
  });
 });
});

const sitePillarScale=2500;
function renderSitePillars(){
document.querySelectorAll('.site .queuepillar.svgcolumn').forEach(pillar=>{
 const numbers=Array.from(pillar.title.matchAll(/\d[\d,]*/g),match=>Number(match[0].replaceAll(',','')));
 const value=pillar.dataset.value===undefined?(numbers[0]||0):Number(pillar.dataset.value);
 const svg=pillar.querySelector('svg');
 const color=svg.querySelector('path').getAttribute('fill');
 const defs=svg.querySelector('defs').outerHTML;
 const shade=svg.querySelector('linearGradient').id;
 const height=sitePillarHeight(value);
 const body='M0 3H18V'+(height-3)+'Q9 '+(height+3)+' 0 '+(height-3)+'Z';
 let red='';
 let top=color;
 const finished=pillar.dataset.finished===undefined?(numbers[1]||0):Number(pillar.dataset.finished);
 const failures=pillar.dataset.failed===undefined?(numbers[2]||0):Number(pillar.dataset.failed);
 if((pillar.dataset.failed!==undefined||pillar.title.includes('finished'))&&failures>0){
  const failed=pillarRedHeight(value,height,failures/(finished+failures),6);
  // Keep the curved front inside the measured red extent, so a small
  // green remainder stays visible without clipping the curve flat.
  const radius=Math.min(3,failed/2);
  const center=3+failed-radius;
  red=failed>=height-6?'<path d="'+body+'" fill="#f47c70"/>':
   '<rect x="0" y="3" width="18" height="'+(failed-radius)+'" fill="#f47c70"/><ellipse cx="9" cy="'+center+'" rx="9" ry="'+radius+'" fill="#f47c70"/>';
  top='#f47c70';
 }
 setPillarOverflow(pillar,value>sitePillarScale);
 pillar.style.height=height+'px';
 svg.style.overflow='visible';
 svg.setAttribute('height',height);
 svg.setAttribute('viewBox','0 0 18 '+height);
 svg.innerHTML=defs+'<path d="'+body+'" fill="'+color+'"/>'+red+'<path d="'+body+'" fill="url(#'+shade+')"/><ellipse cx="9" cy="3" rx="9" ry="3" fill="'+top+'"/>';
});

}
renderSitePillars();

function setPillarOverflow(pillar,overflow){
 pillar.classList.toggle('pillar-break',overflow);
}
document.querySelectorAll('.cylinder.capped').forEach(pillar=>setPillarOverflow(pillar,true));

function sitePillarHeight(value){
 return Math.min(116,fullPillarHeight(value));
}
function fullPillarHeight(value){
 return Math.max(8,6+110*Math.max(0,value)/sitePillarScale);
}
function pillarRedHeight(value,height,fraction,ends=0){
 // Cut away excess green height without rescaling the complete red segment.
 return Math.min(Math.max(0,height-ends),Math.max(0,fullPillarHeight(value)-ends)*fraction);
}
const execPillarMaxHeight=27;
function renderExecPillars(){
document.querySelectorAll('.exec').forEach(executor=>{
 const pillar=executor.querySelector('.cylinder');
 if(!pillar)return;
 const count=Number(executor.querySelector('.count').textContent.replaceAll(',',''));
 const scaledHeight=sitePillarHeight(count);
 const height=Math.min(execPillarMaxHeight,scaledHeight);
 if(pillar.dataset.failureFraction===undefined){
  const percent=parseFloat(pillar.style.getPropertyValue('--fail'));
  pillar.dataset.failureFraction=String(Number.isFinite(percent)?percent/100:0);
 }
 const fraction=Number(pillar.dataset.failureFraction)||0;
 const extent=height*fraction;
 const radius=Math.min(3,extent/2);
 pillar.style.setProperty('--fail',Math.max(0,extent-radius)+'px');
 pillar.style.setProperty('--red-radius',radius+'px');
 const red=pillar.querySelector('.failurecap');
 if(red){red.style.clipPath='none';red.style.borderRadius='0';red.style.display=fraction>0?'':'none';}
 pillar.style.height=height+'px';
 pillar.classList.remove('capped');
 setPillarOverflow(pillar,scaledHeight>execPillarMaxHeight);
});
document.querySelector('.executeplot').style.height='58px';
}
renderExecPillars();

rightColumn.insertBefore(document.getElementById('report'),document.getElementById('storage'));

(() => {
 const info=document.getElementById('detail');
 let active=null,text='',timer=null,x=0,y=0;
 info.setAttribute('role','tooltip');
 info.style.pointerEvents='none';
 function hide(){
  clearTimeout(timer);info.style.display='none';
  if(active)active.setAttribute('title',text);
  active=null;
 }
 function position(){
  const box=info.getBoundingClientRect();
  info.style.left=Math.max(8,Math.min(x+12,innerWidth-box.width-8))+'px';
  info.style.top=Math.max(8,Math.min(y+14,innerHeight-box.height-8))+'px';
 }
 function schedule(){
  clearTimeout(timer);
  timer=setTimeout(()=>{
   if(!active)return;
   info.textContent=text;
   info.style.right='auto';info.style.bottom='auto';
   info.style.maxWidth=Math.min(440,innerWidth-16)+'px';
   info.style.display='block';position();
  },150);
 }
 document.addEventListener('pointerover',event=>{
  if(active&&active.contains(event.target))return;
  const target=event.target.closest('[title]');
  if(!target||!target.getAttribute('title'))return;
  hide();active=target;text=target.getAttribute('title');
  target.removeAttribute('title');x=event.clientX;y=event.clientY;schedule();
 });
 document.addEventListener('pointermove',event=>{
  if(!active)return;
  x=event.clientX;y=event.clientY;
  if(info.style.display==='block')position();else schedule();
 });
 document.addEventListener('pointerout',event=>{
  if(active&&!active.contains(event.relatedTarget))hide();
 });
 document.addEventListener('click',hide);
 document.addEventListener('scroll',hide,true);
 document.addEventListener('keydown',event=>{if(event.key==='Escape')hide();});
})();
