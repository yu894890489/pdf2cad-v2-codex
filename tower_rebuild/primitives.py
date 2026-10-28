"""Lossless native path records plus conservative geometric/annotation labels.

Coordinates are PDF points on the unrotated page, never engineering millimetres.
Circles/arcs are recognized from sampled cubic paths; the original commands
remain attached. No primitive is asserted to be a physical steel member.
"""
from collections import defaultdict
import math
import re

import fitz
import numpy as np


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, fitz.Point, fitz.Rect, fitz.Quad)):
        return [plain(v) for v in value]
    return value


def bounds(points):
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def distance(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])


def circle_fit(items):
    if not items or any(item[0] != "c" for item in items):
        return None
    if any(distance(items[i-1][-1], items[i][1]) > .02 for i in range(1, len(items))):
        return None
    samples = []
    for _, a, b, c, d in items:
        for t in np.linspace(0, 1, 13):
            samples.append((1-t)**3*np.array(a)+3*(1-t)**2*t*np.array(b)+3*(1-t)*t*t*np.array(c)+t**3*np.array(d))
    pts = np.array(samples)
    origin = pts.mean(axis=0)
    q = pts-origin
    solution, _, rank, _ = np.linalg.lstsq(np.column_stack([2*q, np.ones(len(q))]), (q*q).sum(axis=1), rcond=None)
    if rank != 3:
        return None
    center = solution[:2]+origin
    radius = math.sqrt(max(0., solution[2]+solution[0]**2+solution[1]**2))
    if radius < .15:
        return None
    residual = float(np.max(np.abs(np.linalg.norm(pts-center, axis=1)-radius)))
    if residual > max(.003, radius*.001):
        return None
    angles = np.unwrap(np.arctan2(pts[:, 1]-center[1], pts[:, 0]-center[0]))
    sweep = float(angles[-1]-angles[0])
    if abs(sweep) < math.radians(10):
        return None
    closed = distance(items[0][1], items[-1][-1]) < .03 and abs(abs(sweep)-2*math.pi) < .03
    return {"center": center.tolist(), "radius": radius, "start_angle": float(angles[0]),
            "sweep_angle": sweep, "fit_error_pt": residual, "closed": closed}


def geometry(items):
    """One connected PDF path is a primitive; mixed paths retain all commands."""
    circle = circle_fit(items)
    if circle:
        return ("circle" if circle["closed"] else "circular_arc"), circle
    if len(items) == 1:
        op, *values = items[0]
        if op == "l":
            return "line", {"start": values[0], "end": values[1]}
        if op == "re":
            return "rectangle", {"rect": values[0]}
        if op == "qu":
            a, b, c, d = values[0]
            return "quadrilateral", {"points": [a, b, d, c]}
        if op == "c":
            return "bezier_curve", {"points": values}
    if all(item[0] == "l" for item in items):
        if all(distance(items[i-1][2], items[i][1]) < .01 for i in range(1, len(items))):
            pts = [items[0][1], *[item[2] for item in items]]
            closed = distance(pts[0], pts[-1]) < .01
            return ("polygon" if closed else "polyline"), {"points": pts, "closed": closed}
    return "compound_path", {}


def contained(box, outer, tolerance=.8):
    return box[0] >= outer[0]-tolerance and box[1] >= outer[1]-tolerance and box[2] <= outer[2]+tolerance and box[3] <= outer[3]+tolerance


def table_assignment(box, tables):
    matches = [t for t in tables if contained(box, t["bbox_pt"])]
    return min(matches, key=lambda t: (t["bbox_pt"][2]-t["bbox_pt"][0])*(t["bbox_pt"][3]-t["bbox_pt"][1]))["id"] if matches else None


def is_dashed(value):
    match = re.search(r"\[([^]]*)\]", value or "")
    return bool(match and any(float(n) > 0 for n in re.findall(r"[-+]?\d*\.?\d+", match[1])))


