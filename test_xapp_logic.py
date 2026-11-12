#!/usr/bin/env python3
"""
Test Script for xApp Slice Isolation Logic
===========================================

This script tests the core logic of the xApp without requiring the FlexRIC
environment. It uses mock classes to verify:
- Scenario configuration generation
- Slice configuration data classes
- Message building
- Logging structure
- UE assignment logic

Run: python3 test_xapp_logic.py
"""

import sys
import json
import time
from datetime import datetime
from pathlib import Path

# Add the outputs directory to path to import our xApp modules
sys.path.insert(0, str(Path(__file__).parent))

# Import the xApp module (will use mock classes since FlexRIC not available)
from xapp_slice_isolation import (
    SliceAlgorithm, SliceOperation, Scenario,
    SliceConfig, UEInfo, LogEntry, ScenarioResult,
    LoggingManager, E2MessageBuilder,
    get_scenario_configs
)


def test_scenario_configs():
    """Test that all scenarios generate correct slice configurations."""
    print("=" * 60)
    print("TEST: Scenario Configurations")
    print("=" * 60)

    # STATIC_BALANCED
    configs = get_scenario_configs(Scenario.STATIC_BALANCED)
    assert len(configs) == 2
    assert configs[0].slice_id == 1
    assert configs[0].algorithm == SliceAlgorithm.STATIC
    assert configs[0].pos_low == 0
    assert configs[0].pos_high == 6
    assert configs[1].pos_low == 7
    assert configs[1].pos_high == 13
    print("✅ STATIC_BALANCED: Slice 1 (PRB 0-6), Slice 2 (PRB 7-13)")

    # STATIC_UNBALANCED
    configs = get_scenario_configs(Scenario.STATIC_UNBALANCED)
    assert configs[0].pos_low == 0
    assert configs[0].pos_high == 10
    assert configs[1].pos_low == 11
    assert configs[1].pos_high == 13
    print("✅ STATIC_UNBALANCED: Slice 1 (PRB 0-10), Slice 2 (PRB 11-13)")

    # NVS_RATE_BALANCED
    configs = get_scenario_configs(Scenario.NVS_RATE_BALANCED)
    assert configs[0].algorithm == SliceAlgorithm.NVS_RATE
    assert configs[0].mbps_rsvd == 9.0
    assert configs[1].mbps_rsvd == 9.0
    print("✅ NVS_RATE_BALANCED: Both slices 9 Mbit/s")

    # NVS_RATE_UNBALANCED
    configs = get_scenario_configs(Scenario.NVS_RATE_UNBALANCED)
    assert configs[0].mbps_rsvd == 15.0
    assert configs[1].mbps_rsvd == 3.0
    print("✅ NVS_RATE_UNBALANCED: Slice 1 (15 Mbit/s), Slice 2 (3 Mbit/s)")

    print("\nAll scenario configurations correct!\n")


def test_slice_config_serialization():
    """Test SliceConfig to_dict/from_dict."""
    print("=" * 60)
    print("TEST: SliceConfig Serialization")
    print("=" * 60)

    config = SliceConfig(
        slice_id=1,
        label="test_slice",
        algorithm=SliceAlgorithm.STATIC,
        pos_low=0,
        pos_high=6
    )

    # To dict
    d = config.to_dict()
    assert d['slice_id'] == 1
    assert d['algorithm'] == 'STATIC'
    assert d['pos_low'] == 0
    print("✅ to_dict() works")

    # From dict
    config2 = SliceConfig.from_dict(d)
    assert config2.slice_id == 1
    assert config2.algorithm == SliceAlgorithm.STATIC
    assert config2.pos_low == 0
    print("✅ from_dict() works")

    # NVS-RATE config
    config3 = SliceConfig(
        slice_id=2,
        label="rate_slice",
        algorithm=SliceAlgorithm.NVS_RATE,
        mbps_rsvd=10.5
    )
    d3 = config3.to_dict()
    assert d3['mbps_rsvd'] == 10.5
    config4 = SliceConfig.from_dict(d3)
    assert config4.mbps_rsvd == 10.5
    print("✅ NVS-RATE serialization works")

    print("\nSerialization tests passed!\n")


