import csv
import base64
import html
import shutil
from pathlib import Path

import ezdxf
import numpy as np

from .io import digest, file_digest, read_json, write_json
from .materials import classify
from .pdf_input import crop
from .schema import Review
from .solver import solve
from .validation import DrawingError, compile_drawing, issue

REVIEW_CHECKS = {"bom", "callouts", "joints", "faces", "dimensions", "assembly"}


def build(drawing, run_dir, settings, review_path=None):
    run_dir = Path(run_dir)
    site = run_dir / "site"
    site.mkdir(parents=True, exist_ok=True)
    compiled, solution = None, None
    problems = []
    for message in [*drawing.issues, *drawing.assembly.issues, *[s for f in drawing.faces for s in f.issues]]:
        problems.append(issue("unresolved_observation", message))
    manifest_path = run_dir / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else None
    if manifest:
        source = Path(manifest["source"])
        if manifest["source_sha256"] != drawing.source_sha256 or not source.exists() or file_digest(source) != drawing.source_sha256:
            problems.append(issue("source_changed", "当前 PDF 与识别数据的源文件哈希不一致"))
            manifest = None
        elif [p["size_pt"] for p in manifest["pages"]] != [list(p) for p in drawing.page_sizes]:
            problems.append(issue("page_metadata_changed", "页面尺寸与原 PDF 不一致"))
            manifest = None
    else:
        problems.append(issue("source_unavailable", "没有绑定原 PDF；仅可作为结构化输入示例/候选模型，不能完成原图核对"))
    try:
        compiled = compile_drawing(drawing)
        print(f"[solve] {len(compiled['members'])} members, {len(compiled['faces'])} faces", flush=True)
        solution = solve(drawing, compiled, settings)
        problems.extend(solution["issues"])
    except DrawingError as exc:
        problems.extend(exc.issues)
    result_hash = digest({"drawing": drawing.model_dump(mode="json"), "settings": settings.model_dump(mode="json"),
                          "solution": solution})
    drawing_hash = digest(drawing.model_dump(mode="json"))
    accepted = False
    if review_path:
        review = Review.model_validate(read_json(review_path))
        accepted = (review.accepted and review.drawing_sha256 == drawing_hash and review.result_sha256 == result_hash
                    and set(review.checks) == REVIEW_CHECKS)
        if not accepted:
            problems.append(issue("review_invalid", "核对记录未勾选全部检查，或不属于当前输入/计算结果"))
    errors = [p for p in problems if p["severity"] == "error"]
    status = "inconsistent" if errors else ("reviewed_frame" if accepted else "needs_review")
    if not accepted:
        problems.append(issue("review_required", "自动识别及几何一致性不能替代原图核对；逐面确认后填写 review-template.json", "warning"))
    model = {"schema_version": "1.0", "title": drawing.title, "units": "mm", "status": status,
             "scope": "rod_connection_axes", "source_sha256": drawing.source_sha256,
             "drawing_sha256": drawing_hash, "result_sha256": result_hash, "issues": problems,
             "bom": [{**r.model_dump(mode="json"), "classification": classify(r.specification)} for r in drawing.bom],
             "members": [], "faces": [], "nodes": solution["nodes"] if solution else {}}
    if solution:
        for member in drawing.assembly.members:
            a, b = compiled["member_nodes"][member.id]
            start, end = np.array(solution["nodes"][a]), np.array(solution["nodes"][b])
            row = compiled["bom"][member.mark]
            span = float(np.linalg.norm(end-start))
            model["members"].append({"id": member.id, "mark": member.mark, "nodes": [a, b],
                "start": start.tolist(), "end": end.tolist(), "axis_length_mm": span,
                "cut_length_mm": row.length_mm, "end_offsets_mm": member.end_offsets_mm,
                "section": row.specification, "faces": [r.view for r in member.occurrences]})
        for face in drawing.faces:
            entry = {"id": face.id, "title": face.title, "pose": solution["poses"][face.id],
                     "members": [m["id"] for m in model["members"] if face.id in m["faces"]]}
            if manifest:
                render_face_overlay(manifest["source"], face, compiled, solution, site)
                entry["source_image"] = f"faces/{face.id}.png"
                if face.coordinate_units == "page":
                    entry["overlay_image"] = f"faces/{face.id}.svg"
            model["faces"].append(entry)
    elif manifest:
        # Even a rejected graph must expose its source regions for correction.
        for face in drawing.faces:
            path = site / "faces" / f"{face.id}.png"
            metadata = crop(manifest["source"], face.evidence, path)
            entry = {"id": face.id, "title": face.title, "members": [], "source_image": f"faces/{face.id}.png"}
            if face.coordinate_units == "page":
                render_observation_overlay(face, site, metadata)
                entry.update(overlay_image=f"faces/{face.id}-observations.svg", overlay_kind="observations")
            model["faces"].append(entry)
    report = {"status": status, "scope": model["scope"], "drawing_sha256": drawing_hash, "result_sha256": result_hash,
              "issues": problems, "solver": {k: v for k, v in (solution or {}).items() if k not in {"nodes", "poses", "issues"}},
              "calibration_checks": compiled["calibrations"] if compiled else [],
              "limitations": ["当前输出为杆件连接轴线；孔位、角钢转角、切角、火曲、板件未建模。",
                              "小投影误差只说明给定观察与模型一致，不证明件号识别无误。",
                              "多初值搜索可以发现歧义，不能证明所有非线性解已被穷尽。"]}
    write_json(site / "model.json", model)
    write_json(site / "report.json", report)
    write_json(site / "observations.json", drawing.model_dump(mode="json"))
    write_json(run_dir / "review-template.json", {"drawing_sha256": drawing_hash, "result_sha256": result_hash,
               "reviewer": "填写实际核对人", "accepted": False, "checks": [], "notes": "逐面核对原图后填写，不能由视觉模型自动批准。"})
    with (site / "bom.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["件号", "原始规格", "分类", "下料长度mm", "BOM数量", "实例数量"])
        for row in model["bom"]:
            writer.writerow([row["mark"], row["specification"], row["classification"]["kind"], row["length_mm"],
                             row["quantity"], sum(m["mark"] == row["mark"] for m in model["members"])])
    write_dxf(model, site / "candidate_axes.dxf")
    approved = site / "reviewed_axes.dxf"
    if status == "reviewed_frame":
        shutil.copy2(site / "candidate_axes.dxf", approved)
    elif approved.exists():
        approved.unlink()  # Never leave a stale approved artifact after a failed rebuild.
    for asset in (Path(__file__).parent / "web").iterdir():
        if asset.is_file():
            shutil.copy2(asset, site / asset.name)
    write_json(run_dir / "build-settings.json", settings.model_dump(mode="json"))
    print(f"[result] {status}; {len(model['members'])} members; {len(errors)} blocking issues", flush=True)
    return report


def write_dxf(model, path):
    doc = ezdxf.new("R2010")
    doc.units = 4
    doc.header["$INSUNITS"] = 4
    space = doc.modelspace()
    space.add_text(f"STATUS: {model['status']} | CONNECTION AXES ONLY | units mm", dxfattribs={"height": 60})
    for member in model["members"]:
        layer = f"M_{member['id']}"
        if layer not in doc.layers:
            doc.layers.new(layer)
        line = space.add_line(member["start"], member["end"], dxfattribs={"layer": layer})
        if "TOWER_REBUILD" not in doc.appids:
            doc.appids.new("TOWER_REBUILD")
        line.set_xdata("TOWER_REBUILD", [(1000, member["id"]), (1000, member["mark"]), (1040, member["cut_length_mm"])])
    doc.saveas(path)


def render_face_overlay(pdf, face, compiled, solution, site):
    metadata = crop(pdf, face.evidence, site / "faces" / f"{face.id}.png")
    if face.coordinate_units != "page":
        return
    w, h = metadata["width"], metadata["height"]
    bx, by, ex, ey = face.evidence.bbox
    pose = solution["poses"][face.id]
    matrix, origin = np.array(pose["matrix"]), np.array(pose["origin"])
    scale = np.array(compiled["scale"][face.id])

    def pixel(joint):
        uv = (matrix.T @ (np.array(solution["nodes"][joint])-origin))[:2]
        xy = np.array(face.origin) + uv / scale
        return ((xy[0]-bx)*w/(ex-bx), (xy[1]-by)*h/(ey-by))

    encoded = base64.b64encode((site / "faces" / f"{face.id}.png").read_bytes()).decode("ascii")
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">',
             f'<image href="data:image/png;base64,{encoded}" width="{w}" height="{h}"/>']
    for member in compiled["members"].values():
        if not any(o.view == face.id for o in member.occurrences):
            continue
        a, b = compiled["member_nodes"][member.id]
        x1, y1 = pixel(a)
        x2, y2 = pixel(b)
        lines.append(f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" stroke="#009b72" stroke-width="2" stroke-opacity=".8"/>')
        lines.append(f'<text x="{(x1+x2)/2:.3f}" y="{(y1+y2)/2:.3f}" font-size="12" fill="#bf2951">{html.escape(member.mark)}</text>')
    lines.append("</svg>")
    (site / "faces" / f"{face.id}.svg").write_text("\n".join(lines), encoding="utf-8")


def render_observation_overlay(face, site, metadata):
    """Show rejected 2D observations for review; do not invent 3D positions."""
    bx, by, ex, ey = face.evidence.bbox
    w, h = 1200., 1200.*metadata["height"]/metadata["width"]
    encoded = base64.b64encode((site / "faces" / f"{face.id}.png").read_bytes()).decode("ascii")
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">',
             f'<image href="data:image/png;base64,{encoded}" width="{w}" height="{h}" preserveAspectRatio="none"/>']
    nodes = {n.id: ((n.xy[0]-bx)*w/(ex-bx), (n.xy[1]-by)*h/(ey-by)) for n in face.nodes}
    for edge in face.edges:
        if edge.line_style != "solid" or edge.a not in nodes or edge.b not in nodes:
            continue
        x1, y1 = nodes[edge.a]
        x2, y2 = nodes[edge.b]
        lines.append(f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" stroke="#d76d21" stroke-width="2"/>')
        lines.append(f'<text x="{(x1+x2)/2:.3f}" y="{(y1+y2)/2:.3f}" font-size="12" fill="#bb341c">{html.escape(edge.mark or "?")}</text>')
    lines.append("</svg>")
    (site / "faces" / f"{face.id}-observations.svg").write_text("\n".join(lines), encoding="utf-8")
