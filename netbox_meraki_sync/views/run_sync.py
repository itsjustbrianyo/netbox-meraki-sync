from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from core.choices import JobStatusChoices
from core.models import Job

from ..forms import RunSyncForm
from ..jobs import JOB_NAME, MerakiSyncJob


class RunSyncView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Run the Meraki sync now, or schedule it (optionally recurring)."""

    permission_required = "netbox_meraki_sync.add_synclog"
    template_name = "netbox_meraki_sync/run_sync.html"

    def _scheduled_jobs(self):
        return Job.objects.filter(
            name=JOB_NAME,
            status__in=(JobStatusChoices.STATUS_SCHEDULED, JobStatusChoices.STATUS_PENDING),
        ).order_by("scheduled")

    def _recent_jobs(self):
        return Job.objects.filter(name=JOB_NAME).order_by("-created")[:10]

    def _render(self, request, form):
        return render(request, self.template_name, {
            "form": form,
            "scheduled_jobs": self._scheduled_jobs(),
            "recent_jobs": self._recent_jobs(),
        })

    def get(self, request):
        return self._render(request, RunSyncForm())

    def post(self, request):
        form = RunSyncForm(request.POST)
        if not form.is_valid():
            return self._render(request, form)

        data = form.cleaned_data
        site = data.get("site")
        interval = data.get("interval")
        schedule_at = data.get("schedule_at")

        if interval and not schedule_at:
            schedule_at = timezone.now()

        job = MerakiSyncJob.enqueue(
            name=JOB_NAME,
            user=request.user,
            schedule_at=schedule_at,
            interval=interval,
            network_id=(site.custom_field_data.get("meraki_network_id") if site else None),
            dry_run=data["dry_run"],
            backfill_device_types=data["backfill_device_types"],
            username=request.user.username,
        )

        if schedule_at:
            messages.success(request, "Meraki sync scheduled.")
        else:
            messages.success(request, "Meraki sync started.")
        return redirect(job.get_absolute_url())
