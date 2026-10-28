"""Rebuild sheet 9 from its solid front graph and shared face piece marks."""
from pathlib import Path
import csv, json, math, shutil
from collections import Counter
import ezdxf
from sheet9_geometry import make_geometry, cut_endpoints, FACE_INFO, FRONT_SEGMENTS, front_coord
OUT=Path('output/sheet9');OUT.mkdir(parents=True,exist_ok=True)
SITE=Path('site/tower-rods/dist')
BOM = {
  301:('L125x10',7025,1),302:('L125x10',7025,1),303:('L125x10',5485,1),304:('L125x10',5485,1),
  305:('L50x4',2317,1),306:('L50x4',2317,1),307:('L56x4',1650,1),308:('L56x4',1650,1),
  309:('L63x5',1503,1),310:('L63x5',1503,1),311:('L40x3',558,2),
  312:('L70x5',1624,1),313:('L70x5',1624,1),314:('L70x5',606,2),
  315:('L63x5',477,1),316:('L63x5',477,1),317:('L56x4',1106,2),318:('L40x3',885,2),
  319:('L40x3',1339,1),320:('L40x3',1339,1),321:('L40x3',1271,2),
  331:('L63x5',1859,1),332:('L63x5',1859,1),333:('L63x5',3068,1),334:('L63x5',3068,1),
  335:('L63x5',2663,1),336:('L63x5',2663,1),337:('L63x5',2100,1),338:('L63x5',2100,1),
  339:('L75x5',1430,1),340:('L56x5',640,2),341:('L75x5',1258,1),342:('L40x4',1366,2),
  343:('L100x8',1062,1),344:('L100x8',1009,1),
  354:('L63x5',3443,1),355:('L63x5',3443,1),356:('L63x5',3116,1),357:('L63x5',3116,1),
  358:('L50x4',1961,1),359:('L125x10',705,1),360:('L125x10',705,1),
  362:('L63x5',2147,1),363:('L63x5',2147,1),364:('L45x4',1253,1),
  367:('L63x5',816,2),370:('L40x3',1615,1),371:('L40x3',1615,1),372:('L40x3',1115,2),
}

# Remaining numbered BOM rows: plate stock, shims and the welded ring. These
# remain in the audit so plate callouts cannot be mistaken for angle members.
NON_RODS = {
  322:('Q345-8x279',466,1),323:('Q345-8x279',466,1),
  324:('Q345-6x207',357,2),325:('-6x218',295,2),
  326:('Q345-12x385',693,1),327:('Q345-12x385',693,1),
  328:('Q345-6x220',241,2),329:('-5x228',365,1),330:('-5x228',365,1),
  345:('-6x182',224,2),346:('Q345-6x179',254,2),347:('Q345-6x179',269,2),
  348:('-6x150',319,1),349:('Q345-22x460',470,1),350:('Q345-22x460',470,1),
  351:('Q345-22x460',470,1),352:('Q345-22x460',470,1),353:('Q345-10x304',383,2),
  361:('Q345-8x380',403,1),365:('-6x135',165,2),366:('-6x221',228,2),
  368:('-6x232',250,2),369:('-6x209',340,1),
  373:('Q345-d60/d38',34,4),374:('-16x50',50,1),375:('-10x60',60,6),
  376:('-16x60',60,3),377:('-18x60',60,1),378:('-10x60',120,2),
  379:('Q345-12x643',885,1),380:('Q345-12x643',885,1),
  381:('Q345-12x570',772,1),382:('Q345-12x570',772,1),
}
assert set(BOM).isdisjoint(NON_RODS)
assert set(BOM)|set(NON_RODS)==set(range(301,383))


P,M=make_geometry()
counts=Counter(m['mark'] for m in M)
assert counts==Counter({mark:row[2] for mark,row in BOM.items()}),counts
assert not(set(counts)&set(NON_RODS))
doc=ezdxf.new('R2013',setup=True);doc.header['$INSUNITS']=4
msp=doc.modelspace()
for layer,color in [('MEMBER_AXIS_MARK',8),('ANGLE_FACES',3)]:
    doc.layers.new(layer,dxfattribs={'color':color})
