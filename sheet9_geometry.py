"""Sheet 9 assembly graph, registered to the PDF's solid front diagram.

X points out along the crossarm, Y across it, Z up. Drawing coordinates are
PDF points, not screenshot pixels. All member lengths are in millimetres.
"""
from pathlib import Path
import json
import math

# Centres of the 13 continuous solid quadrilaterals in the front single-line
# diagram (PDF drawing paths 50, 52, ..., 74). Dashed tower/deflection lines
# and dimension leaders are excluded.
FRONT_SEGMENTS = {
    321: ((326.50072,143.68884),(300.41122,166.37225)),
    319: ((290.00969,144.58984),(326.50070,143.68890)),
    318: ((300.41121,166.37225),(288.30063,146.07574)),
    317: ((459.91257,162.43414),(423.52472,146.27924)),
    315: ((423.52472,146.27924),(423.68521,163.32855)),
    314: ((409.88431,163.66931),(423.52472,146.27924)),
    312: ((373.60025,147.51190),(423.52472,146.27930)),
    311: ((373.39328,164.57031),(373.60024,147.51190)),
    309: ((336.90222,165.47131),(373.60025,147.51190)),
    307: ((336.90224,165.47125),(316.09918,121.90643)),
    305: ((263.92020,167.27319),(316.09921,121.90649)),
    303: ((409.88431,163.66937),(258.59811,96.30109)),
    301: ((263.92020,167.27325),(459.91258,162.43414)),
}
SCALE = 2500 / (167.27325-96.30109)
ORIGIN = (263.92020,167.27325)
# Calibrate horizontal extent independently against the dimension chain:
# root = 3364/2; end = 7000+1500. The printed drawing is not perfectly scaled.
X_SCALE = (8500-1682)/(459.91258-ORIGIN[0])

def front_coord(p):
    return (1682+(p[0]-ORIGIN[0])*X_SCALE,(ORIGIN[1]-p[1])*SCALE)

LOWER_RISE = (167.27325-162.43414)*SCALE/((459.91258-263.92020)*X_SCALE)
tx,tz=front_coord(FRONT_SEGMENTS[303][1])
ROOT_TOP_HALF_WIDTH=1494  # 2988 / 2, read from the tower attachment dimension.
# Use the annotated 1347 transverse distance in section 2-2 to close the
# depth plane. Measuring a flange's outer silhouette as its connection axis
# introduced a 100 mm width error in the previous reconstruction.
cx,cz=front_coord(FRONT_SEGMENTS[315][0])
root_dx=tx-1682
ratio=(cz-LOWER_RISE*(cx-1682))/(tz-LOWER_RISE*root_dx)
DEPTH_TAPER=(1682+ratio*(ROOT_TOP_HALF_WIDTH-1682)-1347/2)/((cx-1682)-ratio*root_dx)
SIDE_LEAN=(ROOT_TOP_HALF_WIDTH-(1682-DEPTH_TAPER*(tx-1682)))/(tz-LOWER_RISE*(tx-1682))

def halfwidth(x,z):
    # A plane through the lower rail and the 2988-wide upper attachment.
    # The small inward lean is also visible in section 2-2.
    return 1682-DEPTH_TAPER*(x-1682)+SIDE_LEAN*(z-LOWER_RISE*(x-1682))

