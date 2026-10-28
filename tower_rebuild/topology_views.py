"""Group native primitives by connected drawing ink, not overlapping crop boxes."""
from collections import defaultdict
import math
import re

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage


def center(box):
    return [(box[0]+box[2])/2,(box[1]+box[3])/2]


def find_views(summary, primitives, callouts, site):
    views, assignment, unassigned = [], {}, []
    labels_by_line=defaultdict(list)
    for p in primitives:
        if p["primitive_type"]=="text":
            labels_by_line[(p["page"],p["text_block"],p["text_line"])].append(p)
    phrases=[]
    for items in labels_by_line.values():
        items.sort(key=lambda p:p["text_word"])
        phrases.append({"page":items[0]["page"],"text":"".join(p["text"] for p in items).replace(" ",""),
                        "bbox_pt":[min(p["bbox_pt"][0] for p in items),min(p["bbox_pt"][1] for p in items),max(p["bbox_pt"][2] for p in items),max(p["bbox_pt"][3] for p in items)],
                        "primitive_ids":[p["id"] for p in items]})
    for page in summary["pages"]:
        pn=page["page"]; w,h=page["size_pt"]
        bitmap=Image.open(site/page["structure"]).convert("L")
        bitmap.thumbnail((1800,1800))
        # Remove native page-frame strokes before dilation; otherwise a narrow
        # section touching the border through annotation can be swallowed by the
        # huge frame component and incorrectly discarded.
        ink=ImageDraw.Draw(bitmap);rx,ry=bitmap.width/w,bitmap.height/h
        for p in primitives:
            if p["page"]!=pn:continue
            b=p["bbox_pt"];bw,bh=b[2]-b[0],b[3]-b[1]
            if p["role"]=="sheet_frame_candidate" or (max(bw/w,bh/h)>.75 and min(bw,bh)<3):
                rectangle=[b[0]*rx,b[1]*ry,b[2]*rx,b[3]*ry]
                if min(bw,bh)<3:
                    ink.rectangle([rectangle[0]-2,rectangle[1]-2,rectangle[2]+2,rectangle[3]+2],fill=255)
                else:
                    ink.rectangle(rectangle,outline=255,width=4)
        pixels=np.array(bitmap); sy,sx=pixels.shape
        # Thin 0.4 pt strokes become gray after thumbnail resampling. A dark-only
        # threshold drops long section edges while retaining just their captions.
        labels,_=ndimage.label(ndimage.binary_dilation(pixels<215,iterations=2))
        counts=np.bincount(labels.ravel()); components=[]
        for index,slices in enumerate(ndimage.find_objects(labels),1):
            if slices is None:
                continue
            yy,xx=slices
            box=[xx.start/sx*w,yy.start/sy*h,xx.stop/sx*w,yy.stop/sy*h]
            bw,bh=box[2]-box[0],box[3]-box[1]
            if bw>w*.85 or bh>h*.85 or bw<6 or bh<25:
                continue
            components.append({"component":index,"bbox_pt":box,"ink_pixels":int(counts[index])})
        # Large connected drawings include very narrow vertical sections.
        chosen=[dict(c,role="structural_view") for c in components if c["ink_pixels"]>=1100 and max(c["bbox_pt"][2]-c["bbox_pt"][0],c["bbox_pt"][3]-c["bbox_pt"][1])>=150]
        # A thin edge-on section can contain fewer pixels than a lattice face.
        # Its native section title supplies a second, independent seed criterion.
        for phrase in phrases:
            if phrase["page"]!=pn or not re.fullmatch(r"\d+[-—–]{1,3}\d+",phrase["text"]):
                continue
            x,y=center(phrase["bbox_pt"])
            candidates=[c for c in components if c["bbox_pt"][0]-25<=x<=c["bbox_pt"][2]+25
                        and 0<=y-c["bbox_pt"][3]<=160 and c["bbox_pt"][3]-c["bbox_pt"][1]>=60]
            if candidates:
                best=min(candidates,key=lambda c:y-c["bbox_pt"][3]+abs(x-center(c["bbox_pt"])[0])*.25-min(50,c["ink_pixels"]/100))
                if not any(c["component"]==best["component"] for c in chosen):
                    chosen.append(dict(best,role="section_view"))
        callout_circles={pid for c in callouts for pid in c["circle_ids"]}
        hole_circles=[p for p in primitives if p["page"]==pn and p["primitive_type"]=="circle" and p["id"] not in callout_circles and p["geometry"]["radius"]<8]
        for phrase in phrases:
            if phrase["page"]!=pn or not re.fullmatch(r"1:\d+",phrase["text"]):
                continue
            x,y=center(phrase["bbox_pt"])
            nearby=[p for p in hole_circles if abs(p["geometry"]["center"][0]-x)<90 and 0<y-p["geometry"]["center"][1]<180]
            if not nearby:
                continue
            best=min(nearby,key=lambda p:math.dist(p["geometry"]["center"],[x,y]))
            hx,hy=best["geometry"]["center"]
            candidates=[c for c in components if c["bbox_pt"][0]<=hx<=c["bbox_pt"][2] and c["bbox_pt"][1]<=hy<=c["bbox_pt"][3]]
            if candidates:
                component=max(candidates,key=lambda c:c["ink_pixels"])
                if not any(c["component"]==component["component"] for c in chosen):
                    chosen.append(dict(component,role="detail",scale_label=phrase["text"]))
        chosen.sort(key=lambda c:(c["bbox_pt"][1],c["bbox_pt"][0]))
        if not chosen:
            unassigned.extend(p["id"] for p in primitives if p["page"]==pn)
            continue
        seed=np.zeros(labels.shape,dtype=np.int16)
        for i,c in enumerate(chosen,1):
            seed[labels==c["component"]]=i
        distances,indices=ndimage.distance_transform_edt(seed==0,return_indices=True)
        nearest=seed[indices[0],indices[1]]
        groups=defaultdict(list)
        contents_by_id={p["id"]:p for p in primitives if p["page"]==pn}
        for p in primitives:
            if p["page"]!=pn:
                continue
            box=p["bbox_pt"]; px,py=center(box)
            if (box[2]-box[0]>w*.75 or box[3]-box[1]>h*.75 or p["role"]=="sheet_frame_candidate"
                    or px < w*.035 or px > w*.985 or py < h*.025 or py > h*.975):
                unassigned.append(p["id"]);continue
            x=int(np.clip(px/w*sx,0,sx-1));y=int(np.clip(py/h*sy,0,sy-1))
            if distances[y,x]*w/sx>75:
                unassigned.append(p["id"]);continue
            groups[int(nearest[y,x])].append(p)
        # A circle and its enclosed marks form one observation. Long chords can
        # leave these labels >75 pt from the connected seed; never split the rings.
        for callout in callouts:
            if callout["page"]!=pn: continue
            px,py=center(callout["bbox_pt"])
            x=int(np.clip(px/w*sx,0,sx-1));y=int(np.clip(py/h*sy,0,sy-1))
            if distances[y,x]*w/sx>150: continue
            owner=int(nearest[y,x]); ids=set(callout["circle_ids"]+[m["text_id"] for m in callout["marks"]])
            for group in groups.values(): group[:]=[p for p in group if p["id"] not in ids]
            unassigned[:]=[pid for pid in unassigned if pid not in ids]
            groups[owner].extend(contents_by_id[pid] for pid in sorted(ids) if pid in contents_by_id)
        main_number=0
        for i,c in enumerate(chosen,1):
            contents=groups[i]; box=list(c["bbox_pt"]); title=None; title_evidence=[]
            content_ids={p["id"] for p in contents}
            for phrase in phrases:
                if phrase["page"]!=pn:
                    continue
                if not content_ids.intersection(phrase["primitive_ids"]):
                    continue
                x,y=center(phrase["bbox_pt"])
                if not (box[0]-45<=x<=box[2]+45 and box[1]<=y<=box[3]+100):
                    continue
                if re.fullmatch(r"\d+[-—–]{1,3}\d+",phrase["text"]):
                    title="剖面 "+re.sub(r"[-—–]+","—",phrase["text"]);c["role"]="section_view";title_evidence=phrase["primitive_ids"]
                elif "单线图"==phrase["text"]:
                    title="单线图 · 正视基准";c["role"]="front_schematic";title_evidence=phrase["primitive_ids"]
            if title is None:
                if c["role"]=="detail":
                    title=f"零件详图 {sum(v['role']=='detail' for v in views)+1}"
                else:
                    main_number+=1;title=f"主结构视图 {main_number}"
            # Expand from the connected figure using assigned annotation positions.
            for p in contents:
                if p["primitive_type"] in {"text","circle"}:
                    b=p["bbox_pt"]
                    box=[min(box[0],b[0]),min(box[1],b[1]),max(box[2],b[2]),max(box[3],b[3])]
            box=[max(0,box[0]-8),max(0,box[1]-8),min(w,box[2]+8),min(h,box[3]+8)]
            vid=f"p{pn}_view_{i:02d}"
            for p in contents:
                assignment[p["id"]]=vid
            views.append({"id":vid,"page":pn,"title":title,"role":c["role"],"bbox_pt":box,"core_bbox_pt":c["bbox_pt"],
                          "primitive_ids":[p["id"] for p in contents],"title_evidence":title_evidence,
                          "grouping_basis":"connected native ink + nearest connected component; bounding-box overlap does not merge views",
                          "status":"geometric_grouping_needs_review"})
            # A view-only background avoids showing adjacent faces inside an
            # overlapping rectangular crop. The unchanged original stays available.
            original=Image.open(site/page["structure"]).convert("RGB")
            mask=Image.fromarray(((nearest==i)&(distances*w/sx<=150)).astype(np.uint8)*255).resize(original.size,Image.Resampling.NEAREST)
            isolated=Image.composite(original,Image.new("RGB",original.size,"white"),mask)
            target=site/"topology"/"view-images"/f"{vid}.png";target.parent.mkdir(parents=True,exist_ok=True)
            isolated.save(target)
            views[-1]["image"]=f"view-images/{vid}.png"
    for view in views:
        ids=set(view["primitive_ids"])
        view["callout_ids"]=[c["id"] for c in callouts if any(pid in ids for pid in c["circle_ids"])]
        view["projection_mode"]="overlapping_front_rear" if any(m.get("side") for c in callouts if c["id"] in view["callout_ids"] for m in c["marks"]) else "single_or_unspecified"
    return views,assignment,unassigned
