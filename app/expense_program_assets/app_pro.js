const state = {
  page:'home', paymentStatus:'pending', dashboard:null,
  scope:'global', storeId:null, stores:[],
  awareSelectedDay:null, awarePinnedByUser:false,
  pendingPayments:[], fixedExpenses:[], variableBudgets:[], distanceMode:'days', distanceValue:null, distancePinnedByUser:false
};
const UI_SESSION_KEY = 'gestione_pagamenti_ui';
let savedScrollPosition = {x:0,y:0};
let restoringScroll = false;
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
const euro = new Intl.NumberFormat('it-IT',{style:'currency',currency:'EUR'});
const formatMoney = cents => euro.format((Number(cents)||0)/100);
const esc = (s='') => String(s).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

async function api(url, options={}) {
  const res = await fetch(url,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});
  let data={}; try{data=await res.json()}catch{}
  if(!res.ok) throw new Error(data.error||'Errore del programma');
  return data;
}
function toast(message,error=false){const el=document.getElementById('toast');el.textContent=message;el.classList.toggle('error',error);el.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.hidden=true,3200)}
function openModal(id){document.getElementById(id).hidden=false}
function closeModal(id){document.getElementById(id).hidden=true}
function italianDate(iso){if(!iso)return'Nessuna scadenza';const d=new Date(iso.length===10?iso+'T12:00:00':iso.replace(' ','T'));return d.toLocaleDateString('it-IT',{day:'2-digit',month:'2-digit',year:'numeric'})}
function shortDateTime(iso){if(!iso)return'';const d=new Date(iso.replace(' ','T'));return d.toLocaleString('it-IT',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})}
function monthLabel(ym){if(!ym)return'mese scorso';const[y,m]=ym.split('-').map(Number);return new Date(y,m-1,1,12).toLocaleDateString('it-IT',{month:'long',year:'numeric'})}
function monthLabelCap(ym){const x=monthLabel(ym);return x?x.charAt(0).toUpperCase()+x.slice(1):x}
function nextMonthYm(ym){if(!ym)return'';const[y,m]=ym.split('-').map(Number),d=new Date(y,m,1,12);return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`}
function forecastSourceLabel(d){const src=d.forecast_reference_source||d.previous_month_source||'';const m=monthLabelCap(d.forecast_reference_month);if(src==='actual_complete')return `Consuntivo reale · ${m}`;if(src==='baseline_exact')return `Storico caricato · ${m}`;if(src==='baseline_guardrail')return `Storico protetto · ${m}`;if(src==='baseline_partial_fallback')return `Storico affidabile · ${m}`;if(src==='partial_actual')return `Dati parziali · ${m}`;if(src==='mixed')return 'Fonti affidabili per negozio';return m?`Base · ${m}`:'Base previsione'}
function clampPct(v){return Math.max(0,Math.min(100,Number(v)||0))}
function priorityClass(p){return p>=80?'high':p>=50?'medium':'low'}
function currentStore(){return state.stores.find(s=>Number(s.id)===Number(state.storeId))||null}
function isGlobal(){return state.scope==='global'}
function scopeQuery(){return isGlobal()?'scope=global':`store_id=${encodeURIComponent(state.storeId)}`}

function animateMoney(id,target){const el=document.getElementById(id);if(el){el.dataset.cents=String(Number(target)||0);el.textContent=formatMoney(target)}}
function animateInteger(id,target){const el=document.getElementById(id);if(el){el.dataset.value=String(Number(target)||0);el.textContent=String(Math.round(Number(target)||0))}}

function saveUiState(){
  if(restoringScroll)return;
  const payload={page:state.page,paymentStatus:state.paymentStatus,scope:state.scope,storeId:state.storeId,
    paidMonth:document.getElementById('paidMonth')?.value||'',rechargeMonth:document.getElementById('rechargeMonth')?.value||'',
    distanceMode:state.distanceMode,distanceValue:state.distanceValue,distancePinnedByUser:state.distancePinnedByUser,
    historyPeriod:document.getElementById('historyPeriod')?.value||'',historyAnchor:document.getElementById('historyAnchor')?.value||'',
    commandFilter,commandShowAll,awareSelectedDay:state.awareSelectedDay,awarePinnedByUser:state.awarePinnedByUser,
    scrollX:window.scrollX,scrollY:window.scrollY};
  savedScrollPosition={x:payload.scrollX,y:payload.scrollY};
  try{sessionStorage.setItem(UI_SESSION_KEY,JSON.stringify(payload))}catch{}
}
function readSavedUiState(){try{const raw=sessionStorage.getItem(UI_SESSION_KEY);return raw?JSON.parse(raw):null}catch{return null}}
function applySavedUiState(saved){
  if(!saved)return;
  const allowedPages=new Set(['home','payments','selected','fixed','variables','recharges','budgets','history']);state.page=allowedPages.has(saved.page)?saved.page:'home';
  state.paymentStatus=saved.paymentStatus==='paid'?'paid':'pending';
  if(saved.scope==='global')state.scope='global';
  else if(state.stores.some(s=>Number(s.id)===Number(saved.storeId))){state.scope='store';state.storeId=Number(saved.storeId)}
  if(saved.historyPeriod&&Array.from(document.getElementById('historyPeriod').options).some(o=>o.value===saved.historyPeriod))document.getElementById('historyPeriod').value=saved.historyPeriod;
  if(saved.historyAnchor)document.getElementById('historyAnchor').value=saved.historyAnchor;
  if(['now','week','no_date','all'].includes(saved.commandFilter))commandFilter=saved.commandFilter;
  commandShowAll=Boolean(saved.commandShowAll);state.awarePinnedByUser=Boolean(saved.awarePinnedByUser);state.awareSelectedDay=saved.awareSelectedDay??null;
  if(saved.paidMonth)document.getElementById('paidMonth').value=saved.paidMonth;
  if(saved.rechargeMonth)document.getElementById('rechargeMonth').value=saved.rechargeMonth;
  state.distanceMode=saved.distanceMode==='months'?'months':'days';
  state.distanceValue=Number.isFinite(Number(saved.distanceValue))?Number(saved.distanceValue):null;
  state.distancePinnedByUser=Boolean(saved.distancePinnedByUser);
  savedScrollPosition={x:Number(saved.scrollX)||0,y:Number(saved.scrollY)||0};
}
function restoreExactScrollPosition(){restoringScroll=true;const{x,y}=savedScrollPosition;let attempts=0;const restore=()=>{window.scrollTo(x,y);attempts++;if(attempts<12&&(Math.abs(window.scrollX-x)>1||Math.abs(window.scrollY-y)>1))setTimeout(restore,45);else setTimeout(()=>restoringScroll=false,30)};requestAnimationFrame(restore)}

function setPage(page,loadData=true,persist=true){
  state.page=page;document.querySelectorAll('.page').forEach(x=>x.classList.toggle('active',x.id===`page-${page}`));document.querySelectorAll('.nav-btn').forEach(x=>x.classList.toggle('active',x.dataset.page===page));
  document.getElementById('pageTitle').textContent=page==='selected'?'Spese selezionate':page==='home'?'Cruscotto':page==='payments'?'Pagamenti':page==='fixed'?'Spese fisse':page==='variables'?'Spese variabili':page==='budgets'?'Budget spese':page==='history'?'Storico spese':'Ricariche';
  if(persist){window.scrollTo(0,0);saveUiState()}
  if(loadData&&page==='selected')loadSelectedExpenses().catch(e=>toast(e.message,true));if(loadData&&page==='payments')loadPayments();if(loadData&&page==='fixed')loadFixedExpenses();if(loadData&&page==='variables')loadVariableBudgets();if(loadData&&page==='recharges')loadRecharges();if(loadData&&page==='budgets')loadSpendingBudgets();if(loadData&&page==='history')loadExpenseHistory();
}
function updateContextUI(){
  const name=isGlobal()?'Tutti i negozi':(currentStore()?.name||'Seleziona negozio');
  document.getElementById('contextName').textContent=name;document.getElementById('contextIcon').textContent=isGlobal()?'∞':name.slice(0,1).toUpperCase();
  document.getElementById('scopeChip').textContent=isGlobal()?'Globale':'Negozio';document.getElementById('scopeEyebrow').textContent=isGlobal()?'VISIONE CONSOLIDATA':'CONTROLLO FINANZIARIO';
  document.getElementById('balanceSubtitle').textContent=isGlobal()?'Somma della liquidità reale di tutti i negozi.':'Disponibilità reale aggiornata con movimenti e pagamenti.';
  refreshOperationStoreFields();
}
function refreshOperationStoreFields(){
  const configured=state.stores.filter(s=>s.setup_complete);const opts=configured.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('');
  for(const id of ['rechargeStoreField','paymentStoreField','fixedStoreField','variableStoreField']){const field=document.getElementById(id);const select=field.querySelector('select');select.innerHTML=opts;field.hidden=!isGlobal();if(!isGlobal()&&state.storeId)select.value=String(state.storeId)}
}

function renderStoreChoices(){
  const box=document.getElementById('storeChoiceList');
  box.innerHTML=state.stores.map(s=>`<button class="store-choice ${!isGlobal()&&Number(state.storeId)===Number(s.id)?'active':''}" type="button" data-store-choice="${s.id}"><span class="context-icon">${esc(s.name.slice(0,1).toUpperCase())}</span><span><strong>${esc(s.name)}</strong><small>${s.setup_complete?`${formatMoney(s.balance_cents)} • ${s.pending_count} in corso`:'Configurazione richiesta'}</small></span><span class="store-delete-inline" data-delete-store="${s.id}" title="Elimina negozio">Elimina</span></button>`).join('');
  box.querySelectorAll('[data-store-choice]').forEach(btn=>btn.addEventListener('click',e=>{if(e.target.closest('[data-delete-store]'))return;selectStore(Number(btn.dataset.storeChoice))}));
  box.querySelectorAll('[data-delete-store]').forEach(btn=>btn.addEventListener('click',e=>{e.stopPropagation();deleteStore(Number(btn.dataset.deleteStore))}));
}
async function refreshStores(){const data=await api('/spese-programma/api/stores');state.stores=data.stores||[];renderStoreChoices();refreshOperationStoreFields();return state.stores}
async function changeStoreContext(scope,id=null){
  const position={x:window.scrollX,y:window.scrollY};
  state.scope=scope;if(id!==null)state.storeId=Number(id);
  closeModal('storeSwitcherModal');updateContextUI();saveUiState();restoringScroll=true;
  try{await loadCurrentContext()}finally{
    savedScrollPosition=position;restoreExactScrollPosition();
    // Save the new context with the original position before a possible host reload.
    try{const saved=readSavedUiState()||{};sessionStorage.setItem(UI_SESSION_KEY,JSON.stringify({...saved,scope:state.scope,storeId:state.storeId,page:state.page,scrollX:position.x,scrollY:position.y}))}catch{}
    const stores={1:'spinza',2:'reburger_camaldoli',3:'reburger_maggio'};
    if(window.parent!==window)window.parent.postMessage({type:'expense-program-store',store:scope==='global'?'ALL':stores[state.storeId]},location.origin);
  }
}
async function selectStore(id){await changeStoreContext('store',id)}
async function selectGlobal(){await changeStoreContext('global')}
async function loadCurrentContext(){await loadDashboard();if(state.page==='payments')await loadPayments();if(state.page==='fixed')await loadFixedExpenses();if(state.page==='variables')await loadVariableBudgets();if(state.page==='recharges')await loadRecharges();if(state.page==='budgets')await loadSpendingBudgets();if(state.page==='history')await loadExpenseHistory();if(state.page==='selected')await loadSelectedExpenses()}

function daysInMonthFromIso(iso){const[y,m]=String(iso).split('-').map(Number);return new Date(y,m,0).getDate()}
function daysInMonthForDate(d){return new Date(d.getFullYear(),d.getMonth()+1,0,12).getDate()}
function currentMonthTargetDate(todayIso,day){const[y,m]=String(todayIso).split('-').map(Number);return new Date(y,m-1,Number(day),12,0,0,0)}
function projectFixedExpenseAtDate(f,target){
  const amount=Number(f.amount_cents)||0,freq=Math.max(1,Number(f.frequency_months)||1),cycleStart=isoDateToLocalDate(f.cycle_start_date),due=isoDateToLocalDate(f.next_due_date);
  if(!cycleStart||!due||!target||target<cycleStart)return{reserved:0,remaining:amount,pct:0};
  if(target>=due)return{reserved:amount,remaining:0,pct:100};
  const monthly=amount/freq;let total=0,cursor=new Date(cycleStart);
  while(cursor<=target){total+=monthly/daysInMonthForDate(cursor);cursor.setDate(cursor.getDate()+1)}
  const reserved=Math.max(0,Math.min(amount,Math.round(total))),pct=amount?Math.max(0,Math.min(100,reserved*100/amount)):0;
  return{reserved,remaining:Math.max(0,amount-reserved),pct};
}
function projectCommittedForDay(d,day){
  const target=currentMonthTargetDate(d.today,day),items=(d.fixed_expenses||[]).map(f=>({...f,_projection:projectFixedExpenseAtDate(f,target)}));
  const fixedReserved=items.reduce((sum,f)=>sum+f._projection.reserved,0),fixedInstallments=items.reduce((sum,f)=>sum+(Number(f.amount_cents)||0),0);
  const variableCommitted=Number(d.variable_budget_commitment_cents)||0;
  const totalReserved=fixedReserved+variableCommitted,totalInstallments=fixedInstallments+variableCommitted,totalPct=totalInstallments?Math.max(0,Math.min(100,totalReserved*100/totalInstallments)):0;
  return{target,items,totalReserved,totalInstallments,totalPct,fixedReserved,variableCommitted};
}
function percentText(n){return `${Number(n||0).toLocaleString('it-IT',{minimumFractionDigits:0,maximumFractionDigits:1})}%`}
function buildAwareDayLabels(maxDays,selectedDay,todayDay){const labels=document.getElementById('awareDayLabels'),den=Math.max(1,maxDays-1);labels.innerHTML=Array.from({length:maxDays},(_,i)=>{const day=i+1,pos=((day-1)/den)*100,cls=['aware-day-label'];if(day===selectedDay)cls.push('selected');if(day===todayDay)cls.push('today');return`<span class="${cls.join(' ')}" style="left:${pos}%">${day}</span>`}).join('')}
function renderAwareForDay(day,d=state.dashboard){
  if(!d)return;
  const slider=document.getElementById('awareDaySlider'),maxDays=Number(slider.max)||daysInMonthFromIso(d.today);
  day=Math.max(1,Math.min(maxDays,Number(day)||1));state.awareSelectedDay=day;slider.value=String(day);
  const monthDays=Number(d.month_days)||maxDays,variableBudget=Number(d.variable_budget_cents)||0,gross=Math.round(variableBudget*day/monthDays),variableSpent=Number(d.current_variable_expenses_cents)||0,funds=gross-variableSpent,isToday=day===Number(d.aware_days),shownFunds=isToday?Number(d.aware_funds_cents):funds,shownGross=isToday?Number(d.aware_gross_cents):gross,den=Math.max(1,maxDays-1),pos=((day-1)/den)*100;
  const committed=projectCommittedForDay(d,day),committedPct=committed.totalPct;

  animateMoney('awareValue',shownFunds);
  animateMoney('committedMoney',committed.totalReserved);
  animateMoney('awareLinkedFunds',shownFunds);
  animateMoney('awareLinkedCommitted',committed.totalReserved);
  document.getElementById('committedProgressFill').style.width=`${committedPct}%`;
  document.getElementById('awareLinkedCommittedPct').textContent=percentText(committedPct);
  document.getElementById('committedSubtitle').textContent=((d.fixed_expenses_count||0)||(d.variable_budget_count||0))?(isToday?`${formatMoney(committed.totalReserved)} protetti tra rate e budget fornitori`:`Giorno ${day} • ${formatMoney(committed.totalReserved)} protetti`):'Nessuna spesa fissa o variabile attiva';
  document.getElementById('awareLinkedFundsSub').textContent=isToday?'Budget flessibile maturato oggi • non è il saldo reale':`Budget flessibile simulato al giorno ${day} • non è il saldo reale`;

  document.getElementById('awareProgress').style.width=`${pos}%`;
  document.getElementById('awarePreviewLine').style.left=`${pos}%`;
  document.getElementById('awareRulerDayValue').textContent=`Giorno ${day}`;
  document.getElementById('awareRulerLabel').textContent=isToday?'Oggi • previsione automatica':'Simulazione • giorno scelto';
  document.getElementById('awareModeBadge').textContent=isToday?'Previsione':'Simulazione';
  document.querySelector('.aware-card').classList.toggle('previewing',!isToday);

  const fixed=Number(d.fixed_monthly_equivalent_cents)||0,source=forecastSourceLabel(d);
  const suffix=isToday?`maturati ${formatMoney(shownGross)} − altre uscite non già protette ${formatMoney(variableSpent)}`:`simulazione ${formatMoney(shownGross)} − altre uscite attuali ${formatMoney(variableSpent)}`;
  document.getElementById('awareExplanation').textContent=`Base ${formatMoney(d.forecast_reference_cents??d.previous_month_expenses_cents)} (${source}) − ${formatMoney(fixed)} di quota protetta = ${formatMoney(variableBudget)} flessibili • ${formatMoney(d.daily_aware_cents)}/giorno • ${suffix}. È una previsione, non liquidità presente.`;

  const note=document.getElementById('allocationNote');
  if((d.fixed_expenses_count||0)||(d.variable_budget_count||0)){
    note.textContent=isToday?`${formatMoney(committed.totalReserved)} sono già protetti oggi: ${formatMoney(committed.fixedReserved)} dalle spese fisse e ${formatMoney(committed.variableCommitted)} dai budget variabili.`:`Al giorno ${day} avresti ${formatMoney(committed.totalReserved)} impegnati tra rate e budget dei fornitori.`;
  }else note.textContent='Nessuna quota mensile è ancora protetta. Aggiungi spese fisse o budget variabili.';

  renderHomeFixedPreview(d,day);
  buildAwareDayLabels(maxDays,day,Number(d.aware_days));
}
function setupAwareRuler(d){const slider=document.getElementById('awareDaySlider'),maxDays=daysInMonthFromIso(d.today);slider.max=String(maxDays);if(!state.awarePinnedByUser||state.awareSelectedDay===null)state.awareSelectedDay=Math.min(maxDays,Math.max(1,Number(d.aware_days)||1));else state.awareSelectedDay=Math.min(maxDays,Math.max(1,Number(state.awareSelectedDay)||1));renderAwareForDay(state.awareSelectedDay,d)}

function renderStoreBreakdown(d){
  const section=document.getElementById('globalBreakdown');section.hidden=!d.is_global;if(!d.is_global)return;
  const grid=document.getElementById('storeBreakdownGrid');
  grid.innerHTML=(d.stores||[]).map(s=>`<article class="store-snapshot" data-open-store="${s.id}"><div class="snapshot-head"><span class="snapshot-name">${esc(s.name)}</span><span class="snapshot-status">${s.setup_complete?'ATTIVO':'DA CONFIGURARE'}</span></div><div class="snapshot-balance">${formatMoney(s.balance_cents)}</div><div class="snapshot-meta"><div><span>Fondi consapevoli (previsione)</span><strong>${formatMoney(s.aware_funds_cents)}</strong></div><div><span>Soldi impegnati</span><strong>${formatMoney(s.committed_money_cents)}</strong></div><div><span>Spese mese</span><strong>${formatMoney(s.current_month_expenses_cents)}</strong></div><div><span>Quota / giorno</span><strong>${formatMoney(s.daily_aware_cents)}</strong></div><div><span>Spese fisse</span><strong>${s.fixed_expenses_count||0}</strong></div><div><span>Variabili</span><strong>${s.variable_budget_count||0}</strong></div><div><span>In corso</span><strong>${s.pending_count}</strong></div></div><div class="fixed-card-actions"><button class="small-btn delete" data-delete-store="${s.id}" type="button">Elimina negozio</button></div></article>`).join('')||'<div class="empty-state">Nessun negozio disponibile.</div>';
  grid.querySelectorAll('[data-open-store]').forEach(el=>el.addEventListener('click',e=>{if(e.target.closest('[data-delete-store]'))return;selectStore(Number(el.dataset.openStore))}));
  grid.querySelectorAll('[data-delete-store]').forEach(btn=>btn.addEventListener('click',e=>{e.stopPropagation();deleteStore(Number(btn.dataset.deleteStore))}));
}
function renderHomeFixedPreview(d,day=Number(d.aware_days)||1){
  const box=document.getElementById('homeFixedPreview'),projection=projectCommittedForDay(d,day),items=projection.items.slice(0,3);
  if(!items.length){box.innerHTML='<div class="home-fixed-empty"><span>◆</span><div><strong>Nessuna spesa fissa attiva</strong><small>Aggiungine una per iniziare a proteggere automaticamente una parte del denaro.</small></div></div>';return}
  box.innerHTML=items.map(f=>{const p=f._projection,pct=Math.max(0,Math.min(100,Number(p.pct)||0));return`<div class="home-fixed-row"><div class="home-fixed-top"><span>${isGlobal()&&f.store_name?`<b class="store-tag">${esc(f.store_name)}</b>`:''}<strong>${esc(f.description)}</strong><small>${esc(f.frequency_label)} • rata ${formatMoney(f.amount_cents)}</small></span><b>${percentText(pct)}</b></div><div class="fixed-progress compact"><i style="width:${pct}%"></i></div><div class="home-fixed-bottom"><span>Impegnati ${formatMoney(p.reserved)}</span><span>Mancano ${formatMoney(p.remaining)}</span></div></div>`}).join('');
}
function setMoneyIfPresent(id,value){const el=document.getElementById(id);if(el)animateMoney(id,Number(value)||0)}
function setTextIfPresent(id,value){const el=document.getElementById(id);if(el)el.textContent=value}
function renderHomeVariableMetrics(d){
  const budget=Number(d.variable_monthly_budget_total_cents)||0,ordered=Number(d.variable_orders_cents)||0,remaining=Number(d.variable_remaining_to_order_cents)||0,pending=Number(d.variable_pending_orders_cents)||0,paid=Number(d.variable_paid_orders_cents)||0,projection=Number(d.variable_projection_cents)||0,progress=Number(d.variable_progress_percent)||0,over=Number(d.variable_over_budget_cents)||0,delta=Number(d.variable_projection_delta_cents)||0;
  setMoneyIfPresent('monthlyProtectedQuota',d.fixed_monthly_equivalent_cents);setTextIfPresent('monthlyProtectedQuotaSub',`${formatMoney(d.fixed_only_monthly_equivalent_cents||0)} fisse + ${formatMoney(budget)} variabili`);
  setMoneyIfPresent('homeVariableBudget',budget);setTextIfPresent('homeVariableBudgetSub',budget?`${formatMoney(remaining)} ancora da ordinare`:'Nessun budget variabile');
  setMoneyIfPresent('homeVarBudgetKpi',budget);setMoneyIfPresent('homeVarOrderedKpi',ordered);setMoneyIfPresent('homeVarRemainingKpi',remaining);setMoneyIfPresent('homeVarPendingKpi',pending);setMoneyIfPresent('homeVarPaidKpi',paid);setMoneyIfPresent('homeVarProjectionKpi',projection);
  setTextIfPresent('homeVarOrderCount',`${d.variable_order_count||0} ${(d.variable_order_count||0)===1?'ordine':'ordini'} questo mese`);setTextIfPresent('homeVarPendingCount',`${d.variable_pending_order_count||0} ${(d.variable_pending_order_count||0)===1?'ordine':'ordini'} ancora da saldare`);
  const pct=Math.max(0,progress),fill=Math.min(100,pct),progressEl=document.getElementById('homeVarProgressFill');if(progressEl)progressEl.style.width=`${fill}%`;setTextIfPresent('homeVarProgress',`${pct.toLocaleString('it-IT',{maximumFractionDigits:1})}%`);
  let analysis='';if(!budget)analysis='Nessun budget variabile impostato: crea una previsione mensile per fornitori e materie prime.';else if(over>0)analysis=`Hai già ordinato ${formatMoney(over)} oltre il budget previsto.`;else if(projection>budget&&ordered>0)analysis=`Al ritmo attuale la proiezione di fine mese è ${formatMoney(projection)}, circa ${formatMoney(Math.max(0,delta))} sopra il budget.`;else if(ordered>0)analysis=`Hai utilizzato ${pct.toLocaleString('it-IT',{maximumFractionDigits:1})}% del budget. Restano ${formatMoney(remaining)} da ordinare per arrivare alla previsione.`;else analysis=`Il budget di ${formatMoney(budget)} pesa già sulla Quota fissa mensile anche se non hai ancora registrato ordini.`;setTextIfPresent('homeVarAnalysisText',analysis);
  setTextIfPresent('homeVarProjectionSub',ordered?`Stima lineare sul ritmo dei primi ${d.day_of_month||1} giorni`:'Nessun ordine: proiezione non disponibile');
}
function renderVariableSummary(items){
  const a=items||[],sum=k=>a.reduce((t,x)=>t+(Number(x[k])||0),0),budget=sum('monthly_budget_cents'),ordered=sum('ordered_cents'),remaining=sum('remaining_to_order_cents'),pending=sum('pending_order_cents'),paid=sum('paid_order_cents'),count=sum('order_count'),pendingCount=sum('pending_order_count'),avg=count?Math.round(ordered/count):0;
  setMoneyIfPresent('variableSummaryBudget',budget);setMoneyIfPresent('variableSummaryOrdered',ordered);setMoneyIfPresent('variableSummaryRemaining',remaining);setMoneyIfPresent('variableSummaryPending',pending);setMoneyIfPresent('variableSummaryPaid',paid);setMoneyIfPresent('variableSummaryAverage',avg);setTextIfPresent('variableSummaryOrderCount',`${count} ${count===1?'ordine registrato':'ordini registrati'}`);setTextIfPresent('variableSummaryPendingCount',`${pendingCount} ${pendingCount===1?'ordine aperto':'ordini aperti'}`);
}
function renderForecastGuardrail(d){
  const quality=document.getElementById('forecastQualityBadge'),title=document.getElementById('forecastGuardrailTitle'),text=document.getElementById('forecastGuardrailText');
  const ref=Number(d.forecast_reference_cents??d.previous_month_expenses_cents)||0,source=forecastSourceLabel(d),prev=monthLabelCap(d.previous_month),refMonth=monthLabelCap(d.forecast_reference_month),current=monthLabelCap(d.current_month),next=monthLabelCap(nextMonthYm(d.current_month));
  const prevDays=Number(d.previous_month_days)||0,tracked=Number(d.previous_month_tracking_days)||0,currentTracked=Number(d.current_month_tracking_days)||0,monthDays=Number(d.month_days)||0;
  let q='Affidabile',cls='good',headline='',body='';
  const src=d.forecast_reference_source||d.previous_month_source;
  if(src==='actual_complete'){
    q='Dati reali';cls='good';headline=`Previsione basata sul consuntivo reale di ${refMonth}`;body=`${prev} è stato monitorato dall’inizio alla fine e contiene uscite reali. La base mensile è quindi ${formatMoney(ref)} e si aggiornerà automaticamente a ogni nuovo mese completo.`;
  }else if(src==='baseline_exact'){
    q='Storico caricato';cls='safe';headline=`Base storica pronta: ${refMonth}`;body=`Hai caricato ${formatMoney(ref)} come storico di ${refMonth}. Questa cifra resta la base affidabile finché il programma non avrà un mese completo di uscite reali da sostituirle.`;
  }else if(src==='baseline_partial_fallback'){
    q='Storico protetto';cls='safe';headline=`${current} non riparte da zero`;
    body=`${prev} non è un mese completo (${tracked}/${prevDays} giorni monitorati). Per evitare una previsione falsa a zero, il programma mantiene ${formatMoney(ref)} usando lo storico di ${refMonth}.`;
  }else if(src==='baseline_guardrail'){
    q='Guardrail attivo';cls='warn';headline='Nessuna uscita reale registrata: lo storico resta protetto';body=`Il mese precedente risulta monitorato, ma non contiene uscite realmente pagate. Invece di trasformare la previsione in 0 €, il programma conserva ${formatMoney(ref)} (${refMonth}) finché non arrivano dati reali affidabili.`;
  }else if(src==='mixed'){
    q='Controllo per negozio';cls='safe';headline='Ogni negozio usa la propria base più affidabile';body=`La visione globale somma basi reali o storiche selezionate separatamente per ciascun punto vendita. Totale usato per la previsione: ${formatMoney(ref)}.`;
  }else{
    q='Dati parziali';cls='warn';headline='Previsione da verificare';body=d.forecast_reference_reason||`Il programma sta usando ${formatMoney(ref)} come migliore dato disponibile.`;
  }
  quality.textContent=q;quality.className=`forecast-quality ${cls}`;title.textContent=headline;text.textContent=body;
  setTextIfPresent('forecastReferenceFact',`${formatMoney(ref)} • ${source}`);
  setTextIfPresent('forecastTrackingFact',d.current_month_tracking_complete?`Attivo dall’inizio di ${current}`:`${currentTracked}/${monthDays} giorni di ${current}`);
  if(d.next_month_policy==='fallback_history') setTextIfPresent('forecastNextMonthFact',`${next}: mantiene ${formatMoney(d.next_month_reference_cents||ref)}`);
  else setTextIfPresent('forecastNextMonthFact',`${next}: userà il consuntivo di ${current} a chiusura`);
  setTextIfPresent('prevExpensesSub',source);
  setTextIfPresent('allocationPreviousSub',source);
}

function renderPlanAndCashHealth(d){
  const total=Number(d.planned_month_total_cents)||0,fixed=Number(d.fixed_only_monthly_equivalent_cents)||0,variable=Number(d.variable_monthly_budget_total_cents)||0,flexible=Number(d.variable_budget_cents)||0;
  const pct=x=>total?Math.max(0,Math.min(100,x*100/total)):0;
  setMoneyIfPresent('planTotal',total);setMoneyIfPresent('planFixedValue',fixed);setMoneyIfPresent('planVariableValue',variable);setMoneyIfPresent('planFlexibleValue',flexible);
  const pf=pct(fixed),pv=pct(variable),pflex=pct(flexible);setTextIfPresent('planFixedPct',percentText(pf));setTextIfPresent('planVariablePct',percentText(pv));setTextIfPresent('planFlexiblePct',percentText(pflex));
  const a=document.getElementById('planFixedBar'),b=document.getElementById('planVariableBar'),c=document.getElementById('planFlexibleBar');if(a)a.style.width=`${pf}%`;if(b)b.style.width=`${pv}%`;if(c)c.style.width=`${pflex}%`;
  const gap=Number(d.forecast_gap_cents)||0;setTextIfPresent('planChartNote',gap>0?`Attenzione: fisse + budget variabili superano la base storica di ${formatMoney(gap)}. Il piano totale è stato alzato automaticamente per non sottostimare i costi.`:`La previsione di ${formatMoney(total)} è divisa in costi fissi, budget fornitori e parte flessibile. Nessuna voce viene contata due volte.`);

  const balance=Number(d.balance_cents)||0,oblig=Number(d.known_obligations_cents)||0,after=Number(d.cash_after_commitments_cents)||0,coverage=oblig>0?Math.max(0,Math.min(100,Number(d.commitment_coverage_percent)||0)):100;
  setMoneyIfPresent('knownObligations',oblig);setMoneyIfPresent('cashAfterCommitments',after);setMoneyIfPresent('healthBalance',balance);setMoneyIfPresent('healthObligations',oblig);setMoneyIfPresent('healthAfter',after);setTextIfPresent('coveragePct',oblig?percentText(coverage):'100%');
  const ring=document.getElementById('coverageRing');if(ring)ring.style.setProperty('--coverage',`${coverage*3.6}deg`);
  const margin=document.getElementById('cashAfterCommitments');if(margin)margin.classList.toggle('negative',after<0);const healthAfter=document.getElementById('healthAfter');if(healthAfter)healthAfter.classList.toggle('negative',after<0);
  setTextIfPresent('cashAfterCommitmentsSub',after>=0?'Liquidità residua dopo gli obblighi conosciuti':`Mancano ${formatMoney(Math.abs(after))} per coprire gli impegni conosciuti`);
  setMoneyIfPresent('healthOverdue',d.overdue_amount_cents);setMoneyIfPresent('healthDue7',d.due_7_amount_cents);setMoneyIfPresent('healthDue30',d.due_30_amount_cents);setTextIfPresent('healthOverdueCount',`${d.overdue_count||0} ${(d.overdue_count||0)===1?'voce':'voci'}`);setTextIfPresent('healthDue7Count',`${d.due_7_count||0} ${(d.due_7_count||0)===1?'voce':'voci'}`);setTextIfPresent('healthDue30Count',`${d.due_30_count||0} ${(d.due_30_count||0)===1?'voce':'voci'}`);
  let note;if(balance===0&&oblig>0)note=`Il saldo reale è 0 €. I fondi consapevoli sono solo una previsione: per misurare la copertura reale devi caricare la liquidità effettivamente disponibile.`;else if(after<0)note=`Gli impegni conosciuti superano il saldo reale di ${formatMoney(Math.abs(after))}. Questa è la differenza da coprire.`;else if(oblig>0)note=`Il saldo reale copre ${percentText(Number(d.commitment_coverage_percent)||0)} degli impegni conosciuti; dopo averli coperti restano ${formatMoney(after)}.`;else note='Non risultano impegni aperti: tutta la liquidità caricata è libera da obblighi conosciuti.';setTextIfPresent('cashHealthNote',note);
}

function renderInsights(d){
  const current=monthLabelCap(d.current_month),ref=Number(d.forecast_reference_cents)||0;
  if(d.current_month_tracking_complete){setTextIfPresent('insightDataTitle',`Monitoraggio completo da inizio ${current}`);setTextIfPresent('insightDataText',`Il mese corrente è tracciato dall’inizio. A chiusura potrà diventare la nuova base reale per il mese successivo.`)}else{setTextIfPresent('insightDataTitle',`Mese avviato in ritardo: ${d.current_month_tracking_days||0}/${d.month_days||0} giorni`);setTextIfPresent('insightDataText',`Non uso questo mese parziale per azzerare la previsione. Resta attiva la base affidabile di ${formatMoney(ref)}.`)}
  if(d.current_month_tracking_complete){const time=Number(d.month_progress_percent)||0,spent=Number(d.actual_vs_plan_percent)||0,delta=spent-time;setTextIfPresent('insightPaceTitle',`${percentText(spent)} del piano già pagato`);setTextIfPresent('insightPaceText',Math.abs(delta)<5?`La spesa reale è vicina all’avanzamento del mese (${percentText(time)} del tempo trascorso).`:delta>0?`La spesa reale corre circa ${percentText(Math.abs(delta))} più avanti rispetto al tempo trascorso nel mese.`:`La spesa reale è circa ${percentText(Math.abs(delta))} più indietro rispetto al tempo trascorso nel mese.`)}else{setTextIfPresent('insightPaceTitle','Ritmo non confrontabile');setTextIfPresent('insightPaceText','Hai iniziato a tracciare il mese in corso troppo tardi: evito confronti fuorvianti tra spesa e giorni trascorsi.')}
  const budget=Number(d.variable_monthly_budget_total_cents)||0,ordered=Number(d.variable_orders_cents)||0,pending=Number(d.variable_pending_orders_cents)||0,remaining=Number(d.variable_remaining_to_order_cents)||0;setTextIfPresent('insightSupplierTitle',budget?`${formatMoney(ordered)} ordinati su ${formatMoney(budget)}`:'Nessun budget variabile');setTextIfPresent('insightSupplierText',budget?`${formatMoney(pending)} sono ancora da pagare e ${formatMoney(remaining)} restano da ordinare per arrivare alla previsione mensile.`:'Aggiungi Metro, grossisti o altri fornitori per confrontare previsione, ordini e pagamenti reali.');
}

async function loadDashboard(){
  const d=await api(`/spese-programma/api/dashboard?${scopeQuery()}`);state.dashboard=d;
  animateMoney('balanceValue',d.balance_cents);animateMoney('prevExpenses',d.forecast_reference_cents??d.previous_month_expenses_cents);animateMoney('currentExpenses',d.current_month_expenses_cents);animateMoney('currentRecharges',d.current_month_recharges_cents);animateMoney('pendingCredit',d.pending_amount_cents);animateMoney('committedMoney',d.committed_money_cents);animateMoney('allocationPrevious',d.forecast_reference_cents??d.previous_month_expenses_cents);animateMoney('allocationFixed',d.fixed_monthly_equivalent_cents);animateMoney('allocationVariable',d.variable_budget_cents);animateMoney('allocationDaily',d.daily_aware_cents);animateInteger('pendingCount',d.pending_count);
  document.getElementById('allocationDays').textContent=`per giorno • ${d.month_days} giorni`;
  document.getElementById('allocationNote').textContent=((d.fixed_expenses_count||0)||(d.variable_budget_count||0))?`${formatMoney(d.fixed_monthly_equivalent_cents)} di Quota fissa mensile: ${formatMoney(d.fixed_only_monthly_equivalent_cents||0)} dalle spese fisse + ${formatMoney(d.variable_monthly_budget_total_cents||0)} dai budget variabili. I budget variabili entrano subito, prima ancora di registrare gli ordini.`:'Nessuna quota mensile è ancora protetta. Aggiungi spese fisse o budget variabili.';
  document.getElementById('todayLabel').textContent=new Date(d.today+'T12:00:00').toLocaleDateString('it-IT',{weekday:'long',day:'numeric',month:'long',year:'numeric'});
  renderForecastGuardrail(d);renderHomeVariableMetrics(d);renderPlanAndCashHealth(d);renderInsights(d);setupAwareRuler(d);renderNextDue(d.next_due);renderStoreBreakdown(d);updateContextUI();await Promise.all([loadExecutiveOverview(),loadSelectedExpenses()]);
  if(!d.is_global&&!d.setup_complete)prepareSetup(d);
}

function prepareSetup(d){const form=document.getElementById('setupForm');form.reset();form.store_id.value=String(d.store_id);document.getElementById('setupStoreName').textContent=d.store_name;document.getElementById('setupPreviousMonth').textContent=monthLabel(d.previous_month);updateSetupDailyAware();openModal('setupModal')}
function parseEuroInput(value){let t=String(value||'').trim().replace(/€/g,'').replace(/\s/g,'');if(!t)return 0;if(t.includes(',')&&t.includes('.')){if(t.lastIndexOf(',')>t.lastIndexOf('.'))t=t.replace(/\./g,'').replace(',','.');else t=t.replace(/,/g,'')}else if(t.includes(','))t=t.replace(/\./g,'').replace(',','.');else if(/^\d{1,3}(\.\d{3})+$/.test(t))t=t.replace(/\./g,'');const n=Number(t);return Number.isFinite(n)?Math.round(n*100):0}
function updateSetupDailyAware(){const cents=parseEuroInput(document.querySelector('#setupForm [name=previous_month_total]').value),days=Number(state.dashboard?.month_days)||daysInMonthFromIso(state.dashboard?.today||new Date().toISOString().slice(0,10));document.getElementById('setupDailyAware').textContent=`${formatMoney(Math.round(cents/days))} / giorno • ${days} giorni`}

function storeTag(p){return isGlobal()&&p.store_name?`<span class="store-tag">${esc(p.store_name)}</span>`:''}
function renderNextDue(items){const box=document.getElementById('nextDueList');if(!items?.length){box.innerHTML='<div class="empty-state">Nessun pagamento in corso.</div>';return}box.innerHTML=items.map(p=>`<div class="list-row"><div><div class="row-title">${esc(p.description)}</div><div class="row-meta">${storeTag(p)}<span>${italianDate(p.due_date)}</span><span class="priority ${priorityClass(p.priority)}">Priorità ${p.priority}</span>${p.category?`<span>${esc(p.category)}</span>`:''}</div></div><div class="row-amount">${formatMoney(p.amount_cents)}</div><div class="row-actions">${selectionButton(p)}<button class="small-btn pay" onclick="openPay(${p.id},'${esc(p.description).replace(/'/g,'&#39;')}',${Number(p.amount_cents)||0})">Pagato</button></div></div>`).join('')}

function isoDateToLocalDate(iso){if(!iso)return null;const parts=String(iso).slice(0,10).split('-').map(Number);if(parts.length!==3||parts.some(x=>!Number.isFinite(x)))return null;return new Date(parts[0],parts[1]-1,parts[2],12,0,0,0)}
function paymentTodayDate(){const iso=state.dashboard?.today;return isoDateToLocalDate(iso)||(()=>{const d=new Date();d.setHours(12,0,0,0);return d})()}
function daysFromToday(dueDate){const due=isoDateToLocalDate(dueDate);if(!due)return null;return Math.round((due-paymentTodayDate())/86400000)}
function addMonthsSafe(dateObj,months){const d=new Date(dateObj.getFullYear(),dateObj.getMonth(),1,12),targetMonth=d.getMonth()+Number(months||0),targetYear=d.getFullYear()+Math.floor(targetMonth/12),normalized=((targetMonth%12)+12)%12,lastDay=new Date(targetYear,normalized+1,0,12).getDate();return new Date(targetYear,normalized,Math.min(dateObj.getDate(),lastDay),12)}
function monthsNeededForDue(dueDate){const due=isoDateToLocalDate(dueDate);if(!due)return null;const today=paymentTodayDate();if(due<=today)return 0;let months=Math.max(0,(due.getFullYear()-today.getFullYear())*12+due.getMonth()-today.getMonth());if(addMonthsSafe(today,months)<due)months++;return months}
function getDistanceMax(mode,payments=state.pendingPayments){if(mode==='months'){const v=payments.map(p=>monthsNeededForDue(p.due_date)).filter(v=>v!==null&&v>=0);return Math.max(12,...v,0)}const v=payments.map(p=>daysFromToday(p.due_date)).filter(v=>v!==null&&v>=0);return Math.max(30,...v,0)}
function makeDistanceTicks(max){const labels=document.getElementById('distanceLabels'),count=max<=31?Math.min(max+1,7):7,values=[];for(let i=0;i<count;i++)values.push(Math.round(max*i/Math.max(1,count-1)));labels.innerHTML=[...new Set(values)].map(v=>`<span class="distance-label" style="left:${max?(v/max)*100:0}%">${v===max?'Tutte':v}</span>`).join('')}
function distanceHumanLabel(value,max,mode){if(value>=max)return'Tutte le scadenze';if(value===0)return'Scaduti e oggi';return mode==='months'?`Entro ${value} ${value===1?'mese':'mesi'}`:`Entro ${value} ${value===1?'giorno':'giorni'}`}
function proximityInfo(p){const days=daysFromToday(p.due_date);if(days===null)return{text:'Senza scadenza',cls:'unknown'};if(days<0)return{text:`Scaduto da ${Math.abs(days)} ${Math.abs(days)===1?'giorno':'giorni'}`,cls:'overdue'};if(days===0)return{text:'Scade oggi',cls:'urgent'};if(days===1)return{text:'Domani',cls:'urgent'};if(days<=7)return{text:`Tra ${days} giorni`,cls:'near'};if(days<=30)return{text:`Tra ${days} giorni`,cls:'soon'};const m=monthsNeededForDue(p.due_date);return{text:m>=2?`Tra circa ${m} mesi`:`Tra ${days} giorni`,cls:'far'}}
function paymentMatchesDistance(p,selected,max,mode){if(selected>=max)return true;if(!p.due_date)return false;const due=isoDateToLocalDate(p.due_date),today=paymentTodayDate();if(!due)return false;if(due<=today)return true;if(mode==='months')return due<=addMonthsSafe(today,selected);const days=daysFromToday(p.due_date);return days!==null&&days<=selected}
function setupDistanceFilter(payments){state.pendingPayments=payments||[];const max=getDistanceMax(state.distanceMode,state.pendingPayments),slider=document.getElementById('distanceSlider');slider.max=String(max);if(!state.distancePinnedByUser||state.distanceValue===null)state.distanceValue=max;state.distanceValue=Math.max(0,Math.min(max,Number(state.distanceValue)||0));slider.value=String(state.distanceValue);document.querySelectorAll('.distance-mode-btn').forEach(btn=>btn.classList.toggle('active',btn.dataset.distanceMode===state.distanceMode));makeDistanceTicks(max);renderPendingPaymentsFiltered()}
function renderPendingPaymentsFiltered(){
  const box=document.getElementById('paymentsList'),slider=document.getElementById('distanceSlider'),max=Number(slider.max)||getDistanceMax(state.distanceMode),value=Math.max(0,Math.min(max,Number(state.distanceValue)||0)),pos=max?(value/max)*100:100;document.getElementById('distanceProgress').style.width=`${pos}%`;document.getElementById('distancePreviewLine').style.left=`${pos}%`;document.getElementById('distanceValueLabel').textContent=distanceHumanLabel(value,max,state.distanceMode);const visible=state.pendingPayments.filter(p=>paymentMatchesDistance(p,value,max,state.distanceMode)),total=state.pendingPayments.length,undated=state.pendingPayments.filter(p=>!p.due_date).length,unit=state.distanceMode==='months'?'mesi':'giorni';document.getElementById('distanceSummary').textContent=value>=max?`${total} ${total===1?'pagamento':'pagamenti'} in corso${undated?` • ${undated} senza scadenza`:''}`:`Mostrati ${visible.length} di ${total} • entro ${value} ${unit}${undated?' • senza scadenza visibili su “Tutte”':''}`;
  if(!total){box.innerHTML='<div class="empty-state">Non ci sono pagamenti ancora da svolgere.</div>';return}if(!visible.length){box.innerHTML='<div class="empty-state">Nessun pagamento entro la distanza selezionata. Sposta la barra verso destra.</div>';return}
  box.innerHTML=visible.map(p=>{const prox=proximityInfo(p);return`<div class="list-row"><div><div class="row-title">${esc(p.description)}</div><div class="row-meta">${storeTag(p)}<span>Scadenza: ${italianDate(p.due_date)}</span><span class="due-distance ${prox.cls}">${prox.text}</span><span class="priority ${priorityClass(p.priority)}">Priorità ${p.priority}</span>${p.category?`<span>${esc(p.category)}</span>`:''}${p.notes?`<span>• ${esc(p.notes)}</span>`:''}</div></div><div class="row-amount">${formatMoney(p.amount_cents)}</div><div class="row-actions">${selectionButton(p)}<button class="small-btn pay" onclick="openPay(${p.id},'${esc(p.description).replace(/'/g,'&#39;')}',${Number(p.amount_cents)||0})">Segna pagato</button><button class="small-btn delete" onclick="deletePayment(${p.id})">Elimina</button></div></div>`}).join('')
}
async function loadPayments(){
  const status=state.paymentStatus,month=document.getElementById('paidMonth').value,url=`/spese-programma/api/payments?status=${status}&${scopeQuery()}${status==='paid'&&month?`&month=${encodeURIComponent(month)}`:''}`,data=await api(url),box=document.getElementById('paymentsList');document.getElementById('pendingDistanceFilter').hidden=status!=='pending';
  if(status==='pending'){setupDistanceFilter(data.payments);return}if(!data.payments.length){box.innerHTML='<div class="empty-state">Nessun pagamento pagato nel periodo selezionato.</div>';return}
  box.innerHTML=data.payments.map(p=>{const est=Number(p.estimated_amount_cents)||0,real=Number(p.amount_cents)||0,diff=est&&est!==real?`<span>Stimato ${formatMoney(est)} → reale ${formatMoney(real)}</span>`:'';return`<div class="list-row"><div><div class="row-title">${esc(p.description)}</div><div class="row-meta">${storeTag(p)}<span>Pagato: ${shortDateTime(p.paid_at)}</span><span class="priority ${priorityClass(p.priority)}">Priorità ${p.priority}</span>${p.fixed_expense_id?'<span class="fixed-badge">Spesa fissa</span>':''}${diff}${p.category?`<span>${esc(p.category)}</span>`:''}${p.notes?`<span>• ${esc(p.notes)}</span>`:''}</div></div><div class="row-amount">${formatMoney(real)}</div><div class="row-actions"><span class="priority low">Svolto</span><button class="small-btn delete" onclick="deletePayment(${p.id},true)">Elimina</button></div></div>`}).join('')
}
function fixedDueText(f){const d=Number(f.days_to_due);if(d<0)return`Scaduta da ${Math.abs(d)} ${Math.abs(d)===1?'giorno':'giorni'}`;if(d===0)return'Scade oggi';if(d===1)return'Scade domani';return`Scade tra ${d} giorni`}
function fixedDueClass(f){const d=Number(f.days_to_due);return d<=0?'overdue':d<=7?'urgent':d<=30?'near':'far'}
function renderFixedExpenses(items){
  state.fixedExpenses=items||[];const box=document.getElementById('fixedExpensesList');
  const d=state.dashboard||{};animateMoney('fixedCommittedTotal',d.committed_money_cents);animateMoney('fixedMonthlyTotal',d.fixed_monthly_equivalent_cents);animateMoney('fixedDailyTotal',d.fixed_daily_reserve_cents);animateInteger('fixedCountTotal',d.fixed_expenses_count);
  if(!state.fixedExpenses.length){box.innerHTML='<article class="card fixed-empty"><span class="fixed-empty-icon">◇</span><h3>Nessuna spesa fissa</h3><p>Inserisci affitti, assicurazioni, abbonamenti, tasse ricorrenti o altre rate. Il programma inizierà a mettere da parte la quota corretta ogni giorno.</p><button class="primary-btn" type="button" onclick="document.getElementById(\'openFixedExpense\').click()">+ Aggiungi la prima</button></article>';return}
  box.innerHTML=state.fixedExpenses.map(f=>{const pct=Math.max(0,Math.min(100,Number(f.progress_percent)||0)),dueClass=fixedDueClass(f);return`<article class="card fixed-expense-card" data-fixed-id="${f.id}"><div class="fixed-card-head"><div>${isGlobal()&&f.store_name?`<span class="store-tag">${esc(f.store_name)}</span>`:''}<p class="eyebrow">${esc(f.frequency_label).toUpperCase()}</p><h3>${esc(f.description)}</h3></div><span class="due-distance ${dueClass}">${fixedDueText(f)}</span></div><div class="fixed-ratio"><div><span>Soldi impegnati</span><strong>${formatMoney(f.reserved_cents)}</strong></div><div class="fixed-ratio-right"><span>Rata completa</span><strong>${formatMoney(f.amount_cents)}</strong></div></div><div class="fixed-progress"><i style="width:${pct}%"></i><b style="left:${pct}%"></b></div><div class="fixed-progress-copy"><strong>${pct.toLocaleString('it-IT',{maximumFractionDigits:1})}% accantonato</strong><span>Mancano ${formatMoney(f.remaining_cents)}</span></div><div class="fixed-metrics"><div><span>Quota mensile</span><strong>${formatMoney(f.monthly_equivalent_cents)}</strong></div><div><span>Quota / giorno</span><strong>${formatMoney(f.daily_reserve_cents)}</strong></div><div><span>Prossima rata</span><strong>${italianDate(f.next_due_date)}</strong></div></div>${f.category||f.notes?`<div class="fixed-notes">${f.category?`<span>${esc(f.category)}</span>`:''}${f.notes?`<small>${esc(f.notes)}</small>`:''}</div>`:''}<div class="fixed-card-actions">${selectionButton({...f,kind:'fixed',due_date:f.next_due_date})}<button class="small-btn pay" type="button" onclick="openFixedPay(${f.id})">Paga rata</button><button class="small-btn delete" type="button" onclick="deleteFixedExpense(${f.id})">Rimuovi</button></div></article>`}).join('')
}
async function loadFixedExpenses(){const data=await api(`/spese-programma/api/fixed-expenses?${scopeQuery()}`);renderFixedExpenses(data.fixed_expenses)}
function updateFixedModalPreview(){const form=document.getElementById('fixedExpenseForm'),amount=parseEuroInput(form.amount.value),months=Number(form.frequency_months.value)||1,days=Number(state.dashboard?.month_days)||30,monthly=Math.round(amount/months),daily=Math.round(monthly/days),box=document.getElementById('fixedModalPreview');box.innerHTML=amount?`<span>Il programma proteggerà circa</span><strong>${formatMoney(monthly)} / mese • ${formatMoney(daily)} / giorno</strong>`:'<span>Quota stimata</span><strong>Inserisci importo e frequenza</strong>'}
window.openFixedPay=function(id){const f=state.fixedExpenses.find(x=>Number(x.id)===Number(id));if(!f)return;document.querySelector('#fixedPayForm [name=fixed_expense_id]').value=String(id);document.querySelector('#fixedPayForm [name=paid_date]').value=state.dashboard?.today||new Date().toISOString().slice(0,10);document.querySelector('#fixedPayForm [name=actual_amount]').value=(Number(f.amount_cents)/100).toFixed(2).replace('.',',');document.getElementById('fixedPaySummary').innerHTML=`<strong>${esc(f.description)}</strong><span>Rata ${formatMoney(f.amount_cents)} • impegnati ${formatMoney(f.reserved_cents)}</span><small>Confermando, la rata viene sottratta dal saldo reale. Il denaro impegnato di questo ciclo viene liberato perché è stato usato per pagare, poi parte automaticamente l'accantonamento per la rata successiva.</small>`;openModal('fixedPayModal')}
window.deleteFixedExpense=async function(id){if(!confirm('Rimuovere questa spesa fissa? I pagamenti già eseguiti resteranno nello storico.'))return;try{await api(`/spese-programma/api/fixed-expenses/${id}`,{method:'DELETE'});toast('Spesa fissa rimossa');await Promise.all([refreshStores(),loadDashboard(),loadFixedExpenses()])}catch(e){toast(e.message,true)}}

async function loadVariableBudgets(){
  const data=await api(`/spese-programma/api/variable-budgets?${scopeQuery()}`);state.variableBudgets=data.variable_budgets||[];renderVariableSummary(state.variableBudgets);const box=document.getElementById('variableBudgetsList');
  if(!state.variableBudgets.length){box.innerHTML='<article class="card fixed-empty"><span class="fixed-empty-icon">◇</span><h3>Nessuna spesa variabile</h3><p>Crea un budget mensile per Metro, grossisti o altri fornitori. Appena lo crei, la cifra entra subito nella Quota fissa mensile anche senza ordini.</p><button class="primary-btn" type="button" onclick="document.getElementById(\'openVariableBudget\').click()">+ Aggiungi la prima</button></article>';return}
  box.innerHTML=state.variableBudgets.map(v=>{const pct=Math.max(0,Number(v.progress_percent)||0),displayPct=Math.min(100,pct),over=Number(v.over_budget_cents)>0;return`<article class="card fixed-expense-card variable-card"><div class="fixed-card-head"><div>${isGlobal()&&v.store_name?`<span class="store-tag">${esc(v.store_name)}</span>`:''}<p class="eyebrow">BUDGET MENSILE PREVISTO</p><h3>${esc(v.description)}</h3></div><span class="due-distance ${over?'overdue':'far'}">${over?'Budget superato':`${Math.round(pct)}% ordinato`}</span></div><div class="fixed-ratio"><div><span>Ordini questo mese</span><strong>${formatMoney(v.ordered_cents)}</strong></div><div class="fixed-ratio-right"><span>Budget previsto</span><strong>${formatMoney(v.monthly_budget_cents)}</strong></div></div><div class="fixed-progress"><i class="${over?'over':''}" style="width:${displayPct}%"></i></div><div class="variable-status"><span>${v.order_count||0} ${(v.order_count||0)===1?'ordine':'ordini'} • ${v.pending_order_count||0} da pagare</span><strong>${over?`Superato di ${formatMoney(v.over_budget_cents)}`:`Da ordinare ${formatMoney(v.remaining_to_order_cents)}`}</strong></div><div class="fixed-metrics variable-order-metrics"><div><span>Da pagare</span><strong>${formatMoney(v.pending_order_cents)}</strong></div><div><span>Già pagato</span><strong>${formatMoney(v.paid_order_cents)}</strong></div><div><span>Impegnati ora</span><strong>${formatMoney(v.committed_cents)}</strong></div></div>${v.category||v.notes?`<div class="fixed-notes">${v.category?`<span>${esc(v.category)}</span>`:''}${v.notes?`<small>${esc(v.notes)}</small>`:''}</div>`:''}<div class="fixed-card-actions"><button class="small-btn pay" type="button" onclick="openVariableOrder(${v.id})">+ Registra ordine</button><button class="small-btn delete" type="button" onclick="deleteVariableBudget(${v.id})">Rimuovi</button></div></article>`}).join('')
}

function syncVariableOrderPaymentFields(){
  const form=document.getElementById('variablePurchaseForm'),status=form.payment_status.value,field=document.getElementById('variableDueDateField');
  const pending=status==='pending';field.hidden=!pending;form.due_date.required=pending;if(!pending)form.due_date.value='';
}
window.openVariableOrder=async function(id){
  const v=state.variableBudgets.find(x=>Number(x.id)===Number(id));if(!v)return;
  const form=document.getElementById('variablePurchaseForm');form.budget_id.value=String(id);form.amount.value='';form.purchase_date.value=state.dashboard?.today||new Date().toISOString().slice(0,10);form.payment_status.value='pending';form.due_date.value='';form.note.value='';syncVariableOrderPaymentFields();
  document.getElementById('variablePurchaseSummary').innerHTML=`<strong>${esc(v.description)}</strong><span>Budget previsto ${formatMoney(v.monthly_budget_cents)} • ordinato ${formatMoney(v.ordered_cents)} • ancora da ordinare ${formatMoney(v.remaining_to_order_cents)}</span><small>Il budget è già incluso nella Quota fissa mensile. Se scegli “Da pagare”, decidi la scadenza e l’ordine passa nei Pagamenti senza toccare il saldo; il saldo scenderà solo quando lo segnerai come pagato.</small>`;
  const data=await api(`/spese-programma/api/variable-orders?budget_id=${id}&month=${encodeURIComponent(v.month)}`),orders=data.orders||data.purchases||[];
  document.getElementById('variablePurchaseHistory').innerHTML=orders.length?`<p class="eyebrow">ORDINI DEL MESE</p>${orders.map(x=>{const pending=x.status==='pending',statusText=pending?`Da pagare${x.due_date?` • scad. ${italianDate(x.due_date)}`:''}`:'Pagato';return`<div class="variable-purchase-row"><span><strong>${formatMoney(x.amount_cents)}</strong><small>${italianDate(x.purchase_date)} • ${statusText}${x.note?` • ${esc(x.note)}`:''}</small></span>${pending&&x.payment_id?selectionButton({id:x.payment_id}):''}<button class="small-btn delete" type="button" onclick="deleteVariableOrder(${x.id})">Elimina</button></div>`}).join('')}`:'';
  openModal('variablePurchaseModal')
}
window.openVariablePurchase=window.openVariableOrder;
window.deleteVariableOrder=async function(id){if(!confirm('Eliminare questo ordine? Se era già pagato il saldo verrà ripristinato; se era nei Pagamenti verrà eliminato anche da lì.'))return;try{await api(`/spese-programma/api/variable-orders/${id}`,{method:'DELETE'});toast('Ordine eliminato');closeModal('variablePurchaseModal');await Promise.all([refreshStores(),loadDashboard(),loadVariableBudgets()]);if(state.page==='payments')await loadPayments()}catch(e){toast(e.message,true)}}
window.deleteVariablePurchase=window.deleteVariableOrder;
window.deleteVariableBudget=async function(id){if(!confirm('Rimuovere questa spesa variabile? Gli ordini già registrati restano nello storico e nel saldo.'))return;try{await api(`/spese-programma/api/variable-budgets/${id}`,{method:'DELETE'});toast('Spesa variabile rimossa');await Promise.all([refreshStores(),loadDashboard(),loadVariableBudgets()])}catch(e){toast(e.message,true)}}

async function loadRecharges(){const month=document.getElementById('rechargeMonth').value,data=await api(`/spese-programma/api/recharges?${scopeQuery()}${month?`&month=${encodeURIComponent(month)}`:''}`),box=document.getElementById('rechargesList');if(!data.recharges.length){box.innerHTML='<div class="empty-state">Nessuna ricarica registrata.</div>';return}box.innerHTML=data.recharges.map(r=>`<div class="list-row"><div><div class="row-title">${esc(r.note||'Ricarica saldo')}</div><div class="row-meta">${isGlobal()?`<span class="store-tag">${esc(r.store_name)}</span>`:''}<span>${shortDateTime(r.created_at)}</span></div></div><div class="row-amount">+ ${formatMoney(r.amount_cents)}</div><div></div></div>`).join('')}

function openOperationModal(type){const configured=state.stores.filter(s=>s.setup_complete);if(!configured.length)return toast('Configura prima almeno un negozio',true);const fieldId=type==='recharge'?'rechargeStoreField':type==='fixed'?'fixedStoreField':type==='variable'?'variableStoreField':'paymentStoreField',modalId=type==='recharge'?'rechargeModal':type==='fixed'?'fixedExpenseModal':type==='variable'?'variableBudgetModal':'paymentModal',field=document.getElementById(fieldId),select=field.querySelector('select');if(isGlobal()){field.hidden=false;select.value=String(configured[0].id)}else{const store=currentStore();if(!store?.setup_complete)return toast('Completa prima la configurazione del negozio',true);field.hidden=true;select.value=String(state.storeId)}openModal(modalId);if(type==='fixed')updateFixedModalPreview()}
window.openPay=function(id,description,amountCents=0){document.querySelector('#payForm [name=payment_id]').value=id;document.querySelector('#payForm [name=paid_date]').value=state.dashboard?.today||new Date().toISOString().slice(0,10);document.querySelector('#payForm [name=actual_amount]').value=(Number(amountCents)/100).toFixed(2).replace('.',',');document.getElementById('payDescription').textContent=description;openModal('payModal')}
window.deletePayment=async function(id,paid=false){if(!confirm(paid?'Eliminare questo pagamento già eseguito? L’importo verrà rimesso nel saldo.':'Eliminare questo pagamento in corso?'))return;try{await api(`/spese-programma/api/payments/${id}`,{method:'DELETE'});toast(paid?'Pagamento eliminato e saldo ripristinato':'Pagamento eliminato');await refreshStores();await Promise.all([loadPayments(),loadDashboard()])}catch(e){toast(e.message,true)}}
window.deleteStore=async function(id){const st=state.stores.find(x=>Number(x.id)===Number(id));if(!st)return;if(!confirm(`Eliminare definitivamente il negozio “${st.name}” e tutti i suoi dati?`))return;try{await api(`/spese-programma/api/stores/${id}`,{method:'DELETE'});toast(`Negozio ${st.name} eliminato`);await refreshStores();if(!state.stores.length){closeModal('storeSwitcherModal');document.getElementById('storeGate').hidden=false;state.dashboard=null;return}state.scope='global';state.storeId=state.stores[0].id;closeModal('storeSwitcherModal');updateContextUI();await loadCurrentContext()}catch(e){toast(e.message,true)}}
function exportCsv(){window.location.href=`/spese-programma/api/export.csv?${scopeQuery()}`}

// Cruscotto leggibile: importi principali, agenda filtrabile e tetti mensili.
let expenseOverview = null;
let commandFilter = 'now';
let commandShowAll = false;
const overviewAllItems = d => {
  const g=d?.groups||{};
  return [...(g.overdue||[]),...(g.today||[]),...(g.week||[]),...(g.no_date||[]),...(g.later||[])];
};
function openQuickBudgetEditor(){
  const box=document.getElementById('homeBudgetEditor');
  if(!box)return;
  box.open=true;box.scrollIntoView({behavior:'smooth',block:'center'});
  const first=box.querySelector('input[name="monthly_limit"]');
  if(first)setTimeout(()=>first.focus({preventScroll:true}),270);
}
function renderCommandPayments(d){
  const g=d.groups||{},all=overviewAllItems(d);
  const now=[...(g.overdue||[]),...(g.today||[])];
  const week=[...now,...(g.week||[])];
  const filtered=commandFilter==='now'?now:commandFilter==='week'?week:commandFilter==='no_date'?(g.no_date||[]):all;
  const list=document.getElementById('urgentPaymentsList');
  const items=commandShowAll?filtered:filtered.slice(0,7);
  const desc={now:'scaduti o in scadenza oggi',week:'scaduti o in scadenza entro 7 giorni',no_date:'senza una data impostata',all:'ancora aperti'}[commandFilter];
  if(!filtered.length){
    list.innerHTML=`<div class="command-empty"><span>✓</span><strong>Nessun pagamento ${desc}</strong><p>Seleziona un altro filtro per controllare le prossime scadenze.</p></div>`;
  }else{
    list.innerHTML=items.map(p=>{
      const tag=p.bucket==='overdue'?'SCADUTO':p.bucket==='today'?'OGGI':p.bucket==='week'?'ENTRO 7 GIORNI':p.bucket==='no_date'?'SENZA DATA':'FUTURO';
      const due=p.due_date?italianDate(p.due_date):'Scadenza non indicata';
      return `<article class="urgent-payment ${p.bucket}"><span class="urgency-label">${tag}</span><div class="urgent-detail"><strong>${esc(p.description)}</strong><small>${isGlobal()?`<span class="command-store-tag">${esc(p.store_name)}</span> · `:''}${p.kind==='fixed'?'Rata fissa · ':''}${due}</small></div><strong class="urgent-amount">${formatMoney(p.amount_cents)}</strong><div class="row-actions">${selectionButton(p)}<button type="button" class="small-btn pay" data-urgent-kind="${p.kind}" data-urgent-id="${p.id}" aria-label="Registra pagamento di ${esc(p.description)}">Paga →</button></div></article>`;
    }).join('');
    list.querySelectorAll('[data-urgent-kind]').forEach(btn=>btn.addEventListener('click',async()=>{
      const p=all.find(x=>Number(x.id)===Number(btn.dataset.urgentId)&&x.kind===btn.dataset.urgentKind);
      if(!p)return;
      if(p.kind==='fixed'){
        try{await loadFixedExpenses();window.openFixedPay(p.id)}catch(e){toast(e.message,true)}
      }else window.openPay(p.id,p.description,p.amount_cents);
    }));
  }
  setTextIfPresent('urgentStatus',`${filtered.length} ${filtered.length===1?'voce':'voci'} ${desc}`);
  document.getElementById('filterNowCount').textContent=now.length;
  document.getElementById('filterWeekCount').textContent=week.length;
  document.getElementById('filterAllCount').textContent=all.length;
  document.getElementById('filterNoDateCount').textContent=(g.no_date||[]).length;
  document.querySelectorAll('[data-urgency-filter]').forEach(x=>{
    const active=x.dataset.urgencyFilter===commandFilter;
    x.classList.toggle('active',active);x.setAttribute('aria-pressed',active?'true':'false');
  });
  const more=document.getElementById('showAllUrgent');
  more.hidden=filtered.length<=7;
  more.textContent=commandShowAll?'Mostra meno ↑':`Mostra tutti i ${filtered.length} pagamenti ↓`;
}
function renderHomeBudgetEditor(d){
  const stores=d.stores||[], box=document.getElementById('homeBudgetStores');
  const incomplete=stores.filter(x=>x.limit_cents===null);
  const summary=document.getElementById('budgetEditorSummary');
  summary.textContent=incomplete.length?`${incomplete.length} ${incomplete.length===1?'negozio senza limite':'negozi senza limite'}`:'Limiti impostati · premi per modificarli';
  // Non chiudere un editor aperto: l'utente potrebbe stare compilando un importo.
  box.innerHTML=stores.map(st=>{
    const configured=st.limit_cents!==null;
    return `<form class="home-budget-store" data-home-limit-store="${st.id}"><div class="home-budget-store-name"><strong>${esc(st.name)}</strong><small>${configured?`Speso ${formatMoney(st.paid_cents)} · Previsto ancora ${formatMoney(st.pending_cents+st.fixed_due_cents)}`:'Nessun limite mensile definito'}</small></div><label><span>Limite mensile (€)</span><input type="text" name="monthly_limit" inputmode="decimal" required placeholder="Es. 15.000" value="${configured?(st.limit_cents/100).toFixed(2).replace('.',','):''}" /></label><button type="submit" class="primary-btn">Salva</button></form>`;
  }).join('');
  box.querySelectorAll('form[data-home-limit-store]').forEach(form=>form.addEventListener('submit',async e=>{
    e.preventDefault();
    try{
      const val=form.elements.monthly_limit.value;
      await api('/spese-programma/api/expenses/limit',{method:'POST',body:JSON.stringify({store_id:Number(form.dataset.homeLimitStore),monthly_limit:val})});
      toast('Limite mensile aggiornato');
      await Promise.all([loadExecutiveOverview(),loadSelectedExpenses()]);
      if(state.page==='budgets')await loadSpendingBudgets();
    }catch(err){toast(err.message,true)}
  }));
}
function renderExecutiveOverview(d){
  if(!d)return;
  const groups=d.groups||{};
  const subtotal=(key)=>(groups[key]||[]).reduce((n,p)=>n+Number(p.amount_cents||0),0);
  const overdue=subtotal('overdue'),today=subtotal('today'),week=subtotal('week');
  const immediately=overdue+today;
  const all=overviewAllItems(d);
  const knownMonthly=Number(d.paid_this_month_cents||0)+Number(d.due_this_month_cents||0);
  const limit=Number(d.monthly_limit_cents)||0,margin=d.margin_cents;
  const hasLimit=Boolean(d.limit_complete);
  const bal=Number(state.dashboard?.balance_cents)||0;
  setTextIfPresent('decisionNow',formatMoney(immediately));
  setTextIfPresent('decisionNowFlag',immediately>0?`${(groups.overdue||[]).length+(groups.today||[]).length} da saldare`:'Tutto in regola');
  document.getElementById('decisionPayCard').classList.toggle('decision-is-clear',immediately===0);
  setTextIfPresent('decisionNowSub',immediately>0?`${(groups.overdue||[]).length} scaduti (${formatMoney(overdue)}) + ${(groups.today||[]).length} di oggi (${formatMoney(today)}).`:'Nessuna scadenza scaduta o prevista per oggi.');
  setTextIfPresent('decisionNextWeek',formatMoney(week));
  setTextIfPresent('decisionOtherOpen',formatMoney(d.due_this_month_cents));
  setTextIfPresent('decisionMonthPaid',formatMoney(d.paid_this_month_cents));
  setTextIfPresent('decisionBalance',formatMoney(bal));
  setTextIfPresent('decisionSpend',hasLimit?(margin<0?`− ${formatMoney(-margin)}`:formatMoney(margin)):'—');
  setTextIfPresent('decisionSpendFlag',!hasLimit?'Limite mancante':margin<0?'Budget superato':margin===0?'Budget esaurito':'Budget disponibile');
  document.getElementById('decisionBudgetCard').classList.toggle('decision-over-budget',hasLimit&&margin<=0);
  document.getElementById('decisionBudgetCard').classList.toggle('decision-needs-budget',!hasLimit);
  setTextIfPresent('decisionSpendSub',hasLimit?`Limite ${formatMoney(limit)} − ${formatMoney(d.paid_this_month_cents)} già pagati − ${formatMoney(d.due_this_month_cents)} da pagare = margine ${margin<0?'negativo':'residuo'}. Non è denaro sul conto.`:'Devi prima indicare il limite mensile dei negozi. Non posso inventare quanto puoi spendere.');
  const bar=document.getElementById('budgetProgressTrack');
  const paid=hasLimit&&limit>0?Number(d.paid_this_month_cents)/limit*100:0;
  const due=hasLimit&&limit>0?Number(d.due_this_month_cents)/limit*100:0;
  const first=Math.min(100,Math.max(0,paid));
  const second=Math.min(100-first,Math.max(0,due));
  document.getElementById('budgetProgressPaid').style.width=`${first}%`;
  document.getElementById('budgetProgressDue').style.width=`${second}%`;
  bar.setAttribute('aria-valuenow',String(Math.round(Math.min(100,paid+due))));
  bar.classList.toggle('unconfigured',!hasLimit);
  bar.classList.toggle('over-budget',hasLimit&&knownMonthly>limit);
  setTextIfPresent('budgetProgressPaidLabel',formatMoney(d.paid_this_month_cents));
  setTextIfPresent('budgetProgressDueLabel',formatMoney(d.due_this_month_cents));
  setTextIfPresent('budgetProgressLeftLabel',hasLimit?formatMoney(margin):'—');
  setTextIfPresent('budgetProgressNote',hasLimit?`Nel mese sono già previsti ${formatMoney(knownMonthly)} di spese su un tetto di ${formatMoney(limit)}.${margin<0?' ATTENZIONE: importo previsto oltre il limite.':''}`:'Per vedere questo grafico, inserisci il limite mensile per ogni negozio.');
  renderCommandPayments(d);
  renderHomeBudgetEditor(d);
}
async function loadExecutiveOverview(){
  if(!state.stores.length)return;
  const d=await api(`/spese-programma/api/expenses/overview?${scopeQuery()}`);expenseOverview=d;renderExecutiveOverview(d);return d;
}
async function loadSpendingBudgets(){
  const d=await Promise.all([loadExecutiveOverview(),loadSelectedExpenses()]);if(!d)return;
  const complete=d.limit_complete;
  setTextIfPresent('spendingLimitTotal',complete?formatMoney(d.monthly_limit_cents):'Da impostare');
  setTextIfPresent('spendingMonthPaid',formatMoney(d.paid_this_month_cents));
  setTextIfPresent('spendingMonthDue',formatMoney(d.due_this_month_cents));
  setTextIfPresent('spendingMonthMargin',complete?formatMoney(d.margin_cents):'Da impostare');
  setTextIfPresent('spendingMarginHint',complete?(d.margin_cents<0?'Limite superato: verifica le prossime spese':'Margine dopo tutte le spese già note'):'Configura ogni negozio per vedere il totale');
  const grid=document.getElementById('spendingBudgetStores');
  grid.innerHTML=(d.stores||[]).map(s=>`<form class="budget-store card" data-limit-store="${s.id}"><div class="budget-store-head"><div><p class="eyebrow">NEGOZIO</p><h3>${esc(s.name)}</h3></div><span class="budget-store-state ${s.margin_cents!==null&&s.margin_cents<0?'negative':''}">${s.limit_cents===null?'Non configurato':s.margin_cents<0?'Sopra il budget':'Entro il budget'}</span></div><div class="budget-store-stats"><div><small>Già pagato</small><strong>${formatMoney(s.paid_cents)}</strong></div><div><small>Da pagare</small><strong>${formatMoney(s.pending_cents+s.fixed_due_cents)}</strong></div><div><small>Margine residuo</small><strong class="${s.margin_cents!==null&&s.margin_cents<0?'negative':''}">${s.margin_cents===null?'—':formatMoney(s.margin_cents)}</strong></div></div><label>Limite mensile di spesa (€)<input name="monthly_limit" inputmode="decimal" required placeholder="es. 25000" value="${s.limit_cents===null?'':(s.limit_cents/100).toFixed(2).replace('.',',')}" /></label><button class="primary-btn" type="submit">Salva limite mensile</button></form>`).join('');
  grid.querySelectorAll('form[data-limit-store]').forEach(form=>form.addEventListener('submit',async e=>{
    e.preventDefault();const sid=Number(form.dataset.limitStore),amount=form.elements.monthly_limit.value;
    try{await api('/spese-programma/api/expenses/limit',{method:'POST',body:JSON.stringify({store_id:sid,monthly_limit:amount})});toast('Budget mensile salvato');await loadSpendingBudgets()}catch(err){toast(err.message,true)}
  }));
}
async function loadExpenseHistory(){
  const period=document.getElementById('historyPeriod').value;
  const input=document.getElementById('historyAnchor');
  if(!input.value)input.value=state.dashboard?.today||new Date().toISOString().slice(0,10);
  const d=await api(`/spese-programma/api/expenses/history?${scopeQuery()}&period=${encodeURIComponent(period)}&anchor=${encodeURIComponent(input.value)}`);
  setTextIfPresent('historyTotal',formatMoney(d.total_cents));setTextIfPresent('historyCount',String(d.count));
  setTextIfPresent('historyPeriodLabel',period==='all'?'Tutto lo storico':`Dal ${italianDate(d.start)} al ${italianDate(new Date(new Date(d.end_exclusive+'T12:00:00')-86400000).toISOString().slice(0,10))}`);
  const list=document.getElementById('expenseHistoryList');
  if(!d.items.length){list.innerHTML='<div class="empty-state">Nessuna spesa pagata in questo periodo. Prova ad ampliare il filtro.</div>';return}
  list.innerHTML=d.items.map(x=>`<div class="list-row history-item"><div><div class="row-title">${esc(x.description)}</div><div class="row-meta">${isGlobal()?`<span class="store-tag">${esc(x.store_name)}</span>`:''}<span>${italianDate(x.event_at)}</span><span>${esc(x.expense_type)}</span>${x.category?`<span>${esc(x.category)}</span>`:''}${x.notes?`<small>${esc(x.notes)}</small>`:''}</div></div><div class="row-amount">− ${formatMoney(x.amount_cents)}</div></div>`).join('');
}
function exportExpenseHistoryCsv(){
  const period=document.getElementById('historyPeriod').value;
  const anchor=document.getElementById('historyAnchor').value||state.dashboard?.today||new Date().toISOString().slice(0,10);
  api(`/spese-programma/api/expenses/history?${scopeQuery()}&period=${encodeURIComponent(period)}&anchor=${encodeURIComponent(anchor)}`).then(d=>{
    const quote=s=>'"'+String(s??'').replace(/"/g,'""')+'"';
    const lines=[['Data','Negozio','Tipo','Descrizione','Categoria','Importo euro','Note'],...d.items.map(x=>[x.event_at,x.store_name,x.expense_type,x.description,x.category,(x.amount_cents/100).toFixed(2).replace('.',','),x.notes])].map(a=>a.map(quote).join(';'));
    const blob=new Blob(['\uFEFF'+lines.join('\r\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');
    a.href=url;a.download=`storico_spese_${period}_${anchor}.csv`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1200);
  }).catch(e=>toast(e.message,true));
}

function initInteractiveEffects(){ /* Decorative continuous animations disabled. */ }

// Eventi UI
document.querySelectorAll('.nav-btn').forEach(btn=>btn.addEventListener('click',()=>setPage(btn.dataset.page)));
document.querySelectorAll('[data-go]').forEach(btn=>btn.addEventListener('click',()=>setPage(btn.dataset.go)));
document.querySelectorAll('[data-close]').forEach(btn=>btn.addEventListener('click',()=>closeModal(btn.dataset.close)));
document.querySelectorAll('.modal-backdrop').forEach(backdrop=>backdrop.addEventListener('click',e=>{if(e.target===backdrop&&backdrop.id!=='setupModal')backdrop.hidden=true}));
document.getElementById('selectGlobal').onclick=selectGlobal;
document.getElementById('quickNewPayment').onclick=()=>openOperationModal('payment');document.getElementById('historyPeriod').addEventListener('change',()=>loadExpenseHistory().catch(e=>toast(e.message,true)));document.getElementById('historyAnchor').addEventListener('change',()=>loadExpenseHistory().catch(e=>toast(e.message,true)));document.getElementById('historyCurrent').onclick=()=>{document.getElementById('historyAnchor').value=state.dashboard?.today||new Date().toISOString().slice(0,10);loadExpenseHistory().catch(e=>toast(e.message,true))};document.getElementById('historyExportCsv').onclick=exportExpenseHistoryCsv;
document.getElementById('openRecharge').onclick=()=>openOperationModal('recharge');document.getElementById('openRecharge2').onclick=()=>openOperationModal('recharge');document.getElementById('openPayment').onclick=()=>openOperationModal('payment');document.getElementById('openFixedExpense').onclick=()=>openOperationModal('fixed');document.getElementById('openVariableBudget').onclick=()=>openOperationModal('variable');document.getElementById('exportCsv').onclick=exportCsv;
document.getElementById('awareDaySlider').addEventListener('input',e=>{state.awarePinnedByUser=true;renderAwareForDay(Number(e.currentTarget.value))});document.getElementById('awareDaySlider').addEventListener('change',e=>{state.awarePinnedByUser=true;renderAwareForDay(Number(e.currentTarget.value))});
document.querySelector('#setupForm [name=previous_month_total]').addEventListener('input',updateSetupDailyAware);
document.querySelector('#fixedExpenseForm [name=amount]').addEventListener('input',updateFixedModalPreview);document.querySelector('#fixedExpenseForm [name=frequency_months]').addEventListener('change',updateFixedModalPreview);
document.getElementById('variablePaymentStatus').addEventListener('change',syncVariableOrderPaymentFields);
document.querySelectorAll('[data-due-days]').forEach(btn=>btn.addEventListener('click',()=>{const form=document.getElementById('variablePurchaseForm'),raw=form.purchase_date.value||state.dashboard?.today||new Date().toISOString().slice(0,10),base=new Date(raw+'T12:00:00');base.setDate(base.getDate()+Number(btn.dataset.dueDays||0));const y=base.getFullYear(),m=String(base.getMonth()+1).padStart(2,'0'),d=String(base.getDate()).padStart(2,'0');form.due_date.value=`${y}-${m}-${d}`;}));
document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',async()=>{state.paymentStatus=tab.dataset.status;document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===tab));document.getElementById('paidFilters').hidden=state.paymentStatus!=='paid';document.getElementById('pendingDistanceFilter').hidden=state.paymentStatus!=='pending';await loadPayments();saveUiState()}));
document.querySelectorAll('.distance-mode-btn').forEach(btn=>btn.addEventListener('click',()=>{const mode=btn.dataset.distanceMode==='months'?'months':'days';if(state.distanceMode===mode)return;state.distanceMode=mode;state.distancePinnedByUser=false;state.distanceValue=null;setupDistanceFilter(state.pendingPayments);saveUiState()}));
document.getElementById('distanceSlider').addEventListener('input',e=>{state.distancePinnedByUser=true;state.distanceValue=Number(e.currentTarget.value);renderPendingPaymentsFiltered();saveUiState()});document.getElementById('distanceSlider').addEventListener('change',e=>{state.distancePinnedByUser=true;state.distanceValue=Number(e.currentTarget.value);renderPendingPaymentsFiltered();saveUiState()});
document.getElementById('paidMonth').addEventListener('change',async()=>{await loadPayments();saveUiState()});document.getElementById('clearPaidMonth').onclick=async()=>{document.getElementById('paidMonth').value='';await loadPayments();saveUiState()};document.getElementById('rechargeMonth').addEventListener('change',async()=>{await loadRecharges();saveUiState()});document.getElementById('clearRechargeMonth').onclick=async()=>{document.getElementById('rechargeMonth').value='';await loadRecharges();saveUiState()};

async function createStoreFromForm(form,first=false){const fd=new FormData(form),name=String(fd.get('name')||'').trim();if(!name)return;try{const data=await api('/spese-programma/api/stores',{method:'POST',body:JSON.stringify({name})});form.reset();await refreshStores();document.getElementById('storeGate').hidden=true;state.scope='store';state.storeId=Number(data.store.id);updateContextUI();closeModal('storeSwitcherModal');await loadDashboard();toast(`Negozio ${name} creato`);saveUiState()}catch(e){toast(e.message,true);if(first)document.getElementById('storeGate').hidden=false}}
document.getElementById('firstStoreForm').addEventListener('submit',e=>{e.preventDefault();createStoreFromForm(e.currentTarget,true)});document.getElementById('newStoreForm').addEventListener('submit',e=>{e.preventDefault();createStoreFromForm(e.currentTarget,false)});

document.getElementById('setupForm').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget;if(parseEuroInput(form.previous_month_total.value)<=0)return toast('Inserisci le spese totali del mese scorso',true);const payload={store_id:Number(form.store_id.value),initial_amount:form.initial_amount.value,previous_month_total:form.previous_month_total.value};try{await api('/spese-programma/api/setup',{method:'POST',body:JSON.stringify(payload)});closeModal('setupModal');toast('Negozio attivato: fondi consapevoli operativi');await refreshStores();await loadDashboard()}catch(err){toast(err.message,true)}});
document.getElementById('rechargeForm').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget,fd=new FormData(form),storeId=isGlobal()?Number(fd.get('store_id')):Number(state.storeId);try{await api('/spese-programma/api/recharges',{method:'POST',body:JSON.stringify({store_id:storeId,amount:fd.get('amount'),note:fd.get('note')})});closeModal('rechargeModal');form.reset();toast('Ricarica aggiunta al saldo');await refreshStores();await loadDashboard();if(state.page==='recharges')await loadRecharges()}catch(err){toast(err.message,true)}});
document.getElementById('paymentForm').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget,fd=new FormData(form),storeId=isGlobal()?Number(fd.get('store_id')):Number(state.storeId),payload=Object.fromEntries(fd);payload.store_id=storeId;try{await api('/spese-programma/api/payments',{method:'POST',body:JSON.stringify(payload)});closeModal('paymentModal');form.reset();form.priority.value=50;toast('Pagamento aggiunto tra quelli in corso');await refreshStores();await Promise.all([loadDashboard(),loadPayments()])}catch(err){toast(err.message,true)}});
document.getElementById('fixedExpenseForm').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget,fd=new FormData(form),storeId=isGlobal()?Number(fd.get('store_id')):Number(state.storeId),payload=Object.fromEntries(fd);payload.store_id=storeId;try{await api('/spese-programma/api/fixed-expenses',{method:'POST',body:JSON.stringify(payload)});closeModal('fixedExpenseModal');form.reset();form.frequency_months.value='1';toast('Spesa fissa attivata: il denaro inizia a essere protetto');await refreshStores();await loadDashboard();if(state.page==='fixed')await loadFixedExpenses()}catch(err){toast(err.message,true)}});
document.getElementById('variableBudgetForm').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget,fd=new FormData(form),storeId=isGlobal()?Number(fd.get('store_id')):Number(state.storeId),payload=Object.fromEntries(fd);payload.store_id=storeId;try{await api('/spese-programma/api/variable-budgets',{method:'POST',body:JSON.stringify(payload)});closeModal('variableBudgetModal');form.reset();toast('Spesa variabile creata');await refreshStores();await loadDashboard();await loadVariableBudgets()}catch(err){toast(err.message,true)}});
document.getElementById('variablePurchaseForm').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget,fd=new FormData(form),id=fd.get('budget_id'),paymentStatus=fd.get('payment_status');try{const result=await api(`/spese-programma/api/variable-budgets/${id}/orders`,{method:'POST',body:JSON.stringify({amount:fd.get('amount'),purchase_date:fd.get('purchase_date'),payment_status:paymentStatus,due_date:fd.get('due_date'),note:fd.get('note')})});closeModal('variablePurchaseModal');toast(paymentStatus==='pending'?'Ordine registrato e aggiunto ai Pagamenti':'Ordine registrato e saldo aggiornato');await refreshStores();await Promise.all([loadDashboard(),loadVariableBudgets()]);if(state.page==='payments')await loadPayments()}catch(err){toast(err.message,true)}});
document.getElementById('fixedPayForm').addEventListener('submit',async e=>{e.preventDefault();const fd=new FormData(e.currentTarget),id=fd.get('fixed_expense_id');try{await api(`/spese-programma/api/fixed-expenses/${id}/pay`,{method:'POST',body:JSON.stringify({paid_date:fd.get('paid_date'),actual_amount:fd.get('actual_amount')})});closeModal('fixedPayModal');toast('Rata pagata: saldo aggiornato e nuovo ciclo di accantonamento avviato');await refreshStores();await loadDashboard();if(state.page==='fixed')await loadFixedExpenses();if(state.page==='payments'&&state.paymentStatus==='paid')await loadPayments()}catch(err){toast(err.message,true)}});
document.getElementById('payForm').addEventListener('submit',async e=>{e.preventDefault();const fd=new FormData(e.currentTarget),id=fd.get('payment_id');try{await api(`/spese-programma/api/payments/${id}/pay`,{method:'POST',body:JSON.stringify({paid_date:fd.get('paid_date'),actual_amount:fd.get('actual_amount')})});closeModal('payModal');toast('Pagamento sottratto dal saldo e dai fondi consapevoli');await refreshStores();await loadDashboard();if(state.page==='payments')await loadPayments()}catch(err){toast(err.message,true)}});

