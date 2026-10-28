"""Separate BOM, per-face graph and instance assembly passes; resume from cache."""
import json
from pathlib import Path

from .io import read_json, write_json
from .materials import classify
from .pdf_input import crop
from .schema import Assembly, Audit, BomResult, Drawing, Evidence, Face, Layout
from .vlm import VlmClient
from .native_bom import read_native_bom
from .layout_refinement import refine_face_regions


def ask_with_failure(client, stages, stage, schema, prompt, images=(), fallback=None):
    """Keep failed observations visible for review, without fabricating data."""
    try:
        return client.ask(stage, schema, prompt, images)
    except ValueError as exc:
        if fallback is None:
            raise
        message = f"{stage} 识别响应未通过格式校验，必须核对：{str(exc)[:1500]}"
        write_json(stages / f"{stage}-failed.json", {"stage": stage, "error": str(exc)})
        print(f"[needs-review] {stage}; invalid response retained in cache", flush=True)
        return fallback(message)


def extract(manifest, run_dir, settings):
    run_dir = Path(run_dir)
    stages = run_dir / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    issues, regions, bom_by_mark, faces = [], [], {}, []
    client = VlmClient(settings, run_dir / "cache")
    try:
        client.probe()
        native_rows, native_regions, native_issues = read_native_bom(manifest["source"])
        write_json(stages / "native-bom.json", {"rows": [r.model_dump(mode="json") for r in native_rows], "issues": native_issues})
        for page in manifest["pages"]:
            page_num = page["page"]
            if page["rotation"]:
                raise ValueError("页面旋转元数据尚未支持，请先将旋转固化。")
            print(f"[layout] page {page_num}", flush=True)
            override = run_dir / "overrides" / f"layout-{page_num}.json"
            layout = Layout.model_validate(read_json(override)) if override.exists() else client.ask(f"layout-{page_num}", Layout,
                f"这是第 {page_num} 页整页图。先定位全部 BOM 表格、正视单线图、各实体结构面、局部详图。"
                "给每个区域独立 bbox 和标题，避免将相邻面的杆件混在一起。尤其中央上、中、下三块各自分离的桁架不能合成一个面，"
                "每个面bbox只包住那一块，不能用整个结构图的标题栏代替面标题。构件BOM与螺栓表必须分开，螺栓表标reference。"
                "含多根角钢及件号的1-1、2-2、3-3等剖面/局部装配图必须标face，不能因标题为详图就排除。"
                "仅单个板件、垫块、孔位加工详图标reference。issues只写无法确定/冲突的问题，不写正常处理说明。不要提取杆件。",
                [run_dir / page["overview"]])
            write_json(stages / f"layout-{page_num}.json", layout.model_dump(mode="json"))
            issues.extend(layout.issues)
            for region in layout.regions:
                if region.evidence.page != page_num:
                    raise ValueError("布局页码与当前页不一致")
                region.id = f"p{page_num}_{region.id}"
                regions.append(region)
        if native_rows and not native_issues:
            # Preserve deterministic native values; do not re-OCR a guessed crop.
            bom_by_mark = {r.mark: r for r in native_rows}
            regions = [r for r in regions if r.kind != "bom"] + native_regions
            print(f"[bom-native] {len(native_rows)} rows from PDF table cells", flush=True)
        changes = refine_face_regions(manifest["source"], regions)
        write_json(stages / "layout-refinement.json", changes)
        write_json(stages / "resolved-layout.json", [r.model_dump(mode="json") for r in regions])
        if not any(r.kind == "bom" for r in regions):
            raise ValueError("未发现 BOM。先纠正布局记录，不能跳过 BOM 建模。")
        # BOM is completely processed before any structure graph request.
        for region in ([] if native_rows and not native_issues else [r for r in regions if r.kind == "bom"]):
            x0, y0, x1, y1 = region.evidence.bbox
            w, h = manifest["pages"][region.evidence.page-1]["size_pt"]
            # Table strips with overlap preserve row readability on large sheets.
            ratio = (y1-y0)*h / ((x1-x0)*w)
            count = max(1, int(ratio * 1600 / settings.bom_tile_height + .999))
            for part in range(count):
                pad = .06 / count
                box = (x0, max(y0, y0+(y1-y0)*part/count-pad), x1,
                       min(y1, y0+(y1-y0)*(part+1)/count+pad))
                evidence = Evidence(page=region.evidence.page, bbox=box, text=region.title)
                path = stages / f"{region.id}-bom-{part}.png"
                crop(manifest["source"], evidence, path, settings.image_max_side)
                print(f"[bom] {region.id} strip {part+1}/{count}", flush=True)
                override = run_dir / "overrides" / f"bom-{region.id}-{part}.json"
                rows = BomResult.model_validate(read_json(override)) if override.exists() else client.ask(f"bom-{region.id}-{part}", BomResult,
                    f"先逐行抄录 BOM。图片对应原页 {evidence.page} bbox={box}。保留原始规格、长度和数量；"
                    "Q345-12×643 中 12 和 643 属于规格，不能冒充长度。不要遗漏板件，它们用于排除干扰。"
                    "只收录完整行，被截断/看不清的行记录 issues；本轮不要推断三维。", [path])
                issues.extend(rows.issues)
                for row in rows.rows:
                    old = bom_by_mark.get(row.mark)
                    if old and (old.specification, old.length_mm, old.quantity) != (row.specification, row.length_mm, row.quantity):
                        issues.append(f"BOM 件号 {row.mark} 在重复区域识别冲突，请核对原行")
                    else:
                        bom_by_mark[row.mark] = row
        write_json(stages / "bom.json", [r.model_dump(mode="json") for r in bom_by_mark.values()])
        bom_context = [{**r.model_dump(mode="json"), "classification": classify(r.specification)} for r in bom_by_mark.values()]
        front_candidates = []
        for region in [r for r in regions if r.kind in {"front", "face"}]:
            path = stages / f"{region.id}.png"
            crop(manifest["source"], region.evidence, path, settings.image_max_side)
            print(f"[face] {region.id} {region.title}", flush=True)
            override = run_dir / "overrides" / f"face-{region.id}.json"
            face = Face.model_validate(read_json(override)) if override.exists() else ask_with_failure(client, stages, f"face-{region.id}", Face,
                f"只分析当前面，id={region.id}。图片对应原页 {region.evidence.page} bbox={region.evidence.bbox}。"
                "coordinate_units 必须 page；xy 为整页归一坐标。origin 选该面明确节点。"
                "每条实线角钢识别件号、两端节点；交叉非连接时不要拆节点。虚线标 dashed，板件不列为角钢。"
                "每个面独立编号，不要现在合并不同面。通过已标注横/纵尺寸提取 calibrations（尺寸界线两端），"
                "不可用 BOM 下料长度作视图比例。缺少尺度标注应报 issues。uniform_scale 默认 false。"
                "旋转摆放的面可使用 distance 比例标注；只有图确实等比例时才能 uniform_scale=true。"
                "尺寸端点是实际节点时再添加 dimensions；尺寸为投影距离时用 x/y。正视单线图可以结合结构详图核对，"
                "但缺失件号不能猜。BOM 如下：" + json.dumps(bom_context, ensure_ascii=False), [path],
                fallback=lambda message: Face(id=region.id, title=region.title, evidence=region.evidence,
                    origin=region.evidence.bbox[:2], nodes=[], edges=[], issues=[message]))
            face.id = region.id
            if face.coordinate_units != "page" or face.evidence.page != region.evidence.page:
                raise ValueError("视觉识别必须返回当前原页坐标，不能自行生成毫米坐标。")
            write_json(stages / f"face-{region.id}.json", face.model_dump(mode="json"))
            faces.append(face)
            if region.kind == "front":
                front_candidates.append(face.id)
        if not faces or not front_candidates:
            raise ValueError("缺少正视图或结构面，请检查布局。")
        context = {"front_candidates": front_candidates, "bom": bom_context,
                   "faces": [f.model_dump(mode="json") for f in faces]}
        print("[assembly] match physical instances and joints", flush=True)
        override = run_dir / "overrides" / "assembly.json"
        assembly = Assembly.model_validate(read_json(override)) if override.exists() else ask_with_failure(client, stages, "assembly", Assembly,
            "根据已经提取的数据提出各面的物理装配关系。正视图垂直地面作为 anchor_view。"
            "joint.observations 必须覆盖所有实体边端点。member.occurrences 将同一根实物在各面的投影合并，"
            "同件号多根必须保留多个实例，数量须与 BOM 对齐。下一个面通过共享实物连接；无法对应报告 issues。"
            "不要重造 BOM 和图上边。placements 只是初值；只有有明确视向证据时才能 fixed_rotation=true。"
            "正视图世界 u=X,v=Z；上/下面结合实际坡度，不可无证据假设水平；平行/垂直关系写 relations。"
            "没有明确端部尺寸，end_offsets_mm 和 offset_evidence 必须 null。数据：" + json.dumps(context, ensure_ascii=False),
            fallback=lambda message: Assembly(anchor_view=front_candidates[0], joints=[], members=[], placements=[], issues=[message]))
        if assembly.anchor_view not in front_candidates:
            raise ValueError("装配基准必须是已识别的正视图。")
        drawing = Drawing(title=Path(manifest["source"]).stem, source_sha256=manifest["source_sha256"],
                          page_sizes=[p["size_pt"] for p in manifest["pages"]], bom=list(bom_by_mark.values()),
                          faces=faces, assembly=assembly, issues=issues)
        write_json(run_dir / "observations.json", drawing.model_dump(mode="json"))
        if settings.audit:
            # A separate pass can catch discrepancies but is not independent proof.
            for face in faces:
                print(f"[audit] {face.id}", flush=True)
                audit = ask_with_failure(client, stages, f"audit-{face.id}", Audit,
                    "复核：逐项对照图上件号、实线连接、虚线排除、已标注尺寸和当前识别结果。列出具体漏件/多件/错件/"
                    "交叉误连/矛盾/无法读清之处；只输出 findings，不要赞同之前的猜测。"
                    "当前面和 BOM：" + json.dumps({"face": face.model_dump(mode="json"), "bom": bom_context}, ensure_ascii=False),
                    [stages / f"{face.id}.png"], fallback=lambda message: Audit(findings=[message]))
                drawing.issues.extend(f"复核 {face.id}: {issue}" for issue in audit.findings)
            write_json(run_dir / "observations.json", drawing.model_dump(mode="json"))
        return drawing
    finally:
        client.close()
