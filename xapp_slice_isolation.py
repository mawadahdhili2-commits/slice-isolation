#!/usr/bin/env python3
"""
xApp Slice Isolation - Phase 2 Implementation
==============================================

This xApp implements slice resource partitioning and isolation measurement
for the O-RAN Near-RT RIC (FlexRIC) platform.

Based on the reference xapp_slice_moni_ctrl.py from the testbed, this xApp:
- Creates two slices with STATIC algorithm (balanced and unbalanced)
- Associates each UE to a distinct slice using RNTI from KPM reports
- Maintains each configuration for a fixed measurement duration
- Logs all commands, ACKs, and observed states with timestamps
- Extends to NVS-RATE algorithm for comparison

Author: Oueslati Ranim, Ben Hedhili Mawadda
Project: Slice Isolation in NextG O-RAN Networks
Date: November 2026
"""

import sys
import time
import json
import logging
import threading
import signal
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from pathlib import Path

# =============================================================================
# FLEXRIC PYTHON SDK IMPORTS
# =============================================================================
# The FlexRIC Python SDK is installed at /opt/flexric/python
# It provides: xapp_sdk, e2ap, e2sm_kpm, e2sm_rc, e2sm_slice
#
# If running outside the testbed, these imports will fail.
# The code is structured to be testable with mocks.

try:
    sys.path.append('/opt/flexric/python')
    from xapp_sdk import XApp, E2Client
    from e2ap import E2AP
    from e2sm_kpm import E2SM_KPM
    from e2sm_rc import E2SM_RC
    from e2sm_slice import E2SM_Slice
    FLEXRIC_AVAILABLE = True
except ImportError:
    FLEXRIC_AVAILABLE = False
    # Define mock classes for development/testing outside testbed
    class XApp:
        def __init__(self, name): self.name = name
        def run(self): pass
        def stop(self): pass
    class E2Client:
        def control_request(self, msg): return {"status": "success"}
        def subscribe(self, *args, **kwargs): pass

# =============================================================================
# CONFIGURATION AND CONSTANTS
# =============================================================================

# Default measurement duration per configuration (seconds)
DEFAULT_MEASUREMENT_DURATION = 30

# Total PRBs available in the testbed cell (from testbed report: 0-13 = 14 PRBs)
TOTAL_PRBS = 14

# Baseline throughput from Phase 1 (Mbit/s)
BASELINE_THROUGHPUT_SENDER = 18.6
BASELINE_THROUGHPUT_RECEIVER = 18.0

# UE IPs for reference (from testbed)
UE1_IP = "10.45.0.39"
UE2_IP = "10.45.0.40"

# Slice algorithms supported
class SliceAlgorithm(Enum):
    STATIC = "STATIC"
    NVS_RATE = "NVS-RATE"
    NVS_CAPACITY = "NVS-CAPACITY"
    NONE = "NONE"

# Slice operations
class SliceOperation(Enum):
    ADD = "ADD"
    ASSOC = "ASSOC"
    DEL = "DEL"
    RESET = "RESET"

# Configuration scenarios
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
    """Configuration for a single slice."""
    slice_id: int
    label: str
    algorithm: SliceAlgorithm
    # STATIC parameters
    pos_low: Optional[int] = None
    pos_high: Optional[int] = None
    # NVS-RATE parameters
    mbps_rsvd: Optional[float] = None
    # NVS-CAPACITY parameters
    percentage: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['algorithm'] = self.algorithm.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'SliceConfig':
        d = d.copy()
        d['algorithm'] = SliceAlgorithm(d['algorithm'])
        return cls(**d)


@dataclass
class UEInfo:
    """Information about a discovered UE."""
    rnti: str
    ip: str
    namespace: str
    slice_id: Optional[int] = None
    associated: bool = False


