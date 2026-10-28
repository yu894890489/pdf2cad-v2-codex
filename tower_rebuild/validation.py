from collections import Counter

import numpy as np

from .materials import classify


def issue(code, message, severity="error", ref=""):
    return {"code": code, "message": message, "severity": severity, "ref": ref}


class DrawingError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("; ".join(i["message"] for i in issues if i["severity"] == "error"))


def compile_drawing(drawing):
    """Check semantics before numerical fitting. Never silently drop a member."""
    problems = []

    def unique(items, attribute, label):
        counts = Counter(getattr(item, attribute) for item in items)
        for key, count in counts.items():
            if count > 1:
                problems.append(issue("duplicate_id", f"{label} {key} 重复 {count} 次"))
        return {getattr(item, attribute): item for item in items}

    bom = unique(drawing.bom, "mark", "BOM 件号")
    faces = unique(drawing.faces, "id", "结构面")
    joints = unique(drawing.assembly.joints, "id", "节点实例")
    members = unique(drawing.assembly.members, "id", "杆件实例")
    placements = unique(drawing.assembly.placements, "view", "面位置")
    if not bom or not members or not joints:
        problems.append(issue("empty_model", "BOM、杆件实例和节点均不能为空"))
    if len(faces) < 2:
        problems.append(issue("missing_faces", "至少需要两个具有空间联系的结构面，单面不能确定三维"))
    if drawing.assembly.anchor_view not in faces:
        problems.append(issue("anchor_missing", "缺少正视基准面"))
    if set(placements) != set(faces):
        problems.append(issue("placement_missing", "每个结构面必须恰好有一条 placement"))
    for row in bom.values():
        kind = classify(row.specification)["kind"]
        if kind == "unknown":
            problems.append(issue("unknown_stock", f"件号 {row.mark} 规格 {row.specification} 无法分类，需核对原文"))
        if kind == "angle" and row.length_mm is None:
            problems.append(issue("missing_cut_length", f"角钢 {row.mark} 缺少下料长度"))
    # Validate all evidence recursively, including cross-view association evidence.
    def evidence_walk(obj):
        if isinstance(obj, dict):
            if {"page", "bbox", "text"} == set(obj):
                if obj["page"] > len(drawing.page_sizes):
                    problems.append(issue("invalid_evidence", "证据页码超出 PDF 页数"))
            for value in obj.values():
                evidence_walk(value)
        elif isinstance(obj, list):
            for value in obj:
                evidence_walk(value)
    evidence_walk(drawing.model_dump(mode="json"))
    for size in drawing.page_sizes:
        if min(size) <= 0:
            problems.append(issue("page_size", "页面尺寸必须大于零"))
    node_obs, edge_obs, uv, scale, calibrations = {}, {}, {}, {}, []
    for face in faces.values():
        nodes = unique(face.nodes, "id", f"{face.id} 节点")
        edges = unique(face.edges, "id", f"{face.id} 边")
        unique(face.dimensions, "id", f"{face.id} 尺寸")
        for node in nodes.values():
            node_obs[(face.id, node.id)] = node
            if node.evidence.page != face.evidence.page:
                problems.append(issue("observation_page", f"{face.id}/{node.id} 证据不在该面所在页"))
            if face.coordinate_units == "page":
                x0, y0, x1, y1 = face.evidence.bbox
                if not (x0-.001 <= node.xy[0] <= x1+.001 and y0-.001 <= node.xy[1] <= y1+.001):
                    problems.append(issue("observation_outside_face", f"{face.id}/{node.id} 坐标超出结构面区域，可能错误使用裁剪坐标"))
        for edge in edges.values():
            edge_obs[(face.id, edge.id)] = edge
            if edge.a == edge.b or edge.a not in nodes or edge.b not in nodes:
                problems.append(issue("edge_endpoints", f"{face.id}/{edge.id} 端点不存在或重合"))
        for dim in face.dimensions:
            if dim.a not in nodes or dim.b not in nodes or dim.a == dim.b:
                problems.append(issue("dimension_endpoints", f"{face.id}/{dim.id} 尺寸端点无效"))
        if face.coordinate_units == "page":
            if face.evidence.page > len(drawing.page_sizes):
                continue
            page_size = np.array(drawing.page_sizes[face.evidence.page-1])
            for xy in [face.origin, *[n.xy for n in nodes.values()],
                       *[p for c in face.calibrations for p in (c.start, c.end)]]:
                if any(v < 0 or v > 1 for v in xy):
                    problems.append(issue("page_coordinate", f"{face.id} 坐标不在原页 0..1 范围内"))
            s = []
            for axis, index in (("x", 0), ("y", 1)):
                pairs = [(abs(c.end[index]-c.start[index])*page_size[index], c.length_mm)
                         for c in face.calibrations if c.axis == axis]
                if any(d < 1e-6 for d, _ in pairs):
                    problems.append(issue("zero_calibration", f"{face.id} {axis} 比例尺寸的投影跨度为零"))
                denom = sum(d*d for d, _ in pairs)
                value = sum(d*length for d, length in pairs) / denom if denom else None
                s.append(value)
                for distance, length in pairs:
                    residual = abs(distance*(value or 0)-length)
                    calibrations.append({"face": face.id, "axis": axis, "nominal_mm": length, "error_mm": residual})
                    if residual > max(2, length * .002):
                        problems.append(issue("scale_conflict", f"{face.id} {axis} 方向标注不符合统一比例，偏差 {residual:.2f} mm；应补充节点尺寸而非强拉比例"))
            distance_pairs = [(np.linalg.norm((np.array(c.end)-c.start)*page_size), c.length_mm)
                              for c in face.calibrations if c.axis == "distance"]
            if distance_pairs and face.uniform_scale:
                denominator = sum(d*d for d, _ in distance_pairs)
                uniform = sum(d*length for d, length in distance_pairs)/denominator if denominator > 1e-12 else None
                s = [v if v is not None else uniform for v in s]
            if face.uniform_scale:
                if s[0] is None:
                    s[0] = s[1]
                if s[1] is None:
                    s[1] = s[0]
            if any(v is None for v in s):
                problems.append(issue("missing_scale", f"{face.id} 缺少横/纵比例证据，不能按图片像素猜尺寸"))
                continue
            for calibration in face.calibrations:
                if calibration.axis == "distance":
                    delta = (np.array(calibration.end)-calibration.start)*page_size*np.array(s)
                    residual = abs(np.linalg.norm(delta)-calibration.length_mm)
                    calibrations.append({"face": face.id, "axis": "distance", "nominal_mm": calibration.length_mm, "error_mm": float(residual)})
                    if residual > max(2, calibration.length_mm*.002):
                        problems.append(issue("scale_conflict", f"{face.id} 斜向标注与比例冲突，偏差 {residual:.2f} mm"))
            scale[face.id] = (s[0]*page_size[0], -s[1]*page_size[1])
        else:
            scale[face.id] = (1., 1.)
        for node in nodes.values():
            uv[(face.id, node.id)] = (np.array(node.xy)-face.origin)*scale[face.id]
    node_to_joint = {}
    for joint in joints.values():
        seen_views = set()
        for ref in joint.observations:
            key = (ref.view, ref.node)
            if key not in node_obs:
                problems.append(issue("unknown_node", f"节点实例 {joint.id} 引用了不存在的 {key}"))
            if key in node_to_joint:
                problems.append(issue("node_reused", f"观察节点 {key} 被分配到多个实例"))
            if ref.view in seen_views:
                problems.append(issue("joint_collapse", f"节点实例 {joint.id} 合并了同一面内多个不同节点"))
            seen_views.add(ref.view)
            node_to_joint[key] = joint.id
    used_edges, member_nodes, actual_counts = set(), {}, Counter()
    face_links = {key: set() for key in faces}
    for member in members.values():
        if member.mark not in bom or classify(bom[member.mark].specification)["kind"] != "angle":
            problems.append(issue("not_angle", f"杆件实例 {member.id} 件号 {member.mark} 不属于 BOM 角钢"))
        endpoints = []
        instance_faces = set()
        for occurrence in member.occurrences:
            key = (occurrence.view, occurrence.edge)
            edge = edge_obs.get(key)
            if key in used_edges:
                problems.append(issue("edge_reused", f"观察边 {key} 被当作两根实物"))
            if occurrence.view in instance_faces:
                problems.append(issue("instance_collapse", f"{member.id} 合并了同一面内不同杆件，不能仅按件号合并"))
            instance_faces.add(occurrence.view)
            used_edges.add(key)
            if not edge:
                problems.append(issue("unknown_edge", f"{member.id} 引用了不存在的边 {key}"))
                continue
            if edge.line_style != "solid":
                problems.append(issue("dashed_member", f"{member.id} 将虚线/参考线当作实体"))
            if edge.mark != member.mark:
                problems.append(issue("mark_mismatch", f"{member.id} 与 {key} 件号不一致"))
            a = node_to_joint.get((occurrence.view, edge.a))
            b = node_to_joint.get((occurrence.view, edge.b))
            if not a or not b or a == b:
                problems.append(issue("unmapped_joint", f"{key} 端点没有正确映射到两个空间节点"))
            else:
                endpoints.append((a, b))
        if endpoints:
            if any(set(pair) != set(endpoints[0]) for pair in endpoints):
                problems.append(issue("shared_member_conflict", f"{member.id} 在不同面对应了不同端点"))
            member_nodes[member.id] = endpoints[0]
        for view in instance_faces & set(faces):
            face_links[view].update(instance_faces & set(faces) - {view})
        if (member.end_offsets_mm is None) != (member.offset_evidence is None):
            problems.append(issue("offset_evidence", f"{member.id} 端偏移和证据必须同时给出"))
        actual_counts[member.mark] += 1
    for key, edge in edge_obs.items():
        if edge.line_style == "solid":
            row = bom.get(edge.mark)
            if row is None:
                problems.append(issue("unknown_callout", f"{key} 的件号 {edge.mark!r} 无法在 BOM 对应"))
            elif classify(row.specification)["kind"] == "angle" and key not in used_edges:
                problems.append(issue("unassembled_member", f"角钢观察 {key} 未参与装配"))
    for row in bom.values():
        if classify(row.specification)["kind"] == "angle" and actual_counts[row.mark] != row.quantity:
            problems.append(issue("quantity_mismatch", f"件号 {row.mark}: BOM {row.quantity} 根，模型 {actual_counts[row.mark]} 根"))
    for place in placements.values():
        u, v = np.array(place.u_guess), np.array(place.v_guess)
        if abs(np.linalg.norm(u)-1) > .001 or abs(np.linalg.norm(v)-1) > .001 or abs(float(u@v)) > .001:
            problems.append(issue("invalid_basis", f"{place.view} 初始 u/v 必须为正交单位向量"))
        if place.fixed_rotation and not place.orientation_evidence:
            problems.append(issue("orientation_evidence", f"{place.view} 固定视向缺少证据"))
    for rel in drawing.assembly.relations:
        if rel.first not in faces or rel.second not in faces or rel.first == rel.second:
            problems.append(issue("invalid_relation", "面方向关系引用错误"))
    for face in faces.values():
        for dim in face.dimensions:
            if any((face.id, n) not in node_to_joint for n in (dim.a, dim.b)):
                problems.append(issue("unmapped_dimension", f"{face.id}/{dim.id} 尺寸端点未装配；辅助尺寸应作为 calibration"))
    if faces:
        connected, stack = set(), [next(iter(faces))]
        while stack:
            view = stack.pop()
            if view not in connected:
                connected.add(view)
                stack.extend(face_links[view]-connected)
        if connected != set(faces):
            problems.append(issue("disconnected_faces", "有结构面未通过同一根实物与其他面连接：" + ", ".join(set(faces)-connected)))
    unused = set(node_to_joint)-{(r.view, endpoint) for m in members.values() for r in m.occurrences
                                if (r.view, r.edge) in edge_obs
                                for endpoint in (edge_obs[(r.view, r.edge)].a, edge_obs[(r.view, r.edge)].b)}
    if unused:
        problems.append(issue("unused_joints", "装配包含不属于角钢端点的节点，请移除尺寸/虚线节点实例"))
    if problems:
        raise DrawingError(problems)
    return {"bom": bom, "faces": faces, "joints": joints, "members": members, "placements": placements,
            "node_to_joint": node_to_joint, "member_nodes": member_nodes, "uv": uv, "scale": scale,
            "calibrations": calibrations}
