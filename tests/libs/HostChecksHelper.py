"""Robot library exposing Chronicle's host-level health checks.

Container-independent, like ConfigTestHelper: the checks shell out to tools that
may be absent and report ``not_applicable`` when they are, so this suite is
meaningful both in CI (nothing configured — everything n/a) and on a real
deployment (everything ok). That is the point — it must never be a test that only
runs on one machine.

The checks themselves are unit-tested in ``extras/chronicle-setup/tests``. What
this adds is the wiring: that the registry is reachable, returns well-formed
results, and reports nothing broken on whatever host the suite runs on.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from chronicle_setup import checks  # noqa: E402


class HostChecksHelper:
    ROBOT_LIBRARY_SCOPE = "SUITE"

    def __init__(self):
        self._results = None

    def run_host_checks(self):
        """Run every registered check and return them as a list of dicts."""
        # A bare context: no containers, no HTTPS host. Everything that needs the
        # deployment reports not_applicable, which is exactly what CI should see.
        # services.py builds the fully-populated context for real use.
        self._results = checks.run_all_checks(checks.CheckContext())
        return [r.as_dict() for r in self._results]

    def get_failing_checks(self):
        """Checks reporting an outright failure. Warnings are not included."""
        if self._results is None:
            self.run_host_checks()
        return [r.as_dict() for r in self._results if r.status == checks.FAIL]

    def get_check_ids(self):
        if self._results is None:
            self.run_host_checks()
        return [r.id for r in self._results]

    def get_registered_check_count(self):
        return len(checks.ALL_CHECKS)

    def get_valid_statuses(self):
        return [checks.OK, checks.WARN, checks.FAIL, checks.NOT_APPLICABLE]
