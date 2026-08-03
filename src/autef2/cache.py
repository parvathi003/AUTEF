"""Cache of what worked, keyed by failure signature.

Generated suites fail in bulk and repetitively: one wrong import convention
produces the same ModuleNotFoundError in forty test files. Diagnosing each of
them separately pays for the same answer forty times.

What is cached is the **strategy**, not the patch. Reusing a literal patch
across tests would be unsound -- the same signature says the failures have the
same shape, not that the same code fixes them. Reusing the strategy is sound:
it only asserts that a repair of this *kind* worked on a failure of this
*shape*, and the repair itself is still generated fresh, applied, re-run and
verified. A cache hit skips two model calls (analysis and selection) and
nothing else; if the cached strategy does not verify, the run falls straight
back to the full ladder and the entry's score drops.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional

from .models import RootCause

logger = logging.getLogger(__name__)

#: Do not reuse a strategy until it has actually worked, and stop reusing it
#: once it has failed more often than it has worked.
MIN_SUCCESSES = 1
MIN_SUCCESS_RATE = 0.5


@dataclass
class CacheEntry:
    signature: str
    root_cause: str
    strategy_id: str
    successes: int = 0
    failures: int = 0
    example_nodeid: str = ""
    example_message: str = ""

    @property
    def success_rate(self) -> float:
        total = self.successes + self.failures
        return self.successes / total if total else 0.0

    @property
    def reusable(self) -> bool:
        return (
            self.successes >= MIN_SUCCESSES
            and self.success_rate >= MIN_SUCCESS_RATE
        )


class SignatureCache:
    """Persistent, thread-safe map from failure signature to winning strategy."""

    def __init__(self, path: Optional[Path] = None, enabled: bool = True):
        self.path = Path(path) if path else None
        self.enabled = enabled
        self._entries: Dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self._load()

    # -- lookup -----------------------------------------------------------

    def lookup(self, signature: str) -> Optional[CacheEntry]:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._entries.get(signature)
            if entry is not None and entry.reusable:
                self.hits += 1
                return entry
            self.misses += 1
            return None

    # -- recording --------------------------------------------------------

    def record_success(
        self,
        signature: str,
        root_cause: RootCause,
        strategy_id: str,
        *,
        nodeid: str = "",
        message: str = "",
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            entry = self._entries.get(signature)
            if entry is None or entry.strategy_id != strategy_id:
                # A different strategy won this time; the entry now describes
                # the strategy with current evidence behind it.
                entry = CacheEntry(
                    signature=signature,
                    root_cause=root_cause.value,
                    strategy_id=strategy_id,
                    example_nodeid=nodeid,
                    example_message=message[:200],
                )
                self._entries[signature] = entry
            entry.successes += 1
            self._save_locked()

    def record_failure(self, signature: str, strategy_id: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            entry = self._entries.get(signature)
            if entry is None or entry.strategy_id != strategy_id:
                return
            entry.failures += 1
            self._save_locked()

    # -- stats ------------------------------------------------------------

    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": (
                    self.hits / (self.hits + self.misses)
                    if (self.hits + self.misses)
                    else 0.0
                ),
            }

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable signature cache: %s", exc)
            return
        for signature, data in (raw.get("entries") or {}).items():
            try:
                self._entries[signature] = CacheEntry(**data)
            except TypeError:
                continue
        logger.info("Loaded %d cached failure signatures", len(self._entries))

    def _save_locked(self) -> None:
        if not self.path:
            return
        payload = {
            "version": 1,
            "entries": {s: asdict(e) for s, e in self._entries.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not persist signature cache: %s", exc)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = 0
            self.misses = 0
            self._save_locked()
