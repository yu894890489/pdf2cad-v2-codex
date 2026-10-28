"""Independent synthetic fixture, NOT a reconstruction of sheet 9.

Generate different known cuboids to exercise scale/instance/face generality.
"""
import math
from pathlib import Path

from tower_rebuild.io import digest, write_json
from tower_rebuild.schema import Drawing


def box_example(width=2400., depth=1200., height=1500.):
    def evidence(text):
        return {"page": 1, "bbox": [.05, .05, .95, .95], "text": "人工构造的测试数据：" + text}
    xyz = {"A": (0, 0, 0), "B": (width, 0, 0), "C": (width, 0, height), "D": (0, 0, height),
           "E": (0, depth, 0), "F": (width, depth, 0), "G": (width, depth, height), "H": (0, depth, height)}
    edges = {"ab": ("A", "B", "101"), "dc": ("D", "C", "101"), "ef": ("E", "F", "101"), "hg": ("H", "G", "101"),
             "ae": ("A", "E", "102"), "bf": ("B", "F", "102"), "cg": ("C", "G", "102"), "dh": ("D", "H", "102"),
             "ad": ("A", "D", "103"), "bc": ("B", "C", "103"), "eh": ("E", "H", "103"), "fg": ("F", "G", "103"),
             "ac": ("A", "C", "104")}
    views = [
        ("front", "正视面", (0, 0, 0), (1, 0, 0), (0, 0, 1), "ABCD"),
        ("rear", "背面", (0, depth, 0), (1, 0, 0), (0, 0, 1), "EFGH"),
        ("top", "上面", (0, 0, height), (1, 0, 0), (0, 1, 0), "DCGH"),
        ("bottom", "底面", (0, 0, 0), (1, 0, 0), (0, 1, 0), "ABFE"),
        ("left", "左端面", (0, 0, 0), (0, 1, 0), (0, 0, 1), "AEHD"),
        ("right", "右端面", (width, 0, 0), (0, 1, 0), (0, 0, 1), "BFGC")]
    faces, placements, occurrences = [], [], {key: [] for key in edges}
    joints = {key: {"id": key, "observations": [], "evidence": evidence(key)} for key in xyz}
    for key, title, origin, u, v, node_names in views:
        nodes, drawn, dims = [], [], []
        for node in node_names:
            delta = [a-b for a, b in zip(xyz[node], origin)]
            xy = [sum(a*b for a, b in zip(delta, basis)) for basis in (u, v)]
            nodes.append({"id": node, "xy": xy, "evidence": evidence(f"{key}/{node}")})
            joints[node]["observations"].append({"view": key, "node": node})
        for eid, (a, b, mark) in edges.items():
            if a in node_names and b in node_names:
                drawn.append({"id": eid, "mark": mark, "a": a, "b": b, "line_style": "solid", "evidence": evidence(eid)})
                occurrences[eid].append({"view": key, "edge": eid})
                dims.append({"id": "dim_"+eid, "a": a, "b": b, "kind": "distance", "value_mm": math.dist(xyz[a], xyz[b]),
                             "tolerance_mm": 1., "evidence": evidence("independent known dimension " + eid)})
        faces.append({"id": key, "title": title, "evidence": evidence(title), "coordinate_units": "mm", "origin": [0, 0],
                      "nodes": nodes, "edges": drawn, "dimensions": dims})
        placements.append({"view": key, "origin_guess": origin, "u_guess": u, "v_guess": v,
                           "fixed_rotation": True, "orientation_evidence": evidence("known cuboid axes")})
    bom = [{"mark": mark, "specification": "Q345L63×5", "length_mm": length, "quantity": quantity, "evidence": evidence(mark)}
           for mark, length, quantity in [("101", width, 4), ("102", depth, 4), ("103", height, 4), ("104", math.hypot(width, height), 1)]]
    bom.append({"mark": "901", "specification": "Q345-12×643", "length_mm": 885, "quantity": 2, "evidence": evidence("excluded plate")})
    members = [{"id": eid, "mark": mark, "occurrences": occurrences[eid], "end_offsets_mm": [0, 0],
                "offset_evidence": evidence("synthetic joint-to-joint length equals cut length")}
               for eid, (a, b, mark) in edges.items()]
    return Drawing.model_validate({"title": "合成箱体测试 · 非原图复原", "source_sha256": digest(xyz), "page_sizes": [[1000, 1000]],
               "bom": bom, "faces": faces, "assembly": {"anchor_view": "front", "joints": list(joints.values()),
               "members": members, "placements": placements, "relations": []}}), xyz


if __name__ == "__main__":
    drawing, expected = box_example()
    root = Path(__file__).resolve().parent
    write_json(root / "box.observations.json", drawing.model_dump(mode="json"))
    write_json(root / "box.expected.json", expected)
    reference = {"annotator": "独立合成箱体公式", "kind": "synthetic", "source_sha256": drawing.source_sha256,
                 "tolerance_mm": 1., "nodes": expected, "bom": [b.model_dump(mode="json") for b in drawing.bom], "members": []}
    # Expected endpoint labels originate from the fixture design, not solved XYZ.
    observations = {(f.id, e.id): e for f in drawing.faces for e in f.edges}
    for member in drawing.assembly.members:
        first = member.occurrences[0]
        edge = observations[(first.view, first.edge)]
        reference["members"].append({"mark": member.mark, "nodes": [edge.a, edge.b]})
    write_json(root / "box.reference.json", reference)
    print(root / "box.observations.json")
