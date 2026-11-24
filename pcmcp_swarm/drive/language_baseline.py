"""Run the existing language baselines in parallel with fixed test entry points."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import subprocess
import sys


def run(entry):
    label, project, args = entry
    try:
        result = subprocess.run([sys.executable, "-B", "-m", "unittest", *args],
                                cwd=Path.home() / "Documents" / project,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return label, 1, "", str(exc), "could not complete within the runner limits"
    match = re.search(r"Ran (\d+) tests?", result.stderr)
    count = match.group(1) if match else "unknown number of"
    skipped = re.search(r"skipped=(\d+)", result.stderr)
    status = "passed" if result.returncode == 0 else "failed"
    detail = f", {skipped.group(1)} skipped" if skipped else ""
    return label, result.returncode, result.stdout, result.stderr, f"{count} tests, {status}{detail}"


if __name__ == "__main__":
    entries = (
        ("Composition", "language-composition", ("discover", "-s", "Tests", "-p", "test_language_composition.py")),
        ("Experiment", "experimental-ai", ("tests.test_language_lessons",)),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, entries))
    for label, code, stdout, stderr, summary in results:
        print(f"{label}:\n{stdout}\n{stderr}\nRESULT {label}: {summary}")
    sys.exit(any(code != 0 for _, code, *_ in results))
