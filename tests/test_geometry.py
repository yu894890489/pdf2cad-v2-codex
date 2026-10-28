import copy

import numpy as np
import pytest

from examples.make_box_example import box_example
from tower_rebuild.materials import classify
from tower_rebuild.schema import Drawing, Relation, Settings
from tower_rebuild.solver import solve
from tower_rebuild.validation import DrawingError, compile_drawing


@pytest.mark.parametrize("spec,kind", [
    ("Q345L125×10", "angle"), ("Q345 L 63*5", "angle"), ("L100×63×6", "angle"),
    ("Q345-12×643", "plate"), ("Q345−12×570", "plate"), ("Q345", "unknown"),
    ("M20螺母", "hardware"), ("L5×63", "unknown")])
def test_classification(spec, kind):
    assert classify(spec)["kind"] == kind


def test_ungraded_plate_stock_and_bolt_are_excluded():
    assert classify("-6×128")["kind"] == "plate"
    assert classify("M20X45")["kind"] == "hardware"


@pytest.mark.parametrize("dimensions", [(2400, 1200, 1500), (3570, 880, 2110)])
def test_recovers_known_3d_box_and_distinct_repeated_marks(dimensions):
    drawing, expected = box_example(*dimensions)
    compiled = compile_drawing(drawing)
    result = solve(drawing, compiled, Settings())
    assert not [p for p in result["issues"] if p["severity"] == "error"]
    assert result["degrees_of_freedom"] == 0
    assert len(compiled["members"]) == 13
    assert sum(m.mark == "101" for m in compiled["members"].values()) == 4
    for key, point in expected.items():
        assert np.allclose(result["nodes"][key], point, atol=.001)
    assert max(c["error"] for c in result["checks"]) < .001


@pytest.mark.parametrize("mutation,code", [
    (lambda d: setattr(d.assembly.members[0], "mark", "901"), "not_angle"),
    (lambda d: setattr(d.faces[0].edges[0], "line_style", "dashed"), "dashed_member"),
    (lambda d: setattr(d.bom[0], "quantity", 9), "quantity_mismatch"),
    (lambda d: d.assembly.members.append(d.assembly.members[0].model_copy(deep=True)), "duplicate_id"),
    (lambda d: setattr(d.assembly.members[0], "offset_evidence", None), "offset_evidence"),
    (lambda d: setattr(d.assembly.joints[0].observations[0], "node", "not_here"), "unknown_node")])
def test_semantic_errors_are_blocking(mutation, code):
    drawing, _ = box_example()
    mutation(drawing)
    with pytest.raises(DrawingError) as failure:
        compile_drawing(drawing)
    assert code in {p["code"] for p in failure.value.issues}


def test_contradictory_cut_length_is_not_satisfied_by_stretching():
    drawing, _ = box_example()
    drawing.bom[0].length_mm += 160
    result = solve(drawing, compile_drawing(drawing), Settings())
    assert "constraint_conflict" in {p["code"] for p in result["issues"]}
    assert any(not check["pass"] for check in result["checks"])


def test_missing_end_offsets_do_not_enforce_bom_length_as_axis_span():
    drawing, expected = box_example()
    drawing.bom[0].length_mm += 160
    for m in drawing.assembly.members:
        m.end_offsets_mm = m.offset_evidence = None
    result = solve(drawing, compile_drawing(drawing), Settings())
    assert not any(p["severity"] == "error" for p in result["issues"])
    assert "cut_geometry_unknown" in {p["code"] for p in result["issues"]}
    assert np.allclose(result["nodes"]["B"], expected["B"])
    assert not any(c["kind"] == "cut_length" for c in result["checks"])


def two_faces():
    drawing, _ = box_example()
    drawing.faces = [f for f in drawing.faces if f.id in {"front", "top"}]
    drawing.assembly.placements = [p for p in drawing.assembly.placements if p.view in {"front", "top"}]
    for p in drawing.assembly.placements:
        if p.view == "top":
            p.fixed_rotation = False
            p.orientation_evidence = None
    for m in drawing.assembly.members:
        m.occurrences = [o for o in m.occurrences if o.view in {"front", "top"}]
    drawing.assembly.members = [m for m in drawing.assembly.members if m.occurrences]
    for j in drawing.assembly.joints:
        j.observations = [o for o in j.observations if o.view in {"front", "top"}]
    drawing.assembly.joints = [j for j in drawing.assembly.joints if j.observations]
    for b in drawing.bom:
        if classify(b.specification)["kind"] == "angle":
            b.quantity = sum(m.mark == b.mark for m in drawing.assembly.members)
    return drawing


def test_two_faces_with_only_shared_edge_leave_hinge_degree_of_freedom():
    drawing = two_faces()
    result = solve(drawing, compile_drawing(drawing), Settings(solver_starts=3))
    assert result["degrees_of_freedom"] >= 1
    assert "underdetermined" in {p["code"] for p in result["issues"]}


def test_perpendicular_hinge_can_still_have_two_fold_directions():
    drawing = two_faces()
    drawing.assembly.relations = [Relation(first="front", second="top", kind="perpendicular", evidence=drawing.faces[0].evidence)]
    result = solve(drawing, compile_drawing(drawing), Settings())
    assert result["degrees_of_freedom"] == 0
    assert result["alternative_solutions"] >= 1
    assert "ambiguous_assembly" in {p["code"] for p in result["issues"]}


def test_scale_missing_does_not_default_to_a_guessed_depth():
    drawing, _ = box_example()
    drawing.faces[0].coordinate_units = "page"
    with pytest.raises(DrawingError) as failure:
        compile_drawing(drawing)
    assert "missing_scale" in {p["code"] for p in failure.value.issues}


def test_same_mark_with_two_instances_in_one_view_cannot_be_merged():
    drawing, _ = box_example()
    first, second = drawing.assembly.members[:2]
    first.occurrences.extend(second.occurrences)
    drawing.assembly.members.remove(second)
    with pytest.raises(DrawingError) as failure:
        compile_drawing(drawing)
    codes = {p["code"] for p in failure.value.issues}
    assert "instance_collapse" in codes and "shared_member_conflict" in codes


def test_unknown_fields_and_nan_rejected():
    drawing, _ = box_example()
    raw = drawing.model_dump(mode="json")
    raw["approved_by_model"] = True
    with pytest.raises(ValueError):
        Drawing.model_validate(raw)
    raw.pop("approved_by_model")
    raw["bom"][0]["length_mm"] = float("nan")
    with pytest.raises(ValueError):
        Drawing.model_validate(raw)
