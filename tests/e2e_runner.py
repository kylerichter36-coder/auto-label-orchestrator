#!/usr/bin/env python3
"""
Autonomous Label Orchestrator - Opaque-Box E2E Test Runner
Target Hardware: Orange Pi PC Plus (Allwinner H3, 1GB RAM)
Target Printer: Argox OS-214 Thermal Transfer Printer (203.2 DPI / 8 dots/mm)
Target Scanner: Lexmark MX511 ADF Scanner (Direct Ethernet SMTP:2525)

Test Methodology: 4-Tier Requirement-Driven Opaque-Box Validation
Integrity Standard: Strict Industrial Enterprise Layout, Zero Emojis, Zero Fake Readings
"""

import sys
import os
import time
import argparse
import unittest
from pathlib import Path
from typing import List, Dict, Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import test cases
try:
    from tests.test_opaque_e2e import (
        TestTier1FeatureCoverage,
        TestTier2BoundaryAndCornerCases,
        TestTier3CrossFeaturePairwise,
        TestTier4RealWorldWarehouseWorkloads,
    )
except ImportError:
    from test_opaque_e2e import (
        TestTier1FeatureCoverage,
        TestTier2BoundaryAndCornerCases,
        TestTier3CrossFeaturePairwise,
        TestTier4RealWorldWarehouseWorkloads,
    )


class ScorecardTestResult(unittest.TestResult):
    """Custom TestResult collecting detailed tier and area metrics for the industrial scorecard."""

    def __init__(self, stream=None, descriptions=None, verbosity=None):
        super().__init__(stream, descriptions, verbosity)
        self.test_records: List[Dict[str, Any]] = []

    def startTest(self, test):
        super().startTest(test)
        self._start_time = time.time()

    def addSuccess(self, test):
        super().addSuccess(test)
        duration = time.time() - getattr(self, "_start_time", time.time())
        self.test_records.append({
            "test_id": test.id(),
            "name": test._testMethodName,
            "doc": (test._testMethodDoc or "").strip(),
            "status": "PASS",
            "duration": duration,
            "error": None
        })

    def addFailure(self, test, err):
        super().addFailure(test, err)
        duration = time.time() - getattr(self, "_start_time", time.time())
        self.test_records.append({
            "test_id": test.id(),
            "name": test._testMethodName,
            "doc": (test._testMethodDoc or "").strip(),
            "status": "FAIL",
            "duration": duration,
            "error": self._exc_info_to_string(err, test)
        })

    def addError(self, test, err):
        super().addError(test, err)
        duration = time.time() - getattr(self, "_start_time", time.time())
        self.test_records.append({
            "test_id": test.id(),
            "name": test._testMethodName,
            "doc": (test._testMethodDoc or "").strip(),
            "status": "ERROR",
            "duration": duration,
            "error": self._exc_info_to_string(err, test)
        })

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        duration = time.time() - getattr(self, "_start_time", time.time())
        self.test_records.append({
            "test_id": test.id(),
            "name": test._testMethodName,
            "doc": (test._testMethodDoc or "").strip(),
            "status": "SKIP",
            "duration": duration,
            "error": reason
        })


