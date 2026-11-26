#!/usr/bin/env python3
"""
xApp Slice Isolation - Phase 2 (v2, real FlexRIC SDK)
=====================================================

Creates two slices (STATIC then NVS-RATE, balanced / unbalanced), attaches
one UE per slice using the RNTI read from MAC reports, holds each
configuration for a fixed duration (iperf3 window), compares the SLICE_STATS
state reported by the gNB with the requested configuration, and logs
everything with timestamps (JSONL + human-readable).

IMPORTANT (why v2): v1 imported classes (XApp, E2Client) that do not exist in
the FlexRIC SDK and silently fell back to mock classes, so no E2 message was
ever sent. v2 uses the real SDK exactly like xapp_slice_moni_ctrl.py:
    import xapp_sdk as ric
    ric.init() / ric.conn_e2_nodes() / ric.report_*_sm() / ric.control_slice_sm()
and FAILS LOUDLY if the SDK is missing (no silent mock).

All SDK-specific code lives in class FlexRICAdapter, so if a field name
differs on your build, only that class needs a fix (see --probe).

Authors: Oueslati Ranim, Ben Hedhili Mawadda
"""

import sys
import time
import json
import logging
import threading
import argparse
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from pathlib import Path

# =============================================================================
# CONSTANTS
# =============================================================================
DEFAULT_MEASUREMENT_DURATION = 30   # seconds, matches the 30 s iperf3 of Phase 1
TOTAL_PRBS = 14                     # PRB indices 0..13 (testbed report)
BASELINE_MBPS = 18.0                # receiver throughput without slicing (report)
NVS_REF_MBPS = 18.0                 # reference rate for NVS-RATE (see SliceConfig)
UE1_IP, UE2_IP = "10.45.0.39", "10.45.0.40"
UE_SCHED = "PF"                     # per-slice UE scheduler (proportional fair)
SLICE_STATS_PERIOD_MS = 1000
SETTLE_AFTER_CONTROL_S = 2.0        # let the gNB apply the config before verifying


class SliceAlgorithm(Enum):
    STATIC = "STATIC"
    NVS_RATE = "NVS-RATE"
    NVS_CAPACITY = "NVS-CAPACITY"
    NONE = "NONE"


class SliceOperation(Enum):
    ADD = "ADD"
    ASSOC = "ASSOC"
    DEL = "DEL"
    RESET = "RESET"


class Scenario(Enum):
    STATIC_BALANCED = "STATIC_BALANCED"
    STATIC_UNBALANCED = "STATIC_UNBALANCED"
    NVS_RATE_BALANCED = "NVS_RATE_BALANCED"
    NVS_RATE_UNBALANCED = "NVS_RATE_UNBALANCED"


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class SliceConfig:
    """One slice. STATIC uses pos_low/pos_high (PRB range, inclusive).
    NVS-RATE uses mbps_rsvd (guaranteed rate) relative to mbps_ref: the slice
    gets the fraction mbps_rsvd/mbps_ref of the resources, so the sum of
    mbps_rsvd must stay <= mbps_ref (9+9 and 15+3 both = 18)."""
    slice_id: int
    label: str
    algorithm: SliceAlgorithm
    pos_low: Optional[int] = None
    pos_high: Optional[int] = None
    mbps_rsvd: Optional[float] = None
    mbps_ref: Optional[float] = None
    percentage: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["algorithm"] = self.algorithm.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SliceConfig":
        d = dict(d)
        d["algorithm"] = SliceAlgorithm(d["algorithm"])
        return cls(**d)


@dataclass
class UEInfo:
    rnti: str                       # hex string, e.g. "0xdf"
    ip: str = ""
    namespace: str = ""
    slice_id: Optional[int] = None
    associated: bool = False


@dataclass
class LogEntry:
    timestamp: str
    event_type: str     # COMMAND_SENT, ACK_RECEIVED, STATE_OBSERVED, STATE_VERIFIED, ...
    details: Dict[str, Any]
    scenario: Optional[str] = None
    configuration: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


