// Shared by the upload workbench and the assembly viewer.
const entries=[
  ['assembly','打开多面装配'],['automaticassembly','自动装配与补全报告'],
  ['finalbom','最终模型 BOM',true],
  ['partdrawings','零件放样 DWG',true],
  ['topology','打开图纸与三维'],['final','全流程终检'],['ocr','OCR 文字与笔画',true],
  ['tables','BOM 与图元'],['audit','尺寸初审'],['schematic','单线图尺寸检查',true],['modelaudit','三维尺寸复核'],
  ['report','检查报告'],['inputreport','输入检查'],['bom','BOM 数组'],
  ['bomrows','BOM 原始行',true],['bomcheck','BOM 检查',true],
  ['model','模型数据'],['source','原 PDF'],['log','运行日志']
];

export function renderDrawingActions(container,job,{includeAssembly=false,showUnavailable=true,onRetry=null}={}){
  container.replaceChildren();container.classList.add('drawing-actions');
  const active=job&&['queued','running'].includes(job.status);
  for(const [key,label,optional] of entries){
    if(key==='assembly'&&!includeAssembly)continue;
    const url=job?.links?.[key];
    if(!url&&(optional||!showUnavailable))continue;
    const element=document.createElement(url?'a':'button');
    element.textContent=label;element.dataset.resultKey=key;element.className='action'+(key==='topology'&&url?' primary':'');
    if(url){element.href=url;element.target='_blank';element.rel='noopener';}
    else{element.disabled=true;element.title=!job?'正在读取图纸结果':active?'图纸处理中，此项结果尚未生成':'该图纸尚未生成此项结果，请查看输入检查或运行日志';}
    container.append(element);
  }
  const retry=document.createElement('button');retry.type='button';retry.textContent='重新处理';retry.dataset.resultKey='retry';
  retry.disabled=!job||active||!onRetry;
  if(!onRetry)retry.title='请在本地图纸工作台中重新处理';
  retry.onclick=async()=>{retry.disabled=true;retry.textContent='正在提交…';try{await onRetry(job);}finally{retry.disabled=!!active;retry.textContent='重新处理';}};
  container.append(retry);
}
