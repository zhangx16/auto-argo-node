'use strict';
(()=>{
 const el=id=>document.getElementById(id);
 if(!el('traffic-module'))return;
 let cfg={};
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const bytes=n=>{n=Number(n||0);const u=['B','KiB','MiB','GiB','TiB'];let i=0;while(n>=1024&&i<4){n/=1024;i++;}return `${n.toFixed(i?2:0)} ${u[i]}`;};
 async function request(path,method='GET',body){const r=await fetch(path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});if(r.status===401){location.href='/static/login.html';throw Error('请先登录');}const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'请求失败');return d;}
 function message(s,bad=false){el('telemetry-message').textContent=s;el('telemetry-message').className=bad?'error':'meta';}
 async function traffic(){try{const d=await request('/api/traffic');const t=d.totals;el('traffic-totals').innerHTML=`今日 ${bytes(t.today_rx+t.today_tx)} · 本月 ${bytes(t.month_rx+t.month_tx)} · 累计 ${bytes(t.rx+t.tx)}`;el('traffic-scope').textContent=d.scope;el('traffic-nodes').innerHTML=d.nodes.map(n=>`<details class="chain-card"><summary>${esc(n.remark)} · 今日 ${bytes(n.today_rx+n.today_tx)} · ${n.available?'采集中':'尚未采集 / 已过期'}</summary><p class="meta">今日 IN ${bytes(n.today_rx)} / OUT ${bytes(n.today_tx)}<br>本月 IN ${bytes(n.month_rx)} / OUT ${bytes(n.month_tx)}<br>累计 IN ${bytes(n.rx)} / OUT ${bytes(n.tx)}<br>最后采集 ${n.sampled_at?esc(new Date(n.sampled_at*1000).toLocaleString()):'尚未采集'}</p>${n.agent_version&&n.agent_version<4?`<button class="ghost" type="button" data-upgrade="${n.id}">更新轻量监控脚本</button>`:''}</details>`).join('')||'<p class="empty">暂无节点</p>';}catch(e){message(e.message,true);}}
 async function settings(){try{cfg=await request('/api/notifications');el('tg-chat').value=cfg.chat_id;el('tg-enabled').checked=cfg.enabled;el('tg-summary').checked=cfg.summary_enabled;el('tg-hours').value=cfg.summary_hours;el('tg-offline').value=cfg.offline_seconds;el('tg-token').placeholder=cfg.configured?'已加密保存，留空保留':'Telegram Bot Token';el('tg-status').textContent=cfg.configured?`@${cfg.bot_username} · 待发送 ${cfg.queued} 条 · ${cfg.last_error||('上次发送 '+(cfg.last_sent||'尚未发送'))}`:'尚未配置 Bot';}catch(e){message(e.message,true);}}
 el('tg-form').onsubmit=async e=>{e.preventDefault();const b=e.submitter||el('tg-form').querySelector('[type="submit"]');b.disabled=true;try{await request('/api/notifications','PUT',{token:el('tg-token').value,chat_id:el('tg-chat').value.trim(),enabled:el('tg-enabled').checked,summary_enabled:el('tg-summary').checked,summary_hours:Number(el('tg-hours').value),offline_seconds:Number(el('tg-offline').value)});el('tg-token').value='';await settings();message('通知配置已保存');}catch(e){message(e.message,true);}finally{b.disabled=false;}};
 el('tg-find').onclick=async()=>{try{const chats=await request('/api/notifications/chats','POST');el('tg-chats').innerHTML=chats.map(c=>`<button class="ghost" type="button" data-chat="${esc(c.id)}">${esc(c.name)} (${esc(c.id)})</button>`).join('')||'没有找到聊天，请先向 Bot 发送 /start。';}catch(e){message(e.message,true);}};
 el('tg-chats').onclick=e=>{const b=e.target.closest('[data-chat]');if(b){el('tg-chat').value=b.dataset.chat;message('已选择聊天，请保存配置');}};
 el('tg-test').onclick=async()=>{try{await request('/api/notifications/test','POST');message('测试通知已发送');}catch(e){message(e.message,true);}};
 el('traffic-notify').onclick=async()=>{try{await request('/api/traffic/notify','POST');message('汇总已加入通知队列');}catch(e){message(e.message,true);}};
 el('traffic-refresh').onclick=traffic;
 el('traffic-nodes').onclick=async e=>{const b=e.target.closest('[data-upgrade]');if(!b)return;b.disabled=true;try{await request(`/api/agent-nodes/${b.dataset.upgrade}/upgrade`,'POST');message('更新任务已排队；节点下次连接时执行，不更改协议与端口。');}catch(e){message(e.message,true);b.disabled=false;}};
 settings();traffic();setInterval(()=>{if(el('traffic-module').open)traffic();},30000);
})();
