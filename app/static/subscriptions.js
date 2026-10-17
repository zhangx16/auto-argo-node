'use strict';
(() => {
 const byId=id=>document.getElementById(id);
 const formats={base64:'通用 Base64',raw:'原始 VLESS 链接',clash:'Clash / Mihomo',singbox:'sing-box'};
 let subscriptions=[],editing=null,loadingSubs=false,lastLoad=0;
 const choices=new Map(),opened=new Set();
 const fail=e=>{byId('subscription-error').textContent=e.message||String(e);byId('subscription-error').hidden=false;};
 function formatOptions(selected='base64'){return Object.entries(formats).map(([k,v])=>`<option value="${k}" ${k===selected?'selected':''}>${v}</option>`).join('');}
 function draw(){
  document.querySelectorAll('.subscription-item').forEach(d=>{if(d.open)opened.add(d.dataset.id);else opened.delete(d.dataset.id);});
  byId('subscription-count').textContent=subscriptions.length;
  byId('subscription-list').innerHTML=subscriptions.length?subscriptions.map(s=>{
   const fmt=choices.get(s.id)||'base64';
   return `<article class="task"><details class="task-details subscription-item" data-id="${s.id}" ${opened.has(String(s.id))?'open':''}><summary><h3>${escape(s.name)}</h3><span class="badge ${s.enabled&&!s.expired?'online':'offline'}">${!s.enabled?'已停用':s.expired?'已到期':s.exportable_count+' 个节点'}</span></summary><p class="meta">已选择 ${s.node_count} 个节点 · 当前可导出 ${s.exportable_count} 个${s.expires_at?' · 到期 '+escape(timeText(s.expires_at)):''}</p><div class="field"><label for="sub-format-${s.id}">订阅格式</label><select id="sub-format-${s.id}" data-sub-format="${s.id}">${formatOptions(fmt)}</select></div><input class="subscription-url" aria-label="${escape(s.name)}订阅地址" value="${escape(s.urls[fmt])}" readonly><div class="task-actions"><button class="ghost" data-sub-action="copy" data-id="${s.id}">复制订阅地址</button><button class="ghost" data-sub-action="preview" data-id="${s.id}">预览内容</button><button class="ghost" data-sub-action="download" data-id="${s.id}">下载配置</button><button class="ghost" data-sub-action="edit" data-id="${s.id}">编辑节点选择</button><button class="ghost" data-sub-action="toggle" data-id="${s.id}">${s.enabled?'停用订阅':'启用订阅'}</button><button class="ghost" data-sub-action="rotate" data-id="${s.id}">重置订阅地址</button><button class="ghost" data-sub-action="delete" data-id="${s.id}">删除订阅</button></div></details></article>`;
  }).join(''):'<p class="empty">创建一个订阅，勾选要导出的节点。</p>';
 }
 async function loadSubscriptions(force=false){
  if(loadingSubs||(!force&&Date.now()-lastLoad<15000))return;
  loadingSubs=true;
  try{subscriptions=await api('/api/subscriptions');lastLoad=Date.now();draw();}catch(e){fail(e);}finally{loadingSubs=false;}
 }
 function countSelected(){byId('sub-selected-count').textContent=byId('sub-node-picker').querySelectorAll('input:checked').length;}
 function openEditor(s=null){
  editing=s;
  byId('subscription-editor-title').textContent=s?'编辑订阅':'创建订阅';
  byId('sub-name').value=s?.name||'';byId('sub-enabled').checked=s?s.enabled:true;
  const dt=s?.expires_at?new Date(s.expires_at):null;
  byId('sub-expires').value=dt?new Date(dt.getTime()-dt.getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  byId('sub-node-search').value='';byId('sub-edit-error').hidden=true;
  const selected=new Set(s?.node_ids||[]);
  const available=nodes.filter(n=>n.share_link&&!['uninstalling','removed'].includes(n.status));
  byId('sub-node-picker').innerHTML=available.length?available.map(n=>`<label class="subscription-node"><input type="checkbox" value="${n.id}" ${selected.has(n.id)?'checked':''}><span><strong>${escape(n.remark)}</strong><small>#${n.id} · ${escape(state[n.status]||n.status)} · ${escape(n.client_address||n.sni)}</small></span></label>`).join(''):'<p class="meta">暂无可选择的已部署节点。部署完成后可再次编辑。</p>';
  countSelected();byId('subscription-editor').showModal();
 }
 byId('new-subscription').onclick=()=>openEditor();
 byId('close-sub-editor').onclick=()=>byId('subscription-editor').close();
 byId('close-sub-preview').onclick=()=>byId('subscription-preview').close();
 byId('copy-sub-preview').onclick=()=>copy(byId('sub-preview-content').textContent);
 byId('sub-node-picker').onchange=countSelected;
 byId('sub-node-search').oninput=e=>{const q=e.target.value.trim().toLowerCase();byId('sub-node-picker').querySelectorAll('label').forEach(l=>{l.hidden=!l.textContent.toLowerCase().includes(q);});};
 byId('sub-select-all').onclick=()=>{byId('sub-node-picker').querySelectorAll('label:not([hidden]) input').forEach(i=>{i.checked=true;});countSelected();};
 byId('sub-select-none').onclick=()=>{byId('sub-node-picker').querySelectorAll('input').forEach(i=>{i.checked=false;});countSelected();};
 byId('subscription-form').onsubmit=async e=>{
  e.preventDefault();byId('save-subscription').disabled=true;byId('sub-edit-error').hidden=true;
  const body={name:byId('sub-name').value.trim(),enabled:byId('sub-enabled').checked,expires_at:byId('sub-expires').value?new Date(byId('sub-expires').value).toISOString():'',node_ids:[...byId('sub-node-picker').querySelectorAll('input:checked')].map(i=>Number(i.value))};
  try{await api(editing?`/api/subscriptions/${editing.id}`:'/api/subscriptions',{method:editing?'PUT':'POST',body:JSON.stringify(body)});byId('subscription-editor').close();await loadSubscriptions(true);}
  catch(e){byId('sub-edit-error').textContent=e.message;byId('sub-edit-error').hidden=false;}finally{byId('save-subscription').disabled=false;}
 };
 byId('subscription-list').onchange=e=>{if(!e.target.dataset.subFormat)return;const id=Number(e.target.dataset.subFormat);choices.set(id,e.target.value);const s=subscriptions.find(s=>s.id===id);e.target.closest('details').querySelector('.subscription-url').value=s.urls[e.target.value];};
 byId('subscription-list').onclick=async e=>{
  const button=e.target.closest('[data-sub-action]');if(!button)return;
  const s=subscriptions.find(s=>s.id===Number(button.dataset.id));if(!s)return;
  const fmt=choices.get(s.id)||'base64';button.disabled=true;byId('subscription-error').hidden=true;
  try{
   switch(button.dataset.subAction){
    case 'copy':await copy(s.urls[fmt]);button.textContent='已复制';break;
    case 'edit':openEditor(s);break;
    case 'preview':{const d=await api(`/api/subscriptions/${s.id}/preview?format=${fmt}`);byId('sub-preview-title').textContent=`${s.name} · ${formats[fmt]}`;byId('sub-preview-content').textContent=d.content;byId('subscription-preview').showModal();break;}
    case 'download':{await api(`/api/subscriptions/${s.id}/preview?format=${fmt}`);const a=document.createElement('a');a.href=`/api/subscriptions/${s.id}/export?format=${fmt}`;a.download='';document.body.appendChild(a);a.click();a.remove();break;}
    case 'toggle':await api(`/api/subscriptions/${s.id}`,{method:'PUT',body:JSON.stringify({name:s.name,node_ids:s.node_ids,enabled:!s.enabled,expires_at:s.expires_at})});await loadSubscriptions(true);break;
    case 'rotate':if(confirm('重置后原订阅地址立即失效，需要在客户端重新导入。继续？')){await api(`/api/subscriptions/${s.id}/rotate`,{method:'POST'});await loadSubscriptions(true);}break;
    case 'delete':if(confirm(`删除订阅「${s.name}」？此操作会使订阅地址失效，节点本身保留。`)){await api(`/api/subscriptions/${s.id}`,{method:'DELETE'});opened.delete(String(s.id));choices.delete(s.id);await loadSubscriptions(true);}break;
   }
  }catch(e){fail(e);}finally{button.disabled=false;}
 };
 byId('subscription-module').addEventListener('toggle',()=>{if(byId('subscription-module').open)loadSubscriptions(true);});
 document.addEventListener('nodes-updated',()=>{if(byId('subscription-module').open)loadSubscriptions();});
 loadSubscriptions(true);
})();