@dataclass
class ScenarioResult:
    scenario: Scenario
    start_time: str
    end_time: str
    slice_configs: List[SliceConfig]
    ue_associations: Dict[str, int]
    commands_sent: List[LogEntry]
    acks_received: List[LogEntry]
    measurement_duration: int
    success: bool
    error_message: Optional[str] = None
    state_verified: Optional[bool] = None   # informational (SLICE_STATS is unreliable)


# =============================================================================
# SCENARIOS  (14 PRBs total; baseline ~18 Mbit/s)
# =============================================================================
def get_scenario_configs(scenario: Scenario) -> List[SliceConfig]:
    S, N = SliceAlgorithm.STATIC, SliceAlgorithm.NVS_RATE
    if scenario == Scenario.STATIC_BALANCED:        # 7 + 7 PRBs
        return [SliceConfig(1, "slice_bal_1", S, pos_low=0, pos_high=6),
                SliceConfig(2, "slice_bal_2", S, pos_low=7, pos_high=13)]
    if scenario == Scenario.STATIC_UNBALANCED:      # 11 + 3 PRBs
        return [SliceConfig(1, "slice_wide", S, pos_low=0, pos_high=10),
                SliceConfig(2, "slice_narrow", S, pos_low=11, pos_high=13)]
    if scenario == Scenario.NVS_RATE_BALANCED:      # 9 + 9 Mbit/s
        return [SliceConfig(1, "rate_bal_1", N, mbps_rsvd=9.0, mbps_ref=NVS_REF_MBPS),
                SliceConfig(2, "rate_bal_2", N, mbps_rsvd=9.0, mbps_ref=NVS_REF_MBPS)]
    if scenario == Scenario.NVS_RATE_UNBALANCED:    # 15 + 3 Mbit/s
        return [SliceConfig(1, "rate_wide", N, mbps_rsvd=15.0, mbps_ref=NVS_REF_MBPS),
                SliceConfig(2, "rate_narrow", N, mbps_rsvd=3.0, mbps_ref=NVS_REF_MBPS)]
    raise ValueError(scenario)


# =============================================================================
# LOGGING
# =============================================================================
class LoggingManager:
    """JSONL (machine) + .log (human) + console. Every entry is timestamped."""

    def __init__(self, log_dir: str = "/tmp/xapp_logs", xapp_name: str = "xapp_slice_isolation"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.jsonl_file = self.log_dir / f"{xapp_name}_{ts}.jsonl"
        self.log_file = self.log_dir / f"{xapp_name}_{ts}.log"
        self._lock = threading.Lock()   # SDK callbacks run in C threads

        self.logger = logging.getLogger(f"{xapp_name}_{ts}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        fh = logging.FileHandler(self.log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%H:%M:%S"))
        self.logger.addHandler(fh)
        self.logger.addHandler(ch)
        self.logger.info(f"JSONL: {self.jsonl_file}")

    def log(self, entry: LogEntry):
        with self._lock:
            with open(self.jsonl_file, "a") as f:
                f.write(entry.to_json() + "\n")
        sc = entry.scenario
        sc = sc.value if hasattr(sc, "value") else sc
        msg = f"[{entry.event_type}] {entry.details}"
        if sc:
            msg = f"[{sc}] {msg}"
        # keep very large state dumps out of the console (they stay in the JSONL)
        if entry.event_type == "STATE_OBSERVED":
            self.logger.debug(msg)
        else:
            self.logger.log(logging.ERROR if entry.event_type == "ERROR" else logging.INFO, msg)

    def _e(self, etype, details, scenario=None):
        sc = scenario.value if hasattr(scenario, "value") else scenario
        self.log(LogEntry(datetime.now().isoformat(), etype, details, sc))

    def info(self, message, scenario=None):
        self._e("INFO", {"message": message}, scenario)

    def error(self, message, scenario=None, exc: Optional[Exception] = None):
        d = {"message": message}
        if exc:
            d["exception"] = repr(exc)
        self._e("ERROR", d, scenario)

    def command_sent(self, operation: SliceOperation, payload: Dict, scenario):
        self._e("COMMAND_SENT", {"operation": operation.value, **payload}, scenario)

    def ack_received(self, operation: SliceOperation, success: bool, latency_ms: float,
                     scenario, extra: Optional[Dict] = None):
        self._e("ACK_RECEIVED", {"operation": operation.value, "success": success,
                                 "latency_ms": round(latency_ms, 1), **(extra or {})}, scenario)

    def state_observed(self, state: Dict, scenario):
        self._e("STATE_OBSERVED", {"state": state}, scenario)

    def state_verified(self, match: bool, expected: Dict, observed: Dict, mismatches: List[str], scenario):
        self._e("STATE_VERIFIED", {"match": match, "mismatches": mismatches,
                                   "expected": expected, "observed": observed}, scenario)

    def ue_discovered(self, ue: UEInfo, scenario):
        self._e("UE_DISCOVERED", {"rnti": ue.rnti}, scenario)

    def ue_associated(self, rnti: str, slice_id: int, scenario):
        self._e("UE_ASSOCIATED", {"rnti": rnti, "slice_id": slice_id}, scenario)


