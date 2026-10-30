(() => {
'use strict';
const ctx=document.getElementById('assist-context');if(!ctx)return;
const isAdmin=ctx.dataset.role==='admin';let session=null,pc=null,dc=null,stream=null,cursor=0,timer=null,pulseAt=0,remoteAllowed=false,capable=false,queue=Promise.resolve(),candidates=[],polling=false,stopping=false,startedAt=0,connectingAt=0,localGrant='',remoteGrant='';
const newId=()=>globalThis.crypto?.randomUUID?.()||Array.from(globalThis.crypto.getRandomValues(new Uint8Array(24)),n=>n.toString(16).padStart(2,'0')).join('');
const handle=newId();
try{navigator.mediaDevices?.setCaptureHandleConfig?.({handle,exposeOrigin:true,permittedOrigins:[location.origin]});}catch(_){}
const el=(tag,text,parent,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;if(parent)parent.appendChild(n);return n;};
const banner=el('aside','',document.body,'assist-banner');banner.id='assist-banner';banner.hidden=true;
const info=el('p','',banner),buttons=el('div','',banner,'assist-buttons');
const button=(text,parent,fn)=>{const b=el('button',text,parent);b.type='button';b.addEventListener('click',fn);return b;};
const stopButton=button('Interrompi condivisione',buttons,()=>stop('Condivisione interrotta.'));stopButton.className='assist-stop';
const revokeButton=button('Revoca controllo',buttons,()=>revoke());revokeButton.hidden=true;
const allowButton=button('Accetta controllo di questa scheda',buttons,()=>allow());allowButton.hidden=true;
const rejectControl=button('Non consentire il controllo',buttons,()=>revoke());rejectControl.hidden=true;
const dialog=el('dialog','',document.body,'assist-viewer');dialog.id='assist-viewer';const head=el('div','',dialog,'assist-viewer-head');const title=el('h2','Assistenza remota',head);button('Chiudi e interrompi',head,()=>stop('Assistenza conclusa.'));
const status=el('p','',dialog),video=el('video','',dialog);video.autoplay=true;video.playsInline=true;video.muted=true;video.tabIndex=0;video.setAttribute('aria-label','Schermo condiviso; i comandi richiedono un consenso separato');
const tools=el('div','',dialog,'assist-buttons');const controlButton=button('Richiedi controllo mouse e tastiera',tools,async()=>{try{await api('request-control');status.textContent='Richiesta di controllo inviata: il dipendente deve accettare.';}catch(e){status.textContent=e.message;}});
const typing=el('div','',dialog,'assist-typing');const typingLabel=el('label','Testo da inviare al campo selezionato nel video',typing);const typingText=el('textarea','',typingLabel);typingText.maxLength=1000;typingText.rows=2;
button('Invia testo',typing,()=>{send({type:'text',text:typingText.value});typingText.value='';});for(const key of ['Tab','Enter','Backspace'])button(key,typing,()=>send({type:'key',key}));typing.hidden=true;
const release=button('Rilascia controllo e mantieni il video',tools,async()=>{remoteGrant='';if(dc?.readyState==='open')dc.send(JSON.stringify({type:'release'}));try{const r=await api('revoke-control');session=r.session;typing.hidden=true;}catch(e){status.textContent=e.message;}});release.hidden=true;
const remoteHint=el('p','',dialog);remoteHint.textContent='Solo visualizzazione finché il dipendente non accetta il controllo. Nessun audio e nessuna registrazione.';
dialog.addEventListener('cancel',event=>{event.preventDefault();stop('Assistenza conclusa.');});
function currentTab(){if(bridgeMode)return true;try{const track=stream?.getVideoTracks()[0],h=track?.getCaptureHandle?.();return !!h&&h.handle===handle&&h.origin===location.origin&&track.getSettings().displaySurface==='browser';}catch(_){return false;}}
async function post(path,body={}){const r=await fetch(path,{method:'POST',credentials:'same-origin',keepalive:path.endsWith('/stop'),headers:{'X-Control-Screen':'1'},body:new URLSearchParams(body)});if(!r.ok)throw new Error(await r.text());return r.json();}
function api(action,body={}){const rid=session?.id;if(!rid)return Promise.reject(new Error('Sessione conclusa'));const task=queue.catch(()=>{}).then(()=>post('/controllo/assistenza/'+rid+'/'+action,body));queue=task;return task;}
function notice(message){info.textContent=message;banner.hidden=false;}
function makePeer(ice){
 pc=new RTCPeerConnection({iceServers:ice});
 pc.onicecandidate=e=>{if(e.candidate&&session)api('candidate',{candidate:JSON.stringify(e.candidate.toJSON())}).catch(()=>{});};
 pc.ontrack=e=>{video.srcObject=e.streams[0]||new MediaStream([e.track]);video.play().catch(()=>{});};
 pc.ondatachannel=e=>attachChannel(e.channel);
 pc.onconnectionstatechange=()=>{if(!pc)return;if(pc.connectionState==='connected'){status.textContent='Schermo collegato · consenso alla visualizzazione attivo';if(!isAdmin)notice('Stai condividendo lo schermo con '+session.admin_name+'. Puoi interrompere in qualsiasi momento.');}if(pc.connectionState==='failed')stop('Connessione video non riuscita. Su reti restrittive può essere necessario configurare un server TURN.');};
 return pc;
}
function attachChannel(channel){
 dc=channel;
 dc.onopen=()=>{if(!isAdmin&&remoteAllowed&&localGrant)dc.send(JSON.stringify({type:'grant',nonce:localGrant}));};
 dc.onmessage=event=>{try{
  if(typeof event.data!=='string'||event.data.length>6000)return;
  const cmd=JSON.parse(event.data);
  if(isAdmin){if(cmd.type==='grant'&&typeof cmd.nonce==='string'&&cmd.nonce.length<=100)remoteGrant=cmd.nonce;if(cmd.type==='revoked')remoteGrant='';return;}
  if(cmd.type==='release'){remoteAllowed=false;localGrant='';return;}
  if(!remoteAllowed||!currentTab()||session?.control_state!=='accepted'||!localGrant||cmd.grant!==localGrant)return;
  applyCommand(cmd);
 }catch(_){}};
}
function protectedNode(node){const input=node?.closest?.('input,textarea');if(input&&/password|passwd|pw_|secret|token|otp/i.test((input.name||'')+' '+(input.autocomplete||'')))return true;return !node||node.closest('#assist-banner,#assist-viewer')||node.closest('input[type="password"],input[type="file"],input[type="hidden"]');}
function editable(node){return node&&(!protectedNode(node))&&!node.disabled&&!node.readOnly&&(node.tagName==='TEXTAREA'||(node.tagName==='INPUT'&&['text','search','number','tel','email','url'].includes(node.type)));}
function applyCommand(cmd){
 if(cmd.type==='click'){
  if(!Number.isFinite(cmd.x)||!Number.isFinite(cmd.y)||cmd.x<0||cmd.x>1||cmd.y<0||cmd.y>1)return;
  let node=document.elementFromPoint(cmd.x*innerWidth,cmd.y*innerHeight);if(protectedNode(node))return;
  node=node?.closest('button,a,input,select,textarea,summary,[role="button"]')||node;if(protectedNode(node)||node.disabled)return;
  if(node.tagName==='A'){try{const url=new URL(node.href,location.href);if(url.origin!==location.origin||!['http:','https:'].includes(url.protocol))return;}catch(_){return;}}
  node.focus?.();node.click?.();
 }else if(cmd.type==='scroll'){
  if(!Number.isFinite(cmd.y)||!Number.isFinite(cmd.x)||!Number.isFinite(cmd.delta))return;
  let node=document.elementFromPoint(Math.max(0,Math.min(1,cmd.x))*innerWidth,Math.max(0,Math.min(1,cmd.y))*innerHeight);if(protectedNode(node))return;
  while(node&&node!==document.body){if(node.scrollHeight>node.clientHeight+2&&['auto','scroll'].includes(getComputedStyle(node).overflowY))break;node=node.parentElement;}
  (node&&node!==document.body?node:window).scrollBy(0,Math.max(-800,Math.min(800,cmd.delta)));
 }else if(cmd.type==='text'){
  const node=document.activeElement;if(!editable(node)||typeof cmd.text!=='string'||cmd.text.length>1000)return;
  const old=node.value,start=node.selectionStart??old.length,end=node.selectionEnd??start;let value=old.slice(0,start)+cmd.text+old.slice(end);if(node.maxLength>0)value=value.slice(0,node.maxLength);
  const setter=Object.getOwnPropertyDescriptor(node.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set;setter.call(node,value);try{node.setSelectionRange(start+cmd.text.length,start+cmd.text.length);}catch(_){}node.dispatchEvent(new Event('input',{bubbles:true}));node.dispatchEvent(new Event('change',{bubbles:true}));
 }else if(cmd.type==='key'){
  const node=document.activeElement,key=cmd.key;if(protectedNode(node))return;
  if(key==='Tab'){const nodes=[...document.querySelectorAll('input,textarea,button,select,a[href],summary')].filter(n=>!protectedNode(n)&&!n.disabled&&n.getClientRects().length);const index=nodes.indexOf(node);nodes[(index+1)%nodes.length]?.focus();return;}
  if(node?.tagName==='SELECT'&&['ArrowDown','ArrowUp'].includes(key)){node.selectedIndex=Math.max(0,Math.min(node.options.length-1,node.selectedIndex+(key==='ArrowDown'?1:-1)));node.dispatchEvent(new Event('change',{bubbles:true}));return;}
  if(!editable(node))return;
  if(key==='Backspace'||key==='Delete'){let a=node.selectionStart??node.value.length,b=node.selectionEnd??a;if(a===b){if(key==='Backspace')a=Math.max(0,a-1);else b=Math.min(node.value.length,b+1);}try{node.setSelectionRange(a,b);}catch(_){}applyCommand({type:'text',text:''});}
  else if(['ArrowLeft','ArrowRight','Home','End'].includes(key)){const a=node.selectionStart??0,p=key==='Home'?0:key==='End'?node.value.length:Math.max(0,Math.min(node.value.length,a+(key==='ArrowRight'?1:-1)));try{node.setSelectionRange(p,p);}catch(_){}}
  else if(key==='Enter'){if(node.tagName==='TEXTAREA')applyCommand({type:'text',text:'\n'});else node.form?.requestSubmit();}
 }
}
function send(cmd){if(bridgeMode){bridgeSend(cmd);return;}if(!isAdmin||session?.control_state!=='accepted'||!remoteGrant||dc?.readyState!=='open')return;dc.send(JSON.stringify({...cmd,grant:remoteGrant}));}
function coordinates(event){const box=video.getBoundingClientRect(),w=video.videoWidth,h=video.videoHeight;if(!w||!h)return null;const scale=Math.min(box.width/w,box.height/h),left=box.left+(box.width-w*scale)/2,top=box.top+(box.height-h*scale)/2,x=(event.clientX-left)/(w*scale),y=(event.clientY-top)/(h*scale);return x>=0&&x<=1&&y>=0&&y<=1?{x,y}:null;}
video.addEventListener('click',e=>{video.focus();const p=coordinates(e);if(p)send({type:'click',...p});});video.addEventListener('wheel',e=>{if(session?.control_state!=='accepted')return;e.preventDefault();const p=coordinates(e);if(p)send({type:'scroll',...p,delta:e.deltaY});},{passive:false});
video.addEventListener('keydown',e=>{if(session?.control_state!=='accepted'||e.ctrlKey||e.altKey||e.metaKey)return;if(e.key==='Escape')return;if(e.key.length===1){e.preventDefault();send({type:'text',text:e.key});}else if(['Tab','Enter','Backspace','Delete','ArrowLeft','ArrowRight','ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();send({type:'key',key:e.key});}});
async function flushCandidates(){if(!pc?.remoteDescription)return;while(candidates.length){try{await pc.addIceCandidate(candidates.shift());}catch(_){}}}
async function pollSession(){
 if(!session||polling)return;polling=true;const id=session.id;
 try{
  const r=await fetch('/controllo/assistenza/'+id+'?cursor='+cursor,{credentials:'same-origin',cache:'no-store'});if(!r.ok)throw new Error('Sessione non raggiungibile');const data=await r.json();if(session?.id!==id)return;if((data.session.updated||0)<(session.updated||0))return;session=data.session;if(['accepted','active'].includes(session.state)&&!connectingAt)connectingAt=Date.now();
  if(!['pending','accepted','active'].includes(session.state)){stop('Sessione '+session.state,false);return;}
  if(Date.now()-pulseAt>8000){await api('pulse');pulseAt=Date.now();}
  if(!bridgeMode&&!isAdmin&&stream&&!pc&&data.offer){makePeer(data.iceServers);await pc.setRemoteDescription({type:'offer',sdp:data.offer});for(const track of stream.getTracks())pc.addTrack(track,stream);const answer=await pc.createAnswer();await pc.setLocalDescription(answer);await api('answer',{sdp:answer.sdp});}
  if(!bridgeMode&&isAdmin&&pc&&!pc.remoteDescription&&data.answer)await pc.setRemoteDescription({type:'answer',sdp:data.answer});
  if(data.bridge){
   bridgeMode=true;video.hidden=true;video.style.setProperty('display','none','important');mirror.hidden=!isAdmin;liveConnect();
   if(pc){pc.onconnectionstatechange=null;pc.close();pc=null;dc=null;}
   if(isAdmin){remoteGrant=session.control_state==='accepted'?data.bridge.grant:'';if(!lastLiveAt||Date.now()-lastLiveAt>3000)bridgeRender(data.bridge.frame);status.textContent='Pagina del gestionale collegata tramite server';}
   else{localGrant=session.control_state==='accepted'?data.bridge.grant:'';for(const entry of data.bridge.commands){if(entry.seq>bridgeAck){bridgeApply(entry.command);bridgeAck=entry.seq;}}}
  }
  for(const signal of data.signals){cursor=Math.max(cursor,signal.id);candidates.push(signal.candidate);}await flushCandidates();
  if(!isAdmin){if(session.control_state==='requested'&&!remoteAllowed){notice(session.admin_name+' chiede anche il controllo di mouse e tastiera della scheda del gestionale. Puoi rifiutare.');allowButton.hidden=!currentTab();rejectControl.hidden=false;}else{allowButton.hidden=true;rejectControl.hidden=true;}if(session.control_state!=='accepted'){remoteAllowed=false;localGrant='';}revokeButton.hidden=!remoteAllowed;}
  else{release.hidden=session.control_state!=='accepted';if(bridgeMode||pc?.connectionState==='connected')status.textContent=session.control_state==='accepted'?'Condivisione collegata · controllo della scheda autorizzato':session.control_state==='requested'?'Condivisione collegata · in attesa della seconda accettazione':'Condivisione collegata · sola visualizzazione';typing.hidden=session.control_state!=='accepted';controlButton.disabled=session.state!=='active'||!session.control_capable||['requested','accepted'].includes(session.control_state);remoteHint.textContent=bridgeMode?(session.control_state==='accepted'?'Controllo autorizzato della pagina condivisa. Seleziona un campo e usa la tastiera o Invia testo. File e password esclusi.':'Pagina condivisa tramite server, anche senza cattura schermo. Il controllo richiede una seconda accettazione.'):session.control_state==='accepted'?'Controllo autorizzato: clicca il video per usare i campi del gestionale. Password, altre applicazioni e comandi di sistema sono esclusi.':session.control_state==='requested'?'In attesa della seconda accettazione del dipendente.':session.control_capable?'Solo visualizzazione. Il controllo richiede una seconda accettazione.':'Solo visualizzazione: il controllo richiede la condivisione verificata di questa scheda del gestionale in un browser compatibile.';}
  if(!bridgeMode&&connectingAt&&Date.now()-connectingAt>45000&&pc?.connectionState!=='connected')stop('Collegamento non riuscito. Controllare HTTPS, rete e configurazione TURN.');
 }catch(e){status.textContent=e.message+'; riconnessione in corso.';if(Date.now()-pulseAt>40000)stop('Connessione persa: condivisione interrotta.');}
 finally{polling=false;if(session?.id===id)timer=setTimeout(pollSession,1200);}
}
async function allow(){if(!currentTab()){notice('Il controllo è disponibile solo condividendo questa scheda del gestionale.');return;}try{const r=await api('allow-control');session=r.session;localGrant=bridgeMode?(r.bridge?.grant||''):newId();remoteAllowed=true;if(liveSocket?.readyState===WebSocket.OPEN)liveSocket.send(JSON.stringify({type:'ping'}));if(dc?.readyState==='open')dc.send(JSON.stringify({type:'grant',nonce:localGrant}));allowButton.hidden=true;rejectControl.hidden=true;revokeButton.hidden=false;notice('Controllo mouse e tastiera autorizzato a '+session.admin_name+' SOLO per questa scheda. Puoi revocarlo subito.');}catch(e){notice(e.message);}}
async function revoke(disable=false){remoteAllowed=false;localGrant='';if(dc?.readyState==='open')dc.send(JSON.stringify({type:'revoked'}));allowButton.hidden=true;rejectControl.hidden=true;revokeButton.hidden=true;try{const r=await api('revoke-control',disable?{disable:'1'}:{});session=r.session;}catch(_){}notice('Controllo revocato. La sola condivisione schermo continua fino a Interrompi.');}
async function stop(message,notify=true){if(stopping)return;bridgeMode=false;clearTimeout(bridgeTimer);clearTimeout(liveRetry);if(liveSocket){liveSocket.onclose=null;liveSocket.close();liveSocket=null;}livePaired=false;liveConnecting=false;lastLiveAt=0;frameTimes=[];bridgeMaps.clear();bridgeFrame=null;bridgeSelected=null;bridgeAck=0;mirror.hidden=true;pendingReplica=null;lastReplicaHTML='';if(replicaReady)replica.contentDocument.body.replaceChildren();video.hidden=false;video.style.removeProperty('display');stopping=true;const old=session;remoteAllowed=false;localGrant='';remoteGrant='';session=null;connectingAt=0;clearTimeout(timer);if(notify&&old)post('/controllo/assistenza/'+old.id+'/stop').catch(()=>{});if(stream){for(const track of stream.getTracks())track.stop();stream=null;}pc?.close();pc=null;dc=null;candidates=[];cursor=0;video.srcObject=null;release.hidden=true;typingText.value='';typing.hidden=true;banner.hidden=true;allowButton.hidden=true;rejectControl.hidden=true;revokeButton.hidden=true;status.textContent=message;if(dialog.open)dialog.close();stopping=false;}
async function accept(request){
 if(session||accepting)return;if(!window.isSecureContext||!navigator.mediaDevices?.getDisplayMedia){acceptBridge(request);return;}accepting=true;
 try{
  // getDisplayMedia is invoked directly by the employee's own click, before network awaits.
  stream=await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'browser'},audio:false,preferCurrentTab:true,selfBrowserSurface:'include'});
  session=request;startedAt=Date.now();pulseAt=Date.now();capable=currentTab();const r=await api('accept',{capable:capable?'1':'0'});session=r.session;
  const track=stream.getVideoTracks()[0];if(track.readyState!=='live'){stop('Condivisione già interrotta dal browser.');return;}track.addEventListener('ended',()=>stop('Condivisione interrotta dal browser.'));track.addEventListener('capturehandlechange',()=>{if(!currentTab())revoke(true);});
  invite.hidden=true;notice('Condivisione autorizzata a '+session.admin_name+'. Collegamento in corso…');pollSession();
 }catch(e){if(stream){stream.getTracks().forEach(t=>t.stop());stream=null;}session=null;notice('Condivisione schermo non avviata: '+e.message+'. Puoi usare Condividi solo il gestionale.');}finally{accepting=false;}
}
const invite=el('aside','',document.body,'assist-invite');invite.id='assist-invite';invite.hidden=true;const inviteText=el('p','',invite);let invitation=null;
button('Accetta e condividi il gestionale',invite,()=>{if(invitation)acceptBridge(invitation);});button('Condividi schermo (se supportato)',invite,()=>{if(invitation)accept(invitation);});button('Rifiuta',invite,async()=>{const old=invitation;invitation=null;invite.hidden=true;if(old)try{await post('/controllo/assistenza/'+old.id+'/decline');}catch(_){};});
async function invitations(){if(!isAdmin&&!session){try{const r=await fetch('/controllo/assistenza/inviti',{credentials:'same-origin',cache:'no-store'});if(r.ok){const data=await r.json();invitation=data.requests[0]||null;invite.hidden=!invitation;if(invitation)inviteText.textContent=invitation.admin_name+' richiede assistenza. Puoi condividere solo questa pagina del gestionale anche dal telefono, senza installazioni. Mouse e tastiera richiedono una seconda accettazione. La condivisione termina cambiando pagina.';}}catch(_){}}setTimeout(invitations,3000);}
window.ControlAssist={request:async(id,name)=>{if(session){dialog.showModal();return;}try{const r=await post('/controllo/assistenza/richiedi',{target_id:id});session=r.session;cursor=0;startedAt=Date.now();pulseAt=Date.now();title.textContent='Assistenza a '+name;status.textContent='Richiesta inviata. In attesa che il dipendente accetti la condivisione del gestionale o dello schermo.';controlButton.disabled=true;dialog.showModal();if(window.RTCPeerConnection){makePeer(r.iceServers);pc.addTransceiver('video',{direction:'recvonly'});attachChannel(pc.createDataChannel('remote'));const offer=await pc.createOffer();await pc.setLocalDescription(offer);await api('offer',{sdp:offer.sdp});}pollSession();}catch(e){stop(e.message);alert(e.message);}}};
// Faithful, inert DOM replica at the employee viewport; live frames never execute code.
let bridgeMode=false,bridgeAck=0,bridgeFrame=null,bridgeSelected=null,bridgeBusy=false,bridgeTimer=null,bridgeMaps=new Map(),accepting=false,bridgeNodeIds=new WeakMap(),bridgeNodeSerial=0;
let liveSocket=null,liveRetry=null,livePaired=false,liveConnecting=false,lastLiveAt=0,frameTimes=[],lastReplicaHTML='',replicaHead='',replicaReady=false,pendingReplica=null,lastPublishedHTML='',replicaVersion=0;
const mirror=el('div','',dialog,'assist-mirror');mirror.hidden=true;mirror.tabIndex=0;mirror.setAttribute('aria-label','Pagina del gestionale condivisa');dialog.insertBefore(mirror,tools);
const replica=el('iframe','',mirror,'assist-replica');replica.title='Copia della pagina del dipendente';replica.setAttribute('sandbox','allow-same-origin');replica.setAttribute('scrolling','no');
const meter=el('p','In attesa della pagina del dipendente…',mirror,'assist-frame-meter');
const privateSelector='#assist-banner,#assist-viewer,#assist-invite,[data-assist-private],script,noscript,iframe,object,embed,input[type="password"],input[type="hidden"],input[type="file"],input[autocomplete="one-time-code"],input[autocomplete*="password"]';
const allowedTags=new Set('html head body div span p a button input textarea select option optgroup label form fieldset legend h1 h2 h3 h4 h5 h6 ul ol li table thead tbody tfoot tr th td caption colgroup col section article aside header footer nav main details summary dialog img picture source canvas br hr strong b em i u small time pre code blockquote dl dt dd style link'.split(' '));
const allowedAttrs=new Set('id class style title role type name value placeholder disabled readonly multiple checked selected open min max step size rows cols colspan rowspan width height loading aria-label aria-expanded aria-hidden tabindex data-theme data-brand'.split(' '));
function bridgePrivate(node){return !node||!!node.closest?.(privateSelector)||protectedNode(node);}
function nodeId(node){if(!bridgeNodeIds.has(node))bridgeNodeIds.set(node,String(++bridgeNodeSerial));return bridgeNodeIds.get(node);}
function safeImage(value){try{if(/^data:image\/(png|jpeg|webp|gif);base64,/i.test(value))return value;const u=new URL(value,location.origin);return u.origin===location.origin&&u.pathname.startsWith('/static/')?u.href:'';}catch(_){return '';}}
function safeStyleSheet(value){try{const u=new URL(value,location.origin);return u.origin===location.origin&&u.pathname.startsWith('/static/')?u.href:'';}catch(_){return '';}}
function cleanReplica(node,doc,sending,map){
 if(node.nodeType===3)return doc.createTextNode(node.textContent);
 if(node.nodeType!==1)return null;
 const tag=node.tagName.toLowerCase();
 if(!allowedTags.has(tag)||node.matches(privateSelector)||protectedNode(node))return null;
 // Hidden form fields, closed dialogs and private sections must never contribute text.
 if(node.hidden||node.getAttribute('aria-hidden')==='true'||(tag==='dialog'&&!node.open))return null;
 if(sending&&!['style','link'].includes(tag)&&getComputedStyle(node).display==='none')return null;
 if(tag==='link'){
  const href=safeStyleSheet(node.getAttribute('href')||'');if(node.getAttribute('rel')!=='stylesheet'||!href)return null;
  const out=doc.createElement('link');out.rel='stylesheet';out.href=href;return out;
 }
 let out;
 if(sending&&tag==='canvas'){
  out=doc.createElement('img');try{out.src=node.toDataURL('image/png');}catch(_){out.alt='Grafico non disponibile';}
 }else out=doc.createElement(tag==='canvas'?'div':tag);
 for(const attr of [...node.attributes]){
  if(allowedAttrs.has(attr.name)||(!sending&&/^data-assist-(key|target)$/.test(attr.name)))out.setAttribute(attr.name,attr.value);
 }
 if(tag==='style'){out.textContent=node.textContent;return out;}
 if(tag==='a')out.setAttribute('href','#');
 if(tag==='img'){const src=safeImage(node.getAttribute('src')||'');if(src)out.setAttribute('src',src);out.alt=node.getAttribute('alt')||'';}
 if(sending){
  const id=nodeId(node);out.dataset.assistKey=id;map.set(id,node);
  if(node.matches('button,a,input,select,textarea,summary,[role="button"]')){out.dataset.assistTarget=id;map.set(id,node);}
  if(node.matches('input,textarea,select')){
   if(tag==='textarea')out.textContent=node.value;
   if(tag==='input'){out.setAttribute('value',node.value);out.toggleAttribute('checked',node.checked);}
  }
  if(tag==='option')out.toggleAttribute('selected',node.selected);
 }
 if(tag!=='textarea'||!sending)for(const child of node.childNodes){const copy=cleanReplica(child,doc,sending,map);if(copy)out.appendChild(copy);}
 return out;
}
function bridgeSnapshot(){
 const doc=document.implementation.createHTMLDocument(''),map=new Map(),id=newId();
 const body=cleanReplica(document.body,doc,true,map);
 const head=doc.createElement('head');for(const node of document.head.querySelectorAll('style,link[rel="stylesheet"]')){const copy=cleanReplica(node,doc,true,map);if(copy)head.appendChild(copy);}
 const scrolls=[];for(const node of document.body.querySelectorAll('*'))if((node.scrollTop||node.scrollLeft)&&bridgeNodeIds.has(node)&&!bridgePrivate(node))scrolls.push({id:nodeId(node),x:node.scrollLeft,y:node.scrollTop});
 bridgeMaps.set(id,map);while(bridgeMaps.size>30)bridgeMaps.delete(bridgeMaps.keys().next().value);
 return {id,width:innerWidth,height:innerHeight,nodes:[],html:body.outerHTML,head:head.innerHTML,rootClass:document.documentElement.className,rootStyle:document.documentElement.getAttribute('style')||'',theme:document.documentElement.getAttribute('data-theme')||'',x:scrollX,y:scrollY,scrolls,focus:bridgeNodeIds.get(document.activeElement)||'',version:++replicaVersion};
}
function liveConnect(){
 if(!session||!bridgeMode||liveConnecting||liveSocket?.readyState===WebSocket.OPEN)return;
 liveConnecting=true;const rid=session.id;
 const socket=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/controllo/assistenza/'+rid+'/live');liveSocket=socket;
 socket.onopen=()=>{liveConnecting=false;if(session?.id!==rid){socket.close();return;}socket.send(JSON.stringify({type:'ping'}));};
 socket.onmessage=event=>{
  if(session?.id!==rid)return;
  try{const data=JSON.parse(event.data);
   if(data.type==='ready'||data.type==='pong'){livePaired=!!data.paired;}
   else if(data.type==='consent'){
    if((data.updated||0)<(session.updated||0))return;session.updated=data.updated;session.control_state=data.state;
    if(isAdmin)remoteGrant=data.state==='accepted'?data.grant:'';
    else{localGrant=data.state==='accepted'?data.grant:'';if(data.state!=='accepted')remoteAllowed=false;}
   }else if(data.type==='frame'&&isAdmin){lastLiveAt=Date.now();bridgeRender(data.frame);}
   else if(data.type==='command'&&!isAdmin)bridgeApply(data.command);
  }catch(e){meter.textContent='Impossibile aggiornare la vista: '+e.message;}
 };
 socket.onclose=()=>{if(liveSocket!==socket)return;liveSocket=null;liveConnecting=false;livePaired=false;if(bridgeMode&&session){meter.textContent='Collegamento rapido interrotto; riconnessione in corso. Vista temporanea più lenta.';clearTimeout(liveRetry);liveRetry=setTimeout(liveConnect,1500);}};
 socket.onerror=()=>{};
}
async function bridgePublish(){
 if(!bridgeMode||isAdmin||!session||bridgeBusy)return;bridgeBusy=true;const started=performance.now();
 try{
  if(document.visibilityState!=='hidden'){
   const frame=bridgeSnapshot();
   if(liveSocket?.readyState===WebSocket.OPEN&&livePaired){
    const raw=JSON.stringify({type:'frame',frame});
    if(new Blob([raw]).size>1400000)throw new Error('Pagina troppo grande per il collegamento rapido. Riduci il numero di articoli visualizzati.');
    if(liveSocket.bufferedAmount<600000)liveSocket.send(raw);
   }else if(Date.now()-lastLiveAt>1500){lastLiveAt=Date.now();await api('bridge-frame',{frame:JSON.stringify(frame),ack:bridgeAck});}
  }
 }catch(e){notice('Collegamento pagina: '+e.message);}
 finally{bridgeBusy=false;if(bridgeMode&&session)bridgeTimer=setTimeout(bridgePublish,Math.max(0,166-(performance.now()-started)));}
}
function patchReplica(oldNode,newNode){
 if(oldNode.nodeType!==newNode.nodeType||oldNode.nodeName!==newNode.nodeName){oldNode.replaceWith(newNode.cloneNode(true));return;}
 if(oldNode.nodeType===3){if(oldNode.textContent!==newNode.textContent)oldNode.textContent=newNode.textContent;return;}
 if(oldNode.nodeType!==1)return;
 for(const a of [...oldNode.attributes])if(!newNode.hasAttribute(a.name))oldNode.removeAttribute(a.name);
 for(const a of [...newNode.attributes])if(oldNode.getAttribute(a.name)!==a.value)oldNode.setAttribute(a.name,a.value);
 if(oldNode.tagName==='INPUT'){oldNode.value=newNode.getAttribute('value')||'';oldNode.checked=newNode.hasAttribute('checked');}
 if(oldNode.tagName==='OPTION')oldNode.selected=newNode.hasAttribute('selected');
 let index=0;while(index<newNode.childNodes.length||index<oldNode.childNodes.length){
  const a=oldNode.childNodes[index],b=newNode.childNodes[index];
  if(!b){a.remove();continue;}if(!a)oldNode.appendChild(b.cloneNode(true));else patchReplica(a,b);index++;
 }
 if(oldNode.tagName==='TEXTAREA')oldNode.value=newNode.textContent;
}
function validFrame(frame){return frame&&typeof frame.html==='string'&&Number.isFinite(frame.width)&&frame.width>=100&&frame.width<=10000&&Number.isFinite(frame.height)&&frame.height>=100&&frame.height<=20000;}
function sizeReplica(frame){
 const available=Math.max(100,Math.min(dialog.clientWidth-40,1000));const scale=Math.min(1,available/frame.width,(innerHeight-250)/frame.height);
 const fit=Math.max(.2,scale);mirror.style.width=Math.ceil(frame.width*fit)+'px';mirror.style.height=Math.ceil(frame.height*fit)+'px';
 replica.style.width=frame.width+'px';replica.style.height=frame.height+'px';replica.style.transform='scale('+fit+')';
 mirror.dataset.sourceWidth=frame.width;mirror.dataset.sourceHeight=frame.height;
}
function paintReplica(frame){
 if(!replicaReady){pendingReplica=frame;return;}
 const doc=replica.contentDocument;
 if(frame.html!==lastReplicaHTML){
  const parsed=new DOMParser().parseFromString(frame.html,'text/html');const safe=cleanReplica(parsed.body,doc,false,new Map());
  if(safe)patchReplica(doc.body,safe);lastReplicaHTML=frame.html;
 }
 if(frame.head!==replicaHead){
  for(const node of [...doc.head.querySelectorAll('[data-copy-style]')])node.remove();
  const parsed=new DOMParser().parseFromString('<html><head>'+frame.head+'</head><body></body></html>','text/html');
  for(const node of parsed.head.querySelectorAll('style,link')){const safe=cleanReplica(node,doc,false,new Map());if(safe){safe.dataset.copyStyle='1';doc.head.appendChild(safe);}}
  replicaHead=frame.head;
 }
 doc.documentElement.className=frame.rootClass||'';doc.documentElement.setAttribute('style',frame.rootStyle||'');doc.documentElement.setAttribute('data-theme',frame.theme||'');
 replica.contentWindow.scrollTo(frame.x||0,frame.y||0);
 for(const s of frame.scrolls||[]){const n=doc.querySelector('[data-assist-key="'+String(s.id).replace(/[^0-9]/g,'')+'"]');if(n){n.scrollLeft=s.x;n.scrollTop=s.y;}}
 if(frame.focus){const n=doc.querySelector('[data-assist-target="'+String(frame.focus).replace(/[^0-9]/g,'')+'"]');if(n&&doc.activeElement!==n)n.focus({preventScroll:true});}
}
function bridgeRender(frame){
 if(!validFrame(frame))return;
 bridgeFrame=frame;bridgeSelected=frame.focus?{target:frame.focus,frame:frame.id}:bridgeSelected;if(bridgeSelected)bridgeSelected.frame=frame.id;
 sizeReplica(frame);paintReplica(frame);
 const now=performance.now();frameTimes.push(now);frameTimes=frameTimes.filter(t=>now-t<2000);
 const fps=frameTimes.length>1?(frameTimes.length-1)*1000/(frameTimes[frameTimes.length-1]-frameTimes[0]):0;
 meter.textContent=frame.width+' × '+frame.height+' · '+(livePaired?'Collegamento rapido · '+fps.toFixed(1)+' aggiornamenti/s':'Collegamento di riserva · aggiornamento lento');
 mirror.dataset.receivedFrames=String(Number(mirror.dataset.receivedFrames||0)+1);mirror.dataset.fps=fps.toFixed(2);
}
function bridgeSend(cmd){
 if(!isAdmin||!bridgeMode||session?.control_state!=='accepted'||!remoteGrant)return;
 if(['text','key'].includes(cmd.type)&&!bridgeSelected){status.textContent='Seleziona prima un campo nella pagina condivisa.';return;}
 const selected=bridgeSelected||{},command={...selected,...cmd,frame:cmd.frame||selected.frame||bridgeFrame?.id,grant:remoteGrant};
 if(liveSocket?.readyState===WebSocket.OPEN&&livePaired)liveSocket.send(JSON.stringify({type:'command',command}));
 else api('bridge-command',{command:JSON.stringify(command)}).catch(e=>{status.textContent=e.message;});
}
function bridgeApply(cmd){
 if(!remoteAllowed||session?.control_state!=='accepted'||!localGrant||cmd.grant!==localGrant)return;
 if(cmd.type==='scroll'){if(!Number.isFinite(cmd.delta))return;let target=bridgeMaps.get(cmd.frame)?.get(cmd.scrollTarget);if(target&&bridgePrivate(target))return;while(target&&target!==document.body){if(target.scrollHeight>target.clientHeight+2&&['auto','scroll'].includes(getComputedStyle(target).overflowY))break;target=target.parentElement;}(target&&target!==document.body?target:window).scrollBy(0,Math.max(-600,Math.min(600,cmd.delta)));return;}
 const node=bridgeMaps.get(cmd.frame)?.get(cmd.target);
 if(!node?.isConnected||bridgePrivate(node)||node.disabled||!node.getClientRects().length)return;
 if(cmd.type==='click'){
  if(node.tagName==='A'){try{const url=new URL(node.href,location.href);if(url.origin!==location.origin||!['http:','https:'].includes(url.protocol))return;}catch(_){return;}}
  node.focus();if(node.tagName!=='SELECT')node.click();
 }else{node.focus();applyCommand(cmd);}
}
function replicaKey(event){if(event.ctrlKey||event.altKey||event.metaKey||event.key==='Escape')return;event.preventDefault();if(session?.control_state!=='accepted')return;if(event.key.length===1)bridgeSend({type:'text',text:event.key});else if(['Tab','Enter','Backspace','Delete','ArrowLeft','ArrowRight','ArrowDown','ArrowUp','Home','End'].includes(event.key))bridgeSend({type:'key',key:event.key});}
replica.addEventListener('load',()=>{
 replicaReady=true;const doc=replica.contentDocument;
 doc.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();const node=event.target.closest('[data-assist-target]');if(!node)return;bridgeSelected={target:node.dataset.assistTarget,frame:bridgeFrame?.id};bridgeSend({type:'click'});},true);
 doc.addEventListener('submit',event=>event.preventDefault(),true);doc.addEventListener('keydown',replicaKey,true);
 doc.addEventListener('wheel',event=>{event.preventDefault();bridgeSend({type:'scroll',scrollTarget:event.target.closest('[data-assist-key]')?.dataset.assistKey,delta:event.deltaY});},{passive:false});
 let touchY=null;doc.addEventListener('touchstart',event=>{touchY=event.touches[0]?.clientY;},{passive:true});doc.addEventListener('touchmove',event=>{event.preventDefault();const y=event.touches[0]?.clientY;if(touchY!==null&&Number.isFinite(y))bridgeSend({type:'scroll',scrollTarget:event.target.closest('[data-assist-key]')?.dataset.assistKey,delta:touchY-y});touchY=y;},{passive:false});
 if(pendingReplica){const f=pendingReplica;pendingReplica=null;paintReplica(f);}
});
replica.srcdoc='<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;self&#39; &#39;unsafe-inline&#39;; img-src &#39;self&#39; data:; font-src &#39;self&#39; data:; base-uri &#39;none&#39;; form-action &#39;none&#39;; script-src &#39;none&#39;"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html{scroll-behavior:auto!important}*{scroll-behavior:auto!important}</style></head><body></body></html>';
mirror.addEventListener('keydown',replicaKey);
window.addEventListener('resize',()=>{if(bridgeFrame)sizeReplica(bridgeFrame);});
async function acceptBridge(request){
 if(session||accepting)return;accepting=true;session=request;bridgeMode=true;bridgeAck=0;bridgeSelected=null;
 try{const r=await api('accept',{transport:'bridge',capable:'1'});session=r.session;pulseAt=Date.now();invite.hidden=true;notice('Stai condividendo questa pagina del gestionale con '+session.admin_name+'. Il controllo richiede una seconda accettazione.');liveConnect();bridgePublish();pollSession();}
 catch(e){bridgeMode=false;session=null;notice('Collegamento non avviato: '+e.message);}
 finally{accepting=false;}
}

window.addEventListener('pagehide',()=>stop('Pagina chiusa: assistenza interrotta.'));invitations();
})();
