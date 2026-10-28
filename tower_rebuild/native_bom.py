"""Read BOM cells directly when a vector PDF exposes text and table borders."""
import re
from collections import defaultdict

import fitz

from .schema import BomRow, Evidence, Region

PART_MARK = r'(?:\d+[A-Za-z]{0,4}|[A-Za-z]{1,4}[-_]?\d+[A-Za-z]{0,4})(?:[-_]\d+)?'


def compact(value):
    return re.sub(r"\s+", "", value or "")


def split_cell_rows(mark, spec, length, quantity):
    """A missing border may merge several rows. Never concatenate their numbers."""
    marks = [compact(s) for s in (mark or "").splitlines() if compact(s)]
    if not marks or not all(re.fullmatch(PART_MARK, m) for m in marks):
        # An unsupported or missing mark in an otherwise numeric material row
        # is a parse problem, not a header to silently discard.
        material = compact(spec)
        if (re.match(r'(?:Q\d+[A-Za-z]*)?(?:L\d|-\d)', material)
                and re.fullmatch(r'\d+(?:\.\d+)?', compact(length))
                and re.fullmatch(r'[1-9]\d*', compact(quantity))):
            raise ValueError(f'材料行件号缺失或格式尚不支持：{compact(mark)!r}')
        return []
    values = [spec or "", length or "", quantity or ""]
    if len(marks) == 1:
        return [(marks[0], *[compact(v) for v in values])]
    columns = [[compact(s) for s in value.splitlines() if compact(s)] for value in values]
    if any(len(column) != len(marks) for column in columns):
        raise ValueError(f"合并单元格的件号与其他列行数不一致：{marks}")
    return [(m, *[column[i] for column in columns]) for i, m in enumerate(marks)]


class BomIntegrityError(ValueError):
    """A BOM cannot be safely reduced to the unique marks used by topology."""


def check_bom_observations(observations, diagnostics=()):
    """Keep every physical row occurrence; identical extraction hits are one row."""
    groups = defaultdict(list)
    seen = set()
    for row in observations:
        key = (row['mark'], row['evidence']['page'], tuple(row['evidence']['bbox']),
               row['specification'], row['length_mm'], row['quantity'])
        if key not in seen:
            groups[row['mark']].append(row)
            seen.add(key)
    issues = []
    for mark, rows in groups.items():
        if len(rows) < 2:
            continue
        values = {(r['specification'], r['length_mm'], r['quantity']) for r in rows}
        conflicting = len(values) > 1
        issues.append({'code': 'conflicting_bom_rows' if conflicting else 'duplicate_bom_scope_unresolved',
                       'mark': mark, 'pages': sorted({r['evidence']['page'] for r in rows}),
                       'message': f'件号 {mark} 的重复行存在规格、长度或数量冲突。' if conflicting else
                                  f'件号 {mark} 在不同位置重复；尚未确认是同一 BOM 重复印刷还是独立零件清单，不能合并数量。',
                       'rows': rows})
    for message in dict.fromkeys(diagnostics):
        issues.append({'code': 'native_table_parse_issue', 'message': message, 'rows': []})
    return {'schema_version': '1.0', 'status': 'blocked' if issues else 'passed',
            'row_observations': sum(len(rows) for rows in groups.values()), 'unique_marks': len(groups),
            'issues': issues, 'scope': 'Unique BOM marks required for the current document topology',
            'engineering_accuracy_verified': False}


def native_header_lefts(page):
    """Locate full BOM header groups even when sheet frames swallow table cells.

    All four field labels must exist on the same native header band. Coordinates
    come from text, not a fixed right-side crop or any sample drawing's marks.
    """
    words = [(compact(w[4]), tuple(w[:4])) for w in page.get_text('words')]

    def phrases(label):
        found = [box for text, box in words if text == label]
        for text, a in words:
            if text != label[0]:
                continue
            for other, b in words:
                if other != label[1]:
                    continue
                height = max(a[3] - a[1], b[3] - b[1])
                ax, ay = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
                bx, by = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                horizontal = 0 < bx - ax < height * 5 and abs(by - ay) < height * .6
                vertical = 0 < by - ay < height * 3 and abs(bx - ax) < height * .6
                if horizontal or vertical:
                    found.append((min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])))
        return found

    marks = phrases('编号') + phrases('件号')
    fields = [phrases(label) for label in ('规格', '长度', '数量')]
    lefts = []
    for mark in marks:
        height = mark[3] - mark[1]; middle = (mark[1] + mark[3]) / 2
        previous_x = mark[0]; complete = True
        for candidates in fields:
            nearby = [b for b in candidates if previous_x < b[0] < mark[0] + height * 24
                      and abs((b[1] + b[3]) / 2 - middle) < height * 1.3]
            if not nearby:
                complete = False; break
            previous_x = min(b[0] for b in nearby)
        if complete:
            lefts.append(max(0, mark[0] - max(18, height * 1.2)))
    return lefts


