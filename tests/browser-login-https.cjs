// Optional read-only HTTPS regression check: Node.js + undici + Chromium.
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {randomUUID}=require('node:crypto');
const {WebSocket}=require('undici');
const base=(process.argv[2]||'https://watermap.dolisi.fr').replace(/\/$/,'');
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'watermap-https-browser-'));
const chrome=spawn('/usr/bin/chromium',['--headless','--disable-gpu','--disable-dev-shm-usage','--disable-background-networking','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let socket;
async function main(){
  const active=path.join(profile,'DevToolsActivePort');
  for(let i=0;!fs.existsSync(active)&&i<100;i++) await delay(100);
  const port=fs.readFileSync(active,'utf8').split('\n')[0];
  const target=await (await fetch('http://127.0.0.1:'+port+'/json/new?about:blank',{method:'PUT'})).json();
  socket=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
  let sequence=0;
  const pending=new Map(), extra=new Map();
  let loginId, status, loginReferer;
  socket.addEventListener('message',event=>{
    const value=JSON.parse(event.data);
    if(value.id){const callback=pending.get(value.id);if(callback){pending.delete(value.id);value.error?callback.reject(new Error(value.error.message)):callback.resolve(value.result);}return;}
    const data=value.params||{};
    if(value.method==='Network.requestWillBeSent'&&data.request.method==='POST'&&data.request.url===base+'/login'){
      loginId=data.requestId;
      loginReferer=data.request.headers.Referer||data.request.headers.referer;
    }
    if(value.method==='Network.requestWillBeSentExtraInfo')extra.set(data.requestId,data.headers);
    if(value.method==='Network.responseReceived'&&data.requestId===loginId)status=data.response.status;
  });
  const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
  await call('Page.enable'); await call('Network.enable'); await call('Runtime.enable');
  await call('Page.navigate',{url:base+'/'});
  let ready=false;
  for(let i=0;i<200;i++){
    const result=await call('Runtime.evaluate',{expression:"Boolean(document.querySelector('input[name=email]') && document.querySelector('input[name=password]'))",returnByValue:true});
    if(result.result.value){ready=true;break;}await delay(100);
  }
  if(!ready)throw new Error('Login form not available');
  const email='browser-'+randomUUID()+'@example.invalid';
  await call('Runtime.evaluate',{expression:`document.querySelector('input[name=email]').value=${JSON.stringify(email)};document.querySelector('input[name=password]').value='invalid-browser-check';document.querySelector('form').requestSubmit();`}).catch(error=>{if(!/context|navigation/i.test(error.message))throw error;});
  for(let i=0;status===undefined&&i<200;i++)await delay(100);
  const headers=extra.get(loginId)||{};
  const referer=headers.Referer||headers.referer||loginReferer||null;
  const summary={url:base+'/login',status,referer};
  console.log(JSON.stringify(summary));
  if(status!==200||referer!==base+'/')throw new Error('Browser HTTPS login/CSRF regression');
}
main().catch(error=>{console.error(error.message);process.exitCode=1;}).finally(async()=>{if(socket)socket.close();chrome.kill('SIGTERM');await delay(300);fs.rmSync(profile,{recursive:true,force:true});});
