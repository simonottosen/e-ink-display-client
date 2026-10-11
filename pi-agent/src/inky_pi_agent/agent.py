"""Reliable Pi-agent orchestration."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from .config import AgentSettings
from inky_contract import ArtifactDescriptor, DesiredState, JobEvent
from .hardware import SerializedDisplayWorker
from .spool import AgentSpool, AgentState, PendingAcknowledgement

logger = logging.getLogger(__name__)


class AgentTransport(Protocol):
    def desired_state(self, current_revision: int) -> DesiredState | None: ...

    def download_artifact(self, descriptor: ArtifactDescriptor, spool: AgentSpool): ...

    def heartbeat(self, current_revision: int, artifact_sha256: str | None) -> None: ...

    def acknowledge(
        self,
        job_id: str,
        event: JobEvent,
        *,
        revision: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None: ...


class InkyAgent:
    def __init__(
        self,
        settings: AgentSettings,
        transport: AgentTransport,
        spool: AgentSpool,
        display_worker: SerializedDisplayWorker,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.spool = spool
        self.display_worker = display_worker
        self.state = spool.load_state()

    def run_once(self, now: datetime | None = None) -> bool:
        """Reconcile the latest desired revision once; return whether it refreshed."""

        self._flush_pending_acknowledgement()

        now = now or datetime.now(UTC)
        if self._heartbeat_is_due(now):
            self.transport.heartbeat(self.state.current_revision, self.state.last_successful_artifact_sha256)
            self.state = replace(self.state, last_heartbeat_at=now.isoformat())
            self.spool.save_state(self.state)

        desired = self.transport.desired_state(self.state.current_revision)
        # Our revision records what we last put on the panel, not how far the host has got.
        # Revisions count up from zero in the host database, so resetting that database makes
        # the next revision lower than the one we are holding. Treating ours as a high water
        # mark strands the panel until the host counts past it, which can be forever.
        if desired is None or desired.revision == self.state.current_revision:
            return False
        if desired.display_id != self.settings.display_id:
            raise ValueError(
                f"desired state targets {desired.display_id!r}, expected {self.settings.display_id!r}"
            )
        if desired.not_before is not None and now < desired.not_before:
            return False
        if desired.expires_at is not None and now >= desired.expires_at:
            raise ValueError(f"desired job {desired.job_id} has expired")
        return self._apply_desired_state(desired)

    def run_forever(self, notify: object | None = None) -> None:
        """Poll forever, using bounded exponential backoff for unavailable hosts."""

        failures = 0
        while True:
            try:
                self.run_once()
                failures = 0
                _notify(notify)
                time.sleep(self.settings.poll_interval_seconds)
            except KeyboardInterrupt:
                return
            except Exception:
                failures += 1
                delay = min(
                    self.settings.retry_initial_seconds * (2 ** min(failures - 1, 10)),
                    self.settings.retry_max_seconds,
                )
                logger.exception("Pi agent reconciliation failed; retrying", extra={"retry_seconds": delay})
                _notify(notify)
                time.sleep(delay)

    def _heartbeat_is_due(self, now: datetime) -> bool:
        if self.state.last_heartbeat_at is None:
            return True
        try:
            last_heartbeat = datetime.fromisoformat(self.state.last_heartbeat_at)
        except ValueError:
            return True
        return (now - last_heartbeat).total_seconds() >= self.settings.heartbeat_interval_seconds

    def _apply_desired_state(self, desired: DesiredState) -> bool:
        artifact_path = self.transport.download_artifact(desired.artifact, self.spool)
        try:
            self.transport.acknowledge(str(desired.job_id), JobEvent.STARTED)
        except Exception:
            logger.warning("could not acknowledge started job; continuing with display update", exc_info=True)

        try:
            self.display_worker.refresh(artifact_path)
        except Exception as error:
            self._save_pending_acknowledgement(
                PendingAcknowledgement(
                    event=JobEvent.FAILED.value,
                    job_id=desired.job_id,
                    revision=desired.revision,
                    error_code="display-refresh-failed",
                    error_message=str(error)[:1000],
                )
            )
            self._flush_pending_acknowledgement()
            raise

        # Persist completion before reporting it. A power loss or network failure
        # now leaves a retryable completion acknowledgement.
        self.state = replace(
            self.state,
            current_revision=desired.revision,
            last_successful_artifact_sha256=desired.artifact.sha256,
            pending_acknowledgement=PendingAcknowledgement(
                event=JobEvent.COMPLETED.value,
                job_id=desired.job_id,
                revision=desired.revision,
            ),
        )
        self.spool.save_state(self.state)
        self._flush_pending_acknowledgement()
        return True

    def _save_pending_acknowledgement(self, acknowledgement: PendingAcknowledgement | None) -> None:
        self.state = replace(self.state, pending_acknowledgement=acknowledgement)
        self.spool.save_state(self.state)

    def _flush_pending_acknowledgement(self) -> None:
        pending = self.state.pending_acknowledgement
        if pending is None:
            return
        try:
            self.transport.acknowledge(
                pending.job_id,
                pending.job_event,
                revision=pending.revision,
                error_code=pending.error_code,
                error_message=pending.error_message,
            )
        except Exception:
            logger.warning("will retry pending display acknowledgement", extra={"job_id": pending.job_id})
            return
        self._save_pending_acknowledgement(None)


def _notify(notify: object | None) -> None:
    if notify is None:
        return
    watchdog = getattr(notify, "watchdog", None)
    if callable(watchdog):
        watchdog()
