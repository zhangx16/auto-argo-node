'use strict';
(() => {
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const bytes=n=>{let v=Number(n||0),i=0;const units=['B','KiB','MiB','GiB','TiB'];while(v>=1024&&i<4){v/=1024;i++;}return v.toFixed(i?2:0)+' '+units[i];};
 const date=v=>{const d=new Date(typeof v==='number'?v*1000:v);return Number.isNaN(d.getTime())?'':d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（北京时间）';};
 function view(u){
  if(!u)return '';
  if(u.scope==='composite')return `<section class="subscription-usage"><div class="usage-heading"><strong>按来源查看用量</strong><span>${u.meter_count} 个用量来源</span></div><p class="meta">${esc(u.note)}</p><p class="meta">客户端用量：${u.client_usage?esc(u.client_usage_name)+' · '+bytes(u.client_usage.used)+' / '+(u.client_usage.total?bytes(u.client_usage.total):'未设额度'):'未提供单一总量，请在此查看明细'}</p>${(u.breakdown||[]).map(x=>`<details class="subscription-usage-source" open><summary><strong>${esc(x.name)}</strong>${x.shared?' · 共用同一上游账户':''}</summary>${view(x.usage)}${x.expires_at?`<p class="meta">原订阅到期：${esc(date(x.expires_at))}</p>`:''}</details>`).join('')||'<p class="meta">暂无有效来源。</p>'}</section>`;
  const upstream=u.scope==='upstream-account',percent=u.total?Math.min(100,u.used/u.total*100):0;
  return `<section class="subscription-usage"><div class="usage-heading"><strong>${upstream?'上游已用 '+bytes(u.used):u.measured_count?'已统计 '+bytes(u.used):'尚无可统计用量'}</strong><span>${u.total?(upstream?'总额度 ':'参考额度 ')+bytes(u.total):'未提供额度'}</span></div>${u.total?`<progress max="100" value="${percent}" aria-label="${upstream?'上游账户':'已统计'}流量比例"></progress>`:''}<p class="meta">${upstream?'上传':'节点入口 IN'} ${bytes(u.upload)} / ${upstream?'下载':'OUT'} ${bytes(u.download)}${u.expire?' · 到期 '+esc(date(u.expire)):''}${u.stale_count?' · 统计待更新':''}${!upstream?' · 已采集 '+u.measured_count+'/'+u.managed_count+' 个自建节点':''}</p><p class="meta">${esc(u.note||'共享节点统计，并非此订阅的独立用量。')}</p>${(upstream?[]:u.upstream_sources||[]).map(x=>`<p class="meta">上游 ${esc(x.name)}：已用 ${bytes(x.upload+x.download)} / ${bytes(x.total)}${x.expire?' · 到期 '+esc(date(x.expire)):''}（账户共享用量，单独展示）</p>`).join('')}</section>`;
 }
 window.subscriptionUsageView=view;
 window.subscriptionSourceWarnings=rows=>(rows||[]).filter(x=>x.status!=='ok').map(x=>`${x.name}：${({missing:'原订阅已删除',disabled:'原订阅已停用',expired:'原订阅已到期',invalid:'原订阅配置不可导出',limit:'合并后超出节点或链路数量上限'})[x.status]||'暂不可用'}`).join('；');
})();