@dataclass
class LogEntry:
    """Structured log entry for all xApp activities."""
    timestamp: str
    event_type: str  # COMMAND_SENT, ACK_RECEIVED, STATE_OBSERVED, ERROR, INFO
    details: Dict[str, Any]
    scenario: Optional[str] = None
    configuration: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class ScenarioResult:
    """Results for a completed scenario."""
    scenario: Scenario
    start_time: str
    end_time: str
    slice_configs: List[SliceConfig]
    ue_associations: Dict[str, int]  # rnti -> slice_id
    commands_sent: List[LogEntry]
    acks_received: List[LogEntry]
    measurement_duration: int
    success: bool
    error_message: Optional[str] = None


# =============================================================================
# SCENARIO DEFINITIONS
# =============================================================================

def get_scenario_configs(scenario: Scenario) -> List[SliceConfig]:
    """
    Returns the slice configurations for a given scenario.

    Based on testbed: 14 PRBs total (indices 0-13)
    Baseline throughput: ~18.6 Mbit/s
    """
    configs = []

    if scenario == Scenario.STATIC_BALANCED:
        # Balanced: 7 PRBs each (0-6 and 7-13)
        configs = [
            SliceConfig(
                slice_id=1,
                label="slice_balanced_1",
                algorithm=SliceAlgorithm.STATIC,
                pos_low=0,
                pos_high=6
            ),
            SliceConfig(
                slice_id=2,
                label="slice_balanced_2",
                algorithm=SliceAlgorithm.STATIC,
                pos_low=7,
                pos_high=13
            )
        ]

    elif scenario == Scenario.STATIC_UNBALANCED:
        # Unbalanced: 11 PRBs (wide) + 3 PRBs (narrow)
        configs = [
            SliceConfig(
                slice_id=1,
                label="slice_wide",
                algorithm=SliceAlgorithm.STATIC,
                pos_low=0,
                pos_high=10
            ),
            SliceConfig(
                slice_id=2,
                label="slice_narrow",
                algorithm=SliceAlgorithm.STATIC,
                pos_low=11,
                pos_high=13
            )
        ]

    elif scenario == Scenario.NVS_RATE_BALANCED:
        # Balanced: ~9 Mbit/s each (half of ~18.6 baseline)
        configs = [
            SliceConfig(
                slice_id=1,
                label="slice_rate_balanced_1",
                algorithm=SliceAlgorithm.NVS_RATE,
                mbps_rsvd=9.0
            ),
            SliceConfig(
                slice_id=2,
                label="slice_rate_balanced_2",
                algorithm=SliceAlgorithm.NVS_RATE,
                mbps_rsvd=9.0
            )
        ]

    elif scenario == Scenario.NVS_RATE_UNBALANCED:
        # Unbalanced: 15 Mbit/s (wide) + 3 Mbit/s (narrow)
        configs = [
            SliceConfig(
                slice_id=1,
                label="slice_rate_wide",
                algorithm=SliceAlgorithm.NVS_RATE,
                mbps_rsvd=15.0
            ),
            SliceConfig(
                slice_id=2,
                label="slice_rate_narrow",
                algorithm=SliceAlgorithm.NVS_RATE,
                mbps_rsvd=3.0
            )
        ]

    return configs


# =============================================================================
# LOGGING MANAGER
# =============================================================================