def categorize_tier_and_area(record: Dict[str, Any]) -> Dict[str, str]:
    """Extracts Tier and Feature Area from test ID and name."""
    test_id = record["test_id"]
    name = record["name"]

    if "TestTier1" in test_id:
        tier = "Tier 1: Feature Coverage"
        if any(k in name for k in ("pplb", "ppla", "coordinate", "barcode", "syntax", "quantity", "outer_box")):
            area = "BarTender / PPLB / PPLA Label Conversion"
        elif any(k in name for k in ("shredding", "pdf", "preview", "disk_write", "mime")):
            area = "In-Memory PDF Rasterization & Shredding"
        elif any(k in name for k in ("web", "status", "index", "setup", "emoji", "telemetry")):
            area = "Enterprise Web UI Endpoints (Port 8080)"
        elif any(k in name for k in ("sd_card", "atomic", "persistent", "audit", "fstab")):
            area = "Non-Volatile SD Card Backup & Persistence"
        elif any(k in name for k in ("autoupdate", "rollback", "snapshot", "preflight", "health")):
            area = "Resilient GitHub Auto-Update State Machine"
        else:
            area = "General Core Features"

    elif "TestTier2" in test_id:
        tier = "Tier 2: Boundary & Corner Cases"
        if any(k in name for k in ("coordinate", "overflow", "quantity", "corrupt", "unencodable")):
            area = "Conversion & Syntax Boundaries"
        elif any(k in name for k in ("zero_byte", "corrupted", "mime", "multipage", "shredding")):
            area = "PDF & Buffer Boundaries"
        elif any(k in name for k in ("web", "json", "out_of_bounds", "wifi", "keys", "bind")):
            area = "Web UI & Network Boundaries"
        elif any(k in name for k in ("power_loss", "erofs", "missing", "pruning", "traversal")):
            area = "SD Card & Storage Boundaries"
        elif any(k in name for k in ("network_drop", "timeout", "http500", "dirty", "pip")):
            area = "Auto-Update Boundaries"
        else:
            area = "General Boundary Cases"

    elif "TestTier3" in test_id:
        tier = "Tier 3: Cross-Feature Pairwise Interactions"
        area = "Cross-Module Pipeline"

    elif "TestTier4" in test_id:
        tier = "Tier 4: Real-World Warehouse Workloads"
        area = "Production Warehouse Scenarios"

    else:
        tier = "Baseline Unit / Integration"
        area = "Core Daemons"

    return {"tier": tier, "area": area}


def print_scorecard(result: ScorecardTestResult, total_duration: float, verbose: bool = False):
    """Renders a structured, high-density industrial scorecard with zero emojis."""
    records = result.test_records

    # Categorize records
    tier_stats: Dict[str, Dict[str, Any]] = {}
    area_stats: Dict[str, Dict[str, Any]] = {}

    for r in records:
        info = categorize_tier_and_area(r)
        tier = info["tier"]
        area = info["area"]

        # Tier breakdown
        if tier not in tier_stats:
            tier_stats[tier] = {"total": 0, "pass": 0, "fail": 0, "error": 0, "skip": 0, "time": 0.0}
        tier_stats[tier]["total"] += 1
        tier_stats[tier]["time"] += r["duration"]
        if r["status"] == "PASS":
            tier_stats[tier]["pass"] += 1
        elif r["status"] == "FAIL":
            tier_stats[tier]["fail"] += 1
        elif r["status"] == "ERROR":
            tier_stats[tier]["error"] += 1
        elif r["status"] == "SKIP":
            tier_stats[tier]["skip"] += 1

        # Area breakdown
        area_key = f"{tier} -> {area}"
        if area_key not in area_stats:
            area_stats[area_key] = {"total": 0, "pass": 0, "fail": 0, "error": 0, "skip": 0}
        area_stats[area_key]["total"] += 1
        if r["status"] == "PASS":
            area_stats[area_key]["pass"] += 1
        elif r["status"] == "FAIL":
            area_stats[area_key]["fail"] += 1
        elif r["status"] == "ERROR":
            area_stats[area_key]["error"] += 1
        elif r["status"] == "SKIP":
            area_stats[area_key]["skip"] += 1

    total_tests = len(records)
    total_passed = sum(1 for r in records if r["status"] == "PASS")
    total_failed = sum(1 for r in records if r["status"] == "FAIL")
    total_errors = sum(1 for r in records if r["status"] == "ERROR")
    total_skipped = sum(1 for r in records if r["status"] == "SKIP")
    pass_rate = (total_passed / max(total_tests, 1)) * 100.0

    sep = "=" * 80
    subsep = "-" * 80

    print("\n" + sep)
    print("  AUTONOMOUS LABEL ORCHESTRATOR - E2E TEST SCORECARD")
    print("  Hardware Target : Orange Pi PC Plus (Armbian trixie, Python 3.11+)")
    print("  Printer Target  : Argox OS-214 (203.2 DPI / 8 dots/mm, /dev/usb/lp0)")
    print("  Scanner Target  : Lexmark MX511 (Direct Ethernet SMTP:2525)")
    print("  Integrity Check : Strict Industrial Enterprise Standard (Zero Emojis)")
    print(sep)

    print("\n[TIER BREAKDOWN]")
    print(f"{'Tier Name':<46} | {'Total':<6} | {'Pass':<6} | {'Fail':<6} | {'Skip':<6} | {'Time (s)':<8}")
    print(subsep)
    for tier, s in tier_stats.items():
        print(f"{tier:<46} | {s['total']:<6} | {s['pass']:<6} | {s['fail']:<6} | {s['skip']:<6} | {s['time']:<8.3f}")

    print("\n[FEATURE AREA DETAIL]")
    print(f"{'Tier / Feature Area':<54} | {'Total':<6} | {'Pass':<6} | {'Fail':<6} | {'Skip':<6}")
    print(subsep)
    for area_key, s in sorted(area_stats.items()):
        area_short = area_key.split(" -> ")[1] if " -> " in area_key else area_key
        tier_short = area_key.split(" -> ")[0].split(":")[0]
        label = f"[{tier_short}] {area_short}"
        print(f"{label:<54} | {s['total']:<6} | {s['pass']:<6} | {s['fail']:<6} | {s['skip']:<6}")

    if verbose:
        print("\n[INDIVIDUAL TEST EXECUTION LOG]")
        print(subsep)
        for r in records:
            badge = f"[{r['status']}]"
            doc_str = f" - {r['doc'][:50]}" if r['doc'] else ""
            print(f"  {badge:<7} {r['name']:<50} ({r['duration']:.3f}s){doc_str}")

    # Failures and errors detail
    failures_and_errors = [r for r in records if r["status"] in ("FAIL", "ERROR")]
    if failures_and_errors:
        print("\n" + sep)
        print(f"  FAILURE & ERROR REPORT ({len(failures_and_errors)} DEFECTS DETECTED)")
        print(sep)
        for idx, item in enumerate(failures_and_errors, 1):
            print(f"\n[{idx}] {item['status']}: {item['test_id']}")
            print(f"    Summary: {item['doc']}")
            if item["error"]:
                # Print indented traceback
                for line in item["error"].splitlines():
                    print(f"    {line}")
        print(sep)

    # Final Overall Summary Banner
    overall_status = "PASS" if (total_failed == 0 and total_errors == 0) else "FAIL"
    print("\n" + sep)
    print(f"  FINAL SUMMARY : [{overall_status}]")
    print(f"  Total Tests   : {total_tests}")
    print(f"  Passed        : {total_passed} ({pass_rate:.1f}%)")
    print(f"  Failed        : {total_failed}")
    print(f"  Errors        : {total_errors}")
    print(f"  Skipped       : {total_skipped}")
    print(f"  Duration      : {total_duration:.3f} seconds")
    print(sep + "\n")


