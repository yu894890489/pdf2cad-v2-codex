const $=id=>document.getElementById(id),el=(tag,text,parent)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(parent)parent.append(e);return e;};
const labels={pass:'本项检查通过',fail:'存在疑点或差异',pending:'待核对',not_requested:'未启用'};
async function main(){
  const response=await fetch('report.json',{cache:'no-store',signal:AbortSignal.timeout(20000)});if(!response.ok)throw Error(`HTTP ${response.status}`);const report=await response.json();
  await Promise.all(['topology/model-preview.json','audit/report.json','model-audit/report.json'].map(async name=>{const r=await fetch('../'+name,{cache:'no-store',signal:AbortSignal.timeout(20000)});if(!r.ok)throw Error(`数据缺失：${name}`);const bytes=await r.arrayBuffer(),hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),v=>v.toString(16).padStart(2,'0')).join('');if(hash!==report.input_files[name])throw Error('模型或检查数据已改变，终检已过期；请重新执行 final-check。');}));
  $('title').textContent=report.title;$('summary').textContent=report.model_generated?'候选三维已生成 · 工程准确性尚未确认':'当前证据不足以生成三维实体';
  for(const key of ['pass','fail','pending','not_requested']){const card=el('div',undefined,$('metrics'));card.className='metric';el('strong',String(report.counts[key]||0),card);el('span',labels[key],card);}
  for(const check of report.checks){const tr=el('tr',undefined,$('checks'));tr.dataset.checkId=check.id;el('td',check.title,tr);el('td',labels[check.status],tr).className=check.status;el('td',check.message,tr);const a=el('a','查看依据',el('td',undefined,tr));a.href=check.evidence_url;}
  report.requirements.forEach(text=>el('li',text,$('requirements')));window.finalCheckReady=true;
}
main().catch(error=>{$('summary').textContent='终检未加载';$('error').hidden=false;$('error').textContent=error.message;});
