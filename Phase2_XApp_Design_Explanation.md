# Phase 2 — xApp Development: Design and Implementation Guide

## Slice Isolation in NextG O-RAN Networks

**Project:** Mini-projet de fin d'études — binôme — 25 heures  
**Team:** Oueslati Ranim, Ben Hedhili Mawadda  
**Supervisor:** Wiem Abderrahim  
**Date:** November 2026

---

## Table of Contents

1. [Overview](#1-overview)
2. [Design Principles](#2-design-principles)
3. [Architecture](#3-architecture)
4. [Core Components](#4-core-components)
5. [Scenario Definitions](#5-scenario-definitions)
6. [Message Flow](#6-message-flow)
7. [Logging and Verification Strategy](#7-logging-and-verification-strategy)
8. [Running on the Testbed](#8-running-on-the-testbed)
9. [Expected Outputs](#9-expected-outputs)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Overview

### 1.1 Objective

Develop an xApp that:
- Creates two slices using **STATIC** algorithm (balanced and unbalanced configurations)
- Associates each UE to a distinct slice using RNTI discovered from KPM reports
- Maintains each configuration for a fixed duration (sufficient for iperf3 measurements)
- Logs all commands, ACKs, and states with timestamps
- Extends to **NVS-RATE** algorithm for comparison

### 1.2 Input from Phase 1

From the testbed experiments (Compte_rendu_testbed_ORAN):
- ✅ FlexRIC Near-RT RIC with Python SDK available at `/opt/flexric/python`
- ✅ Reference xApp: `xapp_slice_moni_ctrl.py` demonstrates ADD/ASSOC/DEL/RESET
- ✅ KPM subscriptions (MAC, RLC, PDCP, GTP) work — use for RNTI discovery
- ✅ SLICE_STATS subscription works but **JSON state is unreliable** (do not trust for verification)
- ✅ Control path: all commands acknowledged
- ✅ Baseline throughput: 18.6/18.0 Mbit/s (no slicing)
- ✅ Testbed has 14 PRBs (indices 0-13), 2 UEs (ue1: 10.45.0.39, ue2: 10.45.0.40)

### 1.3 Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Two slices only** | Project requires exactly 2 slices for isolation comparison |
| **RNTI from KPM** | Testbed showed UE RNTI only available in KPM reports (MAC/RLC/PDCP) |
| **Own state tracking** | gNodeB SLICE_STATS JSON doesn't match commands — log locally |
| **Sequential scenarios** | Run one scenario at a time for clean measurements |
| **Measurement window** | 30s default (matches Phase 1 iperf3 duration) |
| **Cleanup between scenarios** | DEL + RESET ensures clean state for next scenario |

---

## 2. Design Principles

### 2.1 Separation of Concerns

The xApp is organized into distinct modules:

```
┌─────────────────────────────────────────────────────────────────┐
│                    SliceIsolationXApp                           │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  Lifecycle   │  │   Control    │  │  Monitoring  │          │
│  │  Management  │  │  Operations  │  │  (KPM/STATS) │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  Scenario    │  │   Logging    │  │  UE          │          │
│  │  Engine      │  │  Manager     │  │  Discovery   │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Reliability Patterns

1. **ACK Waiting**: Every control request waits for acknowledgment (5s timeout)
2. **Idempotent Cleanup**: DEL + RESET on error or between scenarios
3. **Structured Logging**: JSONL for analysis, human-readable for monitoring
4. **Graceful Shutdown**: Signal handlers for SIGINT/SIGTERM
5. **Deterministic UE Assignment**: Sort by RNTI for reproducible slice mapping

---

## 3. Architecture

### 3.1 Class Diagram

```
┌────────────────────────────────────────────────────────────────┐
│                        XApp (FlexRIC SDK)                      │
└────────────────────────────────┬───────────────────────────────┘
                                 │
                                 ▼
┌────────────────────────────────────────────────────────────────┐
│                   SliceIsolationXApp                           │
├────────────────────────────────────────────────────────────────┤
│  - measurement_duration: int                                   │
│  - scenarios: List[Scenario]                                   │
│  - logging: LoggingManager                                     │
│  - e2_client: E2Client                                         │
│  - connected_e2_nodes: List[Dict]                              │
│  - discovered_ues: Dict[str, UEInfo]                           │
│  - current_scenario: Scenario                                  │
│  - current_slice_configs: List[SliceConfig]                    │
│  - slice_ids_created: List[int]                                │
│  - rnti_to_slice: Dict[str, int]                               │
│  - scenario_results: List[ScenarioResult]                      │
│  - ack_event: threading.Event                                  │
│  - ues_discovered_event: threading.Event                       │
├────────────────────────────────────────────────────────────────┤
│  + on_e2_setup(e2_node)                                        │
│  + on_kpm_indication(indication)                               │
│  + on_slice_stats_indication(indication)                       │
│  + on_control_ack(ack)                                         │
│  + send_control(msg, timeout) -> bool                          │
│  + slice_add(config) -> bool                                   │
│  + slice_assoc(rnti, slice_id) -> bool                         │
│  + slice_del(slice_id) -> bool                                 │
│  + slice_reset() -> bool                                       │
│  + run_scenario(scenario) -> ScenarioResult                    │
│  + run_all_scenarios()                                         │
│  + stop()                                                      │
└────────────────────────────────────────────────────────────────┘
```

### 3.2 Supporting Classes

| Class | Purpose |
|-------|---------|
| `SliceConfig` | Slice configuration (algorithm, parameters) |
| `UEInfo` | Discovered UE (RNTI, IP, namespace, slice association) |
| `LogEntry` | Structured log entry (timestamp, event_type, details) |
| `ScenarioResult` | Complete scenario outcome |
| `LoggingManager` | Dual-output logging (JSONL + human-readable) |
| `E2MessageBuilder` | Builds E2 control messages for slice operations |

---

## 4. Core Components

### 4.1 SliceConfig

Represents a slice configuration with algorithm-specific parameters:

```python
@dataclass
class SliceConfig:
    slice_id: int           # Unique identifier (1, 2, ...)
    label: str              # Human-readable name
    algorithm: SliceAlgorithm
    # STATIC
    pos_low: Optional[int] = None
    pos_high: Optional[int] = None
    # NVS-RATE
    mbps_rsvd: Optional[float] = None
    # NVS-CAPACITY
    percentage: Optional[int] = None
```

**Serialization**: `to_dict()` / `from_dict()` for logging and messaging.

### 4.2 E2MessageBuilder

Builds control messages matching the E2SM-RC/E2SM-Slice service model:

```python
# ADD slice
{
    "operation": "ADD",
    "slice_id": 1,
    "label": "slice_balanced_1",
    "algorithm": "STATIC",
    "pos_low": 0,
    "pos_high": 6
}

# ASSOC UE to slice
{
    "operation": "ASSOC",
    "slice_id": 1,
    "rnti": "0x1234"
}

# DEL slice
{
    "operation": "DEL",
    "slice_id": 1
}

# RESET all
{
    "operation": "RESET",
    "algorithm": "NONE"
}
```

### 4.3 LoggingManager

Dual-output logging for both real-time monitoring and post-analysis:

```python
# JSONL file (machine-readable, one JSON per line)
{"timestamp": "2026-11-11T10:30:00.123456", "event_type": "COMMAND_SENT", ...}

# Human-readable log file
2026-11-11 10:30:00 | INFO     | [STATIC_BALANCED] [COMMAND_SENT] {'operation': 'ADD', ...}
```

**Event Types**:
- `COMMAND_SENT` — Control request sent to gNodeB
- `ACK_RECEIVED` — Acknowledgment received
- `STATE_OBSERVED` — SLICE_STATS indication (for reference only)
- `UE_DISCOVERED` — New UE RNTI found in KPM report
- `UE_ASSOCIATED` — UE successfully associated to slice
- `INFO` — General information
- `ERROR` — Errors and exceptions

### 4.4 UE Discovery

From testbed: RNTI only appears in KPM (MAC/RLC/PDCP/GTP) reports.

```python
def on_kpm_indication(self, indication):
    ue_records = indication.get('ue_records', [])
    for record in ue_records:
        rnti = record.get('rnti') or record.get('ue_id')
        if rnti and rnti not in self.discovered_ues:
            ue_info = self._identify_ue(rnti)  # Map to ue1/ue2
            self.discovered_ues[rnti] = ue_info
    if len(self.discovered_ues) >= 2:
        self.ues_discovered_event.set()
```

**Assignment Logic**: UEs sorted by RNTI → first UE → slice 1, second UE → slice 2. This ensures deterministic, reproducible mapping.

---

## 5. Scenario Definitions

### 5.1 Testbed Parameters

- **Total PRBs**: 14 (indices 0-13)
- **Baseline throughput**: ~18.6 Mbit/s
- **UE1 IP**: 10.45.0.39 (namespace `ue1`, interface `oaitun_ue1`)
- **UE2 IP**: 10.45.0.40 (namespace `ue2`, interface `oaitun_ue2`)

### 5.2 Four Scenarios

| Scenario | Algorithm | Slice 1 | Slice 2 | Purpose |
|----------|-----------|---------|---------|---------|
| **STATIC_BALANCED** | STATIC | PRB 0-6 (7 PRBs) | PRB 7-13 (7 PRBs) | Equal partition baseline |
| **STATIC_UNBALANCED** | STATIC | PRB 0-10 (11 PRBs) | PRB 11-13 (3 PRBs) | Asymmetric partition |
| **NVS_RATE_BALANCED** | NVS-RATE | 9 Mbit/s | 9 Mbit/s | Rate-based equal |
| **NVS_RATE_UNBALANCED** | NVS-RATE | 15 Mbit/s | 3 Mbit/s | Rate-based asymmetric |

### 5.3 Configuration Generation

```python
def get_scenario_configs(scenario: Scenario) -> List[SliceConfig]:
    if scenario == Scenario.STATIC_BALANCED:
        return [
            SliceConfig(1, "slice_balanced_1", SliceAlgorithm.STATIC, pos_low=0, pos_high=6),
            SliceConfig(2, "slice_balanced_2", SliceAlgorithm.STATIC, pos_low=7, pos_high=13)
        ]
    # ... etc for other scenarios
```

---

## 6. Message Flow

### 6.1 Per-Scenario Flow

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   START     │────►│  ADD Slice 1│────►│  ADD Slice 2│────►│  WAIT FOR   │
│  SCENARIO   │     │  (ACK wait) │     │  (ACK wait) │     │  UE DISCOVERY│
└─────────────┘     └─────────────┘     └─────────────┘     │  (KPM RNTI) │
                                                             └──────┬──────┘
                                                                    │
                                                                    ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  CLEANUP    │◄────│  MEASURE    │◄────│  ASSOC UE2  │◄────│  ASSOC UE1  │
│  DEL+RESET  │     │  WINDOW     │     │  (ACK wait) │     │  (ACK wait) │
└─────────────┘     │ (iperf3)    │     └─────────────┘     └─────────────┘
                    └─────────────┘
                         │
                         ▼
                ┌─────────────┐
                │ LOG RESULTS │
                └─────────────┘
```

### 6.2 Timing Details

| Step | Action | Wait | Duration |
|------|--------|------|----------|
| 1 | ADD slice 1 | ACK | ~1s |
| 2 | ADD slice 2 | ACK | ~1s |
| 3 | Wait for KPM RNTI | Event | Up to 10s |
| 4 | ASSOC UE1 | ACK | ~1s |
| 5 | ASSOC UE2 | ACK | ~1s |
| 6 | Measurement window | — | 30s (configurable) |
| 7 | DEL slice 2 | ACK | ~1s |
| 8 | DEL slice 1 | ACK | ~1s |
| 9 | RESET | ACK | ~1s |
| 10 | Stabilize | — | 2s |
| **Total** | | | **~40-50s per scenario** |

---

## 7. Logging and Verification Strategy

### 7.1 Why Not Trust SLICE_STATS JSON?

Testbed showed (Compte_rendu_testbed_ORAN, Section 3.4):
- Slice count fluctuates (0-3) instead of stable
- Slice IDs differ (requested 0,2,5 → reported 497, 989)
- Algorithms/parameters don't match
- UE association not visible
- 5s delay after RESET for stabilization

### 7.2 Verification Method: Measurement-Driven

**Primary verification**: Per-UE throughput measurements via iperf3

```
Isolation Metric = Throughput(protected_slice) / Baseline_throughput
```

- **Perfect isolation**: Ratio ≈ 1.0 (slice maintains baseline)
- **No isolation**: Ratio → 0 (slice starved by neighbor)
- **Partial isolation**: Ratio between 0 and 1

### 7.3 Log Analysis

The JSONL log file enables automated analysis:

```bash
# Extract all COMMAND_SENT events
grep "COMMAND_SENT" xapp_slice_isolation_20261111_103000.jsonl | jq .

# Extract ACK_RECEIVED for verification
grep "ACK_RECEIVED" xapp_slice_isolation_20261111_103000.jsonl | jq .

# Timeline of a scenario
grep "STATIC_BALANCED" xapp_slice_isolation_20261111_103000.jsonl | jq -c '{time: .timestamp, event: .event_type, details: .details}'
```

### 7.4 Expected Log Structure

```
xapp_logs/
├── xapp_slice_isolation_20261111_103000.jsonl   # Machine-readable
└── xapp_slice_isolation_20261111_103000.log     # Human-readable
```

---

## 8. Running on the Testbed

### 8.1 Prerequisites

1. Testbed platform running (RIC: Connected, gNB: Connected, 2 UEs registered)
2. FlexRIC Python SDK at `/opt/flexric/python`
3. ASN.1 fix library: `LD_PRELOAD=/opt/asnfix/libasnfix.so`

### 8.2 Deployment

Copy the xApp to the testbed (or run from the development environment if network access):

```bash
# On testbed / RIC environment
scp xapp_slice_isolation.py user@testbed:/opt/flexric/xapps/
```

### 8.3 Execution

```bash
# Basic run (all 4 scenarios, 30s each)
LD_PRELOAD=/opt/asnfix/libasnfix.so python3 -u xapp_slice_isolation.py

# Custom measurement duration
LD_PRELOAD=/opt/asnfix/libasnfix.so python3 -u xapp_slice_isolation.py --duration 40

# Specific scenarios only
LD_PRELOAD=/opt/asnfix/libasnfix.so python3 -u xapp_slice_isolation.py \
    --scenarios STATIC_BALANCED STATIC_UNBALANCED

# Custom log directory
LD_PRELOAD=/opt/asnfix/libasnfix.so python3 -u xapp_slice_isolation.py --log-dir ./my_logs
```

### 8.4 Coordinated iperf3 Measurements

**Critical**: The xApp prints when to start iperf3:

```
>>> START IPERF3 MEASUREMENTS NOW <<<
  Server (ue1): sudo ip netns exec ue1 iperf3 -s
  Client (ue2): sudo ip netns exec ue2 iperf3 -c 10.45.0.39 -t 30
```

**In separate terminals**:

**Terminal 1 (ue1 - server):**
```bash
sudo ip netns exec ue1 iperf3 -s
```

**Terminal 2 (ue2 - client):**
```bash
# Wait for xApp prompt, then run:
sudo ip netns exec ue2 iperf3 -c 10.45.0.39 -t 30
```

**Terminal 3 (xApp output):**
```bash
# Monitor xApp logs
tail -f /tmp/xapp_logs/xapp_slice_isolation_*.log
```

### 8.5 Multiple Repetitions

For reproducibility (Phase 3 requirement), run each scenario multiple times:

```bash
# Run 3 repetitions of each scenario
for rep in 1 2 3; do
    LD_PRELOAD=/opt/asnfix/libasnfix.so python3 -u xapp_slice_isolation.py \
        --log-dir ./logs_rep${rep}
    sleep 10  # Cool-down between repetitions
done
```

---

## 9. Expected Outputs

### 9.1 Log Files

**JSONL** (`xapp_slice_isolation_YYYYMMDD_HHMMSS.jsonl`):
```json
{"timestamp": "2026-11-11T10:30:00.123456", "event_type": "COMMAND_SENT", "details": {"operation": "ADD", "slice_config": {"slice_id": 1, "label": "slice_balanced_1", "algorithm": "STATIC", "pos_low": 0, "pos_high": 6}}, "scenario": "STATIC_BALANCED", "configuration": null}
{"timestamp": "2026-11-11T10:30:01.234567", "event_type": "ACK_RECEIVED", "details": {"operation": "ADD", "slice_id": 1, "success": true}, "scenario": "STATIC_BALANCED", "configuration": null}
{"timestamp": "2026-11-11T10:30:02.345678", "event_type": "UE_DISCOVERED", "details": {"rnti": "0x1234", "ip": "10.45.0.39", "namespace": "ue1"}, "scenario": "STATIC_BALANCED", "configuration": null}
{"timestamp": "2026-11-11T10:30:03.456789", "event_type": "UE_ASSOCIATED", "details": {"rnti": "0x1234", "slice_id": 1}, "scenario": "STATIC_BALANCED", "configuration": null}
...
```

**Human-readable log** (`xapp_slice_isolation_YYYYMMDD_HHMMSS.log`):
```
2026-11-11 10:30:00 | INFO     | Starting scenario: STATIC_BALANCED
2026-11-11 10:30:00 | INFO     | Creating slice 1 (slice_balanced_1)
2026-11-11 10:30:01 | INFO     | [STATIC_BALANCED] [COMMAND_SENT] {'operation': 'ADD', 'slice_config': {...}}
2026-11-11 10:30:01 | INFO     | [STATIC_BALANCED] [ACK_RECEIVED] {'operation': 'ADD', 'slice_id': 1, 'success': True}
2026-11-11 10:30:02 | INFO     | Waiting for UE RNTI discovery from KPM reports...
2026-11-11 10:30:03 | INFO     | Discovered 2 UEs: ['0x1234', '0x5678']
2026-11-11 10:30:03 | INFO     | Associating UE 0x1234 (ue1) to slice 1
2026-11-11 10:30:04 | INFO     | [STATIC_BALANCED] [UE_ASSOCIATED] {'rnti': '0x1234', 'slice_id': 1}
2026-11-11 10:30:04 | INFO     | Configuration active. Maintaining for 30s for measurements...
2026-11-11 10:30:04 | INFO     | >>> START IPERF3 MEASUREMENTS NOW <<<
2026-11-11 10:30:04 | INFO     |   Server (ue1): sudo ip netns exec ue1 iperf3 -s
2026-11-11 10:30:04 | INFO     |   Client (ue2): sudo ip netns exec ue2 iperf3 -c 10.45.0.39 -t 30
...
2026-11-11 10:30:34 | INFO     | Measurement period complete.
2026-11-11 10:30:34 | INFO     | Cleaning up: deleting slices...
2026-11-11 10:30:35 | INFO     | Resetting slice configuration...
2026-11-11 10:30:37 | INFO     | Scenario STATIC_BALANCED completed successfully
```

### 9.2 Summary Output

At the end of all scenarios:
```
============================================================
EXPERIMENT SUMMARY
============================================================
  STATIC_BALANCED: SUCCESS
    Slices: ['1(slice_balanced_1)', '2(slice_balanced_2)']
    UE Associations: {'0x1234': 1, '0x5678': 2}
  STATIC_UNBALANCED: SUCCESS
    Slices: ['1(slice_wide)', '2(slice_narrow)']
    UE Associations: {'0x1234': 1, '0x5678': 2}
  NVS_RATE_BALANCED: SUCCESS
    Slices: ['1(slice_rate_balanced_1)', '2(slice_rate_balanced_2)']
    UE Associations: {'0x1234': 1, '0x5678': 2}
  NVS_RATE_UNBALANCED: SUCCESS
    Slices: ['1(slice_rate_wide)', '2(slice_rate_narrow)']
    UE Associations: {'0x1234': 1, '0x5678': 2}
Logs saved to: /tmp/xapp_logs
```

### 9.3 Deliverables for Phase 2

1. ✅ **Source code**: `xapp_slice_isolation.py` (commented, modular)
2. ✅ **Design documentation**: This file (`Phase2_XApp_Design_Explanation.md`)
3. ✅ **Log files**: JSONL + human-readable per run
4. ✅ **Configuration**: Scenario definitions, parameters

---

## 10. Troubleshooting

### 10.1 Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| `ImportError: xapp_sdk` | FlexRIC Python SDK not in path | `sys.path.append('/opt/flexric/python')` |
| `ModuleNotFoundError: e2sm_*` | E2SM modules not installed | Check FlexRIC installation |
| No UEs discovered | KPM subscription failed | Verify gNB connected, check RAN function IDs |
| ACK timeout | gNodeB not responding | Check E2 connection, RIC status |
| "Address already in use" | Previous xApp not cleaned up | Run RESET manually, wait 5s |
| JSON state mismatch | Expected behavior | Do not trust SLICE_STATS, use iperf3 |

### 10.2 Debug Commands

```bash
# Check RIC status
curl http://ric_ip:port/ric/v1/nodes

# Check gNB E2 connection
# (from RIC logs or CLI)

# Manual slice reset (if stuck)
LD_PRELOAD=/opt/asnfix/libasnfix.so python3 -c "
from xapp_sdk import E2Client
client = E2Client()
client.control_request({'operation': 'RESET', 'algorithm': 'NONE'})
"

# View slice stats JSON (for reference only)
watch -n1 "cat /tmp/rt_slice_stats.json"
```

### 10.3 Verification Checklist

Before running full experiment:
- [ ] RIC shows "Connected"
- [ ] gNB shows "Connected" (E2 nodes = 1)
- [ ] 2 UEs registered (ue1, ue2)
- [ ] xapp_hw runs successfully (Connected E2 nodes = 1)
- [ ] KPM subscriptions work (measurement xApp runs)
- [ ] Baseline iperf3 works (~18.6 Mbit/s)

---

## Appendix: File Structure

```
Phase 2 Deliverables/
├── xapp_slice_isolation.py           # Main xApp source code
├── Phase2_XApp_Design_Explanation.md # This document
└── (generated at runtime)
    ├── xapp_slice_isolation_YYYYMMDD_HHMMSS.jsonl
    └── xapp_slice_isolation_YYYYMMDD_HHMMSS.log
```

---

*End of Phase 2 Design Explanation*