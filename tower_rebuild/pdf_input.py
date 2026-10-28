"""Local PDF preparation, preserving source coordinates and hashes."""
from pathlib import Path

import fitz

from .io import file_digest, write_json


def prepare(pdf_path, run_dir, max_side=2400, *, analysis_pdf=None, ocr_report=None):
    pdf_path, run_dir = Path(pdf_path).resolve(), Path(run_dir).resolve()
    source_hash = file_digest(pdf_path)
    manifest_file = run_dir / "manifest.json"
    if manifest_file.exists():
        from .io import read_json
        old = read_json(manifest_file)
        if old["source_sha256"] != source_hash:
            raise ValueError("输出目录属于另一份 PDF，请使用新的 --out 目录。")
    pages = []
    with fitz.open(analysis_pdf or pdf_path) as doc:
        if doc.needs_pass:
            raise ValueError("PDF 已加密，需要先提供可读取的文件。")
        for number, page in enumerate(doc, 1):
            folder = run_dir / "pages" / f"{number:03d}"
            folder.mkdir(parents=True, exist_ok=True)
            factor = max_side / max(page.rect.width, page.rect.height)
            page.get_pixmap(matrix=fitz.Matrix(factor, factor), alpha=False).save(folder / "overview.png")
            # Raster blocks contain bytes; keep text geometry here and the full
            # scanned content in overview/crops, which the vision model receives.
            write_json(folder / "text.json", page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES))
            # Path data is evidence for inspection, not an automatic rod detector.
            vectors = []
            for index, path in enumerate(page.get_drawings()):
                vectors.append({"id": index, "bbox_pt": list(path["rect"]), "dashes": path.get("dashes"),
                                "width_pt": path.get("width"), "type": path.get("type"),
                                "items": [[item[0], *[list(v) if hasattr(v, "__iter__") else v for v in item[1:]]]
                                          for item in path["items"]]})
            # fitz.Quad is iterable over Points; default conversion recursively below.
            def plain(value):
                if isinstance(value, dict):
                    return {k: plain(v) for k, v in value.items()}
                if isinstance(value, (list, tuple)) or isinstance(value, (fitz.Point, fitz.Rect, fitz.Quad)):
                    return [plain(v) for v in value]
                return value
            write_json(folder / "vectors.json", plain(vectors))
            pages.append({"page": number, "size_pt": [page.rect.width, page.rect.height],
                          "overview": str((folder / "overview.png").relative_to(run_dir)).replace("\\", "/"),
                          "rotation": page.rotation, "vector_count": len(vectors)})
    manifest = {"schema_version": "1.0", "source": str(pdf_path), "source_sha256": source_hash,
                "coordinate_system": "normalized original rendered page, x right, y down", "pages": pages}
    if analysis_pdf:
        manifest.update(analysis_source=str(Path(analysis_pdf).resolve()), analysis_source_sha256=file_digest(analysis_pdf),
                        ocr_report=str(Path(ocr_report).resolve()), ocr_report_sha256=file_digest(ocr_report))
    write_json(manifest_file, manifest)
    return manifest


def crop(pdf_path, evidence, destination, max_side=2400):
    """Render a crop directly from PDF; don't enlarge an overview bitmap."""
    with fitz.open(pdf_path) as doc:
        page = doc[evidence.page - 1]
        if page.rotation:
            raise ValueError("暂不支持带页面旋转元数据的图纸，请先将页面旋转固化后重新导入。")
        w, h = page.rect.width, page.rect.height
        x0, y0, x1, y1 = evidence.bbox
        box = fitz.Rect(x0*w, y0*h, x1*w, y1*h)
        scale = min(5, max_side / max(box.width, box.height))
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=box, alpha=False)
        pix.save(path)
        return {"bbox": list(evidence.bbox), "page": evidence.page, "width": pix.width, "height": pix.height}
