import * as THREE from 'three';
import {OrbitControls} from '/assembly-assets/OrbitControls.js';
import {MemberLabels} from '/assembly-assets/member-labels.js';
import {renderDrawingActions} from '/assembly-assets/drawing-actions.js';

const $=id=>document.getElementById(id), aid=location.pathname.split('/')[2];
const staticMode=window.ASSEMBLY_STATIC===true;
let data, session, selected, selectedMemberKey=null, busy=false, timer, view, preview=null;
let drawingSource=null,drawingRequest=0;
const drawingJobs=new Map();
const faceMap=()=>new Map(data.faces.map(f=>[f.id,f]));
const edgeMap=()=>new Map(data.faces.flatMap(f=>f.edges.map(e=>[e.id,e])));
const n=(tag,text,parent,cls)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;parent?.append(el);return el;};
const fmt=v=>Number.isFinite(v)?v.toFixed(1):'未定';
const fail=message=>{$('error').hidden=!message;$('error').textContent=message||'';};
const missingSelectionKey=`assembly:${aid}:missing-face`;
function savedMissingSelection(){try{return localStorage.getItem(missingSelectionKey);}catch{return null;}}
function saveMissingSelection(value){try{if(value)localStorage.setItem(missingSelectionKey,value);}catch{}}
async function request(url,options={}){
  if(staticMode){if(url==='/api/session')return {};if(url===`/api/assemblies/${aid}`&&!options.method)url='assembly.json';else throw Error('这是自动处理的保存结果，请在本地工作台的装配项目中编辑。');}
  const {timeout=60000,...fetchOptions}=options;
  const response=await fetch(url,{...fetchOptions,signal:AbortSignal.timeout(timeout)});
  const result=await response.json();
  if(!response.ok){if(response.status===409)await reload();throw Error(result.error||`请求失败 ${response.status}`);}
  return result;
}
async function act(action,values={}){
  if(staticMode)return;
  if(busy)return;view.cancelDrag();cancelPreview();busy=true;fail('');updateActionAvailability();
  $('save-state').textContent=action==='automatic_pipeline'?'正在装配、反算补全并复核尺寸…':action==='auto'?'正在匹配公共件号并尝试装配…':action==='fit_taper'?'正在联合剖面校正收口…':action==='review_missing_face'?'正在反算缺失面并回查原图…':'正在保存…';
  if(action==='auto'){$('auto').textContent='正在尝试装配…';$('auto-status').textContent='正在搜索完整四面环；有尖端方向依据时继续计算局部收口，请稍候。';$('auto-status').hidden=false;}
  try{accept(await request(`/api/assemblies/${aid}`,{method:'POST',timeout:['fit_taper','auto','automatic_pipeline'].includes(action)?180000:60000,headers:{'Content-Type':'application/json','X-Upload-Token':session.token},body:JSON.stringify({revision:data.revision,action,...values})}));if(action==='fit_taper'||action==='automatic_pipeline'||(action==='auto'&&data.auto_attempt?.changed)){choose(data.scene.front_face_id);view.fitConnected(selected);}if(action==='review_missing_face'){choose(values.face_id);if(data.missing_face_review?.correction?.status==='applied_candidate')view.fitConnected(selected);else view.fitMissing();}}
  catch(e){fail(e.message);$('save-state').textContent='操作未保存';if(action==='auto'){$('auto-status').textContent=`装配未完成：${e.message}`;$('auto-status').hidden=false;}if(data){view.applyPoses(data.scene.poses);renderSelection();}}
  finally{busy=false;$('auto').textContent='按公共件号尝试装配';updateActionAvailability();}
}
function updateActionAvailability(){
  for(const id of ['auto','automatic-pipeline','arrange','preview-taper','apply-taper'])$(id).disabled=busy||staticMode;
  const options=data?.missing_face_options||data?.missing_face_candidates?.map(o=>({...o,ready:true}))||[];
  $('review-missing-face').disabled=busy||staticMode||!options.some(o=>o.face_id===$('missing-face').value&&o.ready);
  updateInteraction();
}
function accept(next){
  cancelPreview();
  const rebuild=!data||data.input_digest!==next.input_digest||data.geometry_revision!==next.geometry_revision;
  data=next;window.assemblyData=data;
  if(!data.faces.some(f=>f.id===selected))selected=data.scene.front_face_id||data.faces[0]?.id;
  if(rebuild)view.build(data);else view.applyPoses(data.scene.poses);
  render();view.highlight();
  $('loading').hidden=true;window.assemblyReady=true;
  $('save-state').textContent=staticMode?'自动处理结果 · 查看模式':`已保存 · 修订 ${data.revision}${data.waiting?' · 等待图纸解析完成':''}`;
  if(staticMode){for(const id of ['add','add-existing','front','apply-pose','apply-scale','undo','redo','import','connect','preview-connect'])$(id).disabled=true;for(const button of $('links').querySelectorAll('button'))button.disabled=true;for(const check of $('faces').querySelectorAll('input'))check.disabled=true;}
  if(rebuild){if(data.scene.links.length&&selected)view.fitConnected(selected);else view.fit();}
}
async function reload(){const next=await request(`/api/assemblies/${aid}`);if(view.drag)return;if(!data||next.revision!==data.revision||next.input_digest!==data.input_digest)accept(next);}
async function poll(){if(staticMode)return;clearTimeout(timer);try{if(!busy&&!view.drag&&!preview)await reload();}catch(e){fail(`读取装配失败：${e.message}`);}timer=setTimeout(poll,data?.waiting?2500:12000);}
function choose(fid){if(selected!==fid)selectedMemberKey=null;selected=fid;const face=faceMap().get(fid);if(face)drawingSource=face.source_id;renderDrawingToolbar();renderSelection();renderFaces();view.highlight();}
async function renderDrawingToolbar(){
  const sources=data.sources,select=$('drawing-source');
  if(!sources.some(s=>s.id===drawingSource))drawingSource=faceMap().get(selected)?.source_id||sources[0]?.id;
  if([...select.options].map(o=>o.value).join('|')!==sources.map(s=>s.id).join('|')){
    select.replaceChildren();for(const source of sources){const option=n('option',source.name,select);option.value=source.id;}
  }
  select.value=drawingSource||'';
  const source=sources.find(s=>s.id===drawingSource),sequence=++drawingRequest;
  if(!source){renderDrawingActions($('drawing-actions'),null);$('drawing-status').textContent='等待图纸载入';return;}
  const hosted=staticMode&&location.pathname.match(/^\/results\/([a-f0-9]{32})\/assembly\//);
  const jobId=hosted?hosted[1]:source.id;
  const cached=drawingJobs.get(jobId)||(!staticMode&&source.links?{job:source}:null);
  const draw=job=>{
    renderDrawingActions($('drawing-actions'),job,{onRetry:staticMode?null:retryDrawing});
    $('drawing-status').textContent=job?job.error||(['running','queued'].includes(job.status)?'正在处理；结果生成后自动更新。':'按钮对应当前图纸；选择其他结构面会同步切换。'):'正在读取处理结果…';
    $('drawing-actions').dataset.sourceId=source.id;
  };
  draw(cached?.job);
  if(cached&&!['queued','running'].includes(cached.job.status))return;
  try{
    let job;
    if(staticMode&&!hosted){
      const response=await fetch('drawing-links.json');
      job=response.ok?await response.json():{name:source.name,status:source.status,links:{source:source.source_url}};
    }else{
      const response=await fetch(`/api/jobs/${jobId}`,{signal:AbortSignal.timeout(60000)});
      if(!response.ok)throw Error(`读取图纸结果失败（HTTP ${response.status}）`);
      job=await response.json();
    }
    drawingJobs.set(jobId,{job});if(sequence===drawingRequest)draw(job);
  }catch(e){if(sequence===drawingRequest)$('drawing-status').textContent=e.message;}
}
async function retryDrawing(job){
  try{
    const next=await request(`/api/jobs/${job.id}/retry`,{method:'POST',headers:{'X-Upload-Token':session.token}});
    $('drawing-status').textContent=`已创建重新处理任务：${next.name}。`;
    const link=n('a','查看处理进度',$('drawing-status'));link.href='/';link.target='_blank';link.rel='noopener';
  }catch(e){$('drawing-status').textContent=`重新处理失败：${e.message}`;}
}
$('drawing-source').onchange=()=>{
  drawingSource=$('drawing-source').value;
  const root=data.scene.source_fronts?.[drawingSource];
  const face=data.faces.find(f=>f.id===root)||data.faces.find(f=>f.source_id===drawingSource);
  if(face)choose(face.id);else renderDrawingToolbar();
};
function chooseMember(item){choose(item.faceId);selectedMemberKey=item.key;renderMemberSelection();view.highlight();}
function renderMemberSelection(){
  const item=view.memberLabels.items.find(item=>item.key===selectedMemberKey);$('member-info').hidden=!item;
  if(!item)return;
  const m=item.record,f=faceMap().get(item.faceId);
  $('member-title').textContent=`件号 ${item.text}`;
  $('member-detail').textContent=`${m.specification} · BOM 长 ${fmt(m.bom_length_mm)} mm · 当前模型轴长 ${fmt(item.axisLength)} ${f.units==='mm'?'mm':'pt'}。来源：${f.source_name} / ${f.title}`;
  if(m.correction){const c=m.correction;$('member-detail').textContent+=` 原识别 ${fmt(c.original_length_mm)} mm；补长 ${fmt(c.added_mm)} mm，截去 ${fmt(c.trimmed_mm)} mm；修正后与 BOM 差 ${fmt(c.bom_difference_mm)} mm。修正尺寸来自邻面约束，仍待核对。`;}
}
function edgeLabel(e){const f=faceMap().get(e.face_id);return `${f.title} / ${e.mark?'件 '+e.mark:'推定边界'} / ${fmt(e.length)} ${f.units==='mm'?'mm':'pt'}`;}
function selectMatch(match){cancelPreview();let {a,b}=match;if(edgeMap().get(b).face_id===data.scene.front_face_id)[a,b]=[b,a];$('edge-a').value=a;$('edge-b').value=b;edgeInfo();view.highlight();}
function renderFaces(){
  $('faces').replaceChildren();$('face-count').textContent=`${data.faces.length} 面`;
  for(const f of data.faces){
    const row=n('div',undefined,$('faces'),'face-row'+(f.id===selected?' selected':''));
    const check=n('input',undefined,row);check.type='checkbox';check.disabled=staticMode;check.checked=data.scene.active_face_ids.includes(f.id);check.title='参与闭合检查';check.onchange=()=>act('active',{face_id:f.id,enabled:check.checked});
    const button=n('button',f.title,row);button.onclick=()=>choose(f.id);
    n('small',`${f.source_name} · ${f.members.length} 杆件${f.units!=='mm'?' · 比例未定':''}${f.id===data.scene.front_face_id?' · 正视参考':''}${f.correction?' · 已反算修正':''}`,button);
  }
}
function renderSelection(){
  renderMemberSelection();
  const f=faceMap().get(selected);if(!f){$('selected-title').textContent='尚无可用结构面';$('face-info').textContent=data.waiting?'正在等待图纸解析完成。':'请检查图纸处理结果或补充图纸。';$('crop').hidden=true;$('source-image').removeAttribute('href');for(const id of ['px','py','pz','rx','ry','rz','scale'])$(id).value='';return;}
  $('selected-title').textContent=f.title;
  $('face-info').textContent=`${f.source_name} · 第 ${f.page} 页 · ${f.members.length} 根杆件观察 / ${f.plates.filter(p=>p.polygon).length} 个板形 / ${f.holes.length} 个孔位。${f.units!=='mm'?'比例未定：仅放大展示原线条，不参与装配。连接前请确定实际比例。':''}`;
  $('source-image').href=f.image_url;$('crop').src=f.image_url;$('crop').hidden=false;
  $('crop').onerror=()=>{$('crop').hidden=true;};
  const pose=data.scene.poses[f.id],euler=new THREE.Euler().setFromQuaternion(new THREE.Quaternion().fromArray(pose.quaternion),'XYZ');
  ['px','py','pz'].forEach((id,i)=>$(id).value=pose.position[i].toFixed(2));
  ['rx','ry','rz'].forEach((id,i)=>$(id).value=THREE.MathUtils.radToDeg([euler.x,euler.y,euler.z][i]).toFixed(3));
  $('scale').value=f.scale.mm_per_pt??'';
}
function render(){
  renderDrawingToolbar();
  renderAutomaticPipeline();
  $('title').textContent=data.title;$('assessment').textContent=data.assessment.message;
  $('assessment').className=data.assessment.watertight?'good':'warn';
  $('metrics').replaceChildren();
  for(const label of [`${data.assessment.active_faces} 个参与面`,`${data.assessment.located_face_count||0} 个已连接面`,`${data.assessment.open_edges.length} 条未闭合边`,`${data.assessment.intersections.length} 处推定边界穿插/重叠`])n('span',label,$('metrics'),'metric');
  if(data.assessment.pending_connection_count)n('span',`${data.assessment.pending_connection_count} 条接缝待复核`,$('metrics'),'metric warn');
  if(data.assessment.corrected_face_count)n('span',`${data.assessment.corrected_face_count} 个反算修正面`,$('metrics'),'metric warn');
  if(data.scene.taper_rule)$('tip-direction').value=data.scene.taper_rule.tip_direction;
  else if(!$('tip-direction').value&&data.tip_direction_hint)$('tip-direction').value=data.tip_direction_hint;
  const attempt=data.auto_attempt;$('auto-status').hidden=!attempt;$('auto-status').textContent=attempt?.message||'';
  const taper=data.assessment.parallel_relations.find(p=>p.constraint_kind==='taper');
  $('taper-status').textContent=taper?taperText(taper):'尚未校正。请按原图指定尖端方向，可先预览；应用后支持撤销。';
  if(data.auto_result?.search_limited)n('p','候选过多，自动搜索达到上限，请指定正视面或手动连接。',$('metrics'),'warn');
  if(data.waiting)n('p','图纸处理完成后会自动载入；已有的手动位置会保留。',$('metrics'),'note');
  $('sources').replaceChildren();$('source-count').textContent=`${data.sources.length} 张`;
  for(const s of data.sources){const div=n('div',undefined,$('sources'),'source');const a=n('a',s.name,div);a.href=s.source_url;a.target='_blank';n('small',s.error||(s.model_available?'已载入结构面':`图纸状态：${s.status} · 暂无可用模型`),div,s.error?'warn':'');}
  if(data.duplicate_jobs.length)n('p',`${data.duplicate_jobs.length} 个相同 PDF 已合并，避免重复计入面。`,$('sources'),'note');
  renderFaces();renderSelection();
  renderReview();renderMissingFace();$('undo').disabled=!data.history?.undo;$('redo').disabled=!data.history?.redo;
  for(const id of ['edge-a','edge-b']){
    const saved=$(id).value;$(id).replaceChildren();const empty=n('option','请选择一条边',$(id));empty.value='';
    for(const f of data.faces){const group=n('optgroup',undefined,$(id));group.label=f.title;for(const e of f.edges){const opt=n('option',edgeLabel(e),group);opt.value=e.id;}}
    $(id).value=saved;
  }
  $('links').replaceChildren();
  for(const link of data.assessment.links){
    const div=n('div',undefined,$('links'),'connection'),edges=edgeMap();
    n('div',`${edgeLabel(edges.get(link.a))} ↔ ${edgeLabel(edges.get(link.b))}`,div);
    n('small',`${fmt(link.angle_deg)}° · 最大端点间隙 ${fmt(link.endpoint_gap_mm)} mm · 角度误差 ${fmt(link.angle_error_deg)}°`,div,link.valid?'good':'warn');
    const button=n('button','解除连接',div);button.disabled=staticMode;button.onclick=()=>act('unlink',{link_id:link.id});
  }
  if(!data.assessment.links.length)n('p','尚未指定边连接。',$('links'),'note');
  $('constraints').replaceChildren();
  for(const pair of data.assessment.parallel_relations){
    const connected=view.component(pair.a).has(pair.b)||data.assessment.watertight;
    n('p',pair.constraint_kind==='taper'?taperText(pair):connected?`前后面关系：${pair.satisfied?'平行且分离':'待调整'}，平行误差 ${fmt(pair.parallel_error_deg)}°；面间距 ${fmt(pair.plane_distance_mm)} mm（待按原图核对）。`:'前后面尚未建立连接路径；当前排列位置不能作为实际间距依据。',$('constraints'),'constraint '+(connected&&pair.satisfied?'good':'warn'));
  }
  for(const issue of data.assessment.intersections)n('p',`${faceMap().get(issue.a).title} 与 ${faceMap().get(issue.b).title} 存在${issue.kind==='coplanar_overlap'?'共面重叠':'穿插'}。`,$('constraints'),'constraint warn');
  $('matches').replaceChildren();$('match-count').textContent=`公共边候选 ${data.matches.length} 对`;
  for(const match of data.matches.slice(0,100)){
    const edges=edgeMap(),label=match.automatic_candidate?'公共边界杆件':match.shared_member_candidate?'共享定位杆件（不作为闭合边界）':match.kind==='mark_spec_length'?'同件号但轴长与 BOM 待核对':'仅长度接近，需人工确认';
    const button=n('button',`${label}：${edgeLabel(edges.get(match.a))} ↔ ${edgeLabel(edges.get(match.b))}`,$('matches'),'candidate');
    button.onclick=()=>selectMatch(match);
  }
  if(data.matches.length>100)n('p','仅列前 100 对；可在选边框中选择所有边。',$('matches'),'note');
  edgeInfo();view.showOpenEdges();
}
function renderAutomaticPipeline(){
  const p=data.automatic_pipeline,box=$('automatic-pipeline-details');box.replaceChildren();
  $('automatic-pipeline-status').textContent=p?.message||'上传新图纸后自动执行。已有手动连接会保留；也可点击按钮对尚未定位的面反算补全。';
  if(!p)return;
  if(p.source_results){
    const groups=n('div',undefined,box);
    for(const group of [...p.source_results].sort((a,b)=>b.counts.located_face_count-a.counts.located_face_count)){
      const details=n('details',undefined,groups);
      n('summary',`${group.name}：连接 ${group.counts.located_face_count} 面，补全 ${group.counts.corrected_face_count} 面`,details);
      if(group.front_face_id){const button=n('button',group.counts.located_face_count?'查看此图装配':'查看此图正视面',details,'candidate');button.onclick=()=>{choose(group.front_face_id);view.fitConnected(group.front_face_id);};}
      for(const attempt of group.attempts||[])if(attempt.reason)n('p',attempt.reason,details,'warn');
    }
  }else for(const attempt of p.attempts||[])if(attempt.reason)n('p',attempt.reason,box,'warn');
  const blocked=n('details',undefined,box);n('summary',`${p.unplaced_faces.length} 个面尚未定位`,blocked);
  for(const row of p.unplaced_faces)n('p',`${row.title}：${row.reason}`,blocked,'note');
  const changed=n('details',undefined,box);n('summary',`${p.completion_reports.length} 个面的反算记录`,changed);
  for(const r of p.completion_reports){n('p',`${r.title}：${r.correction?.status==='applied_candidate'?'已修正并放置':r.correction?.message||'待复核'}`,changed,'note');}
  const dimensions=n('details',undefined,box);n('summary',`${p.counts.bom_differences} 条杆长与 BOM 差异`,dimensions);
  for(const row of p.dimensions.filter(d=>d.status==='difference').sort((a,b)=>Math.abs(b.difference_mm/b.bom_length_mm)-Math.abs(a.difference_mm/a.bom_length_mm)).slice(0,30)){
    const f=faceMap().get(row.face_id);const button=n('button',`${f?.title||''} / 件 ${row.mark}：模型 ${fmt(row.model_length_mm)}，BOM ${fmt(row.bom_length_mm)} mm`,dimensions,'candidate');button.onclick=()=>{choose(row.face_id);view.fit(row.face_id);};
  }
}
function edgeInfo(){const es=edgeMap(),a=es.get($('edge-a').value),b=es.get($('edge-b').value);$('edge-info').textContent=a&&b?`边长差 ${fmt(Math.abs(a.length-b.length))}；按中点刚性对齐，不拉伸杆件。${a.basis.startsWith('inferred')||b.basis.startsWith('inferred')?'含推定边界，须核对原图。':''}`:'';}

function renderMissingFace(){
  const report=data.missing_face_review,select=$('missing-face'),saved=select.value||savedMissingSelection();
  const readyIds=new Set((data.missing_face_candidates||[]).map(o=>o.face_id));
  const options=data.missing_face_options||data.faces.filter(f=>!['schematic','detail'].includes(f.role)).map(f=>({face_id:f.id,title:f.title,ready:readyIds.has(f.id),shared_marks:[],reason:'需要已连接邻面提供两个独立公共杆件。'}));
  select.replaceChildren();
  for(const c of options){const o=n('option',`${c.title} · ${c.ready?'可反算':'待连接 / 校核'}`,select);o.value=c.face_id;o.title=c.reason;}
  const preferred=[saved,data.missing_face_selected_id,report?.target_face_id].find(fid=>options.some(c=>c.face_id===fid));
  if(preferred)select.value=preferred;
  if(!options.length){const o=n('option','尚无可用结构面，请检查图纸处理结果',select);o.value='';}
  saveMissingSelection(select.value);updateActionAvailability();
  const chosen=options.find(o=>o.face_id===select.value);
  $('missing-face-status').textContent=!chosen?'尚无可用结构面，请检查图纸处理结果。':!chosen.ready?chosen.reason:report?.target_face_id===chosen.face_id?`${report.title}：${report.diagnosis==='recognition_missing_span_supported'?'原图线条支持识别漏段':'现有证据不足'}；尚未确认闭合。`:'该面满足反算条件，点击“反算并回查原图”开始检查。';
  if(data.missing_face_review_stale)$('missing-face-status').textContent+=' 上次回查依据已变化，需建立连接后重新计算。';
  $('missing-face-result').hidden=!report;view.showMissingFace(report);if(!report)return;
  const p=report.prediction,box=$('missing-face-dimensions');box.replaceChildren();
  n('strong',`反算两侧长：${fmt(p.edge_lengths_mm[0])} / ${fmt(p.edge_lengths_mm[2])} mm`,box);
  n('p',`反算窄端 / 宽端：${p.widths_mm.slice().sort((a,b)=>a-b).map(fmt).join(' / ')} mm。离最佳平面最大 ${fmt(p.planarity_max_mm)} mm，候选面与地面夹角 ${fmt(p.tilt_to_ground_deg)}°。`,box);
  if(report.native_candidate_outline)n('p',`原图外包轴候选窄端 / 宽端：${report.native_candidate_outline.widths_mm.slice().sort((a,b)=>a-b).map(fmt).join(' / ')} mm。横向端边均为推断。`,box,'warn');
  const c=report.correction,applied=c?.status==='applied_candidate';
  if(applied){
    $('missing-face-status').textContent=`${report.title}：整面 ${c.member_count} 根杆件已修正并放置，支持撤销。`;
    n('p',`${c.extended_members} 根补长、${c.trimmed_members} 根截短（同一根可同时发生）。公共边最大间隙 ${fmt(c.max_endpoint_gap_mm)} mm。${c.bom_conflicts} 根杆件与 BOM 仍有差异。对齐结果来自轮廓约束，不作为独立尺寸验证。`,box,'warn');
  }else if(c?.status==='blocked')n('p',`未应用模型修正：${c.message}`,box,'warn');
  const table=$('missing-face-comparisons');table.replaceChildren();n('caption','单位 mm · 原图外包轴端仍待核对',table);
  const header=n('tr',undefined,n('thead',undefined,table));n('th','依据',header);
  for(const row of report.comparisons)n('th',`件 ${row.mark}`,header);
  const body=n('tbody',undefined,table);
  for(const [label,read] of [['BOM',r=>r.bom_length_mm],['原始识别',r=>r.recognized_length_mm],['已拼邻面轴',r=>r.neighbor_length_mm],['原图外包轴候选',r=>r.native_evidence?.candidate_length_mm],...(applied?[['修正后模型轴',r=>c.members.find(m=>m.member_id===r.member_id)?.corrected_length_mm]]:[])]){
    const tr=n('tr',undefined,body);n('th',label,tr);for(const row of report.comparisons)n('td',fmt(read(row)),tr);
  }
  const issues=$('missing-face-issues');issues.replaceChildren();
  n('p',`刚性试拼最大间隙：原识别 ${fmt(report.original_fit.max_gap_mm)} mm${report.native_candidate_fit?` → 补段候选 ${fmt(report.native_candidate_fit.max_gap_mm)} mm`:''}。允许间隙 3 mm。`,issues,'warn');
  const details=n('details',undefined,issues);n('summary',`${report.issues.length} 项依据与疑点`,details);
  for(const message of report.issues)n('p',message,details,'note');
  if(applied){
    const d=n('details',undefined,issues);n('summary',`${c.member_count} 根杆件的长度修正（mm）`,d);
    n('p','颜色按原识别长度在修正轴上的区间分段；橙色包括轮廓调整与推断补段，红色虚线不计入实体模型。原始端点和推断依据保存在回查记录中。',d,'note');
    const scroll=n('div',undefined,d,'table-scroll'),t=n('table',undefined,scroll),h=n('tr',undefined,t);
    for(const label of ['件号','原识别','修正后','补长','截去','BOM 差'])n('th',label,h);
    for(const m of c.members){const tr=n('tr',undefined,t);for(const v of [m.mark,...[m.original_length_mm,m.corrected_length_mm,m.added_mm,m.trimmed_mm,m.bom_difference_mm].map(fmt)])n('td',v,tr);}
  }
  const suffix=`?basis=${encodeURIComponent(report.artifact_key||report.basis_signature)}`;
  $('missing-face-source').hidden=!report.source_overlay_available;
  const artifactBase=staticMode?report.artifact_base:`/assemblies/${aid}/`;
  if(report.source_overlay_available){const src=`${artifactBase}missing-face-source.svg${suffix}`;$('missing-face-source').href=src;if($('missing-face-crop').getAttribute('src')!==src)$('missing-face-crop').src=src;}
  $('missing-face-report').href=`${artifactBase}missing-face-review.json${suffix}`;
  $('missing-face-corrected').hidden=!applied;$('missing-face-corrected').href=`${artifactBase}missing-face-corrected.svg${suffix}`;
}

function renderReview(){
  const r=data.review;$('review-summary').replaceChildren();$('blockers').replaceChildren();$('bridge-conflicts').replaceChildren();$('length-conflicts').replaceChildren();$('axis-overlaps').replaceChildren();if(!r)return;
  n('p',`${r.shared_member_count} 对公共杆件 · ${r.boundary_match_count} 对可用于外边界搜索`,$('review-summary'),'note');
  for(const issue of r.blockers)n('p',issue.message,$('blockers'),'constraint warn');
  for(const match of data.matches.filter(m=>m.shared_member_candidate)){
    const edge=edgeMap().get(match.a),connected=data.scene.links.some(link=>[link.a,link.b].includes(match.a)&&[link.a,link.b].includes(match.b));
    const button=n('button',connected?`已连接件 ${edge.mark} · 聚焦连接组`:`预览公共杆件 ${edge.mark} 的连接`,$('review-summary'),'candidate');button.dataset.matchId=match.id;button.onclick=()=>{if(connected){cancelPreview();choose(edge.face_id);view.fitConnected(edge.face_id);}else{selectMatch(match);previewConnection();}};
  }
  for(const pair of r.parallel_bridges){
    const box=n('div',undefined,$('bridge-conflicts'),'constraint '+(pair.compatible_with_parallel_faces?'good':'warn'));
    n('strong',`${faceMap().get(pair.via).title} · 件号 ${pair.marks.join(' / ')}`,box);
    n('p',pair.constraint_kind==='taper'?`两轴夹角 ${pair.axis_angle_deg.toFixed(2)}°，按本项目的横向收口约束联合定位。接缝误差见“连接与约束”，仍须核对原图。`:`两轴夹角 ${pair.axis_angle_deg.toFixed(2)}°；前后面平行时，同一横向面的交线也应平行（0° ± ${pair.tolerance_deg}°）。${pair.compatible_with_parallel_faces?'本项相容。':'存在冲突，先核对收分、剖面表达或杆件轴线。'}`,box);
    const button=n('button','查看该剖面',box);button.onclick=()=>{choose(pair.via);view.fit(pair.via);};
  }
  $('length-count').textContent=`杆长与 BOM：${r.length_conflicts.length} 条待核对`;
  for(const row of r.length_conflicts){const button=n('button',`${faceMap().get(row.face_id).title} / 件 ${row.mark}：轴长 ${fmt(row.observed_length_mm)}，BOM ${fmt(row.bom_length_mm)}，差 ${fmt(row.difference_mm)} mm`,$('length-conflicts'),'candidate');button.onclick=()=>{choose(row.face_id);view.fit(row.face_id);};}
  $('overlap-count').textContent=`重合杆件轴线：${r.overlapping_axes.length} 对待核对`;
  for(const row of r.overlapping_axes){const button=n('button',`${faceMap().get(row.face_id).title} / 件 ${row.marks.join('、')} 共用同一轴线，需核对前后重叠投影或错配。`,$('axis-overlaps'),'candidate');button.onclick=()=>{choose(row.face_id);view.fit(row.face_id);};}
}
function connectionValues(){return {a:$('edge-a').value,b:$('edge-b').value,angle_deg:Number($('angle').value),flip:$('flip').checked};}
function taperText(pair){const t=pair.taper;return `参考杆件 ${t.reference_marks.join(' / ')} 的共同纵向区间：尖端侧宽 ${fmt(t.tip_width_mm)} mm → 根部侧宽 ${fmt(t.root_width_mm)} mm。${!pair.group_connected?'连接组已拆开，请重新校正。':t.narrowing?'朝尖端收窄。':'收口方向异常。'}${pair.satisfied?'接缝通过几何阈值，仍需按原图复核。':'接缝尺寸仍待复核。'}`;}
async function previewTaper(){
  if(busy)return;cancelPreview();busy=true;updateActionAvailability();fail('');$('save-state').textContent='正在联合剖面预览收口…';
  try{
    preview=await request(`/api/assemblies/${aid}`,{method:'POST',timeout:180000,headers:{'Content-Type':'application/json','X-Upload-Token':session.token},body:JSON.stringify({revision:data.revision,action:'preview_taper',tip_direction:$('tip-direction').value})});
    view.applyPoses(preview.scene.poses);view.showOpenEdges(preview.assessment);choose(data.scene.front_face_id);view.fitConnected(selected,preview.scene);
    $('preview-message').textContent='预览尚未保存：'+taperText(preview.assessment.parallel_relations.find(p=>p.constraint_kind==='taper'))+' 点击“应用收口校正”保存。';
    $('preview-message').hidden=false;$('cancel-preview').hidden=false;$('save-state').textContent='收口预览 · 尚未保存';
  }catch(e){fail(e.message);$('save-state').textContent='未更改装配';}finally{busy=false;updateActionAvailability();}
}
function cancelPreview(){
  if(preview&&data){view.applyPoses(data.scene.poses);view.showOpenEdges();$('save-state').textContent=`已保存 · 修订 ${data.revision}`;}
  preview=null;$('cancel-preview').hidden=true;$('preview-message').hidden=true;
}
async function previewConnection(){
  if(busy)return;cancelPreview();busy=true;updateActionAvailability();fail('');
  try{
    preview=await request(`/api/assemblies/${aid}`,{method:'POST',headers:{'Content-Type':'application/json','X-Upload-Token':session.token},body:JSON.stringify({revision:data.revision,action:'preview_connect',...connectionValues()})});
    view.applyPoses(preview.scene.poses);view.showOpenEdges(preview.assessment);view.fitConnected(edgeMap().get($('edge-a').value).face_id,preview.scene);
    const last=preview.assessment.links.at(-1);$('preview-message').textContent=`预览尚未保存：最大端点间隙 ${fmt(last.endpoint_gap_mm)} mm，角度误差 ${fmt(last.angle_error_deg)}°；${preview.assessment.intersections.length} 处推定面边界穿插/重叠。确认后点击“连接所选两边”。`;
    $('preview-message').hidden=false;$('cancel-preview').hidden=false;$('save-state').textContent='连接预览 · 尚未保存';
  }catch(e){fail(e.message);}finally{busy=false;updateActionAvailability();}
}

class AssemblyView{
  constructor(){
    this.renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});this.renderer.setPixelRatio(Math.min(devicePixelRatio,2));
    this.renderer.setClearColor(0xedf2f7);this.renderer.domElement.id='assembly-canvas';$('viewport').append(this.renderer.domElement);
    this.scene=new THREE.Scene();this.camera=new THREE.PerspectiveCamera(40,1,1,1e8);this.camera.up.set(0,0,1);
    this.controls=new OrbitControls(this.camera,this.renderer.domElement);this.controls.enableDamping=true;this.controls.screenSpacePanning=true;
    this.scene.add(new THREE.HemisphereLight(0xffffff,0x737e94,2.8));const light=new THREE.DirectionalLight(0xffffff,2.8);light.position.set(1,-2,4);this.scene.add(light);
    this.grid=new THREE.GridHelper(10000,20,0x9cabbc,0xd0dae5);this.grid.rotation.x=Math.PI/2;this.scene.add(this.grid);
    this.axes=new THREE.AxesHelper(1000);this.scene.add(this.axes);this.groups=new Map();this.edgeLines=new Map();
    this.memberLabels=new MemberLabels(this.scene,chooseMember);
    this.ray=new THREE.Raycaster();this.ray.params.Line.threshold=15;
    new ResizeObserver(()=>this.resize()).observe($('viewport'));this.resize();
    this.renderer.domElement.addEventListener('pointerdown',e=>this.down(e),true);
    this.renderer.domElement.addEventListener('pointermove',e=>this.move(e));
    this.renderer.domElement.addEventListener('pointerup',e=>this.up(e));
    this.renderer.domElement.addEventListener('pointercancel',e=>{if(e.pointerId===this.drag?.pointerId)this.cancelDrag();});
    this.renderer.domElement.addEventListener('lostpointercapture',e=>{if(e.pointerId===this.drag?.pointerId)this.cancelDrag();});
    window.addEventListener('blur',()=>this.cancelDrag());
    window.addEventListener('keydown',e=>{if(e.key==='Escape'&&this.drag){e.preventDefault();this.cancelDrag();}});
    this.renderer.domElement.addEventListener('webglcontextlost',e=>{e.preventDefault();fail('三维绘图上下文已丢失，请刷新页面。装配布局已保存在本机。');});
    const animate=()=>{requestAnimationFrame(animate);if(!this.drag)this.controls.update();this.updateMemberLabels();this.syncTrims();if(this.missingRoot)this.missingRoot.visible=!this.drag&&!preview&&!busy;if(this.openLines)this.openLines.visible=$('open-edges').checked&&!this.drag&&!busy;this.renderer.render(this.scene,this.camera);};animate();
  }
  resize(){const box=$('viewport').getBoundingClientRect();this.renderer.setSize(box.width,box.height);this.camera.aspect=box.width/Math.max(1,box.height);this.camera.updateProjectionMatrix();}
  dispose(object){object?.traverse(o=>{o.geometry?.dispose();for(const m of o.material?[].concat(o.material):[]){m.map?.dispose();m.dispose();}});if(object)this.scene.remove(object);}
  line(points,color,dashed=false){const geometry=new THREE.BufferGeometry().setFromPoints(points.map(p=>new THREE.Vector3(...p)));const material=dashed?new THREE.LineDashedMaterial({color,dashSize:45,gapSize:30}):new THREE.LineBasicMaterial({color});const line=new THREE.Line(geometry,material);if(dashed)line.computeLineDistances();return line;}
  build(project){
    this.memberLabels.clear();this.dispose(this.root);this.root=new THREE.Group();this.scene.add(this.root);this.groups.clear();this.edgeLines.clear();this.pickables=[];
    this.dispose(this.trimRoot);this.trimRoot=new THREE.Group();this.trimRoot.name='removed-material-guides';this.scene.add(this.trimRoot);this.trimGroups=new Map();
    const labels=[];
    for(const [index,f] of project.faces.entries()){
      const group=new THREE.Group();group.userData.faceId=f.id;this.root.add(group);this.groups.set(f.id,group);
      const trimGroup=new THREE.Group();trimGroup.matrixAutoUpdate=false;this.trimRoot.add(trimGroup);this.trimGroups.set(f.id,trimGroup);
      const color=new THREE.Color().setHSL((index*.13+.55)%1,.42,.5);
      for(const m of f.members){
        const u=new THREE.Vector3(...m.b).sub(new THREE.Vector3(...m.a)),len=u.length();if(len<.001)continue;
        const key=`${f.id}/${m.id}`,text=`${m.side||''}${m.mark??'未标号'}`;
        const labelHeight=Math.min(140,Math.max(60,Math.max(...f.edges.map(e=>e.length))*(f.display_scale||1)*.017))/(f.units==='mm'?1:(f.display_scale||1));
        labels.push({key,text,record:m,faceId:f.id,object:group,axisLength:len,point:new THREE.Vector3(...m.a).lerp(new THREE.Vector3(...m.b),.5),direction:u.clone(),memberWidth:f.units==='mm'?Math.max(...m.section.legs_mm):labelHeight*.5,labelHeight,title:`件号 ${text} · ${m.specification}\nBOM ${fmt(m.bom_length_mm)} mm / 模型轴长 ${fmt(len)} ${f.units}\n${f.source_name} / ${f.title}`});
        if(f.units!=='mm'){const line=this.line([m.a,m.b],0xe6b800);line.userData={faceId:f.id,memberKey:key,baseColor:0xe6b800};group.add(line);this.pickables.push(line);continue;}
        u.normalize();const normal=new THREE.Vector3(-u.y,u.x,0),[a,b]=m.section.legs_mm,t=m.section.thickness_mm;
        const shape=new THREE.Shape();shape.moveTo(0,0);for(const p of [[a,0],[a,t],[t,t],[t,b],[0,b]])shape.lineTo(...p);shape.closePath();
        for(const segment of m.correction?.segments||[{kind:'retained',a:m.a,b:m.b}]){
          if(segment.kind==='trimmed'){const line=this.line([segment.a,segment.b],0xd82948,true);line.userData={faceId:f.id,memberKey:key,correctionKind:'trimmed'};trimGroup.add(line);continue;}
          const depth=new THREE.Vector3(...segment.b).distanceTo(new THREE.Vector3(...segment.a));if(depth<.001)continue;
          const baseColor=segment.kind==='added'?0xf16b13:0xe6b800;
          const mesh=new THREE.Mesh(new THREE.ExtrudeGeometry(shape,{depth,bevelEnabled:false,steps:1}),new THREE.MeshStandardMaterial({color:baseColor,roughness:.65,metalness:.12}));
          mesh.matrixAutoUpdate=false;mesh.matrix.makeBasis(normal,new THREE.Vector3(0,0,1),u);mesh.matrix.setPosition(segment.a[0]-normal.x*a/2,segment.a[1]-normal.y*a/2,segment.a[2]);mesh.userData={faceId:f.id,memberKey:key,baseColor,correctionKind:segment.kind};group.add(mesh);this.pickables.push(mesh);
        }
      }
      for(const p of f.plates){
        if(!p.polygon||!p.stock?.thickness_mm||f.units!=='mm')continue;
        const shape=new THREE.Shape(p.polygon.map(v=>new THREE.Vector2(v[0],v[1])));
        for(const h of p.drill_holes_local||[]){if(!(h.diameter_mm>0))continue;const hole=new THREE.Path();hole.absarc(h.center[0],h.center[1],h.diameter_mm/2,0,Math.PI*2,true);shape.holes.push(hole);}
        const mesh=new THREE.Mesh(new THREE.ExtrudeGeometry(shape,{depth:p.stock.thickness_mm,bevelEnabled:false}),new THREE.MeshStandardMaterial({color:0x8493a6,side:THREE.DoubleSide}));mesh.position.z=-p.stock.thickness_mm;mesh.userData.faceId=f.id;group.add(mesh);this.pickables.push(mesh);
      }
      for(const h of f.holes){const r=h.radius;if(!(r>0))continue;const ring=new THREE.Mesh(new THREE.RingGeometry(r*.8,r,16),new THREE.MeshBasicMaterial({color:0x317ad6,side:THREE.DoubleSide,depthTest:false}));ring.position.fromArray(h.center);ring.position.z+=1;ring.renderOrder=5;group.add(ring);}
      if(f.wire.length){const geometry=new THREE.BufferGeometry().setFromPoints(f.wire.flat().map(v=>new THREE.Vector3(...v)));const line=new THREE.LineSegments(geometry,new THREE.LineBasicMaterial({color:0x62758b}));line.userData.faceId=f.id;group.add(line);this.pickables.push(line);}
      if(f.polygon.length>=3){const hull=new THREE.Mesh(new THREE.ShapeGeometry(new THREE.Shape(f.polygon.map(v=>new THREE.Vector2(v[0],v[1])))),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.09,side:THREE.DoubleSide,depthWrite:false}));hull.userData.faceId=f.id;hull.userData.envelope=true;group.add(hull);this.pickables.push(hull);}
      for(const edge of f.edges){const line=this.line([edge.a,edge.b],color,edge.basis==='inferred_convex_envelope');line.material.transparent=true;line.material.opacity=.8;line.userData={faceId:f.id,edgeId:edge.id};line.position.z=.2;group.add(line);this.edgeLines.set(edge.id,line);}
      // Labels are local canvas textures; no external fonts or network assets.
      const canvas=document.createElement('canvas');canvas.width=640;canvas.height=72;const ctx=canvas.getContext('2d');ctx.fillStyle='#ffffffdc';ctx.fillRect(0,0,640,72);ctx.fillStyle='#233d61';ctx.font='26px sans-serif';ctx.fillText(`${index+1}. ${f.title}`,12,44,615);
      const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(canvas),depthTest:false}));sprite.userData.faceTitle=true;const box=new THREE.Box3().setFromObject(group);const width=Math.max(box.isEmpty()?600:box.max.x-box.min.x,f.units==='mm'?600:60);sprite.scale.set(width*.7,width*.7*72/640,1);sprite.position.set(box.isEmpty()?0:(box.min.x+box.max.x)/2,box.isEmpty()?100:box.max.y+width*.06,0);group.add(sprite);
      group.scale.setScalar(f.display_scale||1);
    }
    this.memberLabels.setItems(labels);this.applyPoses(project.scene.poses);this.highlight();
  }
  updateMemberLabels(){this.memberLabels.update(this.camera,{enabled:$('member-labels').checked,include:item=>$('member-label-scope').value==='all'||item.faceId===selected,selectedKey:selectedMemberKey,size:Number($('member-label-size').value)});}
  syncTrims(){
    if(!this.trimRoot)return;this.trimRoot.visible=$('trimmed-segments').checked;
    for(const [fid,trim] of this.trimGroups){const group=this.groups.get(fid);group.updateWorldMatrix(true,false);trim.matrix.copy(group.matrixWorld);trim.matrixWorldNeedsUpdate=true;}
  }
  applyPoses(poses){for(const [fid,p] of Object.entries(poses)){const group=this.groups.get(fid);if(group){group.position.fromArray(p.position);group.quaternion.fromArray(p.quaternion);}}this.root?.updateMatrixWorld(true);}
  fit(fid,customBox){
    const target=fid?this.groups.get(fid):this.root;if(!target)return;
    const box=customBox||new THREE.Box3().setFromObject(target);if(box.isEmpty())return;
    const center=box.getCenter(new THREE.Vector3()),size=box.getSize(new THREE.Vector3()).length()||1000;
    const damping=this.controls.enableDamping;this.controls.enableDamping=false;this.controls.update();
    this.camera.up.set(0,0,1);this.controls.target.copy(center);this.camera.position.copy(center).add(new THREE.Vector3(1,-1.6,1).normalize().multiplyScalar(size*1.6));this.camera.near=Math.max(.01,size/10000);this.camera.far=Math.max(1e6,size*100);this.camera.updateProjectionMatrix();
    this.grid.scale.setScalar(Math.max(1,size/10000));this.axes.scale.setScalar(Math.max(.1,size/10000));this.ray.params.Line.threshold=size*.0025;this.controls.update();this.controls.enableDamping=damping;
  }
  highlight(){
    if(!data)return;
    for(const object of this.pickables){if(!object.userData.memberKey)continue;const active=object.userData.memberKey===selectedMemberKey;object.material.color.set(active?0xbf47ad:object.userData.baseColor??0xe6b800);object.material.depthTest=!active;object.renderOrder=active?7:0;}
    for(const [fid,g] of this.groups)g.traverse(o=>{if(o.userData.envelope){o.visible=$('envelopes').checked;o.material.opacity=fid===selected ? .18 : .06;}if(o.userData.faceTitle)o.visible=$('face-titles').checked;});
    for(const [eid,line] of this.edgeLines){const chosen=[$('edge-a').value,$('edge-b').value].includes(eid);line.material.color.set(chosen?0xec3546:line.userData.faceId===selected?0x245bb1:0x8797ab);line.material.opacity=chosen?1:.6;line.material.depthTest=!chosen;line.renderOrder=chosen?8:1;}
  }
  showOpenEdges(assessment=data.assessment){this.dispose(this.openLines);this.openLines=new THREE.Group();this.scene.add(this.openLines);this.openLines.visible=$('open-edges').checked;for(const edge of assessment.open_edges)this.openLines.add(this.line([edge.a,edge.b],0xe6533f,true));}
  showMissingFace(report){
    this.dispose(this.missingRoot);this.missingRoot=null;if(!report)return;
    this.missingRoot=new THREE.Group();this.missingRoot.name='unverified-missing-face-review';this.scene.add(this.missingRoot);
    const outline=new THREE.Group();outline.visible=$('missing-face-outline').checked;this.missingRoot.add(outline);
    const points=report.prediction.planar_corners_world_mm,geometry=new THREE.BufferGeometry();
    geometry.setAttribute('position',new THREE.Float32BufferAttribute(points.flat(),3));geometry.setIndex([0,1,2,0,2,3]);
    if(report.prediction.planarity_max_mm<=3)outline.add(new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({color:0xf28b26,side:THREE.DoubleSide,transparent:true,opacity:.18,depthWrite:false})));else geometry.dispose();
    const raw=report.prediction.raw_corners_world_mm;outline.add(this.line([...raw,raw[0]],0xe37518,true));
    const trial=new THREE.Group();trial.visible=$('missing-face-native').checked;this.missingRoot.add(trial);
    if(report.native_candidate_fit){
      const pose=report.native_candidate_fit.pose;trial.position.fromArray(pose.position);trial.quaternion.fromArray(pose.quaternion);
      for(const [a,b] of report.native_candidate_axes_local){
        const start=new THREE.Vector3(...a),end=new THREE.Vector3(...b),delta=end.clone().sub(start);
        const axis=new THREE.Mesh(new THREE.CylinderGeometry(12,12,delta.length(),8),new THREE.MeshBasicMaterial({color:0xffd21b}));
        axis.position.copy(start).lerp(end,.5);axis.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),delta.normalize());trial.add(axis);
      }
    }
  }
  fitMissing(){
    const report=data.missing_face_review;if(!report)return;
    const box=new THREE.Box3().setFromPoints(report.prediction.raw_corners_world_mm.map(p=>new THREE.Vector3(...p)));
    this.fit(null,box);const distance=this.camera.position.distanceTo(this.controls.target);
    this.camera.position.copy(this.controls.target).addScaledVector(new THREE.Vector3(...report.prediction.plane_normal),distance);
    this.controls.update();
  }
  rayAt(e){const box=this.renderer.domElement.getBoundingClientRect();this.ray.setFromCamera(new THREE.Vector2((e.clientX-box.left)/box.width*2-1,1-(e.clientY-box.top)/box.height*2),this.camera);}
  faceHit(e){this.rayAt(e);return this.ray.intersectObjects([...this.pickables,...this.edgeLines.values()],false).find(h=>h.object.visible)?.object.userData.faceId;}
  component(fid,scene=data.scene){const found=new Set([fid]),edges=edgeMap();let changed=true;while(changed){changed=false;for(const link of scene.links){const a=edges.get(link.a).face_id,b=edges.get(link.b).face_id;if(found.has(a)||found.has(b)){for(const id of [a,b])if(!found.has(id)){found.add(id);changed=true;}}}}return found;}
  fitConnected(fid,scene=data.scene){const box=new THREE.Box3();for(const id of this.component(fid,scene))box.union(new THREE.Box3().setFromObject(this.groups.get(id)));this.fit(null,box);}
  planConnected(fid,scene=data.scene){this.fitConnected(fid,scene);const distance=this.camera.position.distanceTo(this.controls.target);this.camera.position.copy(this.controls.target).add(new THREE.Vector3(0,-distance*.0001,distance));this.controls.update();}
  down(e){
    if(this.drag){e.stopImmediatePropagation();return;}
    this.start=e.button===0?[e.clientX,e.clientY]:null;
    if(staticMode||busy||!data||$('mode').value!=='drag'||e.button!==0)return;
    cancelPreview();const fid=this.faceHit(e);if(!fid)return;choose(fid);
    const direction=$('drag-axis').value,normal=this.camera.getWorldDirection(new THREE.Vector3());
    const axis={x:new THREE.Vector3(1,0,0),y:new THREE.Vector3(0,1,0),z:new THREE.Vector3(0,0,1)}[direction];
    // An axis lies in the drag plane; project the ray displacement onto it.
    if(axis)normal.addScaledVector(axis,-normal.dot(axis));
    if(direction==='xy')normal.set(0,0,1);
    e.stopImmediatePropagation();e.preventDefault();
    if(normal.lengthSq()<.0025||Math.abs(this.ray.ray.direction.dot(normal.clone().normalize()))<.05){
      this.start=null;$('hint').textContent='当前移动方向接近视线，请先在空白处旋转视角，或切换“自由（屏幕平面）”。';return;
    }
    const plane=new THREE.Plane().setFromNormalAndCoplanarPoint(normal.normalize(),this.groups.get(fid).position);
    const point=this.ray.ray.intersectPlane(plane,new THREE.Vector3());if(!point)return;
    const moveConnected=$('move-connected').checked,ids=moveConnected?this.component(fid):new Set([fid]);
    this.drag={fid,plane,point,axis,pointerId:e.pointerId,moveConnected,moved:false,
      poses:new Map([...ids].map(id=>[id,this.groups.get(id).position.clone()]))};
    this.controls.enabled=false;this.renderer.domElement.setPointerCapture(e.pointerId);
    $('viewport').classList.add('dragging');updateInteraction();
  }
  move(e){
    const drag=this.drag;if(!drag||e.pointerId!==drag.pointerId)return;
    if(!drag.moved&&Math.hypot(e.clientX-this.start[0],e.clientY-this.start[1])<4)return;
    this.rayAt(e);const point=this.ray.ray.intersectPlane(drag.plane,new THREE.Vector3());if(!point)return;
    const delta=point.sub(drag.point);if(drag.axis)delta.copy(drag.axis.clone().multiplyScalar(delta.dot(drag.axis)));
    drag.moved=true;for(const [fid,start] of drag.poses)this.groups.get(fid).position.copy(start).add(delta);
    this.root.updateMatrixWorld(true);
    ['px','py','pz'].forEach((id,i)=>$(id).value=this.groups.get(drag.fid).position.toArray()[i].toFixed(2));
    $('hint').textContent=`正在移动 ${drag.poses.size} 个面 · ΔX ${fmt(delta.x)} / ΔY ${fmt(delta.y)} / ΔZ ${fmt(delta.z)}（场景单位；已定比例为 mm） · 松手保存，Esc 取消`;
  }
  finishDrag(){
    const drag=this.drag;this.drag=null;this.controls.enabled=true;
    if(drag&&this.renderer.domElement.hasPointerCapture(drag.pointerId))this.renderer.domElement.releasePointerCapture(drag.pointerId);
    $('viewport').classList.remove('dragging');updateInteraction();return drag;
  }
  cancelDrag(){
    if(!this.drag)return;this.finishDrag();this.start=null;
    if(data){this.applyPoses(data.scene.poses);renderSelection();}
  }
  up(e){
    if(this.drag){
      if(e.pointerId!==this.drag.pointerId)return;this.move(e);
      const drag=this.finishDrag(),g=this.groups.get(drag.fid);this.start=null;
      if(drag.moved&&g.position.distanceToSquared(drag.poses.get(drag.fid))>1e-12){
        act('pose',{face_id:drag.fid,pose:{position:g.position.toArray(),quaternion:g.quaternion.toArray()},move_connected:drag.moveConnected});
      }else{this.applyPoses(data.scene.poses);renderSelection();this.pick(e);}
      return;
    }
    if(!data||!this.start||Math.hypot(e.clientX-this.start[0],e.clientY-this.start[1])>5)return;
    this.start=null;this.pick(e);
  }
  pick(e){
    if($('mode').value==='edge'){
      this.rayAt(e);const hit=this.ray.intersectObjects([...this.edgeLines.values()],false)[0];if(!hit)return;
      const edge=edgeMap().get(hit.object.userData.edgeId);cancelPreview();if($('edge-a').value&&$('edge-b').value){$('edge-a').value='';$('edge-b').value='';}
      $($('edge-a').value?'edge-b':'edge-a').value=edge.id;choose(edge.face_id);edgeInfo();this.highlight();
    }else{this.rayAt(e);const label=this.memberLabels.pick(this.ray,this.pickables.filter(o=>!o.userData.envelope));if(label){chooseMember(label);return;}const fid=this.faceHit(e);if(fid)choose(fid);}
  }
}