def add_angle(a, b, width, thick, layer):
    # Sweep an L outline using a 3D orthonormal frame, including verticals.
    axis=[b[i]-a[i] for i in range(3)]
    length=math.sqrt(sum(v*v for v in axis))
    if length<1: return
    axis=[v/length for v in axis]
    ref=(0,0,1) if abs(axis[2])<.9 else (0,1,0)
    u=[axis[1]*ref[2]-axis[2]*ref[1],axis[2]*ref[0]-axis[0]*ref[2],axis[0]*ref[1]-axis[1]*ref[0]]
    un=math.sqrt(sum(v*v for v in u));u=[v/un for v in u]
    v=[axis[1]*u[2]-axis[2]*u[1],axis[2]*u[0]-axis[0]*u[2],axis[0]*u[1]-axis[1]*u[0]]
    section=[(0,0),(width,0),(width,thick),(thick,thick),(thick,width),(0,width)]
    def vertex(origin,s,t): return tuple(origin[i]+u[i]*s+v[i]*t for i in range(3))
    aa=[vertex(a,s,t) for s,t in section]
    bb=[vertex(b,s,t) for s,t in section]
    for i in range(6):
        j=(i+1)%6
        msp.add_3dface([aa[i],aa[j],bb[j],bb[i]],dxfattribs={'layer':layer})
    msp.add_3dface([aa[0],aa[1],aa[2],aa[3]],dxfattribs={'layer':layer})
    msp.add_3dface([bb[0],bb[1],bb[2],bb[3]],dxfattribs={'layer':layer})


web=[];audit=[]
for i,m in enumerate(M):
    mark=m['mark'];section,cut,qty=BOM[mark]
    a,b=P[m['a']],P[m['b']]
    start,end=cut_endpoints(a,b,cut,mark)
    width,thick=map(int,section[1:].split('x'))
    add_angle(start,end,width,thick,'ANGLE_FACES')
    msp.add_line(a,b,dxfattribs={'layer':'MEMBER_AXIS_MARK'})
    mid=tuple((a[j]+b[j])/2 for j in range(3))
    msp.add_text(str(mark),dxfattribs={'height':45,'insert':mid,'layer':'MEMBER_AXIS_MARK'})
    face=m['faces'][0]
    web.append(dict(id=f'{mark}-{i}',mark=mark,section=section,cut=cut,modelLength=round(math.dist(start,end),3),axisLength=round(math.dist(a,b),2),start=start,end=end,axisStart=a,axisEnd=b,nodes=[m['a'],m['b']],faces=m['faces'],provisional=False,note=face))
    audit.append([mark,section,cut,qty,m['a'],m['b'],round(math.dist(a,b),2),round(math.dist(start,end),3),' / '.join(m['faces'])])
try:doc.saveas(OUT/'330-KC22D-DJ-04_rods_stage1.dxf')
except PermissionError:doc.saveas(OUT/'330-KC22D-DJ-04_rods_stage1_updated.dxf')

def csvout(name,header,rows):
    with (OUT/name).open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f);w.writerow(header);w.writerows(rows)
    shutil.copyfile(OUT/name,SITE/name)

csvout('member_mapping.csv',['件号','规格','BOM下料长_mm','BOM数量','起点节点','终点节点','连接轴线跨度_mm','实体长度_mm','所属面'],audit)
csvout('geometry_audit.csv',['件号','规格','BOM下料长_mm','BOM数量','起点节点','终点节点','连接轴线跨度_mm','实体长度_mm','所属面'],audit)
classified=[]
for mark in range(301,383):
    section,cut,qty=(BOM if mark in BOM else NON_RODS)[mark]
    classified.append([mark,section,cut,qty,'角钢杆件' if mark in BOM else '连接板/垫块/焊接件','是' if mark in BOM else '否'])
csvout('bom_classified.csv',['件号','规格','下料长_mm','数量','分类','本阶段建模'],classified)

# Compare the front projection with independently extracted PDF solid paths.
# End order is irrelevant. Full axis spans and cut lengths are checked separately.
front_checks=[]
for mark,ref in FRONT_SEGMENTS.items():
    member=next(m for m in web if m['mark']==mark and 'front' in m['faces'])
    projected=[(p[0],p[2]) for p in [member['axisStart'],member['axisEnd']]]
    expected=[front_coord(p) for p in ref]
    error=min(max(math.dist(projected[j],expected[j]) for j in range(2)),max(math.dist(projected[j],expected[1-j]) for j in range(2)))
    assert error<.02,(mark,error)
    front_checks.append({'mark':mark,'maxErrorMm':round(error,4)})
assert max(abs(m['modelLength']-m['cut']) for m in web)<.001