class LoggingManager:
    """
    Manages structured logging for the xApp.
    Writes to both console (for real-time monitoring) and JSONL file (for analysis).
    """

    def __init__(self, log_dir: str = "/tmp/xapp_logs", xapp_name: str = "slice_isolation"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.xapp_name = xapp_name

        # JSONL file for structured analysis
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.jsonl_file = self.log_dir / f"{xapp_name}_{timestamp}.jsonl"

        # Human-readable log file
        self.log_file = self.log_dir / f"{xapp_name}_{timestamp}.log"

        # Setup Python logging
        self.logger = logging.getLogger(xapp_name)
        self.logger.setLevel(logging.DEBUG)

        # File handler
        fh = logging.FileHandler(self.log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))

        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%H:%M:%S'
        ))

        self.logger.addHandler(fh)
        self.logger.addHandler(ch)

        self.logger.info(f"Logging initialized. JSONL: {self.jsonl_file}")
        self.logger.info(f"Human-readable log: {self.log_file}")

    def log(self, entry: LogEntry):
        """Write a log entry to both JSONL and human-readable log."""
        # JSONL for machine processing
        with open(self.jsonl_file, 'a') as f:
            f.write(entry.to_json() + '\n')

        # Human-readable
        level = logging.ERROR if entry.event_type == "ERROR" else logging.INFO
        msg = f"[{entry.event_type}] {entry.details}"
        if entry.scenario:
            msg = f"[{entry.scenario.value if hasattr(entry.scenario, 'value') else entry.scenario}] {msg}"
        self.logger.log(level, msg)

    def info(self, message: str, scenario: Optional[Scenario] = None):
        self.log(LogEntry(
            timestamp=datetime.now().isoformat(),
            event_type="INFO",
            details={"message": message},
            scenario=scenario.value if scenario else None
        ))

    def error(self, message: str, scenario: Optional[Scenario] = None, exc: Optional[Exception] = None):
        details = {"message": message}
        if exc:
            details["exception"] = str(exc)
        self.log(LogEntry(
            timestamp=datetime.now().isoformat(),
            event_type="ERROR",
            details=details,
            scenario=scenario.value if scenario else None
        ))

    def command_sent(self, operation: SliceOperation, slice_config: SliceConfig,
                     scenario: Scenario, extra: Dict = None):
        details = {
            "operation": operation.value,
            "slice_config": slice_config.to_dict()
        }
        if extra:
            details.update(extra)
        self.log(LogEntry(
            timestamp=datetime.now().isoformat(),
            event_type="COMMAND_SENT",
            details=details,
            scenario=scenario.value
        ))

    def ack_received(self, operation: SliceOperation, slice_id: int,
                     success: bool, scenario: Scenario, extra: Dict = None):
        details = {
            "operation": operation.value,
            "slice_id": slice_id,
            "success": success
        }
        if extra:
            details.update(extra)
        self.log(LogEntry(
            timestamp=datetime.now().isoformat(),
            event_type="ACK_RECEIVED",
            details=details,
            scenario=scenario.value
        ))

    def state_observed(self, state: Dict, scenario: Scenario):
        self.log(LogEntry(
            timestamp=datetime.now().isoformat(),
            event_type="STATE_OBSERVED",
            details={"state": state},
            scenario=scenario.value
        ))

    def ue_discovered(self, ue: UEInfo, scenario: Scenario):
        self.log(LogEntry(
            timestamp=datetime.now().isoformat(),
            event_type="UE_DISCOVERED",
            details={"rnti": ue.rnti, "ip": ue.ip, "namespace": ue.namespace},
            scenario=scenario.value
        ))

    def ue_associated(self, rnti: str, slice_id: int, scenario: Scenario):
        self.log(LogEntry(
            timestamp=datetime.now().isoformat(),
            event_type="UE_ASSOCIATED",
            details={"rnti": rnti, "slice_id": slice_id},
            scenario=scenario.value
        ))


# =============================================================================
# E2 MESSAGE BUILDERS
# =============================================================================

