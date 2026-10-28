import pytest

from tower_rebuild.native_bom import split_cell_rows


def test_merged_rows_are_split_instead_of_concatenating_numbers():
    assert split_cell_rows("469\n470", "-6X220\n-6X208", "249\n343", "2\n1") == [
        ("469", "-6X220", "249", "2"), ("470", "-6X208", "343", "1")]
    assert split_cell_rows("4 0 1", "Q 4 2 0 L125X10", "8004", "1") == [("401", "Q420L125X10", "8004", "1")]


def test_ambiguous_merged_cells_are_not_guessed():
    with pytest.raises(ValueError):
        split_cell_rows("469\n470", "-6X220", "249\n343", "2\n1")


def test_letter_suffix_marks_are_distinct_rows():
    assert split_cell_rows('509A\n509B', 'L56X4\nL56X4', '1466\n1466', '1\n1') == [
        ('509A', 'L56X4', '1466', '1'), ('509B', 'L56X4', '1466', '1')]
    assert split_cell_rows('A-101', 'L50X5', '1200', '2')[0][0] == 'A-101'
    assert split_cell_rows('合计', '', '', '') == []
    with pytest.raises(ValueError, match='件号'):
        split_cell_rows('A.1', 'Q345L70X5', '1200', '2')
    with pytest.raises(ValueError, match='件号'):
        split_cell_rows('', '-5X120', '1200', '2')


def test_header_fallback_uses_all_native_labels_not_fixed_page_side():
    from tower_rebuild.native_bom import native_header_lefts
    class Page:
        def get_text(self, kind):
            assert kind == 'words'
            return [(100, 40, 110, 50, '编'), (100, 54, 110, 64, '号'),
                    (140, 47, 150, 57, '规'), (165, 47, 175, 57, '格'),
                    (200, 47, 210, 57, '长'), (225, 47, 235, 57, '度'),
                    (260, 40, 270, 50, '数'), (260, 54, 270, 64, '量')]
    assert native_header_lefts(Page()) == pytest.approx([71.2])
    class MissingQuantity(Page):
        def get_text(self, kind):
            return super().get_text(kind)[:-2]
    assert native_header_lefts(MissingQuantity()) == []
