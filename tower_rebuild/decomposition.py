"""Split native PDF tables from drawing graphics before structural reasoning."""
from collections import Counter
import json
from pathlib import Path
import shutil

import fitz

from .io import file_digest, read_json, write_json
from .materials import classify
from .native_bom import BomIntegrityError, check_bom_observations, compact, read_native_bom, split_cell_rows
from .pdf_input import prepare
from .primitives import associate_callouts, contained, detect_dash_candidates, extract_primitives


def compact_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def table_seeds(page, native_rows, native_regions, table_options=None):
    """A sheet grid may merge every view. Use actual header cells, not that grid's bbox."""
    clip = page.rect
    if native_regions:
        left = min(r.evidence.bbox[0] for r in native_regions)*page.rect.width
        clip = fitz.Rect(max(0, left-6), 0, page.rect.width, page.rect.height)
    finder = page.find_tables(clip=clip, **(table_options or {}))
    candidates = []
    for table in finder.tables:
        raw = table.extract()
        cells = [(ri, ci, text, table.rows[ri].cells[ci]) for ri, row in enumerate(raw)
                 for ci, text in enumerate(row) if text is not None and table.rows[ri].cells[ci]]
        for ri, ci, text, cell in cells:
            label = compact(text)
            kind = None
            if label in {"构件明细表", "材料明细表", "零件明细表", "BILLOFMATERIALS", "PARTSLIST"}:
                kind = "bom"
            elif "明细表" in label and any(t in label for t in ("螺栓", "螺母", "垫圈", "脚钉")) and len(label) < 50:
                kind = "hardware"
            elif label.endswith("明细表") and len(label) < 30:
                # OCR insertion order can permute widely spaced title words.
                # Actual BOM rows below this title must still confirm its kind.
                kind = "bom"
            elif "有限公司" in label and len(label) < 70:
                kind = "title_block"
            if kind is None:
                continue
            x0, y0, x1, y1 = cell
            related = [c for _, _, _, c in cells if c[0] >= x0-.8 and c[2] <= x1+.8 and c[1] >= y0-.8]
            if kind == "bom":
                matches = [r for r in native_rows if x0 <= (r.evidence.bbox[0]+r.evidence.bbox[2])/2*page.rect.width < x1
                           and r.evidence.bbox[1]*page.rect.height >= y1-.8]
                if not matches:
                    continue
                bottom = max(r.evidence.bbox[3]*page.rect.height for r in matches)
                totals = [c for _, _, t, c in cells if "合计" == compact(t) and x0-.8 <= c[0] < x1-.8
                          and bottom-.8 <= c[1] < bottom+150]
                if totals:
                    bottom = min(c[3] for c in totals)
            elif kind == "hardware":
                totals = [c for _, _, t, c in cells if "合计" == compact(t) and x0-.8 <= c[0] < x1-.8 and c[1] > y1]
                if not totals:
                    continue
                bottom = min(c[3] for c in totals)
            else:
                bottom = max(c[3] for c in related)
            candidates.append({"kind": kind, "title": text, "bbox_pt": [x0, y0, x1, bottom],
                               "detection": "native table header cell and content/total boundary"})
    # Generic compact tables with real text headers (for PDFs without known titles).
    for table in finder.tables:
        rect = list(table.bbox)
        if any(contained(seed["bbox_pt"], rect) or contained(rect, seed["bbox_pt"]) for seed in candidates):
            continue
        if table.row_count < 2 or table.col_count < 2 or fitz.Rect(rect).get_area() > page.rect.get_area()*.45:
            continue
        rows = table.extract()
        header = "".join(compact(v) for v in rows[0])
        if len(header) >= 3 and any(char.isalpha() for char in header):
            candidates.append({"kind": "other_table", "title": header[:80], "bbox_pt": rect,
                               "detection": "grid with textual header; table kind needs review"})
    return candidates


