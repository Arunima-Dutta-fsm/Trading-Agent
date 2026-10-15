"""Student configuration and isolation."""
import json

import pytest
from conftest import add_signal, make_run

from aaba import approvals, db, paths
from aaba import broker as brokers
from aaba.settings import GroupConfig, load_students

PROPOSAL = {
    "S1": ("Akshita Jain", "065009", ["NVDA", "HOOD", "APP", "RCL"], ["MSFT", "LLY", "JPM", "PG", "MCD", "HON", "SLB", "PLD"]),
    "S2": ("Arunima Dutta", "065014", ["AMD", "SHOP", "NET", "TSLA"], ["GOOGL", "JNJ", "GS", "KO", "AMZN", "UNP", "NEM", "NEE"]),
    "S3": ("Navya Gupta", "065015", ["MU", "COIN", "PLTR", "NFLX"], ["AAPL", "UNH", "V", "WMT", "HD", "GE", "XOM", "LIN"]),
}


def test_three_students_load_exactly_as_proposed():
    """The lists must equal the proposal once logged replacements are undone, so a documented
    swap (e.g. NFLX -> MRVL from the reserves) passes, but a silent change does not."""
    g = load_students()
    assert [s.student_id for s in g.students] == ["S1", "S2", "S3"]
    for s in g.students:
        name, roll, sats, core = PROPOSAL[s.student_id]
        assert (s.name, s.roll_no) == (name, roll)
        assert g.original_symbols(s.student_id) == (sats, core)


def test_undocumented_or_invalid_replacement_is_rejected():
    raw = json.loads(paths.STUDENTS_FILE.read_text())
    clean = {k: v for k, v in raw.items() if not k.startswith("_")}
    st = clean["students"][0]
    st["satellites"][3] = "ANET"                     # RCL swapped silently
    st["replacements"] = []
    g = GroupConfig(**clean)
    with pytest.raises(AssertionError):
        assert g.original_symbols("S1") == (PROPOSAL["S1"][2], PROPOSAL["S1"][3])
    st["satellites"][3] = "CRWD"                     # not an approved reserve
    st["replacements"] = [{"old": "RCL", "new": "CRWD", "date": "2026-10-14", "reason": "test"}]
    with pytest.raises(ValueError, match="approved reserve"):
        GroupConfig(**clean)
    st["satellites"][3] = "ANET"
    st["replacements"] = [{"old": "RCL", "new": "ANET", "date": "2026-10-14", "reason": "beta below 1.3"}]
    assert GroupConfig(**clean).original_symbols("S1")[0] == PROPOSAL["S1"][2]


def test_each_student_has_12_unique_symbols_and_100k():
    g = load_students()
    for s in g.students:
        assert len(s.symbols) == 12 and len(set(s.symbols)) == 12
        assert s.starting_capital == 100000
    assert len(set(g.all_symbols)) == 36
    sats = sum(len(s.satellites) for s in g.students)
    assert sats / 36 >= 0.30          # brief: about 30% high-beta growth satellites


def test_shared_symbol_between_students_is_rejected():
    raw = json.loads(paths.STUDENTS_FILE.read_text())
    raw["students"][1]["core"][0] = "MSFT"     # MSFT already belongs to S1
    with pytest.raises(ValueError, match="shared"):
        GroupConfig(**{k: v for k, v in raw.items() if not k.startswith("_")})


def test_each_student_has_own_fake_account_and_files():
    g = load_students()
    accts = {s.student_id: brokers.get_broker(s).account()["account_number"] for s in g.students}
    assert len(set(accts.values())) == 3
    files = sorted(p.name for p in paths.FAKE_BROKER_DIR.glob("*.json"))
    assert files == ["S1.json", "S2.json", "S3.json"]


def test_student_records_are_isolated():
    run = make_run("paper", prices={"NVDA": 200.0, "AMD": 150.0})
    s1 = add_signal(run, "S1", "NVDA", "SELL", 200.0)
    add_signal(run, "S2", "AMD", "BUY", 150.0)
    b1 = brokers.get_broker(load_students().get("S1"))
    st = b1._load()
    st["positions"]["NVDA"] = {"qty": 10, "avg_entry": 190.0, "last_price": 200.0}
    b1._save(st)
    from aaba import controller
    res = controller.create_sell_approval(s1["signal_id"], "test")
    assert res["result"] == "SELL_PENDING_APPROVAL"
    assert [a["symbol"] for a in db.student_approvals("S1")] == ["NVDA"]
    assert db.student_approvals("S2") == [] and db.student_approvals("S3") == []
    assert [s["symbol"] for s in db.student_signals("S2", run_id=run)] == ["AMD"]
    assert all(s["student_id"] == "S1" for s in db.student_signals("S1"))
    # S2's broker never saw S1's position
    assert "NVDA" not in brokers.get_broker(load_students().get("S2")).positions()


def test_shared_account_blocks_orders_for_both_students():
    g = load_students()
    for sid in ("S1", "S2"):
        brokers.FakeBroker(g.get(sid)).set_state(account_number="SAME-ACCOUNT")
    res = brokers.verify_accounts()
    assert not res["S1"]["orders_allowed"] and not res["S2"]["orders_allowed"]
    assert any("shared" in p for p in res["S1"]["problems"])
    assert res["S3"]["orders_allowed"]


def test_expected_account_number_mismatch_blocks(monkeypatch):
    monkeypatch.setenv("STUDENT3_ALPACA_ACCOUNT_NUMBER", "PA-SOMETHING-ELSE")
    res = brokers.verify_accounts()
    assert not res["S3"]["orders_allowed"]


def test_approval_fingerprint_binds_details():
    a = approvals.fingerprint("S1", "A1", "NVDA", 10, "sig-1", "t")
    assert a != approvals.fingerprint("S2", "A1", "NVDA", 10, "sig-1", "t")
    assert a != approvals.fingerprint("S1", "A1", "NVDA", 11, "sig-1", "t")
    assert a != approvals.fingerprint("S1", "A1", "AMD", 10, "sig-1", "t")
