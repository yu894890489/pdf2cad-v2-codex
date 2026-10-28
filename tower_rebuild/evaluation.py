"""Evaluate against separately annotated reference data, not fitted residuals."""
from collections import Counter

import numpy as np


def evaluate(model, reference, node_mapping=None):
    """node_mapping is candidate node ID -> reference node ID.

    A proper rigid alignment removes coordinate-frame choice without accepting
    a reflected assembly. No rescaling is allowed. Missing parts are failures.
    """
    mapping = node_mapping or {key: key for key in model["nodes"]}
    if not reference.get("annotator"):
        raise ValueError("参考数据必须记录 annotator；不能把模型自身输出当人工标准答案。")
    if model["source_sha256"] != reference["source_sha256"]:
        raise ValueError("参考标注与候选模型的源图哈希不一致。")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("节点对照必须一一对应，不能通过合并节点掩盖错误。")
    problems = []
    expected_bom = {r["mark"]: r for r in reference["bom"]}
    actual_bom = {r["mark"]: r for r in model["bom"]}
    tolerance = float(reference.get("tolerance_mm", 1.))
    if tolerance <= 0:
        raise ValueError("参考容差必须为正数。")
    for mark in sorted(set(expected_bom) | set(actual_bom)):
        a, b = actual_bom.get(mark), expected_bom.get(mark)
        if a is None or b is None:
            problems.append(f"BOM 件号 {mark} 缺失或多出")
            continue
        if a["quantity"] != b["quantity"]:
            problems.append(f"BOM 件号 {mark} 数量错误")
        if a["length_mm"] is None or b["length_mm"] is None:
            if a["length_mm"] != b["length_mm"]:
                problems.append(f"BOM 件号 {mark} 长度缺失")
        elif abs(a["length_mm"]-b["length_mm"]) > tolerance:
            problems.append(f"BOM 件号 {mark} 长度错误")
        from .materials import classify
        if classify(a["specification"])["normalized"] != classify(b["specification"])["normalized"]:
            problems.append(f"BOM 件号 {mark} 规格错误")
    expected_edges = Counter((m["mark"], *sorted(m["nodes"])) for m in reference["members"])
    actual_edges = Counter((m["mark"], *sorted(mapping.get(n, "UNMAPPED:"+n) for n in m["nodes"])) for m in model["members"])
    hits = sum((expected_edges & actual_edges).values())
    precision = hits/sum(actual_edges.values()) if actual_edges else 0.
    recall = hits/sum(expected_edges.values()) if expected_edges else 0.
    if expected_edges != actual_edges:
        problems.append("件号/实例数量/端点拓扑与独立标注不一致")
    mapped_nodes = {mapping.get(k, "UNMAPPED:"+k): np.array(v, dtype=float) for k, v in model["nodes"].items()}
    expected_nodes = reference["nodes"]
    if set(mapped_nodes) != set(expected_nodes):
        problems.append("节点集合与独立标注不一致")
    common = sorted(set(mapped_nodes) & set(expected_nodes))
    max_error, rms = None, None
    if len(common) >= 4:
        a = np.array([mapped_nodes[key] for key in common])
        b = np.array([expected_nodes[key] for key in common])
        ac, bc = a-a.mean(axis=0), b-b.mean(axis=0)
        if np.linalg.matrix_rank(bc, tol=.001) < 3:
            problems.append("参考节点没有覆盖三维深度，无法验证空间装配")
        else:
            u, _, vt = np.linalg.svd(ac.T @ bc)
            reflection = np.diag([1., 1., np.linalg.det(u @ vt)])
            aligned = ac @ u @ reflection @ vt
            errors = np.linalg.norm(aligned-bc, axis=1)
            max_error, rms = float(errors.max()), float(np.sqrt(np.mean(errors**2)))
            if max_error > tolerance:
                problems.append(f"对齐后节点最大误差 {max_error:.4f} mm 超过 {tolerance} mm，含镜像/尺度/形状错误")
    else:
        problems.append("不足四个可对应的参考节点，不能验证三维")
    return {"reference_annotator": reference["annotator"], "reference_kind": reference.get("kind", "manual"),
            "source_sha256": model["source_sha256"], "pass": not problems, "problems": problems,
            "member_precision": precision, "member_recall": recall, "max_node_error_mm": max_error,
            "rms_node_error_mm": rms, "tolerance_mm": tolerance,
            "note": "只衡量与这份独立标注的一致性，不能推断未测图纸的准确率。"}