$('fit').onclick=()=>view.fit();$('focus').onclick=()=>view.fit(selected);
$('review-missing-face').onclick=()=>act('review_missing_face',{face_id:$('missing-face').value});
$('automatic-pipeline').onclick=()=>act('automatic_pipeline');
$('missing-face').onchange=()=>{saveMissingSelection($('missing-face').value);renderMissingFace();};
$('focus-missing-face').onclick=()=>{cancelPreview();view.fitMissing();};
for(const id of ['missing-face-outline','missing-face-native'])$(id).onchange=()=>view.showMissingFace(data?.missing_face_review);
$('focus-group').onclick=()=>selected&&view.fitConnected(selected,preview?.scene||data.scene);
$('plan-group').onclick=()=>selected&&view.planConnected(selected,preview?.scene||data.scene);
$('preview-taper').onclick=previewTaper;$('apply-taper').onclick=()=>act('fit_taper',{tip_direction:$('tip-direction').value});
$('tip-direction').onchange=cancelPreview;
$('auto').onclick=()=>act('auto',{tip_direction:$('tip-direction').value||undefined});$('arrange').onclick=()=>act('arrange');
$('front').onclick=()=>selected&&act('front',{face_id:selected});
$('apply-pose').onclick=()=>{if(!selected)return;const position=['px','py','pz'].map(id=>Number($(id).value)),angles=['rx','ry','rz'].map(id=>THREE.MathUtils.degToRad(Number($(id).value)));if(![...position,...angles].every(Number.isFinite))return fail('位置与角度须为数值。');const quaternion=new THREE.Quaternion().setFromEuler(new THREE.Euler(...angles,'XYZ')).toArray();act('pose',{face_id:selected,pose:{position,quaternion},move_connected:$('move-connected').checked});};
$('apply-scale').onclick=()=>selected&&act('scale',{face_id:selected,mm_per_pt:Number($('scale').value)});
$('connect').onclick=()=>act('connect',connectionValues());$('preview-connect').onclick=previewConnection;$('cancel-preview').onclick=cancelPreview;
$('undo').onclick=()=>act('undo');$('redo').onclick=()=>act('redo');
for(const id of ['edge-a','edge-b'])$(id).onchange=()=>{cancelPreview();edgeInfo();view.highlight();};
for(const id of ['angle','flip'])$(id).oninput=cancelPreview;
$('clear-edges').onclick=()=>{cancelPreview();$('edge-a').value='';$('edge-b').value='';edgeInfo();view.highlight();};
$('open-edges').onchange=()=>view.showOpenEdges(preview?.assessment||data.assessment);$('envelopes').onchange=()=>view.highlight();
$('face-titles').onchange=()=>view.highlight();
function updateInteraction(){
  if(staticMode&&$('mode').value==='drag')$('mode').value='orbit';
  const mode=$('mode').value,dragging=!!view?.drag;
  $('drag-model').disabled=staticMode||busy||dragging;$('drag-model').classList.toggle('primary',mode==='drag');
  $('drag-model').setAttribute('aria-pressed',String(mode==='drag'));
  $('mode').querySelector('[value=drag]').disabled=staticMode;
  $('mode').disabled=busy||dragging;$('drag-axis').disabled=staticMode||busy||dragging||mode!=='drag';
  $('move-connected').disabled=staticMode||busy||dragging;
  $('viewport').dataset.mode=mode;
  const scope=$('move-connected').checked?'整个连接组':'单个面';
  $('hint').textContent=staticMode?'保存结果为查看模式；请从图纸工作台打开装配项目以拖动和保存模型。':{
    orbit:'拖动旋转视角，滚轮缩放。点击“拖动模型”可以移动模型；点击件号可核对杆件。',
    drag:`按住模型拖动${scope}；移动方向：${$('drag-axis').selectedOptions[0].textContent}。空白处拖动旋转视角，右键平移，滚轮缩放。松手保存，Esc 取消。`,
    edge:'依次点击固定边 A 和移动边 B，输入角度后点击“连接所选两边”。'
  }[mode];
}
$('drag-model').onclick=()=>{$('mode').value='drag';updateInteraction();};
$('mode').onchange=()=>{view.cancelDrag();updateInteraction();};
for(const id of ['drag-axis','move-connected'])$(id).onchange=updateInteraction;
$('export').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({schema_version:'1.0',input_digest:data.input_digest,scene:data.scene},null,2)],{type:'application/json'}));const a=n('a');a.href=url;a.download=`assembly-${aid}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('import').onclick=()=>$('layout-file').click();$('layout-file').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;if(file.size>2*1024*1024)throw Error('布局文件不能超过 2 MB。');await act('import',JSON.parse(await file.text()));}catch(err){fail(err.message);}finally{e.target.value='';}};
$('add').onclick=()=>$('pdfs').click();$('pdfs').onchange=async e=>{
  const files=[...e.target.files];if(!files.length)return;if(files.length+data.sources.length>30)return fail('一个装配项目最多支持 30 张不同 PDF。');
  let ids=[];try{fail('');for(const f of files){if(!/\.pdf$/i.test(f.name)||!f.size||f.size>session.max_upload_bytes)throw Error(`${f.name}：请选择不超过 100 MB 的 PDF。`);}
    for(const f of files){$('save-state').textContent=`正在上传 ${f.name}…`;const job=await request('/api/jobs',{method:'POST',headers:{'Content-Type':'application/pdf','X-File-Name':encodeURIComponent(f.name),'X-Upload-Token':session.token},body:f});ids.push(job.id);}
  }catch(err){fail(err.message);}finally{if(ids.length)await act('add_jobs',{job_ids:ids});e.target.value='';await existingJobs();}
};
async function existingJobs(){if(staticMode)return;const {jobs}=await request('/api/jobs');$('existing-jobs').replaceChildren();for(const j of jobs.filter(j=>!data.sources.some(s=>s.id===j.id))){const label=n('label',undefined,$('existing-jobs')),check=n('input',undefined,label);check.type='checkbox';check.value=j.id;label.append(j.name);}}
$('add-existing').onclick=async()=>{const ids=[...$('existing-jobs').querySelectorAll('input:checked')].map(i=>i.value);if(!ids.length)return fail('请先选择要补充的图纸。');await act('add_jobs',{job_ids:ids});await existingJobs();};
(async()=>{try{view=new AssemblyView();window.assemblyView=view;session=await request('/api/session');await reload();await existingJobs();timer=setTimeout(poll,2500);}catch(e){fail(`三维装配加载失败：${e.message}`);$('loading').textContent='加载失败，请检查上方提示并刷新重试。';$('save-state').textContent='加载失败';}})();