function scheduleMidnightAwareRefresh(){const now=new Date(),next=new Date(now);next.setHours(24,0,1,0);setTimeout(async()=>{try{state.awarePinnedByUser=false;state.awareSelectedDay=null;await refreshStores();await loadDashboard();if(state.page==='fixed')await loadFixedExpenses();toast('Nuove quote giornaliere aggiunte: fondi consapevoli e soldi impegnati aggiornati')}catch(e){console.error(e)}finally{scheduleMidnightAwareRefresh()}},Math.max(1000,next-now))}
let scrollSaveTimer=null;window.addEventListener('scroll',()=>{if(restoringScroll)return;clearTimeout(scrollSaveTimer);scrollSaveTimer=setTimeout(saveUiState,40)},{passive:true});window.addEventListener('pagehide',saveUiState);window.addEventListener('beforeunload',saveUiState);

// Azioni veloci del nuovo cruscotto, senza cambiare pagine o perdere i filtri.
document.querySelectorAll('[data-urgency-filter]').forEach(btn=>btn.addEventListener('click',()=>{
  commandFilter=btn.dataset.urgencyFilter;commandShowAll=false;
  if(expenseOverview)renderCommandPayments(expenseOverview);
}));
document.getElementById('showAllUrgent').addEventListener('click',()=>{
  commandShowAll=!commandShowAll;
  if(expenseOverview)renderCommandPayments(expenseOverview);
});
document.getElementById('decisionShowUrgent').addEventListener('click',()=>{
  commandFilter='now';commandShowAll=false;
  if(expenseOverview)renderCommandPayments(expenseOverview);
  document.getElementById('actionCenter').scrollIntoView({behavior:'smooth',block:'start'});
});
for(const id of ['decisionSetBudget','quickBudgetEdit'])document.getElementById(id).addEventListener('click',openQuickBudgetEditor);
async function bootstrap(){
  initInteractiveEffects();const saved=readSavedUiState();
  try{await refreshStores();if(!state.stores.length){document.getElementById('storeGate').hidden=false;return}state.storeId=state.stores[0].id;applySavedUiState(saved);const host=window.EXPENSE_SITE_CONTEXT;if(host){if(host.store==='ALL')state.scope='global';else if(state.stores.some(s=>Number(s.id)===Number(host.id))){state.scope='store';state.storeId=Number(host.id)}}if(state.scope==='store'&&!currentStore())state.storeId=state.stores[0].id;updateContextUI();document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.status===state.paymentStatus));document.getElementById('paidFilters').hidden=state.paymentStatus!=='paid';document.querySelectorAll('.distance-mode-btn').forEach(btn=>btn.classList.toggle('active',btn.dataset.distanceMode===state.distanceMode));setPage(state.page,false,false);await loadDashboard();if(state.page==='payments')await loadPayments();if(state.page==='fixed')await loadFixedExpenses();if(state.page==='variables')await loadVariableBudgets();if(state.page==='recharges')await loadRecharges();if(state.page==='budgets')await loadSpendingBudgets();if(state.page==='history')await loadExpenseHistory()}catch(e){toast(e.message,true)}finally{restoreExactScrollPosition();scheduleMidnightAwareRefresh()}
}
bootstrap();