def extract_table_arrays(page, page_number, seeds, site, table_options=None):
    result = []
    for index, seed in enumerate(seeds):
        box = fitz.Rect(seed["bbox_pt"])
        # A detected intersection can be almost a point away from its border.
        # A half-point clip cut off the title block's left border on the real PDF.
        margin = fitz.Rect(box.x0-2, box.y0-2, box.x1+2, box.y1+2) & page.rect
        finder = page.find_tables(clip=margin, **(table_options or {}))
        choices = [t for t in finder.tables if fitz.Rect(t.bbox).get_area() >= box.get_area()*.5]
        if not choices:
            raise ValueError(f"第 {page_number} 页表格区域无法重新提取单元格：{seed['title']}")
        table = max(choices, key=lambda t: fitz.Rect(t.bbox).get_area())
        box = fitz.Rect(table.bbox)
        rows = table.extract()
        cells = [{"row": ri, "column": ci, "text": value, "bbox_pt": list(table.rows[ri].cells[ci]) if table.rows[ri].cells[ci] else None}
                 for ri, row in enumerate(rows) for ci, value in enumerate(row)]
        tid = f"p{page_number}_table_{index+1:02d}"
        image = f"tables/{tid}.png"
        (site / "tables").mkdir(parents=True, exist_ok=True)
        scale = min(4, 2600/max(box.width, box.height))
        page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=margin, alpha=False).save(site / image)
        words = [{"text": word[4], "bbox_pt": list(word[:4])} for word in page.get_text("words") if contained(word[:4], box, tolerance=1)]
        captured = compact("".join(value or "" for row in rows for value in row))
        missing = [word for word in words if compact(word["text"]) not in captured]
        result.append({**seed, "bbox_pt": list(box), "id": tid, "page": page_number, "rows": rows, "cells": cells,
                       "row_count": len(rows), "column_count": table.col_count, "image": image,
                       "native_words": words, "words_not_fully_represented_in_cells": missing,
                       "merged_cell_convention": "null means continuation of a merged cell; empty string means an empty cell"})
    return result


def reconcile_bom_tables(tables, bom):
    observed, issues = {}, []
    for table in tables:
        if table["kind"] != "bom":
            continue
        columns = None
        for row in table["rows"]:
            labels = [compact(value) for value in row]
            if any(value in {"编号", "件号"} for value in labels) and any("规格" in value for value in labels):
                columns = {"mark": next(i for i,v in enumerate(labels) if v in {"编号", "件号"}),
                           "spec": next(i for i,v in enumerate(labels) if "规格" in v),
                           "length": next(i for i,v in enumerate(labels) if "长度" in v),
                           "quantity": next(i for i,v in enumerate(labels) if v == "数量")}
                continue
            if columns is None:
                continue
            try:
                records = split_cell_rows(*[row[columns[k]] for k in ("mark", "spec", "length", "quantity")])
                for mark, spec, length, quantity in records:
                    value = (spec, float(length), int(quantity))
                    if mark in observed and observed[mark] != value:
                        issues.append(f"分表后件号 {mark} 有矛盾记录")
                    observed[mark] = value
            except (ValueError, TypeError) as exc:
                issues.append(f"{table['id']} 分表单元格无法核对：{exc}")
    expected = {r["mark"]: (compact(r["specification"]), r["length_mm"], r["quantity"]) for r in bom}
    for mark in sorted(set(observed) | set(expected)):
        if observed.get(mark) != expected.get(mark):
            issues.append(f"件号 {mark} 的独立分表数组与 BOM 数组不一致")
    return issues


