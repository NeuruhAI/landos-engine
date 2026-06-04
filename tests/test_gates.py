from app.gates import evaluate_gates


def test_environmental_is_curable_flag_not_kill():
    r = evaluate_gates({"environmental_flag": True})
    assert r["killed"] is False
    assert r["flags"] == ["environmental_flag"]
    assert r["gates_failed"] == []


def test_hard_kill_gate_kills():
    r = evaluate_gates({"title_uncured": True})
    assert r["killed"] is True and "title_uncured" in r["gates_failed"]


def test_clean_parcel():
    r = evaluate_gates({})
    assert r["killed"] is False and r["flags"] == [] and r["gates_failed"] == []


def test_mixed_flag_and_kill():
    r = evaluate_gates({"environmental_flag": True, "occupied": True})
    assert r["killed"] is True
    assert "occupied" in r["gates_failed"] and "environmental_flag" in r["flags"]
