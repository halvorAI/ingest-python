"""Disk spool for events that could not be delivered yet. Same layout and limits as the Node.js SDK.

Files are JSON Lines, one signed envelope per line, named so that lexical order is age order.
Events the API will never accept go to ``dead/`` and are never replayed. All file I/O runs on the
client's worker thread, never on the caller's.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PREFIX = "halvor-spool-"
SUFFIX = ".jsonl"
DEAD_DIR = "dead"
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_AGE_S = 7 * 24 * 60 * 60.0

Envelope = dict[str, Any]


@dataclass
class SpoolFile:
    envelopes: list[Envelope]
    corrupt: int


@dataclass
class Eviction:
    files: int = 0
    events: int = 0


def _file_name() -> str:
    return f"{PREFIX}{int(time.time() * 1000):015d}-{secrets.token_hex(4)}{SUFFIX}"


def _serialize(envelopes: list[Envelope]) -> str:
    return "".join(json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n" for e in envelopes)


def _atomic_write(directory: Path, name: str, body: str) -> None:
    tmp = directory / f".{name}.{secrets.token_hex(4)}.tmp"
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, directory / name)


def _count_lines(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.strip())


class Spool:
    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.max_age_s = max_age_s

    def _list(self, directory: Path) -> list[str]:
        if not directory.is_dir():
            return []
        return sorted(
            n for n in os.listdir(directory) if n.startswith(PREFIX) and n.endswith(SUFFIX)
        )

    def files(self) -> list[str]:
        """Pending file names, oldest first. Nothing is read into memory."""
        return self._list(self.directory)

    def write(self, envelopes: list[Envelope]) -> Eviction:
        """Writes a new pending file, then enforces the size and age limits."""
        if not envelopes:
            return Eviction()
        self.directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.directory, _file_name(), _serialize(envelopes))
        return self._evict(self.directory)

    def read(self, name: str) -> SpoolFile:
        try:
            text = (self.directory / name).read_text(encoding="utf-8")
        except OSError:
            return SpoolFile([], 0)
        envelopes: list[Envelope] = []
        corrupt = 0
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                corrupt += 1
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("event"), dict) and isinstance(
                parsed.get("sig"), str
            ):
                envelopes.append(parsed)
            else:
                corrupt += 1
        return SpoolFile(envelopes, corrupt)

    def rewrite(self, name: str, envelopes: list[Envelope]) -> None:
        """Keeps only `envelopes` in the file, in its place in the order."""
        if not envelopes:
            self.remove(name)
            return
        _atomic_write(self.directory, name, _serialize(envelopes))

    def remove(self, name: str) -> None:
        try:
            (self.directory / name).unlink()
        except OSError:
            pass

    def dead_letter(self, envelopes: list[Envelope]) -> None:
        if not envelopes:
            return
        dead = self.directory / DEAD_DIR
        dead.mkdir(parents=True, exist_ok=True)
        _atomic_write(dead, _file_name(), _serialize(envelopes))
        self._evict(dead)

    def _evict(self, directory: Path) -> Eviction:
        """Deletes the oldest files until the folder is under the size limit and none is too old."""
        result = Eviction()
        now = time.time()
        entries: list[tuple[str, int, float]] = []
        for name in self._list(directory):
            try:
                st = (directory / name).stat()
            except OSError:
                continue
            entries.append((name, st.st_size, st.st_mtime))
        total = sum(size for _, size, _ in entries)
        for name, size, mtime in entries:
            too_old = now - mtime > self.max_age_s
            if not too_old and total <= self.max_bytes:
                continue
            path = directory / name
            try:
                text = path.read_text(encoding="utf-8")
                path.unlink()
            except OSError:
                continue
            result.files += 1
            result.events += _count_lines(text)
            total -= size
        return result