def read_native_bom(pdf, *, row_observations=None, ocr_report=None):
    rows, regions, diagnostics = {}, [], []
    try:
        doc = fitz.open(pdf)
    except (fitz.FileDataError, RuntimeError):
        return [], [], []
    with doc:
        for page_num, page in enumerate(doc, 1):
            if page.rotation or not page.get_text().strip():
                continue
            # Discover candidate table areas from native header words. Expand to
            # the whole page: adjoining BOM columns may be one merged grid.
            from .ocr_input import ocr_table_options
            table_options = ocr_table_options(page, ocr_report, page_num) if ocr_report else {}
            tables = page.find_tables(**table_options)
            header_lefts = []
            for candidate in tables.tables:
                for row_number, row in enumerate(candidate.extract()):
                    labels = [compact(s) for s in row]
                    if not any("规格" in label for label in labels) or not any("长度" in label for label in labels):
                        continue
                    for col, label in enumerate(labels):
                        if label in {"编号", "件号"} and candidate.rows[row_number].cells[col]:
                            header_lefts.append(candidate.rows[row_number].cells[col][0])
            if not header_lefts:
                header_lefts = native_header_lefts(page)
            if header_lefts:
                # Sheet borders/drawing strokes can merge with a table and hide
                # its final row. Re-detect from its observed header's left edge.
                tables = page.find_tables(clip=fitz.Rect(max(0, min(header_lefts)-6), 0, page.rect.width, page.rect.height), **table_options)
            for ti, table in enumerate(tables.tables):
                extracted = table.extract()
                for ri, header in enumerate(extracted):
                    headers = [compact(s) for s in header]
                    mark_columns = [i for i, value in enumerate(headers) if value in {"编号", "件号"}]
                    for column in mark_columns:
                        end = next((i for i in mark_columns if i > column), len(header))
                        fields = {}
                        for index in range(column+1, end):
                            label = headers[index]
                            if "规格" in label:
                                fields["spec"] = index
                            elif "长度" in label:
                                fields["length"] = index
                            elif label == "数量":
                                fields["quantity"] = index
                        if set(fields) != {"spec", "length", "quantity"}:
                            continue
                        collected, boxes, errors = [], [], []
                        for row_index in range(ri+1, len(extracted)):
                            row = extracted[row_index]
                            try:
                                records = split_cell_rows(row[column], row[fields["spec"]], row[fields["length"]], row[fields["quantity"]])
                            except ValueError as exc:
                                errors.append(str(exc))
                                continue
                            if not records:
                                continue
                            cells = table.rows[row_index].cells
                            selected = [cells[i] for i in [column, *fields.values()] if cells[i]]
                            box = fitz.Rect(selected[0])
                            for cell in selected[1:]:
                                box |= fitz.Rect(cell)
                            w, h = page.rect.width, page.rect.height
                            for mark, spec, length, quantity in records:
                                if not spec or not re.fullmatch(r"\d+(?:\.\d+)?", length) or not re.fullmatch(r"[1-9]\d*", quantity):
                                    errors.append(f"原生表格件号 {mark} 的规格/长度/数量单元格不完整")
                                    continue
                                evidence = Evidence(page=page_num, bbox=(box.x0/w, box.y0/h, box.x1/w, box.y1/h),
                                                    text=f"PDF table cells: {mark} | {spec} | {length} mm | quantity {quantity}")
                                item = BomRow(mark=mark, specification=spec, length_mm=float(length), quantity=int(quantity), evidence=evidence)
                                collected.append(item)
                                if row_observations is not None:
                                    row_observations.append(item.model_dump(mode='json'))
                            boxes.append(box)
                        if errors:
                            diagnostics.extend(errors)
                            continue  # Do not silently accept an incomplete table.
                        if collected:
                            union = fitz.Rect(boxes[0])
                            for box in boxes[1:]:
                                union |= box
                            # Include header and all cells, not just the LLM's guessed crop.
                            header_cells = [table.rows[ri].cells[i] for i in [column, *fields.values()] if table.rows[ri].cells[i]]
                            for cell in header_cells:
                                union |= fitz.Rect(cell)
                            regions.append(Region(id=f"p{page_num}_native_bom_{ti}_{column}", kind="bom", title="PDF 原生构件表",
                                evidence=Evidence(page=page_num, bbox=(union.x0/w, union.y0/h, union.x1/w, union.y1/h), text="编号 / 规格 / 长度 / 数量")))
                            for item in collected:
                                old = rows.get(item.mark)
                                if old and (old.specification, old.length_mm, old.quantity) != (item.specification, item.length_mm, item.quantity):
                                    diagnostics.append(f"原生表格件号 {item.mark} 重复且数据矛盾")
                                elif not old:
                                    rows[item.mark] = item
    return list(rows.values()), regions, diagnostics
