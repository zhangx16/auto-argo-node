import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=path.resolve(import.meta.dirname,'../app/static');
const jobs=[],subscriptions=[],externalNodes=[];let submitted=null;
function updateFixtureSub(s){
 s.chains=s.chains||[];s.chain_only=!!s.chain_only;s.chain_count=s.chains.length;s.chain_errors=[];
 s.node_count=s.node_ids.length+(s.external_node_ids||[]).length;s.exportable_count=s.chains.length+(s.chain_only?0:s.node_count);
 s.formats=s.chains.length?['clash','singbox']:['base64','raw','clash','singbox'];
 const base=s.urls&&Object.values(s.urls)[0]?Object.values(s.urls)[0].split('?')[0]:'http://example.com/subscribe/1/fixture';
 s.urls=Object.fromEntries(s.formats.map(format=>[format,base+'?format='+format]));
 return s;
}
const server=http.createServer(async(req,res)=>{
 res.setHeader('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'");
 if(req.url.startsWith('/api/')){
  res.setHeader('Content-Type','application/json');
  if(req.url==='/api/notifications')return res.end(JSON.stringify({configured:true,bot_username:'fixture_bot',chat_id:'123',enabled:true,offline_seconds:180,summary_hours:24,summary_enabled:true,queued:0}));
  if(req.url==='/api/traffic')return res.end(JSON.stringify({nodes:jobs.map(n=>({...n,remark:n.remark,agent_version:3,rx:1024,tx:2048,today_rx:1024,today_tx:2048,month_rx:1024,month_tx:2048,available:true,sampled_at:1792600000})),totals:{rx:1024,tx:2048,today_rx:1024,today_tx:2048,month_rx:1024,month_tx:2048},scope:'fixture port accounting'}));
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
 assert.equal(await evaluate('document.querySelector("h1").textContent'),'从服务器，到节点。');
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

 await evaluate('document.querySelector("#subscription-module").open=true;document.querySelector("#new-subscription").click()');
 assert.equal(await evaluate('document.querySelector("#subscription-editor").open'),true);
 assert.equal(await evaluate('document.querySelectorAll("#sub-node-picker input").length'),1);
 await evaluate('document.querySelector("#sub-name").value="Selected nodes";document.querySelector("#sub-node-picker input").click();document.querySelector("#subscription-form").requestSubmit()');
 for(let i=0;i<30;i++){if(subscriptions.length&&await evaluate('!!document.querySelector(".subscription-item")'))break;await new Promise(r=>setTimeout(r,100));}
 assert.deepEqual(subscriptions[0].node_ids,[1]);
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
 await evaluate('document.querySelector("#node-module").open=true;window.scrollTo(0,0)');
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
 await evaluate(`document.getElementById('traffic-module').open=true;document.getElementById('traffic-refresh').click()`);
 await waitFor(`document.getElementById('traffic-totals').textContent.includes('KiB')`);
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
 assert.deepEqual(errors,[]);console.log('UI smoke passed: command enrollment, no SSH secrets, node health, edit/queue settings, probe, logs, deletion, desktop/mobile layout.');
}finally{ws?.close();child.kill();await new Promise(r=>child.once('exit',r));server.close();fs.rmSync(profile,{recursive:true,force:true});}
