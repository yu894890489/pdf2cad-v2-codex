"""Fit a spatial graph to measured face coordinates and independent dimensions.

Global frame: the anchor front face is X/Z, its normal is -Y. Other face
poses are unknown rigid transforms unless an explicit view direction is fixed.
Every quantity residual is checked after solving; no robust loss hides conflicts.
"""
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from .validation import issue


def solve(drawing, compiled, settings):
    c = compiled
    joint_ids = list(c["joints"])
    indices = {key: 3*i for i, key in enumerate(joint_ids)}
    anchor = drawing.assembly.anchor_view
    anchor_rotation = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=float)
    poses, initial = {}, [0.] * (3*len(joint_ids))
    for view, placement in c["placements"].items():
        matrix = np.column_stack((placement.u_guess, placement.v_guess, np.cross(placement.u_guess, placement.v_guess)))
        if view == anchor:
            poses[view] = {"matrix": anchor_rotation, "translation": None, "rotation": None}
        else:
            t = len(initial)
            initial.extend(placement.origin_guess)
            r = None
            if not placement.fixed_rotation:
                r = len(initial)
                initial.extend(Rotation.from_matrix(matrix).as_rotvec())
            poses[view] = {"matrix": matrix, "translation": t, "rotation": r}

    def pose(x, view):
        entry = poses[view]
        r, t = entry["rotation"], entry["translation"]
        matrix = Rotation.from_rotvec(x[r:r+3]).as_matrix() if r is not None else entry["matrix"]
        origin = x[t:t+3] if t is not None else np.zeros(3)
        return matrix, origin

    def seed_nodes(x):
        for key, joint in c["joints"].items():
            points = []
            for ref in joint.observations:
                m, t = pose(x, ref.view)
                points.append(t + m @ np.r_[c["uv"][(ref.view, ref.node)], 0.])
            x[indices[key]:indices[key]+3] = np.mean(points, axis=0)
        return x

    def point(x, key):
        i = indices[key]
        return x[i:i+3]

    def local(x, view, node, matrices):
        matrix, origin = matrices[view]
        return matrix.T @ (point(x, c["node_to_joint"][(view, node)])-origin)

    tol = settings.projection_tolerance_mm
    angle_tol = np.sin(np.deg2rad(settings.angle_tolerance_deg))

    def residuals(x, details=False):
        result, checks = [], []
        matrices = {view: pose(x, view) for view in c["faces"]}

        def add(kind, ref, residual, tolerance, unit="mm"):
            values = np.atleast_1d(residual).astype(float)
            result.extend(values / tolerance)
            if details:
                checks.append({"kind": kind, "ref": ref, "error": float(np.max(np.abs(values))),
                               "tolerance": float(tolerance), "unit": unit,
                               "pass": bool(np.max(np.abs(values)) <= tolerance + 1e-6)})

        for key in c["node_to_joint"]:
            add("projection", "/".join(key), local(x, *key, matrices)-np.r_[c["uv"][key], 0.], tol)
        for face in c["faces"].values():
            for dim in face.dimensions:
                # Dimensions may refer to auxiliary nodes: require assembled endpoints.
                pa = local(x, face.id, dim.a, matrices)
                pb = local(x, face.id, dim.b, matrices)
                delta = pb-pa
                actual = np.linalg.norm(delta) if dim.kind == "distance" else abs(delta[0 if dim.kind == "x" else 1])
                add("dimension", f"{face.id}/{dim.id}", actual-dim.value_mm, dim.tolerance_mm)
        for member in c["members"].values():
            if member.end_offsets_mm is not None:
                a, b = c["member_nodes"][member.id]
                length = np.linalg.norm(point(x, b)-point(x, a))-sum(member.end_offsets_mm)
                add("cut_length", member.id, length-c["bom"][member.mark].length_mm, 1.)
        for rel in drawing.assembly.relations:
            na, nb = matrices[rel.first][0][:, 2], matrices[rel.second][0][:, 2]
            value = na @ nb if rel.kind == "perpendicular" else np.cross(na, nb)
            add(rel.kind, f"{rel.first}/{rel.second}", value, angle_tol, "normal component")
        return checks if details else np.array(result)

    x0 = seed_nodes(np.array(initial))
    if not len(x0) or len(x0) > 3000:
        raise ValueError("当前求解器要求 1..3000 个变量，请按装配分段处理大图。")
    random = np.random.default_rng(20261022)
    solutions = []
    any_free = any(p["rotation"] is not None for p in poses.values())
    for trial in range(settings.solver_starts if any_free else 1):
        guess = x0.copy()
        if trial:
            for entry in poses.values():
                r = entry["rotation"]
                if r is not None:
                    guess[r:r+3] += random.normal(0, 1.5, 3)
            seed_nodes(guess)
        fitted = least_squares(residuals, guess, loss="linear", x_scale="jac", max_nfev=settings.solver_max_evaluations,
                               ftol=1e-10, xtol=1e-10, gtol=1e-10)
        solutions.append(fitted)
    best = min(solutions, key=lambda s: s.cost)
    checks = residuals(best.x, details=True)
    problems = []
    if not best.success:
        problems.append(issue("solver_not_converged", "求解未收敛，不能接受当前坐标"))
    failed = [row for row in checks if not row["pass"]]
    if failed:
        problems.append(issue("constraint_conflict", f"{len(failed)} 项约束超差，见逐项校核表"))
    # Scale columns before rank estimation so radians and mm are comparable.
    jac = np.asarray(best.jac)
    norms = np.maximum(np.linalg.norm(jac, axis=0), 1e-12)
    singular = np.linalg.svd(jac / norms, compute_uv=False)
    threshold = max(jac.shape)*np.finfo(float).eps*max(singular[0] if len(singular) else 1, 1)*1000
    rank = int(np.sum(singular > max(threshold, 1e-7)))
    dof = len(best.x)-rank
    if dof:
        problems.append(issue("underdetermined", f"空间约束不足，还剩 {dof} 个自由度；需要补充视图、共用杆件或尺寸"))
    coordinates = best.x[:3*len(joint_ids)].reshape(-1, 3)
    spatial_rank = np.linalg.matrix_rank(coordinates-coordinates.mean(axis=0), tol=max(tol*.1, .001))
    if spatial_rank < 3:
        problems.append(issue("flat_model", "装配结果仍在一个平面内，尚不能证明箱体深度"))
    alternatives = 0
    for result in solutions:
        if result is best or not result.success or np.max(np.abs(residuals(result.x))) > 1+1e-6:
            continue
        difference = np.max(np.linalg.norm((result.x[:len(coordinates)*3]-best.x[:len(coordinates)*3]).reshape(-1, 3), axis=1))
        if difference > tol*4:
            alternatives += 1
    if alternatives:
        problems.append(issue("ambiguous_assembly", f"找到 {alternatives} 个同样满足尺寸但形状/翻折方向不同的解，需要补充视向证据"))
    # Detect visually plausible but disconnected rods even if view associations pass.
    adjacency = {key: set() for key in joint_ids}
    for a, b in c["member_nodes"].values():
        adjacency[a].add(b)
        adjacency[b].add(a)
        if np.linalg.norm(point(best.x, a)-point(best.x, b)) <= .001:
            problems.append(issue("collapsed_member", f"节点 {a}/{b} 之间杆件长度为零"))
    connected, stack = set(), [joint_ids[0]]
    while stack:
        key = stack.pop()
        if key not in connected:
            connected.add(key)
            stack.extend(adjacency[key]-connected)
    if connected != set(joint_ids):
        problems.append(issue("disconnected_rods", "杆件图包含未连接的部分，请核对节点对应"))
    missing_offsets = sum(m.end_offsets_mm is None for m in c["members"].values())
    if missing_offsets:
        problems.append(issue("cut_geometry_unknown", f"{missing_offsets} 根缺少端部偏移证据，仅建立连接轴线；BOM 下料长度单独保留", "warning"))
    return {"nodes": {key: point(best.x, key).tolist() for key in joint_ids},
            "poses": {view: {"matrix": pose(best.x, view)[0].tolist(), "origin": pose(best.x, view)[1].tolist()} for view in c["faces"]},
            "checks": checks, "issues": problems, "degrees_of_freedom": dof,
            "rank": rank, "variables": len(best.x), "starts": len(solutions), "alternative_solutions": alternatives,
            "max_normalized_residual": float(np.max(np.abs(residuals(best.x))))}
