"""Content-addressed storage for what is too big or too sensitive for the log.

Screenshots live here, one file per SHA-256, in a folder beside the run's log.
The log records only the hash, so the hash chain still commits to exactly the
pixels the model saw, the log stays small, and nothing image-shaped is ever sent
to the control plane with the events.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    @classmethod
    def beside(cls, log_path: Path) -> BlobStore:
        p = Path(log_path)
        return cls(p.with_name(p.stem + ".blobs"))

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self.root / sha
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return sha

    def get(self, sha: str) -> bytes | None:
        path = self.root / sha
        if not path.exists():
            return None
        data = path.read_bytes()
        # A blob that no longer matches its name is treated as missing, not trusted.
        return data if hashlib.sha256(data).hexdigest() == sha else None

    def copy_to(self, other: BlobStore) -> None:
        if self.root.exists():
            shutil.copytree(self.root, other.root, dirs_exist_ok=True)
