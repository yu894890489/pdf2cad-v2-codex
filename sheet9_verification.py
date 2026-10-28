"""Render the source views and a source/model front-projection overlay."""
from pathlib import Path
import base64
import html
import fitz
from sheet9_geometry import SCALE, X_SCALE, ORIGIN

def write_reference_assets(site, out, members, report):
    source=next(Path('图纸/1').rglob('*_9.pdf'))
    doc=fitz.open(source);page=doc[0]
    regions={
        'front_detail':(.12,.35,.59,.62),
        'upper_detail':(.17,.07,.56,.445),
        'lower_detail':(.12,.63,.59,.965),
        'detail_1':(.55,.065,.68,.265),
        'detail_2':(.415,.05,.54,.17),
        'detail_3':(.59,.33,.68,.48),
        'single_line':(.045,.035,.205,.155),
    }
    refdir=site/'reference';refdir.mkdir(exist_ok=True)
    for name,b in regions.items():
        clip=fitz.Rect(b[0]*page.rect.width,b[1]*page.rect.height,b[2]*page.rect.width,b[3]*page.rect.height)
        pix=page.get_pixmap(matrix=fitz.Matrix(3,3),clip=clip)
        pix.save(refdir/(name+'.png'))
        if name=='single_line':front=pix
    image=base64.b64encode(front.tobytes('png')).decode()
    lines=[]
    for m in members:
        if 'front' not in m['faces']:continue
        pts=[((ORIGIN[0]+(p[0]-1682)/X_SCALE)*3-front.x,
              (ORIGIN[1]-p[2]/SCALE)*3-front.y) for p in [m['axisStart'],m['axisEnd']]]
        (x1,y1),(x2,y2)=pts
        lines.append(f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}"><title>件号 {m["mark"]}</title></line>')
    svg=(f'<svg xmlns="http://www.w3.org/2000/svg" width="{front.width}" height="{front.height}" viewBox="0 0 {front.width} {front.height}">'
         f'<image width="100%" height="100%" href="data:image/png;base64,{image}"/>'
         '<g fill="none" stroke="#00a47d" stroke-width="3" opacity=".8">'+''.join(lines)+
         '</g><text x="24" y="600" fill="#087b61" font-size="18" font-family="Microsoft YaHei">绿色：模型正投影连接轴线；黑色：原图。虚线未建模。</text></svg>')
    (refdir/'front_overlay.svg').write_text(svg,encoding='utf-8')
    (out/'front_overlay.svg').write_text(svg,encoding='utf-8')
    dimensions=''.join(f'<tr><td>{html.escape(d["name"])}</td><td>{d["drawingMm"]}</td><td>{d["modelMm"]}</td><td>{abs(d["drawingMm"]-d["modelMm"]):.3f}</td></tr>' for d in report['dimensionChecks'])
    face_checks=[
        ('front','正面','13 条实线建立轮廓，叠加原图检查；虚线剔除。'),
        ('rear','背面','按“前 / 后”件号对应，与正面共用相同纵向节点。'),
        ('bottom','底面','301/302 两侧底弦；331/332 根部 V、372 短撑；333–338 三组 X；339–344 端部框架。'),
        ('upper','上斜面','以 303/304 拼接；354/355、356/357 两组 X；358 横杆。'),
        ('detail1','1—1','312/313 边杆、362/363 交叉杆和 364 横杆；通过共用实例连接两侧。'),
        ('detail2','2—2','315/316 侧边、364 顶边、341 底边、两根 367 的 V 形撑。'),
        ('detail3','3—3','沿两根 317；370/371 连接点分别内退 126 / 274 mm，避免把整根边杆当成 X 框架范围。'),
    ]
    face_rows=''.join(f'<tr><td><a href="./?view={key}">{title}</a></td><td>{description}</td></tr>' for key,title,description in face_checks)
    review='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>横担逐面核对记录</title><style>body{font:15px/1.7 system-ui,"Microsoft YaHei";color:#243c40;background:#edf3f3;margin:0}main{max-width:1000px;margin:32px auto;padding:32px;background:white;border-radius:12px}h1{font-size:28px}h2{margin-top:32px;font-size:19px}a{color:#087b61}table{border-collapse:collapse;width:100%;margin-top:16px}td,th{padding:10px 12px;border-bottom:1px solid #d7e4e3;text-align:left}th{background:#eaf4f0}img{width:100%;border:1px solid #d7e4e3}.note{padding:16px;background:#f1f5f4;border-left:3px solid #4da68e}@media(max-width:700px){main{margin:0;padding:16px}td,th{padding:8px;font-size:12px}}</style><main><a href="./">← 返回三维组装</a><h1>第 9 张 · 横担逐面核对记录</h1><p>49 个角钢件号 · 58 根杆件 · 7 个结构面 · 33 种板 / 垫块 / 焊接件排除</p><p>依据原图 BOM、单线图实线、圆圈件号和共用杆件拼合。379/380 已纠正为连接板。</p><h2>正视轴线叠加</h2><p>绿色为三维模型正投影，黑色为原图。左侧塔身与变形虚线不参与此次建模。</p><img src="reference/front_overlay.svg" alt="正视图实线叠加核对"><h2>图上主要尺寸（mm）</h2><table><thead><tr><th>尺寸</th><th>图纸</th><th>模型</th><th>差值</th></tr></thead><tbody>'''+dimensions+'''</tbody></table><h2>逐面连接检查</h2><table><thead><tr><th>结构面</th><th>核对内容（点击名称打开原图对照）</th></tr></thead><tbody>'''+face_rows+'''</tbody></table><h2>长度与连接</h2><p>所有实体长度保留 BOM 下料长；连接轴线延伸至结构节点。板件省略后，实体端部会留隙，不能把轴线跨度直接当成下料长。</p><p>程序检查了七个面的共面性、12 处共用杆件、BOM 数量和长度；浏览器逐面核对了轮廓、交叉方式与件号。原图上的不同面按各自观察方向展示，底面方向与上斜面相反。</p><p class="note">这是杆件布局核对模型。数值残差用于检查映射和组装，不能作为施工或加工精度证明。孔位、角钢朝向、端部切角与火曲尚未建模。</p><p><a href="bom_classified.csv">BOM 分类</a> · <a href="geometry_audit.csv">长度与节点表</a> · <a href="validation.json">详细检查数据</a></p></main></html>'''
    (site/'review.html').write_text(review,encoding='utf-8')
    doc.close()
