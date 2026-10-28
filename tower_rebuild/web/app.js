/* CPU canvas renderer: no CDN, module loader, or WebGL driver dependency. */
'use strict';
const $ = id => document.getElementById(id);
const text = (node, value) => {node.textContent = String(value);};
const sub = (a,b) => a.map((v,i)=>v-b[i]);
const dot = (a,b) => a.reduce((s,v,i)=>s+v*b[i],0);
const norm = a => Math.hypot(...a);
let data, report, selected=null, face='all', yaw=-.7, pitch=.45, zoom=1, projected=[], drag=null;
const colors=['#17638e','#168c83','#8754a1','#af7c2d','#436daa','#ad5e6d'];
const canvas=$('model'), ctx=canvas.getContext('2d');
function showError(error){$('error').hidden=false;text($('error'),'本地数据读取失败：'+error.message+'\n请通过 tower-rebuild serve 启动页面，并查看运行目录中的 failure.json。');text($('status'),'加载失败');}
async function readJson(url){const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),15000);try{const response=await fetch(url,{cache:'no-store',signal:controller.signal});if(!response.ok)throw new Error(url+' HTTP '+response.status);return await response.json();}finally{clearTimeout(timeout);}}
function currentMembers(){return data.members.filter(m=>face==='all'||m.faces.includes(face));}
function project(p){if(face!=='all'){const f=data.faces.find(f=>f.id===face);if(f?.pose){const q=sub(p,f.pose.origin),r=f.pose.matrix;return [dot(q,r.map(row=>row[0])),dot(q,r.map(row=>row[1])),dot(q,r.map(row=>row[2]))];}}
  const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);const x=cy*p[0]-sy*p[1],d=sy*p[0]+cy*p[1];return [x,cp*p[2]-sp*d,cp*d+sp*p[2]];
}
function draw(){if(!data||!ctx)return;const rect=canvas.getBoundingClientRect(),dpr=devicePixelRatio||1,w=rect.width,h=rect.height;canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
  const members=currentMembers();projected=[];if(!members.length){ctx.fillStyle='#647b8c';ctx.font='15px sans-serif';ctx.fillText('尚无可装配的杆件，请查看下方核对项。',30,h/2);return;}
  const pairs=members.map(m=>({m,a:project(m.start),b:project(m.end)})),points=pairs.flatMap(p=>[p.a,p.b]);let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;for(const p of points){minX=Math.min(minX,p[0]);maxX=Math.max(maxX,p[0]);minY=Math.min(minY,p[1]);maxY=Math.max(maxY,p[1]);}
  const scale=Math.min((w-110)/Math.max(maxX-minX,1),(h-100)/Math.max(maxY-minY,1))*zoom,cx=(minX+maxX)/2,cy=(minY+maxY)/2;
  const pixel=p=>[(p[0]-cx)*scale+w/2,h/2-(p[1]-cy)*scale];
  pairs.sort((a,b)=>(a.a[2]+a.b[2])-(b.a[2]+b.b[2]));ctx.lineCap='round';
  for(const p of pairs){const a=pixel(p.a),b=pixel(p.b);const active=selected===p.m.id;ctx.strokeStyle=active?'#e36d2e':colors[Math.max(0,data.faces.findIndex(f=>f.id===p.m.faces[0]))%colors.length];ctx.lineWidth=active?6:3;ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke();ctx.fillStyle=active?'#e36d2e':'#738b9c';for(const pt of [a,b]){ctx.beginPath();ctx.arc(...pt,2.5,0,2*Math.PI);ctx.fill();}projected.push({a,b,m:p.m});
    if($('labels').checked){const x=(a[0]+b[0])/2,y=(a[1]+b[1])/2;ctx.font=(active?'bold ':'')+'11px sans-serif';const tw=ctx.measureText(p.m.mark).width;ctx.fillStyle='#ffffffdf';ctx.fillRect(x-tw/2-3,y-13,tw+6,15);ctx.fillStyle=active?'#bb4917':'#243f53';ctx.fillText(p.m.mark,x-tw/2,y-2);}}
}
function select(id){selected=id;const m=data.members.find(m=>m.id===id);if(m){text($('selection'),`件号 ${m.mark} · 实例 ${m.id} · ${m.section} · BOM 下料 ${m.cut_length_mm} mm · 节点跨度 ${m.axis_length_mm.toFixed(2)} mm · ${m.end_offsets_mm?'端偏移 '+m.end_offsets_mm.join(' / ')+' mm':'端部偏移未确定'}`);}renderList();draw();}
function renderList(){const query=$('search').value.trim().toLowerCase();$('members').replaceChildren();for(const m of currentMembers().filter(m=>(m.id+' '+m.mark).toLowerCase().includes(query))){const b=document.createElement('button');text(b,m.mark+' · '+m.id);b.classList.toggle('selected',m.id===selected);b.onclick=()=>select(m.id);$('members').append(b);}}
function showSource(){const f=data.faces.find(v=>v.id===face);$('source-panel').hidden=!f?.source_image;if(f?.source_image){text($('source-title'),f.overlay_kind==='observations'?'原图 / 橙色二维识别结果（尚未形成合格三维）':'原图 / 绿色三维模型投影');$('source').src=$('overlay').checked&&f.overlay_image?f.overlay_image:f.source_image;}}
function renderReport(){text($('title'),data.title);document.title=data.title+' · 本地核对';const labels={inconsistent:'三维校验未通过',needs_review:'约束通过 · 待原图核对',reviewed_frame:'已核对杆件框架'};text($('status'),labels[data.status]||data.status);$('status').dataset.state=data.status;
  const angles=data.bom.filter(r=>r.classification.kind==='angle'),required=angles.reduce((sum,r)=>sum+r.quantity,0);
  text($('stats'),`BOM 需要 ${required} 根 · 已生成 ${data.members.length} 根轴线 · ${data.faces.length} 个结构面 · ${angles.length} 种角钢件号`);
  $('axes-download').hidden=!data.members.length;
  if(!data.members.length)text($('selection'),'识别与装配数据尚未通过校验。请选择结构面，对照原图及橙色二维识别结果；尚未生成三维杆件。');
  for(const f of data.faces){const o=document.createElement('option');o.value=f.id;text(o,f.title);$('face').append(o);}
  const names={unresolved_observation:'识别及视觉复核待确认',quantity_mismatch:'BOM 数量不符',mark_mismatch:'件号对应冲突',observation_outside_face:'节点超出结构面',unknown_callout:'件号未对应 BOM',edge_reused:'同一观察边重复使用',shared_member_conflict:'共享杆件端点冲突',unmapped_joint:'节点未装配',scale_conflict:'尺寸比例冲突',missing_scale:'缺少尺寸比例依据',unmapped_dimension:'尺寸端点未装配',instance_collapse:'不同杆件被合并',disconnected_faces:'结构面尚未连接',review_required:'原图核对'};
  const groups=new Map();for(const item of data.issues){const key=item.code+'|'+item.severity;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item);}
  for(const items of groups.values()){const group=document.createElement('li'),details=document.createElement('details'),summary=document.createElement('summary'),list=document.createElement('ul');group.className=items[0].severity;text(summary,`${names[items[0].code]||items[0].code}：${items.length} 项`);for(const item of items){const li=document.createElement('li');text(li,item.message);list.append(li);}details.append(summary,list);group.append(details);$('issues').append(group);}
  const solver=report.solver||{},checks=solver.checks||[];for(const v of [checks.length?`${checks.filter(c=>c.pass).length} / ${checks.length} 约束通过`:'几何求解尚未通过',`剩余自由度：${solver.degrees_of_freedom??'未求解'}`,`${data.issues.filter(i=>i.severity==='error').length} 项阻断问题`]){const s=document.createElement('span');text(s,v);$('metrics').append(s);}
  for(const row of checks){const tr=document.createElement('tr');tr.classList.toggle('fail',!row.pass);for(const value of [row.kind,row.ref,row.error.toFixed(4)+' '+row.unit,row.tolerance,row.pass?'通过':'超差']){const td=document.createElement('td');text(td,value);tr.append(td);}$('checks').append(tr);}
}
$('face').onchange=()=>{face=$('face').value;zoom=1;text($('view-title'),face==='all'?'整体三维':data.faces.find(f=>f.id===face).title+(data.members.length?' · 正投影':' · 原图核对'));showSource();renderList();draw();};
$('overlay').onchange=showSource;$('labels').onchange=draw;$('search').oninput=renderList;$('reset').onclick=()=>{yaw=-.7;pitch=.45;zoom=1;draw();};
canvas.onpointerdown=e=>{drag={x:e.clientX,y:e.clientY,total:0};canvas.setPointerCapture(e.pointerId);};
canvas.onpointermove=e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;drag.total+=Math.abs(dx)+Math.abs(dy);if(face==='all'){yaw+=dx*.008;pitch=Math.max(-1.5,Math.min(1.5,pitch+dy*.008));}drag.x=e.clientX;drag.y=e.clientY;draw();};
canvas.onpointerup=e=>{if(drag&&drag.total<5){const rect=canvas.getBoundingClientRect(),p=[e.clientX-rect.left,e.clientY-rect.top];let closest=null,best=10;for(const edge of projected){const v=sub(edge.b,edge.a),t=Math.max(0,Math.min(1,dot(sub(p,edge.a),v)/Math.max(dot(v,v),.0001)));const d=norm(sub(p,edge.a.map((x,i)=>x+t*v[i])));if(d<best){best=d;closest=edge.m;}}if(closest)select(closest.id);}drag=null;};canvas.onpointercancel=()=>{drag=null;};
canvas.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(.3,Math.min(8,zoom*Math.exp(-e.deltaY*.001)));draw();},{passive:false});
window.addEventListener('resize',draw);window.addEventListener('error',e=>showError(e.error||new Error(e.message)));
Promise.all([readJson('model.json'),readJson('report.json')]).then(([m,r])=>{data=m;report=r;if(!ctx)throw new Error('浏览器不支持 Canvas 2D');renderReport();renderList();draw();window.towerViewerReady=true;}).catch(showError);