class E2MessageBuilder:
    """
    Builds E2 control messages for slice operations.

    Based on the E2SM-RC (Radio Control) and E2SM-Slice service models
    as observed in the testbed xapp_slice_moni_ctrl.py behavior.
    """

    @staticmethod
    def build_slice_add(slice_config: SliceConfig) -> Dict[str, Any]:
        """Build ADD slice control message."""
        msg = {
            "operation": "ADD",
            "slice_id": slice_config.slice_id,
            "label": slice_config.label,
            "algorithm": slice_config.algorithm.value
        }

        if slice_config.algorithm == SliceAlgorithm.STATIC:
            msg["pos_low"] = slice_config.pos_low
            msg["pos_high"] = slice_config.pos_high
        elif slice_config.algorithm == SliceAlgorithm.NVS_RATE:
            msg["mbps_rsvd"] = slice_config.mbps_rsvd
        elif slice_config.algorithm == SliceAlgorithm.NVS_CAPACITY:
            msg["percentage"] = slice_config.percentage

        return msg

    @staticmethod
    def build_slice_assoc(rnti: str, slice_id: int) -> Dict[str, Any]:
        """Build ASSOC (associate UE to slice) control message."""
        return {
            "operation": "ASSOC",
            "slice_id": slice_id,
            "rnti": rnti
        }

    @staticmethod
    def build_slice_del(slice_id: int) -> Dict[str, Any]:
        """Build DEL (delete slice) control message."""
        return {
            "operation": "DEL",
            "slice_id": slice_id
        }

    @staticmethod
    def build_slice_reset() -> Dict[str, Any]:
        """Build RESET (reset all slices) control message."""
        return {
            "operation": "RESET",
            "algorithm": "NONE"
        }


# =============================================================================
# MAIN XAPP CLASS
# =============================================================================

