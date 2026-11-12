# Quick Start Guide: Phase 1 & 2 Deliverables

## Slice Isolation in NextG O-RAN Networks

---

## 📁 Files Created

| File | Description |
|------|-------------|
| `Phase1_State_of_the_Art.md` | Comprehensive Phase 1 documentation (state of the art) |
| `xapp_slice_isolation.py` | Phase 2 xApp source code (Python, fully commented) |
| `Phase2_XApp_Design_Explanation.md` | Detailed design explanation for Phase 2 xApp |
| `test_xapp_logic.py` | Test script verifying xApp logic (runs without FlexRIC) |
| `QUICKSTART.md` | This file |

---

## ✅ Phase 1: State of the Art (Complete)

The document `Phase1_State_of_the_Art.md` covers:

1. **O-RAN Architecture** — SMO, Non-RT RIC, Near-RT RIC, O-DU, O-RU
2. **RIC Details** — Dual-RIC architecture, FlexRIC vs O-RAN SC RIC comparison
3. **E2 Interface** — Subscription/control mechanisms, service models (KPM, RC, Slice)
4. **xApp Model** — Lifecycle, registration, subscription, event loop
5. **Network Slicing** — Concept, RAN-level implementation, isolation definition
6. **Partition Algorithms** — STATIC, NVS-RATE, NVS-CAPACITY with parameters
7. **Testbed Platform** — Open5GS, OAI gNB/UE, FlexRIC, network namespaces
8. **Reference xApp Analysis** — `xapp_slice_moni_ctrl.py` behavior, message structures
9. **Key Findings** — What works, critical issues (unreliable JSON state)
10. **Transition to Phase 2** — Clear objectives based on testbed learnings

---

## ✅ Phase 2: xApp Development (Complete)

The xApp `xapp_slice_isolation.py` implements:

### Features
- ✅ **4 Scenarios**: STATIC_BALANCED, STATIC_UNBALANCED, NVS_RATE_BALANCED, NVS_RATE_UNBALANCED
- ✅ **Two slices per scenario** with algorithm-specific parameters
- ✅ **RNTI Discovery** from KPM (MAC/RLC/PDCP/GTP) reports
- ✅ **Deterministic UE Assignment** (sorted by RNTI → slice 1, slice 2)
- ✅ **ACK Waiting** with 5s timeout for every control command
- ✅ **Measurement Window** (default 30s, configurable) with iperf3 prompts
- ✅ **Cleanup** (DEL + RESET) between scenarios
- ✅ **Dual Logging**: JSONL (machine) + human-readable log files
- ✅ **Graceful Shutdown** (SIGINT/SIGTERM handlers)
- ✅ **Error Recovery** with automatic cleanup on failure

### Scenario Configurations

| Scenario | Algorithm | Slice 1 | Slice 2 |
|----------|-----------|---------|---------|
| STATIC_BALANCED | STATIC | PRB 0-6 (7 PRBs) | PRB 7-13 (7 PRBs) |
| STATIC_UNBALANCED | STATIC | PRB 0-10 (11 PRBs) | PRB 11-13 (3 PRBs) |
| NVS_RATE_BALANCED | NVS-RATE | 9 Mbit/s | 9 Mbit/s |
| NVS_RATE_UNBALANCED | NVS-RATE | 15 Mbit/s | 3 Mbit/s |

---

## 🧪 Verification

Run the logic tests (no FlexRIC needed):

```bash
python3 test_xapp_logic.py
```

Expected output: **ALL TESTS PASSED! ✅**

---

## 🚀 Running on the Testbed

### Prerequisites
- Testbed: RIC Connected, gNB Connected, 2 UEs registered
- FlexRIC Python SDK at `/opt/flexric/python`
- ASN.1 fix: `LD_PRELOAD=/opt/asnfix/libasnfix.so`

### Execute

```bash
# Full experiment (all 4 scenarios, 30s each)
LD_PRELOAD=/opt/asnfix/libasnfix.so python3 -u xapp_slice_isolation.py

# Custom duration
LD_PRELOAD=/opt/asnfix/libasnfix.so python3 -u xapp_slice_isolation.py --duration 40

# Specific scenarios
LD_PRELOAD=/opt/asnfix/libasnfix.so python3 -u xapp_slice_isolation.py \
    --scenarios STATIC_BALANCED STATIC_UNBALANCED
```

### Coordinated iperf3 Measurements

When xApp prints:
```
>>> START IPERF3 MEASUREMENTS NOW <<<
  Server (ue1): sudo ip netns exec ue1 iperf3 -s
  Client (ue2): sudo ip netns exec ue2 iperf3 -c 10.45.0.39 -t 30
```

**Run in separate terminals:**

Terminal 1 (server):
```bash
sudo ip netns exec ue1 iperf3 -s
```

Terminal 2 (client):
```bash
sudo ip netns exec ue2 iperf3 -c 10.45.0.39 -t 30
```

Terminal 3 (monitor):
```bash
tail -f /tmp/xapp_logs/xapp_slice_isolation_*.log
```

---

## 📊 Expected Outputs

### Log Files (per run)
```
xapp_logs/
├── xapp_slice_isolation_YYYYMMDD_HHMMSS.jsonl   # Machine-readable
└── xapp_slice_isolation_YYYYMMDD_HHMMSS.log     # Human-readable
```

### JSONL Event Types
- `COMMAND_SENT` — Control request sent
- `ACK_RECEIVED` — Acknowledgment (success/failure)
- `UE_DISCOVERED` — RNTI found in KPM
- `UE_ASSOCIATED` — UE linked to slice
- `STATE_OBSERVED` — SLICE_STATS (reference only)
- `INFO` / `ERROR` — General events

### Summary Output
```
============================================================
EXPERIMENT SUMMARY
============================================================
  STATIC_BALANCED: SUCCESS
    Slices: ['1(slice_balanced_1)', '2(slice_balanced_2)']
    UE Associations: {'0x1234': 1, '0x5678': 2}
  STATIC_UNBALANCED: SUCCESS
    ...
  NVS_RATE_BALANCED: SUCCESS
    ...
  NVS_RATE_UNBALANCED: SUCCESS
    ...
Logs saved to: /tmp/xapp_logs
```

---

## ⚠️ Critical Notes from Testbed

1. **DO NOT trust `rt_slice_stats.json`** — it doesn't match commands (see Phase 1 doc)
2. **Verify isolation via iperf3** — measure per-UE throughput
3. **Baseline**: 18.6/18.0 Mbit/s (no slicing)
4. **UE IPs**: ue1=10.45.0.39, ue2=10.45.0.40
5. **Total PRBs**: 14 (indices 0-13)

---

## 📋 Phase 3 Preview (Measurements & Analysis)

After Phase 2, you'll have logs for:
- Isolation curves (throughput vs. neighbor load)
- STATIC vs NVS-RATE comparison
- Balanced vs unbalanced comparison
- Multiple repetitions for reproducibility

Graphs to produce:
1. **Isolation curve** — protected slice throughput vs. saturated slice load
2. **Per-slice throughput** — both slices under increasing load

---

## 📞 Support

For issues:
1. Run `python3 test_xapp_logic.py` to verify logic
2. Check testbed prerequisites (RIC, gNB, UEs connected)
3. Verify FlexRIC Python SDK path
4. Check logs in `/tmp/xapp_logs/`

---

*Generated: November 2026*
*Team: Oueslati Ranim, Ben Hedhili Mawadda*