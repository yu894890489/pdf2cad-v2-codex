import math

import fitz

from tower_rebuild.decomposition import reconcile_bom_tables
from tower_rebuild.primitives import associate_callouts, detect_dash_candidates, extract_primitives, geometry, is_dashed, table_assignment


def circle_commands(x=100, y=100, r=10):
    k = .5522847498307936*r
    return [["c", [x+r,y], [x+r,y+k], [x+k,y+r], [x,y+r]],
            ["c", [x,y+r], [x-k,y+r], [x-r,y+k], [x-r,y]],
            ["c", [x-r,y], [x-r,y-k], [x-k,y-r], [x,y-r]],
            ["c", [x,y-r], [x+k,y-r], [x+r,y-k], [x+r,y]]]


def test_circle_arc_and_non_circular_curve_keep_distinct_geometry():
    kind, g = geometry(circle_commands())
    assert kind == "circle" and abs(g["radius"]-10) < .003
    assert math.dist(g["center"], [100,100]) < .003
    assert geometry(circle_commands()[:1])[0] == "circular_arc"
    assert geometry([["c", [0,0], [3,12], [5,-9], [8,0]]])[0] == "bezier_curve"
    assert geometry([["l", [0,0], [5,0]], ["l", [5,0], [5,6]]])[0] == "polyline"


def test_partition_preserves_boundary_lines_and_does_not_swallow_crossing_geometry():
    tables = [{"id":"t1", "bbox_pt":[100,10,200,200]}]
    assert table_assignment([100,20,100,80], tables) == "t1"
    assert table_assignment([20,20,180,80], tables) is None
    assert table_assignment([20,20,80,80], tables) is None


def test_native_paths_and_original_styles_survive_partition():
    class Page:
        rect = fitz.Rect(0,0,300,300)
        def get_drawings(self):
            return [{"items":[("l",fitz.Point(20,20),fitz.Point(40,50))],"rect":fitz.Rect(20,20,40,50),"type":"s","dashes":"[3 2] 0","width":.5,"seqno":12},
                    {"items":[("re",fitz.Rect(120,30,140,40),1)],"rect":fitz.Rect(120,30,140,40),"type":"s","dashes":"[] 0","width":.25,"seqno":14}]
        def get_text(self, kind):
            return [(120,45,140,55,"501",0,0,0)]
        def get_image_info(self):
            return []
    records, count = extract_primitives(Page(), 2, [{"id":"t1","bbox_pt":[100,10,200,200]}])
    assert count == 2 and len(records) == 3
    assert [p["part"] for p in records] == ["structure", "table", "table"]
    assert records[0]["commands"] == [["l",[20,20],[40,50]]]
    assert records[0]["source_sequence"] == 12 and records[0]["line_style"] == "dashed"
    assert all(p["physical_member"] is None for p in records)


def test_paired_marks_keep_front_back_and_material_from_bom():
    circle = {"id":"c1","page":1,"part":"structure","primitive_type":"circle", "bbox_pt":[80,80,120,120],
              "geometry":{"center":[100,100],"radius":20}}
    words = [{"id":"t1","page":1,"part":"structure","primitive_type":"text","text":"前501","bbox_pt":[90,88,110,98]},
             {"id":"t2","page":1,"part":"structure","primitive_type":"text","text":"后502","bbox_pt":[90,102,110,112]}]
    bom = [{"mark":"501","classification":{"kind":"angle"}}, {"mark":"502","classification":{"kind":"angle"}}]
    callouts = associate_callouts([circle,*words], bom)
    assert len(callouts) == 1 and callouts[0]["role"] == "member_callout"
    assert [(m["mark"],m["side"]) for m in callouts[0]["marks"]] == [("501","前"),("502","后")]
    assert "no rod association" in callouts[0]["association_status"]
    bom[0]["classification"]["kind"] = "plate"
    bom[1]["classification"]["kind"] = "plate"
    assert associate_callouts([circle,*words], bom)[0]["role"] == "plate_callout"


def test_dashed_candidates_are_kept_and_never_asserted_as_native_dash():
    assert not is_dashed("[] 0") and is_dashed("[2 1] 0")
    lines = [{"id":str(i),"page":1,"part":"structure","primitive_type":"line","role":"unassigned_geometry","line_style":"solid",
              "geometry":{"start":[i*4,10],"end":[i*4+2,10]}} for i in range(8)]
    lines.append({"id":"solid","page":1,"part":"structure","primitive_type":"line","role":"unassigned_geometry","line_style":"solid",
                  "geometry":{"start":[0,20],"end":[40,20]}})
    groups = detect_dash_candidates(lines)
    assert len(groups) == 1 and len(groups[0]["primitive_ids"]) == 8
    assert all(p["line_style"] == "solid" for p in lines)
    assert lines[-1]["role"] == "unassigned_geometry"


def test_bom_array_reconciles_merged_rows_and_detects_lost_values():
    tables=[{"id":"t1","kind":"bom","rows":[["编号","规格","长度(mm)","数量"],
                                               ["469\n470","-6X220\n-6X208","249\n343","2\n1"]]}]
    bom=[{"mark":"469","specification":"-6X220","length_mm":249,"quantity":2},
         {"mark":"470","specification":"-6X208","length_mm":343,"quantity":1}]
    assert reconcile_bom_tables(tables,bom) == []
    bom[0]["quantity"] = 1
    assert any("469" in s for s in reconcile_bom_tables(tables,bom))


def test_spaced_ocr_title_still_requires_real_bom_rows_beneath_it():
    from types import SimpleNamespace
    from tower_rebuild.decomposition import table_seeds
    from tower_rebuild.schema import BomRow, Evidence
    cells = [[[10,10,90,20], None, None, None],
             [[10,20,30,30], [30,20,50,30], [50,20,70,30], [70,20,90,30]],
             [[10,30,30,40], [30,30,50,40], [50,30,70,40], [70,30,90,40]]]
    table = SimpleNamespace(bbox=(10,10,90,40), row_count=3, col_count=4,
                            rows=[SimpleNamespace(cells=c) for c in cells],
                            extract=lambda: [['件\n构 明 细 表',None,None,None],
                                             ['编号','规格','长度','数量'], ['1','L50X5','200','2']])
    page = SimpleNamespace(rect=fitz.Rect(0,0,100,100),
                           find_tables=lambda **kwargs: SimpleNamespace(tables=[table]))
    row = BomRow(mark='1', specification='L50X5', length_mm=200, quantity=2,
                 evidence=Evidence(page=1,bbox=(.1,.3,.9,.4),text='source cells'))
    seeds = table_seeds(page,[row],[])
    assert len(seeds)==1 and seeds[0]['kind']=='bom'
    assert seeds[0]['bbox_pt']==[10,10,90,40]
    assert not any(s['kind']=='bom' for s in table_seeds(page,[],[]))