class SliceIsolationXApp(XApp):
    """
    Main xApp for Slice Isolation experiments.

    This xApp:
    1. Registers with the Near-RT RIC
    2. Subscribes to KPM (MAC/RLC/PDCP/GTP) for RNTI discovery
    3. Subscribes to SLICE_STATS for state monitoring
    4. Runs a sequence of scenarios (STATIC/NVS-RATE, balanced/unbalanced)
    5. For each scenario:
       - Creates two slices with the specified algorithm
       - Discovers UE RNTIs from KPM reports
       - Associates each UE to a distinct slice
       - Maintains configuration for measurement duration
       - Logs all commands, ACKs, and states with timestamps
       - Cleans up (DEL + RESET) before next scenario
    """

    def __init__(self,
                 name: str = "xapp_slice_isolation",
                 measurement_duration: int = DEFAULT_MEASUREMENT_DURATION,
                 scenarios: List[Scenario] = None,
                 log_dir: str = "/tmp/xapp_logs"):

        super().__init__(name)

        self.measurement_duration = measurement_duration
        self.scenarios = scenarios or [
            Scenario.STATIC_BALANCED,
            Scenario.STATIC_UNBALANCED,
            Scenario.NVS_RATE_BALANCED,
            Scenario.NVS_RATE_UNBALANCED
        ]
        self.log_dir = log_dir

        # Initialize logging
        self.logging = LoggingManager(log_dir, name)

        # E2 client for control requests
        self.e2_client = E2Client()

        # State tracking
        self.connected_e2_nodes: List[Dict] = []
        self.discovered_ues: Dict[str, UEInfo] = {}  # rnti -> UEInfo
        self.current_scenario: Optional[Scenario] = None
        self.current_slice_configs: List[SliceConfig] = []
        self.slice_ids_created: List[int] = []
        self.rnti_to_slice: Dict[str, int] = {}  # rnti -> slice_id
        self.scenario_results: List[ScenarioResult] = []

        # Synchronization
        self.ack_event = threading.Event()
        self.last_ack: Optional[Dict] = None
        self.ues_discovered_event = threading.Event()
        self.running = False
        self.scenario_complete = threading.Event()

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self.logging.info(f"xApp {name} initialized")
        self.logging.info(f"Scenarios to run: {[s.value for s in self.scenarios]}")
        self.logging.info(f"Measurement duration per scenario: {measurement_duration}s")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        self.logging.info(f"Received signal {signum}, initiating graceful shutdown...")
        self.stop()

    # -------------------------------------------------------------------------
    # XAPP LIFECYCLE CALLBACKS
    # -------------------------------------------------------------------------

    def on_e2_setup(self, e2_node: Dict):
        """
        Called when an E2 node (gNodeB) connects to the RIC.
        Subscribe to KPM and SLICE_STATS.
        """
        self.connected_e2_nodes.append(e2_node)
        self.logging.info(f"E2 node connected: {e2_node.get('name', 'unknown')}")
        self.logging.info(f"E2 node capabilities: {e2_node.get('ran_functions', [])}")

        # Subscribe to KPM for RNTI discovery (MAC, RLC, PDCP, GTP)
        self._subscribe_kpm(e2_node)

        # Subscribe to SLICE_STATS for slice state monitoring
        self._subscribe_slice_stats(e2_node)

    def _subscribe_kpm(self, e2_node: Dict):
        """Subscribe to KPM (Key Performance Measurement) reports."""
        # Find KPM RAN function (typically MAC, RLC, PDCP, GTP)
        kpm_functions = [rf for rf in e2_node.get('ran_functions', [])
                         if rf.get('id') in [1, 2, 3, 4]]  # MAC=1, RLC=2, PDCP=3, GTP=4

        for rf in kpm_functions:
            try:
                self.e2_client.subscribe(
                    e2_node_id=e2_node['id'],
                    ran_function_id=rf['id'],
                    event_trigger={"type": "periodic", "period_ms": 1000},
                    action={"type": "report", "format": "json"}
                )
                self.logging.info(f"Subscribed to KPM RAN function {rf['id']} ({rf.get('name', 'unknown')})")
            except Exception as e:
                self.logging.error(f"Failed to subscribe to KPM function {rf['id']}", exc=e)

    def _subscribe_slice_stats(self, e2_node: Dict):
        """Subscribe to SLICE_STATS reports."""
        slice_functions = [rf for rf in e2_node.get('ran_functions', [])
                           if 'slice' in rf.get('name', '').lower() or rf.get('id') == 5]

        for rf in slice_functions:
            try:
                self.e2_client.subscribe(
                    e2_node_id=e2_node['id'],
                    ran_function_id=rf['id'],
                    event_trigger={"type": "periodic", "period_ms": 1000},
                    action={"type": "report", "format": "json"}
                )
                self.logging.info(f"Subscribed to SLICE_STATS RAN function {rf['id']}")
            except Exception as e:
                self.logging.error(f"Failed to subscribe to SLICE_STATS function {rf['id']}", exc=e)

    def on_kpm_indication(self, indication: Dict):
        """
        Handle KPM indication (measurement report).
        Extract RNTI from MAC/RLC/PDCP reports.
        """
        # Extract RNTI from the indication
        # Format depends on E2SM-KPM report structure
        try:
            # The indication contains measurement records per UE
            # Each record has UE ID (RNTI) and metrics
            ue_records = indication.get('ue_records', [])
            for record in ue_records:
                rnti = record.get('rnti') or record.get('ue_id')
                if rnti and rnti not in self.discovered_ues:
                    # Determine UE IP/namespace from RNTI (testbed has 2 UEs)
                    ue_info = self._identify_ue(rnti)
                    self.discovered_ues[rnti] = ue_info
                    self.logging.ue_discovered(ue_info, self.current_scenario or Scenario.STATIC_BALANCED)

            # Signal that UEs have been discovered (need at least 2)
            if len(self.discovered_ues) >= 2:
                self.ues_discovered_event.set()

        except Exception as e:
            self.logging.error("Error processing KPM indication", exc=e)

    def _identify_ue(self, rnti: str) -> UEInfo:
        """Map RNTI to UE info (IP, namespace)."""
        # In testbed: ue1=10.45.0.39, ue2=10.45.0.40
        # We'll assign based on order of discovery
        ue_index = len(self.discovered_ues)
        if ue_index == 0:
            return UEInfo(rnti=rnti, ip=UE1_IP, namespace="ue1")
        else:
            return UEInfo(rnti=rnti, ip=UE2_IP, namespace="ue2")

    def on_slice_stats_indication(self, indication: Dict):
        """Handle SLICE_STATS indication (slice state report)."""
        # Note: Testbed showed this JSON is unreliable
        # We log it for reference but don't trust it for verification
        try:
            self.logging.state_observed(indication, self.current_scenario or Scenario.STATIC_BALANCED)
        except Exception as e:
            self.logging.error("Error processing SLICE_STATS indication", exc=e)

    def on_control_ack(self, ack: Dict):
        """
        Handle control acknowledgment from gNodeB.
        """
        self.last_ack = ack
        success = ack.get('status') == 'success'
        slice_id = ack.get('slice_id')
        operation = ack.get('operation')

        try:
            op_enum = SliceOperation(operation) if operation else None
        except ValueError:
            op_enum = None

        self.logging.ack_received(
            operation=op_enum or SliceOperation.ADD,
            slice_id=slice_id or 0,
            success=success,
            scenario=self.current_scenario or Scenario.STATIC_BALANCED,
            extra={"ack_details": ack}
        )

        # Signal ACK received
        self.ack_event.set()

    # -------------------------------------------------------------------------
    # SLICE CONTROL OPERATIONS
    # -------------------------------------------------------------------------

    def send_control(self, control_msg: Dict, timeout: float = 5.0) -> bool:
        """
        Send a control request and wait for ACK.
        Returns True if ACK received with success, False otherwise.
        """
        self.ack_event.clear()
        self.last_ack = None

        try:
            # Send control request via E2 client
            # In real FlexRIC: self.e2_client.control_request(control_msg)
            # For now, simulate the call
            self.e2_client.control_request(control_msg)

            # Wait for ACK
            ack_received = self.ack_event.wait(timeout=timeout)

            if not ack_received:
                self.logging.error(f"Timeout waiting for ACK for {control_msg.get('operation')}")
                return False

            if self.last_ack and self.last_ack.get('status') == 'success':
                return True
            else:
                self.logging.error(f"Control failed: {self.last_ack}")
                return False

        except Exception as e:
            self.logging.error(f"Error sending control: {e}", exc=e)
            return False

    def slice_add(self, slice_config: SliceConfig) -> bool:
        """Send ADD slice command."""
        msg = E2MessageBuilder.build_slice_add(slice_config)
        self.logging.command_sent(SliceOperation.ADD, slice_config, self.current_scenario)
        return self.send_control(msg)

    def slice_assoc(self, rnti: str, slice_id: int) -> bool:
        """Send ASSOC (associate UE to slice) command."""
        msg = E2MessageBuilder.build_slice_assoc(rnti, slice_id)
        self.logging.command_sent(SliceOperation.ASSOC,
                                  SliceConfig(slice_id=slice_id, label="", algorithm=SliceAlgorithm.STATIC),
                                  self.current_scenario,
                                  extra={"rnti": rnti})
        return self.send_control(msg)

    def slice_del(self, slice_id: int) -> bool:
        """Send DEL (delete slice) command."""
        msg = E2MessageBuilder.build_slice_del(slice_id)
        self.logging.command_sent(SliceOperation.DEL,
                                  SliceConfig(slice_id=slice_id, label="", algorithm=SliceAlgorithm.STATIC),
                                  self.current_scenario)
        return self.send_control(msg)

    def slice_reset(self) -> bool:
        """Send RESET command."""
        msg = E2MessageBuilder.build_slice_reset()
        self.logging.command_sent(SliceOperation.RESET,
                                  SliceConfig(slice_id=0, label="", algorithm=SliceAlgorithm.NONE),
                                  self.current_scenario)
        return self.send_control(msg)

    # -------------------------------------------------------------------------
    # SCENARIO EXECUTION
    # -------------------------------------------------------------------------

    def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        """
        Execute a complete scenario:
        1. Create two slices with specified algorithm
        2. Wait for UE discovery (RNTI from KPM)
        3. Associate each UE to a distinct slice
        4. Maintain configuration for measurement duration
        5. Clean up (DEL + RESET)
        """
        self.current_scenario = scenario
        self.slice_ids_created = []
        self.rnti_to_slice = {}
        self.ues_discovered_event.clear()

        start_time = datetime.now().isoformat()
        self.logging.info(f"Starting scenario: {scenario.value}")

        # Get slice configurations for this scenario
        slice_configs = get_scenario_configs(scenario)
        self.current_slice_configs = slice_configs

        commands_sent = []
        acks_received = []
        success = True
        error_message = None

        try:
            # Step 1: ADD both slices
            for slice_config in slice_configs:
                self.logging.info(f"Creating slice {slice_config.slice_id} ({slice_config.label})")
                if not self.slice_add(slice_config):
                    raise RuntimeError(f"Failed to create slice {slice_config.slice_id}")
                self.slice_ids_created.append(slice_config.slice_id)
                time.sleep(0.5)  # Small delay between commands

            # Step 2: Wait for UE discovery (RNTI from KPM reports)
            self.logging.info("Waiting for UE RNTI discovery from KPM reports...")
            discovered = self.ues_discovered_event.wait(timeout=10.0)
            if not discovered or len(self.discovered_ues) < 2:
                raise RuntimeError(f"Insufficient UEs discovered: {len(self.discovered_ues)}")

            self.logging.info(f"Discovered {len(self.discovered_ues)} UEs: {list(self.discovered_ues.keys())}")

            # Step 3: Associate each UE to a distinct slice
            # Sort UEs by RNTI for deterministic assignment
            sorted_ues = sorted(self.discovered_ues.items())

            for i, (rnti, ue_info) in enumerate(sorted_ues[:2]):  # Only first 2 UEs
                slice_id = slice_configs[i].slice_id
                self.logging.info(f"Associating UE {rnti} ({ue_info.namespace}) to slice {slice_id}")
                if not self.slice_assoc(rnti, slice_id):
                    raise RuntimeError(f"Failed to associate UE {rnti} to slice {slice_id}")
                self.rnti_to_slice[rnti] = slice_id
                ue_info.slice_id = slice_id
                ue_info.associated = True
                self.logging.ue_associated(rnti, slice_id, scenario)
                time.sleep(0.5)

            # Step 4: Maintain configuration for measurement duration
            self.logging.info(f"Configuration active. Maintaining for {self.measurement_duration}s for measurements...")
            self.logging.info(">>> START IPERF3 MEASUREMENTS NOW <<<")
            self.logging.info(f"  Server (ue1): sudo ip netns exec ue1 iperf3 -s")
            self.logging.info(f"  Client (ue2): sudo ip netns exec ue2 iperf3 -c {UE1_IP} -t {self.measurement_duration}")

            # Wait for measurement duration
            # In real deployment, this is when external iperf3 runs
            time.sleep(self.measurement_duration)

            self.logging.info("Measurement period complete.")

            # Step 5: Clean up - DEL slices then RESET
            self.logging.info("Cleaning up: deleting slices...")
            for slice_id in reversed(self.slice_ids_created):  # Delete in reverse order
                if not self.slice_del(slice_id):
                    self.logging.error(f"Failed to delete slice {slice_id} (continuing)")
                time.sleep(0.5)

            self.logging.info("Resetting slice configuration...")
            if not self.slice_reset():
                self.logging.error("Failed to reset (continuing)")

            # Wait a bit for reset to propagate
            time.sleep(2.0)

        except Exception as e:
            success = False
            error_message = str(e)
            self.logging.error(f"Scenario {scenario.value} failed: {e}", exc=e)
            # Attempt cleanup
            self._cleanup_on_error()

        end_time = datetime.now().isoformat()

        result = ScenarioResult(
            scenario=scenario,
            start_time=start_time,
            end_time=end_time,
            slice_configs=slice_configs,
            ue_associations=self.rnti_to_slice.copy(),
            commands_sent=[],  # Would be populated from logging
            acks_received=[],
            measurement_duration=self.measurement_duration,
            success=success,
            error_message=error_message
        )

        self.scenario_results.append(result)

        if success:
            self.logging.info(f"Scenario {scenario.value} completed successfully")
        else:
            self.logging.error(f"Scenario {scenario.value} failed: {error_message}")

        return result

    def _cleanup_on_error(self):
        """Attempt cleanup on error."""
        try:
            for slice_id in reversed(self.slice_ids_created):
                self.slice_del(slice_id)
                time.sleep(0.5)
            self.slice_reset()
            time.sleep(2.0)
        except Exception as e:
            self.logging.error(f"Cleanup failed: {e}", exc=e)

    def run_all_scenarios(self):
        """Run all configured scenarios in sequence."""
        self.running = True
        self.logging.info("=" * 60)
        self.logging.info("STARTING SLICE ISOLATION EXPERIMENTS")
        self.logging.info("=" * 60)

        for scenario in self.scenarios:
            if not self.running:
                break

            self.run_scenario(scenario)

            # Delay between scenarios
            if scenario != self.scenarios[-1]:
                self.logging.info("Waiting 5s before next scenario...")
                time.sleep(5.0)

        self.running = False
        self._print_summary()

    def _print_summary(self):
        """Print experiment summary."""
        self.logging.info("=" * 60)
        self.logging.info("EXPERIMENT SUMMARY")
        self.logging.info("=" * 60)

        for result in self.scenario_results:
            status = "SUCCESS" if result.success else "FAILED"
            self.logging.info(f"  {result.scenario.value}: {status}")
            if result.error_message:
                self.logging.info(f"    Error: {result.error_message}")
            self.logging.info(f"    Slices: {[f'{s.slice_id}({s.label})' for s in result.slice_configs]}")
            self.logging.info(f"    UE Associations: {result.ue_associations}")

        self.logging.info(f"Logs saved to: {self.logging.log_dir}")

    def stop(self):
        """Stop the xApp gracefully."""
        self.logging.info("Stopping xApp...")
        self.running = False
        # Attempt final cleanup
        if self.slice_ids_created:
            self._cleanup_on_error()
        super().stop()


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main():
    """Main entry point for the xApp."""
    import argparse

    parser = argparse.ArgumentParser(description="Slice Isolation xApp")
    parser.add_argument("--duration", type=int, default=DEFAULT_MEASUREMENT_DURATION,
                        help=f"Measurement duration per scenario (default: {DEFAULT_MEASUREMENT_DURATION}s)")
    parser.add_argument("--scenarios", nargs="+",
                        choices=[s.value for s in Scenario],
                        default=[s.value for s in [Scenario.STATIC_BALANCED,
                                                    Scenario.STATIC_UNBALANCED,
                                                    Scenario.NVS_RATE_BALANCED,
                                                    Scenario.NVS_RATE_UNBALANCED]],
                        help="Scenarios to run")
    parser.add_argument("--log-dir", type=str, default="/tmp/xapp_logs",
                        help="Log directory")
    parser.add_argument("--name", type=str, default="xapp_slice_isolation",
                        help="xApp name")

    args = parser.parse_args()

    # Parse scenarios
    scenarios = [Scenario(s) for s in args.scenarios]

    # Create and run xApp
    xapp = SliceIsolationXApp(
        name=args.name,
        measurement_duration=args.duration,
        scenarios=scenarios,
        log_dir=args.log_dir
    )

    try:
        # In real FlexRIC: xapp.run() starts the event loop
        # For now, run scenarios directly
        xapp.run_all_scenarios()
    except KeyboardInterrupt:
        xapp.logging.info("Interrupted by user")
    except Exception as e:
        xapp.logging.error(f"Fatal error: {e}", exc=e)
    finally:
        xapp.stop()


if __name__ == "__main__":
    main()