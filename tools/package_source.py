"""Bundle source/config/examples only; no drawings, credentials or caches."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
destination = root / "output/tower-rebuild-source.zip"
destination.parent.mkdir(parents=True, exist_ok=True)
files = [root / name for name in ("README.md", "PIPELINE_GUIDE.md", "pyproject.toml", "requirements-tested.txt", "run-drawing.ps1", "run-decompose.ps1", "run-topology.ps1", "run-audit.ps1", "run-preview.ps1", "run-model-audit.ps1", "run-pipeline.ps1", "start-local.ps1", ".gitignore")]
files.extend(root / "output" / name for name in ("drawing.schema.json", "REUSABLE_PIPELINE_VALIDATION.md", "DECOMPOSITION_VALIDATION.md", "TOPOLOGY_VALIDATION.md", "OVERLAP_VALIDATION.md", "DIMENSION_AUDIT_VALIDATION.md", "MODEL_PREVIEW_VALIDATION.md", "MODEL_DIMENSION_AUDIT_VALIDATION.md", "UPLOAD_WORKFLOW_VALIDATION.md", "COMPLETE_WORKFLOW_VALIDATION.md", "OCR_VALIDATION.md"))
for directory in ("tower_rebuild", "examples", "tests", "tools"):
    files.extend(p for p in (root / directory).rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts and (p.suffix in {".py", ".json", ".html", ".css", ".js"} or p.name=='LICENSE.txt'))
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, path.relative_to(root))
with ZipFile(destination) as archive:
    assert archive.testzip() is None
    print(f"{destination}: {len(archive.namelist())} files, {destination.stat().st_size} bytes")
