'use strict';
(() => {
 const byId=id=>document.getElementById(id);
 const formats={base64:'通用 Base64',raw:'原始节点链接',clash:'Clash / Mihomo',singbox:'sing-box'};
 let externalNodes=[],subscriptions=[],editing=null,loadingSubs=false,lastLoad=0,editingChains=[];
 const choices=new Map(),opened=new Set(),externalOpened=new Set();
 const fail=e=>{byId('subscription-error').textContent=e.message||String(e);byId('subscription-error').hidden=false;};
 function formatOptions(selected='base64',allowed=Object.keys(formats)){return Object.entries(formats).filter(([key])=>allowed.includes(key)).map(([k,v])=>`<option value="${k}" ${k===selected?'selected':''}>${v}</option>`).join('');}
 function draw(){
  document.querySelectorAll('.subscription-item').forEach(d=>{if(d.open)opened.add(d.dataset.id);else opened.delete(d.dataset.id);});
  byId('subscription-count').textContent=subscriptions.length;
  byId('subscription-list').innerHTML=subscriptions.length?subscriptions.map(s=>{
   const allowed=s.formats||Object.keys(s.urls);let fmt=choices.get(s.id)||(allowed.includes('base64')?'base64':allowed[0])||'base64';if(!allowed.includes(fmt))fmt=allowed[0];choices.set(s.id,fmt);
   return `<article class="task"><details class="task-details subscription-item" data-id="${s.id}" ${opened.has(String(s.id))?'open':''}><summary><h3>${escape(s.name)}</h3><span class="badge ${s.enabled&&!s.expired?'online':'offline'}">${!s.enabled?'已停用':s.expired?'已到期':s.exportable_count+' 个节点'}</span></summary><p class="meta">已选择 ${s.node_count} 个节点 · 当前可导出 ${s.exportable_count} 个${s.chain_count?' · 链式 '+s.chain_count+' 条':''}${s.expires_at?' · 到期 '+escape(timeText(s.expires_at)):''}</p>${s.chain_errors?.length?`<p class="error">${escape(s.chain_errors.join('；'))}</p>`:''}<div class="field"><label for="sub-format-${s.id}">订阅格式</label><select id="sub-format-${s.id}" data-sub-format="${s.id}">${formatOptions(fmt,allowed)}</select></div><input class="subscription-url" aria-label="${escape(s.name)}订阅地址" value="${escape(s.urls[fmt])}" readonly><div class="task-actions"><button class="ghost" data-sub-action="copy" data-id="${s.id}">复制订阅地址</button><button class="ghost" data-sub-action="preview" data-id="${s.id}">预览内容</button><button class="ghost" data-sub-action="download" data-id="${s.id}">下载配置</button><button class="ghost" data-sub-action="edit" data-id="${s.id}">编辑节点选择</button><button class="ghost" data-sub-action="toggle" data-id="${s.id}">${s.enabled?'停用订阅':'启用订阅'}</button><button class="ghost" data-sub-action="rotate" data-id="${s.id}">重置订阅地址</button><button class="ghost" data-sub-action="delete" data-id="${s.id}">删除订阅</button></div></details></article>`;
  }).join(''):'<p class="empty">创建一个订阅，勾选要导出的节点。</p>';
 }
 async function loadSubscriptions(force=false){
  if(loadingSubs||(!force&&Date.now()-lastLoad<15000))return;
  loadingSubs=true;
  try{[subscriptions,externalNodes]=await Promise.all([api('/api/subscriptions'),api('/api/external-nodes')]);lastLoad=Date.now();draw();drawExternal();}catch(e){fail(e);}finally{loadingSubs=false;}
 }
 function chainSources(){return [...byId('sub-node-picker').querySelectorAll('input:checked')].map(input=>({value:input.dataset.kind+':'+input.value,label:input.closest('label').querySelector('strong').textContent}));}
 function chainValue(hop){return hop.kind+':'+hop.id;}
 function readHop(value){const [kind,id]=value.split(':');return {kind,id:Number(id)};}
 function drawChains(){
  const sources=chainSources();
  byId('sub-chain-list').innerHTML=editingChains.length?editingChains.map((chain,index)=>`<section class="chain-card" data-chain-index="${index}"><div class="field"><label>链路名称<input data-chain-name="${index}" value="${escape(chain.name)}" maxlength="80" required></label></div>${chain.hops.map((hop,pos)=>{const value=chainValue(hop),valid=sources.some(s=>s.value===value);return `<div class="chain-hop"><label><span>${pos===0?'入口':pos===chain.hops.length-1?'出口':'中转'} · 第 ${pos+1} 跳</span><select data-chain-hop="${index}" data-hop-index="${pos}">${!valid?`<option value="${escape(value)}" selected>不可用或未勾选：${escape(value)}</option>`:''}${sources.map(s=>`<option value="${escape(s.value)}" ${s.value===value?'selected':''}>${escape(s.label)} · ${escape(s.value)}</option>`).join('')}</select></label><div class="chain-hop-actions"><button class="ghost" type="button" data-chain-action="up" data-chain="${index}" data-hop="${pos}" ${pos===0?'disabled':''} aria-label="上移一跳">↑</button><button class="ghost" type="button" data-chain-action="down" data-chain="${index}" data-hop="${pos}" ${pos===chain.hops.length-1?'disabled':''} aria-label="下移一跳">↓</button><button class="ghost" type="button" data-chain-action="remove-hop" data-chain="${index}" data-hop="${pos}" ${chain.hops.length<=2?'disabled':''}>移除</button></div></div>`;}).join('')}<p class="meta chain-path">本机 → ${chain.hops.map(h=>escape(sources.find(s=>s.value===chainValue(h))?.label||'不可用节点')).join(' → ')} → 网站</p><div class="task-actions"><button class="ghost" type="button" data-chain-action="add-hop" data-chain="${index}" ${chain.hops.length>=5?'disabled':''}>增加一跳</button><button class="ghost" type="button" data-chain-action="remove-chain" data-chain="${index}">删除链路</button></div></section>`).join(''):'<p class="meta">尚未添加链路，保持普通订阅。</p>';
  byId('sub-chain-only').disabled=!editingChains.length;
  if(!editingChains.length)byId('sub-chain-only').checked=false;
  byId('sub-chain-format-hint').hidden=!editingChains.length;
 }
 byId('sub-add-chain').onclick=()=>{
  const sources=chainSources();byId('sub-edit-error').hidden=true;
  if(sources.length<2){byId('sub-edit-error').textContent='请先勾选至少两个节点，作为入口和出口';byId('sub-edit-error').hidden=false;return;}
  if(editingChains.length>=30){byId('sub-edit-error').textContent='一个订阅最多 30 条链路';byId('sub-edit-error').hidden=false;return;}
  let name='链路 '+(editingChains.length+1);while(editingChains.some(c=>c.name===name))name+=' _';
  if(!editingChains.length)byId('sub-chain-only').checked=true;
  editingChains.push({name,hops:sources.slice(0,2).map(s=>readHop(s.value))});drawChains();
 };
 byId('sub-chain-list').oninput=e=>{if(e.target.dataset.chainName!==undefined)editingChains[Number(e.target.dataset.chainName)].name=e.target.value;};
 byId('sub-chain-list').onchange=e=>{if(e.target.dataset.chainHop!==undefined){editingChains[Number(e.target.dataset.chainHop)].hops[Number(e.target.dataset.hopIndex)]=readHop(e.target.value);drawChains();}};
 byId('sub-chain-list').onclick=e=>{
  const button=e.target.closest('[data-chain-action]');if(!button)return;
  const index=Number(button.dataset.chain),pos=Number(button.dataset.hop),chain=editingChains[index];
  if(!chain)return;
  switch(button.dataset.chainAction){
   case 'remove-chain':editingChains.splice(index,1);break;
   case 'add-hop':{const source=chainSources().find(s=>!chain.hops.some(h=>chainValue(h)===s.value));if(!source){byId('sub-edit-error').textContent='请再勾选一个未使用的节点';byId('sub-edit-error').hidden=false;return;}if(chain.hops.length<5)chain.hops.push(readHop(source.value));break;}
   case 'remove-hop':if(chain.hops.length>2)chain.hops.splice(pos,1);break;
   case 'up':if(pos>0)[chain.hops[pos],chain.hops[pos-1]]=[chain.hops[pos-1],chain.hops[pos]];break;
   case 'down':if(pos<chain.hops.length-1)[chain.hops[pos],chain.hops[pos+1]]=[chain.hops[pos+1],chain.hops[pos]];break;
  }
  drawChains();
 };
 function countSelected(){byId('sub-selected-count').textContent=byId('sub-node-picker').querySelectorAll('input:checked').length;drawChains();}
 function openEditor(s=null){
  editing=s;editingChains=JSON.parse(JSON.stringify(s?.chains||[]));byId('sub-chain-only').checked=!!s?.chain_only;
  byId('subscription-editor-title').textContent=s?'编辑订阅':'创建订阅';
  byId('sub-name').value=s?.name||'';byId('sub-enabled').checked=s?s.enabled:true;
  const dt=s?.expires_at?new Date(s.expires_at):null;
  byId('sub-expires').value=dt?new Date(dt.getTime()-dt.getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  byId('sub-node-search').value='';byId('sub-edit-error').hidden=true;
  const selected=new Set(s?.node_ids||[]);
  const externalSelected=new Set(s?.external_node_ids||[]);
  const available=nodes.filter(n=>n.share_link&&!['uninstalling','removed'].includes(n.status));
  byId('sub-node-picker').innerHTML=available.length?available.map(n=>`<label class="subscription-node"><input type="checkbox" data-kind="managed" value="${n.id}" ${selected.has(n.id)?'checked':''}><span><strong>${escape(n.remark)}</strong><small>自建 #${n.id} · ${escape(state[n.status]||n.status)} · ${escape(n.client_address||n.sni)}</small></span></label>`).join(''):'<p class="meta">暂无可选择的已部署节点。部署完成后可再次编辑。</p>';
  const imported=externalNodes.filter(n=>n.enabled);
  if(imported.length){if(!available.length)byId('sub-node-picker').innerHTML='';byId('sub-node-picker').insertAdjacentHTML('beforeend',imported.map(n=>`<label class="subscription-node"><input type="checkbox" data-kind="external" value="${n.id}" ${externalSelected.has(n.id)?'checked':''}><span><strong>${escape(n.name)}</strong><small>外部 #${n.id} · ${escape(n.protocol.toUpperCase())} · ${escape(n.server)}:${n.port}</small></span></label>`).join(''));}
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
  const body={name:byId('sub-name').value.trim(),enabled:byId('sub-enabled').checked,expires_at:byId('sub-expires').value?new Date(byId('sub-expires').value).toISOString():'',node_ids:[...byId('sub-node-picker').querySelectorAll('input[data-kind=managed]:checked')].map(i=>Number(i.value)),external_node_ids:[...byId('sub-node-picker').querySelectorAll('input[data-kind=external]:checked')].map(i=>Number(i.value))};
  body.chains=editingChains;body.chain_only=byId('sub-chain-only').checked;
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
    case 'toggle':await api(`/api/subscriptions/${s.id}`,{method:'PUT',body:JSON.stringify({name:s.name,node_ids:s.node_ids,external_node_ids:s.external_node_ids||[],enabled:!s.enabled,expires_at:s.expires_at,chains:s.chains||[],chain_only:!!s.chain_only})});await loadSubscriptions(true);break;
    case 'rotate':if(confirm('重置后原订阅地址立即失效，需要在客户端重新导入。继续？')){await api(`/api/subscriptions/${s.id}/rotate`,{method:'POST'});await loadSubscriptions(true);}break;
    case 'delete':if(confirm(`删除订阅「${s.name}」？此操作会使订阅地址失效，节点本身保留。`)){await api(`/api/subscriptions/${s.id}`,{method:'DELETE'});opened.delete(String(s.id));choices.delete(s.id);await loadSubscriptions(true);}break;
   }
  }catch(e){fail(e);}finally{button.disabled=false;}
 };

 let importCandidates=[];
 function drawExternal(){
  document.querySelectorAll('.external-item').forEach(d=>{if(d.open)externalOpened.add(d.dataset.id);else externalOpened.delete(d.dataset.id);});
  byId('external-count').textContent=externalNodes.length;
  byId('external-list').innerHTML=externalNodes.length?externalNodes.map(n=>`<article class="task"><details class="task-details external-item" data-id="${n.id}" ${externalOpened.has(String(n.id))?'open':''}><summary><h3>${escape(n.name)}</h3><span class="badge ${n.enabled?'online':'offline'}">${escape(n.protocol.toUpperCase())} · ${n.enabled?'已启用':'已停用'}</span></summary><p class="meta">${escape(n.server)}:${n.port}${n.source_label?' · 来源 '+escape(n.source_label):''}</p><p class="meta">仅用于订阅导出，不参与服务器部署、监控或自动修复。</p><div class="task-actions"><button class="ghost" data-external-action="edit" data-id="${n.id}">编辑节点</button><button class="ghost" data-external-action="toggle" data-id="${n.id}">${n.enabled?'停用':'启用'}</button><button class="ghost" data-external-action="delete" data-id="${n.id}">删除外部节点</button></div></details></article>`).join(''):'<p class="empty">尚未导入外部节点。</p>';
 }
 function importSourceChanged(){const url=byId('external-import-mode').value==='url';byId('external-content-field').hidden=url;byId('external-url-field').hidden=!url;clearImportPreview();}
 function clearImportPreview(){importCandidates=[];byId('external-import-preview').innerHTML='';byId('external-import-report').textContent='';byId('confirm-external-import').disabled=true;}
 byId('import-external-nodes').onclick=()=>{byId('external-import-error').hidden=true;byId('external-import-content').value='';byId('external-import-url').value='';byId('external-source-label').value='';byId('external-import-file').value='';byId('external-import-mode').value='content';importSourceChanged();byId('external-import-dialog').showModal();};
 byId('close-external-import').onclick=()=>byId('external-import-dialog').close();
 byId('external-import-mode').onchange=importSourceChanged;
 byId('external-import-content').oninput=clearImportPreview;byId('external-import-url').oninput=clearImportPreview;
 byId('external-import-file').onchange=async e=>{const file=e.target.files[0];if(!file)return;clearImportPreview();if(file.size>1048576){byId('external-import-error').textContent='文件不能超过 1 MiB';byId('external-import-error').hidden=false;return;}byId('external-import-content').value=await file.text();};
 byId('parse-external-import').onclick=async()=>{
  clearImportPreview();byId('external-import-error').hidden=true;byId('parse-external-import').disabled=true;
  try{
   const body=byId('external-import-mode').value==='url'?{url:byId('external-import-url').value.trim()}:{content:byId('external-import-content').value};
   const result=await api('/api/external-nodes/preview',{method:'POST',body:JSON.stringify(body)});
   importCandidates=result.nodes;
   byId('external-import-report').textContent=`解析 ${result.total} 项，可导入 ${result.nodes.length} 个，重复 ${result.duplicates} 个，无效或不支持 ${result.invalid_count} 项。`;
   byId('external-import-preview').innerHTML=result.nodes.map((n,i)=>`<label class="subscription-node"><input type="checkbox" value="${i}" checked><span><strong>${escape(n.name)}</strong><small>${escape(n.protocol.toUpperCase())} · ${escape(n.server)}:${n.port}</small></span></label>`).join('')+(result.errors.length?`<p class="error">${result.errors.map(e=>'第 '+e.index+' 项：'+escape(e.message)).join('<br>')}</p>`:'');
   byId('confirm-external-import').disabled=!result.nodes.length;
  }catch(e){byId('external-import-error').textContent=e.message;byId('external-import-error').hidden=false;}finally{byId('parse-external-import').disabled=false;}
 };
 byId('confirm-external-import').onclick=async()=>{
  const selected=[...byId('external-import-preview').querySelectorAll('input:checked')].map(i=>importCandidates[Number(i.value)]);
  if(!selected.length){byId('external-import-error').textContent='请至少选择一个节点';byId('external-import-error').hidden=false;return;}
  byId('confirm-external-import').disabled=true;
  try{const result=await api('/api/external-nodes/import',{method:'POST',body:JSON.stringify({nodes:selected.map(n=>({uri:n.uri,name:n.name})),source_label:byId('external-source-label').value.trim()})});byId('external-import-dialog').close();byId('external-import-success').textContent=`已导入 ${result.added} 个节点，跳过已有节点 ${result.duplicates} 个。编辑或创建订阅时可勾选这些节点。`;byId('external-import-success').hidden=false;await loadSubscriptions(true);}
  catch(e){byId('external-import-error').textContent=e.message;byId('external-import-error').hidden=false;}finally{byId('confirm-external-import').disabled=false;}
 };
 let editingExternal=null;
 byId('close-external-editor').onclick=()=>byId('external-node-editor').close();
 byId('external-list').onclick=async e=>{
  const button=e.target.closest('[data-external-action]');if(!button)return;const n=externalNodes.find(n=>n.id===Number(button.dataset.id));if(!n)return;button.disabled=true;
  try{
   if(button.dataset.externalAction==='edit'){editingExternal=n;byId('external-edit-name').value=n.name;byId('external-edit-uri').value=n.uri;byId('external-edit-enabled').checked=n.enabled;byId('external-edit-error').hidden=true;byId('external-node-editor').showModal();}
   if(button.dataset.externalAction==='toggle'){await api(`/api/external-nodes/${n.id}`,{method:'PUT',body:JSON.stringify({name:n.name,uri:n.uri,enabled:!n.enabled})});await loadSubscriptions(true);}
   if(button.dataset.externalAction==='delete'&&confirm(`删除外部节点「${n.name}」？它会从相关订阅中移除，远端服务器不受影响。`)){await api(`/api/external-nodes/${n.id}`,{method:'DELETE'});await loadSubscriptions(true);}
  }catch(e){fail(e);}finally{button.disabled=false;}
 };
 byId('external-node-form').onsubmit=async e=>{
  e.preventDefault();byId('save-external-node').disabled=true;byId('external-edit-error').hidden=true;
  try{await api(`/api/external-nodes/${editingExternal.id}`,{method:'PUT',body:JSON.stringify({name:byId('external-edit-name').value.trim(),uri:byId('external-edit-uri').value.trim(),enabled:byId('external-edit-enabled').checked})});byId('external-node-editor').close();await loadSubscriptions(true);}
  catch(e){byId('external-edit-error').textContent=e.message;byId('external-edit-error').hidden=false;}finally{byId('save-external-node').disabled=false;}
 };
 byId('subscription-module').addEventListener('toggle',()=>{if(byId('subscription-module').open)loadSubscriptions(true);});
 document.addEventListener('nodes-updated',()=>{if(byId('subscription-module').open)loadSubscriptions();});
 loadSubscriptions(true);
})();
