"use strict";
const $=id=>document.getElementById(id),el=(tag,text,parent)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(parent)parent.append(e);return e;};
const number=v=>v===null||v===undefined?"未确定":Number(v.toFixed(3));
let data;
function details(parent,value){const d=el("details",undefined,parent);el("summary","计算过程、前提与原始证据",d);el("pre",JSON.stringify(value,null,2),d);}
function evidenceImage(parent,source,alt){
  const img=el("img",undefined,parent),failure=el("div",undefined,parent);failure.className="image-error";failure.hidden=true;
  el("span","证据图片未加载。 ",failure);const retry=el("button","重新读取此图",failure);let timer;
  const showFailure=()=>{clearTimeout(timer);if(img.isConnected)failure.hidden=false;};
  const load=src=>{clearTimeout(timer);failure.hidden=true;img.src=src;timer=setTimeout(()=>{if(!img.complete||!img.naturalWidth)showFailure();},15000);};
  img.alt=alt;img.addEventListener("load",()=>{clearTimeout(timer);failure.hidden=true;});img.addEventListener("error",showFailure);
  retry.addEventListener("click",()=>load(`${source}?retry=${Date.now()}`));img.addEventListener("click",()=>img.classList.toggle("large"));load(source);return img;
}
function render(){
  const view=$("view").value,visible=r=>!view||r.view_id===view,title=id=>data.views.find(v=>v.id===id)?.title||id;
  $("view-summaries").replaceChildren();
  for(const s of data.view_summaries.filter(visible)){
    const tr=el("tr",undefined,$("view-summaries")),c=s.counts;
    const button=el("button",s.title,el("td",undefined,tr));button.className="view-link";
    button.addEventListener("click",()=>{$("view").value=s.view_id;render();$("view").scrollIntoView({block:"start"});});
    for(const text of [c.dimension_polygons,`${c.direction_consistent} / ${c.direction_conflicts}`,c.correspondence_pending,
      `${c.span_bom_consistent} / ${c.span_bom_checked}`,c.span_basis_pending,s.conclusion])el("td",text,tr);
  }
  $("findings").replaceChildren();
  for(const f of data.findings.filter(visible)){
    const c=el("article",undefined,$("findings"));c.className="finding";el("h3",`${title(f.view_id)} · 核对 ${Number(f.id.split("_").at(-1))}`,c);el("p",f.summary,c);
    if(f.kind==="dimension_direction_conflict"){
      el("p",f.recommended_mm!==null?`${number(f.observed_mm)} → ${number(f.recommended_mm)} mm（推荐候选）`:"当前证据不能唯一确定改哪一个尺寸",c).className="decision";
      el("p",`原标注的方向闭合残差 ${number(f.calculation.residual_norm_mm)} mm；标注取整误差界 ${number(f.calculation.rounding_bound_mm)} mm。`,c);
      if(f.decision?.preferred)el("p",`只改该值后，残差降至 ${number(f.decision.preferred.residual_mm)} mm。`,c);
      for(const t of f.independent_triangle_checks)el("p",`相邻三角形用余弦定理反算：${number(t.computed_mm)} mm；考虑标注取整为 ${t.rounding_interval_mm.map(number).join("～")} mm。`,c);
      el("p",f.calculation.assumption,c).className="hint";
      if(f.decision){
        el("h4","不同改法的计算对照",c);const wrap=el("div",undefined,c);wrap.className="table";
        const table=el("table",undefined,wrap),head=el("tr",undefined,el("thead",undefined,table)),body=el("tbody",undefined,table);
        for(const s of ["假设","方向闭合残差","相邻尺寸验算","BOM 换算","综合判断"])el("th",s,head);
        for(const h of f.decision.hypotheses){
          const tr=el("tr",undefined,body);
          const adjacent=h.triangle_checks.map(t=>t.passed===null?"不能唯一反算":`${number(t.target_mm)} mm：${t.passed?"落在反算区间内":"超出反算区间"}`).join("；")||"暂无可用依据";
          const bom=h.bom_checks.map(b=>`件号 ${b.mark}：${number(b.computed_cut_length_mm)} / BOM ${number(b.bom_length_mm)}，差 ${number(b.delta_mm)} mm`).join("；")||"暂无完整换算依据";
          const verdict=h.all_available_checks_passed?(h.changed_text_id===f.decision.preferred?.changed_text_id?"优先核对候选":"通过现有检查，仍有多解"):h.side_lengths_valid?"未通过全部检查":"边长不能构成有效多边形";
          for(const text of [h.label,`${number(h.residual_mm)} mm（${h.direction_passed?"满足":"超出"}取整界）`,adjacent,bom,verdict])el("td",text,tr);
        }
        el("p",f.decision.selection_rule,c).className="hint";
      }
    }else if(f.kind==="bom_annotation_conflict"){
      const a=f.annotation;el("p",`杆旁 ${a.specification} / ${number(a.length_mm)} mm；BOM ${a.bom_specification} / ${number(a.bom_length_mm)} mm。`,c);
      if(a.preferred_unobserved_mark)el("p",`优先核对圈号是否应为 ${a.preferred_unobserved_mark}：规格、长度吻合，且该 BOM 件号尚未找到圈号观察。`,c).className="decision";
      for(const evidence of a.current_mark_consistent_observations||[])el("p",`原圈号 ${a.mark} 在 ${title(evidence.view_id)} 的另一处观察有 ${evidence.specification} / ${number(evidence.length_mm)} mm 与 BOM 匹配，支持先核对本处圈号。`,c);
      for(const candidate of a.mark_candidates||[])el("p",`候选件号 ${candidate.mark}：BOM 数量 ${candidate.bom_quantity}；${candidate.same_view_other_member_ids.length?"当前面已有其他位置的同件号观察":"当前面未见其他同件号观察"}。`,c).className="hint";
      el("p","件号建议仍需对照引线、其他视图及实物数量确认。",c).className="hint";
    }else if(f.kind==="span_bom_conflict"){
      el("p",`${f.calculation.formula}；BOM ${f.calculation.bom_length_mm} mm。`,c);
    }
    const source=f.image||data.polygons.find(p=>p.id===f.polygon_id)?.image;
    if(source){const d=el("details",undefined,c);el("summary","查看此项原图",d);const img=evidenceImage(d,source,"此项原图证据");img.style.maxWidth="100%";img.style.maxHeight="360px";}
    details(c,f);
  }
  if(!$("findings").children.length)el("p","当前视图未生成冲突记录；未完成对应的尺寸仍在下方保留待定。",$("findings"));
  $("spans").replaceChildren();$("pending-spans").replaceChildren();
  const spans=data.span_bom_checks.filter(visible);
  for(const s of spans){
    if(!s.formula){
      const tr=el("tr",undefined,$("pending-spans"));for(const text of [`${title(s.view_id)} / ${s.mark}`,`${s.span_mm} mm`,`${s.bom_length_mm} mm`,s.pending_reasons.map(r=>r.message).join("；")])el("td",text,tr);continue;
    }
    const c=el("article",undefined,$("spans"));c.className="card";el("h3",`${title(s.view_id)} · 件号 ${s.mark}`,c);
    el("p",s.formula,c).className="decision";
    el("p",`BOM ${s.bom_length_mm} mm；差值 ${number(s.delta_mm)} mm。${s.status==="consistent_given_end_basis"?"在上述基准下吻合。":"端部基准或长度需复核。"}`,c);
    evidenceImage(c,s.image,"杆件端部正负标注原图");
    const images=el("details",undefined,c);el("summary","查看参与换算的尺寸示意图",images);
    const ids=new Set([s.polygon_id,...s.span_extensions.segments.map(e=>e.polygon_id)]);
    for(const id of ids){const p=data.polygons.find(p=>p.id===id);evidenceImage(images,p.image,p.id);}
    details(c,s);
  }
  $("pending-summary").textContent=`${spans.filter(s=>!s.formula).length} 条尺寸边与杆件对应记录缺少完整换算依据`;
  $("polygons").replaceChildren();
  for(const p of data.polygons.filter(visible)){
    const c=el("article",undefined,$("polygons"));c.className="card";el("h3",`${title(p.view_id)} · ${p.edges.length} 边示意图`,c);
    evidenceImage(c,p.image,p.id);
    el("p",p.edges.map(e=>e.value_mm===undefined?"?":number(e.value_mm)).join(" / ")+" mm",c);
    el("p",p.closure?(p.closure.status==="consistent_with_rounding"?"方向闭合与标注取整一致":"方向闭合存在冲突"):(p.labels_complete?"边长已读取；结构面对应待定":"部分边长未对应，保留待定"),c).className=p.closure?.status==="directional_inconsistency"?"bad":"tag";
    if(p.native_support_resolved_ambiguity)el("p","原始杆段位置已排除远处延长线候选；本项对应仍需复核。",c).className="tag";
    if(p.association_status==="unresolved_native_extent")el("p","候选边缺少原始杆段重叠支持，暂不据延长线给出尺寸结论。",c).className="tag";
    if(p.arithmetic?.angles_opposite_edges_deg)el("p","按三边计算的对角："+p.arithmetic.angles_opposite_edges_deg.map(v=>number(v)+"°").join(" / "),c).className="tag";
    if(p.edges.length>3)el("p","仅有多边形边长不能唯一确定形状。",c).className="tag";details(c,p);
  }
  $("annotations").replaceChildren();
  for(const a of data.annotations.filter(visible)){
    let status=a.status==="consistent"?"截面、长度一致":"冲突，需核对圈号或标注";
    if(a.association_status==="ambiguous")status+="；圈号对应有多解，尚不能确认";
    if(!a.outline_direction_agrees)status+="；已识别杆件方向不符，应复核轮廓关联";
    const tr=el("tr",undefined,$("annotations"));for(const text of [`${title(a.view_id)} / ${a.mark}`,`${a.specification} / ${a.length_mm}`,`${a.bom_specification} / ${a.bom_length_mm}`,status])el("td",text,tr);
  }
}
async function main(){try{
  const r=await fetch("report.json",{signal:AbortSignal.timeout(30000)});if(!r.ok)throw Error(`report.json: HTTP ${r.status}`);data=await r.json();
  $("title").textContent=data.title;const c=data.counts;$("summary").textContent=`${c.dimension_polygons} 个尺寸示意图 · ${c.direction_checked} 个已做方向闭合检查 · ${c.correspondence_pending} 个对应待定 · ${c.span_bom_checked} 条跨度换算 · ${c.inline_bom_checks} 条杆旁标注对照 · ${c.findings} 条初审冲突`;
  for(const v of data.views){const o=el("option",v.title,$("view"));o.value=v.id;}for(const s of data.limitations)el("li",s,$("limitations"));$("view").addEventListener("change",render);render();window.auditData=data;window.auditReady=true;
}catch(e){$("error").hidden=false;$("error").textContent=`初审数据读取失败：${e.message}\n请先运行 python -m tower_rebuild audit --out <运行目录>。`;$("summary").textContent="加载失败";}}
main();
