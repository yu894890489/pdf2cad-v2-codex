const aid=location.pathname.split('/')[2],$=id=>document.getElementById(id);
const endpoint=`/api/assemblies/${aid}/part-drawings`;
$('assembly').href=`/assemblies/${aid}/`;$('bom').href=`/assemblies/${aid}/final-bom/`;
let revision,token,busy=false,timer,shown,submitError='';
const labels={not_started:'尚未生成',queued:'等待本地 CAD 转换',running:'正在生成并逐份回读验证 DWG',complete:'DWG 已全部生成并通过回读验证',partial:'部分 DWG 未生成，请查看清单',needs_converter:'需要配置 AutoCAD Core Console',blocked:'没有成功输出 DWG，请查看未生成原因',interrupted:'上次生成中断，请重试',failed:'生成失败'};
async function request(url,options={}){const response=await fetch(url,{...options,signal:AbortSignal.timeout(60000)});const data=await response.json();if(!response.ok)throw Error(data.error||`请求失败 ${response.status}`);return data;}
async function refresh(){
  clearTimeout(timer);
  try{
    const data=await request(endpoint);revision=data.current_revision;
    const active=['queued','running'].includes(data.status);
    $('generate').disabled=busy||active;
    $('status').textContent=`${labels[data.status]||data.status} · 当前模型修订 ${revision}${data.total?` · ${data.completed}/${data.total}`:''}`;
    $('progress').hidden=!active;$('progress').max=data.total||1;$('progress').value=data.completed||0;
    $('stale').hidden=!data.stale;$('error').hidden=!(data.error||submitError);$('error').textContent=data.error||submitError;
    const finished=['complete','partial','blocked','needs_converter'].includes(data.status)&&data.result_url;
    if(finished){
      $('result').href=data.result_url;$('result').hidden=false;
      $('download').href=data.result_url.replace('index.html','parts-dwg.zip');$('download').hidden=!data.counts?.verified_dwg;
      if(shown!==data.result_url){$('preview').src=data.result_url;$('preview').hidden=false;shown=data.result_url;}
    }
  }catch(e){$('error').hidden=false;$('error').textContent=e.message;$('generate').disabled=false;}
  timer=setTimeout(refresh,3000);
}
$('generate').onclick=async()=>{busy=true;submitError='';$('generate').disabled=true;$('error').hidden=true;try{await request(endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-Upload-Token':token},body:JSON.stringify({revision})});}catch(e){submitError=e.message;$('error').hidden=false;$('error').textContent=e.message;}finally{busy=false;await refresh();}};
try{token=(await request('/api/session')).token;await refresh();}catch(e){$('error').hidden=false;$('error').textContent=e.message;}