def masked_parts(page, tables, site, page_number):
    """Render the two complementary masks from the same original PDF bitmap."""
    from PIL import Image, ImageDraw
    scale = 3200/max(page.rect.width, page.rect.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    original = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    structure = original.copy()
    table_image = Image.new("RGB", original.size, "white")
    draw = ImageDraw.Draw(structure)
    for table in tables:
        x0, y0, x1, y1 = table["bbox_pt"]
        rect = (max(0, int((x0-.8)*scale)), max(0, int((y0-.8)*scale)), min(pix.width, int((x1+.8)*scale)+1), min(pix.height, int((y1+.8)*scale)+1))
        table_image.paste(original.crop(rect), rect[:2])
        draw.rectangle((rect[0], rect[1], rect[2]-1, rect[3]-1), fill="white")
    folder = site / "pages"
    folder.mkdir(parents=True, exist_ok=True)
    for kind, bitmap in (("original", original), ("tables", table_image), ("structure", structure)):
        bitmap.save(folder / f"p{page_number:03d}-{kind}.png")
    return {kind: f"pages/p{page_number:03d}-{kind}.png" for kind in ("original", "tables", "structure")}


def decompose(pdf, run_dir, table_regions=None, *, ocr_report=None):
    run_dir = Path(run_dir)
    site = run_dir / "site"
    from .ocr_input import analysis_source, annotate_primitives, ocr_table_options
    recovered = read_json(ocr_report) if ocr_report else None
    if recovered and recovered['source_sha256'] != file_digest(pdf):
        raise ValueError('OCR 文字与原图不匹配。')
    if recovered and recovered['analysis_source_sha256'] != file_digest(recovered['analysis_source']):
        raise ValueError('OCR 分析副本已改变。')
    manifest = prepare(pdf, run_dir, analysis_pdf=recovered['analysis_source'] if recovered else None, ocr_report=ocr_report)
    content_source = analysis_source(manifest)
    print("[tables] reading native BOM and locating table grids", flush=True)
    observations = []
    native_rows, native_regions, native_issues = read_native_bom(content_source, row_observations=observations, ocr_report=recovered)
    if recovered:
        with fitz.open(content_source) as content:
            for row in observations:
                pn = row['evidence']['page']; w,h = content[pn-1].rect.width,content[pn-1].rect.height
                b = row['evidence']['bbox']; box = [b[0]*w,b[1]*h,b[2]*w,b[3]*h]
                candidates = [o for o in recovered['words'] if o['page'] == pn and contained(o['bbox_pt'],box,tolerance=2)]
                if candidates:
                    row.update(text_source='ocr', needs_review=True, ocr_word_ids=[o['id'] for o in candidates],
                               ocr_min_confidence=min(o['confidence'] for o in candidates))
    bom_check = check_bom_observations(observations, native_issues)
    bom_check['source_sha256'] = manifest['source_sha256']
    write_json(site / 'tables/bom-observations.json', observations)
    write_json(site / 'tables/bom-check.json', bom_check)
    if bom_check['status'] == 'blocked':
        raise BomIntegrityError('BOM 原始行需要核对：' + '；'.join(i['message'] for i in bom_check['issues'][:4])
                                + ' 详见 site/tables/bom-check.json，可解析行和原始 PDF 已保留。')
    bom = [{**row.model_dump(mode="json"), "classification": classify(row.specification)} for row in native_rows]
    overrides = read_json(table_regions) if table_regions else None
    all_tables, all_primitives, pages, issues, native_path_count = [], [], [], list(native_issues), 0
    with fitz.open(content_source) as document:
        for page_number, page in enumerate(document, 1):
            if page.rotation:
                raise ValueError("分解前须将 PDF 页面旋转元数据固化，避免坐标错位。")
            page_rows = [row for row in native_rows if row.evidence.page == page_number]
            page_regions = [region for region in native_regions if region.evidence.page == page_number]
            table_options = ocr_table_options(page, recovered, page_number) if recovered else {}
            seeds = [s for s in overrides if s["page"] == page_number] if overrides is not None else table_seeds(page, page_rows, page_regions, table_options)
            for seed in seeds:
                box = seed["bbox_pt"]
                if not (0 <= box[0] < box[2] <= page.rect.width and 0 <= box[1] < box[3] <= page.rect.height):
                    raise ValueError("表格覆盖区域必须是原页 PDF 点坐标中的有效矩形。")
            tables = extract_table_arrays(page, page_number, seeds, site, table_options)
            print(f"[partition] page {page_number}: {len(tables)} tables", flush=True)
            graphics, path_count = extract_primitives(page, page_number, tables)
            if recovered:
                annotate_primitives(graphics, recovered, page_number)
            native_path_count += path_count
            if not tables:
                issues.append(f"第 {page_number} 页未找到可确认的表格区域；请检查或提供 --table-regions。")
            if any(p["primitive_type"] == "raster_image" and (p["bbox_pt"][2]-p["bbox_pt"][0])*(p["bbox_pt"][3]-p["bbox_pt"][1]) > page.rect.get_area()*.5 for p in graphics):
                issues.append(f"第 {page_number} 页包含大幅栅格图；当前原生图元提取不覆盖栅格内部线条，需要 OCR/矢量化。")
            images = masked_parts(page, tables, site, page_number)
            pages.append({"page": page_number, "size_pt": [page.rect.width, page.rect.height], **images})
            all_tables.extend(tables)
            all_primitives.extend(graphics)
    for row in bom:
        x0, y0, x1, y1 = row["evidence"]["bbox"]
        w, h = pages[row["evidence"]["page"]-1]["size_pt"]
        matches = [table for table in all_tables if table["page"] == row["evidence"]["page"] and contained([x0*w, y0*h, x1*w, y1*h], table["bbox_pt"])]
        row["table_id"] = matches[0]["id"] if matches else None
        if not matches:
            issues.append(f"BOM 件号 {row['mark']} 未落入已分出的表格区域。")
    bom_table_issues = reconcile_bom_tables(all_tables, bom)
    issues.extend(bom_table_issues)
    for table in all_tables:
        if table["words_not_fully_represented_in_cells"]:
            issues.append(f"{table['id']} 有 {len(table['words_not_fully_represented_in_cells'])} 处原生文字未完整进入单元格；已保留在 native_words 供核对。")
    print("[primitives] classifying native geometry and enclosed callouts", flush=True)
    callouts = associate_callouts(all_primitives, bom)
    dash_groups = detect_dash_candidates(all_primitives)
    structure = [p for p in all_primitives if p["part"] == "structure"]
    table_primitives = [p for p in all_primitives if p["part"] == "table"]
    ids = [p["id"] for p in all_primitives]
    represented_paths = {(p["page"], p["source_path_index"]) for p in all_primitives if "source_path_index" in p}
    checks = {"unique_primitive_ids": len(ids) == len(set(ids)), "all_native_paths_preserved": len(represented_paths) == native_path_count,
              "partition_is_complete_and_disjoint": len(structure)+len(table_primitives) == len(all_primitives)
                  and not ({p["id"] for p in structure} & {p["id"] for p in table_primitives}),
              "all_bom_rows_in_tables": all(r["table_id"] for r in bom), "bom_values_match_split_tables": not bom_table_issues,
              "source_hash_unchanged": file_digest(pdf) == manifest["source_sha256"]}
    if not all(checks.values()):
        raise ValueError(f"分解完整性检查失败：{checks}")
    matched_marks = {m["mark"] for c in callouts for m in c["marks"] if m["bom_found"]}
    unmatched_marks = sorted({m["mark"] for c in callouts for m in c["marks"] if not m["bom_found"]})
    unmatched_bom_marks = sorted({r["mark"] for r in bom}-matched_marks)
    summary = {"schema_version": "1.0", "stage": "tables_and_primitives", "title": Path(pdf).stem,
               "source_sha256": manifest["source_sha256"], "coordinate_system": "unrotated original page; PDF points; x right, y down; not engineering mm",
               "pages": pages, "table_count": len(all_tables), "bom_rows": len(bom),
               "angle_marks": sum(r["classification"]["kind"] == "angle" for r in bom),
               "angle_quantity": sum(r["quantity"] for r in bom if r["classification"]["kind"] == "angle"),
               "native_path_count": native_path_count, "structure_primitive_count": len(structure), "table_primitive_count": len(table_primitives),
               "primitive_types": dict(Counter(p["primitive_type"] for p in structure)), "roles": dict(Counter(p["role"] for p in structure)),
               "callout_groups": len(callouts), "matched_bom_marks": sorted(matched_marks), "dash_candidate_groups": len(dash_groups),
               "circle_marks_not_in_bom": unmatched_marks, "bom_marks_not_found_in_circles": unmatched_bom_marks,
               "checks": checks, "issues": issues,
               "limitations": ["图元是 PDF 绘图对象，不等于一根角钢；原始路径和重复绘制均保留。",
                               "圆圈内件号按 BOM 分类，仅对应文字与圆圈，尚未连接到杆件轮廓。",
                               "数字文字与重复短线只做候选分类；未确定尺寸界线、实体连接或虚实语义。",
                               "纯扫描图需要另行 OCR/矢量化，不能把图片内部线条称为已提取原生图元。"]}
    if recovered:
        summary['ocr'] = {'status': 'needs_review', 'counts': recovered['counts'], 'evidence_url': 'ocr/'}
        summary['issues'].append('BOM、件号和尺寸含 OCR 候选文字；识别置信度不等于准确率，需查看 OCR 原图对照。')
        for row in bom:
            if any(o['page'] == row['evidence']['page'] for o in recovered['words']):
                row['text_source'] = 'ocr'
                row['needs_review'] = True
    write_json(site / "tables/tables.json", all_tables)
    write_json(site / "tables/bom.json", bom)
    compact_json(site / "tables/primitives.json", table_primitives)
    compact_json(site / "structure/primitives.json", structure)
    write_json(site / "structure/callouts.json", callouts)
    write_json(site / "structure/dash-candidates.json", dash_groups)
    write_json(site / "structure/classification.json", {"by_type": {kind: [p["id"] for p in structure if p["primitive_type"] == kind] for kind in summary["primitive_types"]},
                                                        "by_role": {role: [p["id"] for p in structure if p["role"] == role] for role in summary["roles"]}})
    write_json(site / "summary.json", summary)
    write_json(run_dir / "partition.json", {"source_sha256": manifest["source_sha256"], "coordinate_system": summary["coordinate_system"],
                                           "tables": [{k: table[k] for k in ("id", "kind", "title", "page", "bbox_pt")} for table in all_tables], "checks": checks})
    for asset in (Path(__file__).parent / "decomposition_web").iterdir():
        if asset.is_file():
            shutil.copy2(asset, site / asset.name)
    (run_dir / "failure.json").unlink(missing_ok=True)
    print(f"[decomposed] {len(all_tables)} tables; {len(bom)} BOM rows; {len(structure)} structure primitives; {len(callouts)} callout groups", flush=True)
    return summary
