import {datasets,jobs,recheck} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {RecheckStatus,ClaimStatus,Weekdays,Role} from '../constants/enums.js';
import {esc,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {localStamp,minutesLabel,shiftLabel,nowLocalInput,localInputToEpoch} from '../utils/time.js';

const canWrite=()=>['admin','analyst'].includes(session.user.role);
const isAdmin=()=>session.user.role==='admin';

function statusPill(value){return '<span class="status '+esc(value)+'">'+esc(RecheckStatus[value]||value)+'</span>';}
function claimPill(value){return '<span class="status '+esc(value)+'">'+esc(ClaimStatus[value]||value)+'</span>';}

function reasonList(reasons){
 if(!reasons?.length)return '';
 return '<ul class="reason-list">'+reasons.map(r=>'<li><strong>'+esc(r.message||r.code)+'</strong>'+(r.detail?'<span>'+esc(r.detail)+'</span>':'')+'</li>').join('')+'</ul>';
}

function scheduleBlock(s){
 if(!s)return '<p class="empty">尚无有效排程。请先预排并确认。</p>';
 return '<div class="plan-sheet"><div class="kv-grid">'+
  '<div><span>排程版本</span><strong>V'+s.plan_version+'</strong></div>'+
  '<div><span>仪器</span><strong>'+esc(s.instrument?.name||'—')+'</strong><small>'+esc(s.instrument?.code||'')+'</small></div>'+
  '<div><span>检验员</span><strong>'+esc(s.inspector?.name||'—')+'</strong><small>'+esc(s.inspector?.code||'')+'</small></div>'+
  '<div><span>制样</span><strong>'+esc(localStamp(s.prep_start_local))+' → '+esc(localStamp(s.prep_end_local))+'</strong><small>'+esc(minutesLabel(s.prep_minutes))+'</small></div>'+
  '<div><span>检测</span><strong>'+esc(localStamp(s.test_start_local))+' → '+esc(localStamp(s.test_end_local))+'</strong><small>'+esc(minutesLabel(s.test_minutes))+'</small></div>'+
  '<div><span>制样后有效期</span><strong>'+esc(localStamp(s.validity_end_local))+'</strong><small>检测全程落在有效期内：'+(s.validity_ok?'是':'否')+'</small></div>'+
  '</div><p class="hint">预约依据指纹 '+esc((s.basis?.hash||'').slice(0,16)||'—')+' · 资源版本 仪器 V'+esc(s.basis?.instrument?.version??'—')+' / 检验员 V'+esc(s.basis?.inspector?.version??'—')+'</p></div>';
}

function previewBlock(preview){
 if(!preview)return '';
 if(!preview.feasible)return '<section class="preview-card blocked"><div class="section-title"><h3>无可行时段</h3><span>'+esc(preview.expires_at_local||'').slice(0,16)+' 前有效</span></div>'+reasonList(preview.reasons)+'<p class="hint">可调整班次、仪器能力、时长或停机安排后重新预排。</p></section>';
 const p=preview.plan;
 return '<section class="preview-card"><div class="section-title"><h3>可行方案（预览）</h3><span>'+(preview.expired?'已过期，确认将自动重排':'有效期至 '+esc(localStamp(preview.expires_at_local)))+'</span></div><div class="kv-grid">'+
  '<div><span>仪器</span><strong>'+esc(p.instrument?.name||'')+'</strong><small>'+esc(p.instrument?.code||'')+' · 制样 '+p.prep_minutes+' 分</small></div>'+
  '<div><span>检验员</span><strong>'+esc(p.inspector?.name||'')+'</strong><small>'+esc(p.inspector?.code||'')+'</small></div>'+
  '<div><span>制样</span><strong>'+esc(localStamp(p.prep_start_local))+' → '+esc(localStamp(p.prep_end_local))+'</strong></div>'+
  '<div><span>检测</span><strong>'+esc(localStamp(p.test_start_local))+' → '+esc(localStamp(p.test_end_local))+'</strong><small>'+minutesLabel(p.test_minutes)+'</small></div>'+
  '<div><span>有效期至</span><strong>'+esc(localStamp(p.validity_end_local))+'</strong><small>检测结束 '+minutesLabel(Math.max(0,(p.validity_end-p.test_end)/60))+' 前</small></div>'+
  '<div><span>工单范围</span><strong>'+esc((p.batches||[]).length)+' 批 · '+esc((p.item_labels||[]).join('、'))+'</strong></div>'+
  '</div><button class="primary" id="confirm-plan" '+(canWrite()?'':'disabled')+'>确认排程并占用资源 ↗</button><p class="hint">确认时整单占用仪器与检验员；并发确认由服务端串行校验。过期或资源变化会拒绝并返回重排预览。</p></section>';
}

function orderDetail(order){
 const claims=order.claims||[],revisions=order.revisions||[];
 return '<section class="panel recheck-detail"><div class="section-title"><h2>'+esc(order.name)+'</h2>'+statusPill(order.status)+'</div>'+
  '<p class="hint">来源分析 #'+order.job_id+' · 批次 '+order.batches.length+' 个 · 检验项 '+esc((order.item_labels||[]).join('、'))+'</p>'+
  reasonList(order.status==='blocked'||order.status==='abnormal'?order.block_reasons:[])+
  scheduleBlock(order.current_schedule)+
  '<div class="inline-actions recheck-actions">'+
   (['pending','previewed','blocked'].includes(order.status)?'<button class="button" data-action="preview">预排可行时段 ↻</button>':'')+
   (order.status==='scheduled'?'<button class="button" data-action="claim" '+(canWrite()?'':'disabled')+'>领工</button>':'')+
   (order.status==='scheduled'?'<button class="button" data-action="cancel-order" '+(canWrite()?'':'disabled')+'>取消排程</button>':'')+
   (order.status==='abnormal'?'<button class="button primary" data-action="resume">恢复检测</button><button class="button" data-action="replan">重排</button><button class="button" data-action="cancel-order">取消工单</button>':'')+
  '</div>'+
  '<div id="preview-slot">'+(workspace.recheckPreview&&workspace.recheckPreview.orderId===order.id?previewBlock(workspace.recheckPreview.data):'')+'</div>'+
  (order.block_reasons?.length&&order.status==='blocked'?'<p class="hint">阻塞工单可重新预排；系统会保留每次排程与重排依据。</p>':'')+
  '<div class="section-title"><h3>领工记录</h3><span>'+claims.length+' 条</span></div>'+
  table([{key:'id',label:'#'},{key:'status',label:'状态'},{key:'inspector_id',label:'检验员'},{key:'schedule_version',label:'排程版本'},{key:'issued_at_local',label:'领工时间'},{key:'note',label:'说明'}],claims.map(c=>({...c,status:ClaimStatus[c.status]||c.status,issued_at_local:localStamp(c.issued_at_local)})))+
  '<div class="inline-actions">'+claims.filter(c=>c.status==='issued'&&order.status==='scheduled').map(c=>'<button class="button small" data-start-claim="'+c.id+'">开工 #'+c.id+'</button>').join('')+claims.filter(c=>c.status==='started'&&order.status==='in_progress').map(c=>'<button class="button primary small" data-complete-claim="'+c.id+'">完工 #'+c.id+'</button>').join('')+'</div>'+
  '<div class="section-title"><h3>历次排程依据</h3><span>'+revisions.length+' 个版本</span></div>'+
  (revisions.length?'<div class="history-list">'+revisions.map(r=>'<article><div class="inline-actions"><strong>V'+r.version+' · '+esc(r.reason)+'</strong><span>'+esc(r.actor)+' · '+esc(localStamp(r.at_local))+'</span></div><p>'+esc(r.plan?.instrument?.name||'')+' / '+esc(r.plan?.inspector?.name||'')+' · 制样 '+esc(localStamp(r.plan?.prep_start_local))+' → 检测 '+esc(localStamp(r.plan?.test_start_local))+'–'+esc(localStamp(r.plan?.test_end_local))+'</p><small>依据指纹 '+esc((r.basis?.hash||'').slice(0,24))+' · 原排程 #'+r.schedule_id+'</small></article>').join('')+'</div>':'<p class="empty">确认排程后，每次建立、重排与恢复都会记录依据。</p>');
}

function orderList(orders){
 return '<div class="order-list">'+orders.map(o=>'<a class="order-item '+(workspace.recheckSelected===o.id?'active':'')+'" href="#/recheck/'+o.id+'"><div class="inline-actions"><strong>'+esc(o.name)+'</strong>'+statusPill(o.status)+'</div><span>'+o.batches.length+' 批 · '+esc((o.item_labels||[]).join('、'))+' · 分析 #'+o.job_id+'</span><small>'+(o.current_schedule?'检测 '+esc(localStamp(o.current_schedule.test_start_local))+'–'+esc(localStamp(o.current_schedule.test_end_local)):'尚未排程')+'</small></a>').join('')+(!orders.length?'<p class="empty">暂无复检工单。</p>':'')+'</div>';
}

function createOrderPanel(allJobs,datasetMap){
 const done=allJobs.filter(j=>j.status==='completed');
 return '<section class="panel"><div class="section-title"><h2>新建复检工单</h2><span>来自已完成分析</span></div><form id="recheck-order-form"><label>工单名称<input name="name" required minlength="2" value="复检排程 · 共败线索复核"></label><label>来源分析<select name="job_id" required>'+done.map(j=>'<option value="'+j.id+'">'+esc(j.name)+' · #'+j.id+'</option>').join('')+'</select></label><label>复检批次<select name="batches" id="batch-select" multiple size="8" required></select></label><label>检验项<div class="checks">'+workspace.recheckCatalog.items.map(i=>'<label><input type="checkbox" name="items" value="'+esc(i.key)+'"> '+esc(i.label)+'</label>').join('')+'</div></label><button type="submit" class="primary" '+(canWrite()&&done.length?'':'disabled')+'>登记工单</button><p class="hint">批次来自该分析的数据快照；检验项用于匹配仪器与检验员能力。</p></form></section>';
}

function resourcePanel(shifts,instruments,inspectors){
 const items=workspace.recheckCatalog.items;
 const shiftOptions=shifts.filter(s=>s.active).map(s=>'<option value="'+s.id+'">'+esc(shiftLabel(s))+'</option>').join('');
 const capChecks=(prefix,selected=[])=>'<div class="checks tight">'+items.map(i=>'<label><input type="checkbox" name="'+prefix+'-'+esc(i.key)+'" '+(selected.includes(i.key)?'checked':'')+'> '+esc(i.label)+'</label>').join('')+'</div>';
 return '<section class="panel"><div class="section-title"><h2>资源维护</h2><span>班次 · 仪器能力 · 检验员</span></div>'+
  '<div class="tabs"><button class="tab active" data-tab="instruments">仪器</button><button class="tab" data-tab="inspectors">检验员</button><button class="tab" data-tab="shifts">班次</button></div>'+
  '<div class="tab-pane" data-pane="instruments"><div class="resource-list">'+instruments.map(i=>'<article><div class="inline-actions"><strong>'+esc(i.name)+'</strong><span class="badge">'+esc(i.code)+'</span>'+(i.active?'':'<span class="badge off">已停用</span>')+'</div><span>'+esc(shiftLabel(i.shift))+' · 制样 '+i.prep_minutes+' 分 · 有效期 '+minutesLabel(i.validity_minutes)+'</span><small>能力：'+esc((i.item_labels||[]).join('、'))+'</small><small>单件检测：'+esc(Object.entries(i.item_minutes||{}).map(([k,v])=>k.replace('_fail','')+' '+v+'分').join('，')||'—')+'</small><div class="inline-actions">'+(isAdmin()?'<button class="text-button" data-toggle-instrument="'+i.id+'" data-active="'+(!i.active)+'">'+(i.active?'停用':'启用')+'</button>':'')+'</div></article>').join('')+'</div>'+
   (isAdmin()?'<details class="mini-form"><summary>维护仪器能力与时长</summary><form id="instrument-form"><label>选择仪器<select name="id" required>'+instruments.map(i=>'<option value="'+i.id+'">'+esc(i.name)+' ('+esc(i.code)+')</option>').join('')+'</select></label><label>班次<select name="shift_id">'+shiftOptions+'</select></label><label>检验项能力</label>'+capChecks('cap')+'<div class="parameter-grid"><label>单批制样时长(分)<input name="prep_minutes" type="number" min="1" value="5" required></label><label>制样后有效期(分)<input name="validity_minutes" type="number" min="1" value="240" required></label></div><div class="parameter-grid"><label>热稳定检测(分)<input name="m-heat_fail" type="number" min="1" value="20"></label><label>pH 检测(分)<input name="m-ph_fail" type="number" min="1" value="10"></label><label>黏度检测(分)<input name="m-viscosity_fail" type="number" min="1" value="12"></label><label>灰分检测(分)<input name="m-ash_fail" type="number" min="1" value="20"></label><label>色差检测(分)<input name="m-color_fail" type="number" min="1" value="10"></label></div><button type="submit" class="primary">保存仪器配置</button><p class="hint">能力或时长变更会导致未开工工单自动重排、已开工工单转异常待处置。</p></form></details>':'')+
  '</div>'+
  '<div class="tab-pane hidden" data-pane="inspectors"><div class="resource-list">'+inspectors.map(p=>'<article><div class="inline-actions"><strong>'+esc(p.name)+'</strong><span class="badge">'+esc(p.code)+'</span>'+(p.active?'':'<span class="badge off">已停用</span>')+'</div><span>'+esc(shiftLabel(p.shift))+'</span><small>能力：'+esc((p.item_labels||[]).join('、'))+'</small><div class="inline-actions">'+(isAdmin()?'<button class="text-button" data-toggle-inspector="'+p.id+'" data-active="'+(!p.active)+'">'+(p.active?'停用':'启用')+'</button>':'')+'</div></article>').join('')+'</div>'+
   (isAdmin()?'<details class="mini-form"><summary>维护检验员能力</summary><form id="inspector-form"><label>选择检验员<select name="id" required>'+inspectors.map(p=>'<option value="'+p.id+'">'+esc(p.name)+' ('+esc(p.code)+')</option>').join('')+'</select></label><label>班次<select name="shift_id">'+shiftOptions+'</select></label><label>检验项能力</label>'+capChecks('icap')+'<button type="submit" class="primary">保存检验员配置</button></form></details>':'')+
  '</div>'+
  '<div class="tab-pane hidden" data-pane="shifts"><div class="resource-list">'+shifts.map(s=>'<article><div class="inline-actions"><strong>'+esc(s.name)+'</strong><span class="badge">'+(s.active?'启用':'停用')+'</span></div><span>'+esc(shiftLabel(s))+'</span><small>'+esc((s.weekdays||[]).map(d=>Weekdays[d]).join('、'))+'</small></article>').join('')+'</div>'+
   (isAdmin()?'<form id="shift-form" class="mini-form"><div class="parameter-grid"><label>班次名称<input name="name" required minlength="2" value="补充班"></label><label>开始(时:分)<input name="start" type="time" value="08:00" required></label><label>结束(时:分)<input name="end" type="time" value="12:00" required></label></div><label>星期</label><div class="checks tight">'+Weekdays.map((d,i)=>'<label><input type="checkbox" name="wd" value="'+i+'" '+(i<5?'checked':'')+'> '+d+'</label>').join('')+'</div><button type="submit" class="primary">新增班次</button></form>':'')+
  '</div></section>';
}

function downtimePanel(instruments,downtimes){
 return '<section class="panel"><div class="section-title"><h2>仪器停机</h2><span>重排未开工 · 已开工转异常</span></div>'+
  (isAdmin()?'<form id="downtime-form"><label>仪器<select name="instrument_id" required>'+instruments.map(i=>'<option value="'+i.id+'">'+esc(i.name)+' ('+esc(i.code)+')</option>').join('')+'</select></label><div class="parameter-grid"><label>开始<input name="start" type="datetime-local" value="'+nowLocalInput()+'" required></label><label>预计结束<input name="end" type="datetime-local" value="'+nowLocalInput(480,90)+'" required></label></div><label>原因<input name="reason" required minlength="2" value="仪器故障停机"></label><button type="submit" class="primary">登记停机并重排</button><p class="hint">停机开始前未开工的工单自动移出原时段；已开工检测转异常待处置。提前恢复时可恢复原排程。</p></form>':'<p class="hint">仅业务负责人可登记停机。</p>')+
  '<div class="history-list">'+downtimes.map(d=>'<article><div class="inline-actions"><strong>'+esc(d.instrument?.name||'')+'</strong>'+(d.resolved_at?'<span class="badge">已恢复</span>':'<span class="badge off">停机中</span>')+'</div><span>'+esc(localStamp(d.start_at_local))+' → '+esc(localStamp(d.end_at_local))+'</span><small>'+esc(d.reason)+' · 影响 '+((d.impact||[]).length)+' 个工单</small>'+(!d.resolved_at&&isAdmin()?'<div class="inline-actions"><button class="button small" data-resolve-downtime="'+d.id+'">提前恢复并尝试还原原排程</button></div>':'')+'<ul class="reason-list">'+(d.impact||[]).map(x=>'<li><strong>'+esc(x.order_name)+' · '+(x.action==='abnormal'?'已开工转异常':x.action==='replanned'?'已重排':x.action==='restored'?'已恢复原排程':'无可行时段' )+'</strong><span>原排程 #'+x.schedule_id+(x.new_schedule_id?' → 新排程 #'+x.new_schedule_id:'')+'</span></li>').join('')+'</ul></article>').join('')+(!downtimes.length?'<p class="empty">暂无停机记录。</p>':'')+'</div></section>';
}

export async function recheckPage(id){
 workspace.recheckSelected=Number(id)||workspace.recheckSelected||null;
 const [catalog,shifts,instruments,inspectors,orders,downtimes,allJobs]=await Promise.all([recheck.catalog(),recheck.shifts(),recheck.instruments(),recheck.inspectors(),recheck.orders(),recheck.downtimes(),jobs.list()]);
 workspace.recheckCatalog=catalog;workspace.recheckJobs=allJobs;
 if(!workspace.recheckSelected&&orders[0])workspace.recheckSelected=orders[0].id;
 const detail=workspace.recheckSelected?(await recheck.order(workspace.recheckSelected)):null;
 let datasetMap={};
 for(const j of allJobs.filter(j=>j.status==='completed')){
   if(datasetMap[j.dataset_id])continue;
   const d=await datasets.get(j.dataset_id);
   datasetMap[j.dataset_id]=d.records.map(r=>String(r.batch_id));
 }
 workspace.recheckDatasetMap=datasetMap;
 return heading('05 / RECHECK SCHEDULING','复检排程与领工','从已完成分析选择批次与检验项，按仪器能力、班次、检测时长与制样后有效期安排先制样后检测。确认后整单占用仪器与检验员，停机、中断与重排全程留痕。','<button class="button" id="refresh-recheck">刷新排程 ↻</button>')+
  '<div class="two-columns recheck-top"><section class="panel"><div class="section-title"><h2>复检工单</h2><span>'+orders.length+' 张</span></div>'+orderList(orders)+'</section>'+
  (detail?orderDetail(detail):'<section class="panel"><p class="empty">选择或新建一张复检工单开始排程。</p></section>')+'</div>'+
  '<div class="two-columns recheck-mid">'+(canWrite()?createOrderPanel(allJobs,datasetMap):'')+(detail?'':'')+resourcePanel(shifts,instruments,inspectors)+'</div>'+
  downtimePanel(instruments,downtimes);
}

export function bindRecheck(refresh){
 document.querySelector('#refresh-recheck').onclick=refresh;
 document.querySelectorAll('.tab').forEach(tab=>tab.onclick=()=>{
   document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t===tab));
   document.querySelectorAll('.tab-pane').forEach(p=>p.classList.toggle('hidden',p.dataset.pane!==tab.dataset.tab));
 });
 const jobSelect=document.querySelector('#recheck-order-form [name=job_id]');
 const fillBatches=()=>{
   if(!jobSelect)return;
   const job=(workspace.recheckJobs||[]).find(j=>j.id===Number(jobSelect.value));
   const list=job?workspace.recheckDatasetMap?.[job.dataset_id]||[]:[];
   const select=document.querySelector('#batch-select');
   if(select)select.innerHTML=list.map(b=>'<option value="'+esc(b)+'">'+esc(b)+'</option>').join('');
 };
 if(jobSelect){fillBatches();jobSelect.onchange=fillBatches;}
 if(document.querySelector('#recheck-order-form')){
   submit(document.querySelector('#recheck-order-form'),async f=>{
     const selected=[...document.querySelector('#batch-select').selectedOptions].map(o=>o.value);
     const items=[...document.querySelectorAll('input[name=items]:checked')].map(i=>i.value);
     if(!selected.length)throw new Error('请至少选择一个复检批次');
     if(!items.length)throw new Error('请至少选择一个检验项');
     const order=await recheck.createOrder({name:f.get('name'),job_id:Number(f.get('job_id')),batches:selected,items});
     workspace.recheckSelected=order.id;workspace.recheckPreview=null;notify('复检工单已登记，可预排时段');location.hash='#/recheck/'+order.id;
   });
 }
 document.querySelectorAll('[data-action]').forEach(button=>button.onclick=async()=>{
   const action=button.dataset.action;button.disabled=true;
   try{
     if(action==='preview'){
       const preview=await recheck.preview(workspace.recheckSelected);
       workspace.recheckPreview={orderId:workspace.recheckSelected,data:preview};
       if(preview.feasible)notify('已生成可行方案，请核对后确认');
       else notify('没有可行时段，已列出阻塞原因',true);
       await refresh();
     }else if(action==='claim'){
       const claim=await recheck.claim(workspace.recheckSelected,{note:'现场领工'});
       workspace.recheckClaim=claim;
       notify('已领工，凭领工记录开工（#'+claim.id+'）');
       await refresh();
     }else if(action==='resume'||action==='replan'){
       const result=await recheck.dispose(workspace.recheckSelected,{action});
       notify(action==='resume'?'已恢复检测':'已按当前资源重排');
       workspace.recheckPreview=null;
       await refresh();
     }else if(action==='cancel-order'){
       if(!window.confirm('取消该工单会释放仪器与检验员占用，并作废未开工的领工记录。确认取消？')){button.disabled=false;return;}
       await recheck.cancelOrder(workspace.recheckSelected);
       notify('工单已取消，资源占用已释放');
       workspace.recheckPreview=null;
       await refresh();
     }
   }catch(error){
     if(error.payload?.preview){workspace.recheckPreview={orderId:workspace.recheckSelected,data:error.payload.preview};}
     notify(error.message,true);
     if(error.payload?.replan)await refresh();
     else button.disabled=false;
   }
 });
 const confirmButton=document.querySelector('#confirm-plan');
 if(confirmButton)confirmButton.onclick=async()=>{
   confirmButton.disabled=true;
   const pending=workspace.recheckPending?.orderId===workspace.recheckSelected?workspace.recheckPending:null;
   const key=pending?.key||((crypto.randomUUID&&crypto.randomUUID())||('key-'+Date.now()+'-'+Math.random().toString(16).slice(2)));
   workspace.recheckPending={orderId:workspace.recheckSelected,key};
   try{
     const result=await recheck.confirm(workspace.recheckSelected,{preview_token:workspace.recheckPreview.data.token,idempotency_key:key});
     workspace.recheckPending=null;workspace.recheckPreview=null;
     notify(result.created?'排程已确认，仪器与检验员已整单占用':'该确认已处理，未重复占位');
     await refresh();
   }catch(error){
     if(error.payload?.preview)workspace.recheckPreview={orderId:workspace.recheckSelected,data:error.payload.preview};
     notify(error.message,true);
     await refresh();
   }
 };
 document.querySelectorAll('[data-start-claim]').forEach(button=>button.onclick=async()=>{
   button.disabled=true;
   try{
     const claim=await recheck.startClaim(Number(button.dataset.startClaim),workspace.recheckClaim?.token);
     notify('已开工，检测占用生效');await refresh();
   }catch(error){notify(error.message,true);button.disabled=false;}
 });
 document.querySelectorAll('[data-complete-claim]').forEach(button=>button.onclick=async()=>{
   button.disabled=true;
   try{await recheck.completeClaim(Number(button.dataset.completeClaim));notify('已完工，资源占用释放');await refresh();}
   catch(error){notify(error.message,true);button.disabled=false;}
 });
 document.querySelectorAll('[data-toggle-instrument]').forEach(button=>button.onclick=async()=>{
   try{const r=await recheck.patchInstrument(Number(button.dataset.toggleInstrument),{active:button.dataset.active==='true'});
     notify(r.impact?.length?'资源状态已更新，相关排程已按规则处理':'资源状态已更新');await refresh();}catch(error){notify(error.message,true);}
 });
 document.querySelectorAll('[data-toggle-inspector]').forEach(button=>button.onclick=async()=>{
   try{const r=await recheck.patchInspector(Number(button.dataset.toggleInspector),{active:button.dataset.active==='true'});
     notify(r.impact?.length?'资源状态已更新，相关排程已按规则处理':'资源状态已更新');await refresh();}catch(error){notify(error.message,true);}
 });
 const instrumentForm=document.querySelector('#instrument-form');
 if(instrumentForm)submit(instrumentForm,async f=>{
   const caps=[...instrumentForm.querySelectorAll('input[name^=cap-]:checked')].map(i=>i.name.slice(4));
   if(!caps.length)throw new Error('请至少保留一个检验项能力');
   const minutes={};for(const item of workspace.recheckCatalog.items){const v=Number(f.get('m-'+item.key));if(v>0)minutes[item.key]=v;}
   await recheck.patchInstrument(Number(f.get('id')),{shift_id:Number(f.get('shift_id')),capabilities:caps,item_minutes:minutes,prep_minutes:Number(f.get('prep_minutes')),validity_minutes:Number(f.get('validity_minutes'))});
   notify('仪器配置已保存，排程依据已更新');await refresh();
 });
 const inspectorForm=document.querySelector('#inspector-form');
 if(inspectorForm)submit(inspectorForm,async f=>{
   const caps=[...inspectorForm.querySelectorAll('input[name^=icap-]:checked')].map(i=>i.name.slice(5));
   if(!caps.length)throw new Error('请至少保留一个检验项能力');
   await recheck.patchInspector(Number(f.get('id')),{shift_id:Number(f.get('shift_id')),capabilities:caps});
   notify('检验员配置已保存');await refresh();
 });
 const shiftForm=document.querySelector('#shift-form');
 if(shiftForm)submit(shiftForm,async f=>{
   const toMinute=t=>{const [h,m]=t.split(':').map(Number);return h*60+m;};
   const weekdays=[...shiftForm.querySelectorAll('input[name=wd]:checked')].map(i=>Number(i.value));
   if(!weekdays.length)throw new Error('请至少选择一个星期');
   await recheck.createShift({name:f.get('name'),start_minute:toMinute(f.get('start')),end_minute:toMinute(f.get('end')),weekdays});
   notify('班次已新增');await refresh();
 });
 const downtimeForm=document.querySelector('#downtime-form');
 if(downtimeForm)submit(downtimeForm,async f=>{
   const offset=workspace.recheckCatalog?.timezone_offset_minutes??480;
   const start=localInputToEpoch(f.get('start'),offset),end=localInputToEpoch(f.get('end'),offset);
   if(!start||!end||end<=start)throw new Error('停机结束时间必须晚于开始时间');
   const d=await recheck.downtime({instrument_id:Number(f.get('instrument_id')),start_at:start,end_at:end,reason:f.get('reason')});
   notify('停机已登记：影响 '+((d.impact||[]).length)+' 个工单');
   await refresh();
 });
 document.querySelectorAll('[data-resolve-downtime]').forEach(button=>button.onclick=async()=>{
   button.disabled=true;
   try{const r=await recheck.resolveDowntime(Number(button.dataset.resolveDowntime));notify('停机已恢复，还原原排程 '+r.restored_count+' 个');await refresh();}
   catch(error){notify(error.message,true);button.disabled=false;}
 });
}
