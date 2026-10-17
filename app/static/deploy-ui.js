'use strict';
const $=id=>document.getElementById(id);
const expanded=new Set();
let nodes=[],selectedLog=null,loading=false,editingNode=null;
async function api(path,options={}){
 const r=await fetch(path,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...options});
 if(r.status===401){location.href='/static/login.html';throw Error('请先登录');}
 const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'参数无效，请检查输入');return d;
}
function error(message){$('form-error').textContent=message;$('form-error').hidden=false;}
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state={online:'已部署',installing:'处理中',error:'失败',pending:'待执行命令',uninstalling:'卸载中',removed:'已移除',degraded:'隧道异常',offline:'心跳超时'};
const health={waiting:'等待上报',online:'正常',degraded:'检查异常',network_error:'出站网络异常',repairing:'修复中',error:'执行失败',installing:'配置执行中',offline:'心跳超时'};
const probe={unknown:'等待测试',pending:'测试中',ok:'转发正常',failed:'转发失败'};
function timeText(value){return value?new Date(value).toLocaleString('zh-CN'):'尚未上报';}
async function copy(value){try{await navigator.clipboard.writeText(value);}catch{prompt('复制下面内容',value);}}
function showCommand(data){$('install-command').textContent=data.command;$('command-title').textContent=data.agent_bound?'重新接入节点监控':'在节点服务器执行';$('copy-command').textContent='复制安装命令';$('command-dialog').showModal();}
$('close-command').onclick=()=>$('command-dialog').close();
$('copy-command').onclick=async()=>{await copy($('install-command').textContent);$('copy-command').textContent='已复制';};
$('deploy-form').onsubmit=async e=>{
 e.preventDefault();$('form-error').hidden=true;$('submit').disabled=true;
 const body=Object.fromEntries(new FormData(e.target));body.node_port=Number(body.node_port);body.client_port=Number(body.client_port);body.client_address=body.client_address.trim();
 try{const result=await api('/api/agent-nodes',{method:'POST',body:JSON.stringify(body)});showCommand(result);await load();}
 catch(e){error(e.message);}finally{$('submit').disabled=false;}
};
async function loadLogs(){
 if(!selectedLog)return;const list=await api(`/api/nodes/${selectedLog}/logs`);$('logs-title').textContent=`任务 #${selectedLog} 日志`;
 const pre=$('logs'),bottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<50;
 pre.textContent=list.map(l=>`${l.created_at}  ${l.line}`).join('\n')||'等待节点输出…';$('logs-panel').hidden=false;if(bottom)pre.scrollTop=pre.scrollHeight;
}
function busy(n){return n.management==='agent'?n.job_status==='running':n.status==='installing';}
function actions(n,deployed=false){
 if(n.deleting)return '<p class="meta">等待节点执行卸载并确认清理；离线节点恢复连接后自动处理。</p>';
 const argo=n.protocol==='vless-ws-argo',running=busy(n),managed=n.management==='agent'&&n.agent_bound;
 return `<div class="task-actions">${deployed?`<button class="ghost" data-action="copy" data-id="${n.id}">复制 VLESS 链接</button>`:''}<button class="ghost" data-action="logs" data-id="${n.id}">查看日志</button>${argo&&!running?`<button class="ghost" data-action="edit" data-id="${n.id}">编辑</button>`:''}${argo&&!managed?`<button class="ghost" data-action="enroll" data-id="${n.id}">${n.share_link?'接入监控':'安装命令'}</button>`:''}${managed&&!running?`<button class="ghost" data-action="refresh-link" data-id="${n.id}">同步最新链接</button>${n.share_link?`<button class="ghost" data-action="probe" data-id="${n.id}" ${n.probe_status==='pending'?'disabled':''}>${n.probe_status==='pending'?'测活中':'立即测活'}</button>`:''}${n.job_status==='failed'?`<button class="ghost" data-action="retry" data-id="${n.id}">重试任务</button>`:''}`:''}${!running?`<button class="ghost" data-action="remove" data-id="${n.id}">删除节点</button>`:''}</div>`;
}
function healthCard(n){
 if(n.management!=='agent'||!n.agent_bound)return '<p class="meta">尚未接入主动监控，当前链接状态未经实时确认。</p>';
 const ok=n.health_status==='online';
 return `<div class="health-grid"><span>节点上报</span><strong class="${ok?'health-ok':'health-bad'}">${escape(health[n.health_status]||n.health_status)}</strong><span>本地 / 公网 WS</span><strong>${n.local_ws?'正常':'未通过'} / ${n.public_ws?'正常':'未通过'}</strong><span>面板实际转发</span><strong class="${n.probe_status==='ok'?'health-ok':n.probe_status==='failed'?'health-bad':''}">${escape(probe[n.probe_status]||n.probe_status)}${n.latency_ms!=null&&n.probe_status==='ok'?' · '+n.latency_ms+' ms':''}</strong><span>最后心跳</span><strong>${escape(timeText(n.last_report_at))}</strong><span>最近转发测试</span><strong>${escape(n.probe_at?timeText(n.probe_at):'尚未测试')}</strong><span>自动修复次数</span><strong>${Number(n.repair_count)||0}</strong></div>${n.probe_error&&n.probe_status==='failed'?`<p class="error">${escape(n.probe_error)}</p>`:''}`;
}
async function load(){
 if(loading)return;loading=true;
 try{
 document.querySelectorAll('.task-details').forEach(d=>{if(d.open)expanded.add(d.dataset.key);else expanded.delete(d.dataset.key);});
 nodes=await api('/api/nodes');const deployed=nodes.filter(n=>n.share_link);$('node-count').textContent=deployed.length;
 $('deployed-nodes').innerHTML=deployed.length?deployed.map(n=>`<article class="task"><details class="task-details" data-key="deployed-${n.id}" ${expanded.has(`deployed-${n.id}`)?'open':''}><summary><h3>${escape(n.remark)}</h3><span class="badge ${escape(n.status)}">${escape(state[n.status]||n.status)}</span></summary><dl class="node-params"><div><dt>服务器 / 本地监听端口</dt><dd>${escape(n.host_ip||n.hostname||'等待连接')} · ${n.node_port}</dd></div><div><dt>优选地址 / 客户端端口</dt><dd>${escape(n.client_address||n.sni)} · ${n.client_port||443}</dd></div><div><dt>TLS SNI / WS Host</dt><dd>${escape(n.sni)}</dd></div><div><dt>协议</dt><dd>${n.protocol==='vless-ws-argo'?'VLESS-WS + Argo':'已有节点'}</dd></div></dl>${n.desired_port&&n.desired_port!==n.node_port?`<p class="pending-port">等待节点应用新监听端口：${n.desired_port}</p>`:''}${healthCard(n)}<div class="link">${escape(n.share_link)}</div>${n.last_error?`<div class="error">${escape(n.last_error)}</div>`:''}${actions(n,true)}</details></article>`).join(''):'<p class="empty">执行安装命令后，节点和链接将自动显示在这里。</p>';
 $('tasks').innerHTML=nodes.length?nodes.map(n=>`<article class="task"><details class="task-details" data-key="task-${n.id}" ${expanded.has(`task-${n.id}`)?'open':''}><summary><h3>${escape(n.remark)}</h3><span class="badge ${escape(n.status)}">${escape(state[n.status]||n.status)}</span></summary><p class="meta">${escape(n.host_ip||n.hostname||'等待节点执行安装命令')} · 本地端口 ${n.node_port}${n.management==='agent'?' · 配置版本 '+n.applied_revision+'/'+n.desired_revision:''}</p>${n.last_error?`<div class="error">${escape(n.last_error)}</div>`:''}${actions(n)}</details></article>`).join(''):'<p class="empty">填写上方配置，生成第一条安装命令。</p>';
 await loadLogs();
 }finally{loading=false;}
}
function openEditor(n){editingNode=n;$('edit-remark').value=n.remark;$('edit-node-port').value=n.desired_port||n.node_port;$('edit-client-port').value=n.client_port||443;$('edit-address').value=n.client_address||'';$('edit-error').hidden=true;$('edit-dialog').showModal();}
$('close-edit').onclick=()=>$('edit-dialog').close();
$('edit-form').onsubmit=async e=>{
 e.preventDefault();if(!editingNode)return;const draft=Object.fromEntries(new FormData(e.target));draft.node_port=Number(draft.node_port);draft.client_port=Number(draft.client_port);$('edit-error').hidden=true;$('save-edit').disabled=true;
 try{await api(`/api/agent-nodes/${editingNode.id}`,{method:'PATCH',body:JSON.stringify(draft)});$('edit-dialog').close();await load();}
 catch(e){$('edit-error').textContent=e.message;$('edit-error').hidden=false;}finally{$('save-edit').disabled=false;}
};
async function onAction(e){
 const button=e.target.closest('[data-action]');if(!button)return;const n=nodes.find(x=>x.id===Number(button.dataset.id));if(!n)return;
 button.disabled=true;
 try{
 switch(button.dataset.action){
 case 'copy':await copy(n.share_link);button.textContent='已复制';break;
 case 'logs':selectedLog=n.id;await loadLogs();$('logs-panel').scrollIntoView({behavior:'smooth',block:'nearest'});break;
 case 'edit':openEditor(n);break;
 case 'enroll':showCommand(await api(`/api/agent-nodes/${n.id}/enrollment`,{method:'POST'}));await load();break;
 case 'refresh-link':await api(`/api/agent-nodes/${n.id}/refresh`,{method:'POST'});await load();break;
 case 'retry':await api(`/api/agent-nodes/${n.id}/retry`,{method:'POST'});await load();break;
 case 'probe':{const result=await api(`/api/agent-nodes/${n.id}/probe`,{method:'POST'});if(!result.scheduled)error('测活任务正在运行或执行队列已满，请稍后查看结果');await load();break;}
 case 'remove':if(confirm(`删除「${n.remark}」？将卸载远端节点、监控脚本及相关配置，成功后自动删除记录。离线节点会等待恢复连接。`)){await api(`/api/nodes/${n.id}`,{method:'DELETE'});if(selectedLog===n.id){selectedLog=null;$('logs-panel').hidden=true;}await load();}break;
 }
 }catch(e){error(e.message);}finally{button.disabled=false;}
}
$('tasks').onclick=onAction;$('deployed-nodes').onclick=onAction;
$('close-logs').onclick=()=>{selectedLog=null;$('logs-panel').hidden=true;};
$('refresh').onclick=()=>load().catch(e=>error(e.message));
$('logout').onclick=async()=>{await api('/api/logout',{method:'POST'});location.href='/static/login.html';};
load().catch(e=>error(e.message));setInterval(()=>load().catch(()=>{}),3000);
