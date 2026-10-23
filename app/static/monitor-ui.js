'use strict';
// Monitor card/meter structure adapted from monitor-theme-default (MIT).
(()=>{
 const el=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const bytes=n=>{n=Number(n)||0;return n>=1099511627776?(n/1099511627776).toFixed(2)+' TiB':n>=1073741824?(n/1073741824).toFixed(2)+' GiB':n>=1048576?(n/1048576).toFixed(1)+' MiB':(n/1024).toFixed(1)+' KiB';};
 const pct=(n,t)=>t>0?Math.min(100,n/t*100):0;
 const meter=(label,value,detail)=>`<div class="resource-metric"><span>${label}</span><strong>${esc(detail)}</strong><progress max="100" value="${value}" aria-label="${label}"></progress></div>`;
 let loading=false,hosts=[],opened=new Set(),quotaHost=null,groupHost=null,available=true,saving=false,drag=null,scrollFrame=0,filter='';
 try{filter=localStorage.getItem('monitor-host-group')||'';}catch{}
 async function request(url,method='GET',body){const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});if(r.status===401){location.href='/static/login.html';throw Error('请重新登录');}const d=await r.json();if(!r.ok)throw Error(d.detail||'请求失败');return d;}
 async function refresh(){if(loading||saving||drag)return;loading=true;try{
  const d=await request('/api/monitor/hosts');hosts=d.hosts;available=d.available;
  el('monitor-summary').textContent=d.available?`${hosts.length} 台主机 · ${hosts.filter(h=>h.online&&!h.stale).length} 台在线 · 10 秒资源采样`:'Monitor 采集服务暂不可用，旧节点监控仍独立运行';
  window.monitorNodeIds=new Set(hosts.flatMap(h=>h.nodes.filter(n=>n.health_mode==='monitor').map(n=>n.id)));
  document.querySelectorAll('#resources-list > details[data-id]').forEach(row=>{row.hidden=window.monitorNodeIds.has(Number(row.dataset.id));});
  renderHosts();
 }catch(e){el('monitor-notice').textContent=e.message;}finally{loading=false;}}
 function visibleHosts(){return hosts.filter(h=>!filter||(filter==='ungrouped'?!h.group_name:'group:'+h.group_name===filter));}
 function renderHosts(){
  const focused=document.activeElement?.dataset.hostDrag;
  const select=el('monitor-group-filter'),groups=[...new Set(hosts.map(h=>h.group_name).filter(Boolean))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
  if(filter&&filter!=='ungrouped'&&!groups.some(g=>'group:'+g===filter))filter='';
  select.replaceChildren(new Option(`全部服务器（${hosts.length}）`,''),new Option(`未分类（${hosts.filter(h=>!h.group_name).length}）`,'ungrouped'));
  groups.forEach(g=>select.add(new Option(`${g}（${hosts.filter(h=>h.group_name===g).length}）`,'group:'+g)));select.value=filter;
  el('monitor-hosts').innerHTML=visibleHosts().map(h=>{const m=h.metrics||{},health=h.health||{},flag=h.country!=='ZZ'&&/^[A-Z]{2}$/.test(h.country)?String.fromCodePoint(...[...h.country].map(c=>127397+c.charCodeAt(0))):'';return `<article class="monitor-card" data-monitor-host="${esc(h.id)}"><div class="monitor-card-heading"><button type="button" class="ghost monitor-drag" data-host-drag="${esc(h.id)}" aria-label="拖动 ${esc(h.name)} 排序，也可用方向键调整" aria-describedby="monitor-sort-help" title="拖动排序，或用方向键移动"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg></button><h3>${esc(flag+' '+h.name)}</h3><span class="badge ${h.online&&!h.stale?'online':'offline'}">${!available?'采集服务不可用':h.online&&!h.stale?'在线':h.last_seen?'数据过期':'等待接入'}</span></div><p class="meta">${h.group_name?`<span class="monitor-group-label">${esc(h.group_name)}</span> · `:''}${esc(h.os)} · ${esc(h.arch)}${m.uptime?' · 已运行 '+(m.uptime/86400).toFixed(1)+' 天':''}</p><div class="resource-grid">${meter('CPU',m.cpu||0,m.cpu==null?'等待采样':m.cpu.toFixed(1)+'%')}${meter('内存',pct(m.mem_used,m.mem_total),bytes(m.mem_used)+' / '+bytes(m.mem_total))}${meter('磁盘',pct(m.disk_used,m.disk_total),bytes(m.disk_used)+' / '+bytes(m.disk_total))}${trafficUsage(h)}</div><div class="monitor-rates"><span>↓ 接收 <strong>${bytes(m.net_rx)}/s</strong></span><span>↑ 发送 <strong>${bytes(m.net_tx)}/s</strong></span></div><p class="meta">核心 ${bytes(health.singbox_rss)} · 隧道 ${bytes(health.cloudflared_rss)} · 探针 ${bytes(health.probe_rss)}</p><div class="task-actions"><button type="button" class="ghost" data-monitor-group="${esc(h.id)}">分类</button><button type="button" class="ghost" data-monitor-quota="${esc(h.id)}" ${h.traffic?'':'disabled'}>服务器流量总量控制</button><button type="button" class="ghost" data-monitor-bind="${esc(h.id)}">关联同机节点</button><button type="button" class="ghost" data-monitor-uninstall="${esc(h.id)}">卸载探针命令</button></div><details ${opened.has(h.id)?'open':''} data-monitor-services="${esc(h.id)}"><summary>关联协议 · ${h.nodes.length}</summary>${h.nodes.map(n=>`<p class="meta">${esc(n.remark)} · ${n.health_mode==='monitor'?'探针心跳':'旧监控 / 待切换'} · ${n.management_enabled?'远程管理已启用':'手动管理'}</p>`).join('')}</details></article>`;}).join('')||(hosts.length?'<p class="empty">此分类暂无服务器。</p>':'<p class="empty">暂无 Monitor 主机。可在已部署节点的“资源探针”中生成安装命令。</p>');
  if(focused)focusHandle(focused);
 }
 function notice(text,bad=false){el('monitor-notice').textContent=text;el('monitor-notice').className=bad?'error':'meta';}
 function focusHandle(id){[...el('monitor-hosts').querySelectorAll('[data-host-drag]')].find(b=>b.dataset.hostDrag===id)?.focus({preventScroll:true});}
 function cardIds(){return [...el('monitor-hosts').querySelectorAll('[data-monitor-host]')].map(c=>c.dataset.monitorHost);}
 el('monitor-group-filter').onchange=()=>{if(drag||saving)return;filter=el('monitor-group-filter').value;try{localStorage.setItem('monitor-host-group',filter);}catch{}renderHosts();};
 async function saveOrder(ids,focusId){
  saving=true;el('monitor-group-filter').disabled=true;
  try{const d=await request('/api/monitor/hosts/order','PUT',{host_ids:ids});const ranks=new Map(d.host_ids.map((id,i)=>[id,i]));hosts.sort((a,b)=>(ranks.get(a.id)??Infinity)-(ranks.get(b.id)??Infinity));notice('服务器顺序已保存');}
  catch(e){notice('排序保存失败：'+e.message,true);}
  finally{saving=false;el('monitor-group-filter').disabled=false;renderHosts();if(focusId)focusHandle(focusId);}
 }
 function restoreCards(ids){const target=el('monitor-hosts'),cards=new Map([...target.querySelectorAll('[data-monitor-host]')].map(c=>[c.dataset.monitorHost,c]));ids.forEach(id=>{if(cards.has(id))target.append(cards.get(id));});}
 function placeAt(x,y){if(!drag?.active)return;const target=document.elementFromPoint(x,y)?.closest('[data-monitor-host]');if(!target||target===drag.card||target===drag.target)return;drag.target=target;const cards=[...el('monitor-hosts').querySelectorAll('[data-monitor-host]')];el('monitor-hosts').insertBefore(drag.card,cards.indexOf(drag.card)<cards.indexOf(target)?target.nextSibling:target);}
 function autoScroll(){
  if(!drag?.active)return;
  const top=(document.querySelector('.app-header')?.getBoundingClientRect().bottom||0)+48,bottom=innerHeight-64;
  const delta=drag.y<top?-Math.min(18,(top-drag.y)/3):drag.y>bottom?Math.min(18,(drag.y-bottom)/3):0;
  if(delta){window.scrollBy(0,delta);drag.target=null;placeAt(drag.x,drag.y);}
  scrollFrame=requestAnimationFrame(autoScroll);
 }
 function finishDrag(cancel=false){
  if(!drag)return;const d=drag;drag=null;cancelAnimationFrame(scrollFrame);d.card.classList.remove('is-dragging');document.body.classList.remove('monitor-reordering');
  if(cancel){restoreCards(d.before);notice('已取消排序');focusHandle(d.id);return;}
  if(d.active){const ids=cardIds();if(ids.join('|')!==d.before.join('|'))saveOrder(ids,d.id);else focusHandle(d.id);}
 }
 el('monitor-hosts').addEventListener('pointerdown',e=>{
  const handle=e.target.closest('[data-host-drag]');if(!handle||e.button!==0||!e.isPrimary||saving||loading)return;
  e.preventDefault();handle.focus({preventScroll:true});drag={id:handle.dataset.hostDrag,card:handle.closest('[data-monitor-host]'),pointer:e.pointerId,startX:e.clientX,startY:e.clientY,x:e.clientX,y:e.clientY,before:cardIds(),active:false,target:null};
  handle.setPointerCapture(e.pointerId);
 });
 document.addEventListener('pointermove',e=>{
  if(!drag||e.pointerId!==drag.pointer)return;drag.x=e.clientX;drag.y=e.clientY;
  if(!drag.active&&Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)>6){drag.active=true;drag.card.classList.add('is-dragging');document.body.classList.add('monitor-reordering');scrollFrame=requestAnimationFrame(autoScroll);}
  if(drag.active){e.preventDefault();placeAt(e.clientX,e.clientY);}
 },{passive:false});
 document.addEventListener('pointerup',e=>{if(drag&&e.pointerId===drag.pointer)finishDrag();});
 document.addEventListener('pointercancel',e=>{if(drag&&e.pointerId===drag.pointer)finishDrag(true);});
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&drag){e.preventDefault();finishDrag(true);}});
 el('monitor-hosts').addEventListener('keydown',e=>{
  const handle=e.target.closest('[data-host-drag]');if(!handle||saving||loading||drag||!['ArrowUp','ArrowLeft','ArrowDown','ArrowRight','Home','End'].includes(e.key))return;
  e.preventDefault();const ids=cardIds(),at=ids.indexOf(handle.dataset.hostDrag),to=e.key==='Home'?0:e.key==='End'?ids.length-1:Math.max(0,Math.min(ids.length-1,at+(['ArrowUp','ArrowLeft'].includes(e.key)?-1:1)));
  if(at===to)return;ids.splice(to,0,ids.splice(at,1)[0]);restoreCards(ids);saveOrder(ids,handle.dataset.hostDrag);
 });
 el('host-group-close').onclick=()=>el('host-group-dialog').close();
 el('host-group-dialog').addEventListener('close',()=>{groupHost=null;});
 el('host-group-form').onsubmit=async e=>{
  e.preventDefault();const id=groupHost;if(!id)return;el('host-group-save').disabled=true;el('host-group-error').hidden=true;
  try{const d=await request(`/api/monitor/hosts/${id}/group`,'PUT',{group_name:el('host-group-value').value.trim()});const h=hosts.find(h=>h.id===id);if(h)h.group_name=d.group_name;if(groupHost===id)el('host-group-dialog').close();renderHosts();notice('服务器分类已保存');}
  catch(e){if(groupHost===id){el('host-group-error').textContent=e.message;el('host-group-error').hidden=false;}}
  finally{el('host-group-save').disabled=false;}
 };
 el('monitor-hosts').addEventListener('toggle',e=>{const id=e.target.dataset.monitorServices;if(id)e.target.open?opened.add(id):opened.delete(id);},true);
 el('monitor-hosts').addEventListener('click',async e=>{const b=e.target.closest('button');if(!b||drag||saving||b.dataset.hostDrag)return;b.disabled=true;try{
  if(b.dataset.monitorGroup){const h=hosts.find(h=>h.id===b.dataset.monitorGroup);if(!h)return;groupHost=h.id;el('host-group-name').textContent=h.name;el('host-group-value').value=h.group_name||'';el('host-group-options').innerHTML=[...new Set(hosts.map(h=>h.group_name).filter(Boolean))].map(g=>`<option value="${esc(g)}"></option>`).join('');el('host-group-error').hidden=true;el('host-group-save').disabled=false;el('host-group-dialog').showModal();}
  if(b.dataset.monitorQuota){const h=hosts.find(h=>h.id===b.dataset.monitorQuota);if(!h?.traffic)throw Error('流量数据暂不可用，请稍后重试');quotaHost=h.id;const t=h.traffic,unit=t.limit_bytes>=1099511627776?1099511627776:1073741824;el('host-traffic-name').textContent=h.name;el('host-traffic-unit').value=String(unit);el('host-traffic-limit').value=String(t.limit_bytes/unit);el('host-traffic-mode').value=t.count_mode;el('host-traffic-reset').value=t.reset_day;el('host-traffic-error').hidden=true;el('host-traffic-save').disabled=false;el('host-traffic-dialog').showModal();}

  if(b.dataset.monitorBind){const id=prompt('输入同一服务器上要关联的节点编号（可在部署任务查看）');if(!/^\d+$/.test(id||''))return;const d=await request(`/api/monitor/nodes/${id}/enrollment`,'POST',{host_id:b.dataset.monitorBind});showCommand(d);}
  if(b.dataset.monitorUninstall)showCommand({command:'sh /usr/local/lib/argo-deploy/monitor-uninstall.sh'});
 }catch(e){el('monitor-notice').textContent=e.message;}finally{b.disabled=false;}});
 function trafficUsage(h){
  const t=h.traffic;
  let detail='数据暂不可用';
  if(t)detail=t.limit_bytes?`${bytes(t.used_bytes)} / ${bytes(t.limit_bytes)} · ${t.percent.toFixed(2)}%${t.exceeded?' · 已达额度':''}`:`${bytes(t.used_bytes)} · 未设额度`;
  return `<div class="resource-metric" data-node="resources/host-traffic"><span>服务器流量</span><strong>${esc(detail)}</strong>${t?.limit_bytes?`<progress max="100" value="${Math.min(100,t.percent)}" aria-label="服务器已用流量占总额度比例"></progress>`:''}</div>`;
 }
 el('host-traffic-close').onclick=()=>el('host-traffic-dialog').close();
 el('host-traffic-dialog').addEventListener('close',()=>{quotaHost=null;});
 el('host-traffic-form').onsubmit=async e=>{e.preventDefault();const host=quotaHost;if(!host)return;const button=el('host-traffic-save');button.disabled=true;el('host-traffic-error').hidden=true;try{const limit=Math.round(Number(el('host-traffic-limit').value)*Number(el('host-traffic-unit').value));if(!Number.isFinite(limit)||limit<0||limit>2**60)throw Error('请输入有效的服务器流量额度');await request(`/api/monitor/hosts/${host}/traffic`,'PUT',{traffic_limit:limit,traffic_mode:el('host-traffic-mode').value,traffic_reset_day:Number(el('host-traffic-reset').value)});if(quotaHost===host){el('host-traffic-dialog').close();await refresh();}}catch(e){if(quotaHost===host){el('host-traffic-error').textContent=e.message;el('host-traffic-error').hidden=false;}}finally{button.disabled=false;}};
 el('resources-refresh').addEventListener('click',refresh);
 document.addEventListener('module-navigate',e=>{if(e.detail?.id!=='resources-module'&&drag)finishDrag(true);if(e.detail?.id==='resources-module')refresh();});
 setInterval(()=>{if(!document.hidden&&document.body.dataset.activeModule==='resources-module')refresh();},10000);
 refresh();
})();
