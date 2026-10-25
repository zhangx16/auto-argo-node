'use strict';
(()=>{
 const el=id=>document.getElementById(id);
 if(!el('traffic-workspace'))return;
 let cfg={};
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const bytes=n=>{n=Number(n||0);const u=['B','KiB','MiB','GiB','TiB'];let i=0;while(n>=1024&&i<4){n/=1024;i++;}return `${n.toFixed(i?2:0)} ${u[i]}`;};
 async function request(path,method='GET',body){const r=await fetch(path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});if(r.status===401){location.href='/static/login.html';throw Error('请先登录');}const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'请求失败');return d;}
 function message(s,bad=false){el('telemetry-message').textContent=s;el('telemetry-message').className=bad?'error':'meta';}
 let chartHistory=null,chartRequest=0,chartLayout=null;
 const chartHost=el('traffic-chart'),tooltip=document.createElement('div');
 tooltip.id='traffic-chart-tooltip';tooltip.className='chart-tooltip';tooltip.setAttribute('role','tooltip');tooltip.hidden=true;document.body.append(tooltip);
 function hideTooltip(){tooltip.hidden=true;chartHost.querySelector('.chart-cursor')?.remove();}
 function showTooltip(event){
  if(!chartLayout)return;const svg=chartHost.querySelector('svg');if(!svg)return;
  const r=svg.getBoundingClientRect(),l=chartLayout,x=(event.clientX-r.left)*l.width/r.width,y=(event.clientY-r.top)*250/r.height;
  if(x<l.left||x>l.left+l.w||y<l.top||y>l.top+l.h||!l.points.length){hideTooltip();return;}
  const index=Math.min(l.points.length-1,Math.floor((x-l.left)/l.step)),p=l.points[index];
  tooltip.innerHTML=`<strong>${esc(p.date)} <span>UTC</span></strong>${p.measured?`<div><span>上行（IN）</span><b>${bytes(p.rx)}</b></div><div><span>下行（OUT）</span><b>${bytes(p.tx)}</b></div><div class="chart-tooltip-total"><span>合计</span><b>${bytes(p.rx+p.tx)}</b></div>`:'<p>未采集</p>'}`;
  tooltip.hidden=false;
  const bounds=tooltip.getBoundingClientRect(),gap=12,edge=8;
  const tx=event.clientX+gap+bounds.width>innerWidth-edge?event.clientX-gap-bounds.width:event.clientX+gap;
  const ty=event.clientY+gap+bounds.height>innerHeight-edge||event.pointerType==='touch'?event.clientY-gap-bounds.height:event.clientY+gap;
  tooltip.style.left=Math.max(edge,Math.min(tx,innerWidth-bounds.width-edge))+'px';tooltip.style.top=Math.max(edge,Math.min(ty,innerHeight-bounds.height-edge))+'px';
  let cursor=svg.querySelector('.chart-cursor');if(!cursor){cursor=document.createElementNS('http://www.w3.org/2000/svg','line');cursor.setAttribute('class','chart-cursor');svg.append(cursor);}
  const cx=l.left+l.step*(index+.5);for(const [key,value]of Object.entries({x1:cx,x2:cx,y1:l.top,y2:l.top+l.h}))cursor.setAttribute(key,value);
 }
 chartHost.addEventListener('pointerdown',showTooltip);
 chartHost.addEventListener('pointermove',event=>{if(event.pointerType!=='touch'||event.buttons)showTooltip(event);});
 chartHost.addEventListener('pointerleave',event=>{if(event.pointerType!=='touch')hideTooltip();});
 chartHost.addEventListener('pointercancel',hideTooltip);
 document.addEventListener('pointerdown',event=>{if(!chartHost.contains(event.target))hideTooltip();});
 document.addEventListener('keydown',event=>{if(event.key==='Escape')hideTooltip();});
 document.addEventListener('scroll',hideTooltip,true);
 document.addEventListener('module-navigate',e=>{hideTooltip();if(e.detail?.id==='traffic-module')traffic();});
 function renderChart(){
  hideTooltip();const host=el('traffic-chart');if(!host||!chartHistory)return;
  const points=chartHistory.points||[];const width=Math.max(260,host.clientWidth||600),height=250,left=66,right=12,top=14,bottom=42,w=width-left-right,h=height-top-bottom;
  const max=Math.max(0,...points.map(p=>p.rx+p.tx));const magnitude=10**Math.floor(Math.log10(max||1024));const ceiling=[1,2,5,10].map(n=>n*magnitude).find(n=>n>=max)||magnitude*10;
  const unitIndex=Math.min(4,Math.max(0,Math.floor(Math.log(ceiling)/Math.log(1024)))),unit=1024**unitIndex,units=['B','KiB','MiB','GiB','TiB'];
  const axis=n=>(n/unit).toLocaleString('zh-CN',{maximumFractionDigits:1})+' '+units[unitIndex];
  let marks='';for(let i=0;i<=4;i++){const y=top+h-h*i/4;marks+=`<line class="chart-gridline" x1="${left}" y1="${y}" x2="${width-right}" y2="${y}"/><text class="chart-axis-label" x="${left-10}" y="${y+4}" text-anchor="end">${esc(axis(ceiling*i/4))}</text>`;}
  const step=w/Math.max(points.length,1),bar=Math.max(1,step*.62),ticks=width<500?3:6,labelEvery=Math.max(1,Math.ceil((points.length-1)/ticks));
  points.forEach((p,i)=>{const x=left+step*i+(step-bar)/2,rx=p.rx/ceiling*h,tx=p.tx/ceiling*h,y=top+h;
   marks+=`<g><rect class="chart-in" x="${x}" y="${y-rx}" width="${bar}" height="${rx}"/><rect class="chart-out" x="${x}" y="${y-rx-tx}" width="${bar}" height="${tx}"/>${!p.measured?`<line class="chart-unmeasured" x1="${x}" x2="${x+bar}" y1="${y-1}" y2="${y-1}"/>`:''}</g>`;
   if(i%labelEvery===0||i===points.length-1){const anchor=i===0?'start':i===points.length-1?'end':'middle';marks+=`<text class="chart-axis-label" x="${left+step*(i+.5)}" y="${height-17}" text-anchor="${anchor}">${esc(p.date.slice(5))}</text>`;}
  });
  chartLayout={width,left,top,w,h,step,points};
  const measured=points.filter(p=>p.measured).length,total=points.reduce((n,p)=>n+p.rx+p.tx,0);
  host.innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="traffic-chart-title traffic-chart-desc"><title id="traffic-chart-title">按日流量用量（UTC）</title><desc id="traffic-chart-desc">横轴日期，纵轴用量。黑色为 IN，灰色为 OUT。${points.length} 天合计 ${esc(bytes(total))}，${measured} 天有采集记录。详细值见每日数据表。</desc>${marks}${!max?`<text class="chart-empty" x="${left+w/2}" y="${top+h/2}" text-anchor="middle">${measured?'该时段暂无已统计用量':'尚无采集记录'}</text>`:''}</svg>`;
  el('traffic-chart-status').textContent=`${points[0]?.date||''} — ${points.at(-1)?.date||''} · 合计 ${bytes(total)} · ${measured}/${points.length} 天有记录；未采集日期不视为实际零用量。`;
  el('traffic-chart-data').innerHTML=points.map(p=>`<tr><td>${esc(p.date)}</td><td>${p.measured?bytes(p.rx):'未采集'}</td><td>${p.measured?bytes(p.tx):'未采集'}</td><td>${p.measured?bytes(p.rx+p.tx):'—'}</td></tr>`).join('');
 }
 async function traffic(){const requestId=++chartRequest;try{
  const scope=window.serverWorkspace||window.monitorTrafficScope,days=el('traffic-days').value,node=el('traffic-chart-node').value,params=new URLSearchParams({days});if(scope)params.set('server_id',scope.id);if(node&&(!scope||scope.nodeIds.includes(Number(node))))params.set('node_id',node);
  const d=await request('/api/traffic?'+params);if(requestId!==chartRequest)return;const t=d.totals;if(!scope&&el('stat-today'))el('stat-today').textContent=bytes(t.today_rx+t.today_tx);
  el('traffic-totals').textContent=`今日 ${bytes(t.today_rx+t.today_tx)} · 本月 ${bytes(t.month_rx+t.month_tx)} · 累计 ${bytes(t.rx+t.tx)}`;el('traffic-scope').textContent=d.scope;
  const select=el('traffic-chart-node');select.replaceChildren(new Option(scope?'此服务器全部节点':'全部节点',''));d.nodes.forEach(n=>select.add(new Option(nodeLabel(n),String(n.id))));select.value=d.nodes.some(n=>String(n.id)===node)?node:'';
  chartHistory=d.history||{points:[]};renderChart();
  const opened=new Set([...el('traffic-nodes').querySelectorAll('details[open]')].map(n=>n.dataset.trafficNode));
  el('traffic-nodes').innerHTML=d.nodes.map(n=>`<details class="chain-card" data-traffic-node="${n.id}" ${opened.has(String(n.id))?'open':''}><summary>${esc(nodeLabel(n))} · 今日 ${bytes(n.today_rx+n.today_tx)} · ${n.available?'采集中':'尚未采集 / 已过期'}</summary><p class="meta">今日 IN ${bytes(n.today_rx)} / OUT ${bytes(n.today_tx)}<br>本月 IN ${bytes(n.month_rx)} / OUT ${bytes(n.month_tx)}<br>累计 IN ${bytes(n.rx)} / OUT ${bytes(n.tx)}<br>最后采集 ${n.sampled_at?esc(new Date(n.sampled_at*1000).toLocaleString()):'尚未采集'}</p>${n.quota?.limit_bytes?`<div class="subscription-usage"><div class="usage-heading"><strong>${bytes(n.quota.used_bytes)} / ${bytes(n.quota.limit_bytes)}</strong><span>${n.quota.percent}%${n.quota.exceeded?' · 已超额度':''}</span></div><progress max="100" value="${Math.min(100,n.quota.percent)}" aria-label="本周期流量用量"></progress><p class="meta">结算周期（UTC）：${esc(n.quota.cycle_start)} 至 ${esc(n.quota.cycle_end)}（不含） · 剩余 ${bytes(n.quota.remaining_bytes)} · ${n.quota.measured_days} 天有记录 · 只提醒，不停用</p></div>`:''}<button class="ghost" type="button" data-quota="${n.id}" data-node-name="${esc(n.remark)}">设置流量额度</button>${n.agent_version&&n.agent_version<4?`<button class="ghost" type="button" data-upgrade="${n.id}">更新轻量监控脚本</button>`:''}</details>`).join('')||'<p class="empty">暂无节点</p>';
 }catch(e){message(e.message,true);}}
 document.addEventListener('server-workspace-change',()=>{chartRequest++;chartHistory=null;chartLayout=null;hideTooltip();el('traffic-chart-node').value='';chartHost.textContent='读取当前范围流量…';if(window.serverWorkspace)el('server-dialog').append(tooltip);else document.body.append(tooltip);traffic();});
 document.addEventListener('server-workspace-tab',e=>{if(e.detail.tab==='traffic')traffic();});
 el('traffic-days').onchange=traffic;el('traffic-chart-node').onchange=traffic;
 const observer=new ResizeObserver(()=>renderChart());observer.observe(el('traffic-chart'));
 document.addEventListener('regions-updated',()=>traffic());
 async function settings(){try{cfg=await request('/api/notifications');el('tg-chat').value=cfg.chat_id;document.querySelectorAll('[data-tg-category]').forEach(e=>e.checked=cfg['alerts_'+e.dataset.tgCategory]!==false);el('tg-enabled').checked=cfg.enabled;el('tg-summary').checked=cfg.summary_enabled;el('tg-hours').value=cfg.summary_hours;el('tg-offline').value=cfg.offline_seconds;el('tg-token').placeholder=cfg.configured?'已加密保存，留空保留':'Telegram Bot Token';el('tg-status').textContent=cfg.configured?`@${cfg.bot_username} · 待发送 ${cfg.queued} 条 · ${cfg.last_error||('上次发送 '+(cfg.last_sent||'尚未发送'))}`:'尚未配置 Bot';}catch(e){message(e.message,true);}}
 if(el('tg-form')){el('tg-form').onsubmit=async e=>{e.preventDefault();const b=e.submitter||el('tg-form').querySelector('[type="submit"]');b.disabled=true;try{await request('/api/notifications','PUT',{token:el('tg-token').value,chat_id:el('tg-chat').value.trim(),enabled:el('tg-enabled').checked,summary_enabled:el('tg-summary').checked,summary_hours:Number(el('tg-hours').value),offline_seconds:Number(el('tg-offline').value),...Object.fromEntries([...document.querySelectorAll('[data-tg-category]')].map(e=>['alerts_'+e.dataset.tgCategory,e.checked]))});el('tg-token').value='';await settings();message('通知配置已保存');}catch(e){message(e.message,true);}finally{b.disabled=false;}};
 el('tg-find').onclick=async()=>{try{const chats=await request('/api/notifications/chats','POST');el('tg-chats').innerHTML=chats.map(c=>`<button class="ghost" type="button" data-chat="${esc(c.id)}">${esc(c.name)} (${esc(c.id)})</button>`).join('')||'没有找到聊天，请先向 Bot 发送 /start。';}catch(e){message(e.message,true);}};
 el('tg-chats').onclick=e=>{const b=e.target.closest('[data-chat]');if(b){el('tg-chat').value=b.dataset.chat;message('已选择聊天，请保存配置');}};
 el('tg-test').onclick=async()=>{try{await request('/api/notifications/test','POST');message('测试通知已发送');}catch(e){message(e.message,true);}};
 }
 if(el('traffic-notify'))el('traffic-notify').onclick=async()=>{try{await request('/api/traffic/notify','POST');message('汇总已加入通知队列');}catch(e){message(e.message,true);}};
 el('traffic-refresh').onclick=traffic;
 el('traffic-nodes').onclick=async e=>{const b=e.target.closest('[data-upgrade]');if(!b)return;b.disabled=true;try{await request(`/api/agent-nodes/${b.dataset.upgrade}/upgrade`,'POST');message('更新任务已排队；节点下次连接时执行，不更改协议与端口。');}catch(e){message(e.message,true);b.disabled=false;}};
 if(el('tg-form'))settings();document.addEventListener('monitor-traffic-scope',()=>{el('traffic-chart-node').value='';traffic();});traffic();setInterval(()=>{const r=el('traffic-module')?.getBoundingClientRect()||el('traffic-workspace').getBoundingClientRect();if(!document.hidden&&((r.bottom>0&&r.top<innerHeight)||document.getElementById('overview')?.getBoundingClientRect().bottom>0))traffic();},30000);
})();
