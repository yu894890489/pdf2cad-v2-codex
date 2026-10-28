"use strict";
const $=id=>document.getElementById(id);
const state={stage:"members",view:null,selected:null,images:new Map(),zoom:1,pan:[0,0],hits:[],viewToken:0,modelActive:false};
const labels={reviewed_2d_association:"已复核二维对应",leader_supported_candidate:"引线支持 · 待核对",proximity_candidate:"邻近候选 · 待核对",unresolved:"未对应",boundary_unresolved:"轮廓未闭合对应",leader_supported_contour_candidate:"引线支持轮廓 · 待核对",detail_contour_candidate:"详图轮廓候选",needs_review:"待核对",detail_diameter_annotation_supported:"详图孔径标注支持",multiple_projected_owners:"多个投影归属",projected_owner_candidate:"投影归属候选"};
const partName=p=>(p.side||"")+(p.mark||p.id.split("_").slice(-2).join("_"));
const local=v=>labels[v]||v;
const recoveryStatus=r=>!r?"未分析":r.bom_conflict?"轮廓已接续 · BOM 冲突待核对":r.status==="partial_overlap_recovered_candidate"?"仅补回部分 · 端点待核对":r.applied?"已补重叠段 · 端点待核对":r.status==="already_matches_bom_candidate"?"原段长度相符":r.status==="ambiguous_end_allocation"?"补段位置有多解":r.status==="observed_span_exceeds_bom_candidate"?"原段疑似过长":"板下端点未确定";
async function json(file){const response=await fetch(file,{signal:AbortSignal.timeout(30000)});if(!response.ok)throw Error(`${file}: HTTP ${response.status}`);return response.json();}
function showError(e){$("error").hidden=false;$("error").textContent=`读取失败：${e.message}\n请使用 run-topology.ps1 启动本地服务，并检查输出目录。`;$("status").textContent="加载失败";}
function el(tag,text,parent){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(parent)parent.append(node);return node;}
function list(kind){return state.data[kind].filter(p=>p.view_id===state.view.id);}
async function chooseView(id){
  const version=++state.viewToken;
  state.view=state.data.views.find(v=>v.id===id);state.selected=null;state.zoom=1;state.pan=[0,0];
  const page=state.data.report.pages.find(p=>p.page===state.view.page);
  if(!state.images.has(page.page)){
    const img=new Image();img.src="../"+page.structure;
    await Promise.race([img.decode(),new Promise((_,reject)=>setTimeout(()=>reject(Error("原图读取超时")),30000))]);
    state.images.set(page.page,img);
  }
  if(!state.images.has(id)){
    const img=new Image();img.src=state.view.image;
    await Promise.race([img.decode(),new Promise((_,reject)=>setTimeout(()=>reject(Error("视图底图读取超时")),30000))]);state.images.set(id,img);
  }
  if(version!==state.viewToken)return;
  document.querySelectorAll(".view-button").forEach(b=>b.classList.toggle("active",b.dataset.id===id));
  $("view-title").textContent=state.view.title;
  $("view-summary").textContent=`${state.view.primitive_ids.length} 个所属图元 · ${state.view.member_ids.length} 条杆件观察（${list("members").filter(m=>m.overlap_recovery?.applied).length} 条已补重叠段） · ${state.view.plate_ids.length} 条板件观察 · ${state.view.hole_ids.length} 个圆形投影${state.view.projection_mode==="overlapping_front_rear"?" · 含前/后重叠件号":""}`;
  $("issues").replaceChildren();
  state.data.report.issues.filter(i=>i.view_id===id).forEach(i=>el("li",`${i.message}（${i.object_ids.length} 条）`,$("issues")));
  $("selected-title").textContent="选择杆件或图元";$("selected").textContent="点击记录，查看 BOM 和原生图元证据。";
  refreshRecords();drawGraph();render();renderRecognition();if(state.modelLayer)updateModelView();
}
function modelFailure(message){
  state.modelActive=false;state.modelLayer?.setVisible(false);$("model-overlay").hidden=true;
  $("model-toggle").textContent="重试三维模型";$("model-toggle").setAttribute("aria-pressed","false");
  $("model-message").textContent=`三维模型未加载：${message}`;$("model-message").classList.add("failure");
  $("model-align").disabled=true;$("model-oblique").disabled=true;
}
function renderRecognition(){
  const r=state.preflight;if(!r||!state.view)return;const v=r.views.find(v=>v.view_id===state.view.id),c=v?.counts;
  $("recognition-global").textContent=`角钢件号 ${r.observed_angle_marks}/${r.bom_angle_marks}；未找到件号：${r.missing_member_marks.join("、")||"无"}。未归属小圆 ${r.unassigned_small_circle_ids.length} 个。观察数不等于实物数量。`;
  $("recognition-view").textContent=c?`本面：角钢模型 ${c.angle_models}/${c.member_observations}；杆端未完整 ${c.member_incomplete}；板形 ${c.plate_polygons}/${c.plate_observations}；孔位投影 ${c.hole_projections}，明确孔径 ${c.explicit_hole_diameters}。`:"本面检查记录未生成";
  $("model-issues").replaceChildren();
  for(const i of r.issues.filter(i=>i.view_id===state.view.id))el("li",`件号 ${i.mark}：${i.message}`,$("model-issues"));
  for(const text of r.notes)el("li",text,$("model-issues"));
}
function applyPreviewEvidence(){
  if(!state.modelData)return;
  if(state.modelData.source_sha256!==state.data.report.source_sha256)throw Error("三维预览来源与当前图纸不一致");
  for(const v of state.modelData.views){
    for(const p of v.plates){
      const original=state.data.plates.find(x=>x.id===p.id);if(!original)continue;
      original.source_polygon??=original.polygon;original.polygon=p.outline?.polygon_pt||null;
      original.status=p.outline?"stock_checked_native_contour_candidate":"boundary_unresolved";original.preview_check=p;
    }
    for(const h of v.holes){
      const original=state.data.holes.find(x=>x.id===h.id);if(original&&h.diameter_mm!==null){original.diameter_mm=h.diameter_mm;original.preview_diameter=h;}
    }
  }
  labels.stock_checked_native_contour_candidate="原生板形与板坯尺寸候选对应";
  refreshRecords();render();
}
function updateModelLayers(){
  state.modelLayer?.setLayers(Object.fromEntries(["members","plates","holes"].map(k=>[k,$("model-"+k).checked])));
  state.modelLayer?.setOpacity(Number($("model-opacity").value));
}
function updateModelView(){
  const model=state.modelData?.views.find(v=>v.view_id===state.view.id);if(!model||!state.modelLayer)return;
  state.modelLayer.setView(model,transform());updateModelLayers();
  if(state.modelActive){state.modelLayer.setVisible(true);state.modelLayer.oblique();}
  const c=model.counts;
  $("model-message").classList.remove("failure");
  $("model-message").textContent=c.angle_models||c.plate_polygons?`本面基础三维：${c.angle_models} 条角钢、${c.plate_polygons} 个板形、${c.hole_projections} 个孔位标记。前后层距与角钢朝向待核对；板形缺项 ${c.plate_shapes_pending}。`:`此图尚无可生成的杆件或板形；请查看其他结构面及识别缺项。`;
}
async function toggleModel(){
  if(state.modelActive){state.modelActive=false;state.modelLayer.setVisible(false);$("model-toggle").textContent="显示三维模型";$("model-toggle").setAttribute("aria-pressed","false");$("canvas-hint").textContent="二维图纸：拖动平移 · 滚轮缩放";return;}
  $("model-toggle").disabled=true;$("model-message").classList.remove("failure");$("model-message").textContent="正在读取本地三维模型…";
  let timer;
  try{
    if(!state.modelLayer){
      const [data,module]=await Promise.race([Promise.all([json("model-preview.json"),import("./model-layer.js")]),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error("本地模型或绘图组件读取超过 30 秒")),30000);})]);
      if(data.source_sha256!==state.data.report.source_sha256)throw Error("模型与当前图纸不一致，请重新运行 preview");
      const checks=await json("final-check.json");state.preflight=checks;state.modelData=data;applyPreviewEvidence();
      state.modelLayer=new module.ModelLayer($("model-overlay"),record=>{
        const original=state.data[record.kind].find(x=>x.id===record.id);if(original)select(original);
        $("selected-title").textContent=record.mark?`三维 · 件号 ${record.mark}`:"三维孔位标记";
        $("selected").textContent=JSON.stringify(record,null,2);
      },modelFailure);
      window.modelPreview=state.modelLayer;renderRecognition();
    }
    state.modelActive=true;updateModelView();state.modelLayer.setVisible(true);
    $("model-toggle").textContent="隐藏三维模型";$("model-toggle").setAttribute("aria-pressed","true");
    $("model-align").disabled=false;$("model-oblique").disabled=false;$("canvas-hint").textContent="三维图层：拖动旋转 · 滚轮缩放 · 右键平移 · 图纸对齐恢复正投影";
  }catch(e){modelFailure(e.message);}finally{clearTimeout(timer);$("model-toggle").disabled=false;}
}
function transform(){const canvas=$("drawing"),rect=canvas.getBoundingClientRect(),b=state.view.bbox_pt;const k=Math.min((rect.width-38)/(b[2]-b[0]),(rect.height-38)/(b[3]-b[1]))*state.zoom;return {k,ox:rect.width/2-(b[0]+b[2])/2*k+state.pan[0],oy:rect.height/2-(b[1]+b[3])/2*k+state.pan[1],w:rect.width,h:rect.height};}
function render(){
  if(!state.view||!state.images.has(state.view.page))return;
  const canvas=$("drawing"),t=transform(),dpr=devicePixelRatio||1;canvas.width=Math.round(t.w*dpr);canvas.height=Math.round(t.h*dpr);const ctx=canvas.getContext("2d");ctx.scale(dpr,dpr);ctx.fillStyle="#fff";ctx.fillRect(0,0,t.w,t.h);state.hits=[];
  const xy=p=>[p[0]*t.k+t.ox,p[1]*t.k+t.oy];
  const line=(a,b,color,width=1,dash=[])=>{a=xy(a);b=xy(b);ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash(dash);ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke();ctx.setLineDash([]);};
  const circle=(p,r,color,fill=false)=>{p=xy(p);ctx.beginPath();ctx.arc(...p,r,0,2*Math.PI);ctx.strokeStyle=color;ctx.lineWidth=1.5;if(fill){ctx.fillStyle=color;ctx.fill();}else ctx.stroke();};
  if($("background").checked){const page=state.data.report.pages.find(p=>p.page===state.view.page),image=state.images.get($("raw-page").checked?page.page:state.view.id),b=state.view.bbox_pt;if(image){const sx=image.width/page.size_pt[0],sy=image.height/page.size_pt[1];ctx.globalAlpha=.72;ctx.drawImage(image,b[0]*sx,b[1]*sy,(b[2]-b[0])*sx,(b[3]-b[1])*sy,b[0]*t.k+t.ox,b[1]*t.k+t.oy,(b[2]-b[0])*t.k,(b[3]-b[1])*t.k);ctx.globalAlpha=1;}}
  if($("ink").checked)state.data.geometry.find(g=>g.view_id===state.view.id).strokes.forEach(s=>line(s.a,s.b,"#283746",Math.max(.6,s.width_pt*t.k)));
  const selected=state.selected;
  if($("axes").checked){
    const seen=new Set();for(const m of list("members")){
      if(!m.axis)continue;
      const axis=$("overlap-layer").checked?m.axis:(m.visible_axis||m.axis),key=JSON.stringify(axis);
      if(!seen.has(key)){line(axis.a,axis.b,"#e6b800",2.5);seen.add(key);}
      state.hits.push({object:m,a:xy(axis.a),b:xy(axis.b)});
    }
  }
  for(const c of list("connections")){
    const crossing=c.kind==="crossing_without_connection_evidence";
    if((crossing&&!$("crossings").checked)||(!crossing&&!$("contacts").checked))continue;
    if(crossing){const p=xy(c.point);ctx.strokeStyle="#8a949a";ctx.beginPath();ctx.moveTo(p[0]-4,p[1]-4);ctx.lineTo(p[0]+4,p[1]+4);ctx.moveTo(p[0]+4,p[1]-4);ctx.lineTo(p[0]-4,p[1]+4);ctx.stroke();}
    else circle(c.point,3,c.connected?"#167b69":"#bc7213");
    state.hits.push({object:c,p:xy(c.point)});
  }
  if($("plate-layer").checked)for(const p of list("plates")){
    if(p.polygon){ctx.beginPath();p.polygon.forEach((q,i)=>i?ctx.lineTo(...xy(q)):ctx.moveTo(...xy(q)));ctx.closePath();ctx.fillStyle="#1c99c638";ctx.fill();ctx.strokeStyle="#0876ae";ctx.stroke();}
    const b=p.callout_bbox_pt,q=[(b[0]+b[2])/2,(b[1]+b[3])/2];circle(q,Math.max(5,(b[2]-b[0])/2*t.k),"#0876ae");state.hits.push({object:p,p:xy(q)});
  }
  if($("hole-layer").checked)for(const h of list("holes")){ctx.setLineDash(h.visibility==="hidden_dashed_circle_projection"?[2,2]:[]);circle(h.center_pt,Math.max(3,Math.max(...h.radii_pt)*t.k),"#a538b0");ctx.setLineDash([]);state.hits.push({object:h,p:xy(h.center_pt)});}
  if(selected){
    if(selected.axis&&$("axes").checked){const axis=$("overlap-layer").checked?selected.axis:(selected.visible_axis||selected.axis);line(axis.a,axis.b,"#e6b800",4);}
    const q=selected.center_pt||selected.point;
    if(q)circle(q,8,"#d93919");
    if(selected.callout_bbox_pt){const b=selected.callout_bbox_pt,p=xy([b[0],b[1]]);ctx.strokeStyle="#d93919";ctx.lineWidth=2;ctx.strokeRect(...p,(b[2]-b[0])*t.k,(b[3]-b[1])*t.k);}
  }
}
function select(object){
  state.selected=object;$("selected-title").textContent=object.mark?`件号 ${partName(object)} · ${local(object.status)}`:object.id;
  let info;
  if(object.bom){info={记录ID:object.id,件号:object.mark,前后:object.side||"未指定",状态:local(object.status),重叠补全:object.overlap_recovery?recoveryStatus(object.overlap_recovery):undefined,规格:object.bom.specification,BOM下料长度_mm:object.bom.length_mm,BOM总数量:object.bom.quantity,原识别轴线:object.visible_axis,补全候选轴线:object.overlap_recovery?.applied?object.axis:undefined,原段长度_pt:object.overlap_recovery?.visible_length_pt,补全后长度_pt:object.overlap_recovery?.recovered_length_pt,剩余缺长候选_pt:object.overlap_recovery?.remaining_length_pt_candidate,端点余量:object.overlap_recovery?.hole_envelope_constraint,补全段及依据:object.overlap_recovery?.extensions,候选轮廓:object.band_id||object.contour_id,BOM投影长度检查:object.bom_projection_check,投影孔位:object.hole_ids,关联杆件:object.member_ids,原生图元证据:object.primitive_ids,候选方案:object.candidate_bands||object.candidate_contours,注意:"BOM 总数量不等于本视图数量；二维轮廓长度单位为 PDF 点。补段与板允许重合，BOM 拟合不作为独立尺寸验收。"};}
  else if(object.center_pt){info={记录ID:object.id,PDF坐标:object.center_pt,孔径_mm:object.diameter_mm,孔径标注证据:object.diameter_evidence,杆件归属候选:object.member_ids,连接板归属候选:object.plate_ids,归属状态:local(object.owner_status),原生图元证据:object.primitive_ids,注意:"没有标注时孔径为空；圆形投影可能代表孔或螺栓。"};}
  else info=object;
  if(object.preview_check)info.建模前轮廓检查=object.preview_check;
  if(object.preview_diameter)info.建模前孔径检查=object.preview_diameter;
  $("selected").textContent=JSON.stringify(info,null,2);refreshRecords();drawGraph();render();
}
function refreshRecords(){
  const type=state.stage,query=$("search").value.trim();const rows=list(type).filter(r=>!query||`${r.mark||""} ${r.side||""} ${r.id}`.includes(query));
  const headers=type==="holes"?["投影记录","原页坐标（pt）","标注孔径","归属候选","状态"]:["件号 / 方位","BOM 规格","下料长 mm / 总数","孔位投影数",type==="members"?"杆端 / 重叠段状态":"轮廓对应状态"];
  $("records-title").textContent=`${{members:"杆件观察",plates:"连接板观察",holes:"孔位 / 螺栓投影"}[type]} · ${rows.length} 条`;
  $("records-head").replaceChildren();const tr=el("tr",undefined,$("records-head"));headers.forEach(h=>el("th",h,tr));$("records").replaceChildren();
  for(const r of rows){const tr=el("tr",undefined,$("records"));tr.classList.toggle("selected",state.selected?.id===r.id);const td=el("td",undefined,tr),button=el("button",partName(r),td);button.dataset.id=r.id;button.addEventListener("click",()=>select(r));
    const values=type==="holes"?[r.center_pt.map(x=>x.toFixed(2)).join(", "),r.diameter_mm===null?"未确定":`${r.diameter_mm} mm`,`${r.member_ids.length} 杆 / ${r.plate_ids.length} 板`,local(r.owner_status)]:[r.bom.specification,`${r.bom.length_mm} / ${r.bom.quantity}`,r.hole_ids.length,type==="members"?recoveryStatus(r.overlap_recovery):local(r.status)+(r.ambiguous?" · 多解":"")];values.forEach(v=>el("td",String(v),tr));
  }
}
function drawGraph(){
  const svg=$("graph");svg.replaceChildren();const ns="http://www.w3.org/2000/svg",create=(tag,attrs,text)=>{const e=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,String(v)));if(text)e.textContent=text;svg.append(e);return e;};
  const members=list("members"),positions=new Map();
  members.forEach((m,i)=>{const angle=(i/Math.max(1,members.length))*Math.PI*2-Math.PI/2;positions.set(m.id,[325+270*Math.cos(angle),130+100*Math.sin(angle)]);});
  for(const c of list("connections")){if(c.status==="not_a_connection"||c.status==="reviewed_disconnected")continue;const a=positions.get(c.member_ids[0]),b=positions.get(c.member_ids[1]);if(!a||!b)continue;const e=create("line",{x1:a[0],y1:a[1],x2:b[0],y2:b[1],stroke:c.connected?"#167b69":"#c1a06c","stroke-dasharray":c.connected?"":"4 4","stroke-width":state.selected?.id===c.id?3:1.3});e.style.cursor="pointer";e.addEventListener("click",()=>select(c));}
  for(const m of members){const [x,y]=positions.get(m.id);const e=create("circle",{cx:x,cy:y,r:state.selected?.id===m.id?9:7,fill:state.selected?.id===m.id?"#ce4c29":"#247d8b"});e.addEventListener("click",()=>select(m));create("text",{x:x+11,y:y+4,fill:"#364e5b"},partName(m));}
  if(!members.length)create("text",{x:20,y:30},"本视图尚无可对应到 BOM 杆件的圈号观察。");
}
async function main(){
  const names=["report","views","members","connections","plates","holes","graph","geometry"];const values=await Promise.all(names.map(n=>json(`${n}.json`)));state.data=Object.fromEntries(names.map((n,i)=>[n,values[i]]));
  const r=state.data.report,c=r.counts;$("drawing-title").textContent=r.title;$("status").textContent="数据已加载 · 二维拓扑待核对";$("notice").textContent=`${c.views} 个视图 · ${c.member_observations} 条杆件观察 · ${c.overlap_recovered_observations||0} 条已补回重叠段（其中 ${c.overlap_partial_observations||0} 条仅补回部分） · ${c.circular_projections} 个孔/螺栓投影（含 ${c.hidden_circle_projections||0} 个分段圆）。补全段为候选，仍需核对端点与空间归属。`;
  for(const view of state.data.views){const b=el("button",view.title,$("views"));b.className="view-button";b.dataset.id=view.id;el("small",`${view.member_ids.length} 杆件观察 / ${view.plate_ids.length} 板件观察`,b);b.addEventListener("click",()=>chooseView(view.id).catch(showError));}
  for(const group of state.data.graph.cross_view_correspondences){const p=el("div",`件号 ${group.mark}：`,$("correspondences"));for(const vid of group.view_ids){const b=el("button",state.data.views.find(v=>v.id===vid).title,p);b.addEventListener("click",()=>{chooseView(vid).then(()=>{$("search").value=group.mark;refreshRecords();}).catch(showError);});}}
  $("checks").textContent=Object.entries(r.checks).map(([k,v])=>`${v?"✓":"✗"} ${k}`).join("\n");r.limitations.forEach(s=>el("li",s,$("limitations")));
  for(const id of ["background","raw-page","ink","axes","overlap-layer","contacts","plate-layer","hole-layer","crossings"])$(id).addEventListener("change",render);
  $("fit").addEventListener("click",()=>{state.zoom=1;state.pan=[0,0];render();state.modelLayer?.align(transform());});$("search").addEventListener("input",refreshRecords);
  $("model-toggle").addEventListener("click",toggleModel);$("model-align").addEventListener("click",()=>state.modelLayer?.align(transform()));$("model-oblique").addEventListener("click",()=>state.modelLayer?.oblique());
  for(const id of ["model-members","model-plates","model-holes","model-opacity"])$(id).addEventListener("input",updateModelLayers);
  document.querySelectorAll("[data-stage]").forEach(b=>b.addEventListener("click",()=>{state.stage=b.dataset.stage;$("search").value="";document.querySelectorAll("[data-stage]").forEach(x=>x.classList.toggle("active",x===b));$("plate-layer").checked=state.stage==="plates";$("hole-layer").checked=state.stage==="holes";refreshRecords();render();}));
  let down=null;const canvas=$("drawing");canvas.addEventListener("pointerdown",e=>{down={x:e.offsetX,y:e.offsetY,pan:[...state.pan],moved:false};canvas.setPointerCapture(e.pointerId);});canvas.addEventListener("pointermove",e=>{if(!down)return;const dx=e.offsetX-down.x,dy=e.offsetY-down.y;down.moved ||= Math.hypot(dx,dy)>4;state.pan=[down.pan[0]+dx,down.pan[1]+dy];render();});canvas.addEventListener("pointerup",e=>{if(down&&!down.moved){const p=[e.offsetX,e.offsetY],distance=h=>{if(h.p)return Math.hypot(h.p[0]-p[0],h.p[1]-p[1]);const d=[h.b[0]-h.a[0],h.b[1]-h.a[1]],t=Math.max(0,Math.min(1,((p[0]-h.a[0])*d[0]+(p[1]-h.a[1])*d[1])/(d[0]*d[0]+d[1]*d[1])));return Math.hypot(p[0]-h.a[0]-t*d[0],p[1]-h.a[1]-t*d[1]);};const best=state.hits.map(h=>({h,d:distance(h)})).sort((a,b)=>a.d-b.d)[0];if(best?.d<12)select(best.h.object);}down=null;});canvas.addEventListener("wheel",e=>{e.preventDefault();state.zoom=Math.min(20,Math.max(.3,state.zoom*Math.exp(-e.deltaY*.001)));render();},{passive:false});new ResizeObserver(render).observe(canvas.parentElement);
  const defaultView=[...state.data.views].sort((a,b)=>b.member_ids.length-a.member_ids.length||Number(b.role==="structural_view")-Number(a.role==="structural_view"))[0];
  if(!defaultView)throw Error("没有分出可核对的视图，请检查原生图元或扫描图输入。");
  await chooseView(defaultView.id);window.topologyReady=true;window.topologyState=state;
  try{const [checks,preview]=await Promise.all([json("final-check.json"),json("model-preview.json")]);state.preflight=checks;state.modelData=preview;applyPreviewEvidence();renderRecognition();window.preflightReady=true;}catch(e){$("recognition-global").textContent=`建模前检查读取失败：${e.message}。请运行 preview；二维图纸仍可查看。`;}
}
main().catch(showError);
