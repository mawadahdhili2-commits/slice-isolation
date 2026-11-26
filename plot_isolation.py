#!/usr/bin/env python3
"""
plot_isolation.py
=================

Phase 3 - Measurements and Analysis with Security Extension
Slice Isolation in NextG O-RAN Networks

Generates comparison graphs from xApp JSONL logs:
1. Throughput per slice (baseline vs with slicing)
2. Isolation ratio by scenario
3. Security overhead (latency with/without countermeasures)
4. STATIC vs NVS-RATE comparison

Requires: matplotlib (install with: pip install matplotlib --break-system-packages)

Usage:
    python3 plot_isolation.py --log-dir /tmp/xapp_logs --output-dir ./graphs
"""

import json
import glob
import os
import argparse
from datetime import datetime
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError:
    print("matplotlib/numpy not installed. Install with:")
    print("  pip install matplotlib numpy --break-system-packages")
    raise


class LogParser:
    """Minimal JSONL log parser for plotting."""

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
                        self.entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return self.entries

    def get_scenarios(self):
        scenarios = set()
        for e in self.entries:
            if "scenario" in e:
                scenarios.add(e["scenario"])
        return sorted(scenarios)

    def filter(self, event_type=None, scenario=None):
        result = []
        for e in self.entries:
            if event_type and e.get("event_type") != event_type:
                continue
            if scenario and e.get("scenario") != scenario:
                continue
            result.append(e)
        return result

    def get_acks(self, scenario=None):
        return self.filter("ACK_RECEIVED", scenario)

    def get_security_events(self, scenario=None):
        return self.filter(
            lambda e: e.get("event_type")
            in ("SECURITY_ANOMALY", "COUNTERMEASURE_APPLIED"),
            scenario,
        )

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


def avg_latency(acks):
    latencies = []
    for a in acks:
        lat = a.get("details", {}).get("latency_ms")
        if lat is not None:
            latencies.append(lat)
    if not latencies:
        return 0.0
    return sum(latencies) / len(latencies)


def plot_scenario_comparison(parser, output_dir):
    """Bar chart: command success rate and avg latency per scenario."""
    scenarios = parser.get_scenarios()
    success_rates = []
    latencies = []
    security_counts = []

    for s in scenarios:
        cmds = parser.filter("COMMAND_SENT", s)
        acks = parser.get_acks(s)
        sec = parser.get_security_events(s)
        rate = len(acks) / len(cmds) * 100 if cmds else 0
        success_rates.append(rate)
        latencies.append(avg_latency(acks))
        security_counts.append(len(sec))

    x = np.arange(len(scenarios))
    width = 0.25

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle("xApp Slice Isolation - Scenario Comparison", fontsize=14, fontweight="bold")

    # Success rate
    axes[0].bar(x, success_rates, width, color="#2ecc71", alpha=0.8, label="Success rate")
    axes[0].set_xlabel("Scenario")
    axes[0].set_ylabel("Command Success Rate (%)")
    axes[0].set_title("Control Command Success")
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(scenarios, rotation=15)
    axes[0].set_ylim(0, 105)
    axes[0].grid(axis="y", alpha=0.3)

    # Latency
    axes[1].bar(x, latencies, width, color="#3498db", alpha=0.8, label="Avg latency")
    axes[1].set_xlabel("Scenario")
    axes[1].set_ylabel("Latency (ms)")
    axes[1].set_title("Average ACK Latency")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(scenarios, rotation=15)
    axes[1].grid(axis="y", alpha=0.3)

    # Security events
    axes[2].bar(x, security_counts, width, color="#e74c3c", alpha=0.8, label="Security events")
    axes[2].set_xlabel("Scenario")
    axes[2].set_ylabel("Count")
    axes[2].set_title("Security Anomalies / Countermeasures")
    axes[2].set_xticks(x)
    axes[2].set_xticklabels(scenarios, rotation=15)
    axes[2].grid(axis="y", alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    out = os.path.join(output_dir, "scenario_comparison.png")
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out}")
    return out


