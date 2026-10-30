"""
Background job that runs the Meraki sync on NetBox's worker (netbox-rq).

Triggered from the plugin's "Run sync" page, either immediately or on a
schedule (optionally recurring).  NetBox's job framework owns scheduling,
status and the job log, so this survives the removal of core custom scripts.
"""

from __future__ import annotations

from core.choices import JobStatusChoices
from core.models import Job
from netbox.jobs import JobRunner

from .sync_runner import (
    Reporter,
    SyncRunError,
    run_backfill_device_types,
    run_sync,
)

try:  # NetBox >= 4.4
    from core.exceptions import JobFailed
except ImportError:  # pragma: no cover - older NetBox
    class JobFailed(Exception):
        pass

# A full sync across many sites can take a while.
JOB_TIMEOUT = 3600
JOB_NAME = "Meraki Sync"


class _JobReporter(Reporter):
    """Write run progress to the NetBox job log."""

    def __init__(self, logger):
        self.log = logger

    def info(self, message):
        for line in str(message).splitlines() or [""]:
            if line.strip():
                self.log.info(line)

    def warning(self, message):
        for line in str(message).splitlines():
            if line.strip():
                self.log.warning(line)

    def error(self, message):
        for line in str(message).splitlines():
            if line.strip():
                self.log.error(line)


class MerakiSyncJob(JobRunner):

    class Meta:
        # Constant: JobRunner.get_jobs() filters by this name.
        name = JOB_NAME

    @classmethod
    def enqueue(cls, *args, **kwargs):
        # RQ strips job_timeout before running, so recurring reschedules would
        # otherwise fall back to the (short) default queue timeout.
        kwargs.setdefault("job_timeout", JOB_TIMEOUT)
        return super().enqueue(*args, **kwargs)

    def run(self, network_id=None, dry_run=False, backfill_device_types=False,
            username=None, **kwargs):
        reporter = _JobReporter(self.logger)

        # Don't let two syncs write at once (e.g. a slow run overlapping the
        # next scheduled one).
        overlapping = Job.objects.filter(
            name=JOB_NAME, status=JobStatusChoices.STATUS_RUNNING,
        ).exclude(pk=self.job.pk)
        if overlapping.exists():
            reporter.warning("Another Meraki sync is already running; skipping this run.")
            return

        try:
            if backfill_device_types:
                run_backfill_device_types(reporter)
                return

            totals = run_sync(
                reporter,
                network_filter=network_id,
                dry_run=dry_run,
                username=username,
            )
        except SyncRunError as exc:
            raise JobFailed(str(exc))

        if totals.failed_networks:
            raise JobFailed(f"{totals.failed_networks} network(s) failed — see the job log.")
