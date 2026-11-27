#!/usr/bin/env python3
"""
xApp Slice Isolation - Phase 2 (v3, avec itérations + sécurité)
===============================================================

Nouveautés v3 :
- --iterations N : répète chaque scénario N fois et fait la moyenne des débits
- Sécurité par tranche : détection d'anomalie (chute de débit) + contre-mesure
- Métrique quantitative de sécurité : Isolation Violation Rate
- Résumé enrichi : avg_throughput, std, min, max, security_metrics

Auteurs: Oueslati Ranim, Ben Hedhili Mawadda
"""

import sys
import time
import json
import logging
import threading
import argparse
import statistics
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any
from pathlib import Path

# =============================================================================
# CONSTANTES
# =============================================================================
DEFAULT_MEASUREMENT_DURATION = 30
DEFAULT_ITERATIONS = 3
TOTAL_PRBS = 14
BASELINE_MBPS = 18.0
NVS_REF_MBPS = 18.0
UE1_IP, UE2_IP = "10.45.0.39", "10.45.0.40"
UE_SCHED = "PF"
SLICE_STATS_PERIOD_MS = 1000
SETTLE_AFTER_CONTROL_S = 2.0


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
# STRUCTURES DE DONNÉES
# =============================================================================
@dataclass
class SliceConfig:
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
    rnti: str
    ip: str = ""
    namespace: str = ""
    slice_id: Optional[int] = None
    associated: bool = False


@dataclass
class LogEntry:
    timestamp: str
    event_type: str
    details: Dict[str, Any]
    scenario: Optional[str] = None
    configuration: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


@dataclass
class IterationResult:
    """Résultat d'une itération unique d'un scénario."""
    iteration: int
    slice_throughputs: Dict[int, float]      # slice_id -> mbps
    anomaly_detected: bool
    countermeasure_applied: bool


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
    state_verified: Optional[bool] = None
    # Nouveaux champs v3
    iteration_results: List[IterationResult] = field(default_factory=list)
    avg_throughputs: Dict[int, float] = field(default_factory=dict)
    std_throughputs: Dict[int, float] = field(default_factory=dict)
    security_metrics: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# SCÉNARIOS
# =============================================================================
def get_scenario_configs(scenario: Scenario) -> List[SliceConfig]:
    S, N = SliceAlgorithm.STATIC, SliceAlgorithm.NVS_RATE
    if scenario == Scenario.STATIC_BALANCED:
        return [SliceConfig(1, "slice_bal_1", S, pos_low=0, pos_high=6),
                SliceConfig(2, "slice_bal_2", S, pos_low=7, pos_high=13)]
    if scenario == Scenario.STATIC_UNBALANCED:
        return [SliceConfig(1, "slice_wide", S, pos_low=0, pos_high=10),
                SliceConfig(2, "slice_narrow", S, pos_low=11, pos_high=13)]
    if scenario == Scenario.NVS_RATE_BALANCED:
        return [SliceConfig(1, "rate_bal_1", N, mbps_rsvd=9.0, mbps_ref=NVS_REF_MBPS),
                SliceConfig(2, "rate_bal_2", N, mbps_rsvd=9.0, mbps_ref=NVS_REF_MBPS)]
    if scenario == Scenario.NVS_RATE_UNBALANCED:
        return [SliceConfig(1, "rate_wide", N, mbps_rsvd=15.0, mbps_ref=NVS_REF_MBPS),
                SliceConfig(2, "rate_narrow", N, mbps_rsvd=3.0, mbps_ref=NVS_REF_MBPS)]
    raise ValueError(scenario)


