"""
NetBox custom script: run the Meraki sync from the web UI.

Upload via Customization → Scripts → Add.  Runs the same code as
`manage.py sync_meraki`, on NetBox's background worker (netbox-rq).
Can be run on demand or scheduled to recur from the script's run page.
"""

import io

from django.core.management import call_command
from django.core.management.base import CommandError

from dcim.models import Site
from extras.scripts import BooleanVar, ObjectVar, Script


class MerakiSync(Script):

    class Meta:
        name = "Meraki Sync"
        description = "Sync Cisco Meraki devices, IPAM and SSIDs into NetBox"
        commit_default = True
        # A full sync across many sites can take a while
        job_timeout = 3600

    site = ObjectVar(
        model=Site,
        required=False,
        query_params={"cf_meraki_network_id__empty": "false"},
        description="Sync just this site. Leave blank to sync all mapped sites.",
    )
    dry_run = BooleanVar(
        default=False,
        description="Collect from Meraki without writing anything to NetBox",
    )

    def run(self, data, commit):
        options = {"dry_run": data["dry_run"]}

        if not commit:
            self.log_warning(
                "'Commit changes' is unticked, so NetBox will roll back "
                "everything this run writes. Tick it on the run page (and "
                "re-create any schedule with it ticked) to keep the changes."
            )

        site = data.get("site")
        if site:
            network_id = site.custom_field_data.get("meraki_network_id")
            if not network_id:
                self.log_failure(f"{site} has no Meraki Network ID set.")
                return
            options["network"] = network_id

        # Attribute changelog entries to whoever ran the script. Scheduled
        # runs are attributed to the user who scheduled them.
        user = getattr(getattr(self, "request", None), "user", None)
        if user and user.is_authenticated:
            options["user"] = user.username

        out, err = io.StringIO(), io.StringIO()
        try:
            call_command("sync_meraki", stdout=out, stderr=err, **options)
        except (CommandError, SystemExit) as exc:
            self.log_failure(f"Sync failed: {exc}")
        finally:
            for line in out.getvalue().splitlines():
                if line.strip():
                    self.log_info(line)
            for line in err.getvalue().splitlines():
                if line.strip():
                    self.log_warning(line)

        if options["dry_run"]:
            self.log_info("Dry run: no changes were written.")
        elif not commit:
            self.log_warning("Sync ran, but its changes will be rolled back (Commit changes is off).")
        else:
            self.log_success("Meraki sync complete.")
