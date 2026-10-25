'use strict';
(()=>{
 const sidebar=document.getElementById('app-sidebar'),toggle=document.getElementById('nav-toggle'),scrim=document.getElementById('nav-scrim');
 if(!sidebar||!toggle)return;
 const mobile=()=>matchMedia('(max-width:767px)').matches;
 const navLinks=[...sidebar.querySelectorAll('a[href^="#"]')];
 function closeMobile(restore=false){document.body.classList.remove('mobile-nav-open');scrim.hidden=true;if(mobile()){toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-label','展开导航');}if(restore)toggle.focus();syncInert();}
 function syncInert(){const open=mobile()&&document.body.classList.contains('mobile-nav-open');sidebar.inert=mobile()&&!open;if(open){sidebar.setAttribute('role','dialog');sidebar.setAttribute('aria-modal','true');}else{sidebar.removeAttribute('role');sidebar.removeAttribute('aria-modal');}}
 function sync(){closeMobile();const expanded=mobile()?false:!document.body.classList.contains('sidebar-collapsed');toggle.setAttribute('aria-expanded',String(expanded));toggle.setAttribute('aria-label',expanded?'收起导航':'展开导航');if(!mobile())sidebar.inert=document.body.classList.contains('sidebar-collapsed');}
 toggle.onclick=()=>{if(mobile()){const opening=!document.body.classList.contains('mobile-nav-open');document.body.classList.toggle('mobile-nav-open',opening);scrim.hidden=!opening;toggle.setAttribute('aria-expanded',String(opening));toggle.setAttribute('aria-label',opening?'收起导航':'展开导航');syncInert();if(opening)sidebar.querySelector('a').focus();}else{document.body.classList.toggle('sidebar-collapsed');sync();}};
 scrim.onclick=()=>closeMobile(true);
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('mobile-nav-open'))closeMobile(true);if(e.key==='Tab'&&mobile()&&document.body.classList.contains('mobile-nav-open')){const links=sidebar.querySelectorAll('a[href]:not([hidden])');if(e.shiftKey&&document.activeElement===links[0]){e.preventDefault();links[links.length-1].focus();}else if(!e.shiftKey&&document.activeElement===links[links.length-1]){e.preventDefault();links[0].focus();}}});
 function activate(target){if(['resources-module'].includes(target))target='server-module';document.querySelectorAll('.nav-link').forEach(a=>{const active=a.getAttribute('href')==='#'+target;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','location');else a.removeAttribute('aria-current');});const label=sidebar.querySelector('.nav-link.active span');if(label)document.getElementById('header-page').textContent=label.textContent;}
 const ids=['server-module','overview','deployment-module','subscription-module','traffic-module','operations-module','ssh-module'];
 const surfaces=new Map(ids.map(id=>{const el=document.getElementById(id);return [id,el?.tagName==='SECTION'?el:el?.closest('section.panel')];}));
 function go(id,push=true){
  const oldNode=id==='node-module'||id.startsWith('node-module?');if(oldNode){const node=new URLSearchParams(id.split('?')[1]||'').get('node');if(node)document.dispatchEvent(new CustomEvent('server-node-open',{detail:{node_id:Number(node)}}));}if(id==='task-module'||id==='resources-module'||oldNode)id='server-module';if(!ids.includes(id))id='overview';
  const section=document.getElementById(id);if(!section)return;
  for(const [key,el] of surfaces){if(el){el.hidden=key!==id;el.inert=key!==id;}}
  document.querySelector('.layout > .note')?.setAttribute('hidden','');
  const overviewMetrics=document.querySelector('.overview-grid');if(overviewMetrics)overviewMetrics.hidden=id!=='overview';
  const layout=document.querySelector('main > .layout');if(layout)layout.hidden=id==='overview';

  document.body.dataset.activeModule=id;activate(id);closeMobile();
  if(push&&location.hash!=='#'+id)history.pushState(null,'','#'+id);
  const heading=section.querySelector('h1,h2');if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true});}
  document.dispatchEvent(new CustomEvent('module-navigate',{detail:{id}}));
  window.scrollTo({top:0,behavior:'instant'});
 }
 navLinks.forEach(a=>a.addEventListener('click',e=>{e.preventDefault();go(a.hash.slice(1));}));
 document.querySelector('.intro-create')?.addEventListener('click',e=>{e.preventDefault();go('deployment-module');document.getElementById('remark').focus({preventScroll:true});});
 addEventListener('popstate',()=>go(location.hash.slice(1),false));
 addEventListener('hashchange',()=>go(location.hash.slice(1),false));
 go(location.hash.slice(1)||'overview',false);
 document.addEventListener('nodes-updated',e=>{if(!e.detail)return;for(const key of ['deployed','online','pending']){const n=document.getElementById('stat-'+key);if(n)n.textContent=String(e.detail[key]);}});
 matchMedia('(max-width:767px)').addEventListener('change',sync);sync();
})();
