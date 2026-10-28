import copy

from examples.make_box_example import box_example
from tower_rebuild.evaluation import evaluate


def reference_and_candidate():
    drawing, nodes = box_example()
    lookup = {(f.id, e.id): e for f in drawing.faces for e in f.edges}
    members = []
    for member in drawing.assembly.members:
        first = member.occurrences[0]
        edge = lookup[(first.view, first.edge)]
        members.append({"mark": member.mark, "nodes": [edge.a, edge.b]})
    reference = {"annotator": "test independent fixture", "source_sha256": drawing.source_sha256,
                 "bom": [b.model_dump(mode="json") for b in drawing.bom], "members": members, "nodes": nodes, "tolerance_mm": 1.}
    return reference, copy.deepcopy(reference)


def test_reference_check_ignores_translation_and_rotation_but_not_mirror_or_scale():
    reference, model = reference_and_candidate()
    model["nodes"] = {k: [-p[1]+50, p[0]-800, p[2]+150] for k, p in model["nodes"].items()}
    assert evaluate(model, reference)["pass"]
    model["nodes"] = {k: [-p[0], p[1], p[2]] for k, p in model["nodes"].items()}
    assert not evaluate(model, reference)["pass"]
    reference, model = reference_and_candidate()
    model["nodes"] = {k: [v*1.1 for v in p] for k, p in model["nodes"].items()}
    assert not evaluate(model, reference)["pass"]


def test_missing_member_and_wrong_topology_reduce_accuracy():
    reference, model = reference_and_candidate()
    model["members"].pop()
    result = evaluate(model, reference)
    assert not result["pass"] and result["member_recall"] < 1
    model["members"][0]["nodes"] = ["A", "G"]
    assert evaluate(model, reference)["member_precision"] < 1