# =============================================================================
# FLEXRIC ADAPTER  (all SDK-specific code is here)
# =============================================================================
def _g(obj, path: str, default=None):
    """Safe nested getattr: _g(ind, 'slice_stats.dl.len_slices')."""
    for p in path.split("."):
        try:
            obj = getattr(obj, p)
        except Exception:
            return default
    return obj


class FlexRICAdapter:
    """Thin wrapper around the FlexRIC Python SDK (xapp_sdk).

    Control calls (ric.control_slice_sm) are synchronous: the SDK sends the
    RIC CONTROL REQUEST and blocks until the CONTROL ACK, which is why the
    reference xApp prints 'CONTROL-REQUEST tx / CONTROL ACK rx / Successfully
    received CONTROL response' in order. Returning without exception == ACK.
    """

    def __init__(self, ric_module, logger: LoggingManager, extra_argv: List[str]):
        self.ric = ric_module
        self.log = logger
        self.lock = threading.Lock()
        self.rntis: List[str] = []              # discovered RNTIs (hex strings)
        self.last_slice_state: Dict[str, Any] = {}
        self.current_scenario = None
        self.conn = None
        self.node_id = None
        self._cbs = []                          # keep callbacks alive (GC!)
        self.ric.init(extra_argv)
        self.conn = self.ric.conn_e2_nodes()
        if len(self.conn) == 0:
            raise RuntimeError("No E2 node connected to the RIC (start RIC then gNB first)")
        self.node_id = self.conn[0].id

    # ---- subscriptions ----------------------------------------------------
    def subscribe(self):
        ric, adapter = self.ric, self

        class MacCb(ric.mac_cb):
            def __init__(self):
                ric.mac_cb.__init__(self)

            def handle(self, ind):
                try:
                    for u in ind.ue_stats:
                        r = hex(u.rnti)
                        with adapter.lock:
                            if r not in adapter.rntis:
                                adapter.rntis.append(r)
                except Exception as e:
                    adapter.log.error("MAC callback error", adapter.current_scenario, e)

        class SliceCb(ric.slice_cb):
            def __init__(self):
                ric.slice_cb.__init__(self)

            def handle(self, ind):
                try:
                    st = adapter.slice_ind_to_dict(ind)
                    with adapter.lock:
                        adapter.last_slice_state = st
                    adapter.log.state_observed(st, adapter.current_scenario)
                except Exception as e:
                    adapter.log.error("SLICE callback error", adapter.current_scenario, e)

        mac, sl = MacCb(), SliceCb()
        self._cbs += [mac, sl]
        self.ric.report_mac_sm(self.node_id, self.ric.Interval_ms_10, mac)
        self.ric.report_slice_sm(self.node_id, self.ric.Interval_ms_1000, sl)
        self.log.info("Subscribed to MAC (RNTI discovery) and SLICE_STATS")

    @staticmethod
    def slice_ind_to_dict(ind) -> Dict[str, Any]:
        """Convert a SLICE_STATS indication to a plain dict (same fields as
        the reference xApp writes into rt_slice_stats.json)."""
        names = {1: "STATIC", 2: "NVS", 3: "SCN19", 4: "EDF"}
        slices = []
        for s in (_g(ind, "slice_stats.dl.slices", []) or []):
            t = _g(s, "params.type")
            d = {"id": _g(s, "id"), "algo": names.get(t, str(t))}
            if t == 1:
                d["pos_low"] = _g(s, "params.u.sta.pos_low")
                d["pos_high"] = _g(s, "params.u.sta.pos_high")
            elif t == 2:
                d["mbps_rsvd"] = _g(s, "params.u.nvs.u.rate.u1.mbps_required")
            slices.append(d)
        slices = slices[: int(_g(ind, "slice_stats.dl.len_slices", 0) or 0)]
        ues = [{"rnti": hex(_g(u, "rnti", 0)), "dl_id": _g(u, "dl_id")}
               for u in (_g(ind, "ue_slice_stats.ues", []) or [])]
        return {"num_slices": _g(ind, "slice_stats.dl.len_slices", 0),
                "slices": slices, "ues": ues}

    # ---- message builders -------------------------------------------------
    def _make_slice(self, c: SliceConfig):
        ric = self.ric
        s = ric.fr_slice_t()
        s.id = c.slice_id
        s.label = c.label
        s.len_label = len(c.label)
        s.sched = UE_SCHED
        s.len_sched = len(UE_SCHED)
        if c.algorithm == SliceAlgorithm.STATIC:
            s.params.type = ric.SLICE_ALG_SM_V0_STATIC
            s.params.u.sta.pos_low = c.pos_low
            s.params.u.sta.pos_high = c.pos_high
        elif c.algorithm == SliceAlgorithm.NVS_RATE:
            s.params.type = ric.SLICE_ALG_SM_V0_NVS
            s.params.u.nvs.conf = ric.SLICE_SM_NVS_V0_RATE
            s.params.u.nvs.u.rate.u1.mbps_required = c.mbps_rsvd
            s.params.u.nvs.u.rate.u2.mbps_reference = c.mbps_ref
        elif c.algorithm == SliceAlgorithm.NVS_CAPACITY:
            s.params.type = ric.SLICE_ALG_SM_V0_NVS
            s.params.u.nvs.conf = ric.SLICE_SM_NVS_V0_CAPACITY
            s.params.u.nvs.u.capacity.u.pct_reserved = c.percentage
        return s

    def build_add(self, configs: List[SliceConfig]):
        ric = self.ric
        msg = ric.slice_ctrl_msg_t()
        msg.type = ric.SLICE_CTRL_SM_V0_ADD
        dl = msg.u.add_mod_slice.dl
        dl.len_slices = len(configs)
        dl.sched_name = UE_SCHED
        dl.len_sched_name = len(UE_SCHED)
        arr = ric.slice_array(len(configs))
        for i, c in enumerate(configs):
            arr[i] = self._make_slice(c)
        dl.slices = arr
        return msg

    def build_assoc(self, pairs: List[tuple]):
        ric = self.ric
        msg = ric.slice_ctrl_msg_t()
        msg.type = ric.SLICE_CTRL_SM_V0_UE_SLICE_ASSOC
        msg.u.ue_slice.len_ue_slice = len(pairs)
        arr = ric.ue_slice_assoc_array(len(pairs))
        for i, (rnti_hex, slice_id) in enumerate(pairs):
            a = ric.ue_slice_assoc_t()
            a.rnti = int(rnti_hex, 16)
            a.dl_id = slice_id
            arr[i] = a
        msg.u.ue_slice.ues = arr
        return msg

    def build_del(self, slice_ids: List[int]):
        ric = self.ric
        msg = ric.slice_ctrl_msg_t()
        msg.type = ric.SLICE_CTRL_SM_V0_DEL
        msg.u.del_slice.len_dl = len(slice_ids)
        arr = ric.del_dl_array(len(slice_ids))
        for i, sid in enumerate(slice_ids):
            arr[i] = sid
        msg.u.del_slice.dl = arr
        return msg

    def build_reset(self):
        # RESET == ADD with zero slices: the gNB falls back to algo NONE.
        return self.build_add([])

    # ---- control ----------------------------------------------------------
    def send(self, msg) -> None:
        """Blocking: returns when CONTROL ACK received, raises otherwise."""
        self.ric.control_slice_sm(self.node_id, msg)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.last_slice_state, default=str))

    def shutdown(self):
        for fn in ("rm_report_slice_sm", "rm_report_mac_sm"):
            try:
                getattr(self.ric, fn)(self.node_id)
            except Exception:
                pass