def build_suite(selected_tier: Optional[int] = None) -> unittest.TestSuite:
    """Builds test suite based on selected tier or includes all."""
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()

    if selected_tier == 1:
        suite.addTests(loader.loadTestsFromTestCase(TestTier1FeatureCoverage))
    elif selected_tier == 2:
        suite.addTests(loader.loadTestsFromTestCase(TestTier2BoundaryAndCornerCases))
    elif selected_tier == 3:
        suite.addTests(loader.loadTestsFromTestCase(TestTier3CrossFeaturePairwise))
    elif selected_tier == 4:
        suite.addTests(loader.loadTestsFromTestCase(TestTier4RealWorldWarehouseWorkloads))
    else:
        # All Tiers
        suite.addTests(loader.loadTestsFromTestCase(TestTier1FeatureCoverage))
        suite.addTests(loader.loadTestsFromTestCase(TestTier2BoundaryAndCornerCases))
        suite.addTests(loader.loadTestsFromTestCase(TestTier3CrossFeaturePairwise))
        suite.addTests(loader.loadTestsFromTestCase(TestTier4RealWorldWarehouseWorkloads))

    return suite


def main():
    parser = argparse.ArgumentParser(description="Autonomous Label Orchestrator Opaque-Box E2E Runner")
    parser.add_argument("--tier", type=int, choices=[1, 2, 3, 4], help="Run specific test tier (1 to 4)")
    parser.add_argument("--all", action="store_true", default=True, help="Run all 4 tiers (default)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print verbose individual test logs")
    parser.add_argument("--failfast", "-f", action="store_true", help="Stop suite on first failure/error")
    args = parser.parse_args()

    suite = build_suite(selected_tier=args.tier)
    result = ScorecardTestResult()
    result.failfast = args.failfast

    t0 = time.time()
    suite.run(result)
    total_duration = time.time() - t0

    print_scorecard(result, total_duration, verbose=args.verbose)

    # Return exit code
    if result.failures or result.errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