def make_geometry():
    P={}
    uv={
      'B0':FRONT_SEGMENTS[301][0], 'U0':FRONT_SEGMENTS[303][1],
      'U1':FRONT_SEGMENTS[305][1], 'B1':FRONT_SEGMENTS[321][1],
      'B2':FRONT_SEGMENTS[309][0], 'B3':FRONT_SEGMENTS[311][0],
      'U2':FRONT_SEGMENTS[312][0], 'B4':FRONT_SEGMENTS[314][0],
      'U3':FRONT_SEGMENTS[315][0], 'B5':FRONT_SEGMENTS[315][1],
      'B6':FRONT_SEGMENTS[301][1], 'P':FRONT_SEGMENTS[319][0],
      'P318':FRONT_SEGMENTS[318][1], 'Q':FRONT_SEGMENTS[319][1],
    }
    for name,p in uv.items():
        x,z=front_coord(p)
        for suffix,sign in [('F',-1),('R',1)]:
            P[name+suffix]=(x,sign*halfwidth(x,z),z)
        P[name+'C']=(x,0,z)
    # End-frame stations are distinct in the bottom detail (343 and 344).
    # Distances from the 341 station are measured in that view, scale 1:20.
    for name,pdf_x in [('E343',1337.029),('E344',1348.070)]:
        x=P['B5F'][0]+(pdf_x-1167.176)*7.0654
        z=LOWER_RISE*(x-1682)
        for suffix,sign in [('F',-1),('R',1)]:P[name+suffix]=(x,sign*halfwidth(x,z),z)
    # 331/332 start at the central root plate. 372 joins each of them, not
    # a fictitious longitudinal member. The 890.5 / 1691.5 detail locates it.
    for suffix in ['F','R']:
        a,b=P['B0C'],P['B1'+suffix]
        P['K'+suffix]=tuple(a[i]+(890.5/1691.5)*(b[i]-a[i]) for i in range(3))
        a=P['B0'+suffix];b=P['B6'+suffix]
        direction=[(b[i]-a[i])/math.dist(a,b) for i in range(3)]
        P['ST0'+suffix]=tuple(a[i]-350*direction[i] for i in range(3))
        P['ST1'+suffix]=tuple(a[i]+355*direction[i] for i in range(3))
        # Section 3-3 dimensions locate the X-brace attachment stations
        # inside 317. Its end extensions are not part of the X panel.
        a,b=P['U3'+suffix],P['B6'+suffix]
        span=math.hypot(b[0]-a[0],b[2]-a[2])
        sign=-1 if suffix=='F' else 1
        for name,fraction,width in [('D3A',126/span,1361),('D3B',1-274/span,1013)]:
            point=[a[j]+fraction*(b[j]-a[j]) for j in range(3)]
            point[1]=sign*width/2
            P[name+suffix]=tuple(point)
    M=[]
    def add(mark,a,b,faces):M.append({'mark':mark,'a':a,'b':b,'faces':faces})
    side=[(301,302,'B0','B6'),(303,304,'U0','B4'),
          (305,306,'B0','U1'),(307,308,'U1','B2'),
          (309,310,'B2','U2'),(311,311,'B3','U2'),
          (312,313,'U2','U3'),(314,314,'B4','U3'),
          (315,316,'B5','U3'),(317,317,'U3','B6'),
          (318,318,'P318','B1'),(319,320,'P','Q'),(321,321,'B1','Q')]
    for f,r,a,b in side:
        extras={301:['bottom'],303:['upper'],312:['detail1'],315:['detail2'],317:['detail3']}.get(f,[])
        add(f,a+'F',b+'F',['front',*extras]);add(r,a+'R',b+'R',['rear',*extras])
    for mark,a,b in [(331,'B0C','B1R'),(332,'B0C','B1F'),
        (333,'B1F','B2R'),(334,'B1R','B2F'),(335,'B2F','B3R'),(336,'B2R','B3F'),
        (337,'B3F','B4R'),(338,'B3R','B4F'),(339,'B4F','B4R'),
        (340,'B4C','B5F'),(340,'B4C','B5R'),
        (341,'B5F','B5R'),(342,'B5F','E343R'),(342,'B5R','E343F'),
        (343,'E343F','E343R'),(344,'E344F','E344R'),
        (372,'B0F','KF'),(372,'B0R','KR'),
        (359,'ST0F','ST1F'),(360,'ST0R','ST1R')]:
        add(mark,a,b,['bottom']+(['detail2'] if mark==341 else []))
    for mark,a,b in [(354,'U0R','U1F'),(355,'U0F','U1R'),
                     (356,'U1R','U2F'),(357,'U1F','U2R'),(358,'U2F','U2R')]:
        add(mark,a,b,['upper'])
    for mark,a,b in [(362,'U2R','U3F'),(363,'U2F','U3R'),(364,'U3F','U3R')]:
        add(mark,a,b,['detail1']+(['detail2'] if mark==364 else []))
    for suffix in ['F','R']:add(367,'U3'+suffix,'B5C',['detail2'])
    add(370,'D3AR','D3BF',['detail3']);add(371,'D3AF','D3BR',['detail3'])
    return P,M

FACE_INFO=[
    ('front','正面','301 / 303 / 305–321','single_line.png'),
    ('rear','背面','302 / 304 / 配对件号','front_detail.png'),
    ('bottom','底面','301 / 302 / 331–344 / 359–360 / 372','lower_detail.png'),
    ('upper','上斜面','303 / 304 / 354–358','upper_detail.png'),
    ('detail1','1—1 顶部小面','312 / 313 / 362 / 363 / 364','detail_1.png'),
    ('detail2','2—2 横向立面','315 / 316 / 341 / 364 / 367','detail_2.png'),
    ('detail3','3—3 端部斜面','317 / 370 / 371','detail_3.png'),
]

def cut_endpoints(a,b,length,mark):
    """Keep BOM cut length; join axes include the omitted plate connections."""
    span=math.dist(a,b);gap=span-length
    # Asymmetric end clearances explicitly annotated on 303, 312 and 317.
    ratio={303:79/306,304:79/306,312:200/280,313:200/280,317:76/300}.get(mark,.5)
    d=[(b[i]-a[i])/span for i in range(3)]
    return (tuple(a[i]+gap*ratio*d[i] for i in range(3)),
            tuple(b[i]-gap*(1-ratio)*d[i] for i in range(3)))
