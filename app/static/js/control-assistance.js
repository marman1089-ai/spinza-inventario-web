(() => {
'use strict';
const ctx=document.getElementById('assist-context');if(!ctx)return;
const isAdmin=ctx.dataset.role==='admin';let session=null,pc=null,dc=null,stream=null,cursor=0,timer=null,pulseAt=0,remoteAllowed=false,capable=false,queue=Promise.resolve(),candidates=[],polling=false,stopping=false,startedAt=0,connectingAt=0,localGrant='',remoteGrant='';
const handle=crypto.randomUUID();
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
function currentTab(){try{const track=stream?.getVideoTracks()[0],h=track?.getCaptureHandle?.();return !!h&&h.handle===handle&&h.origin===location.origin&&track.getSettings().displaySurface==='browser';}catch(_){return false;}}
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
function send(cmd){if(!isAdmin||session?.control_state!=='accepted'||!remoteGrant||dc?.readyState!=='open')return;dc.send(JSON.stringify({...cmd,grant:remoteGrant}));}
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
  if(!isAdmin&&stream&&!pc&&data.offer){makePeer(data.iceServers);await pc.setRemoteDescription({type:'offer',sdp:data.offer});for(const track of stream.getTracks())pc.addTrack(track,stream);const answer=await pc.createAnswer();await pc.setLocalDescription(answer);await api('answer',{sdp:answer.sdp});}
  if(isAdmin&&pc&&!pc.remoteDescription&&data.answer)await pc.setRemoteDescription({type:'answer',sdp:data.answer});
  for(const signal of data.signals){cursor=Math.max(cursor,signal.id);candidates.push(signal.candidate);}await flushCandidates();
  if(!isAdmin){if(session.control_state==='requested'&&!remoteAllowed){notice(session.admin_name+' chiede anche il controllo di mouse e tastiera della scheda del gestionale. Puoi rifiutare.');allowButton.hidden=!currentTab();rejectControl.hidden=false;}else{allowButton.hidden=true;rejectControl.hidden=true;}if(session.control_state!=='accepted'){remoteAllowed=false;localGrant='';}revokeButton.hidden=!remoteAllowed;}
  else{release.hidden=session.control_state!=='accepted';if(pc?.connectionState==='connected')status.textContent=session.control_state==='accepted'?'Condivisione collegata · controllo della scheda autorizzato':session.control_state==='requested'?'Condivisione collegata · in attesa della seconda accettazione':'Condivisione collegata · sola visualizzazione';typing.hidden=session.control_state!=='accepted';controlButton.disabled=session.state!=='active'||!session.control_capable||['requested','accepted'].includes(session.control_state);remoteHint.textContent=session.control_state==='accepted'?'Controllo autorizzato: clicca il video per usare i campi del gestionale. Password, altre applicazioni e comandi di sistema sono esclusi.':session.control_state==='requested'?'In attesa della seconda accettazione del dipendente.':session.control_capable?'Solo visualizzazione. Il controllo richiede una seconda accettazione.':'Solo visualizzazione: il controllo richiede la condivisione verificata di questa scheda del gestionale in un browser compatibile.';}
  if(connectingAt&&Date.now()-connectingAt>45000&&pc?.connectionState!=='connected')stop('Collegamento non riuscito. Controllare HTTPS, rete e configurazione TURN.');
 }catch(e){status.textContent=e.message+'; riconnessione in corso.';if(Date.now()-pulseAt>40000)stop('Connessione persa: condivisione interrotta.');}
 finally{polling=false;if(session?.id===id)timer=setTimeout(pollSession,1200);}
}
async function allow(){if(!currentTab()){notice('Il controllo è disponibile solo condividendo questa scheda del gestionale.');return;}try{const r=await api('allow-control');session=r.session;localGrant=crypto.randomUUID();remoteAllowed=true;if(dc?.readyState==='open')dc.send(JSON.stringify({type:'grant',nonce:localGrant}));allowButton.hidden=true;rejectControl.hidden=true;revokeButton.hidden=false;notice('Controllo mouse e tastiera autorizzato a '+session.admin_name+' SOLO per questa scheda. Puoi revocarlo subito.');}catch(e){notice(e.message);}}
async function revoke(disable=false){remoteAllowed=false;localGrant='';if(dc?.readyState==='open')dc.send(JSON.stringify({type:'revoked'}));allowButton.hidden=true;rejectControl.hidden=true;revokeButton.hidden=true;try{const r=await api('revoke-control',disable?{disable:'1'}:{});session=r.session;}catch(_){}notice('Controllo revocato. La sola condivisione schermo continua fino a Interrompi.');}
async function stop(message,notify=true){if(stopping)return;stopping=true;const old=session;remoteAllowed=false;localGrant='';remoteGrant='';session=null;connectingAt=0;clearTimeout(timer);if(notify&&old)post('/controllo/assistenza/'+old.id+'/stop').catch(()=>{});if(stream){for(const track of stream.getTracks())track.stop();stream=null;}pc?.close();pc=null;dc=null;candidates=[];cursor=0;video.srcObject=null;release.hidden=true;typingText.value='';typing.hidden=true;banner.hidden=true;allowButton.hidden=true;rejectControl.hidden=true;revokeButton.hidden=true;status.textContent=message;if(dialog.open)dialog.close();stopping=false;}
async function accept(request){
 if(session)return;if(!window.isSecureContext||!navigator.mediaDevices?.getDisplayMedia){notice('Condivisione non disponibile: occorre HTTPS e un browser con condivisione schermo. Su alcuni telefoni non è supportata.');return;}
 try{
  // getDisplayMedia is invoked directly by the employee's own click, before network awaits.
  stream=await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'browser'},audio:false,preferCurrentTab:true,selfBrowserSurface:'include'});
  session=request;startedAt=Date.now();pulseAt=Date.now();capable=currentTab();const r=await api('accept',{capable:capable?'1':'0'});session=r.session;
  const track=stream.getVideoTracks()[0];if(track.readyState!=='live'){stop('Condivisione già interrotta dal browser.');return;}track.addEventListener('ended',()=>stop('Condivisione interrotta dal browser.'));track.addEventListener('capturehandlechange',()=>{if(!currentTab())revoke(true);});
  invite.hidden=true;notice('Condivisione autorizzata a '+session.admin_name+'. Collegamento in corso…');pollSession();
 }catch(e){if(stream){stream.getTracks().forEach(t=>t.stop());stream=null;}session=null;notice('Condivisione non avviata: '+e.message);}
}
const invite=el('aside','',document.body,'assist-invite');invite.id='assist-invite';invite.hidden=true;const inviteText=el('p','',invite);let invitation=null;
button('Accetta e scegli cosa condividere',invite,()=>{if(invitation)accept(invitation);});button('Rifiuta',invite,async()=>{const old=invitation;invitation=null;invite.hidden=true;if(old)try{await post('/controllo/assistenza/'+old.id+'/decline');}catch(_){};});
async function invitations(){if(!isAdmin&&!session){try{const r=await fetch('/controllo/assistenza/inviti',{credentials:'same-origin',cache:'no-store'});if(r.ok){const data=await r.json();invitation=data.requests[0]||null;invite.hidden=!invitation;if(invitation)inviteText.textContent=invitation.admin_name+' richiede di vedere il tuo schermo per aiutarti. Sceglierai cosa condividere nel browser. Mouse e tastiera restano esclusi finché non accetti una seconda richiesta.';}}catch(_){}}setTimeout(invitations,3000);}
window.ControlAssist={request:async(id,name)=>{if(session){dialog.showModal();return;}if(!window.RTCPeerConnection){alert('Browser non compatibile con WebRTC.');return;}try{const r=await post('/controllo/assistenza/richiedi',{target_id:id});session=r.session;cursor=0;startedAt=Date.now();pulseAt=Date.now();title.textContent='Assistenza a '+name;status.textContent='Richiesta inviata. In attesa che il dipendente accetti e scelga cosa condividere.';controlButton.disabled=true;dialog.showModal();makePeer(r.iceServers);pc.addTransceiver('video',{direction:'recvonly'});attachChannel(pc.createDataChannel('remote'));const offer=await pc.createOffer();await pc.setLocalDescription(offer);await api('offer',{sdp:offer.sdp});pollSession();}catch(e){stop(e.message);alert(e.message);}}};
window.addEventListener('pagehide',()=>stop('Pagina chiusa: assistenza interrotta.'));invitations();
})();
