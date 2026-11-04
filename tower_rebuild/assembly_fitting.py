"""Fit a tapered opposite-face pair using several independently drawn bridges.

Hinge rotations initialize placement; inconsistent observations can then share
their residual through a joint rigid-pose fit. BOM and observed geometry are
never rescaled. A fit is not an engineering acceptance.
"""
import copy
import itertools
import math
from collections import defaultdict

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from .assembly import (TOLERANCE_MM, connected_component,
                       pose_matrix, matrix_pose, transformed, link_residual)
from .io import digest


def validate_taper_rule(rule, edges, face_ids):
    if rule is None:
        return
    if not isinstance(rule, dict) or rule.get('tip_direction') not in {'negative_x', 'positive_x'}:
        raise ValueError('收口方向必须是正视面局部 X 轴的负向或正向。')
    a, b = rule.get('a'), rule.get('b')
    if a not in face_ids or b not in face_ids or a == b:
        raise ValueError('收口约束引用的前后面不存在。')
    for name, fid in [('edge_a', a), ('edge_b', b)]:
        if rule.get(name) not in edges or edges[rule[name]]['face_id'] != fid:
            raise ValueError('收口约束的测宽参考杆件无效。')
    if not isinstance(rule.get('face_ids'), list) or not {a, b} <= set(rule['face_ids']) <= face_ids:
        raise ValueError('收口约束中的结构面无效。')


def taper_measurement(rule, edges, poses):
    """Compare widths at equal longitudinal coordinates in the front frame.

    The interval is only the overlapping projection of the reference rods,
    which need not reach the ultimate crossarm tip. Normal offsets must stay
    on one side so an intersecting pair cannot appear to narrow successfully.
    """
    a, b = edges[rule['edge_a']], edges[rule['edge_b']]
    front = pose_matrix(poses[rule['a']])
    rear = np.asarray(transformed([b['a'], b['b']], poses[rule['b']]))
    rear = (rear-front[:3, 3]) @ front[:3, :3]
    local = np.asarray([a['a'], a['b']])
    lo = max(min(local[:, 0]), min(rear[:, 0]))
    hi = min(max(local[:, 0]), max(rear[:, 0]))
    result = {'tip_direction': rule['tip_direction'], 'reference_marks': [a['mark'], b['mark']],
              'interval_basis': 'common_longitudinal_extent_of_reference_members',
              'narrowing': False, 'tip_width_mm': None, 'root_width_mm': None, 'stations': []}
    if hi-lo <= TOLERANCE_MM or abs(rear[1, 0]-rear[0, 0]) <= TOLERANCE_MM:
        return result
    xs = np.linspace(lo, hi, 5)
    if rule['tip_direction'] == 'positive_x':
        xs = xs[::-1]
    offsets = rear[0, 2]+(xs-rear[0, 0])/(rear[1, 0]-rear[0, 0])*(rear[1, 2]-rear[0, 2])
    widths = np.abs(offsets)
    result.update(tip_width_mm=float(widths[0]), root_width_mm=float(widths[-1]),
                  stations=[{'front_local_x_mm': float(x), 'width_mm': float(w)} for x, w in zip(xs, widths)],
                  narrowing=bool(np.all(offsets*offsets[0] > 0) and min(widths) > TOLERANCE_MM
                                 and widths[-1]-widths[0] > TOLERANCE_MM
                                 and np.all(np.diff(widths) > 0)))
    return result


def _bridges(faces, matches, root, rear):
    lookup = {f['id']: f for f in faces}
    edges = {e['id']: e for f in faces for e in f['edges']}
    pairs = defaultdict(list)
    for match in matches:
        if not match.get('shared_member_candidate'):
            continue
        a, b = edges[match['a']], edges[match['b']]
        # Repeated BOM items do not prove that two observations are one object.
        # Cross-document mark namespaces require explicit mapping, not guessing.
        if any(e.get('bom_quantity') != 1 for e in (a, b)):
            continue
        if lookup[a['face_id']].get('source_id') != lookup[b['face_id']].get('source_id'):
            continue
        pairs[(a['face_id'], b['face_id'])].append((a, b))
        pairs[(b['face_id'], a['face_id'])].append((b, a))
    bridges = []
    for fid in sorted(lookup):
        if fid in {root, rear} or lookup[fid]['units'] != 'mm':
            continue
        choices = [(a, b, c, d) for (a, b), (d, c) in itertools.product(pairs[(root, fid)], pairs[(rear, fid)])
                   if b['id'] != c['id']]
        # Multiple equally plausible members on a bridge need explicit review.
        if len(choices) == 1:
            bridges.extend(choices)
    bridges.sort(key=lambda row: (-row[0]['length'], row[1]['face_id']))
    used = set()
    distinct = []
    for row in bridges:
        ids = {e['id'] for e in row}
        if not ids & used:
            distinct.append(row)
            used |= ids
    return distinct


