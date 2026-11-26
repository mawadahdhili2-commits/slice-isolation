#!/usr/bin/env python3
"""Offline tests: the real xApp logic runs against a FAKE xapp_sdk (no FlexRIC needed).
Run: python3 test_xapp_logic.py"""
import sys, json, types, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

# ---------------- fake SDK ----------------
class Auto:
    def __getattr__(self, k):
        if k.startswith("__"): raise AttributeError(k)
        v = Auto(); object.__setattr__(self, k, v); return v
class _Cb:
    def __init__(self): pass
fake = types.ModuleType("xapp_sdk")
fake.sent = []; fake.cbs = {}; fake.fail_next = False
fake.mac_cb = type("mac_cb", (_Cb,), {}); fake.slice_cb = type("slice_cb", (_Cb,), {})
for n, v in dict(Interval_ms_10=10, Interval_ms_1000=1000, SLICE_CTRL_SM_V0_ADD=1, SLICE_CTRL_SM_V0_DEL=2,
                 SLICE_CTRL_SM_V0_UE_SLICE_ASSOC=3, SLICE_ALG_SM_V0_STATIC=1, SLICE_ALG_SM_V0_NVS=2,
                 SLICE_SM_NVS_V0_RATE=1, SLICE_SM_NVS_V0_CAPACITY=2).items(): setattr(fake, n, v)
for n in ("slice_ctrl_msg_t", "fr_slice_t", "ue_slice_assoc_t"): setattr(fake, n, Auto)
for n in ("slice_array", "ue_slice_assoc_array", "del_dl_array"): setattr(fake, n, lambda k: [None]*k)
fake.init = lambda argv: None
fake.conn_e2_nodes = lambda: [types.SimpleNamespace(id=7)]
fake.report_mac_sm = lambda i, p, cb: fake.cbs.__setitem__("mac", cb)
fake.report_slice_sm = lambda i, p, cb: fake.cbs.__setitem__("slice", cb)
def ctrl(i, msg):
    if fake.fail_next: fake.fail_next = False; raise RuntimeError("no ack")
    fake.sent.append(msg)
fake.control_slice_sm = ctrl
sys.modules["xapp_sdk"] = fake

import xapp_slice_isolation as X

def mk():
    d = tempfile.mkdtemp(); lg = X.LoggingManager(d)
    ad = X.FlexRICAdapter(fake, lg, ["x"]); ad.subscribe()
    return ad, lg, d

def test_scenarios():
    c = X.get_scenario_configs(X.Scenario.STATIC_BALANCED)
    assert [(s.pos_low, s.pos_high) for s in c] == [(0, 6), (7, 13)]
    c = X.get_scenario_configs(X.Scenario.STATIC_UNBALANCED)
    assert [(s.pos_low, s.pos_high) for s in c] == [(0, 10), (11, 13)]
    for sc, exp in ((X.Scenario.NVS_RATE_BALANCED, (9, 9)), (X.Scenario.NVS_RATE_UNBALANCED, (15, 3))):
        c = X.get_scenario_configs(sc)
        assert tuple(s.mbps_rsvd for s in c) == exp and sum(s.mbps_rsvd for s in c) <= X.NVS_REF_MBPS
    r = X.SliceConfig.from_dict(c[0].to_dict()); assert r == c[0]
    print("OK scenarios (STATIC 7+7 / 11+3, NVS 9+9 / 15+3, sum<=ref, serialization)")

def test_messages():
    ad, *_ = mk()
    m = ad.build_add(X.get_scenario_configs(X.Scenario.STATIC_UNBALANCED))
    assert m.type == 1 and m.u.add_mod_slice.dl.len_slices == 2
    s0 = m.u.add_mod_slice.dl.slices[0]
    assert (s0.id, s0.params.u.sta.pos_low, s0.params.u.sta.pos_high) == (1, 0, 10)
    m = ad.build_add(X.get_scenario_configs(X.Scenario.NVS_RATE_UNBALANCED))
    s1 = m.u.add_mod_slice.dl.slices[1]
    assert s1.params.u.nvs.u.rate.u1.mbps_required == 3.0 and s1.params.u.nvs.u.rate.u2.mbps_reference == 18.0
    m = ad.build_assoc([("0xdf", 2)]); assert m.u.ue_slice.ues[0].rnti == 0xdf and m.u.ue_slice.ues[0].dl_id == 2
    assert ad.build_del([2, 1]).u.del_slice.len_dl == 2
    assert ad.build_reset().u.add_mod_slice.dl.len_slices == 0
    print("OK message building (STATIC, NVS-RATE, ASSOC, DEL, RESET)")

