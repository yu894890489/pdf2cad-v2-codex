const $=id=>document.getElementById(id),el=(tag,text,parent)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(parent)parent.append(e);return e;};
const fmt=v=>v==null?'待定':v.toFixed(2),names={difference:'尺寸差异',within_diagnostic_bound:'阈值内相容',pending:'待定',input_reused:'输入复用',blank_reference:'板坯参考'},kinds={member_bom:'杆件全长 / BOM',member_initial_audit:'杆件全长 / 初审换算',member_annotation:'杆件全长 / 杆旁标注',node_span:'模型参考轴线交点跨度',member_section:'角钢截面',plate_envelope:'板件包络',plate_thickness:'板厚',hole_diameter:'孔径',hole_distance:'孔中心距'};
let report;
async function read(url){const r=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(20000)});if(!r.ok)throw Error(`${url}：HTTP ${r.status}`);return r;}
function selected(r){return (!$('view').value||r.view_id===$('view').value)&&(!$('search').value||JSON.stringify([r.mark,r.object_id,r.label,r.view_title]).includes($('search').value.trim()));}
function evidence(r,cell){
  el('div',r.note,cell);
  const d=r.dependency;
  if(d.reference_used_in_scale.length)el('div','该尺寸参与本面比例标定；数值接近属于共用依据。',cell);
  if(d.bom_scale_reused)el('div','本面比例使用了 BOM，不能作为独立验证。',cell);
  if(r.geometry?.uses_axis_extension)el('div','使用参考轴线延长线交点，非实体接触点。',cell);
  if(r.candidate_check)el('div',`初审建议 ${fmt(r.candidate_check.reference_mm)}；模型差值 ${fmt(r.candidate_check.delta_mm)} mm。${r.candidate_note}`,cell);
  const details=el('details',undefined,cell);el('summary','查看记录、基准与标定来源',details);el('pre',JSON.stringify(r,null,2),details);
}
function render(){
  const rows=report.rows.filter(selected);$('full-lengths').replaceChildren();
  for(const r of rows.filter(r=>r.kind==='member_initial_audit'&&r.reference_mm!=null)){
    const tr=el('tr',undefined,$('full-lengths'));for(const s of [`${r.view_title} / ${r.mark}`,fmt(r.measured_mm),fmt(r.reference_mm),fmt(r.bom_mm),fmt(r.delta_mm),`${r.formula||''}；${names[r.status]}`])el('td',s,tr);
    tr.lastChild.className=r.status;
  }
  if(!$('full-lengths').children.length){const td=el('td','本筛选范围没有可完成全长比对的换算记录。',el('tr',undefined,$('full-lengths')));td.colSpan=6;}
  $('findings').replaceChildren();
  for(const finding of report.initial_findings.filter(f=>!$('view').value||f.view_id===$('view').value)){
    const card=el('div',undefined,$('findings'));card.className='finding';el('strong',`${report.views.find(v=>v.view_id===finding.view_id)?.title||finding.view_id} · ${finding.summary}`,card);
    const matches=rows.filter(r=>r.initial_finding_id===finding.id);
    for(const r of matches)el('p',`模型参考轴线跨度 ${fmt(r.measured_mm)} mm；原标注 ${fmt(r.reference_mm)} mm；初审建议 ${fmt(r.candidate_check.reference_mm)} mm。${r.candidate_note}`,card);
    if(!matches.length)el('p','保留初审疑点；本项没有可据模型单独裁定的尺寸对应。',card);
  }
  if(!$('findings').children.length)el('p','本面没有原初审单列的疑点；这不表示所有尺寸均已通过。',$('findings'));
  $('rows').replaceChildren();const filtered=rows.filter(r=>(!$('kind').value||r.kind===$('kind').value)&&(!$('status').value||r.status===$('status').value));$('row-count').textContent=`${filtered.length} 条观察记录`;
  for(const r of filtered){const tr=el('tr',undefined,$('rows'));const first=el('td',undefined,tr);el('strong',r.view_title,first);el('div',r.label+(r.kind==='node_span'&&r.mark?` / ${r.mark}`:''),first);
    for(const value of [r.measured_mm,r.reference_mm,r.delta_mm])el('td',fmt(value),tr);
    el('td',names[r.status]||r.status,tr).className=r.status;evidence(r,el('td',undefined,tr));
  }
}
async function main(){
  report=await (await read('report.json')).json();
  await Promise.all(Object.entries(report.input_files).map(async([name,expected])=>{const bytes=await (await read('../'+name)).arrayBuffer();const actual=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');if(actual!==expected)throw Error('模型或原初审已更新，此比对已过期。请运行 model-audit 重新生成。');}));
  $('title').textContent=report.title;$('summary').textContent='已加载 · 当前模型与原初审均保持原状 · 需逐面复核';$('tolerance').textContent=report.tolerance_note;
  const members=report.rows.filter(r=>r.kind==='member_bom'&&r.measured_mm!=null),complete=report.rows.filter(r=>r.kind==='member_initial_audit'&&r.reference_mm!=null);
  for(const [value,label] of [[members.length,'杆件模型观察'],[members.filter(r=>r.status==='difference').length,'杆长与 BOM 有差异'],[complete.length,'可比初审全长换算'],[report.counts.pending||0,'依据待定记录']]){const box=el('div',undefined,$('metrics'));box.className='metric';el('strong',String(value),box);el('span',label,box);}
  for(const v of report.views){const o=el('option',v.title,$('view'));o.value=v.view_id;}
  for(const [key,label] of Object.entries(kinds)){const o=el('option',label,$('kind'));o.value=key;}
  report.limitations.forEach(s=>el('li',s,$('limitations')));
  for(const id of ['view','kind','status'])$(id).addEventListener('change',render);$('search').addEventListener('input',render);render();window.modelAuditReady=true;
}
main().catch(error=>{$('summary').textContent='尺寸复核未加载';$('error').hidden=false;$('error').textContent=error.message+'\n请确认本地服务和报告文件，修复后刷新页面。';});
