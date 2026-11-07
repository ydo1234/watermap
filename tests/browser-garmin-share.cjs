// Browser regression checks. Reads a temporary authenticated session from stdin.
// The native share is mocked; no file is sent to an external app.
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const {WebSocket}=require('undici');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'watermap-gpx-browser-'));
const chrome=spawn('/usr/bin/chromium',['--headless','--disable-gpu','--disable-dev-shm-usage','--no-first-run','--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
const delay=ms=>new Promise(r=>setTimeout(r,ms));let socket;
async function main(){
 const active=path.join(profile,'DevToolsActivePort');
 for(let i=0;!fs.existsSync(active)&&i<150;i++)await delay(100);
 const port=fs.readFileSync(active,'utf8').split('\n')[0];
 const target=await(await fetch('http://127.0.0.1:'+port+'/json/new?about:blank',{method:'PUT'})).json();
 socket=new WebSocket(target.webSocketDebuggerUrl);
 await new Promise((r,j)=>{socket.addEventListener('open',r,{once:true});socket.addEventListener('error',j,{once:true});});
 let id=0;const pending=new Map();
 socket.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.j(new Error(m.error.message)):p.r(m.result);}});
 const call=(method,params={})=>new Promise((r,j)=>{const n=++id;pending.set(n,{r,j});socket.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>(await call('Runtime.evaluate',{expression,returnByValue:true})).result.value;
 await call('Page.enable');await call('Runtime.enable');await call('Network.enable');
 await call('Network.setCookie',{name:input.cookieName,value:input.cookie,url:input.base,path:'/'});

 const inject=await call('Page.addScriptToEvaluateOnNewDocument',{source:`
 window.__shareMode='success';window.__shareCalls=0;
 Object.defineProperty(navigator,'canShare',{configurable:true,value:()=>window.__shareMode!=='unsupported'});
 Object.defineProperty(navigator,'share',{configurable:true,value:async data=>{
  if(window.__shareMode==='cancel')throw new DOMException('cancel','AbortError');
  window.__shareCalls++;
  window.__shared={name:data.files[0].name,type:data.files[0].type,text:await data.files[0].text(),keys:Object.keys(data)};
 }});
 `});
 const waitFor=async expression=>{for(let i=0;i<250;i++){if(await evaluate(expression))return;await delay(100);}throw new Error('Timed out: '+expression);};
 for(const [label,url] of [['list',input.base+'/'],['detail',input.base+'/generated/'+encodeURIComponent(input.page)]]){
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await call('Page.navigate',{url});
  await waitFor("Boolean(document.querySelector('[data-garmin-share]'))");
  await evaluate("document.querySelector('[data-garmin-share]').click()");
  await waitFor("!document.getElementById('garmin-share-file').hidden");
  await evaluate("document.getElementById('garmin-share-file').click()");
  await waitFor('Boolean(window.__shared)');
  const shared=await evaluate('window.__shared');
  if(!shared.name.endsWith('-watermap.gpx')||shared.type!=='application/gpx+xml'||!shared.text.includes('<wpt')||shared.keys.join(',')!=='files')throw new Error('Wrong GPX attachment');
  await evaluate("window.__shareMode='cancel';document.getElementById('garmin-share-file').click()");
  await waitFor("document.getElementById('garmin-share-status').textContent.includes('annulé')");
  await evaluate("document.getElementById('garmin-share-close').click();window.__shareMode='unsupported';document.querySelector('[data-garmin-share]').click()");
  await waitFor("document.getElementById('garmin-share-status').textContent.includes('ne permet pas')");
  const fallback=await evaluate("({hidden:document.getElementById('garmin-share-file').hidden,href:document.getElementById('garmin-share-download').getAttribute('href'),width:document.documentElement.scrollWidth,screen:innerWidth})");
  if(!fallback.hidden||!fallback.href.endsWith('/enriched.gpx')||fallback.width>fallback.screen+2)throw new Error('Fallback or mobile layout invalid');
  await evaluate("document.getElementById('garmin-share-close').click();Object.defineProperty(navigator,'share',{configurable:true,value:undefined});document.querySelector('[data-garmin-share]').click()");
  await waitFor("document.getElementById('garmin-share-status').textContent.includes('ne propose pas')");
  const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  fs.writeFileSync('/home/yann/watermap-deployment/garmin-share-'+label+'.png',Buffer.from(shot.data,'base64'));
  console.log(JSON.stringify({page:label,sharedGpx:true,cancel:true,unsupportedFallback:true,mobile:true,nativeSharing:'mocked; no external application contacted'}));
 }
 await call('Page.removeScriptToEvaluateOnNewDocument',{identifier:inject.identifier});
 const insecure=input.base.replace('127.0.0.1','192.168.2.11');
 await call('Network.setCookie',{name:input.cookieName,value:input.cookie,url:insecure,path:'/'});
 await call('Page.navigate',{url:insecure+'/'});
 await waitFor("Boolean(document.querySelector('[data-garmin-share]'))");
 await evaluate("document.querySelector('[data-garmin-share]').click()");
 await waitFor("document.getElementById('garmin-share-status').textContent.includes('HTTPS')");
 console.log(JSON.stringify({insecureHttpFallback:true}));
}
main().catch(e=>{console.error(e.message);process.exitCode=1;}).finally(async()=>{if(socket)socket.close();chrome.kill('SIGTERM');await delay(300);fs.rmSync(profile,{recursive:true,force:true});});
