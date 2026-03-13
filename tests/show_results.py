#!/usr/bin/env python3
"""
Chronicle Test Results Viewer

Quick command-line tool to view Robot Framework test results.
SSH-friendly with multiple display modes.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET


# ANSI color codes
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def colorize(text, color):
    """Add color to text if stdout is a TTY"""
    if sys.stdout.isatty():
        return f"{color}{text}{Colors.RESET}"
    return text


def find_latest_results(result_dir=None):
    """
    Find the most recent output.xml file.

    Args:
        result_dir: Specific directory to search, or None to search all

    Returns:
        Path to output.xml, or None if not found
    """
    tests_dir = Path(__file__).parent

    if result_dir:
        # Search specific directory
        search_path = tests_dir / result_dir / "output.xml"
        if search_path.exists():
            return search_path
        return None

    # Search all result directories
    result_dirs = [
        tests_dir / "results",
        tests_dir / "results-no-api",
        tests_dir / "results-slow",
        tests_dir / "results-sdk",
    ]

    # Find all output.xml files
    xml_files = []
    for result_dir in result_dirs:
        xml_path = result_dir / "output.xml"
        if xml_path.exists():
            xml_files.append(xml_path)

    if not xml_files:
        return None

    # Return most recently modified
    return max(xml_files, key=lambda p: p.stat().st_mtime)


def parse_results(xml_file):
    """
    Parse Robot Framework output.xml file.

    Returns:
        dict with keys: passed, failed, skipped, total, duration_seconds,
                       failed_tests (list of dicts with name, suite, error, duration)
    """
    tree = ET.parse(xml_file)
    root = tree.getroot()

    # Get statistics
    stats = root.find(".//statistics/total/stat")
    if stats is None:
        return None

    passed = int(stats.get("pass", 0))
    failed = int(stats.get("fail", 0))
    skipped = int(stats.get("skip", 0))
    total = passed + failed + skipped

    # Get suite duration from robot element
    robot_elem = root.find("./suite")
    status_elem = robot_elem.find("./status") if robot_elem is not None else None

    duration_seconds = 0
    if status_elem is not None and status_elem.get("elapsed"):
        # Duration is in milliseconds
        elapsed_ms = status_elem.get("elapsed")
        try:
            duration_seconds = int(elapsed_ms) / 1000.0
        except (ValueError, TypeError):
            pass

    # Find failed tests
    failed_tests = []
    for test in root.findall(".//test"):
        status = test.find("./status")
        if status is not None and status.get("status") == "FAIL":
            # Get suite path
            suite_path = []
            parent = test
            while True:
                parent = find_parent_suite(root, parent)
                if parent is None:
                    break
                suite_name = parent.get("name")
                if suite_name:
                    suite_path.insert(0, suite_name)

            # Get error message
            error_msg = status.text or "No error message"

            # Get test duration
            test_duration = 0
            if status.get("elapsed"):
                try:
                    test_duration = int(status.get("elapsed")) / 1000.0
                except (ValueError, TypeError):
                    pass

            failed_tests.append(
                {
                    "name": test.get("name"),
                    "suite": " > ".join(suite_path) if suite_path else "Unknown Suite",
                    "error": error_msg.strip(),
                    "duration": test_duration,
                }
            )

    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "duration_seconds": duration_seconds,
        "failed_tests": failed_tests,
    }


def find_parent_suite(root, element):
    """Find parent suite of an element"""
    for suite in root.findall(".//suite"):
        for child in suite:
            if child == element:
                return suite
            # Check nested tests
            for test in suite.findall(".//test"):
                if test == element:
                    return suite
    return None


def format_duration(seconds):
    """Format duration in human-readable format"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds / 3600)
        minutes = int((seconds % 3600) / 60)
        return f"{hours}h {minutes}m"


