import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=path.resolve(import.meta.dirname,'../app/static');
const jobs=[];let submitted=null;
const server=http.createServer(async(req,res)=>{
 res.setHeader('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'");
 if(req.url.startsWith('/api/')){
  res.setHeader('Content-Type','application/json');
  if(req.url==='/api/nodes')return res.end(JSON.stringify(jobs));
  if(req.url.endsWith('/logs'))return res.end(JSON.stringify([{created_at:'2026-10-17',line:'QA: node sent heartbeat'}]));
  if(req.url==='/api/agent-nodes'&&req.method==='POST'){
   let body='';for await(const chunk of req)body+=chunk;submitted=JSON.parse(body);
   jobs.push({id:1,...submitted,status:'pending',protocol:'vless-ws-argo',management:'agent',agent_bound:false,job_status:'queued',desired_port:submitted.node_port,applied_revision:0,desired_revision:1});
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
 const dest=path.resolve(import.meta.dirname,'../artifacts');fs.mkdirSync(dest,{recursive:true});
 await evaluate('window.scrollTo(0,0)');
 let shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(dest+'/desktop.png',Buffer.from(shot.data,'base64'));
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
 shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(dest+'/mobile.png',Buffer.from(shot.data,'base64'));
 await evaluate("window.confirm=()=>true;document.querySelector('#deployed-nodes [data-action=remove]').click()");
 for(let i=0;i<30;i++){if(!jobs.length)break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(jobs.length,0);
 assert.deepEqual(errors,[]);console.log('UI smoke passed: command enrollment, no SSH secrets, node health, edit/queue settings, probe, logs, deletion, desktop/mobile layout.');
}finally{ws?.close();child.kill();await new Promise(r=>child.once('exit',r));server.close();fs.rmSync(profile,{recursive:true,force:true});}