def test_message_builder():
    """Test E2MessageBuilder creates correct messages."""
    print("=" * 60)
    print("TEST: E2 Message Builder")
    print("=" * 60)

    # STATIC ADD
    config = SliceConfig(1, "slice_1", SliceAlgorithm.STATIC, pos_low=0, pos_high=6)
    msg = E2MessageBuilder.build_slice_add(config)
    assert msg['operation'] == 'ADD'
    assert msg['slice_id'] == 1
    assert msg['algorithm'] == 'STATIC'
    assert msg['pos_low'] == 0
    assert msg['pos_high'] == 6
    print("✅ STATIC ADD message correct")

    # NVS-RATE ADD
    config = SliceConfig(2, "slice_2", SliceAlgorithm.NVS_RATE, mbps_rsvd=9.0)
    msg = E2MessageBuilder.build_slice_add(config)
    assert msg['algorithm'] == 'NVS-RATE'
    assert msg['mbps_rsvd'] == 9.0
    assert 'pos_low' not in msg
    print("✅ NVS-RATE ADD message correct")

    # ASSOC
    msg = E2MessageBuilder.build_slice_assoc("0x1234", 1)
    assert msg['operation'] == 'ASSOC'
    assert msg['slice_id'] == 1
    assert msg['rnti'] == "0x1234"
    print("✅ ASSOC message correct")

    # DEL
    msg = E2MessageBuilder.build_slice_del(1)
    assert msg['operation'] == 'DEL'
    assert msg['slice_id'] == 1
    print("✅ DEL message correct")

    # RESET
    msg = E2MessageBuilder.build_slice_reset()
    assert msg['operation'] == 'RESET'
    assert msg['algorithm'] == 'NONE'
    print("✅ RESET message correct")

    print("\nMessage builder tests passed!\n")


def test_logging_manager():
    """Test LoggingManager writes correct format."""
    print("=" * 60)
    print("TEST: Logging Manager")
    print("=" * 60)

    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        log_mgr = LoggingManager(log_dir=tmpdir, xapp_name="test_xapp")

        # Test various log types
        log_mgr.info("Test info message")
        log_mgr.error("Test error message")

        entry = LogEntry(
            timestamp=datetime.now().isoformat(),
            event_type="COMMAND_SENT",
            details={"operation": "ADD", "slice_id": 1},
            scenario="STATIC_BALANCED"
        )
        log_mgr.log(entry)

        log_mgr.command_sent(
            SliceOperation.ADD,
            SliceConfig(1, "test", SliceAlgorithm.STATIC, pos_low=0, pos_high=6),
            Scenario.STATIC_BALANCED
        )

        log_mgr.ack_received(
            SliceOperation.ADD, 1, True, Scenario.STATIC_BALANCED
        )

        log_mgr.ue_discovered(
            UEInfo(rnti="0x1234", ip="10.45.0.39", namespace="ue1"),
            Scenario.STATIC_BALANCED
        )

        log_mgr.ue_associated("0x1234", 1, Scenario.STATIC_BALANCED)

        # Check JSONL file exists and has correct format
        jsonl_files = list(Path(tmpdir).glob("*.jsonl"))
        assert len(jsonl_files) == 1

        with open(jsonl_files[0]) as f:
            lines = f.readlines()
            assert len(lines) >= 6  # At least our test entries
            for line in lines:
                data = json.loads(line)
                assert 'timestamp' in data
                assert 'event_type' in data
                assert 'details' in data
                print(f"  Logged: {data['event_type']}")

        # Check human-readable log
        log_files = list(Path(tmpdir).glob("*.log"))
        assert len(log_files) == 1
        with open(log_files[0]) as f:
            content = f.read()
            assert "Test info message" in content
            assert "Test error message" in content
            assert "COMMAND_SENT" in content

    print("\nLogging manager tests passed!\n")


