"""End-to-end stage contract with controlled vision responses, not an AI score."""
import base64
from pathlib import Path

from examples.make_box_example import box_example
from tower_rebuild.extraction import extract
from tower_rebuild.extraction import ask_with_failure
from tower_rebuild.io import file_digest, read_json
from tower_rebuild.pipeline import build
from tower_rebuild.schema import Assembly, Audit, BomResult, Face, Layout, Settings


def test_invalid_model_record_stays_an_explicit_blocking_issue(tmp_path):
    class FailedClient:
        def ask(self, *args):
            raise ValueError("invalid structure after bounded retries")
    result = ask_with_failure(FailedClient(), tmp_path, "audit-face", Audit, "test",
                              fallback=lambda message: Audit(findings=[message]))
    assert "未通过格式校验" in result.findings[0]
    assert (tmp_path / "audit-face-failed.json").exists()


def test_bom_first_face_assembly_and_review_gate(tmp_path, monkeypatch):
    truth, expected = box_example()
    source = tmp_path / "synthetic-source.bin"
    source.write_bytes(b"controlled extraction-stage contract fixture")
    overview = tmp_path / "overview.png"
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
    overview.write_bytes(png)
    manifest = {"source": str(source), "source_sha256": file_digest(source),
                "pages": [{"page": 1, "size_pt": [1000, 1000], "rotation": 0, "overview": "overview.png"}]}
    from tower_rebuild.io import write_json
    write_json(tmp_path / "manifest.json", manifest)
    evidence = truth.faces[0].evidence.model_dump(mode="json")
    layout = {"regions": [{"id": "table", "kind": "bom", "title": "BOM", "evidence": evidence}], "issues": []}
    for face in truth.faces:
        layout["regions"].append({"id": face.id, "kind": "front" if face.id == "front" else "face",
                                  "title": face.title, "evidence": evidence})
    faces = {}
    for original in truth.faces:
        face = original.model_copy(deep=True)
        face.id = "p1_" + face.id
        face.coordinate_units = "page"
        face.origin = (.1, .8)
        for node in face.nodes:
            node.xy = (.1+node.xy[0]/5000, .8-node.xy[1]/5000)
        raw = face.model_dump(mode="json")
        raw["calibrations"] = [
            {"axis": "x", "start": [.1, .8], "end": [.9, .8], "length_mm": 4000, "evidence": evidence},
            {"axis": "y", "start": [.1, .8], "end": [.1, .1], "length_mm": 3500, "evidence": evidence}]
        faces[face.id] = raw
    assembly = truth.assembly.model_copy(deep=True)
    assembly.anchor_view = "p1_front"
    for joint in assembly.joints:
        for ref in joint.observations:
            ref.view = "p1_" + ref.view
    for member in assembly.members:
        for ref in member.occurrences:
            ref.view = "p1_" + ref.view
    for placement in assembly.placements:
        placement.view = "p1_" + placement.view
    stages = []
    class FakeVision:
        def __init__(self, *args):
            pass
        def probe(self):
            return ["test-only"]
        def close(self):
            pass
        def ask(self, stage, schema, prompt, images=()):
            stages.append(stage)
            if schema is Layout:
                return Layout.model_validate(layout)
            if schema is BomResult:
                return BomResult(rows=truth.bom)
            if schema is Face:
                return Face.model_validate(faces[stage.removeprefix("face-")])
            if schema is Assembly:
                return assembly
            if schema is Audit:
                return Audit(findings=[])
            raise AssertionError(schema)
    def fake_crop(pdf, evidence, destination, max_side=2400):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(png)
        return {"width": 800, "height": 600}
    monkeypatch.setattr("tower_rebuild.extraction.VlmClient", FakeVision)
    monkeypatch.setattr("tower_rebuild.extraction.crop", fake_crop)
    monkeypatch.setattr("tower_rebuild.pipeline.crop", fake_crop)
    drawing = extract(manifest, tmp_path, Settings(model="test-only"))
    assert max(i for i, s in enumerate(stages) if s.startswith("bom-")) < min(i for i, s in enumerate(stages) if s.startswith("face-"))
    result = build(drawing, tmp_path, Settings(model="test-only"))
    assert result["status"] == "needs_review"
    assert not any(i["severity"] == "error" for i in result["issues"])
    assert len(read_json(tmp_path / "site/model.json")["members"]) == 13
    assert len(list((tmp_path / "site/faces").glob("*.svg"))) == 6
