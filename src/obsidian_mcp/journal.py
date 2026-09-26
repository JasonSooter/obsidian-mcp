"""Version journal and audit log: why nothing this server does is irreversible.

This module is what makes a publicly-reachable ``vault_delete`` defensible. Every
mutating tool calls ``snapshot()`` *before* touching the vault, so the previous
bytes always survive somewhere the caller cannot reach through the MCP surface.

Design notes worth keeping:

* Snapshots live in the state directory, **outside the vault**. If they lived
  inside it, Dropbox would sync them to every device and they would show up in
  search results and backlink scans -- the journal would corrupt the thing it
  exists to protect.
* The audit log is append-only and is never read by any tool. A caller holding
  an authenticated caller can change the vault, but cannot use this server to rewrite
  the record of having done so.
* This is an undo buffer, not a backup. It is on the same disk as the vault and
  Dropbox does not replicate it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import telemetry
from .vault import Vault

log = logging.getLogger(__name__)

# Snapshot filenames: <utc timestamp>-<op><original suffix>. Sorting these
# lexicographically sorts them chronologically, which is why the timestamp is
# fixed-width and leads.
_TS_FORMAT = "%Y%m%dT%H%M%S%f"


class Journal:
    """Pre-mutation snapshots plus the audit trail."""

    def __init__(
        self,
        versions_dir: Path,
        audit_log_path: Path,
        *,
        retention_days: int,
    ) -> None:
        self._versions = versions_dir
        self._audit = audit_log_path
        self._retention_days = retention_days

    # -- snapshots ---------------------------------------------------------

    def snapshot(self, vault: Vault, path: Path, *, op: str) -> str | None:
        """Copy the current contents of ``path`` aside. Returns a version id.

        Returns None when the file does not exist yet -- a create has no prior
        version, and that is not an error.
        """
        if not path.is_file():
            return None

        rel = vault.relative(path)
        bucket = self._bucket(vault.name, rel)
        bucket.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime(_TS_FORMAT)
        version_id = f"{stamp}-{op}{path.suffix}"
        target = bucket / version_id

        # Read-and-write rather than shutil.copy2 so the snapshot cannot inherit
        # a symlink; by this point `path` is already confined, but the journal
        # should not be the place that reintroduces one.
        target.write_bytes(path.read_bytes())
        return version_id

    def list_versions(self, vault: Vault, rel: str, *, limit: int) -> list[dict]:
        """Snapshots for one note, newest first."""
        bucket = self._bucket(vault.name, rel)
        if not bucket.is_dir():
            return []
        entries = []
        for item in sorted(bucket.iterdir(), reverse=True):
            if not item.is_file():
                continue
            stamp, _, rest = item.name.partition("-")
            op = Path(rest).stem or "unknown"
            try:
                when = datetime.strptime(stamp, _TS_FORMAT).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            entries.append(
                {
                    "version_id": item.name,
                    "timestamp": when.isoformat(),
                    "op": op,
                    "size": item.stat().st_size,
                }
            )
            if len(entries) >= limit:
                break
        return entries

    def read_version(self, vault: Vault, rel: str, version_id: str) -> str:
        """Contents of one snapshot.

        ``version_id`` is caller-supplied, so it is validated as a bare filename
        -- otherwise it would be a second, unconfined path parameter sitting
        right next to the carefully confined one.
        """
        if "/" in version_id or "\\" in version_id or version_id in {".", ".."}:
            raise ValueError(f"Invalid version id: {version_id!r}")
        target = self._bucket(vault.name, rel) / version_id
        resolved = target.resolve()
        try:
            resolved.relative_to(self._versions.resolve())
        except ValueError:
            raise ValueError(f"Invalid version id: {version_id!r}") from None
        if not resolved.is_file():
            raise FileNotFoundError(version_id)
        return resolved.read_text(encoding="utf-8", errors="replace")

    def _bucket(self, vault_name: str, rel: str) -> Path:
        """Directory holding every snapshot of one note.

        ``rel`` has already been through Vault.resolve(), so it is a safe
        relative path; we mirror the vault's own layout to keep the journal
        browsable by hand with `find` when something has gone wrong.
        """
        return self._versions / vault_name / rel

    # -- audit -------------------------------------------------------------

    def record(self, *, tool: str, vault: str, path: str, **extra) -> None:
        """Append one line of JSON to the audit log. Never raises into a tool."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "vault": vault,
            "path": path,
            **extra,
        }
        # Also ship it: this method is the single funnel every mutation passes
        # through, so instrumenting here covers all of them at one site.
        telemetry.emit(telemetry.EVENT_MUTATION, **{
            k: v for k, v in entry.items()
            if k != "ts" and isinstance(v, (str, int, float, bool))
        })

        try:
            self._audit.parent.mkdir(parents=True, exist_ok=True)
            # Append mode with a single write() call: the O_APPEND write of a
            # short line is atomic enough that concurrent tool calls interleave
            # cleanly rather than corrupting each other's lines.
            with self._audit.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # An unwritable audit log must not take down a tool the user is
            # relying on, but it is a real operational problem -- make it loud
            # in the container logs.
            log.exception("could not write audit log entry: %s", entry)

    # -- retention ---------------------------------------------------------

    def prune(self) -> int:
        """Drop snapshots past the retention window, keeping one per day.

        Called at startup and daily. Everything inside the window is kept in
        full; beyond it we thin to the last snapshot of each day, so the journal
        stays useful for 'what did this note look like last month' without
        growing without bound.
        """
        if not self._versions.is_dir():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        removed = 0
        for bucket in self._versions.rglob("*"):
            if not bucket.is_dir():
                continue
            seen_days: set[str] = set()
            for item in sorted(bucket.iterdir(), reverse=True):
                if not item.is_file():
                    continue
                stamp = item.name.partition("-")[0]
                try:
                    when = datetime.strptime(stamp, _TS_FORMAT).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
                if when >= cutoff:
                    continue
                day = when.date().isoformat()
                if day in seen_days:
                    item.unlink(missing_ok=True)
                    removed += 1
                else:
                    seen_days.add(day)
        if removed:
            log.info("journal prune removed %d old snapshots", removed)
        return removed