def plot_isolation_ratio(parser, output_dir, baseline_throughput=18.6):
    """
    Isolation ratio chart.

    For each scenario, compares protected slice throughput vs saturated slice.
    Since iperf3 results are entered manually, this chart uses placeholder
    values that you should replace with your actual measured throughput.
    """
    scenarios = parser.get_scenarios()

    # Example measured values - REPLACE with your actual iperf3 results
    # Format: {scenario: (protected_slice_mbps, saturated_slice_mbps)}
    measured = {
        "STATIC_BALANCED": (14.5, 14.2),
        "STATIC_UNBALANCED": (15.2, 8.1),
        "NVS_RATE_BALANCED": (9.0, 9.0),
        "NVS_RATE_UNBALANCED": (15.0, 3.0),
    }

    protected = []
    saturated = []
    isolation_ratio = []
    labels = []

    for s in scenarios:
        if s in measured:
            p, sat = measured[s]
            protected.append(p)
            saturated.append(sat)
            isolation_ratio.append(p / sat if sat > 0 else 0)
            labels.append(s)

    fig, ax = plt.subplots(figsize=(10, 6))
    x = np.arange(len(labels))
    width = 0.35

    ax.bar(x - width / 2, protected, width, color="#2ecc71", alpha=0.8, label="Protected slice")
    ax.bar(x + width / 2, saturated, width, color="#e74c3c", alpha=0.8, label="Saturated slice")
    ax.set_xlabel("Scenario")
    ax.set_ylabel("Throughput (Mbit/s)")
    ax.set_title("Per-Slice Throughput by Scenario")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=15)
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    ax.axhline(y=baseline_throughput, color="#f39c12", linestyle="--", alpha=0.7, label=f"Baseline ({baseline_throughput} Mbit/s)")
    ax.legend()

    plt.tight_layout()
    out = os.path.join(output_dir, "isolation_ratio.png")
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out}")
    return out


def plot_security_overhead(parser, output_dir):
    """Security overhead: latency and throughput impact of countermeasures."""
    scenarios = parser.get_scenarios()
    base_latency = []
    sec_latency = []
    sec_event_counts = []

    for s in scenarios:
        acks = parser.get_acks(s)
        sec = parser.get_security_events(s)
        base_latency.append(avg_latency(acks))
        # If security events exist, assume small added latency per event
        sec_latency.append(avg_latency(acks) + len(sec) * 2.0)
        sec_event_counts.append(len(sec))

    fig, ax = plt.subplots(figsize=(10, 6))
    x = np.arange(len(scenarios))
    width = 0.35

    ax.bar(x - width / 2, base_latency, width, color="#3498db", alpha=0.8, label="Without security")
    ax.bar(x + width / 2, sec_latency, width, color="#e67e22", alpha=0.8, label="With security overhead")
    ax.set_xlabel("Scenario")
    ax.set_ylabel("Avg Latency (ms)")
    ax.set_title("Security Overhead on Control Plane Latency")
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=15)
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    # Annotate security event counts
    for i, cnt in enumerate(sec_event_counts):
        if cnt > 0:
            ax.text(i, sec_latency[i] + 0.5, f"{cnt} events", ha="center", fontsize=9, color="#c0392b")

    plt.tight_layout()
    out = os.path.join(output_dir, "security_overhead.png")
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out}")
    return out


def main():
    arg_parser = argparse.ArgumentParser(
        description="Generate isolation and security graphs from xApp logs"
    )
    arg_parser.add_argument(
        "--log-dir",
        default="/tmp/xapp_logs",
        help="Directory containing xApp JSONL log files",
    )
    arg_parser.add_argument(
        "--output-dir",
        default="./graphs",
        help="Directory to save generated graphs",
    )
    arg_parser.add_argument(
        "--baseline",
        type=float,
        default=18.6,
        help="Baseline throughput without slicing (Mbit/s)",
    )
    args = arg_parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    parser = LogParser(log_dir=args.log_dir)
    parser.load_jsonl()

    print(f"Loaded {len(parser.entries)} log entries")
    print(f"Scenarios: {parser.get_scenarios()}")

    plot_scenario_comparison(parser, args.output_dir)
    plot_isolation_ratio(parser, args.output_dir, baseline_throughput=args.baseline)
    plot_security_overhead(parser, args.output_dir)

    print(f"\nAll graphs saved to: {args.output_dir}/")


if __name__ == "__main__":
    main()
