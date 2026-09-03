"""Stateful compression health/warning tracking (agent_protocols_v1_metadata_v1.md §20).

Compression failure or timeout must never be silent, but it also must
not spam a caller with a fresh warning on every single failed call
during a sustained outage. `CompressionWarningTracker` tracks a simple
healthy/unhealthy state machine and only returns a new warning string
on the two edges that matter: the first failure after being healthy,
and the first success after being unhealthy (a one-time recovery
notice). Repeated failures while already unhealthy return `[]` -- the
caller can still inspect `consecutive_failures` for its own bookkeeping,
it just doesn't get a duplicate user-facing warning.

Deliberately holds no reference to `acp.compressor.FailurePolicy` (or
any other compressor-internal type) so this module has no import-time
dependency on `acp.compressor` -- callers pass a plain `blocked: bool`
instead. Scope one instance per compression flow (e.g. one per
`Compressor` instance); this class keeps no module-level state, so a
later coordinator wave can construct as many independent trackers as
it needs (per-provider, per-receiver, ...).
"""
from __future__ import annotations

from acp.errors import Outcome

RECOVERY_MESSAGE = "ACP compression restored; external provider is available again."


class CompressionWarningTracker:
    def __init__(self) -> None:
        self._healthy = True
        self._consecutive_failures = 0

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def record_failure(
        self,
        outcome: Outcome,
        *,
        elapsed_seconds: float,
        estimated_tokens: int,
        blocked: bool,
    ) -> list[str]:
        """Advance to (or stay in) the unhealthy state.

        Returns a single-element list with a new warning string on the
        healthy -> unhealthy transition, or `[]` if already unhealthy.
        """
        was_healthy = self._healthy
        self._healthy = False
        self._consecutive_failures += 1
        if not was_healthy:
            return []

        if blocked:
            exposure = "payload blocked instead of exposed"
        else:
            exposure = (
                f"~{estimated_tokens} estimated tokens are passing through "
                "uncompressed"
            )
        warning = (
            f"ACP compression {outcome.value} after {elapsed_seconds:.1f}s; "
            f"{exposure}. Native fallback is disabled."
        )
        return [warning]

    def record_success(self) -> list[str]:
        """Advance to (or stay in) the healthy state.

        Returns a single-element list with the recovery message on the
        unhealthy -> healthy transition, or `[]` if already healthy.
        """
        was_healthy = self._healthy
        self._healthy = True
        self._consecutive_failures = 0
        if was_healthy:
            return []
        return [RECOVERY_MESSAGE]