def feed(ad, rntis, slice_state):
    ind = Auto(); ind.ue_stats = [types.SimpleNamespace(rnti=r) for r in rntis]
    fake.cbs["mac"].handle(ind)
    fake.cbs["slice"].handle(slice_state)

def slice_ind(cfgs, assoc):
    i = Auto(); sl = []
    for c in cfgs:
        s = Auto(); s.id = c.slice_id; s.params.type = 1
        s.params.u.sta.pos_low = c.pos_low; s.params.u.sta.pos_high = c.pos_high; sl.append(s)
    i.slice_stats.dl.slices = sl; i.slice_stats.dl.len_slices = len(sl)
    i.ue_slice_stats.ues = [types.SimpleNamespace(rnti=int(r, 16), dl_id=d) for r, d in assoc.items()]
    return i

def test_full_scenario():
    fake.sent.clear(); X.SETTLE_AFTER_CONTROL_S = 0
    ad, lg, d = mk()
    cfgs = X.get_scenario_configs(X.Scenario.STATIC_BALANCED)
    feed(ad, [0x5678, 0x1234], slice_ind(cfgs, {"0x1234": 1, "0x5678": 2}))  # reversed discovery order
    app = X.SliceIsolationXApp(ad, lg, 0, [X.Scenario.STATIC_BALANCED])
    r = app.run_scenario(X.Scenario.STATIC_BALANCED)
    assert r.success and r.state_verified is True, r
    assert r.ue_associations == {"0x1234": 1, "0x5678": 2}          # deterministic
    assert [m.type for m in fake.sent] == [1, 3, 2, 1]              # ADD, ASSOC, DEL, RESET
    ev = [json.loads(l) for l in open(next(Path(d).glob("*.jsonl")))]
    types_ = [e["event_type"] for e in ev]
    for t in ("COMMAND_SENT", "ACK_RECEIVED", "UE_DISCOVERED", "UE_ASSOCIATED", "STATE_VERIFIED", "STATE_OBSERVED"):
        assert t in types_, t
    assert all("timestamp" in e for e in ev)
    print("OK full scenario: ADD->ASSOC->verify->DEL->RESET, logs timestamped")

def test_state_mismatch_detected():
    fake.sent.clear(); X.SETTLE_AFTER_CONTROL_S = 0
    ad, lg, d = mk()
    cfgs = X.get_scenario_configs(X.Scenario.STATIC_BALANCED)
    wrong = X.get_scenario_configs(X.Scenario.STATIC_UNBALANCED)
    feed(ad, [1, 2], slice_ind(wrong, {"0x1": 2, "0x2": 1}))
    app = X.SliceIsolationXApp(ad, lg, 0, [])
    r = app.run_scenario(X.Scenario.STATIC_BALANCED)
    assert r.success and r.state_verified is False   # logged, scenario not aborted
    print("OK mismatch between request and reported state is detected and logged")

def test_ack_failure_and_cleanup():
    fake.sent.clear(); X.SETTLE_AFTER_CONTROL_S = 0
    ad, lg, d = mk(); feed(ad, [1, 2], Auto())
    fake.fail_next = True
    r = X.SliceIsolationXApp(ad, lg, 0, []).run_scenario(X.Scenario.NVS_RATE_BALANCED)
    assert not r.success and "ADD failed" in r.error_message
    assert fake.sent and fake.sent[-1].u.add_mod_slice.dl.len_slices == 0   # RESET still sent
    print("OK control failure -> scenario FAILED, RESET still sent")

def test_missing_ue():
    ad, lg, d = mk(); feed(ad, [1], Auto()); ad.rntis = ad.rntis[:1]
    app = X.SliceIsolationXApp(ad, lg, 0, [])
    try: app.wait_for_ues(2, timeout=0.6); assert False
    except RuntimeError as e: assert "Only 1" in str(e)
    print("OK missing 2nd UE detected")

if __name__ == "__main__":
    for t in (test_scenarios, test_messages, test_full_scenario, test_state_mismatch_detected,
              test_ack_failure_and_cleanup, test_missing_ue): t()
    print("\nALL TESTS PASSED")