def print_summary(results, xml_file, show_detailed=False):
    """Print terminal summary of test results"""

    # Header
    print()
    print(colorize("Chronicle Test Results", Colors.BOLD))
    print("=" * 50)
    print()

    # File info
    file_mtime = datetime.fromtimestamp(xml_file.stat().st_mtime)
    print(
        f"Results from: {colorize(str(xml_file.relative_to(Path.cwd())), Colors.BLUE)}"
    )
    print(f"Last modified: {file_mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total duration: {format_duration(results['duration_seconds'])}")
    print()

    # Statistics
    print(colorize("Test Statistics:", Colors.BOLD))
    print(
        f"  {colorize('✓', Colors.GREEN)} Passed:  {colorize(str(results['passed']), Colors.GREEN)}"
    )
    print(
        f"  {colorize('✗', Colors.RED)} Failed:  {colorize(str(results['failed']), Colors.RED)}"
    )
    if results["skipped"] > 0:
        print(
            f"  {colorize('○', Colors.YELLOW)} Skipped: {colorize(str(results['skipped']), Colors.YELLOW)}"
        )
    print(f"  Total:    {results['total']}")

    if results["total"] > 0:
        pass_rate = (results["passed"] / results["total"]) * 100
        color = (
            Colors.GREEN
            if pass_rate == 100
            else Colors.YELLOW if pass_rate >= 90 else Colors.RED
        )
        print(f"  Pass rate: {colorize(f'{pass_rate:.1f}%', color)}")
    print()

    # Failed tests
    if results["failed"] > 0:
        print(colorize(f"Failed Tests ({results['failed']}):", Colors.BOLD))
        print()

        for i, test in enumerate(results["failed_tests"], 1):
            print(
                f"{colorize(f'{i}.', Colors.RED)} {colorize(test['name'], Colors.BOLD)}"
            )
            print(f"   Suite: {test['suite']}")

            if show_detailed:
                # Show full error in detailed mode
                print(f"   Error: {test['error']}")
                print(f"   Duration: {format_duration(test['duration'])}")
            else:
                # Truncate error in summary mode
                error_lines = test["error"].split("\n")
                first_line = error_lines[0]
                if len(first_line) > 100:
                    first_line = first_line[:97] + "..."
                print(f"   Error: {first_line}")
                if len(error_lines) > 1:
                    print(
                        f"   {colorize('(Use --detailed for full error)', Colors.YELLOW)}"
                    )
            print()
    else:
        print(colorize("🎉 All tests passed!", Colors.GREEN))
        print()


def print_report_path(xml_file):
    """Print paths to HTML reports"""
    result_dir = xml_file.parent
    report_file = result_dir / "report.html"
    log_file = result_dir / "log.html"

    print()
    print(colorize("Test Report Files:", Colors.BOLD))
    print()

    if report_file.exists():
        print(f"HTML report: {colorize(str(report_file.absolute()), Colors.BLUE)}")
    else:
        print(f"HTML report: {colorize('Not found', Colors.RED)}")

    if log_file.exists():
        print(f"Detailed log: {colorize(str(log_file.absolute()), Colors.BLUE)}")
    else:
        print(f"Detailed log: {colorize('Not found', Colors.RED)}")

    print()

    if report_file.exists():
        print(colorize("To view reports:", Colors.BOLD))
        print(f"  1. Copy to local machine: scp user@server:{report_file.absolute()} .")
        print(f"  2. Open in browser: file://{report_file.absolute()}")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="View Chronicle Robot Framework test results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Quick summary (default, SSH-friendly)
  %(prog)s --path            # Print path to HTML report
  %(prog)s --detailed        # Detailed terminal output with full errors
  %(prog)s --dir results-no-api  # Specific result directory
        """,
    )

    parser.add_argument(
        "--dir",
        help="Specific result directory to use (default: auto-find most recent)",
    )

    parser.add_argument(
        "--path",
        action="store_true",
        help="Print path to HTML report instead of showing summary",
    )

    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Show detailed output with full error messages",
    )

    args = parser.parse_args()

    # Find results file
    xml_file = find_latest_results(args.dir)

    if xml_file is None:
        if args.dir:
            print(
                colorize(
                    f"Error: No test results found in '{args.dir}' directory",
                    Colors.RED,
                )
            )
        else:
            print(colorize("Error: No test results found", Colors.RED))
        print()
        print("Run tests first:")
        print("  make test        # Full test run")
        print("  make test-quick  # Quick test on existing containers")
        print()
        return 1

    # Show path mode
    if args.path:
        print_report_path(xml_file)
        return 0

    # Parse results
    try:
        results = parse_results(xml_file)
        if results is None:
            print(colorize("Error: Could not parse test results", Colors.RED))
            print(f"File: {xml_file}")
            return 1
    except Exception as e:
        print(colorize(f"Error parsing results: {e}", Colors.RED))
        print(f"File: {xml_file}")
        return 1

    # Show summary
    print_summary(results, xml_file, show_detailed=args.detailed)

    # Return exit code based on test results
    return 1 if results["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