// Selection is a saved view of the original expenses, never a duplicate payment.
let selectedExpenseItems = [];
function expenseSelectionKey(p){
  return p.selection_key || (p.kind==='fixed' ? `fixed:${p.id}:${p.due_date||p.next_due_date}` : p.fixed_expense_id ? `fixed:${p.fixed_expense_id}:${p.due_date}` : `payment:${p.id}`);
}
function selectionButton(p){
  const key=expenseSelectionKey(p), selected=selectedExpenseItems.some(x=>x.selection_key===key);
  return `<button type="button" class="small-btn select-expense" data-selection-key="${esc(key)}" aria-pressed="${selected}" onclick="toggleExpenseSelection(this)">${selected?'Rimuovi selezione':'Seleziona'}</button>`;
}
async function toggleExpenseSelection(button){
  button.disabled=true;
  try{
    await api('/spese-programma/api/expenses/selection',{method:'POST',body:JSON.stringify({key:button.dataset.selectionKey,selected:button.getAttribute('aria-pressed')!=='true'})});
    await loadSelectedExpenses();
  }catch(e){toast(e.message,true)}finally{button.disabled=false}
}
async function loadSelectedExpenses(){
  const scope=scopeQuery();
  const data=await api(`/spese-programma/api/expenses/selected?${scope}`);
  if(scope!==scopeQuery())return;
  selectedExpenseItems=data.items;
  document.getElementById('selectionTotal').textContent=formatMoney(data.total_cents);
  document.getElementById('selectionCount').textContent=data.count;
  document.getElementById('selectionScope').textContent=isGlobal()?'Tutti i negozi':currentStore()?.name||'';
  document.getElementById('selectedExpensesList').innerHTML=data.items.length?data.items.map(p=>`<div class="list-row"><div><div class="row-title">${esc(p.description)}</div><div class="row-meta">${storeTag(p)}<span>Scadenza: ${italianDate(p.due_date)}</span><span class="priority ${priorityClass(p.priority)}">Priorità ${p.priority}</span>${p.category?`<span>${esc(p.category)}</span>`:''}${p.notes?`<span>${esc(p.notes)}</span>`:''}</div></div><div class="row-amount">${formatMoney(p.amount_cents)}</div><div class="row-actions">${selectionButton(p)}<button type="button" class="small-btn pay" data-selected-pay="${esc(p.selection_key)}">Paga</button><button type="button" class="small-btn delete" onclick="${p.kind==='fixed'?'deleteFixedExpense':'deletePayment'}(${p.id})">${p.kind==='fixed'?'Rimuovi spesa fissa':'Elimina'}</button></div></div>`).join(''):'<div class="empty-state">Nessuna spesa selezionata. Premi Seleziona nelle spese da pagare, nel cruscotto o nelle spese fisse.</div>';
  document.querySelectorAll('[data-selected-pay]').forEach(button=>button.onclick=async()=>{
    const p=selectedExpenseItems.find(x=>x.selection_key===button.dataset.selectedPay);if(!p)return;
    if(p.kind==='fixed'){try{await loadFixedExpenses();openFixedPay(p.id)}catch(e){toast(e.message,true)}}else openPay(p.id,p.description,p.amount_cents);
  });
  document.querySelectorAll('[data-selection-key]').forEach(button=>{
    const selected=data.items.some(p=>p.selection_key===button.dataset.selectionKey);
    button.setAttribute('aria-pressed',String(selected));button.textContent=selected?'Rimuovi selezione':'Seleziona';
  });
}
