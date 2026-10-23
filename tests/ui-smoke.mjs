import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=path.resolve(import.meta.dirname,'../app/static');
const jobs=[],subscriptions=[],externalNodes=[];let submitted=null,mediaActive=null,sourceSubmitted=null,mediaSubmitted=null;const mediaHistory=[],sourceRows=[];let quotaRule={limit_bytes:107374182400,reset_day:15,warning_percent:80,count_mode:'both',used_bytes:10737418240,remaining_bytes:96636764160,percent:10,cycle_start:'2026-10-15',cycle_end:'2026-11-15',measured_days:8};
function updateFixtureSub(s){
 s.page_url='http://127.0.0.1:18909/subscription/'+s.id+'/fixture';
 s.usage={upload:1024,download:2048,used:3072,total:s.traffic_limit_bytes||0,managed_count:1,measured_count:1,external_count:(s.external_node_ids||[]).length,stale_count:0,since:'2026-10-22'};
 s.chains=s.chains||[];s.chain_only=!!s.chain_only;s.chain_count=s.chains.length;s.chain_errors=[];
 s.node_count=s.node_ids.length+(s.external_node_ids||[]).length;s.exportable_count=s.chains.length+(s.chain_only?0:s.node_count);
 s.formats=s.chains.length?['clash','singbox']:['base64','raw','clash','singbox'];
 const base=s.urls&&Object.values(s.urls)[0]?Object.values(s.urls)[0].split('?')[0]:'http://example.com/subscribe/1/fixture';
 s.urls=Object.fromEntries(s.formats.map(format=>[format,base+'?format='+format]));
 return s;
}
const server=http.createServer(async(req,res)=>{
 res.setHeader('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'");
 if(req.url.startsWith('/subscription/1/fixture')){
  if(req.url.endsWith('/data')){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({name:'UI Portal',formats:['base64','raw','clash','singbox'],urls:{base64:'http://example.com/sub?format=base64',raw:'http://example.com/sub?format=raw',clash:'http://example.com/sub?format=clash',singbox:'http://example.com/sub?format=singbox'},usage:{used:3072,total:1073741824,upload:1024,download:2048,managed_count:1,measured_count:1,external_count:0,stale_count:0},expires_at:'',exportable_count:1,chain_errors:[],nodes:[{name:'🇺🇸 Portal node',protocol:'vless',group_name:'家宽',tags:['备用'],kind:'managed'}]}));}
  res.setHeader('Content-Type','text/html');return res.end(fs.readFileSync(path.join(root,'subscription.html')));
 }
 if(req.url.startsWith('/api/')){
  res.setHeader('Content-Type','application/json');
  if(/^\/api\/nodes\/\d+\/events/.test(req.url))return res.end(JSON.stringify({events:[{id:1,created_at:'2026-10-23T00:00:00Z',kind:'health',level:'warning',message:'节点心跳停止：超过 180 秒未收到心跳'},{id:2,created_at:'2026-10-23T01:00:00Z',kind:'change',level:'info',message:'已恢复修改前配置并通过验证'}]}));
  if(/^\/api\/traffic\/nodes\/\d+\/quota$/.test(req.url)){
   if(req.method==='PUT'){let raw='';for await(const chunk of req)raw+=chunk;Object.assign(quotaRule,JSON.parse(raw));}
   return res.end(JSON.stringify(quotaRule));
  }
  if(req.url==='/api/resources')return res.end(JSON.stringify({nodes:[{node_id:1,name:'Resource QA',bound:true,agent_version:4,stale:false,diagnostic_command:'sh /usr/local/lib/argo-deploy/diagnose.sh 1',sample:{received_at:Math.floor(Date.now()/1000),cpu_percent:12.5,cpu_cores:2,load1:.5,memory_total:1073741824,memory_used:536870912,swap_total:0,swap_used:0,disk_total:2147483648,disk_used:1073741824,uptime_seconds:86400,singbox_rss:12582912,cloudflared_rss:25165824,singbox_processes:1,cloudflared_processes:1}}]}));
  if(req.url==='/api/resources/1/history')return res.end(JSON.stringify({samples:[{received_at:Math.floor(Date.now()/1000),cpu_percent:12.5,memory_total:1024,memory_used:512}]}));
  if(req.url==='/api/labels')return res.end(JSON.stringify({groups:[...new Set(jobs.map(n=>n.group_name).filter(Boolean))],tags:[...new Set(jobs.flatMap(n=>n.tags||[]))]}));
  if(req.url.startsWith('/api/labels/')&&req.method==='PUT'){let raw='';for await(const chunk of req)raw+=chunk;const d=JSON.parse(raw);Object.assign(jobs[0],d);return res.end(JSON.stringify(d));}
  if(req.url.startsWith('/api/media/')){
   if(req.method==='POST'){let raw='';for await(const chunk of req)raw+=chunk;mediaSubmitted=JSON.parse(raw);mediaActive={id:1,kind:'managed',node_id:1,status:'running',current:'netflix',results:[{platform:'netflix',name:'Netflix',status:'available',region:'US',detail:'抽样版权页面可用；未验证播放'}],created_at:'2026-10-23T00:00:00Z'};return res.end(JSON.stringify(mediaActive));}
   if(req.method==='DELETE'){mediaHistory.push({...mediaActive,status:'cancelled'});mediaActive=null;return res.end('{}');}
   return res.end(JSON.stringify({active:mediaActive,history:mediaHistory}));
  }
  if(req.url==='/api/external-sources'){
   if(req.method==='POST'){let raw='';for await(const chunk of req)raw+=chunk;sourceSubmitted=JSON.parse(raw);sourceRows.push({id:1,name:sourceSubmitted.name,enabled:true,interval_minutes:sourceSubmitted.interval_minutes,subscription_ids:sourceSubmitted.subscription_ids,running:false,node_count:1,stats:{added:1},last_checked:'2026-10-23',last_success:'2026-10-23'});return res.end(JSON.stringify(sourceRows[0]));}
   return res.end(JSON.stringify(sourceRows));
  }
  if(req.url.startsWith('/api/external-sources/'))return res.end(JSON.stringify({scheduled:true}));
  if(req.url==='/api/regions')return res.end(JSON.stringify({countries:{HK:'Hong Kong',US:'United States',JP:'Japan',ZZ:'未知地区'},release:'2026-10'}));
  if(req.url.startsWith('/api/regions/managed/')&&req.method==='PUT'){let body='';for await(const chunk of req)body+=chunk;const data=JSON.parse(body);Object.assign(jobs[0],{country_code:data.country_code||'JP',country_source:data.country_code?'manual':'auto',country_flag:data.country_code==='US'?'🇺🇸':'🇯🇵'});jobs[0].display_name=jobs[0].country_flag+' '+jobs[0].remark;return res.end(JSON.stringify(jobs[0]));}
  if(req.url==='/api/notifications')return res.end(JSON.stringify({configured:true,bot_username:'fixture_bot',chat_id:'123',enabled:true,offline_seconds:180,summary_hours:24,summary_enabled:true,queued:0}));
  if(req.url.startsWith('/api/traffic'))return res.end(JSON.stringify({nodes:jobs.map(n=>({...n,remark:n.remark,agent_version:3,rx:1024,tx:2048,today_rx:1024,today_tx:2048,month_rx:1024,month_tx:2048,available:true,sampled_at:1792600000})),totals:{rx:1024,tx:2048,today_rx:1024,today_tx:2048,month_rx:1024,month_tx:2048},history:{points:Array.from({length:Number(new URL(req.url,'http://local').searchParams.get('days')||7)},(_,i)=>({date:new Date(Date.UTC(2026,9,22)-((Number(new URL(req.url,'http://local').searchParams.get('days')||7)-1-i)*86400000)).toISOString().slice(0,10),rx:(i+1)*1024,tx:(i+1)*2048,measured:i>0}))},scope:'fixture port accounting'}));
  if(req.url==='/api/notifications/chats')return res.end(JSON.stringify([{id:'123',name:'Fixture user',type:'private'}]));
  if(req.url==='/api/notifications/test'||req.url==='/api/traffic/notify'||req.url.endsWith('/upgrade'))return res.end(JSON.stringify({ok:true}));
  if(req.url==='/api/cloudflare')return res.end(JSON.stringify({configured:true,zone_name:'example.com'}));
  if(req.url==='/api/external-nodes'&&req.method==='GET')return res.end(JSON.stringify(externalNodes));
  if(req.url==='/api/external-nodes/preview')return res.end(JSON.stringify({total:1,duplicates:0,invalid_count:0,errors:[],nodes:[{name:'Imported fixture',protocol:'trojan',server:'edge.example.com',port:443,uri:'trojan://fixture-only-password@edge.example.com:443?sni=tls.example.com#Imported'}]}));
  if(req.url==='/api/external-nodes/import'){
   let body='';for await(const chunk of req)body+=chunk;const data=JSON.parse(body);
   externalNodes.push({id:1,...data.nodes[0],protocol:'trojan',server:'edge.example.com',port:443,enabled:true});return res.end(JSON.stringify({added:1,duplicates:0,node_ids:[1]}));
  }
  if(req.url==='/api/subscriptions/1'&&req.method==='PUT'){
   let body='';for await(const chunk of req)body+=chunk;Object.assign(subscriptions[0],JSON.parse(body));updateFixtureSub(subscriptions[0]);return res.end(JSON.stringify(subscriptions[0]));
  }
  if(req.url==='/api/nodes')return res.end(JSON.stringify(jobs));
  if(req.url==='/api/subscriptions'&&req.method==='GET')return res.end(JSON.stringify(subscriptions));
  if(req.url==='/api/subscriptions'&&req.method==='POST'){
   let body='';for await(const chunk of req)body+=chunk;const data=JSON.parse(body);
   const sub={id:1,...data,node_count:data.node_ids.length,exportable_count:data.node_ids.length,expired:false,urls:{}};
   for(const fmt of ['raw','base64','clash','singbox'])sub.urls[fmt]='http://example.com/subscribe/1/fixture?format='+fmt;
   updateFixtureSub(sub);subscriptions.push(sub);return res.end(JSON.stringify(sub));
  }
  if(req.url.startsWith('/api/subscriptions/1/preview'))return res.end(JSON.stringify({content:'selected fixture node only',node_count:1}));
  if(req.url==='/api/subscriptions/1/rotate'&&req.method==='POST'){
   for(const fmt of ['raw','base64','clash','singbox'])subscriptions[0].urls[fmt]=subscriptions[0].urls[fmt].replace('/fixture?','/rotated?');
   return res.end(JSON.stringify(subscriptions[0]));
  }
  if(req.url==='/api/subscriptions/1'&&req.method==='DELETE'){subscriptions.splice(0);return res.end('{}');}

  if(req.url.endsWith('/logs'))return res.end(JSON.stringify([{created_at:'2026-10-17',line:'QA: node sent heartbeat'}]));
  if(req.url==='/api/agent-nodes'&&req.method==='POST'){
   let body='';for await(const chunk of req)body+=chunk;submitted=JSON.parse(body);
   jobs.push({id:1,...submitted,status:'pending',protocol:submitted.protocol||'vless-ws-argo',management:'agent',agent_bound:false,job_status:'queued',desired_port:submitted.node_port,applied_revision:0,desired_revision:1});
   return res.end(JSON.stringify({id:1,command:'sh -c "echo installation-fixture"',agent_bound:false}));
  }
  if(req.url.endsWith('/enrollment'))return res.end(JSON.stringify({id:1,command:'sh -c "echo enrollment-fixture"',agent_bound:jobs[0].agent_bound}));
  if(req.url.endsWith('/probe')){jobs[0].probe_status='pending';return res.end(JSON.stringify({scheduled:true}));}
  if(req.url.endsWith('/refresh')){jobs[0].job_status='queued';return res.end('{}');}
  if(req.url.startsWith('/api/agent-nodes/')&&req.method==='PATCH'){
   let body='';for await(const chunk of req)body+=chunk;const update=JSON.parse(body);
   if(update.node_port!==jobs[0].node_port){jobs[0].desired_port=update.node_port;jobs[0].job_status='queued';jobs[0].status='installing';}
   Object.assign(jobs[0],{remark:update.remark,client_address:update.client_address,client_port:update.client_port});
   jobs[0].share_link=jobs[0].share_link.replace(/@[^?]+\?/, '@'+update.client_address+':'+update.client_port+'?');
   return res.end(JSON.stringify(jobs[0]));
  }
  if(req.url.startsWith('/api/nodes/')&&req.method==='DELETE'){jobs.splice(0,jobs.length);return res.end('{}');}
  res.statusCode=404;return res.end('{}');
 }
 const url=req.url.split('?')[0];const file=path.join(root,url==='/'?'index.html':url.replace('/static/',''));
 if(!file.startsWith(root+'/')){res.statusCode=403;return res.end();}
 try{res.setHeader('Content-Type',file.endsWith('.css')?'text/css':file.endsWith('.js')?'text/javascript':'text/html');res.end(fs.readFileSync(file));}catch{res.statusCode=404;res.end();}
});
await new Promise(r=>server.listen(18909,'127.0.0.1',r));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'argo-ui-'));
const child=spawn(process.env.CHROMIUM_BIN||'/usr/bin/chromium',['--headless','--no-sandbox','--disable-gpu','--no-first-run','--remote-debugging-port=18910','--remote-debugging-address=127.0.0.1',`--user-data-dir=${profile}`,'about:blank'],{stdio:'ignore'});
let ws;
try{
 let tabs;
 for(let i=0;i<50;i++){try{tabs=await(await fetch('http://127.0.0.1:18910/json')).json();break;}catch{await new Promise(r=>setTimeout(r,100));}}
 assert(tabs?.length,'Chromium started');
 ws=new WebSocket(tabs[0].webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
 let seq=0;const pending=new Map();const errors=[];
 ws.onmessage=e=>{const msg=JSON.parse(e.data);if(msg.id){const p=pending.get(msg.id);if(p){pending.delete(msg.id);msg.error?p.reject(Error(JSON.stringify(msg.error))):p.resolve(msg.result);}}else if(msg.method==='Runtime.exceptionThrown')errors.push(msg.params.exceptionDetails.text);};
 const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
 await send('Page.navigate',{url:'http://127.0.0.1:18909/'});
 for(let i=0;i<40;i++){if(await evaluate('!!document.querySelector("#tasks .empty")'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(await evaluate('document.querySelector("h1").textContent'),'节点概览');
 assert.equal(await evaluate(`['node','task','subscription','resources','traffic'].every(id=>{const module=document.getElementById(id+'-module');return module.tagName==='DIV'&&!module.querySelector(':scope > summary')&&module.getClientRects().length>0;})`),true,'main modules are always expanded');
 assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
 assert.equal(await evaluate('document.querySelector("#password")===null'),true,'command installation never asks for SSH secrets');
 await evaluate(`document.querySelector('#remark').value='QA node';document.querySelector('#deploy-form').requestSubmit()`);
 for(let i=0;i<30;i++){if(submitted)break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(submitted.tunnel_mode,'temp');assert.equal(submitted.fixed_hostname,undefined);
 assert.equal(submitted.client_address,'');assert.equal(submitted.node_port,18080);
 for(let i=0;i<30;i++){if(await evaluate('document.querySelector("#command-dialog").open'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(await evaluate('document.querySelector("#install-command").textContent.includes("installation-fixture")'),true);
 await evaluate('document.querySelector("#close-command").click()');
 Object.assign(jobs[0],{status:'online',job_status:'done',agent_bound:true,host_ip:'192.0.2.1',health_status:'online',last_report_at:'2026-10-17T08:00:00Z',local_ws:true,public_ws:true,probe_status:'ok',latency_ms:120,probe_at:'2026-10-17T08:00:00Z',repair_count:1,applied_revision:1,sni:'qa-example.trycloudflare.com',share_link:'vless://341cad6d-9bf5-4b09-9c96-a7879ebc8fe1@edge.example.com:443?security=tls&type=ws&host=qa-example.trycloudflare.com'});
 await evaluate('document.querySelector("#refresh").click()');
 for(let i=0;i<30;i++){if(await evaluate('document.querySelector("#node-count").textContent==="1"'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(await evaluate('document.querySelector("#deployed-nodes .health-grid").textContent.includes("转发正常")'),true);
 assert.equal(await evaluate('document.querySelector("#deployed-nodes details").open'),false);
 assert.equal(await evaluate('document.querySelector("#tasks details").open'),false);
 assert.equal(await evaluate('document.querySelector(".deployed").getBoundingClientRect().top > document.querySelector("#deploy-form").getBoundingClientRect().bottom'),true);
 await evaluate('document.querySelector("#deployed-nodes summary").click()');
 await evaluate('load()');
 assert.equal(await evaluate('document.querySelector("#deployed-nodes details").open'),true,'expanded state survives refresh');

 await evaluate('document.querySelector("#deployed-nodes [data-action=edit]").click()');
 assert.equal(await evaluate('document.querySelector("#edit-dialog").open'),true);
 await evaluate('document.querySelector("#edit-address").value="new.example.com";document.querySelector("#edit-client-port").value="8443";document.querySelector("#edit-form").requestSubmit()');
 for(let i=0;i<30;i++){if(!await evaluate('document.querySelector("#edit-dialog").open'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(jobs[0].client_address,'new.example.com');assert.equal(jobs[0].client_port,8443);
 await evaluate('document.querySelector("#deployed-nodes [data-action=edit]").click();document.querySelector("#edit-node-port").value="18081";document.querySelector("#edit-form").requestSubmit()');
 for(let i=0;i<30;i++){if(jobs[0].desired_port===18081)break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(jobs[0].node_port,18080,'actual port is preserved until the agent acknowledges');
 assert.equal(jobs[0].desired_port,18081);
 await evaluate('document.querySelector("#deployed-nodes [data-action=logs]").click()');
 for(let i=0;i<30;i++){if(await evaluate('!document.querySelector("#logs-panel").hidden'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(await evaluate('document.querySelector("#logs").textContent.includes("heartbeat")'),true);
 await evaluate('document.querySelector("#deployed-nodes [data-action=probe]").click()');
 for(let i=0;i<30;i++){if(jobs[0].probe_status==='pending')break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(jobs[0].probe_status,'pending');jobs[0].probe_status='ok';
 await evaluate('document.querySelector("#refresh").click()');await new Promise(r=>setTimeout(r,250));

 await evaluate('document.querySelector("#new-subscription").click()');
 assert.equal(await evaluate('document.querySelector("#subscription-editor").open'),true);
 assert.equal(await evaluate('document.querySelectorAll("#sub-node-picker input").length'),1);
 await evaluate('document.querySelector("#sub-name").value="Selected nodes";document.querySelector("#sub-traffic-limit").value="1.5";document.querySelector("#sub-node-picker input").click();document.querySelector("#subscription-form").requestSubmit()');
 for(let i=0;i<30;i++){if(subscriptions.length&&await evaluate('!!document.querySelector(".subscription-item")'))break;await new Promise(r=>setTimeout(r,100));}
 assert.deepEqual(subscriptions[0].node_ids,[1]);
 assert.equal(subscriptions[0].traffic_limit_bytes,1610612736);
 await evaluate('document.querySelector(".subscription-item summary").click();document.querySelector("[data-sub-format]").value="clash";document.querySelector("[data-sub-format]").dispatchEvent(new Event("change",{bubbles:true}))');
 assert.equal(await evaluate('document.querySelector(".subscription-url").value.endsWith("format=clash")'),true);
 await evaluate('document.querySelector("[data-sub-action=preview]").click()');
 for(let i=0;i<30;i++){if(await evaluate('document.querySelector("#subscription-preview").open'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(await evaluate('document.querySelector("#sub-preview-content").textContent'),'selected fixture node only');
 await evaluate('document.querySelector("#close-sub-preview").click()');

 await evaluate('document.querySelector("#import-external-nodes").click();document.querySelector("#external-import-content").value="trojan://fixture-only-password@edge.example.com:443#Imported";document.querySelector("#parse-external-import").click()');
 for(let i=0;i<30;i++){if(await evaluate('!!document.querySelector("#external-import-preview input")'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(await evaluate('document.querySelector("#external-import-report").textContent.includes("可导入 1 个")'),true);
 await evaluate('document.querySelector("#confirm-external-import").click()');
 for(let i=0;i<30;i++){if(externalNodes.length&&await evaluate('!document.querySelector("#external-import-dialog").open'))break;await new Promise(r=>setTimeout(r,100));}
 for(let i=0;i<50;i++){if(await evaluate('document.querySelector("#external-count").textContent==="1"'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(externalNodes.length,1);
 await evaluate('document.querySelector("[data-sub-action=edit]").click()');
 assert.equal(await evaluate('document.querySelectorAll("#sub-node-picker input").length'),2);
 await evaluate('document.querySelector("#sub-node-picker input[data-kind=external]").click();document.querySelector("#subscription-form").requestSubmit()');
 for(let i=0;i<30;i++){if(subscriptions[0].external_node_ids?.length)break;await new Promise(r=>setTimeout(r,100));}
 assert.deepEqual(subscriptions[0].external_node_ids,[1]);
 for(let i=0;i<50;i++){if(await evaluate('!document.querySelector("#subscription-editor").open && document.querySelector(".subscription-item .meta").textContent.includes("已选择 2")'))break;await new Promise(r=>setTimeout(r,100));}
 await evaluate('document.querySelector("[data-sub-action=edit]").click();document.querySelector("#sub-add-chain").click()');
 assert.equal(await evaluate('document.querySelectorAll("[data-chain-hop]").length'),2);
 assert.equal(await evaluate('document.querySelector("#sub-chain-only").checked'),true);
 await evaluate('document.querySelector("[data-chain-name]").value="入口 → 落地";document.querySelector("[data-chain-name]").dispatchEvent(new Event("input",{bubbles:true}))');
 await evaluate('document.querySelectorAll("[data-chain-action=up]")[1].click()');
 assert.equal(await evaluate('document.querySelector("[data-chain-hop]").value'),'external:1');
 await evaluate('document.querySelectorAll("[data-chain-action=down]")[0].click()');
 assert.equal(await evaluate('document.querySelector("[data-chain-hop]").value'),'managed:1');
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
 const chainDest=path.resolve(import.meta.dirname,'../artifacts');fs.mkdirSync(chainDest,{recursive:true});
 const chainShot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(chainDest+'/chain-mobile.png',Buffer.from(chainShot.data,'base64'));
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
 await evaluate('document.querySelector("#subscription-form").requestSubmit()');
 for(let i=0;i<30;i++){if(subscriptions[0].chain_count===1&&!await evaluate('document.querySelector("#subscription-editor").open'))break;await new Promise(r=>setTimeout(r,100));}
 assert.deepEqual(subscriptions[0].chains,[{name:'入口 → 落地',hops:[{kind:'managed',id:1},{kind:'external',id:1}]}]);
 assert.equal(subscriptions[0].chain_only,true);
 for(let i=0;i<30;i++){if(await evaluate('document.querySelector("[data-sub-format]").options.length===2'))break;await new Promise(r=>setTimeout(r,100));}
 assert.deepEqual(await evaluate('[...document.querySelector("[data-sub-format]").options].map(o=>o.value)'),['clash','singbox']);
 await evaluate('document.querySelector("[data-sub-action=toggle]").click()');
 for(let i=0;i<30;i++){if(!subscriptions[0].enabled&&await evaluate('document.querySelector("[data-sub-action=toggle]").textContent.includes("启用")'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(subscriptions[0].chain_count,1);
 await evaluate('document.querySelector("[data-sub-action=edit]").click()');
 assert.equal(await evaluate('document.querySelectorAll("[data-chain-hop]").length'),2);
 await evaluate('document.querySelector("[data-chain-action=remove-chain]").click()');
 assert.equal(await evaluate('document.querySelector("#sub-chain-only").checked'),false);
 await evaluate('document.querySelector("#subscription-form").requestSubmit()');
 for(let i=0;i<30;i++){if(subscriptions[0].chain_count===0&&!await evaluate('document.querySelector("#subscription-editor").open'))break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(subscriptions[0].formats.length,4);

 assert.equal(await evaluate('document.querySelector("#deployed-nodes .tunnel-badge").textContent'),'临时隧道');
 Object.assign(jobs[0],{tunnel_mode:'fixed',sni:'fixed.example.com'});await evaluate('load()');
 assert.equal(await evaluate('document.querySelector("#deployed-nodes .tunnel-fixed").textContent'),'固定隧道');
 await evaluate('document.querySelector("#deployed-nodes [data-action=edit]").click()');
 assert.equal(await evaluate('document.querySelector("#edit-node-port").readOnly'),true);
 await evaluate('document.querySelector("#close-edit").click();document.querySelector("#tunnel_mode").value="fixed";document.querySelector("#tunnel_mode").dispatchEvent(new Event("change"))');
 assert.equal(await evaluate('document.querySelector("#fixed-hostname-field").hidden'),false);
 assert.equal(await evaluate('document.querySelector("#fixed-domain-hint").textContent.includes("example.com")'),true);
 const dest=path.resolve(import.meta.dirname,'../artifacts');fs.mkdirSync(dest,{recursive:true});
 await evaluate('window.scrollTo({top:0,behavior:"instant"})');
 let shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(dest+'/desktop.png',Buffer.from(shot.data,'base64'));
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
 shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(dest+'/mobile.png',Buffer.from(shot.data,'base64'));
 await evaluate("window.confirm=()=>true;document.querySelector('#deployed-nodes [data-action=remove]').click()");
 for(let i=0;i<30;i++){if(!jobs.length)break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(jobs.length,0);
 submitted=null;
 await evaluate('document.querySelector("#remark").value="Fixed QA";document.querySelector("#fixed_hostname").value="kr01.example.com";document.querySelector("#deploy-form").requestSubmit()');
 for(let i=0;i<30;i++){if(submitted)break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(submitted.tunnel_mode,'fixed');assert.equal(submitted.fixed_hostname,'kr01.example.com');

 for(const kind of ['reality','hysteria2']){
  jobs.splice(0);submitted=null;
  await evaluate('document.querySelector("#command-dialog").close()');
  await evaluate(`document.querySelector('#protocol').value='${kind}';document.querySelector('#protocol').dispatchEvent(new Event('change'));`);
  assert.equal(await evaluate('document.querySelector("#tunnel_mode").disabled'),true);
  assert.equal(await evaluate('document.querySelector("#fixed_hostname").disabled'),true);
  assert.equal(await evaluate('document.querySelector("#client_address").required'),true);
  assert.equal(await evaluate('document.querySelector("#direct_client_port").disabled'),false);
  await evaluate(`document.querySelector('#remark').value='${kind} QA';document.querySelector('#client_address').value='203.0.113.10';document.querySelector('#direct_client_port').value='32660';document.querySelector('#deploy-form').requestSubmit()`);
  for(let i=0;i<30;i++){if(submitted)break;await new Promise(r=>setTimeout(r,100));}
  for(let i=0;i<30;i++){if(await evaluate('document.querySelector("#command-dialog").open'))break;await new Promise(r=>setTimeout(r,100));}
  assert.equal(submitted.protocol,kind);assert.equal(submitted.client_port,32660);assert.equal(submitted.tunnel_mode,undefined);assert.equal(submitted.fixed_hostname,undefined);assert.equal(submitted.server_name,'www.amd.com');
  await evaluate('document.querySelector("#command-dialog").close()');
  Object.assign(jobs[0],{status:'online',job_status:'done',agent_bound:true,health_status:'online',local_ws:true,public_ws:false,probe_status:'ok',sni:'www.amd.com',share_link:kind==='reality'?'vless://fixture@203.0.113.10:32660?security=reality':'hysteria2://fixture@203.0.113.10:32660?sni=www.amd.com'});
  await evaluate('load()');
  await evaluate('document.querySelector("#node-module").open=true');
  for(let i=0;i<40;i++){if(await evaluate('!!document.querySelector("#deployed-nodes .tunnel-badge")'))break;await new Promise(r=>setTimeout(r,100));}
  assert.equal(await evaluate('document.querySelector("#deployed-nodes .tunnel-badge").textContent'),kind==='reality'?'Reality Vision':'Hysteria2');
  assert.equal(await evaluate('document.querySelector("#deployed-nodes .health-grid").textContent.includes("本地 / 公网 WS")'),false);
  await evaluate('document.querySelector("#deployed-nodes [data-action=edit]").click()');
  assert.equal(await evaluate('document.querySelector("#edit-node-port").readOnly'),false);
  assert.equal(await evaluate('document.querySelector("#edit-client-port").value'),'32660');
  await evaluate('document.querySelector("#close-edit").click()');
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
  let directShot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(dest+'/'+kind+'-mobile.png',Buffer.from(directShot.data,'base64'));
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
 }
 const waitFor=async expr=>{for(let i=0;i<50;i++){if(await evaluate(expr))return;await new Promise(r=>setTimeout(r,100));}throw Error('Timed out: '+expr);};
 await evaluate(`document.getElementById('traffic-refresh').click()`);
 await waitFor(`document.getElementById('traffic-totals').textContent.includes('KiB')`);
 assert.equal(await evaluate('document.querySelectorAll("#traffic-chart svg rect").length'),14);
 await evaluate(`document.getElementById('traffic-days').value='30';document.getElementById('traffic-days').dispatchEvent(new Event('change'))`);
 await waitFor(`document.querySelectorAll('#traffic-chart-data tr').length===30`);
 await evaluate(`document.getElementById('traffic-chart-node').value='1';document.getElementById('traffic-chart-node').dispatchEvent(new Event('change'))`);
 await waitFor(`document.querySelectorAll('#traffic-chart-data tr').length===30`);
 await evaluate(`document.querySelector('.chart-data').open=true`);
 await send('Emulation.setDeviceMetricsOverride',{width:375,height:844,deviceScaleFactor:1,mobile:true});
 await new Promise(r=>setTimeout(r,250));
 await evaluate(`document.querySelector('.traffic-chart-section').scrollIntoView({behavior:'instant',block:'start'})`);
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 const chartPoint=await evaluate(`(()=>{const r=document.querySelector('#traffic-chart svg').getBoundingClientRect();return{x:r.left+66+(r.width-78)*.95,y:r.top+80}})()`);
 await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[chartPoint]});
 await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 await waitFor(`!document.getElementById('traffic-chart-tooltip').hidden`);
 assert.equal(await evaluate(`(()=>{const t=document.getElementById('traffic-chart-tooltip'),r=t.getBoundingClientRect();return t.textContent.includes('上行（IN）')&&t.textContent.includes('下行（OUT）')&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()`),true);
 await send('Page.captureScreenshot',{format:'png'}).then(shot=>fs.writeFileSync(dest+'/traffic-tooltip-mobile.png',Buffer.from(shot.data,'base64')));
 await evaluate(`document.body.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))`);
 assert.equal(await evaluate(`document.getElementById('traffic-chart-tooltip').hidden`),true);
 const chartBounds=await evaluate(`(()=>{const r=document.querySelector('.traffic-chart-section').getBoundingClientRect();return{x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:Math.min(r.height,600),scale:1}})()`);
 const chartShot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:chartBounds});fs.writeFileSync(dest+'/traffic-history-mobile.png',Buffer.from(chartShot.data,'base64'));
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
 await new Promise(r=>setTimeout(r,200));

 await evaluate(`document.getElementById('traffic-chart').scrollIntoView({behavior:'instant',block:'center'})`);
 await new Promise(r=>setTimeout(r,350));
 const desktopPoint=await evaluate(`(()=>{const r=document.querySelector('#traffic-chart svg').getBoundingClientRect();return{x:r.left+66+(r.width-78)*.5,y:r.top+80}})()`);
 await send('Input.dispatchMouseEvent',{type:'mouseMoved',...desktopPoint});
 await waitFor(`!document.getElementById('traffic-chart-tooltip').hidden`);
 assert.equal(await evaluate(`document.getElementById('traffic-chart-tooltip').textContent.includes('合计')`),true);
 await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:10,y:10});
 await waitFor(`document.getElementById('traffic-chart-tooltip').hidden`);
 await evaluate(`document.querySelector('[data-events]').click()`);
 await waitFor(`document.getElementById('events-list').textContent.includes('心跳停止')`);
 assert.equal(await evaluate(`document.getElementById('events-list').textContent.includes('恢复修改前配置')`),true);
 await evaluate(`document.getElementById('events-close').click();document.querySelector('[data-quota]').click()`);
 await waitFor(`!document.getElementById('quota-save').disabled`);
 assert.equal(await evaluate(`document.getElementById('quota-limit').value`),'100');
 await send('Emulation.setDeviceMetricsOverride',{width:375,height:844,deviceScaleFactor:1,mobile:true});
 await new Promise(r=>setTimeout(r,200));
 assert.equal(await evaluate(`document.getElementById('quota-dialog').scrollWidth<=document.getElementById('quota-dialog').clientWidth&&document.documentElement.scrollWidth<=innerWidth`),true);
 const quotaShot=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(dest+'/quota-mobile.png',Buffer.from(quotaShot.data,'base64'));
 await evaluate(`document.getElementById('quota-limit').value='200';document.getElementById('quota-reset').value='31';document.getElementById('quota-form').requestSubmit()`);
 await waitFor(`!document.getElementById('quota-dialog').open`);
 assert.equal(quotaRule.limit_bytes,214748364800);assert.equal(quotaRule.reset_day,31);
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
 await evaluate(`document.querySelector('[data-label-kind="managed"]').click()`);
 await waitFor(`!document.getElementById('labels-save').disabled`);
 await evaluate(`document.getElementById('labels-group').value='家宽';document.getElementById('labels-tags').value='备用,流媒体';document.getElementById('labels-form').requestSubmit()`);
 await waitFor(`!document.getElementById('labels-dialog').open&&document.querySelector('#deployed-nodes .node-tags').textContent.includes('家宽')`);
 await evaluate(`document.getElementById('node-search').value='no-matching-node';document.getElementById('node-search').dispatchEvent(new Event('input'))`);
 await waitFor(`!!document.querySelector('#deployed-nodes .empty')`);
 assert.equal(await evaluate(`document.getElementById('stat-deployed').textContent`),'1');
 await evaluate(`document.getElementById('node-search').value='';document.getElementById('node-search').dispatchEvent(new Event('input'))`);
 await waitFor(`!!document.querySelector('[data-media-kind="managed"]')`);
 await evaluate(`document.querySelector('[data-media-kind="managed"]').click()`);
 await waitFor(`!document.getElementById('media-start').disabled`);
 await evaluate(`document.getElementById('media-select-all').click()`);
 assert.equal(await evaluate(`document.querySelectorAll('#media-form input:checked').length`),12);
 await evaluate(`document.getElementById('media-form').requestSubmit()`);
 await waitFor(`document.getElementById('media-results').textContent.includes('可用信号')`);
 assert.equal(mediaSubmitted.platforms.length,12);
 await send('Emulation.setDeviceMetricsOverride',{width:375,height:844,deviceScaleFactor:1,mobile:true});
 await new Promise(r=>setTimeout(r,200));
 assert.equal(await evaluate(`document.getElementById('media-dialog').scrollWidth<=document.getElementById('media-dialog').clientWidth`),true);
 const mediaShot=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(dest+'/media-mobile.png',Buffer.from(mediaShot.data,'base64'));
 await evaluate(`document.getElementById('media-cancel').click()`);
 await waitFor(`document.getElementById('media-status').textContent.includes('已取消')`);
 await evaluate(`document.getElementById('media-close').click();document.getElementById('new-source').click()`);
 await waitFor(`!document.getElementById('source-save').disabled`);
 await evaluate(`document.getElementById('source-name').value='Provider fixture';document.getElementById('source-url').value='https://example.com/private?token=fixture';document.getElementById('source-form').requestSubmit()`);
 await waitFor(`!document.getElementById('source-dialog').open&&document.getElementById('source-list').textContent.includes('Provider fixture')`);
 assert.equal(sourceSubmitted.interval_minutes,360);assert.equal(sourceSubmitted.subscription_ids.length,0);
 assert.equal(JSON.stringify(sourceRows).includes('token=fixture'),false);
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
 await evaluate(`document.querySelector('[data-region-kind="managed"]').click()`);
 await waitFor(`document.getElementById('region-dialog').open`);
 await evaluate(`document.getElementById('region-country').value='US';document.getElementById('region-form').requestSubmit()`);
 await waitFor(`document.querySelector('#deployed-nodes summary h3').textContent.startsWith('🇺🇸')`);
 assert.equal(await evaluate('decodeURIComponent(nodeShareLink(nodes[0]).split("#")[1]).startsWith("🇺🇸")'),true);

 await evaluate(`document.getElementById('tg-find').click()`);
 await waitFor(`document.getElementById('tg-chats').textContent.includes('Fixture user')`);
 await evaluate(`document.querySelector('[data-chat]').click();document.getElementById('tg-form').requestSubmit()`);
 await waitFor(`document.getElementById('telemetry-message').textContent.includes('已保存')`);
 await evaluate(`document.getElementById('tg-test').click()`);
 await waitFor(`document.getElementById('telemetry-message').textContent.includes('已发送')`);
 await evaluate(`document.getElementById('tg-form').closest('details').open=true`);
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
 const telemetryShot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(dest+'/telemetry-mobile.png',Buffer.from(telemetryShot.data,'base64'));
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await evaluate(`window.scrollTo({top:0,behavior:"instant"})`);
 assert.equal(await evaluate('document.getElementById("stat-deployed").textContent'),'1');
 await evaluate(`document.getElementById('nav-toggle').click()`);
 assert.equal(await evaluate('document.body.classList.contains("sidebar-collapsed")'),true);
 await evaluate(`document.getElementById('nav-toggle').click()`);
 await send('Emulation.setDeviceMetricsOverride',{width:375,height:844,deviceScaleFactor:1,mobile:true});
 await evaluate(`document.getElementById('nav-toggle').click()`);
 assert.equal(await evaluate('document.body.classList.contains("mobile-nav-open")'),true);
 assert.equal(await evaluate('document.getElementById("app-sidebar").contains(document.activeElement)'),true);
 await evaluate(`document.querySelector('.nav-link[href="#subscription-module"]').click()`);
 assert.equal(await evaluate('document.getElementById("subscription-module").tagName'),"DIV");
 assert.equal(await evaluate('document.activeElement.id'),"subscription-module-title");
 assert.equal(await evaluate('document.body.classList.contains("mobile-nav-open")'),false);
 await evaluate(`window.scrollTo({top:0,behavior:"instant"})`);
 for(const width of[375,479,480,767,768,1199,1200,1440]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:width<768});
  await new Promise(r=>setTimeout(r,180));
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true,`no overflow at ${width}`);
  await evaluate('window.scrollTo({top:0,behavior:"instant"});document.activeElement?.blur()');
  await new Promise(r=>setTimeout(r,100));
  if([375,768,1440].includes(width)){const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(dest+'/redesign-'+width+'.png',Buffer.from(shot.data,'base64'));}
 }
 fs.writeFileSync(dest+'/redesign-dom.html',await evaluate('document.documentElement.outerHTML'));
 await send('Page.navigate',{url:'http://127.0.0.1:18909/subscription/1/fixture'});
 await waitFor(`document.getElementById('portal-name')?.textContent==='UI Portal'`);
 await evaluate(`document.getElementById('portal-format').value='singbox';document.getElementById('portal-format').dispatchEvent(new Event('change'))`);
 assert.equal(await evaluate(`document.getElementById('portal-import').getAttribute('href').startsWith('sing-box://')`),true);
 await send('Emulation.setDeviceMetricsOverride',{width:375,height:844,deviceScaleFactor:1,mobile:true});await new Promise(r=>setTimeout(r,200));
 assert.equal(await evaluate(`document.documentElement.scrollWidth<=innerWidth`),true);
 const portalShot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(dest+'/subscription-portal-mobile.png',Buffer.from(portalShot.data,'base64'));
 await send('Page.navigate',{url:'http://127.0.0.1:18909/'});
 await waitFor(`document.querySelector('#resources-list details')`);
 await evaluate(`document.querySelector('#resources-list details').open=true;document.getElementById('resources-module').scrollIntoView()`);
 assert.equal(await evaluate(`document.getElementById('resources-list').textContent.includes('12.5%')&&document.getElementById('resources-list').textContent.includes('diagnose.sh 1')`),true);
 await evaluate(`document.querySelector('[data-resource-history]').click()`);
 await waitFor(`document.querySelectorAll('.resource-chart').length===2`);
 assert.equal(await evaluate(`document.documentElement.scrollWidth<=innerWidth`),true,'resource mobile layout');
 const resourceShot=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(dest+'/resources-mobile.png',Buffer.from(resourceShot.data,'base64'));
 await evaluate(`document.querySelector('[data-resource-upgrade]').click()`);
 await waitFor(`document.querySelector('[data-resource-upgrade]').textContent.includes('已排队')`);
 assert.deepEqual(errors,[]);console.log('UI smoke passed: command enrollment, no SSH secrets, node health, edit/queue settings, probe, logs, deletion, desktop/mobile layout.');
}finally{ws?.close();child.kill();await new Promise(r=>child.once('exit',r));server.close();fs.rmSync(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});}