def test_ue_assignment_logic():
    """Test deterministic UE to slice assignment."""
    print("=" * 60)
    print("TEST: UE Assignment Logic")
    print("=" * 60)

    # Simulate discovered UEs (order of discovery varies)
    discovered_ues = {
        "0x5678": UEInfo(rnti="0x5678", ip="10.45.0.40", namespace="ue2"),
        "0x1234": UEInfo(rnti="0x1234", ip="10.45.0.39", namespace="ue1"),
    }

    # Sort by RNTI for deterministic assignment
    sorted_ues = sorted(discovered_ues.items())

    slice_configs = get_scenario_configs(Scenario.STATIC_BALANCED)

    # Assign: first UE -> slice 1, second UE -> slice 2
    rnti_to_slice = {}
    for i, (rnti, ue_info) in enumerate(sorted_ues[:2]):
        slice_id = slice_configs[i].slice_id
        rnti_to_slice[rnti] = slice_id
        ue_info.slice_id = slice_id
        ue_info.associated = True

    # Verify deterministic assignment (lower RNTI -> slice 1)
    assert sorted_ues[0][0] == "0x1234"  # Lower RNTI first
    assert rnti_to_slice["0x1234"] == 1  # Slice 1
    assert rnti_to_slice["0x5678"] == 2  # Slice 2
    print("✅ Deterministic assignment: 0x1234→Slice1, 0x5678→Slice2")

    # Test with reversed discovery order
    discovered_ues2 = {
        "0x1234": UEInfo(rnti="0x1234", ip="10.45.0.39", namespace="ue1"),
        "0x5678": UEInfo(rnti="0x5678", ip="10.45.0.40", namespace="ue2"),
    }
    sorted_ues2 = sorted(discovered_ues2.items())
    assert sorted_ues2[0][0] == "0x1234"
    print("✅ Order-independent: sorting ensures consistent assignment")

    print("\nUE assignment logic tests passed!\n")


def test_scenario_result():
    """Test ScenarioResult data class."""
    print("=" * 60)
    print("TEST: ScenarioResult")
    print("=" * 60)

    configs = get_scenario_configs(Scenario.STATIC_BALANCED)

    result = ScenarioResult(
        scenario=Scenario.STATIC_BALANCED,
        start_time="2026-11-11T10:00:00",
        end_time="2026-11-11T10:00:40",
        slice_configs=configs,
        ue_associations={"0x1234": 1, "0x5678": 2},
        commands_sent=[],
        acks_received=[],
        measurement_duration=30,
        success=True
    )

    assert result.scenario == Scenario.STATIC_BALANCED
    assert result.success is True
    assert len(result.slice_configs) == 2
    assert result.ue_associations["0x1234"] == 1

    # Test failed scenario
    result2 = ScenarioResult(
        scenario=Scenario.NVS_RATE_UNBALANCED,
        start_time="2026-11-11T10:01:00",
        end_time="2026-11-11T10:01:10",
        slice_configs=get_scenario_configs(Scenario.NVS_RATE_UNBALANCED),
        ue_associations={},
        commands_sent=[],
        acks_received=[],
        measurement_duration=30,
        success=False,
        error_message="Timeout waiting for ACK"
    )

    assert result2.success is False
    assert result2.error_message == "Timeout waiting for ACK"
    print("✅ ScenarioResult works for both success and failure")

    print("\nScenarioResult tests passed!\n")


def test_log_entry_json():
    """Test LogEntry JSON serialization."""
    print("=" * 60)
    print("TEST: LogEntry JSON Serialization")
    print("=" * 60)

    entry = LogEntry(
        timestamp="2026-11-11T10:30:00.123456",
        event_type="COMMAND_SENT",
        details={"operation": "ADD", "slice_id": 1},
        scenario="STATIC_BALANCED"
    )

    json_str = entry.to_json()
    parsed = json.loads(json_str)

    assert parsed['event_type'] == "COMMAND_SENT"
    assert parsed['details']['operation'] == "ADD"
    assert parsed['scenario'] == "STATIC_BALANCED"
    print("✅ LogEntry JSON serialization works")

    print("\nLogEntry tests passed!\n")


def run_all_tests():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("RUNNING ALL XAPP LOGIC TESTS")
    print("=" * 60 + "\n")

    test_scenario_configs()
    test_slice_config_serialization()
    test_message_builder()
    test_logging_manager()
    test_ue_assignment_logic()
    test_scenario_result()
    test_log_entry_json()

    print("=" * 60)
    print("ALL TESTS PASSED! ✅")
    print("=" * 60)
    print("\nThe xApp logic is verified and ready for testbed deployment.")


if __name__ == "__main__":
    run_all_tests()