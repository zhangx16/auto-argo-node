import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=path.resolve(import.meta.dirname,'../app/static');
const jobs=[],subscriptions=[],externalNodes=[];let bulkDeleted=null;let sshSaved=null,sshQueued=null,opsMaintenance=null,opsUpgrade=null,opsPolicy=null,submitted=null,mediaActive=null,sourceSubmitted=null,mediaSubmitted=null;const mediaHistory=[],sourceRows=[];let quotaRule={limit_bytes:107374182400,reset_day:15,warning_percent:80,count_mode:'both',used_bytes:10737418240,remaining_bytes:96636764160,percent:10,cycle_start:'2026-10-15',cycle_end:'2026-11-15',measured_days:8};
const sshServers=[{id:'ssh-fixture',name:'SSH fixture',host:'',port:22,mode:'agent',auth_kind:'',configured:false,managed_key:false,nodes:[],host_key:'',fingerprint:''}],sshTasks=[];
let fixtureAssociated=false,boundNodeIds=null;let lastTaskServer='',lastTrafficServer='';let serverSettings=null,serverNodeBody=null,recoveryBody=null,healthBody=null;
const serverFixture={id:'server-fixture',name:'Fixture host',host:'192.0.2.10',port:22,configured:true,mode:'agent',group_name:'试点',notes:'',control_url:'',fallback_urls:[],monitor:{id:'monitor-fixture',online:true},components:{protocols_ok:1,protocols_total:1},capabilities:{stale:false,data:{os:'alpine',arch:'x86_64',init:'openrc',container:'lxc',memory_kib:262144,disk_free_kib:1048576,public_ipv6:false,checked_at:'2026-10-25T00:00:00Z',install_supported:true,warnings:[]}},nodes:[{id:1,remark:'Fixture existing',protocol:'vless-ws-argo',node_port:33001,probe_status:'ok',agent_version:8,client_address:'fixture.example.com'}]};
const recoveryFixture={id:'recovery-fixture',server_id:'server-fixture',server_name:'Fixture host',status:'paused',created:1792886400,steps:[{name:'连接与系统能力检查',key:'preflight',status:'done',task_id:'progress-fixture'},{name:'安装协议',key:'protocol',status:'failed'}],nodes:[{id:1,name:'Fixture existing',link_changed:false}],error:'fixture: waiting for review'};
const taskFixture={id:'ssh:progress-fixture',source_id:'progress-fixture',kind:'ssh',name:'Fixture host · protocol',status:'running',phase:'protocol_install',created:1792886400,error:''};
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
const nativeTheme={short:'native',name:'管理看板',builtin:true,version:'1',config:[{key:'title',type:'string',default:'服务器看板'},{key:'notice',type:'text',default:''},{key:'columns',type:'select',default:'3',options:[{value:'2'},{value:'3'},{value:'4'}]},{key:'color',type:'select',default:'system',options:[{value:'system'},{value:'light'},{value:'dark'}]},{key:'show_summary',type:'boolean',default:true}]};let themeState={active:'native',themes:[nativeTheme],configs:{}};
const server=http.createServer(async(req,res)=>{
 res.setHeader('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'");
 if(req.url.startsWith('/subscription/1/fixture')){
  if(req.url.endsWith('/data')){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({name:'UI Portal',formats:['base64','raw','clash','singbox'],urls:{base64:'http://example.com/sub?format=base64',raw:'http://example.com/sub?format=raw',clash:'http://example.com/sub?format=clash',singbox:'http://example.com/sub?format=singbox'},usage:{used:3072,total:1073741824,upload:1024,download:2048,managed_count:1,measured_count:1,external_count:0,stale_count:0},expires_at:'',exportable_count:1,chain_errors:[],nodes:[{name:'🇺🇸 Portal node',protocol:'vless',group_name:'家宽',tags:['备用'],kind:'managed'}]}));}
  res.setHeader('Content-Type','text/html');return res.end(fs.readFileSync(path.join(root,'subscription.html')));
 }
 if(req.url.startsWith('/api/')){
  res.setHeader('Content-Type','application/json');
  if(req.url==='/api/monitor/latency')return res.end(JSON.stringify({targets:[{name:'安徽电信',target:'ah-ct-v4.ip.zstaticcdn.com:80',interval:60},{name:'安徽联通',target:'ah-cu-v4.ip.zstaticcdn.com:80',interval:60},{name:'安徽移动',target:'ah-cm-v4.ip.zstaticcdn.com:80',interval:60}],auto_join:true,tasks:[],hosts:[]}));
  if(req.url==='/api/monitor/themes')return res.end(JSON.stringify(themeState));
  if(req.url==='/api/monitor/themes/config'){let raw='';for await(const chunk of req)raw+=chunk;const d=JSON.parse(raw);themeState.configs[d.short]=d.values;return res.end(JSON.stringify(themeState));}
  if(req.url==='/api/servers')return res.end(JSON.stringify([{...serverFixture,nodes:fixtureAssociated?serverFixture.nodes:[]}]));
  if(req.url==='/api/servers/server-fixture'){
   if(req.method==='PUT'){let raw='';for await(const chunk of req)raw+=chunk;serverSettings=JSON.parse(raw);Object.assign(serverFixture,serverSettings);}
   return res.end(JSON.stringify(serverFixture));
  }
  if(req.url==='/api/servers/server-fixture/nodes'){let raw='';for await(const chunk of req)raw+=chunk;serverNodeBody=JSON.parse(raw);return res.end(JSON.stringify({id:2,recovery:recoveryFixture,reuses_probe:true}));}
  if(req.url==='/api/servers/server-fixture/recoveries'){let raw='';for await(const chunk of req)raw+=chunk;recoveryBody=JSON.parse(raw);return res.end(JSON.stringify(recoveryFixture));}
  if(req.url==='/api/recoveries')return res.end(JSON.stringify([]));
  if(req.url==='/api/recoveries/recovery-fixture')return res.end(JSON.stringify(recoveryFixture));
  if(req.url==='/api/recoveries/recovery-fixture/resume'){recoveryFixture.status='running';recoveryFixture.error='';return res.end(JSON.stringify(recoveryFixture));}
  if(req.url.startsWith('/api/tasks?')){lastTaskServer=new URL(req.url,'http://local').searchParams.get('server_id');return res.end(JSON.stringify({items:[taskFixture],total:1,stats:{running:1}}));}
  if(req.url.startsWith('/api/tasks/ssh%3Aprogress-fixture/output'))return res.end(JSON.stringify({result:'fixture partial progress\ninstallation in progress',phase:'protocol_install',status:'running',error:''}));
  if(/^\/api\/subscriptions\/\d+\/health$/.test(req.url)){if(req.method==='PUT'){let raw='';for await(const chunk of req)raw+=chunk;healthBody=JSON.parse(raw);}return res.end(JSON.stringify({policy:healthBody||{enabled:false,fail_count:3,fail_minutes:5,recover_count:2,recover_minutes:5},nodes:[],isolated_count:0}));}
  if(/^\/api\/nodes\/\d+\/(events|diagnostics)/.test(req.url))return res.end(JSON.stringify({node_id:1,name:'Fixture',checks:[{name:'实际转发',state:'ok',at:'2026-10-23T00:00:00Z'}],control:{repair:{limit_count:3,window_minutes:60,attempts:0}},agent_version:8,note:'诊断测试',events:[{id:1,created_at:'2026-10-23T00:00:00Z',kind:'health',level:'warning',message:'节点心跳停止：超过 180 秒未收到心跳'},{id:2,created_at:'2026-10-23T01:00:00Z',kind:'change',level:'info',message:'已恢复修改前配置并通过验证'}]}));
  if(req.url==='/api/ssh/servers')return res.end(JSON.stringify([...sshServers,{...serverFixture,auth_kind:'managed_key',host_key:'fixture',fingerprint:'SHA256:fixture'}]));
  if(req.url==='/api/ssh/servers/server-fixture/nodes'&&req.method==='PUT'){let raw='';for await(const chunk of req)raw+=chunk;boundNodeIds=JSON.parse(raw).node_ids;fixtureAssociated=true;return res.end(JSON.stringify(serverFixture));}
  if(req.url==='/api/ssh/servers/server-fixture/tasks')return res.end(JSON.stringify([]));
  if(req.url==='/api/ssh/connection-probe')return res.end(JSON.stringify({host_key:'fixture-public-host-key',fingerprint:'SHA256:fixture',algorithm:'ssh-ed25519'}));
  if(req.url==='/api/ssh/servers/ssh-fixture'&&req.method==='PUT'){let raw='';for await(const chunk of req)raw+=chunk;sshSaved=JSON.parse(raw);Object.assign(sshServers[0],{name:sshSaved.name,host:sshSaved.host,port:sshSaved.ssh_port,host_key:sshSaved.host_key,configured:true,auth_kind:'password',fingerprint:'SHA256:fixture'});return res.end(JSON.stringify(sshServers[0]));}
  if(req.url==='/api/ssh/servers/ssh-fixture/tasks'){if(req.method==='POST'){let raw='';for await(const chunk of req)raw+=chunk;sshQueued=JSON.parse(raw);sshTasks.unshift({id:String(sshTasks.length+1),action:sshQueued.action,status:'done',created:1792800000,result:'fixture task completed'});return res.end(JSON.stringify(sshTasks[0]));}return res.end(JSON.stringify(sshTasks));}
  if(req.url==='/api/operations/health')return res.end(JSON.stringify({components:{'数据库读写':{state:'ok',at:1792800000}},jobs:{queued:0},notifications:{n:0,retries:0},cloudflare:{},note:'健康检查'}));
  if(req.url==='/api/operations/versions')return res.end(JSON.stringify({nodes:[{id:1,name:'Fixture',host_name:'Host',agent_version:8,core_channel:'minimal',core_version:'1.14.3',eligible:true,fresh:true,control:{repair:{attempts:0,limit_count:3,window_minutes:60}}}],hosts:[{id:'fixture',name:'Host'}],target_agent_version:8,release:{core_version:'1.14.3'}}));
  if(req.url==='/api/operations/maintenance'&&req.method==='PUT'){let raw='';for await(const chunk of req)raw+=chunk;opsMaintenance=JSON.parse(raw);return res.end('{"ok":true}');}
  if(req.url.endsWith('/repair-policy')&&req.method==='PUT'){let raw='';for await(const chunk of req)raw+=chunk;opsPolicy=JSON.parse(raw);return res.end('{"ok":true}');}
  if(req.url==='/api/operations/upgrades'){if(req.method==='POST'){let raw='';for await(const chunk of req)raw+=chunk;opsUpgrade=JSON.parse(raw);return res.end('{"id":1}');}return res.end(JSON.stringify([]));}
  if(/^\/api\/traffic\/nodes\/\d+\/quota$/.test(req.url)){
   if(req.method==='PUT'){let raw='';for await(const chunk of req)raw+=chunk;Object.assign(quotaRule,JSON.parse(raw));}
   return res.end(JSON.stringify(quotaRule));
  }
  if(req.url==='/api/monitor/hosts')return res.end(JSON.stringify({available:true,hosts:[{id:'monitor-fixture',name:'Monitor fixture',country:'KR',os:'alpine',arch:'amd64',online:true,stale:false,last_seen:Math.floor(Date.now()/1000),group_name:'试点',metrics:{cpu:12.5,mem_used:536870912,mem_total:1073741824,disk_used:1073741824,disk_total:2147483648,net_rx:1024,net_tx:2048},health:{singbox_rss:123456},traffic:{used_bytes:1024,limit_bytes:1048576,percent:.1},nodes:[{id:1,remark:'Fixture existing',health_mode:'legacy',management_enabled:true}]},{id:'other-monitor',name:'Other host',country:'US',os:'debian',arch:'amd64',online:false,stale:true,last_seen:0,group_name:'',metrics:{},health:{},nodes:[]}]}));
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
   if(req.method==='POST'){let raw='';for await(const chunk of req)raw+=chunk;sourceSubmitted=JSON.parse(raw);sourceRows.push({...sourceSubmitted,id:1,name:sourceSubmitted.name,enabled:true,interval_minutes:sourceSubmitted.interval_minutes,subscription_ids:sourceSubmitted.subscription_ids,running:false,node_count:1,stats:{added:1},last_checked:'2026-10-23',last_success:'2026-10-23'});return res.end(JSON.stringify(sourceRows[0]));}
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
  if(req.url==='/api/external-nodes/bulk-delete'&&req.method==='POST'){
   let raw='';for await(const chunk of req)raw+=chunk;bulkDeleted=JSON.parse(raw);
   for(let i=externalNodes.length-1;i>=0;i--)if(bulkDeleted.node_ids.includes(externalNodes[i].id))externalNodes.splice(i,1);
   for(const s of subscriptions)s.external_node_ids=(s.external_node_ids||[]).filter(id=>!bulkDeleted.node_ids.includes(id));
   return res.end(JSON.stringify({ok:true,deleted:bulkDeleted.node_ids.length}));
  }
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
   const sub={id:subscriptions.length+1,...data,node_count:data.node_ids.length,exportable_count:data.node_ids.length,expired:false,urls:{}};
   for(const fmt of ['raw','base64','clash','singbox'])sub.urls[fmt]='http://example.com/subscribe/'+sub.id+'/fixture?format='+fmt;
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
  if(req.url.endsWith('/reinstall'))return res.end(JSON.stringify({id:1,command:'sh -c "echo reinstall-fixture"',monitor_command:'sh -c "echo monitor-fixture"',agent_bound:true,reinstall:true}));
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
 const url=req.url.split('?')[0];const file=path.join(root,url==='/'?'index.html':url==='/monitor'?'monitor.html':url.replace('/static/',''));
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
 const waitFor=async expr=>{for(let i=0;i<80;i++){if(await evaluate(expr))return;await new Promise(r=>setTimeout(r,100));}throw Error('Timed out: '+expr);};
 const dest=path.resolve(import.meta.dirname,'../artifacts');fs.mkdirSync(dest,{recursive:true});
 await send('Page.navigate',{url:'http://127.0.0.1:18909/'});await waitFor(`document.getElementById('tg-form')&&document.body.dataset.activeModule==='overview'`);
 assert.equal(await evaluate(`document.querySelector('a[href="/monitor"]')===null&&document.getElementById('resources-module')===null&&document.querySelector('[data-server-tab="overview"]')===null`),true);
 await evaluate(`document.querySelector('a[href="#deployment-module"]').click();document.getElementById('remark').value='QA node';document.getElementById('deploy-form').requestSubmit()`);
 await waitFor(`document.getElementById('command-dialog').open`);assert.equal(submitted.install_monitor,false);assert.equal(submitted.management_enabled,true);
 await evaluate(`document.getElementById('command-dialog').close()`);
 Object.assign(jobs[0],{status:'online',job_status:'done',agent_bound:true,health_mode:'legacy',management_enabled:true,host_ip:'192.0.2.10',health_status:'online',last_report_at:'2026-10-25T11:00:00Z',local_ws:true,public_ws:true,probe_status:'ok',latency_ms:120,applied_revision:1,sni:'qa-example.trycloudflare.com',share_link:'vless://fixture@edge.example.com:443?type=ws'});
 fixtureAssociated=true;serverFixture.monitor=null;
 await evaluate(`document.querySelector('a[href="#server-module"]').click();document.getElementById('server-refresh').click();load()`);await waitFor(`document.querySelector('[data-server-open]')`);
 await evaluate(`document.querySelector('[data-server-open]').click()`);await waitFor(`document.getElementById('server-dialog').open&&document.querySelector('#server-node-cards .health-grid')`);
 assert.equal(await evaluate(`document.getElementById('server-node-cards').textContent.includes('节点脚本巡检')`),true);
 await evaluate(`document.querySelector('[data-server-tab="traffic"]').click()`);await waitFor(`document.querySelector('#server-traffic-mount #traffic-chart svg')`);
 await evaluate(`document.getElementById('server-dialog').close();document.querySelector('a[href="#traffic-module"]').click()`);await waitFor(`document.querySelector('#traffic-module #traffic-chart svg')`);
 assert.equal(await evaluate(`document.querySelector('#traffic-module #tg-form')!==null&&document.body.dataset.activeModule==='traffic-module'`),true);
 await evaluate(`document.getElementById('tg-find').click()`);await waitFor(`document.querySelector('[data-chat]')`);
 await evaluate(`document.querySelector('[data-chat]').click();document.getElementById('tg-form').requestSubmit()`);await waitFor(`document.getElementById('telemetry-message').textContent.includes('已保存')`);
 await evaluate(`document.getElementById('traffic-days').value='30';document.getElementById('traffic-days').dispatchEvent(new Event('change'))`);await waitFor(`document.querySelectorAll('#traffic-chart-data tr').length===30`);
 fs.writeFileSync(dest+'/restored-main-traffic-1440.png',Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});assert.equal(await evaluate(`document.documentElement.scrollWidth<=innerWidth`),true);
 await evaluate(`document.getElementById('tg-form').closest('details').open=true`);fs.writeFileSync(dest+'/restored-main-traffic-mobile.png',Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 await evaluate(`location.hash='#server-module'`);await waitFor(`document.body.dataset.activeModule==='server-module'`);await evaluate(`document.querySelector('[data-server-open]').click()`);await waitFor(`document.getElementById('server-dialog').open`);assert.equal(await evaluate(`document.getElementById('server-dialog').scrollWidth<=document.getElementById('server-dialog').clientWidth`),true);
 await evaluate(`document.getElementById('server-dialog').close();document.querySelector('a[href="#subscription-module"]').click();document.getElementById('source-module').open=true;document.getElementById('new-source').click()`);
 await waitFor(`document.getElementById('source-dialog').open&&!document.getElementById('source-save').disabled`);
 await evaluate(`document.getElementById('source-name').value='Imported IPv4';document.getElementById('source-url').value='https://example.com/sub';document.getElementById('source-user-agent').value='clash.meta';document.getElementById('source-clean-prefixes').checked=true;document.getElementById('source-address-overrides').value='2606:4700::1111 = 1.1.1.1';document.getElementById('source-form').requestSubmit()`);
 await waitFor(`!document.getElementById('source-dialog').open&&!!document.querySelector('[data-source-action="edit"]')`);
 assert.equal(sourceSubmitted.clean_prefixes,true);assert.equal(sourceSubmitted.user_agent,'clash.meta');assert.deepEqual(sourceSubmitted.address_overrides,{'2606:4700::1111':'1.1.1.1'});
 sourceRows[0].usage={upload:1024,download:2048,total:322122547200,expire:1900000000};
 await evaluate(`document.querySelector('[data-source-action="edit"]').click()`);await waitFor(`document.getElementById('source-dialog').open`);
 assert.equal(await evaluate(`document.getElementById('source-clean-prefixes').checked`),true);await evaluate(`document.getElementById('source-close').click();document.getElementById('new-subscription').click()`);
 await waitFor(`document.getElementById('subscription-editor').open`);
 await evaluate(`document.getElementById('sub-name').value='Imported IPv4';document.getElementById('subscription-form').requestSubmit()`);await waitFor(`!document.getElementById('subscription-editor').open`);
 subscriptions[0].usage={scope:'upstream-account',upload:1024,download:2048,used:3072,total:322122547200,measured_count:0,managed_count:0,external_count:29,stale_count:0,expire:1900000000,note:'上游账户共享用量'};
 await evaluate(`document.dispatchEvent(new Event('subscriptions-refresh'))`);await waitFor(`document.getElementById('subscription-list').textContent.includes('上游已用')`);
 assert.equal(await evaluate(`document.getElementById('subscription-list').textContent.includes('300.00 GiB')`),true);
 await evaluate(`document.getElementById('new-subscription').click()`);await waitFor(`document.getElementById('subscription-editor').open`);
 assert.equal(await evaluate(`document.querySelectorAll('#sub-included-picker input').length`),1);
 await evaluate(`document.getElementById('sub-name').value='Combined fixture';const input=document.querySelector('#sub-included-picker input');input.checked=true;input.dispatchEvent(new Event('change',{bubbles:true}));document.getElementById('sub-usage-source').value='1';document.getElementById('subscription-form').requestSubmit()`);
 await waitFor(`!document.getElementById('subscription-editor').open`);
 assert.deepEqual(subscriptions[1].included_subscription_ids,[1]);assert.equal(subscriptions[1].usage_source_id,1);assert.deepEqual(subscriptions[1].node_ids,[]);
 subscriptions[1].usage={scope:'composite',meter_count:1,breakdown:[{name:'Imported IPv4',usage:subscriptions[0].usage}],client_usage:subscriptions[0].usage,client_usage_name:'Imported IPv4',note:'按原订阅展示共享用量'};
 subscriptions[1].source_subscriptions=[{subscription_id:1,name:'Imported IPv4',status:'ok'}];
 await evaluate(`document.dispatchEvent(new Event('subscriptions-refresh'))`);await waitFor(`document.getElementById('subscription-list').textContent.includes('按来源查看用量')`);
 assert.equal(await evaluate(`document.getElementById('subscription-list').textContent.includes('客户端用量：Imported IPv4')`),true);
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await evaluate(`document.querySelector('[data-sub-action="edit"][data-id="2"]').click()`);await waitFor(`document.getElementById('subscription-editor').open`);
 assert.equal(await evaluate(`document.querySelector('#sub-included-picker input').checked&&document.getElementById('sub-usage-source').value==='1'`),true);
 assert.equal(await evaluate(`document.getElementById('subscription-editor').scrollWidth<=document.getElementById('subscription-editor').clientWidth`),true);
 await evaluate(`document.getElementById('close-sub-editor').click()`);
 externalNodes.push({id:1,name:'First compact fixture',protocol:'trojan',server:'first.example.com',port:443,enabled:true,uri:'trojan://fixture@first.example.com:443#First'});externalNodes.push({id:2,name:'Second compact fixture',protocol:'trojan',server:'second.example.com',port:443,enabled:true,uri:'trojan://fixture@second.example.com:443#Second'});subscriptions[0].external_node_ids=[1,2];
 await evaluate(`document.dispatchEvent(new Event('subscriptions-refresh'))`);await waitFor(`document.querySelectorAll('[data-member-group="sub-1"] [data-external-card]').length===2`);
 assert.equal(await evaluate(`document.querySelectorAll('#external-list [data-external-card]').length`),0);
 await evaluate(`document.querySelector('.subscription-item[data-id="1"]').open=true`);
 assert.equal(await evaluate(`getComputedStyle(document.querySelector('[data-member-group="sub-1"] .subscription-node-grid')).gridTemplateColumns.split(' ').length>=2`),true);
 await evaluate(`document.querySelector('[data-member-group="sub-1"] [data-external-batch="all"]').click()`);
 assert.equal(await evaluate(`document.querySelector('[data-member-group="sub-1"] [data-picked-count]').textContent`),'2');
 await evaluate(`window.confirm=message=>{window.fixtureDeleteConfirmation=message;return true;};document.querySelector('[data-member-group="sub-1"] [data-external-batch="delete"]').click()`);
 await waitFor(`document.querySelectorAll('[data-external-card]').length===0`);
 assert.deepEqual(bulkDeleted.node_ids,[1,2]);assert.equal(await evaluate(`window.fixtureDeleteConfirmation.includes('Imported IPv4')&&window.fixtureDeleteConfirmation.includes('Combined fixture')`),true);
 assert.deepEqual(errors,[]);console.log('UI smoke passed: compact subscription node grids, atomic external bulk deletion, subscription references, selected client usage, per-source breakdown, mobile editor; independent probe removed, node-script management, server tabs, scoped traffic, restored global traffic/TG configuration and desktop/mobile layout.');
}finally{ws?.close();child.kill();await new Promise(r=>child.once('exit',r));server.close();fs.rmSync(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});}
