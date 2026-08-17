# Screenpipe fork sync

Chronicle consumes the `chronicle` branch of
[`AnkushMalaker/screenpipe`](https://github.com/AnkushMalaker/screenpipe). It is
a single patch stack rebased directly onto `screenpipe/screenpipe`'s `main`.
The older `custom/linux-timeline-stability` intermediate branch no longer
exists; its commits were folded into `chronicle` in August 2026.

## Automated sync

`.github/workflows/sync-screenpipe-fork.yml` runs daily and can also be started
manually. It:

1. checks out the fork's `chronicle` branch with full history;
2. fetches only `main` from upstream;
3. rebases `chronicle` onto `upstream/main` when it is behind;
4. pushes with `--force-with-lease`; and
5. opens or updates a Chronicle issue if the rebase conflicts.

The workflow needs the `SCREENPIPE_FORK_TOKEN` repository secret with write
access to `AnkushMalaker/screenpipe`. A manual dry run can be dispatched with:

```bash
gh workflow run sync-screenpipe-fork.yml -F push=false
```

Use `-F`, not `-f`, so GitHub receives a boolean rather than the truthy string
`"false"`.

## Local sync and conflict recovery

The fork is normally cloned at `untracked/screenpipe` with the upstream remote
named `origin` and the writable fork remote named `fork`. The script resolves
the remotes by URL, so their names are not a contract.

First check divergence without changing the worktree:

```bash
cd untracked/screenpipe
./sync-fork.sh --check
```

Then rebase locally. The script uses `--autostash` because Tauri builds commonly
leave generated schema changes in the worktree:

```bash
./sync-fork.sh
```

If Git stops on a conflict, resolve it, stage the resolved files, and continue:

```bash
git add <files>
git rebase --continue
```

Run tests appropriate to the touched Screenpipe areas (see the fork's
`AGENTS.md` and `TESTING.md`), then publish the rewritten branch:

```bash
./sync-fork.sh --push
```

The push deliberately uses `--force-with-lease`. Never replace it with an
unconditional force push.

## Topology invariant

The automation and `untracked/screenpipe/sync-fork.sh` must agree on this
relationship:

```text
screenpipe/screenpipe main -> AnkushMalaker/screenpipe chronicle
```

When changing the branch topology, update both files together and verify the
remote branches with:

```bash
git -C untracked/screenpipe ls-remote --heads fork chronicle
git -C untracked/screenpipe ls-remote --heads origin main
```
