const editor=document.getElementById('mapping-editor'),button=document.getElementById('apply-mapping'),status=document.getElementById('mapping-status');
const job=location.pathname.match(/^\/results\/([a-f0-9]{32})\/schematic\//)?.[1];
async function request(url,options={}){
  const response=await fetch(url,{...options,signal:AbortSignal.timeout(30000)});
  const data=await response.json();if(!response.ok)throw Error(data.error||`HTTP ${response.status}`);return data;
}
document.getElementById('mapping-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  try{if(file.size>1024*1024)throw Error('对应文件不能超过 1 MB。');const value=JSON.parse(await file.text());editor.value=JSON.stringify(value,null,2);status.textContent='已读取，请核对后提交。';}
  catch(error){status.textContent=error.message;}
});
button.addEventListener('click',async()=>{
  button.disabled=true;status.textContent='正在创建独立检查任务…';
  try{
    const mapping=JSON.parse(editor.value);if(!Array.isArray(mapping.bindings)||!mapping.bindings.length)throw Error('请先填写至少一条 bindings 对应记录。');
    const session=await request('/api/session');
    const result=await request(`/api/jobs/${job}/schematic-review`,{method:'POST',headers:{'Content-Type':'application/json','X-Upload-Token':session.token},body:JSON.stringify(mapping)});
    status.textContent=`已创建任务 ${result.id}，原结果保留。`;
    const link=document.createElement('a');link.href='/';link.textContent='返回工作台查看处理进度';status.append(' ',link);
  }catch(error){status.textContent=error.message;}
  finally{button.disabled=!job;}
});
request('mapping.json').then(mapping=>{editor.value=JSON.stringify(mapping,null,2);button.disabled=!job;if(!job)status.textContent='静态结果：可下载对应记录，通过 audit --schematic-map 在命令行应用。';}).catch(error=>{status.textContent=`对应记录读取失败：${error.message}`;});
