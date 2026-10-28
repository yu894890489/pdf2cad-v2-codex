'use strict';
const $=id=>document.getElementById(id);
const labels={queued:'等待处理',running:'正在处理',needs_review:'已生成 · 待复核',no_model:'证据不足 · 未生成实体',failed:'处理失败',unsupported:'需要补充输入',interrupted:'处理已中断'};
let selected=[],session=null,busy=false,pollTimer=null;
function node(tag,text,parent,className){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;if(parent)parent.append(n);return n;}
function error(message){$('error').hidden=!message;$('error').textContent=message||'';}
async function request(path,options={}){const response=await fetch(path,{...options,signal:AbortSignal.timeout(30000)});let data;try{data=await response.json();}catch{throw Error(`本地服务返回了无效数据（HTTP ${response.status}），请检查服务是否仍在运行。`);}if(!response.ok)throw Error(data.error||`HTTP ${response.status}`);return data;}
function selectFiles(files){if(busy)return;selected=Array.from(files);$('upload').disabled=!selected.length;$('selected-files').replaceChildren();for(const f of selected)node('li',`${f.name} · ${(f.size/1024/1024).toFixed(2)} MB`,$('selected-files'));$('selection').hidden=!selected.length;$('upload-status').textContent='';$('upload-progress').hidden=true;}
function link(parent,text,url,primary=false){const a=node('a',text,parent,'action'+(primary?' primary':''));a.href=url;a.target='_blank';a.rel='noopener';return a;}
function renderJobs(jobs){
  if(session?.profile_name)$('profile-mode').textContent=`当前配置：${session.profile_name} · ${{disabled:'视觉模型未启用',local:'本地视觉复核',remote:'已启用外部视觉服务，结构面裁剪和 BOM 将发送至配置的服务'}[session.vision_mode]||''}`;
  if(session?.vision_mode==='remote')$('runtime-label').textContent='本地建模 · 外部视觉复核';
  $('jobs').replaceChildren();const active=jobs.filter(j=>['queued','running'].includes(j.status)).length;
  $('summary').textContent=`共 ${jobs.length} 张图纸${active?` · ${active} 张等待或处理中`:' · 结果保存在本机'}。`;
  if(!jobs.length){node('div','还没有图纸。上传 PDF 后，可在这里查看处理进度和结果。',$('jobs'),'empty');return;}
  for(const j of jobs){
    const card=node('article',undefined,$('jobs'),'job');card.dataset.jobId=j.id;card.dataset.status=j.status;
    const header=node('div',undefined,card,'job-head'),title=node('div',undefined,header);
    node('h3',j.name,title);node('div',`${new Date(j.created_at).toLocaleString()} · ${j.id.slice(0,8)}${j.retry_of?' · 重试记录':''}`,title,'meta');node('span',labels[j.status]||j.status,header,'badge '+j.status);
    const w=j.workflow;
    if(w?.stages){const list=node('ol',undefined,card,'steps');for(const [i,s] of w.stages.entries())node('li',`${i+1}. ${s.label}`,list,s.status);}
    if(j.error||w?.error?.message)node('p',j.error||w.error.message,card,'job-message issue');
    else {const active=w?.stages?.find(s=>s.status==='running'),elapsed=active?Math.max(0,Math.round((Date.now()-new Date(active.started_at).getTime())/1000)):0;node('p',j.status==='queued'?'已保存图纸，等待前面的任务完成。':j.status==='running'?`${active?.label||'准备处理'}${active?` · 本阶段已用 ${elapsed} 秒`:''}。页面会自动更新进度。`:w?.message||'可打开下方结果进行检查。',card,'job-message');}
    if(w?.counts){const c=w.counts,row=node('div',undefined,card,'counts');for(const [value,label] of [[w.bom_rows,'个 BOM 件号'],[w.views,'个视图'],[c.angle_models,'条角钢观察模型'],[c.plate_polygons,'个板形'],[c.hole_projections,'个孔位投影']]){const span=node('span',undefined,row);node('b',String(value),span);span.append(label);}
      const missing=w.missing_member_marks||[];node('p',`待复核：板形 ${c.plate_shapes_pending} 条；杆端未完整 ${c.member_incomplete} 条；明确孔径 ${c.explicit_hole_diameters}/${c.hole_projections}；未找到角钢件号 ${missing.join('、')||'无'}。`,card,'job-message issue');
    }
    if(w?.input_coverage?.issues.length){const detail=node('details',undefined,card);node('summary',`有 ${w.input_coverage.pages_with_coverage_issues} 页尚未完整覆盖`,detail);const list=node('ul',undefined,detail,'job-message issue');for(const issue of w.input_coverage.issues)node('li',issue.message,list);}
    if(w?.bom_integrity?.status==='blocked'){const detail=node('details',undefined,card);detail.open=true;node('summary','BOM 原始行需要核对',detail);const list=node('ul',undefined,detail,'job-message issue');for(const issue of w.bom_integrity.issues)node('li',issue.message,list);}
    const actions=node('div',undefined,card,'actions');
    for(const [key,text] of [['topology','打开图纸与三维'],['final','全流程终检'],['ocr','OCR 文字与笔画'],['tables','BOM 与图元'],['audit','尺寸初审'],['modelaudit','三维尺寸复核'],['report','检查报告'],['inputreport','输入检查'],['bom','BOM 数组'],['bomrows','BOM 原始行'],['model','模型数据'],['source','原 PDF'],['log','运行日志']])if(j.links[key])link(actions,text,j.links[key],key==='topology');
    if(!['running','queued'].includes(j.status)){const retry=node('button','重新处理',actions);retry.type='button';retry.addEventListener('click',async()=>{retry.disabled=true;try{await request(`/api/jobs/${j.id}/retry`,{method:'POST',headers:{'X-Upload-Token':session.token}});error('');await refresh();}catch(e){error(e.message);retry.disabled=false;}});}
  }
}
async function refresh(){clearTimeout(pollTimer);try{const {jobs}=await request('/api/jobs');renderJobs(jobs);if($('error').dataset.network==='true'){error('');delete $('error').dataset.network;}pollTimer=setTimeout(refresh,jobs.some(j=>['queued','running'].includes(j.status))?1500:10000);}catch(e){error(`无法读取本地记录：${e.message} 可点击“刷新记录”重试。`);$('error').dataset.network='true';pollTimer=setTimeout(refresh,10000);}}
function uploadFile(file,index,total){return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/jobs');xhr.setRequestHeader('Content-Type','application/pdf');xhr.setRequestHeader('X-File-Name',encodeURIComponent(file.name));xhr.setRequestHeader('X-Upload-Token',session.token);xhr.timeout=120000;
  xhr.upload.onprogress=e=>{if(e.lengthComputable){$('upload-progress').value=e.loaded/e.total*100;$('upload-status').textContent=`上传 ${index}/${total}：${file.name} · ${Math.round(e.loaded/e.total*100)}%`;}};
  xhr.onload=()=>{let data;try{data=JSON.parse(xhr.responseText);}catch{return reject(Error('上传响应无效，请检查本地服务。'));}if(xhr.status<200||xhr.status>=300)return reject(Error(data.error||`上传失败 HTTP ${xhr.status}`));resolve(data);};
  xhr.onerror=()=>reject(Error('上传失败，请确认本地服务仍在运行。'));xhr.ontimeout=()=>reject(Error('上传超时，请重试。'));xhr.send(file);
});}
$('pick').addEventListener('click',e=>{e.stopPropagation();$('files').click();});$('drop-zone').addEventListener('click',()=>$('files').click());$('drop-zone').addEventListener('keydown',e=>{if(e.target===$('drop-zone')&&['Enter',' '].includes(e.key)){e.preventDefault();$('files').click();}});$('files').addEventListener('change',e=>selectFiles(e.target.files));
for(const event of ['dragenter','dragover'])$('drop-zone').addEventListener(event,e=>{e.preventDefault();$('drop-zone').classList.add('drag');});for(const event of ['dragleave','drop'])$('drop-zone').addEventListener(event,e=>{e.preventDefault();$('drop-zone').classList.remove('drag');});$('drop-zone').addEventListener('drop',e=>selectFiles(e.dataTransfer.files));
$('upload').addEventListener('click',async()=>{if(busy||!selected.length)return;busy=true;$('upload').disabled=true;$('pick').disabled=true;error('');let uploaded=0;try{session=await request('/api/session');for(const file of selected){if(!/\.pdf$/i.test(file.name)||!file.size||file.size>session.max_upload_bytes)throw Error(`${file.name}：请选择不超过 100 MB 的 PDF 文件。`);}$('upload-progress').hidden=false;for(const [i,file] of selected.entries()){await uploadFile(file,i+1,selected.length);uploaded++;await refresh();}$('upload-status').textContent=`已保存 ${uploaded} 张图纸，可在下方查看进度和结果。`;selected=[];$('files').value='';}catch(e){error(`${uploaded?`已保存 ${uploaded} 张。`:''}${e.message}`);selected=selected.slice(uploaded);}finally{busy=false;$('upload').disabled=!selected.length;$('pick').disabled=false;}});
$('refresh').addEventListener('click',async()=>{try{session=await request('/api/session');error('');await refresh();}catch(e){error(e.message);}});
(async()=>{try{session=await request('/api/session');await refresh();window.studioReady=true;}catch(e){error(`本地工作台读取失败：${e.message}`);}})();
