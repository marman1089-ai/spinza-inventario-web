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
  const r=await fetch('/controllo/assistenza/'+id+'?cursor='+cursor,{credentials:'same-origin',cache:'no-store'});if(!r.ok)throw new Error('Sessione non raggiungibile');const data=await r.json();if(session?.id!==id)return;session=data.session;if(['accepted','active'].includes(session.state)&&!connectingAt)connectingAt=Date.now();
  if(!['pending','accepted','active'].includes(session.state)){stop('Sessione '+session.state,false);return;}
  if(Date.now()-pulseAt>8000){await api('pulse');pulseAt=Date.now();}
  if(!bridgeMode&&!isAdmin&&stream&&!pc&&data.offer){makePeer(data.iceServers);await pc.setRemoteDescription({type:'offer',sdp:data.offer});for(const track of stream.getTracks())pc.addTrack(track,stream);const answer=await pc.createAnswer();await pc.setLocalDescription(answer);await api('answer',{sdp:answer.sdp});}
  if(!bridgeMode&&isAdmin&&pc&&!pc.remoteDescription&&data.answer)await pc.setRemoteDescription({type:'answer',sdp:data.answer});
  if(data.bridge){
   bridgeMode=true;video.hidden=true;mirror.hidden=!isAdmin;
   if(pc){pc.onconnectionstatechange=null;pc.close();pc=null;dc=null;}
   if(isAdmin){remoteGrant=session.control_state==='accepted'?data.bridge.grant:'';bridgeRender(data.bridge.frame);status.textContent='Pagina del gestionale collegata tramite server';}
   else{localGrant=session.control_state==='accepted'?data.bridge.grant:'';for(const entry of data.bridge.commands){if(entry.seq>bridgeAck){bridgeApply(entry.command);bridgeAck=entry.seq;}}}
  }
  for(const signal of data.signals){cursor=Math.max(cursor,signal.id);candidates.push(signal.candidate);}await flushCandidates();
  if(!isAdmin){if(session.control_state==='requested'&&!remoteAllowed){notice(session.admin_name+' chiede anche il controllo di mouse e tastiera della scheda del gestionale. Puoi rifiutare.');allowButton.hidden=!currentTab();rejectControl.hidden=false;}else{allowButton.hidden=true;rejectControl.hidden=true;}if(session.control_state!=='accepted'){remoteAllowed=false;localGrant='';}revokeButton.hidden=!remoteAllowed;}
  else{release.hidden=session.control_state!=='accepted';if(bridgeMode||pc?.connectionState==='connected')status.textContent=session.control_state==='accepted'?'Condivisione collegata · controllo della scheda autorizzato':session.control_state==='requested'?'Condivisione collegata · in attesa della seconda accettazione':'Condivisione collegata · sola visualizzazione';typing.hidden=session.control_state!=='accepted';controlButton.disabled=session.state!=='active'||!session.control_capable||['requested','accepted'].includes(session.control_state);remoteHint.textContent=bridgeMode?(session.control_state==='accepted'?'Controllo autorizzato della pagina condivisa. Seleziona un campo e usa la tastiera o Invia testo. File e password esclusi.':'Pagina condivisa tramite server, anche senza cattura schermo. Il controllo richiede una seconda accettazione.'):session.control_state==='accepted'?'Controllo autorizzato: clicca il video per usare i campi del gestionale. Password, altre applicazioni e comandi di sistema sono esclusi.':session.control_state==='requested'?'In attesa della seconda accettazione del dipendente.':session.control_capable?'Solo visualizzazione. Il controllo richiede una seconda accettazione.':'Solo visualizzazione: il controllo richiede la condivisione verificata di questa scheda del gestionale in un browser compatibile.';}
  if(!bridgeMode&&connectingAt&&Date.now()-connectingAt>45000&&pc?.connectionState!=='connected')stop('Collegamento non riuscito. Controllare HTTPS, rete e configurazione TURN.');
 }catch(e){status.textContent=e.message+'; riconnessione in corso.';if(Date.now()-pulseAt>40000)stop('Connessione persa: condivisione interrotta.');}
 finally{polling=false;if(session?.id===id)timer=setTimeout(pollSession,1200);}
}
async function allow(){if(!currentTab()){notice('Il controllo è disponibile solo condividendo questa scheda del gestionale.');return;}try{const r=await api('allow-control');session=r.session;localGrant=bridgeMode?'':newId();remoteAllowed=true;if(dc?.readyState==='open')dc.send(JSON.stringify({type:'grant',nonce:localGrant}));allowButton.hidden=true;rejectControl.hidden=true;revokeButton.hidden=false;notice('Controllo mouse e tastiera autorizzato a '+session.admin_name+' SOLO per questa scheda. Puoi revocarlo subito.');}catch(e){notice(e.message);}}
async function revoke(disable=false){remoteAllowed=false;localGrant='';if(dc?.readyState==='open')dc.send(JSON.stringify({type:'revoked'}));allowButton.hidden=true;rejectControl.hidden=true;revokeButton.hidden=true;try{const r=await api('revoke-control',disable?{disable:'1'}:{});session=r.session;}catch(_){}notice('Controllo revocato. La sola condivisione schermo continua fino a Interrompi.');}
async function stop(message,notify=true){if(stopping)return;bridgeMode=false;clearTimeout(bridgeTimer);bridgeMaps.clear();bridgeFrame=null;bridgeSelected=null;bridgeAck=0;mirror.hidden=true;mirrorSurface.replaceChildren();video.hidden=false;stopping=true;const old=session;remoteAllowed=false;localGrant='';remoteGrant='';session=null;connectingAt=0;clearTimeout(timer);if(notify&&old)post('/controllo/assistenza/'+old.id+'/stop').catch(()=>{});if(stream){for(const track of stream.getTracks())track.stop();stream=null;}pc?.close();pc=null;dc=null;candidates=[];cursor=0;video.srcObject=null;release.hidden=true;typingText.value='';typing.hidden=true;banner.hidden=true;allowButton.hidden=true;rejectControl.hidden=true;revokeButton.hidden=true;status.textContent=message;if(dialog.open)dialog.close();stopping=false;}
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
// Data-only page mirror: no HTML, external resources, passwords or executable content.
let bridgeMode=false,bridgeAck=0,bridgeFrame=null,bridgeSelected=null,bridgeBusy=false,bridgeTimer=null,bridgeMaps=new Map(),accepting=false,bridgeNodeIds=new WeakMap(),bridgeNodeSerial=0;
const mirror=el('div','',dialog,'assist-mirror');mirror.hidden=true;mirror.tabIndex=0;mirror.setAttribute('aria-label','Pagina del gestionale condivisa');
const mirrorSurface=el('div','',mirror,'assist-mirror-surface');
const privateSelector='#assist-banner,#assist-viewer,#assist-invite,[data-assist-private],script,style,noscript,iframe,object,embed,input[type="password"],input[type="hidden"],input[type="file"],input[autocomplete="one-time-code"],input[autocomplete*="password"]';
function bridgePrivate(node){return !node||!!node.closest?.(privateSelector)||protectedNode(node);}
function bridgeSnapshot(){
 const nodes=[],map=new Map(),id=newId(),width=innerWidth,height=innerHeight;
 const rect=node=>{const r=node.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<height&&r.left<width?r:null;};
 const style=node=>{const s=getComputedStyle(node);return {color:s.color,background:s.backgroundColor,fontSize:s.fontSize,fontWeight:s.fontWeight,borderColor:s.borderTopColor};};
 const box=(r,text,s,target='',kind='text')=>({x:r.x,y:r.y,w:r.width,h:r.height,text:String(text).slice(0,2500),...s,target,kind});
 const walk=document.createTreeWalker(document.body,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT);
 let node;while((node=walk.nextNode())&&nodes.length<1400){
  const parent=node.nodeType===3?node.parentElement:node;
  if(bridgePrivate(parent)||parent.closest('textarea,select,button,a,input')&&node.nodeType===3)continue;
  if(getComputedStyle(parent).visibility==='hidden'||getComputedStyle(parent).display==='none')continue;
  if(node.nodeType===3){if(!node.textContent.trim())continue;const range=document.createRange();range.selectNode(node);const r=range.getBoundingClientRect();if(r.width&&r.height&&r.bottom>0&&r.top<height&&r.right>0&&r.left<width)nodes.push(box(r,node.textContent,style(parent)));continue;}
  const r=rect(node);if(!r)continue;
  const tag=node.tagName;
  if(['BUTTON','A','INPUT','TEXTAREA','SELECT','SUMMARY'].includes(tag)){
   let text=node.value??node.textContent??'';
   if(tag==='INPUT'&&['checkbox','radio'].includes(node.type))text=node.checked?'☑':'☐';
   if(tag==='SELECT')text=node.selectedOptions[0]?.textContent||'';
   if(tag==='INPUT'&&['submit','button'].includes(node.type))text=node.value;
   if(!['INPUT','TEXTAREA','SELECT'].includes(tag))text=node.textContent;
   if(!bridgeNodeIds.has(node))bridgeNodeIds.set(node,String(++bridgeNodeSerial));const target=bridgeNodeIds.get(node);map.set(target,node);
   nodes.push(box(r,text,style(node),target,editable(node)?'field':'button'));
  }else if(['IMG','CANVAS','VIDEO'].includes(tag))nodes.push(box(r,tag==='IMG'?(node.alt||'Immagine'):'Contenuto grafico',style(node),'','graphic'));
 }
 bridgeMaps.set(id,map);while(bridgeMaps.size>3)bridgeMaps.delete(bridgeMaps.keys().next().value);
 return {id,width,height,nodes,focus:bridgeNodeIds.get(document.activeElement)||'',background:getComputedStyle(document.body).backgroundColor};
}
async function bridgePublish(){
 if(!bridgeMode||isAdmin||!session||bridgeBusy)return;bridgeBusy=true;
 try{if(document.visibilityState!=='hidden')await api('bridge-frame',{frame:JSON.stringify(bridgeSnapshot()),ack:bridgeAck});}
 catch(e){notice('Collegamento pagina: '+e.message);}
 finally{bridgeBusy=false;if(bridgeMode&&session)bridgeTimer=setTimeout(bridgePublish,1600);}
}
function bridgeRender(frame){
 if(!frame||!Array.isArray(frame.nodes)||!Number.isFinite(frame.width)||frame.width<100||frame.width>10000||!Number.isFinite(frame.height)||frame.height<100||frame.height>20000)return;
 bridgeFrame=frame;if(frame.focus&&frame.nodes.some(n=>n.target===frame.focus))bridgeSelected={target:frame.focus,frame:frame.id};else if(bridgeSelected&&frame.nodes.some(n=>n.target===bridgeSelected.target))bridgeSelected.frame=frame.id;else bridgeSelected=null;const scale=Math.min(1,(mirror.clientWidth||800)/frame.width);mirrorSurface.replaceChildren();mirrorSurface.style.width=frame.width+'px';mirrorSurface.style.height=frame.height+'px';mirrorSurface.style.transform='scale('+scale+')';mirrorSurface.style.backgroundColor=frame.background;mirror.style.height=Math.ceil(frame.height*scale)+'px';
 for(const n of frame.nodes.slice(0,1400)){
  if(![n.x,n.y,n.w,n.h].every(Number.isFinite)||typeof n.text!=='string')continue;
  const cell=el('div',n.text,mirrorSurface,'assist-mirror-node');
  Object.assign(cell.style,{left:n.x+'px',top:n.y+'px',width:Math.max(0,n.w)+'px',height:Math.max(0,n.h)+'px',color:n.color,backgroundColor:n.background,fontSize:n.fontSize,fontWeight:n.fontWeight});
  if(n.target){cell.dataset.target=n.target;cell.dataset.frame=frame.id;cell.style.border='1px solid #8297b0';cell.style.borderRadius='4px';cell.style.cursor='pointer';}
 }
}
function bridgeSend(cmd){
 if(!isAdmin||!bridgeMode||session?.control_state!=='accepted'||!remoteGrant)return;
 if(['text','key'].includes(cmd.type)&&!bridgeSelected){status.textContent='Seleziona prima un campo nella pagina condivisa.';return;}
 const selected=bridgeSelected||{};
 api('bridge-command',{command:JSON.stringify({...selected,...cmd,frame:cmd.frame||selected.frame||bridgeFrame?.id,grant:remoteGrant})}).catch(e=>{status.textContent=e.message;});
}
function bridgeApply(cmd){
 if(!remoteAllowed||session?.control_state!=='accepted'||!localGrant||cmd.grant!==localGrant)return;
 if(cmd.type==='scroll'){if(Number.isFinite(cmd.delta))window.scrollBy(0,Math.max(-600,Math.min(600,cmd.delta)));return;}
 const node=bridgeMaps.get(cmd.frame)?.get(cmd.target);
 if(!node?.isConnected||bridgePrivate(node)||node.disabled||!node.getClientRects().length)return;
 if(cmd.type==='click'){
  if(node.tagName==='A'){try{const url=new URL(node.href,location.href);if(url.origin!==location.origin||!['http:','https:'].includes(url.protocol))return;}catch(_){return;}}
  node.focus();if(node.tagName!=='SELECT')node.click();
 }else{node.focus();applyCommand(cmd);}
}
mirror.addEventListener('click',event=>{const cell=event.target.closest('[data-target]');if(!cell)return;bridgeSelected={target:cell.dataset.target,frame:cell.dataset.frame};bridgeSend({type:'click'});mirror.focus();});
mirror.addEventListener('wheel',event=>{if(session?.control_state!=='accepted')return;event.preventDefault();bridgeSend({type:'scroll',delta:event.deltaY});},{passive:false});
mirror.addEventListener('keydown',event=>{if(event.ctrlKey||event.altKey||event.metaKey||event.key==='Escape')return;if(session?.control_state!=='accepted')return;if(event.key.length===1){event.preventDefault();bridgeSend({type:'text',text:event.key});}else if(['Tab','Enter','Backspace','Delete','ArrowLeft','ArrowRight','ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();bridgeSend({type:'key',key:event.key});}});
async function acceptBridge(request){
 if(session||accepting)return;accepting=true;session=request;bridgeMode=true;bridgeAck=0;bridgeSelected=null;
 try{const r=await api('accept',{transport:'bridge',capable:'1'});session=r.session;pulseAt=Date.now();invite.hidden=true;notice('Stai condividendo SOLO questa pagina del gestionale con '+session.admin_name+'. Il controllo richiede una seconda accettazione.');bridgePublish();pollSession();}
 catch(e){bridgeMode=false;session=null;notice('Collegamento non avviato: '+e.message);}
 finally{accepting=false;}
}

window.addEventListener('pagehide',()=>stop('Pagina chiusa: assistenza interrotta.'));invitations();
})();