# =============================================================================
# MAIN XAPP
# =============================================================================
class SliceIsolationXApp:
    def __init__(self, adapter: FlexRICAdapter, logger: LoggingManager,
                 measurement_duration: int, scenarios: List[Scenario]):
        self.a = adapter
        self.log = logger
        self.duration = measurement_duration
        self.scenarios = scenarios
        self.results: List[ScenarioResult] = []
        self.running = False
        self.created: List[int] = []
        self.sc: Optional[Scenario] = None

    # ---- one control operation with logging + ACK timing ------------------
    def _do(self, op: SliceOperation, payload: Dict, msg) -> bool:
        self.log.command_sent(op, payload, self.sc)
        t0 = time.time()
        try:
            self.a.send(msg)
            ok, err = True, None
        except Exception as e:
            ok, err = False, repr(e)
        self.log.ack_received(op, ok, (time.time() - t0) * 1000, self.sc,
                              {"error": err} if err else None)
        return ok

    def slice_add(self, configs):
        return self._do(SliceOperation.ADD, {"slices": [c.to_dict() for c in configs]},
                        self.a.build_add(configs))

    def slice_assoc(self, pairs):
        return self._do(SliceOperation.ASSOC, {"assoc": [{"rnti": r, "slice_id": s} for r, s in pairs]},
                        self.a.build_assoc(pairs))

    def slice_del(self, ids):
        return self._do(SliceOperation.DEL, {"slice_ids": ids}, self.a.build_del(ids))

    def slice_reset(self):
        return self._do(SliceOperation.RESET, {"algorithm": "NONE"}, self.a.build_reset())

    # ---- verification: requested config vs state reported by the gNB ------
    def verify_state(self, configs, assoc: Dict[str, int]) -> bool:
        obs = self.a.snapshot()
        exp = {"num_slices": len(configs),
               "slices": [c.to_dict() for c in configs],
               "assoc": assoc}
        mism = []
        if obs.get("num_slices") != len(configs):
            mism.append(f"num_slices expected {len(configs)} got {obs.get('num_slices')}")
        by_id = {s.get("id"): s for s in obs.get("slices", [])}
        for c in configs:
            o = by_id.get(c.slice_id)
            if o is None:
                mism.append(f"slice {c.slice_id} missing")
            elif c.algorithm == SliceAlgorithm.STATIC and \
                    (o.get("pos_low"), o.get("pos_high")) != (c.pos_low, c.pos_high):
                mism.append(f"slice {c.slice_id} PRB range expected {c.pos_low}-{c.pos_high} "
                            f"got {o.get('pos_low')}-{o.get('pos_high')}")
            elif c.algorithm == SliceAlgorithm.NVS_RATE and o.get("mbps_rsvd") != c.mbps_rsvd:
                mism.append(f"slice {c.slice_id} mbps expected {c.mbps_rsvd} got {o.get('mbps_rsvd')}")
        obs_assoc = {u["rnti"]: u["dl_id"] for u in obs.get("ues", [])}
        for r, sid in assoc.items():
            if obs_assoc.get(r) != sid:
                mism.append(f"UE {r} expected slice {sid} got {obs_assoc.get(r)}")
        match = not mism
        self.log.state_verified(match, exp, obs, mism, self.sc)
        return match

    def wait_for_ues(self, n=2, timeout=15.0) -> List[str]:
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.a.lock:
                if len(self.a.rntis) >= n:
                    return sorted(self.a.rntis, key=lambda x: int(x, 16))
            time.sleep(0.5)
        raise RuntimeError(f"Only {len(self.a.rntis)} UE RNTI(s) seen in MAC reports (need {n})")

    # ---- scenario ---------------------------------------------------------
    def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        self.sc = scenario
        self.a.current_scenario = scenario
        self.created = []
        configs = get_scenario_configs(scenario)
        assoc: Dict[str, int] = {}
        start = datetime.now().isoformat()
        ok, err, verified = True, None, None
        self.log.info(f"Starting scenario {scenario.value}", scenario)
        try:
            # 1. RNTIs first (they come from MAC reports, independent of slices)
            rntis = self.wait_for_ues(2)
            for r in rntis:
                self.log.ue_discovered(UEInfo(r), scenario)

            # 2. ADD both slices in a single control message
            if not self.slice_add(configs):
                raise RuntimeError("ADD failed (no CONTROL ACK / error)")
            self.created = [c.slice_id for c in configs]
            time.sleep(0.5)

            # 3. ASSOC: lowest RNTI -> slice 1, next -> slice 2 (deterministic)
            pairs = [(rntis[i], configs[i].slice_id) for i in range(2)]
            if not self.slice_assoc(pairs):
                raise RuntimeError("ASSOC failed")
            for r, sid in pairs:
                assoc[r] = sid
                self.log.ue_associated(r, sid, scenario)

            # 4. Verify reported state vs request (informational, JSON is flaky)
            time.sleep(SETTLE_AFTER_CONTROL_S)
            verified = self.verify_state(configs, assoc)

            # 5. Hold the configuration for the measurement window
            self.log.info(">>> START IPERF3 NOW (config active for "
                          f"{self.duration}s) <<<", scenario)
            self.log.info(f"  server: sudo ip netns exec ue1 iperf3 -s", scenario)
            self.log.info(f"  client: sudo ip netns exec ue2 iperf3 -c {UE1_IP} -t {self.duration}", scenario)
            time.sleep(self.duration)

            # 6. Cleanup: DEL both slices then RESET
            self._cleanup()
        except Exception as e:
            ok, err = False, str(e)
            self.log.error(f"Scenario {scenario.value} failed: {e}", scenario, e)
            self._cleanup()

        res = ScenarioResult(scenario, start, datetime.now().isoformat(), configs, assoc,
                             [], [], self.duration, ok, err, verified)
        self.results.append(res)
        return res

    def _cleanup(self):
        try:
            if self.created:
                self.slice_del(list(reversed(self.created)))
                self.created = []
            self.slice_reset()
            time.sleep(SETTLE_AFTER_CONTROL_S)
        except Exception as e:
            self.log.error("Cleanup failed", self.sc, e)

    def run_all(self):
        self.running = True
        self.log.info("=" * 50 + " SLICE ISOLATION EXPERIMENTS " + "=" * 50)
        for i, sc in enumerate(self.scenarios):
            if not self.running:
                break
            self.run_scenario(sc)
            if i < len(self.scenarios) - 1:
                time.sleep(5)
        self.running = False
        self.summary()

    def summary(self):
        self.log.info("=" * 30 + " SUMMARY " + "=" * 30)
        for r in self.results:
            st = "SUCCESS" if r.success else "FAILED"
            v = {True: "state matches", False: "state MISMATCH (see STATE_VERIFIED)",
                 None: "state not checked"}[r.state_verified]
            self.log.info(f"  {r.scenario.value}: {st} | {v}")
            if r.error_message:
                self.log.info(f"    error: {r.error_message}")
            self.log.info(f"    slices: {[f'{c.slice_id}({c.label})' for c in r.slice_configs]}")
            self.log.info(f"    UE associations: {r.ue_associations}")
        self.log.info(f"Logs: {self.log.log_dir}")


