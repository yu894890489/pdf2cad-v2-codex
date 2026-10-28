from pathlib import Path

from examples.make_box_example import box_example
from tower_rebuild.io import file_digest, read_json, write_json
from tower_rebuild.pipeline import REVIEW_CHECKS, build
from tower_rebuild.schema import Settings


def test_candidate_report_contains_all_instances_and_excludes_plates(tmp_path):
    drawing, _ = box_example()
    report = build(drawing, tmp_path, Settings())
    model = read_json(tmp_path / "site/model.json")
    assert len(model["members"]) == 13
    assert all(m["mark"] != "901" for m in model["members"])
    assert report["solver"]["degrees_of_freedom"] == 0
    assert report["status"] != "reviewed_frame"
    assert (tmp_path / "site/index.html").exists()
    assert not (tmp_path / "site/reviewed_axes.dxf").exists()


def test_review_is_bound_to_source_data_and_result_and_stale_export_removed(tmp_path, monkeypatch):
    drawing, _ = box_example()
    # Stub only PDF rasterization; this test targets review state, not PDF parsing.
    source = tmp_path / "test-source.bin"
    source.write_bytes(b"synthetic review-state test")
    drawing.source_sha256 = file_digest(source)
    write_json(tmp_path / "manifest.json", {"source": str(source), "source_sha256": drawing.source_sha256,
                "pages": [{"page": 1, "size_pt": [1000, 1000]}]})
    def fake_crop(pdf, evidence, destination, max_side=2400):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"raster stub")
        return {"width": 100, "height": 100}
    monkeypatch.setattr("tower_rebuild.pipeline.crop", fake_crop)
    first = build(drawing, tmp_path, Settings())
    assert first["status"] == "needs_review"
    review = read_json(tmp_path / "review-template.json")
    review.update(accepted=True, reviewer="test reviewer", checks=sorted(REVIEW_CHECKS))
    write_json(tmp_path / "review.json", review)
    second = build(drawing, tmp_path, Settings(), tmp_path / "review.json")
    assert second["status"] == "reviewed_frame"
    assert (tmp_path / "site/reviewed_axes.dxf").exists()
    drawing.title += "changed"
    third = build(drawing, tmp_path, Settings(), tmp_path / "review.json")
    assert third["status"] != "reviewed_frame"
    assert "review_invalid" in {i["code"] for i in third["issues"]}
    assert not (tmp_path / "site/reviewed_axes.dxf").exists()


def test_invalid_graph_still_produces_actionable_report_page(tmp_path):
    drawing, _ = box_example()
    drawing.bom[0].quantity = 100
    report = build(drawing, tmp_path, Settings())
    assert report["status"] == "inconsistent"
    assert "quantity_mismatch" in {p["code"] for p in report["issues"]}
    assert read_json(tmp_path / "site/model.json")["members"] == []
    assert (tmp_path / "site/app.js").exists()
