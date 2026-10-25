'use strict';
(async()=>{
 const el=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const formats={base64:'通用 Base64',raw:'原始节点链接',clash:'Clash / Mihomo',singbox:'sing-box'};
 const bytes=n=>{let value=Number(n||0),i=0;const units=['B','KiB','MiB','GiB','TiB'];while(value>=1024&&i<4){value/=1024;i++;}return value.toLocaleString('zh-CN',{maximumFractionDigits:i?2:0})+' '+units[i];};
 try{
  const r=await fetch(location.pathname.replace(/\/$/,'')+'/data',{credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer'});
  if(!r.ok)throw Error('订阅不存在、已停用、已到期或地址已重置。请联系分享者获取新地址。');
  const d=await r.json();document.title=d.name+' · 订阅';el('portal-name').textContent=d.name;el('portal-expiry').textContent=d.expires_at?'到期时间（UTC）：'+d.expires_at:'未设置到期时间';el('portal-content').hidden=false;
  el('portal-format').replaceChildren(...d.formats.map(f=>new Option(formats[f]||f,f)));
  el('portal-usage').innerHTML=window.subscriptionUsageView(d.usage);
  const sourceWarning=window.subscriptionSourceWarnings(d.source_subscriptions);el('portal-source-warning').textContent=sourceWarning;el('portal-source-warning').hidden=!sourceWarning;
  el('portal-node-heading').textContent=`节点与链路 · ${d.exportable_count} 个可导出`;
  el('portal-nodes').innerHTML=d.nodes.map(n=>`<article class="compact-node"><h3 class="node-name">${esc(n.name)}</h3><p class="meta">${esc(n.protocol.toUpperCase())} · ${n.kind==='external'?'外部节点':n.kind==='chain'?'链式代理':'自建节点'}</p><div class="node-tags">${[n.group_name,...n.tags].filter(Boolean).map(t=>`<span class="badge">${esc(t)}</span>`).join('')}</div></article>`).join('')||'<p class="empty">暂无可用节点。</p>';
  el('portal-chain-errors').textContent=(d.chain_errors||[]).join('；');el('portal-chain-errors').hidden=!d.chain_errors?.length;
  function selected(){return d.urls[el('portal-format').value];}
  function update(){el('portal-url').value=selected();el('portal-status').textContent=d.exportable_count?'请选择格式后复制或导入客户端。':'暂无可导出的有效配置，请联系分享者。';el('portal-copy').disabled=!d.exportable_count;el('portal-download').disabled=!d.exportable_count;const format=el('portal-format').value,link=el('portal-import');link.hidden=!d.exportable_count||!['clash','singbox'].includes(format);if(format==='clash'){link.href='clash://install-config?url='+encodeURIComponent(selected())+'&name='+encodeURIComponent(d.name);link.textContent='导入 Clash / Mihomo';}else if(format==='singbox'){link.href='sing-box://import-remote-profile?url='+encodeURIComponent(selected())+'#'+encodeURIComponent(d.name);link.textContent='导入 sing-box';}}
  el('portal-format').onchange=update;update();
  el('portal-copy').onclick=async()=>{try{await navigator.clipboard.writeText(selected());el('portal-status').textContent='订阅地址已复制';}catch{el('portal-url').focus();el('portal-url').select();el('portal-status').textContent='请手动复制上方地址';}};
  el('portal-download').onclick=()=>{const a=document.createElement('a');a.href=selected();a.download='subscription.'+(el('portal-format').value==='clash'?'yaml':el('portal-format').value==='singbox'?'json':'txt');document.body.append(a);a.click();a.remove();};
 }catch(e){el('portal-name').textContent='订阅暂不可用';el('portal-error').textContent=e.message;el('portal-error').hidden=false;}
})();