def extract_primitives(page, page_number, tables):
    records = []
    w, h = page.rect.width, page.rect.height
    paths = page.get_drawings()
    for index, path in enumerate(paths):
        items = plain(path["items"])
        kind, shape = geometry(items)
        box = list(path["rect"])
        table_id = table_assignment(box, tables)
        style = {k: plain(path.get(k)) for k in ("type", "width", "color", "fill", "dashes", "closePath", "lineCap", "lineJoin", "stroke_opacity", "fill_opacity", "layer", "even_odd")}
        dashed = is_dashed(path.get("dashes"))
        role = "native_dashed" if dashed else "unassigned_geometry"
        if table_id:
            role = "table_graphics"
        elif kind == "line" and (box[2]-box[0] > w*.85 or box[3]-box[1] > h*.85):
            role = "sheet_frame_candidate"
        records.append({"id": f"p{page_number}_v{index:06d}", "page": page_number, "source_path_index": index,
                        "source_sequence": path.get("seqno"), "primitive_type": kind, "geometry": shape,
                        "commands": items, "bbox_pt": box, "bbox": [box[0]/w, box[1]/h, box[2]/w, box[3]/h],
                        "style": style, "line_style": "dashed" if dashed else "solid",
                        "part": "table" if table_id else "structure", "table_id": table_id,
                        "role": role, "role_basis": "native PDF dash pattern" if dashed else "geometric type only",
                        "physical_member": None})
    for index, word in enumerate(page.get_text("words")):
        box, text = list(word[:4]), word[4]
        table_id = table_assignment(box, tables)
        compact = re.sub(r"\s+", "", text)
        role = "text"
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:°)?", compact):
            role = "numeric_text"
        elif re.search(r"(?:Q\d{3})?L\d+[X×]\d+", compact, re.I):
            role = "specification_text"
        elif re.fullmatch(r"\d+[-—–]{1,3}\d+|1:\d+", compact):
            role = "view_label"
        records.append({"id": f"p{page_number}_t{index:05d}", "page": page_number, "primitive_type": "text",
                        "text": text, "text_block": word[5], "text_line": word[6], "text_word": word[7],
                        "bbox_pt": box, "bbox": [box[0]/w, box[1]/h, box[2]/w, box[3]/h],
                        "part": "table" if table_id else "structure", "table_id": table_id,
                        "role": "table_text" if table_id else role, "role_basis": "native text; numeric text is not automatically a dimension",
                        "physical_member": None})
    for index, block in enumerate(page.get_image_info()):
        box = list(block["bbox"])
        table_id = table_assignment(box, tables)
        records.append({"id": f"p{page_number}_r{index:04d}", "page": page_number, "primitive_type": "raster_image",
                        "bbox_pt": box, "bbox": [box[0]/w, box[1]/h, box[2]/w, box[3]/h],
                        "image_size": [block["width"], block["height"]], "part": "table" if table_id else "structure",
                        "table_id": table_id, "role": "raster_requires_vectorization", "physical_member": None})
    return records, len(paths)