def _paired_residual(a, b, poses):
    x = np.asarray(transformed([a['a'], a['b']], poses[a['face_id']]))
    y = np.asarray(transformed([b['a'], b['b']], poses[b['face_id']]))
    flip = np.sum((x-y[::-1])**2) < np.sum((x-y)**2)
    return x-(y[::-1] if flip else y), bool(flip)


def _link(a, b, poses):
    _, flip = _paired_residual(a, b, poses)
    ma, mb = pose_matrix(poses[a['face_id']]), pose_matrix(poses[b['face_id']])
    axis = ma[:3, :3] @ (np.asarray(a['b'])-a['a'])
    axis /= np.linalg.norm(axis)
    na, nb = ma[:3, 2], mb[:3, 2]
    angle = math.degrees(math.atan2(np.dot(axis, np.cross(na, nb)), np.dot(na, nb)))
    return {'id': digest([a['id'], b['id']])[:16], 'a': a['id'], 'b': b['id'],
            'angle_deg': angle, 'flip': flip, 'basis': 'multi_bridge_rigid_taper_candidate'}


class AssemblyFitError(ValueError):
    """A rejected fit with measurements that can be shown in the audit report."""
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        failed = [r for r in diagnostics if not r['within_candidate_limits']]
        details = '；'.join(f"件号 {r['mark']}：端点差 {r['endpoint_gap_mm']:.1f} mm"
                           f"（候选上限 {r['gap_limit_mm']:.1f} mm），"
                           f"角度差 {r['angle_error_deg']:.2f}°" for r in failed)
        super().__init__('联合拼接残差过大，未应用装配。'+details+'。请复核杆端、比例和件号。')


def _fit_diagnostics(links, edges, poses):
    result = []
    for link in links:
        a, b = edges[link['a']], edges[link['b']]
        row = {**link_residual(link, edges, poses), 'mark': a['mark'],
               'face_a': a['face_id'], 'face_b': b['face_id'],
               'edge_a': a['id'], 'edge_b': b['id'],
               'gap_limit_mm': max(TOLERANCE_MM, min(a['length'], b['length'])*.03),
               'angle_limit_deg': math.degrees(math.asin(.06))}
        row['within_candidate_limits'] = (row['endpoint_gap_mm'] <= row['gap_limit_mm']
                                         and row['angle_error_deg'] <= row['angle_limit_deg'])
        result.append(row)
    return result


def _refine_rigid_poses(connections, root, poses):
    """Balance measured endpoints without pinning all error to the last seam.

    The front stays fixed. Each other face has only six rigid degrees of
    freedom, with a bounded local search and a full-rank check. Individual rods
    and their lengths cannot be adjusted by this solver.
    """
    ids = sorted(set(poses)-{root})
    anchor = {fid: pose_matrix(pose) for fid, pose in poses.items()}
    pairs = [(x, y, _paired_residual(x, y, poses)[1])
             for a, b, c, d in connections for x, y in ((a, b), (c, d))]
    scale = max(e['length'] for row in connections for e in row)
    if not ids or not math.isfinite(scale) or scale <= TOLERANCE_MM:
        return None

    def matrices(values):
        result = {root: anchor[root]}
        for i, fid in enumerate(ids):
            row = values[6*i:6*i+6]
            matrix = anchor[fid].copy()
            matrix[:3, :3] = Rotation.from_rotvec(row[:3]).as_matrix() @ matrix[:3, :3]
            matrix[:3, 3] += row[3:]*scale
            result[fid] = matrix
        return result

    def residual(values):
        current = matrices(values); parts = []
        for a, b, flip in pairs:
            world = []
            for edge in (a, b):
                matrix = current[edge['face_id']]
                world.append(np.asarray([edge['a'], edge['b']]) @ matrix[:3, :3].T+matrix[:3, 3])
            parts.append((world[0]-(world[1][::-1] if flip else world[1])).ravel())
        return np.concatenate(parts)

    start = np.zeros(6*len(ids))
    bounds = np.tile([math.radians(10)]*3+[.05]*3, len(ids))
    fit = least_squares(residual, start, bounds=(-bounds, bounds), max_nfev=100,
                        ftol=1e-10, xtol=1e-10, gtol=1e-7)
    before = float(np.linalg.norm(residual(start)))
    after = float(np.linalg.norm(fit.fun))
    if (not fit.success or not np.isfinite(fit.x).all() or after >= before-1e-6
            or np.linalg.matrix_rank(fit.jac, tol=1e-4) < len(start)
            or np.any(np.abs(fit.x) >= bounds*.999)):
        return None
    updated = {fid: matrix_pose(matrix) for fid, matrix in matrices(fit.x).items()}
    updated[root] = copy.deepcopy(poses[root])
    return updated, {'basis': 'joint_rigid_endpoint_fit', 'fixed_face_id': root,
                     'before_residual_norm_mm': before, 'after_residual_norm_mm': after,
                     'degrees_of_freedom': len(start), 'full_rank': True,
                     'geometry_modified': False, 'engineering_verified': False}


