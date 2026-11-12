# Phase 1 — State of the Art and Hands-on

## Slice Isolation in NextG O-RAN Networks

**Project:** Mini-projet de fin d'études — binôme — 25 heures  
**Team:** Oueslati Ranim, Ben Hedhili Mawadda  
**Supervisor:** Wiem Abderrahim  
**Date:** November 2026

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [O-RAN Architecture](#2-o-ran-architecture)
3. [The RAN Intelligent Controller (RIC)](#3-the-ran-intelligent-controller-ric)
4. [The E2 Interface](#4-the-e2-interface)
5. [xApp Model and Lifecycle](#5-xapp-model-and-lifecycle)
6. [Network Slicing in O-RAN](#6-network-slicing-in-o-ran)
7. [Slice Isolation: Problem Statement](#7-slice-isolation-problem-statement)
8. [Partition Algorithms](#8-partition-algorithms)
9. [Testbed Platform Description](#9-testbed-platform-description)
10. [Reference xApp Analysis: xapp_slice_moni_ctrl.py](#10-reference-xapp-analysis-xapp_slicemoni_ctrlpy)
11. [Key Findings from Testbed Experiments](#11-key-findings-from-testbed-experiments)
12. [Conclusion and Transition to Phase 2](#12-conclusion-and-transition-to-phase-2)

---

## 1. Introduction

The evolution of mobile networks toward 5G and beyond (6G/NextG) has introduced the concept of **network slicing** as a fundamental mechanism to support diverse service requirements—enhanced Mobile Broadband (eMBB), Ultra-Reliable Low-Latency Communications (URLLC), and massive Machine-Type Communications (mMTC)—over a shared physical infrastructure.

The **O-RAN Alliance** has defined an open, intelligent, and disaggregated RAN architecture that enables third-party applications (xApps) to control and optimize RAN behavior in near-real-time through the **E2 interface**. This project focuses on developing an xApp that partitions radio resources between slices and experimentally evaluates the quality of isolation achieved.

**Isolation** is defined as the ability of a slice to maintain its throughput when a neighboring slice is saturated. The base station scheduler enforces isolation by applying the resource partition transmitted by the xApp. This project evaluates whether isolation is effective and whether its quality varies with the chosen partition algorithm.

---

## 2. O-RAN Architecture

### 2.1 Overview

The O-RAN architecture disaggregates the traditional monolithic RAN into modular components connected by open interfaces:

```
┌─────────────────────────────────────────────────────────────────┐
│                      Service Management and Orchestration (SMO) │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │   Non-RT RIC    │  │   O1 Interface  │  │   O2 Interface  │  │
│  │   (rApps)       │  │   (Management)  │  │   (Cloud Infra) │  │
│  └────────┬────────┘  └─────────────────┘  └─────────────────┘  │
└───────────┼──────────────────────────────────────────────────────┘
            │ A1 Interface
┌───────────▼──────────────────────────────────────────────────────┐
│                    Near-RT RIC (Edge)                            │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  xApp 1  │  xApp 2  │  xApp 3  │  ...  │  xApp N           │ │
│  └─────────────────────────────────────────────────────────────┘ │
└───────────┬──────────────────────────────────────────────────────┘
            │ E2 Interface
┌───────────▼──────────────────────────────────────────────────────┐
│                      O-RAN Distributed Unit (O-DU)               │
│              (gNodeB with E2 Agent)                              │
└───────────┬──────────────────────────────────────────────────────┘
            │ F1 Interface
┌───────────▼──────────────────────────────────────────────────────┐
│                      O-RAN Radio Unit (O-RU)                     │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 Key Components

| Component | Role | Time Scale |
|-----------|------|------------|
| **SMO** | Service Management and Orchestration; hosts Non-RT RIC | > 1 second |
| **Non-RT RIC** | Runs rApps for policy, optimization, ML model training | > 1 second |
| **Near-RT RIC** | Runs xApps for near-real-time control (10 ms – 1 s) | 10 ms – 1 s |
| **O-DU / gNodeB** | Radio processing with E2 agent for RIC communication | Sub-millisecond |
| **O-RU** | Radio frequency transmission/reception | Sub-millisecond |

### 2.3 Interfaces

- **A1**: Non-RT RIC ↔ Near-RT RIC (policy delivery, ML model deployment)
- **E2**: Near-RT RIC ↔ gNodeB (control, measurement, subscription)
- **O1**: SMO ↔ Managed Functions (FCAPS management)
- **O2**: SMO ↔ Cloud Infrastructure (resource management)
- **F1**: O-DU ↔ O-RU (split PHY/MAC)

---

## 3. The RAN Intelligent Controller (RIC)

### 3.1 Dual-RIC Architecture

The O-RAN RIC is split into two distinct entities operating at different time scales:

#### Non-RT RIC (in SMO)
- **Time scale**: > 1 second
- **Applications**: rApps (rAN applications)
- **Functions**: 
  - Network-wide optimization
  - Policy management
  - AI/ML model training and distribution
  - Long-term resource planning
- **Interface to Near-RT RIC**: A1 (policy delivery, enrichment info, ML models)

#### Near-RT RIC (Edge)
- **Time scale**: 10 ms – 1 second
- **Applications**: xApps (xRAN applications)
- **Functions**:
  - Per-UE scheduling optimization
  - Load balancing
  - Mobility management
  - **Slice resource partitioning** (this project)
  - QoS enforcement
- **Interface to gNodeB**: E2 (service models for control and monitoring)

### 3.2 Near-RT RIC Platform: FlexRIC

The testbed uses **FlexRIC** (developed by OpenAirInterface) instead of the O-RAN SC RIC for its lightweight footprint:

| Criterion | O-RAN SC RIC | FlexRIC |
|-----------|--------------|---------|
| Storage | Higher (containerized components) | Lower |
| Latency (RTT) | Higher (two extra message hops) | Lower |
| CPU | Higher | Lower |
| Memory | Higher | Lower |

FlexRIC provides:
- E2 termination and message routing
- xApp registration and lifecycle management
- SDK for C/C++ and Python (via SWIG) xApp development
- Example xApps: hello world, measurement, slicing

---

## 4. The E2 Interface

### 4.1 Purpose

The E2 interface connects the Near-RT RIC to the gNodeB (E2 node). It enables:
- **Subscription**: xApp requests measurement reports for specific RAN functions
- **Control**: xApp sends control requests to modify gNodeB behavior
- **Node Management**: Setup, configuration, and reset of E2 nodes

### 4.2 E2 Service Models (E2SM)

E2SMs define the semantics of messages for specific RAN functions. Relevant to this project:

| E2SM | RAN Function | Purpose |
|------|--------------|---------|
| **E2SM-KPM** | Key Performance Measurement | MAC, RLC, PDCP, GTP metrics |
| **E2SM-RC** | Radio Control | Slice creation, association, deletion |
| **E2SM-Slice** | Slice Statistics | Slice state reporting (SLICE_STATS) |

### 4.3 E2 Message Flow

```
xApp                                    Near-RT RIC                              gNodeB (E2 Node)
 │                                        │                                        │
 │──── RIC SUBSCRIPTION REQUEST ────────►│                                        │
 │                                        │──── E2 SUBSCRIPTION REQUEST ────────►│
 │                                        │◄─── E2 SUBSCRIPTION RESPONSE ────────│
 │◄─── RIC SUBSCRIPTION RESPONSE ────────│                                        │
 │                                        │                                        │
 │                                        │◄─── RIC INDICATION (periodic) ────────│  Measurement reports
 │◄─── RIC INDICATION ────────────────────│                                        │
 │                                        │                                        │
 │──── RIC CONTROL REQUEST ─────────────►│                                        │
 │                                        │──── E2 CONTROL REQUEST ─────────────►│
 │                                        │◄─── E2 CONTROL ACKNOWLEDGE ──────────│
 │◄─── RIC CONTROL ACKNOWLEDGE ──────────│                                        │
```

### 4.4 Subscription Mechanism

1. xApp identifies the target RAN function (e.g., MAC, RLC, PDCP, GTP, SLICE)
2. xApp sends RIC SUBSCRIPTION REQUEST with:
   - Event trigger definition (periodic, on-change, etc.)
   - Action definition (report format, granularity)
3. gNodeB acknowledges and starts sending RIC INDICATION messages periodically

### 4.5 Control Mechanism

1. xApp sends RIC CONTROL REQUEST with:
   - Control header (slice ID, operation type)
   - Control message (parameters: pos_low, pos_high, mbps_rsvd, etc.)
2. gNodeB applies the control action (scheduler reconfiguration)
3. gNodeB responds with RIC CONTROL ACKNOWLEDGE (success/failure)

---

## 5. xApp Model and Lifecycle

### 5.1 What is an xApp?

An xApp is a microservice that:
- Runs on the Near-RT RIC platform
- Communicates with gNodeBs via the E2 interface
- Implements a specific RAN control or monitoring function
- Can be written in C/C++ or Python (FlexRIC provides Python bindings via SWIG)

### 5.2 xApp Lifecycle

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  REGISTER   │────►│  SUBSCRIBE  │────►│  RUN LOOP   │────►│  TERMINATE  │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
      │                   │                   │                   │
      ▼                   ▼                   ▼                   ▼
  Announce to        Subscribe to       Process            Cleanup
  RIC (E2 nodes,     RAN functions      indications,       resources,
  capabilities)      (KPM, RC)          send controls      disconnect
```

### 5.3 xApp Registration

On startup, an xApp:
1. Connects to the RIC's gRPC interface
2. Registers its name, version, and supported RAN functions
3. Receives the list of connected E2 nodes and their capabilities

### 5.4 Subscription to RAN Functions

The xApp subscribes to RAN functions relevant to its purpose:
- **KPM (MAC/RLC/PDCP/GTP)**: For measurement and monitoring
- **RC (Radio Control)**: For slice management operations
- **SLICE_STATS**: For observing slice state

### 5.5 Main Event Loop

The xApp typically runs an event loop that:
- Receives RIC INDICATION messages (measurement reports)
- Processes reports (extracts RNTI, throughput, slice stats)
- Sends RIC CONTROL REQUESTS (slice operations)
- Logs state and decisions
- Handles timeouts and errors

---

## 6. Network Slicing in O-RAN

### 6.1 Concept of Network Slicing

Network slicing creates multiple logical networks over a shared physical infrastructure. Each slice:
- Has dedicated (or shared with guarantees) resources
- Provides specific QoS characteristics (bandwidth, latency, reliability)
- Is isolated from other slices to prevent interference

### 6.2 RAN-Level Slicing

In the RAN, slicing is implemented at the **MAC scheduler** level:
- The scheduler allocates Physical Resource Blocks (PRBs) to UEs
- Slices define which PRBs (or what fraction of capacity) each slice can use
- The gNodeB scheduler enforces the partition

### 6.3 Slice Isolation

**Definition**: The ability of a slice to maintain its performance (throughput, latency) when other slices experience high load.

**Why it matters**:
- Without isolation, a "greedy" slice can starve others
- SLAs for critical slices (URLLC, mission-critical) require guarantees
- Regulatory and business requirements demand predictable performance

### 6.4 Isolation Enforcement Point

The isolation is **enforced by the gNodeB MAC scheduler**, not by the xApp. The xApp only:
1. Defines the partition (which PRBs / what capacity per slice)
2. Associates UEs to slices (via RNTI)
3. The scheduler applies the partition at each TTI (Transmission Time Interval)

---

## 7. Slice Isolation: Problem Statement

### 7.1 The Core Problem

When multiple slices share a cell's radio resources:
- **Without partitioning**: Best-effort scheduling → slice with more traffic dominates
- **With static partitioning**: Fixed PRB ranges → predictable but inflexible
- **With rate-based partitioning**: Guaranteed Mbps → flexible but depends on channel conditions

### 7.2 Research Questions

1. **Is isolation effective?** Does a slice maintain its throughput when neighbor is saturated?
2. **Does algorithm choice matter?** STATIC vs NVS-RATE — which provides better isolation?
3. **How does balance affect isolation?** Balanced (equal shares) vs unbalanced (wide/narrow) configurations?
4. **Is the gNodeB reporting accurate?** Testbed showed JSON state mismatch — verify via UE throughput

### 7.3 Metrics for Isolation Quality

| Metric | Description | Target |
|--------|-------------|--------|
| **Observed Slice Throughput Stability** | Throughput of protected slice vs. load on neighbor | Flat curve (ideal) |
| **Isolation Gain** | Difference between sliced and non-sliced throughput under load | Maximum |
| **Resource Utilization** | Total cell throughput vs. theoretical maximum | High |
| **Fairness Index** | Jain's fairness between slices under load | Close to 1.0 |

---

## 8. Partition Algorithms

The testbed platform supports three partition algorithms:

### 8.1 STATIC (Static Partition by PRB Range)

| Parameter | Description | Type |
|-----------|-------------|------|
| `pos_low` | Starting PRB index (inclusive) | Integer [0–N] |
| `pos_high` | Ending PRB index (inclusive) | Integer [0–N] |

**Characteristics**:
- Fixed PRB allocation per slice
- Predictable, deterministic
- Inflexible to traffic variations
- Simple to verify and debug

**Example Configuration (Balanced, 14 PRBs total)**:
- Slice 1: pos_low=0, pos_high=6 (7 PRBs)
- Slice 2: pos_low=7, pos_high=13 (7 PRBs)

**Example Configuration (Unbalanced)**:
- Slice 1: pos_low=0, pos_high=10 (11 PRBs) — wide
- Slice 2: pos_low=11, pos_high=13 (3 PRBs) — narrow

### 8.2 NVS-RATE (Guaranteed Bit Rate Reservation)

| Parameter | Description | Type |
|-----------|-------------|------|
| `mbps_rsvd` | Reserved throughput in Mbit/s | Integer/Float |

**Characteristics**:
- Guarantees minimum throughput per slice
- Scheduler dynamically allocates PRBs to meet rate
- More flexible than STATIC
- Depends on channel conditions (MCS, SINR)

**Example Configuration (Balanced)**:
- Slice 1: mbps_rsvd = 9 Mbit/s
- Slice 2: mbps_rsvd = 9 Mbit/s

**Example Configuration (Unbalanced)**:
- Slice 1: mbps_rsvd = 15 Mbit/s — wide
- Slice 2: mbps_rsvd = 3 Mbit/s — narrow

### 8.3 NVS-CAPACITY (Capacity Percentage Reservation)

| Parameter | Description | Type |
|-----------|-------------|------|
| `percentage` | Percentage of cell capacity | Integer [0–100] |

**Characteristics**:
- Reserves fraction of total capacity
- Adapts to cell capacity changes
- Not used in this project (Phase 2 focuses on STATIC and NVS-RATE)

### 8.4 Algorithm Comparison

| Aspect | STATIC | NVS-RATE | NVS-CAPACITY |
|--------|--------|----------|--------------|
| Predictability | High | Medium | Medium |
| Flexibility | Low | High | High |
| Channel Awareness | No | Yes | Yes |
| Configuration Simplicity | High | Medium | Medium |
| Verification | Easy (PRB counts) | Harder (rate measurement) | Medium |

---

## 9. Testbed Platform Description

### 9.1 Hardware and Software Stack

The IEEE 5G/6G testbed is a cloud-hosted end-to-end 5G network with O-RAN compatible RAN:

| Component | Implementation | Details |
|-----------|---------------|---------|
| **5G Core** | Open5GS on Kubernetes | AMF, SMF, UPF, NSSF, etc. |
| **gNodeB** | OAI NR gNB (SA mode) | RFSIM (radio simulation), E2 agent embedded |
| **UEs** | OAI NR UE (ue1, ue2) | Each in separate network namespace |
| **Near-RT RIC** | FlexRIC (OAI) | Lightweight, C/C++ and Python SDK |
| **xApps** | FlexRIC examples | helloworld, measurement, slicing |

### 9.2 Network Namespaces

Each UE runs in its own Linux network namespace:
- `ue1`: Tunnel interface `oaitun_ue1`, IP `10.45.0.39`
- `ue2`: Tunnel interface `oaitun_ue2`, IP `10.45.0.40`

Commands execute in namespace:
```bash
sudo ip netns exec ue1 <command>
sudo ip netns exec ue2 <command>
```

### 9.3 Traffic Generation

- **Tool**: iperf3 (TCP)
- **Server**: ue1 (`sudo ip netns exec ue1 iperf3 -s`)
- **Client**: ue2 (`sudo ip netns exec ue2 iperf3 -c <ue1_ip> -t 30`)
- **Baseline throughput (no slicing)**: 18.6 Mbit/s sender / 18.0 Mbit/s receiver
- **RTT**: ~24 ms average
- **Retransmissions**: 131 in 30s baseline test

### 9.4 Reference xApps Available

| xApp | Type | Language | Purpose |
|------|------|----------|---------|
| `xapp_hw` | Test | C | Connect to RIC, list E2 nodes and RAN functions |
| `xapp_mac_rlc_pdcp_gtp_moni.py` | Measurement | Python | Subscribe to MAC, RLC, PDCP, GTP KPM reports |
| `xapp_slice_moni_ctrl.py` | Control + Measurement | Python | Slice CRUD operations, state observation |

---

## 10. Reference xApp Analysis: xapp_slice_moni_ctrl.py

### 10.1 Overview

The reference xApp `xapp_slice_moni_ctrl.py` demonstrates the complete slice lifecycle:
1. **ADD** — Create slices with partition algorithm and parameters
2. **ASSOC** — Associate UE (by RNTI) to a slice
3. **DEL** — Delete a slice
4. **RESET** — Reset all slice configuration to initial state

### 10.2 Sequence of Operations (from testbed log)

```
Timestamp     Operation                    Parameters
────────────────────────────────────────────────────────────────────
08:43:49      ADD                          3 slices: STATIC, s1/s2/s3
                                            pos_low/pos_high: 0-2, 3-10, 11-13
08:44:09      ASSOC                        RNTI df → Slice 2 (id 5)
08:44:29      DEL                          Slice 5
08:44:39      RESET                        Algorithm = NONE
```

### 10.3 Control Message Structure

Each control request contains:
- **Slice ID** (identifier)
- **Operation**: ADD, ASSOC, DEL, RESET
- **Algorithm**: STATIC, NVS-RATE, NVS-CAPACITY, NONE
- **Parameters**: Algorithm-specific (pos_low/pos_high, mbps_rsvd, percentage)
- **Label**: Human-readable name (e.g., "This is my label")
- **UE RNTI**: For ASSOC operation

### 10.4 State Reporting (SLICE_STATS)

The gNodeB reports slice state via `rt_slice_stats.json`:
- Written continuously during xApp execution (~1 second interval)
- Contains slice count, IDs, labels, algorithms, parameters, associated UEs
- **Critical finding**: JSON state **does not match** control commands sent:
  - Slice count fluctuates (0–3) instead of stable 3
  - Slice IDs differ from requested (497, 989 vs 0, 2, 5)
  - Algorithms/parameters don't match
  - UE association not visible in JSON
  - State only stabilizes after RESET (5s delay)

### 10.5 Key Code Patterns (Inferred from Behavior)

```python
# Pseudocode structure based on observed behavior

import sys
sys.path.append('/opt/flexric/python')  # FlexRIC Python SDK

from xapp_sdk import XApp, E2Client, RANFunction

class SliceMoniCtrlXApp(XApp):
    def __init__(self):
        super().__init__("xapp_slice_moni_ctrl")
        self.e2_client = E2Client()
        self.slice_id_counter = 0
        self.ue_rnti = None
        
    def on_e2_setup(self, e2_node):
        # Subscribe to SLICE_STATS for state monitoring
        self.subscribe_slice_stats(e2_node)
        # Subscribe to KPM (MAC/RLC/PDCP/GTP) for RNTI discovery
        self.subscribe_kpm(e2_node)
        
    def on_kpm_indication(self, indication):
        # Extract RNTI from MAC/RLC/PDCP reports
        self.ue_rnti = extract_rnti(indication)
        
    def run_slice_scenario(self):
        # 1. ADD three slices with STATIC algorithm
        self.slice_add(algorithm="STATIC", slices=[
            {"id": 0, "label": "s1", "pos_low": 0, "pos_high": 2},
            {"id": 2, "label": "s2", "pos_low": 3, "pos_high": 10},
            {"id": 5, "label": "s3", "pos_low": 11, "pos_high": 13},
        ])
        
        # 2. ASSOC UE to slice 2 (using discovered RNTI)
        self.slice_assoc(rnti=self.ue_rnti, slice_id=2)
        
        # 3. DEL slice 5
        self.slice_del(slice_id=5)
        
        # 4. RESET all
        self.slice_reset()
        
    def slice_add(self, algorithm, slices):
        for s in slices:
            ctrl_msg = build_slice_control(
                op="ADD",
                slice_id=s["id"],
                algorithm=algorithm,
                pos_low=s.get("pos_low"),
                pos_high=s.get("pos_high"),
                label=s.get("label")
            )
            self.e2_client.control_request(ctrl_msg)
            wait_for_ack()
            
    def slice_assoc(self, rnti, slice_id):
        ctrl_msg = build_slice_control(
            op="ASSOC",
            slice_id=slice_id,
            rnti=rnti
        )
        self.e2_client.control_request(ctrl_msg)
        wait_for_ack()
        
    def slice_del(self, slice_id):
        ctrl_msg = build_slice_control(op="DEL", slice_id=slice_id)
        self.e2_client.control_request(ctrl_msg)
        wait_for_ack()
        
    def slice_reset(self):
        ctrl_msg = build_slice_control(op="RESET", algorithm="NONE")
        self.e2_client.control_request(ctrl_msg)
        wait_for_ack()
```

### 10.6 Critical Observations for Phase 2 Design

1. **RNTI Discovery**: Must subscribe to KPM (MAC/RLC/PDCP) to learn UE RNTIs before ASSOC
2. **State Verification**: Cannot trust `rt_slice_stats.json` — must verify via UE throughput measurements
3. **ACK Handling**: Every control request receives an acknowledgment — must wait for it
4. **Timing**: Scenario runs ~60 seconds; state JSON only valid between ADD and RESET
5. **Algorithm Parameter Mapping**: 
   - STATIC → pos_low, pos_high
   - NVS-RATE → mbps_rsvd
   - NVS-CAPACITY → percentage

---

## 11. Key Findings from Testbed Experiments

### 11.1 Successful Validations

✅ **Full O-RAN chain operational**: RIC, gNB, two UEs connected  
✅ **E2 connectivity**: Connected E2 nodes = 1  
✅ **Subscription working**: 4/4 KPM subscriptions successful (MAC, RLC, PDCP, GTP)  
✅ **Control path working**: All slice commands (ADD, ASSOC, DEL, RESET) acknowledged  
✅ **Traffic generation**: iperf3 works over 5G path (UE→gNB→UPF→UE)  
✅ **Baseline measured**: 18.6/18.0 Mbit/s without slicing  

### 11.2 Critical Issues Discovered

❌ **Slice state reporting unreliable**: `rt_slice_stats.json` does not reflect actual configuration  
❌ **Slice IDs mismatch**: Requested IDs (0,2,5) vs reported (497, 989)  
❌ **Algorithm/parameter mismatch**: STATIC with pos_low/pos_high not reflected correctly  
❌ **UE association invisible**: ASSOC not visible in JSON state  
❌ **State stabilization delay**: ~5 seconds after RESET for JSON to show "0 slices"

### 11.3 Implications for Phase 2

1. **Do not rely on JSON state** for verification — use UE throughput measurements
2. **Implement own state tracking** in xApp (log sent commands, received ACKs, timestamps)
3. **RNTI-based UE association** is critical — must extract RNTI from KPM reports
4. **Measurement-driven validation** is the only reliable isolation verification method

---

## 12. Conclusion and Transition to Phase 2

Phase 1 has established:
- **Solid understanding** of O-RAN architecture, RIC, E2 interface, and xApp model
- **Clear picture** of three partition algorithms (STATIC, NVS-RATE, NVS-CAPACITY)
- **Working testbed** with validated control and measurement paths
- **Baseline throughput** of 18.6/18.0 Mbit/s for comparison
- **Critical insight**: gNodeB state reporting is unreliable; isolation must be verified via per-UE throughput measurements

### Phase 2 Objectives (12 hours)

Building on `xapp_slice_moni_ctrl.py`, develop a new xApp that:

1. **Creates two slices with STATIC algorithm**:
   - Balanced: equal PRB ranges (e.g., 0-6 and 7-13 for 14 PRBs)
   - Unbalanced: wide + narrow (e.g., 0-10 and 11-13)

2. **Associates each UE to a distinct slice** using RNTI from KPM reports

3. **Maintains each configuration** for fixed duration (sufficient for iperf3 measurement)

4. **Verifies applied configuration** by:
   - Logging sent commands with timestamps
   - Recording received ACKs
   - Measuring per-UE throughput via iperf3 (external coordination)

5. **Logs all configurations and observed states** to file with timestamps

6. **Extends to NVS-RATE algorithm**:
   - Same balanced/unbalanced scenarios
   - Compare isolation quality between STATIC and NVS-RATE

### Deliverables for Phase 2

- ✅ Enhanced xApp source code (Python, commented)
- ✅ Configuration logging with timestamps
- ✅ Support for both STATIC and NVS-RATE algorithms
- ✅ Balanced and unbalanced scenarios for each algorithm
- ✅ Clear separation of concerns (control, monitoring, logging)

---

## References

1. O-RAN Alliance, "O-RAN Architecture Description", O-RAN.WG1.O-RAN-Architecture-Description-v04.00
2. O-RAN Alliance, "E2 Interface Specification", O-RAN.WG3.E2AP-v02.00
3. O-RAN Alliance, "E2 Service Models", O-RAN.WG3.E2SM-KPM, E2SM-RC, E2SM-Slice
4. FlexRIC Documentation, OpenAirInterface
5. Testbed Report: "Compte_rendu_testbed_ORAN" (22/10/2026)
6. Project Sheet: "Fiche_Projet_Slice_Isolation" (2026)

---

*End of Phase 1 Documentation*