# =============================================================================
# ENTRY POINT
# =============================================================================
def load_sdk():
    """Import the REAL SDK or fail loudly (no silent mock)."""
    for p in ("/opt/flexric/python", "/usr/local/lib/python3/dist-packages",
              str(Path.home() / "ieeetestbed/oran/flexric/build-python/src/xApp/swig")):
        if p not in sys.path:
            sys.path.append(p)
    try:
        import xapp_sdk as ric
        return ric
    except ImportError as e:
        raise SystemExit(f"FATAL: cannot import xapp_sdk ({e}). Run from the FlexRIC "
                         f"build-python/examples/xApp/python3 directory, like xapp_slice_moni_ctrl.py.")


def probe(ric):
    """Print the SDK names this xApp relies on, to validate them on your build."""
    needed = ["init", "conn_e2_nodes", "report_mac_sm", "report_slice_sm", "control_slice_sm",
              "mac_cb", "slice_cb", "Interval_ms_10", "Interval_ms_1000",
              "slice_ctrl_msg_t", "fr_slice_t", "slice_array", "ue_slice_assoc_t",
              "ue_slice_assoc_array", "del_dl_array",
              "SLICE_CTRL_SM_V0_ADD", "SLICE_CTRL_SM_V0_DEL", "SLICE_CTRL_SM_V0_UE_SLICE_ASSOC",
              "SLICE_ALG_SM_V0_STATIC", "SLICE_ALG_SM_V0_NVS",
              "SLICE_SM_NVS_V0_RATE", "SLICE_SM_NVS_V0_CAPACITY"]
    for n in needed:
        print(f"  {'OK     ' if hasattr(ric, n) else 'MISSING'} ric.{n}")
    print("\nAll SLICE*/slice* names exposed by the SDK:")
    for n in sorted(x for x in dir(ric) if "slice" in x.lower()):
        print("  ", n)


