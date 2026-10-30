from dcim.models import Site
from django import forms

from ..choices import SyncStatusChoices


class SyncLogFilterForm(forms.Form):
    """Simple filter form for the SyncLog list view."""

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(attrs={"placeholder": "Network ID or name…"}),
    )
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Any status")] + list(SyncStatusChoices),
        label="Status",
    )


class RunSyncForm(forms.Form):
    """Options for running or scheduling a Meraki sync job."""

    site = forms.ModelChoiceField(
        queryset=Site.objects.none(),
        required=False,
        label="Site",
        help_text="Sync just this site. Leave blank to sync all mapped sites.",
    )
    dry_run = forms.BooleanField(
        required=False,
        label="Dry run",
        help_text="Collect from Meraki without writing anything to NetBox.",
    )
    backfill_device_types = forms.BooleanField(
        required=False,
        label="Backfill device types",
        help_text=(
            "Instead of syncing, enrich existing Meraki device types from the "
            "NetBox Community Device Type Library. Only fills device types with "
            "no data yet; never overwrites. Requires enable_devicetype_library."
        ),
    )
    schedule_at = forms.DateTimeField(
        required=False,
        label="Schedule at",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="Leave blank to run now.",
    )
    interval = forms.IntegerField(
        required=False,
        min_value=1,
        label="Recurs every (minutes)",
        help_text="Leave blank for a one-time run. 1440 = daily, 240 = every 4 hours.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = Site.objects.filter(
            custom_field_data__meraki_network_id__isnull=False,
        ).exclude(custom_field_data__meraki_network_id="")

    def clean(self):
        data = super().clean()
        if data.get("site") and data.get("backfill_device_types"):
            self.add_error("site", "Backfill applies to all device types, not a single site.")
        return data
