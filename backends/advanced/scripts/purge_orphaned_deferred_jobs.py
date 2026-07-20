"""Manual cleanup: delete orphaned deferred RQ jobs.

A deferred job is "orphaned" when nothing will ever promote it: none of its
dependencies is still pending (every dependency is either missing from Redis —
evicted/deleted — or already terminal). The detection + deletion logic lives in
:mod:`advanced_omi_backend.services.job_reaper` and is shared with the periodic
backstop reaper (services/reaper.py); this is just a CLI wrapper for an on-demand
sweep.

Usage (run where the backend package is importable):
    python scripts/purge_orphaned_deferred_jobs.py            # dry run
    python scripts/purge_orphaned_deferred_jobs.py --delete   # actually delete
"""

import sys

from advanced_omi_backend.services.job_reaper import (
    find_orphaned_deferred_jobs,
    reap_orphaned_deferred_jobs,
)


def main() -> int:
    if "--delete" not in sys.argv:
        orphans = find_orphaned_deferred_jobs()
        print(f"Found {len(orphans)} orphaned deferred job(s):")
        for queue_name, job_id, conv, reason in orphans:
            print(f"  [{queue_name}] {job_id} conv={conv} :: {reason}")
        print(
            "\nDRY RUN — re-run with --delete to remove them (deletion cascades "
            "through dependent chains)."
        )
        return 0

    result = reap_orphaned_deferred_jobs()
    for d in result["details"]:
        print(f"  deleted [{d['queue']}] {d['job_id']} conv={d['conversation_id']}")
    print(f"\nDeleted {result['deleted']} orphaned deferred job(s) total.")
    remaining = find_orphaned_deferred_jobs()
    if remaining:
        print(f"WARNING: {len(remaining)} orphan(s) still remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