def main():
    ap = argparse.ArgumentParser(description="Slice Isolation xApp")
    ap.add_argument("--duration", type=int, default=DEFAULT_MEASUREMENT_DURATION)
    ap.add_argument("--scenarios", nargs="+", choices=[s.value for s in Scenario],
                    default=[s.value for s in Scenario])
    ap.add_argument("--log-dir", default="/tmp/xapp_logs")
    ap.add_argument("--probe", action="store_true", help="check SDK names and exit")
    args, rest = ap.parse_known_args()   # rest (-c, -p ...) goes to ric.init()

    ric = load_sdk()
    if args.probe:
        probe(ric)
        return

    logger = LoggingManager(args.log_dir)
    adapter = FlexRICAdapter(ric, logger, [sys.argv[0]] + rest)
    logger.info(f"E2 node connected (id={adapter.node_id}); scenarios={args.scenarios}")
    adapter.subscribe()
    time.sleep(2)  # let first indications arrive

    xapp = SliceIsolationXApp(adapter, logger, args.duration, [Scenario(s) for s in args.scenarios])
    try:
        xapp.run_all()
    except KeyboardInterrupt:
        logger.info("Interrupted: cleaning up")
        xapp.running = False
        xapp._cleanup()
    finally:
        adapter.shutdown()


if __name__ == "__main__":
    main()