# Each face has a plane defined by shared main members. All its member axes
# must remain on this plane; crossing braces do not become accidental nodes.
import numpy as np
face_data=[]
face_residuals=[]
for key,title,shared,source in FACE_INFO:
    members=[m for m in web if key in m['faces']]
    pts=np.array([p for m in members for p in [m['axisStart'],m['axisEnd']]])
    center=pts.mean(axis=0);_,_,vh=np.linalg.svd(pts-center)
    normal=vh[-1];residual=float(np.max(np.abs((pts-center)@normal)))
    assert residual<2,(key,residual)
    face_residuals.append({'face':title,'maxPlanarityErrorMm':round(residual,4)})
    face_data.append({'key':key,'title':title,'shared':shared,'source':'reference/'+source,'normal':normal.tolist(),'center':center.tolist(),'count':len(members),'planarityError':round(residual,3)})

# A plane/edge audit confirms shared IDs are the SAME instances in all faces.
joins=[('bottom','front',301),('bottom','rear',302),('upper','front',303),('upper','rear',304),('detail1','front',312),('detail1','rear',313),('detail2','front',315),('detail2','rear',316),('detail2','bottom',341),('detail2','detail1',364),('detail3','front',317),('detail3','rear',317)]
for f1,f2,mark in joins:assert any(m['mark']==mark and f1 in m['faces'] and f2 in m['faces'] for m in web)
report={'frontSolidSegments':front_checks,'facePlanes':face_residuals,'sharedMembers':[dict(face1=a,face2=b,mark=c) for a,b,c in joins], 'marks':len(counts),'instances':len(web),'excludedMarks':len(NON_RODS),'bomLengthMaxErrorMm':max(abs(m['modelLength']-m['cut']) for m in web),'scope':'核对连接轴线与本张实线结构；下料长保留，连接板处的轴线跨度与实体下料长度分开。节点孔距、火曲及角钢朝向未作为加工验收。'}
report['dimensionChecks']=[
    {'name':'根部高度','drawingMm':2500,'modelMm':round(P['U0F'][2]-P['B0F'][2],3)},
    {'name':'底部根宽','drawingMm':3364,'modelMm':round(abs(P['B0F'][1]-P['B0R'][1]),3)},
    {'name':'顶部根宽','drawingMm':2988,'modelMm':round(abs(P['U0F'][1]-P['U0R'][1]),3)},
    {'name':'末端至塔中心','drawingMm':8500,'modelMm':round(P['B6F'][0],3)},
    {'name':'两挂点间距','drawingMm':480,'modelMm':round(P['B5F'][0]-P['B4F'][0],3)},
    {'name':'挂点中线至塔中心','drawingMm':7000,'modelMm':round((P['B4F'][0]+P['B5F'][0])/2,3)},
    {'name':'端部层高','drawingMm':600,'modelMm':round(P['U3F'][2]-P['B5F'][2],3)},
    {'name':'2—2 顶横向距离','drawingMm':1347,'modelMm':round(abs(P['U3F'][1]-P['U3R'][1]),3)},
]
assert max(abs(x['drawingMm']-x['modelMm']) for x in report['dimensionChecks'])<3
(OUT/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
shutil.copyfile(OUT/'validation.json',SITE/'validation.json')
(SITE/'model.json').write_text(json.dumps({'drawing':'330-KC22D-DJ-04','units':'mm','version':'20261021-faces-3','members':web,'faces':face_data,'checks':report},ensure_ascii=False,separators=(',',':')),encoding='utf-8')
from sheet9_verification import write_reference_assets
write_reference_assets(SITE,OUT,web,report)
(OUT/'README.md').write_text('# 第9张横担杆件：逐面重建\n\n以单线图实线作为正投影基准，忽略塔身与变形虚线。正面、背面、底面、上斜面、1—1、2—2、3—3 共七个面，通过同一杆件实例拼合。\n\n纠正了 379/380 的分类：二者为 Q345-12×643 连接板，381/382 为 Q345-12×570，均不建模。本张共49种角钢件号、58根角钢；33种板/垫块/焊接件排除。\n\n实体按 BOM 下料长生成。连接轴线延伸至结构节点，实体端头按省略的连接板留隙；不得将连接跨度误当成下料长。单线图实线的底弦有上扬，保留原图投影，未强行压到水平。\n\nvalidation.json 记录正视实线端点偏差、各面共面误差、共用杆件与数量检查。网页的分面图以轴线核对连接关系，可同时显示原图。逐面图形吻合不等于加工验收：孔位、角钢朝向、火曲和端部切角未建模。\n',encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