def fit_tapered_faces(faces, matches, parallel, scene, tip_direction):
    if tip_direction not in {'auto', 'negative_x', 'positive_x'}:
        raise ValueError('请先指定正视图的尖端在左侧（局部 −X）还是右侧（局部 +X）。')
    root = scene.get('front_face_id')
    opposites = [p['b'] if p['a'] == root else p['a'] for p in parallel if root in (p['a'], p['b'])]
    if len(opposites) != 1:
        raise ValueError('正视参考面须有唯一的前后件对应面，才能联合剖面校正收口。')
    rear = opposites[0]
    connections = _bridges(faces, matches, root, rear)
    if len(connections) < 2:
        raise ValueError('至少需要两个可独立校核的横向结构面，且公共杆件的 BOM 数量须为 1；当前证据不足，请核对件号或手动连接。')
    if len(connections) > 4:
        raise ValueError('可用横向面超过四个，请先分组确认结构段，避免混合多个收口段。')
    edges = {e['id']: e for f in faces for e in f['edges']}
    fitted_ids = {root, rear} | {b['face_id'] for _, b, _, _ in connections}
    allowed_pairs = {frozenset((x['id'], y['id'])) for a, b, c, d in connections for x, y in [(a, b), (c, d)]}
    for link in scene['links']:
        touched = {edges[link[k]]['face_id'] for k in ('a', 'b')} & fitted_ids
        if touched and frozenset((link['a'], link['b'])) not in allowed_pairs:
            raise ValueError('待校正面还有其他手动连接，请先解除这些连接或另存项目再校正。')
    first = connections[0]
    if abs(first[0]['b'][0]-first[0]['a'][0]) < first[0]['length']*.5:
        raise ValueError('参考公共杆件不沿正视图横向延伸，无法按左右尖端判断收口。')
    rule = {'a': root, 'b': rear, 'edge_a': first[0]['id'], 'edge_b': first[3]['id'],
            'face_ids': sorted(fitted_ids), 'tip_direction': 'negative_x' if tip_direction=='auto' else tip_direction,
            'basis': 'multiple_shared_member_bridges' if tip_direction=='auto' else 'user_tip_direction_and_multiple_shared_member_bridges'}

    # Cache local frames: residual evaluation never serializes poses or converts
    # matrices to quaternions. This keeps the bounded multistart search usable
    # in the local viewer with several transverse sections.
    points = {e['id']: np.array([e['a'], e['b']]) for row in connections for e in row}
    axes = {eid: (p[1]-p[0])/np.linalg.norm(p[1]-p[0]) for eid, p in points.items()}
    bases = {eid: np.column_stack((u, np.cross([0,0,1], u), [0,0,1])) for eid, u in axes.items()}
    centers = {eid: p.mean(axis=0) for eid, p in points.items()}
    anchor = pose_matrix(scene['poses'][root])

    def attach(a, b, parent, angle, flip):
        axis = parent[:3,:3] @ axes[a['id']]
        normal = parent[:3,2]
        theta = math.radians(angle)
        desired = normal*math.cos(theta)+np.cross(axis,normal)*math.sin(theta)
        target = axis*(-1 if flip else 1)
        matrix = np.eye(4)
        matrix[:3,:3] = np.column_stack((target,np.cross(desired,target),desired)) @ bases[b['id']].T
        matrix[:3,3] = parent[:3,:3] @ centers[a['id']]+parent[:3,3]-matrix[:3,:3] @ centers[b['id']]
        return matrix

    def matrices_for(angles, flips):
        a,b,c,d = first
        matrices = {root: anchor}
        matrices[b['face_id']] = attach(a,b,anchor,angles[0],flips[0])
        matrices[rear] = attach(c,d,matrices[c['face_id']],angles[1],flips[1])
        for i,(a,b,_,_) in enumerate(connections[1:]):
            matrices[b['face_id']] = attach(a,b,anchor,angles[i+2],flips[i+2])
        return matrices

    def residual(angles, flips):
        matrices = matrices_for(angles,flips)
        world = {e['id']: points[e['id']] @ matrices[e['face_id']][:3,:3].T+matrices[e['face_id']][:3,3]
                 for row in connections for e in row}
        parts=[]
        for a,b,c,d in connections:
            for x,y in [(a,b),(c,d)]:
                direct=world[x['id']]-world[y['id']]
                reverse=world[x['id']]-world[y['id']][::-1]
                parts.append((direct if np.sum(direct**2)<=np.sum(reverse**2) else reverse).ravel())
        return np.concatenate(parts)

    count = len(connections)+1
    solutions = []
    for flips in itertools.product((False, True), repeat=count):
        for sign, second in itertools.product((1, -1), repeat=2):
            start = [sign*90, second*90]+[sign*90]*(count-2)
            fit = least_squares(residual, start, args=(flips,), bounds=(-179.99, 179.99),
                                max_nfev=100, ftol=1e-8, xtol=1e-8, gtol=1e-5)
            if not fit.success or np.linalg.matrix_rank(fit.jac, tol=1e-5) < count:
                continue
            poses = {fid: matrix_pose(matrix) for fid,matrix in matrices_for(fit.x,flips).items()}
            poses[root] = scene['poses'][root]
            measurement = taper_measurement(rule, edges, poses)
            if tip_direction=='auto' and not measurement['narrowing']:
                measurement=taper_measurement({**rule,'tip_direction':'positive_x'},edges,poses)
            solutions.append((float(np.linalg.norm(fit.fun)), poses, measurement))
    if not solutions:
        raise ValueError('这些结构面的约束不足或求解未收敛，不能确定收口姿态，请核对剖面。')
    best_cost = min(s[0] for s in solutions)
    eligible = [s for s in solutions if s[0] <= best_cost+max(.1, best_cost*.001) and s[2]['narrowing']]
    if not eligible:
        raise ValueError('图元支持的最佳拼接与所选尖端方向不一致；请核对左右方向或杆件轴线，程序没有翻转尺寸强行拼合。')
    if tip_direction=='auto':
        directions={s[2]['tip_direction'] for s in eligible}
        if len(directions)!=1:raise ValueError('同等残差的候选支持不同收口方向，需要按原图指定尖端方向。')
        rule['tip_direction']=next(iter(directions))
    # Choose the mirror on the user's current side when a layout already exists.
    normal = pose_matrix(scene['poses'][root])[:3, 2]
    old_side = -1.
    if rear in connected_component(root, scene['links'], edges):
        old_side = np.dot(np.asarray(scene['poses'][rear]['position'])-scene['poses'][root]['position'], normal)
    def preference(solution):
        pose = solution[1][rear]
        side = np.dot(np.asarray(pose['position'])-scene['poses'][root]['position'], normal)
        return (side*old_side < 0, solution[0])
    cost, poses, measurement = min(eligible, key=preference)
    links = [_link(x, y, poses) for a, b, c, d in connections for x, y in [(a, b), (c, d)]]
    diagnostics = _fit_diagnostics(links, edges, poses)
    refinement = None
    # A hinge chain pins earlier seams exactly and concentrates drawing noise
    # at the closing seam. Retry rejected chains with all observations balanced.
    if any(not row['within_candidate_limits'] for row in diagnostics):
        refined = _refine_rigid_poses(connections, root, poses)
        if refined:
            candidate_poses, evidence = refined
            candidate_measurement = taper_measurement(rule, edges, candidate_poses)
            side = np.dot(np.asarray(candidate_poses[rear]['position'])-candidate_poses[root]['position'], normal)
            previous_side = np.dot(np.asarray(poses[rear]['position'])-poses[root]['position'], normal)
            if candidate_measurement['narrowing'] and side*previous_side > 0:
                poses, measurement, refinement = candidate_poses, candidate_measurement, evidence
                cost = evidence['after_residual_norm_mm']
                links = [_link(x, y, poses) for a, b, c, d in connections for x, y in [(a, b), (c, d)]]
                diagnostics = _fit_diagnostics(links, edges, poses)
    # A 3% envelope is only a candidate-fit rejection limit. Engineering
    # acceptance still uses the original strict 3 mm / 1 degree checks.
    if any(not row['within_candidate_limits'] for row in diagnostics):
        raise AssemblyFitError(diagnostics)
    result = copy.deepcopy(scene)
    result['poses'].update(poses)
    result['links'] = [link for link in scene['links'] if frozenset((link['a'], link['b'])) not in allowed_pairs]+links
    result['active_face_ids'] = sorted(set(scene['active_face_ids']) | fitted_ids)
    result['taper_rule'] = rule
    result['taper_fit'] = {'basis': 'rigid_faces_only', 'endpoint_residual_norm_mm': cost,
                           'face_count': len(fitted_ids), 'bridge_count': len(connections),
                           'candidate_gap_fraction_limit': .03, 'engineering_verified': False,
                           'seams': diagnostics}
    if refinement:
        result['taper_fit']['refinement'] = refinement
    return result
