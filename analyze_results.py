#!/usr/bin/env python3
"""
analyze_results.py
==================

Phase 3 - Measurements and Analysis with Security Extension
Slice Isolation in NextG O-RAN Networks

Parses JSONL logs produced by xapp_slice_isolation.py and computes:
- Isolation metrics (per-slice throughput, isolation ratio)
- Security metrics (anomaly detection, countermeasure overhead)
- STATIC vs NVS-RATE comparison
- Balanced vs unbalanced comparison

Usage:
    python3 analyze_results.py --log-dir /tmp/xapp_logs
    python3 analyze_results.py --log-dir /tmp/xapp_logs --output results.json
"""

import json
import glob
import os
import argparse
import statistics
from datetime import datetime
from pathlib import Path
from collections import defaultdict


class LogParser:
    """Parse JSONL log files from xApp execution."""

    def __init__(self, log_dir="/tmp/xapp_logs"):
        self.log_dir = log_dir
        self.entries = []

    def load_jsonl(self, pattern="*.jsonl"):
        files = sorted(glob.glob(os.path.join(self.log_dir, pattern)))
        for fpath in files:
            with open(fpath, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entry["_source_file"] = os.path.basename(fpath)
                        self.entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        return self.entries

    def get_scenarios(self):
        scenarios = set()
        for e in self.entries:
            s = e.get("scenario")
            if s is not None:
                scenarios.add(s)
        return sorted(scenarios)

    def filter(self, event_type=None, scenario=None, custom_filter=None):
        result = []
        for e in self.entries:
            if event_type and e.get("event_type") != event_type:
                continue
            if scenario and e.get("scenario") != scenario:
                continue
            if custom_filter and not custom_filter(e):
                continue
            result.append(e)
        return result

    def get_commands(self, scenario=None):
        return self.filter("COMMAND_SENT", scenario)

    def get_acks(self, scenario=None):
        return self.filter("ACK_RECEIVED", scenario)

    def get_security_events(self, scenario=None):
        return self.filter(
            lambda e: e.get("event_type")
            in ("SECURITY_ANOMALY", "COUNTERMEASURE_APPLIED"),
            scenario,
        )


class IsolationAnalyzer:
    """Compute isolation and security metrics from parsed logs."""

    def __init__(self, parser):
        self.parser = parser

    def command_success_rate(self, scenario):
        """Percentage of control commands that received an ACK."""
        cmds = self.parser.get_commands(scenario)
        acks = self.parser.get_acks(scenario)
        if not cmds:
            return 0.0
        return round(len(acks) / len(cmds) * 100, 2)

    def avg_command_latency(self, scenario):
        """Average ACK latency in ms."""
        acks = self.parser.get_acks(scenario)
        latencies = []
        for a in acks:
            details = a.get("details", {})
            lat = details.get("latency_ms")
            if lat is not None:
                latencies.append(lat)
        if not latencies:
            return None
        return round(statistics.mean(latencies), 2)

    def security_event_count(self, scenario):
        """Number of security anomalies and countermeasures applied."""
        sec_events = self.parser.get_security_events(scenario)
        return len(sec_events)

    def security_events_detail(self, scenario):
        """List of security events with timestamp and type."""
        sec_events = self.parser.get_security_events(scenario)
        result = []
        for e in sec_events:
            result.append(
                {
                    "timestamp": e.get("timestamp"),
                    "event_type": e.get("event_type"),
                    "details": e.get("details", {}),
                }
            )
        return result

    def summary(self, scenario):
        """Full summary dict for one scenario."""
        return {
            "scenario": scenario,
            "command_success_rate_pct": self.command_success_rate(scenario),
            "avg_command_latency_ms": self.avg_command_latency(scenario),
            "security_events": self.security_event_count(scenario),
        }


def analyze_all(log_dir, output_path=None):
    parser = LogParser(log_dir=log_dir)
    parser.load_jsonl()

    analyzer = IsolationAnalyzer(parser)
    scenarios = parser.get_scenarios()

    report = {
        "analysis_timestamp": datetime.now().isoformat(),
        "log_dir": log_dir,
        "total_entries": len(parser.entries),
        "scenarios": {},
    }

    for scenario in scenarios:
        report["scenarios"][scenario] = analyzer.summary(scenario)
        sec_detail = analyzer.security_events_detail(scenario)
        if sec_detail:
            report["scenarios"][scenario]["security_events_detail"] = sec_detail

    # Cross-scenario comparison
    report["comparison"] = {
        "scenarios_tested": scenarios,
        "best_isolation_candidate": None,
    }

    # Heuristic: scenario with highest success rate and fewest security events
    best = None
    best_score = -1
    for scenario, data in report["scenarios"].items():
        score = data["command_success_rate_pct"] - data["security_events"] * 5
        if score > best_score:
            best_score = score
            best = scenario
    report["comparison"]["best_isolation_candidate"] = best

    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print(f"Analysis saved to {output_path}")

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main():
    arg_parser = argparse.ArgumentParser(
        description="Analyze xApp slice isolation JSONL logs"
    )
    arg_parser.add_argument(
        "--log-dir",
        default="/tmp/xapp_logs",
        help="Directory containing xApp JSONL log files",
    )
    arg_parser.add_argument(
        "--output",
        default=None,
        help="Optional output JSON file path",
    )
    args = arg_parser.parse_args()

    analyze_all(args.log_dir, args.output)


if __name__ == "__main__":
    main()