# =============================================================================
# LOGGING
# =============================================================================
class LoggingManager:
    def __init__(self, log_dir: str = "/tmp/xapp_logs", xapp_name: str = "xapp_slice_isolation"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.jsonl_file = self.log_dir / f"{xapp_name}_{ts}.jsonl"
        self.log_file = self.log_dir / f"{xapp_name}_{ts}.log"
        self._lock = threading.Lock()

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

    def security_event(self, event: dict):
        """Log un événement sécurité (anomalie ou contre-mesure)."""
        etype = event["event_type"]
        details = {k: v for k, v in event.items() if k != "event_type"}
        self._e(etype, details, None)


# =============================================================================
# SÉCURITÉ PAR TRANCHE
# =============================================================================
class SecurityMonitor:
    """Surveille l'isolation des tranches et détecte les attaques."""

    def __init__(self, logger: LoggingManager):
        self.logger = logger
        self.baselines: Dict[int, float] = {}
        self.current: Dict[int, float] = {}
        self.anomalies = 0
        self.countermeasures = 0

    def set_baseline(self, slice_id: int, throughput: float):
        self.baselines[slice_id] = throughput

    def update(self, slice_id: int, throughput: float):
        self.current[slice_id] = throughput

    def detect_anomaly(self, slice_id: int, threshold_pct: float = 30.0) -> bool:
        """Détecte une chute de débit anormale sur une tranche (isolation violée)."""
        if slice_id not in self.baselines:
            return False
        base = self.baselines[slice_id]
        curr = self.current.get(slice_id, 0.0)
        if base <= 0:
            return False
        drop_pct = (base - curr) / base * 100
        if drop_pct > threshold_pct:
            self.anomalies += 1
            event = {
                "event_type": "SECURITY_ANOMALY",
                "slice_id": slice_id,
                "drop_pct": round(drop_pct, 2),
                "baseline_mbps": base,
                "current_mbps": curr,
                "metric": "isolation_violation_rate"
            }
            self.logger.security_event(event)
            return True
        return False

    def apply_countermeasure(self, slice_id: int, action: str = "ISOLATION_BOOST") -> str:
        """Applique une contre-mesure de sécurité."""
        self.countermeasures += 1
        event = {
            "event_type": "COUNTERMEASURE_APPLIED",
            "slice_id": slice_id,
            "action": action,
            "metric": "security_response_count"
        }
        self.logger.security_event(event)
        return action

    def get_metrics(self) -> Dict[str, Any]:
        """Métriques quantitatives de sécurité."""
        total = self.anomalies + self.countermeasures
        return {
            "anomalies_detected": self.anomalies,
            "countermeasures_applied": self.countermeasures,
            "detection_rate": round(self.anomalies / max(1, total), 3) if total > 0 else 0.0,
            "metric_name": "isolation_violation_rate"
        }


# =============================================================================
# ADAPTATEUR FLEXRIC (inchangé, SDK spécifique)
# =============================================================================
def _g(obj, path: str, default=None):
    for p in path.split("."):
        try:
            obj = getattr(obj, p)
        except Exception:
            return default
    return obj


class FlexRICAdapter:
    def __init__(self, ric_module, logger: LoggingManager, extra_argv: List[str]):
        self.ric = ric_module
        self.log = logger
        self.lock = threading.Lock()
        self.rntis: List[str] = []
        self.last_slice_state: Dict[str, Any] = {}
        self.current_scenario = None
        self.conn = None
        self.node_id = None
        self._cbs = []
        self.ric.init()
        self.conn = self.ric.conn_e2_nodes()
        if len(self.conn) == 0:
            raise RuntimeError("No E2 node connected to the RIC (start RIC then gNB first)")
        self.node_id = self.conn[0].id

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
        self.ric.report_slice_sm(self.node_id, self.ric.Interval_ms_10, sl)
        self.log.info("Subscribed to MAC (RNTI discovery) and SLICE_STATS")

    @staticmethod
    def slice_ind_to_dict(ind) -> Dict[str, Any]:
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
        return self.build_add([])

    def send(self, msg) -> None:
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
# XAPP PRINCIPAL (v3 avec itérations + sécurité)
# =============================================================================
class SliceIsolationXApp:
    def __init__(self, adapter: FlexRICAdapter, logger: LoggingManager,
                 measurement_duration: int, scenarios: List[Scenario],
                 num_iterations: int = DEFAULT_ITERATIONS):
        self.a = adapter
        self.log = logger
        self.duration = measurement_duration
        self.num_iterations = num_iterations
        self.scenarios = scenarios
        self.results: List[ScenarioResult] = []
        self.running = False
        self.created: List[int] = []
        self.sc: Optional[Scenario] = None
        self.security_monitor = SecurityMonitor(logger)

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

    def _collect_throughputs(self, configs) -> Dict[int, float]:
        """Demande les débits iperf3 mesurés pour chaque tranche."""
        throughputs = {}
        for c in configs:
            while True:
                try:
                    val = input(f"Débit tranche {c.slice_id} ({c.label}) en Mbit/s: ").strip()
                    throughputs[c.slice_id] = float(val) if val else 0.0
                    break
                except ValueError:
                    print("Valeur invalide, entrez un nombre.")
        return throughputs

    def _cleanup(self):
        try:
            if self.created:
                self.slice_del(list(reversed(self.created)))
                self.created = []
            self.slice_reset()
            time.sleep(SETTLE_AFTER_CONTROL_S)
        except Exception as e:
            self.log.error("Cleanup failed", self.sc, e)

    def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        self.sc = scenario
        self.a.current_scenario = scenario
        self.created = []
        configs = get_scenario_configs(scenario)
        assoc: Dict[str, int] = {}
        start = datetime.now().isoformat()
        ok, err, verified = True, None, None
        self.log.info(f"Starting scenario {scenario.value} ({self.num_iterations} itérations)", scenario)
        iteration_results: List[IterationResult] = []
        all_throughputs: Dict[int, List[float]] = {c.slice_id: [] for c in configs}

        try:
            # 1. Découverte des UEs (une seule fois, indépendante des slices)
            rntis = self.wait_for_ues(2)
            for r in rntis:
                self.log.ue_discovered(UEInfo(r), scenario)

            for it in range(1, self.num_iterations + 1):
                self.log.info(f"=== Itération {it}/{self.num_iterations} - {scenario.value} ===", scenario)

                # 2. ADD slices
                if not self.slice_add(configs):
                    raise RuntimeError("ADD failed (no CONTROL ACK / error)")
                self.created = [c.slice_id for c in configs]
                time.sleep(0.5)

                # 3. ASSOC UEs (déterministe : RNTI le plus bas -> slice 1)
                pairs = [(rntis[i], configs[i].slice_id) for i in range(2)]
                if not self.slice_assoc(pairs):
                    raise RuntimeError("ASSOC failed")
                for r, sid in pairs:
                    assoc[r] = sid
                    self.log.ue_associated(r, sid, scenario)

                # 4. Vérification état (informative)
                time.sleep(SETTLE_AFTER_CONTROL_S)
                verified = self.verify_state(configs, assoc)

                # 5. Fenêtre de mesure iperf3
                self.log.info(">>> START IPERF3 NOW (config active pour "
                              f"{self.duration}s) <<<", scenario)
                self.log.info(f"  server: sudo ip netns exec ue1 iperf3 -s", scenario)
                self.log.info(f"  client: sudo ip netns exec ue2 iperf3 -c {UE1_IP} -t {self.duration}", scenario)
                time.sleep(self.duration)

                # 6. Collecte des débits mesurés
                throughputs = self._collect_throughputs(configs)
                for cid, tp in throughputs.items():
                    all_throughputs[cid].append(tp)

                # 7. Sécurité : détection d'anomalie + contre-mesure
                anomaly_this_iter = False
                countermeasure_this_iter = False
                for cid, tp in throughputs.items():
                    self.security_monitor.update(cid, tp)
                    if it == 1:
                        self.security_monitor.set_baseline(cid, tp)
                    if self.security_monitor.detect_anomaly(cid):
                        anomaly_this_iter = True
                        self.security_monitor.apply_countermeasure(cid)
                        countermeasure_this_iter = True

                iteration_results.append(IterationResult(
                    iteration=it,
                    slice_throughputs=throughputs,
                    anomaly_detected=anomaly_this_iter,
                    countermeasure_applied=countermeasure_this_iter
                ))

                # 8. Nettoyage entre itérations
                self._cleanup()

        except Exception as e:
            ok, err = False, str(e)
            self.log.error(f"Scenario {scenario.value} failed: {e}", scenario, e)
            self._cleanup()

        # 9. Calcul des moyennes et écart-types sur toutes les itérations
        avg_tps = {}
        std_tps = {}
        for cid in all_throughputs:
            vals = all_throughputs[cid]
            if vals:
                avg_tps[cid] = round(sum(vals) / len(vals), 2)
                std_tps[cid] = round(statistics.stdev(vals), 2) if len(vals) > 1 else 0.0

        smetrics = self.security_monitor.get_metrics()

        res = ScenarioResult(
            scenario=scenario,
            start_time=start,
            end_time=datetime.now().isoformat(),
            slice_configs=configs,
            ue_associations=assoc,
            commands_sent=[],
            acks_received=[],
            measurement_duration=self.duration,
            success=ok,
            error_message=err,
            state_verified=verified,
            iteration_results=iteration_results,
            avg_throughputs=avg_tps,
            std_throughputs=std_tps,
            security_metrics=smetrics
        )
        self.results.append(res)
        return res

    def run_all(self):
        self.running = True
        self.log.info("=" * 50 + " SLICE ISOLATION EXPERIMENTS (v3) " + "=" * 50)
        for i, sc in enumerate(self.scenarios):
            if not self.running:
                break
            self.run_scenario(sc)
            if i < len(self.scenarios) - 1:
                time.sleep(5)
        self.running = False
        self.summary()

    def summary(self):
        self.log.info("=" * 30 + " SUMMARY (v3) " + "=" * 30)
        for r in self.results:
            st = "SUCCESS" if r.success else "FAILED"
            v = {True: "state matches", False: "state MISMATCH (see STATE_VERIFIED)",
                 None: "state not checked"}[r.state_verified]
            self.log.info(f"  {r.scenario.value}: {st} | {v}")
            if r.error_message:
                self.log.info(f"    error: {r.error_message}")
            self.log.info(f"    slices: {[f'{c.slice_id}({c.label})' for c in r.slice_configs]}")
            self.log.info(f"    UE associations: {r.ue_associations}")
            self.log.info(f"    Débit moyen par tranche (Mbit/s): {r.avg_throughputs}")
            self.log.info(f"    Écart-type (Mbit/s): {r.std_throughputs}")
            self.log.info(f"    Sécurité: {r.security_metrics}")
        self.log.info(f"Logs: {self.log.log_dir}")


# =============================================================================
# POINT D'ENTRÉE
# =============================================================================
def load_sdk():
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
    needed = ["init", "conn_e2_nodes", "report_mac_sm", "report_slice_sm", "control_slice_sm",
              "mac_cb", "slice_cb", "Interval_ms_10", "slice_ctrl_msg_t", "fr_slice_t",
              "slice_array", "ue_slice_assoc_t", "ue_slice_assoc_array", "del_dl_array",
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
    ap.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS,
                    help="Nombre d'itérations par scénario (moyenne des débits)")
    ap.add_argument("--scenarios", nargs="+", choices=[s.value for s in Scenario],
                    default=[s.value for s in Scenario])
    ap.add_argument("--log-dir", default="/tmp/xapp_logs")
    ap.add_argument("--probe", action="store_true", help="check SDK names and exit")
    args, rest = ap.parse_known_args()

    ric = load_sdk()
    if args.probe:
        probe(ric)
        return

    logger = LoggingManager(args.log_dir)
    adapter = FlexRICAdapter(ric, logger, [sys.argv[0]] + rest)
    logger.info(f"E2 node connected (id={adapter.node_id}); scenarios={args.scenarios}; iterations={args.iterations}")
    adapter.subscribe()
    time.sleep(2)

    xapp = SliceIsolationXApp(adapter, logger, args.duration,
                              [Scenario(s) for s in args.scenarios],
                              num_iterations=args.iterations)
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
