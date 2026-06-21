"""MergeTree invariants: acyclicity, the unfolding, path validity."""

import pytest

from rhizome.utils.data_structures import MergeTree, Path
from rhizome.utils.data_structures.graph import CycleError


@pytest.fixture
def diamond() -> MergeTree[str]:
    # A -> B -> C and A -> D -> C: one merged node, two locations in the unfolding.
    tree = MergeTree("A")
    for parent, child in [("A", "B"), ("A", "D"), ("B", "C"), ("D", "C")]:
        tree.add_edge(parent, child)
    return tree


def test_unfolding_visits_merged_node_once_per_path(diamond):
    paths = {tuple(p) for p in diamond.paths_to("C")}
    assert paths == {("A", "B", "C"), ("A", "D", "C")}
    assert sum(1 for p in diamond.walk() if p.node == "C") == 2
    assert {tuple(p) for p in diamond.leaf_paths()} == {("A", "B", "C"), ("A", "D", "C")}


def test_acyclicity_enforced(diamond):
    with pytest.raises(CycleError):
        diamond.add_edge("C", "B")  # would close B -> C -> B
    with pytest.raises(ValueError):
        diamond.add_edge("C", "A")  # the root cannot acquire a parent
    with pytest.raises(KeyError):
        diamond.add_edge("nope", "X")


def test_reachable_is_reflexive_and_directional(diamond):
    assert diamond.reachable("A", "C") and diamond.reachable("B", "C")
    assert not diamond.reachable("C", "A") and not diamond.reachable("B", "D")
    assert diamond.reachable("B", "B")


def test_merge_shape_via_add_edge(diamond):
    # The conversation-merge shape: a fresh node below two existing ones.
    diamond.add_edge("B", "E")
    diamond.add_edge("D", "E")
    assert {tuple(p) for p in diamond.paths_to("E")} == {("A", "B", "E"), ("A", "D", "E")}


def test_path_validity_goes_stale_after_removal(diamond):
    path = next(p for p in diamond.paths_to("C") if tuple(p) == ("A", "B", "C"))
    assert diamond.is_valid(path)
    diamond.remove_edge("B", "C")
    assert not diamond.is_valid(path)
    # The other lineage is unaffected.
    assert {tuple(p) for p in diamond.paths_to("C")} == {("A", "D", "C")}


def test_sibling_and_parent_order_is_insertion_order(diamond):
    # Children in edge-creation order, parents in arrival order (first parent = home lineage).
    diamond.add_edge("A", "E")
    assert diamond.children("A") == ("B", "D", "E")
    assert diamond.parents("C") == ("B", "D")


def test_path_value_semantics():
    p1 = Path("C", Path("B", Path("A")))
    p2 = Path("C", Path("B", Path("A")))
    assert p1 == p2 and hash(p1) == hash(p2) and len(p1) == 3
    assert list(p1) == ["A", "B", "C"]
