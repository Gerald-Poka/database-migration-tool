"""Wave computation / FK graph tests."""

import pytest

from ferry.engine.waves import mapping_edges, plan_waves, topo_waves


def test_topo_plain_chain():
    # parent <- child <- grandchild
    waves = topo_waves(["p", "c", "g"], {"c": {"p"}, "g": {"c"}, "p": set()})
    assert waves == [["p"], ["c"], ["g"]]


def test_topo_parallel_groups():
    waves = topo_waves(["t1", "t2", "t3"], {"t3": {"t1", "t2"}})
    assert waves[0] == ["t1", "t2"]
    assert waves[1] == ["t3"]


def test_topo_self_ref_ignored():
    waves = topo_waves(["node"], {"node": {"node"}})
    assert waves == [["node"]]


def test_topo_cycle_detected():
    with pytest.raises(ValueError) as e:
        topo_waves(["a", "b"], {"a": {"b"}, "b": {"a"}})
    assert "cycle" in str(e.value).lower()


def test_mapping_edges_schema_qualified_targets():
    mappings = [
        {"target": "admissions.applicants",
         "_target_fks": [{"ref_table": "academic_years"}]},
        {"target": "core.academic_years",
         "_target_fks": []},
    ]
    edges = mapping_edges(mappings)
    assert edges["admissions.applicants"] == {"core.academic_years"}


def test_mapping_edges_prefers_exact_schema_for_duplicate_bare_names():
    mappings = [
        {"target": "app.users", "_target_fks": []},
        {"target": "archive.users", "_target_fks": []},
        {"target": "app.orders", "_target_fks": [{"ref_table": "app.users"}]},
    ]
    edges = mapping_edges(mappings)
    assert edges["app.orders"] == {"app.users"}


def test_mapping_edges_ignores_ambiguous_unqualified_reference():
    mappings = [
        {"target": "app.users", "_target_fks": []},
        {"target": "archive.users", "_target_fks": []},
        {"target": "orders", "_target_fks": [{"ref_table": "users"}]},
    ]
    assert mapping_edges(mappings)["orders"] == set()


def test_plan_waves_returns_mappings():
    mappings = [
        {"target": "b", "_target_fks": [{"ref_table": "a"}]},
        {"target": "a", "_target_fks": []},
    ]
    waves, by = plan_waves(mappings)
    assert waves == [[mappings[1]], [mappings[0]]]
    assert by["a"] is mappings[1]