def associate_callouts(primitives, bom):
    """Associate enclosed part marks with circles, preserving letter suffixes."""
    from .native_bom import PART_MARK
    rows = {row["mark"]: row for row in bom}
    circles = [p for p in primitives if p["part"] == "structure" and p["primitive_type"] == "circle"]
    words = [p for p in primitives if p["part"] == "structure" and p["primitive_type"] == "text"]
    groups, used = [], set()
    for circle in circles:
        if circle["id"] in used:
            continue
        center, radius = circle["geometry"]["center"], circle["geometry"]["radius"]
        rings = [c for c in circles if c["id"] not in used and c["page"] == circle["page"]
                 and distance(c["geometry"]["center"], center) < .35 and abs(c["geometry"]["radius"]-radius) < max(3., radius*.35)]
        used.update(c["id"] for c in rings)
        outer = max(rings, key=lambda c: c["geometry"]["radius"])
        r = outer["geometry"]["radius"]
        enclosed = []
        for word in words:
            box = word["bbox_pt"]
            midpoint = [(box[0]+box[2])/2, (box[1]+box[3])/2]
            if word["page"] == circle["page"] and distance(midpoint, center) < r*.88 and contained(box, outer["bbox_pt"], tolerance=r*.22):
                enclosed.append(word)
        marks = []
        for word in sorted(enclosed, key=lambda t: (t["bbox_pt"][1], t["bbox_pt"][0])):
            value = re.sub(r"\s+", "", word["text"])
            match = re.fullmatch(r'(前|后)?(' + PART_MARK + r')', value)
            if match:
                mark = match[2]
                row = rows.get(mark)
                marks.append({"mark": mark, "side": match[1], "text_id": word["id"], "bom_found": row is not None,
                              "material_kind": row["classification"]["kind"] if row else None})
        if not marks:
            continue
        found = [m for m in marks if m["bom_found"]]
        kinds = {m["material_kind"] for m in found}
        role = "member_callout" if kinds == {"angle"} else "plate_callout" if kinds == {"plate"} else "other_callout" if kinds else "unmatched_callout"
        group = {"id": f"callout_{len(groups)+1:04d}", "page": circle["page"], "bbox_pt": outer["bbox_pt"],
                 "circle_ids": [c["id"] for c in rings], "ring_count": len(rings), "marks": marks,
                 "text": " ".join(w["text"] for w in enclosed), "role": role,
                 "association_status": "enclosed_text_only; no rod association", "needs_review": len(found) != len(marks)}
        groups.append(group)
        for item in [*rings, *[w for w in enclosed if any(m["text_id"] == w["id"] for m in marks)]]:
            item.update(role=role, role_basis="numeric text enclosed by native circle; material from BOM", callout_id=group["id"])
    return groups


def detect_dash_candidates(primitives):
    """PDFs may draw dashed lines as solid segments. Preserve them as candidates.

    This conservative pattern detector does not delete any segment or prove that
    it is a hidden line. Holes, dimensions and short solid rods can look similar.
    """
    buckets = defaultdict(list)
    for p in primitives:
        if p["part"] != "structure" or p["primitive_type"] != "line" or p["role"] != "unassigned_geometry":
            continue
        a, b = np.array(p["geometry"]["start"]), np.array(p["geometry"]["end"])
        length = float(np.linalg.norm(b-a))
        if not .3 <= length <= 12:
            continue
        angle = math.atan2(b[1]-a[1], b[0]-a[0]) % math.pi
        angle_bucket = round(math.degrees(angle)*2) % 360
        radians = math.radians(angle_bucket/2)
        u = np.array([math.cos(radians), math.sin(radians)])
        n = np.array([-u[1], u[0]])
        offset = float(((a+b)/2)@n)
        start, end = sorted([float(a@u), float(b@u)])
        buckets[(p["page"], angle_bucket, round(offset*2))].append((start, end, p))
    groups = []
    for values in buckets.values():
        values.sort(key=lambda v: v[0])
        chains, chain = [], []
        for item in values:
            if chain:
                prev = chain[-1]
                length, before = item[1]-item[0], prev[1]-prev[0]
                gap = item[0]-prev[1]
                if not (.2*min(length, before) < gap < 3*max(length, before) and .7 < length/before < 1.4):
                    if len(chain) >= 6:
                        chains.append(chain)
                    chain = []
            chain.append(item)
        if len(chain) >= 6:
            chains.append(chain)
        for chain in chains:
            gid = f"dash_candidate_{len(groups)+1:04d}"
            gaps = [chain[i][0]-chain[i-1][1] for i in range(1, len(chain))]
            if np.std(gaps) > np.mean(gaps)*.3:
                continue
            groups.append({"id": gid, "page": chain[0][2]["page"], "primitive_ids": [p[2]["id"] for p in chain],
                           "basis": "six or more similar collinear segments with repeated gaps", "status": "candidate_needs_review"})
            for _, _, p in chain:
                p.update(role="dash_segment_candidate", role_basis="repeated segment/gap geometry; PDF stroke itself is solid", dash_group_id=gid)
    return groups
