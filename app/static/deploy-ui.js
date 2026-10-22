'use strict';
const $=id=>document.getElementById(id);
const expanded=new Set();
let nodes=[],selectedLog=null,loading=false,editingNode=null,cloudflareStatus=null;
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
function updateTunnelMode(){
 const argo=$('protocol').value==='vless-ws-argo';
 const fixed=$('tunnel_mode').value==='fixed';
 $('fixed-hostname-field').hidden=!fixed||!argo;$('fixed_hostname').disabled=!fixed||!argo;
 $('tunnel-mode-hint').textContent=fixed?'固定域名在隧道重启后保持不变，面板自动创建隧道和 DNS。':'无需域名；重建后域名可能变化，面板会同步更新链接。';
 $('fixed-domain-hint').textContent=cloudflareStatus?.configured?'可用域名：'+cloudflareStatus.zone_name+'；填写完整子域名，留空自动分配 node-编号.'+cloudflareStatus.zone_name:'尚未配置 Cloudflare 账户，暂无法部署固定隧道。';
 $('fixed_hostname').placeholder=cloudflareStatus?.configured?'例如 kr01.'+cloudflareStatus.zone_name+'，留空自动分配':'留空自动分配';
}
let previousProtocol='vless-ws-argo',savedArgoAddress=$('client_address').value;
function updateProtocol(){
 const kind=$('protocol').value,argo=kind==='vless-ws-argo';
 $('client_port').closest('.field').hidden=!argo;$('client_port').disabled=!argo;
 $('direct-port-field').hidden=argo;$('direct_client_port').disabled=argo;
 $('tunnel_mode').closest('.field').hidden=!argo;$('tunnel_mode').disabled=!argo;
 $('server-name-field').hidden=argo;$('server_name').disabled=argo;
 $('ws_path').closest('.field').hidden=!argo;$('ws_path').disabled=!argo;
 $('client_address').required=!argo;$('node_port').min=argo?'1024':'1';
 $('protocol-hint').textContent=argo?'通过 Cloudflare 隧道连接，无需公网入站映射。':kind==='reality'?'直连 TCP；请放行或映射 TCP 监听端口。':'直连 UDP / QUIC；请放行或映射 UDP 监听端口。';
 $('listen-port-hint').textContent=argo?'隧道访问本机回环地址，无需 NAT 入站映射':kind==='reality'?'服务器 TCP 监听端口':'服务器 UDP 监听端口';
 $('address-label').textContent=argo?'优选域名 / IP':'公网连接地址 / NAT 出口 IP';
 $('address-hint').textContent=argo?'仅修改连接地址，TLS SNI 和 WS Host 自动使用隧道域名':'请填写真实可达的服务器公网 IP 或域名，不使用 Cloudflare 优选地址。';
 $('client_address').placeholder=argo?'留空则使用隧道域名':'例如服务器公网 IP 或节点域名';
 $('tls-mode-hint').textContent=kind==='reality'?'填写支持 TLS 1.3 的伪装站点域名，Reality 密钥自动生成。':'沿用脚本自签名证书；节点链接携带跳过证书验证参数。';
 updateTunnelMode();
}
$('protocol').onchange=()=>{const next=$('protocol').value;if(previousProtocol==='vless-ws-argo'&&next!==previousProtocol){savedArgoAddress=$('client_address').value;$('client_address').value='';}else if(next==='vless-ws-argo')$('client_address').value=savedArgoAddress;previousProtocol=next;$('direct_client_port').value=$('node_port').value;updateProtocol();};
$('tunnel_mode').onchange=updateTunnelMode;
api('/api/cloudflare').then(status=>{cloudflareStatus=status;updateTunnelMode();}).catch(()=>{updateTunnelMode();});
updateProtocol();
$('deploy-form').onsubmit=async e=>{
 e.preventDefault();$('form-error').hidden=true;$('submit').disabled=true;
 const body=Object.fromEntries(new FormData(e.target));body.node_port=Number(body.node_port);body.client_port=Number(body.client_port);body.client_address=body.client_address.trim();
 try{if(body.protocol==='vless-ws-argo'&&body.tunnel_mode==='fixed'&&!cloudflareStatus?.configured)throw Error('请先配置 Cloudflare 账户，再选择固定隧道');if(body.fixed_hostname)body.fixed_hostname=body.fixed_hostname.trim();const result=await api('/api/agent-nodes',{method:'POST',body:JSON.stringify(body)});showCommand(result);await load();}
 catch(e){error(e.message);}finally{$('submit').disabled=false;}
};
async function loadLogs(){
 if(!selectedLog)return;const list=await api(`/api/nodes/${selectedLog}/logs`);$('logs-title').textContent=`任务 #${selectedLog} 日志`;
 const pre=$('logs'),bottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<50;
 pre.textContent=list.map(l=>`${l.created_at}  ${l.line}`).join('\n')||'等待节点输出…';$('logs-panel').hidden=false;if(bottom)pre.scrollTop=pre.scrollHeight;
}
function busy(n){return n.management==='agent'?n.job_status==='running':n.status==='installing';}
function actions(n,deployed=false){
 if(n.deleting)return `<p class="meta">等待节点执行卸载并确认清理；离线节点恢复连接后自动处理。若卸载卡住，可强制移除记录，远端可能残留配置。</p><div class="task-actions"><button class="ghost" data-action="logs" data-id="${n.id}">查看日志</button><button class="ghost" data-action="force-remove" data-id="${n.id}">强制删除</button></div>`;
 const argo=n.protocol==='vless-ws-argo',supported=['vless-ws-argo','reality','hysteria2'].includes(n.protocol),running=busy(n),managed=n.management==='agent'&&n.agent_bound;
 return `<div class="task-actions">${deployed?`<button class="ghost" data-action="copy" data-id="${n.id}">复制节点链接</button>`:''}<button class="ghost" data-action="logs" data-id="${n.id}">查看日志</button>${supported&&!running?`<button class="ghost" data-action="edit" data-id="${n.id}">编辑</button>`:''}${supported&&!managed?`<button class="ghost" data-action="enroll" data-id="${n.id}">${n.share_link?'接入监控':'安装命令'}</button>`:''}${managed&&!running?`<button class="ghost" data-action="refresh-link" data-id="${n.id}">同步最新链接</button>${n.share_link?`<button class="ghost" data-action="probe" data-id="${n.id}" ${n.probe_status==='pending'?'disabled':''}>${n.probe_status==='pending'?'测活中':'立即测活'}</button>`:''}${n.job_status==='failed'?`<button class="ghost" data-action="retry" data-id="${n.id}">重试任务</button>`:''}`:''}${!running?`<button class="ghost" data-action="remove" data-id="${n.id}">删除节点</button>`:''}<button class="ghost" data-action="force-remove" data-id="${n.id}">强制删除</button></div>`;
}
function tunnelBadge(n){
 if(n.protocol==='reality')return '<span class="tunnel-badge tunnel-reality">Reality Vision</span>';
 if(n.protocol==='hysteria2')return '<span class="tunnel-badge tunnel-hysteria2">Hysteria2</span>';
 if(n.protocol!=='vless-ws-argo')return '';
 const fixed=n.tunnel_mode==='fixed';
 return `<span class="tunnel-badge ${fixed?'tunnel-fixed':'tunnel-temp'}">${fixed?'固定隧道':'临时隧道'}</span>`;
}
function healthCard(n){
 if(n.management!=='agent'||!n.agent_bound)return '<p class="meta">尚未接入主动监控，当前链接状态未经实时确认。</p>';
 const ok=n.health_status==='online';
 const direct=n.protocol!=='vless-ws-argo';
 return `<div class="health-grid"><span>节点上报</span><strong class="${ok?'health-ok':'health-bad'}">${escape(health[n.health_status]||n.health_status)}</strong><span>${direct?'本地监听':'本地 / 公网 WS'}</span><strong>${n.local_ws?'正常':'未通过'}${direct?'':' / '+(n.public_ws?'正常':'未通过')}</strong><span>面板实际转发</span><strong class="${n.probe_status==='ok'?'health-ok':n.probe_status==='failed'?'health-bad':''}">${escape(probe[n.probe_status]||n.probe_status)}${n.latency_ms!=null&&n.probe_status==='ok'?' · '+n.latency_ms+' ms':''}</strong><span>最后心跳</span><strong>${escape(timeText(n.last_report_at))}</strong><span>最近转发测试</span><strong>${escape(n.probe_at?timeText(n.probe_at):'尚未测试')}</strong><span>自动修复次数</span><strong>${Number(n.repair_count)||0}</strong></div>${n.probe_error&&n.probe_status==='failed'?`<p class="error">${escape(n.probe_error)}</p>`:''}`;
}
async function load(){
 if(loading)return;loading=true;
 try{
 document.querySelectorAll('.task-details').forEach(d=>{if(d.open)expanded.add(d.dataset.key);else expanded.delete(d.dataset.key);});
 nodes=await api('/api/nodes');const deployed=nodes.filter(n=>n.share_link);$('node-count').textContent=deployed.length;
 $('deployed-nodes').innerHTML=deployed.length?deployed.map(n=>`<article class="task"><details class="task-details" data-key="deployed-${n.id}" ${expanded.has(`deployed-${n.id}`)?'open':''}><summary><h3>${escape(n.remark)}</h3>${tunnelBadge(n)}<span class="badge ${escape(n.status)}">${escape(state[n.status]||n.status)}</span></summary><dl class="node-params"><div><dt>服务器 / 本地监听端口</dt><dd>${escape(n.host_ip||n.hostname||'等待连接')} · ${n.node_port}</dd></div><div><dt>${n.protocol==='vless-ws-argo'?'优选地址 / 客户端端口':'公网地址 / 映射端口'}</dt><dd>${escape(n.client_address||n.sni)} · ${n.client_port||443}</dd></div><div><dt>${n.protocol==='vless-ws-argo'?'TLS SNI / WS Host':'TLS SNI'}</dt><dd>${escape(n.sni)}</dd></div><div><dt>协议</dt><dd>${n.protocol==='vless-ws-argo'?'VLESS-WS + Argo'+(n.tunnel_mode==='fixed'?' · 固定隧道':' · 临时隧道'):n.protocol==='reality'?'VLESS TCP Reality Vision':n.protocol==='hysteria2'?'Hysteria2':'已有节点'}</dd></div></dl>${n.desired_port&&n.desired_port!==n.node_port?`<p class="pending-port">等待节点应用新监听端口：${n.desired_port}</p>`:''}${healthCard(n)}<div class="link">${escape(n.share_link)}</div>${n.last_error?`<div class="error">${escape(n.last_error)}</div>`:''}${actions(n,true)}</details></article>`).join(''):'<p class="empty">执行安装命令后，节点和链接将自动显示在这里。</p>';
 $('tasks').innerHTML=nodes.length?nodes.map(n=>`<article class="task"><details class="task-details" data-key="task-${n.id}" ${expanded.has(`task-${n.id}`)?'open':''}><summary><h3>${escape(n.remark)}</h3>${tunnelBadge(n)}<span class="badge ${escape(n.status)}">${escape(state[n.status]||n.status)}</span></summary><p class="meta">${escape(n.host_ip||n.hostname||'等待节点执行安装命令')} · 本地端口 ${n.node_port}${n.management==='agent'?' · 配置版本 '+n.applied_revision+'/'+n.desired_revision:''}</p>${n.last_error?`<div class="error">${escape(n.last_error)}</div>`:''}${actions(n)}</details></article>`).join(''):'<p class="empty">填写上方配置，生成第一条安装命令。</p>';
 await loadLogs();document.dispatchEvent(new Event('nodes-updated'));
 }finally{loading=false;}
}
function openEditor(n){editingNode=n;$('edit-node-port').min=n.protocol==='vless-ws-argo'?'1024':'1';$('edit-remark').value=n.remark;$('edit-node-port').value=n.desired_port||n.node_port;$('edit-client-port').value=n.client_port||443;$('edit-address').value=n.client_address||'';$('edit-address-label').textContent=n.protocol==='vless-ws-argo'?'优选域名 / IP':'公网连接地址 / NAT 出口 IP';$('edit-node-port').readOnly=n.protocol==='vless-ws-argo'&&n.tunnel_mode==='fixed';$('edit-port-hint').textContent=n.protocol!=='vless-ws-argo'?'名称、公网地址与映射端口立即更新链接；本地端口由节点执行后确认，密钥和认证信息保留。':n.tunnel_mode==='fixed'?'优选地址、客户端端口和名称立即更新链接。固定隧道试运行期间，本地监听端口暂不可修改。':'优选地址、客户端端口和名称立即更新链接。监听端口由节点下次连接时应用；端口修改会生成新的临时隧道链接。';$('edit-error').hidden=true;$('edit-dialog').showModal();}
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
 case 'force-remove':if(confirm(`强制删除「${n.remark}」？\n将立即移除记录、订阅引用并使节点凭据失效，不执行远端或 Cloudflare 清理。\n远端节点、脚本和配置可能仍然存在，需要手动卸载；正在运行的远端任务无法撤销。`)){const result=await api(`/api/nodes/${n.id}?mode=force`,{method:'DELETE'});if(selectedLog===n.id){selectedLog=null;$('logs-panel').hidden=true;}await load();if(result.warning)error(result.warning);}break;
 case 'remove':if(confirm(`删除「${n.remark}」？将卸载远端节点、监控脚本及相关配置，成功后自动删除记录。离线节点会等待恢复连接。`)){await api(`/api/nodes/${n.id}`,{method:'DELETE'});if(selectedLog===n.id){selectedLog=null;$('logs-panel').hidden=true;}await load();}break;
 }
 }catch(e){error(e.message);}finally{button.disabled=false;}
}
$('tasks').onclick=onAction;$('deployed-nodes').onclick=onAction;
$('close-logs').onclick=()=>{selectedLog=null;$('logs-panel').hidden=true;};
$('refresh').onclick=()=>load().catch(e=>error(e.message));
$('logout').onclick=async()=>{await api('/api/logout',{method:'POST'});location.href='/static/login.html';};
load().catch(e=>error(e.message));setInterval(()=>load().catch(()=>{}),3000);
