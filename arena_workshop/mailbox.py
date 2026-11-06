"""Atomic file mailbox shared by the Windows worker and Blender add-on.

The mailbox is local to the Windows user profile. It opens no network listener.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from .protocol import JobValidationError, decode_job

_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def default_queue_root():
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "ArenaBlenderWorkshop" / "queue"
    return Path.home() / "AppData" / "Local" / "ArenaBlenderWorkshop" / "queue"


class FileMailbox:
    """Filesystem-backed request/result channel, using atomic same-volume renames."""

    def __init__(self, root):
        self.root = Path(root)
        self.inbox = self.root / "inbox"
        self.processing = self.root / "processing"
        self.results = self.root / "results"
        self.rejected = self.root / "rejected"
        for directory in (self.inbox, self.processing, self.results, self.rejected):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _job_id(value):
        if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
            raise JobValidationError("invalid_job_id")
        return value

    def _atomic_json(self, destination, value, overwrite=False):
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".arena-", suffix=".tmp", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            if not overwrite and destination.exists():
                raise FileExistsError(str(destination))
            os.replace(temp_name, destination)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def enqueue(self, job, allowed_workspaces=("test",)):
        validated = decode_job(json.dumps(job), allowed_workspaces=allowed_workspaces)
        job_id = self._job_id(validated["job_id"])
        if any((directory / (job_id + ".json")).exists() for directory in
               (self.inbox, self.processing, self.results, self.rejected)):
            raise FileExistsError("job_id_already_present")
        self._atomic_json(self.inbox / (job_id + ".json"), validated)

    def claim_next(self, allowed_workspaces=("test",)):
        """Atomically move and validate one request; malformed files go to rejected."""
        for source in sorted(self.inbox.glob("*.json"), key=lambda path: path.name):
            job_id = source.stem
            if not _JOB_ID.fullmatch(job_id):
                try:
                    os.rename(source, self.rejected / (source.name + ".invalid"))
                except (FileNotFoundError, FileExistsError, OSError):
                    continue
                continue
            claimed = self.processing / source.name
            try:
                os.rename(source, claimed)
            except (FileNotFoundError, FileExistsError, OSError):
                continue
            try:
                raw = claimed.read_bytes()
                job = decode_job(raw, allowed_workspaces=allowed_workspaces)
                if job["job_id"] != job_id:
                    raise JobValidationError("job_id_filename_mismatch")
                return job
            except Exception:
                try:
                    os.rename(claimed, self.rejected / (source.name + ".invalid"))
                except OSError:
                    pass
                continue
        return None

    def write_result(self, result):
        job_id = self._job_id(result.get("job_id") if isinstance(result, dict) else None)
        self._atomic_json(self.results / (job_id + ".json"), result)

    def read_result(self, job_id):
        job_id = self._job_id(job_id)
        path = self.results / (job_id + ".json")
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def update_result(self, result):
        """Atomically update an existing result, e.g. with Drive upload metadata."""
        job_id = self._job_id(result.get("job_id") if isinstance(result, dict) else None)
        path = self.results / (job_id + ".json")
        if not path.is_file():
            raise FileNotFoundError(str(path))
        self._atomic_json(path, result, overwrite=True)

    def acknowledge(self, job_id):
        """Remove local mailbox copies after the worker has durably reported results."""
        job_id = self._job_id(job_id)
        for path in (self.processing / (job_id + ".json"), self.results / (job_id + ".json")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
