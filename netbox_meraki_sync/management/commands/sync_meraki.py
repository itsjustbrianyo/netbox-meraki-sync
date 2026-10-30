"""
Management command: sync_meraki

Syncs Cisco Meraki network devices into NetBox by reading every NetBox Site
that has the meraki_network_id custom field populated, collecting device data
from the corresponding Meraki network via the official Meraki Python SDK, and
writing it to NetBox via the Django ORM.

Usage
-----
  # Sync all mapped sites
  python manage.py sync_meraki

  # Sync a single Meraki network (by network ID)
  python manage.py sync_meraki --network N_xxxxxxxxxxxx

  # Dry run — show what would change without writing anything
  python manage.py sync_meraki --dry-run

  # Regular runs always sync devices, interfaces, MACs, IP addresses, and
  # each network's MX VLANs/subnets (VLAN + Prefix) — no flags needed.
  python manage.py sync_meraki

  # List all Meraki networks accessible to the API key
  python manage.py sync_meraki --list-networks

Configuration
-------------
Set meraki_api_key in NetBox's PLUGINS_CONFIG (or the MERAKI_DASHBOARD_API_KEY
environment variable):

  PLUGINS_CONFIG = {
      "netbox_meraki_sync": {
          "meraki_api_key": "your-api-key",
          "http_proxy": None,
      }
  }

Scheduling
----------
Use the "Run sync" page in NetBox (Meraki Sync > Run Sync) to run or schedule
the sync from the web UI.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from ...change_logging import DEFAULT_USERNAME
from ...collector import MerakiCollector
from ...sync_runner import (
    Reporter,
    SyncRunError,
    build_collector,
    run_backfill_device_types,
    run_sync,
)


class _CommandReporter(Reporter):
    """Send run progress to the command's stdout/stderr."""

    def __init__(self, command):
        self.cmd = command

    def info(self, message):
        self.cmd.stdout.write(message)

    def success(self, message):
        self.cmd.stdout.write(self.cmd.style.SUCCESS(message))

    def warning(self, message):
        self.cmd.stdout.write(self.cmd.style.WARNING(message))

    def error(self, message):
        self.cmd.stderr.write(self.cmd.style.ERROR(message))


class Command(BaseCommand):
    help = "Sync Cisco Meraki network devices into NetBox."

    def add_arguments(self, parser):
        parser.add_argument(
            "--network",
            metavar="NETWORK_ID",
            default=None,
            help=(
                "Sync only this Meraki network ID (e.g. N_xxxxxxxxxxxx). "
                "The corresponding NetBox site must have meraki_network_id set "
                "to this value.  Default: sync all mapped sites."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Collect data but do not write anything to NetBox.",
        )
        parser.add_argument(
            "--user",
            default=None,
            help=(
                "NetBox username that changes are attributed to in the "
                "changelog.  Default: plugin setting changelog_username, "
                f"or '{DEFAULT_USERNAME}' (created automatically, inactive)."
            ),
        )
        parser.add_argument(
            "--list-networks",
            action="store_true",
            help=(
                "List all Meraki networks accessible to the configured API key "
                "and exit.  Useful for finding network IDs to map to sites."
            ),
        )
        parser.add_argument(
            "--backfill-device-types",
            action="store_true",
            help=(
                "Enrich existing Meraki DeviceTypes from the NetBox Community "
                "Device Type Library and exit, without running a sync. Only "
                "fills DeviceTypes that don't already have data (rack height, "
                "an image, or comments) — never overwrites anything. Requires "
                "enable_devicetype_library to be set in PLUGINS_CONFIG."
            ),
        )

    def handle(self, *args, **options):
        try:
            if options["backfill_device_types"]:
                run_backfill_device_types(_CommandReporter(self))
                return

            if options["list_networks"]:
                self._list_networks(build_collector())
                return

            totals = run_sync(
                _CommandReporter(self),
                network_filter=options["network"],
                dry_run=options["dry_run"],
                username=options.get("user"),
            )
        except SyncRunError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            sys.exit(1)
        if totals.failed_networks:
            sys.exit(2)

    # ------------------------------------------------------------------
    # List networks helper
    # ------------------------------------------------------------------

    def _list_networks(self, collector: MerakiCollector) -> None:
        orgs = collector.get_organizations()
        if not orgs:
            self.stderr.write(self.style.ERROR(
                "No organisations returned.  Check the API key has access."
            ))
            return

        for org in orgs:
            org_id   = org.get("id", "?")
            org_name = org.get("name", "?")
            self.stdout.write(f"\nOrganisation: {org_name}  (id: {org_id})")
            self.stdout.write("-" * 60)

            networks = collector.get_organization_networks(org_id)
            if not networks:
                self.stdout.write("  (no networks)")
                continue

            for net in networks:
                net_id   = net.get("id", "?")
                net_name = net.get("name", "?")
                tags     = ", ".join(net.get("tags") or []) or "—"
                self.stdout.write(f"  {net_id:<30}  {net_name}  [tags: {tags}]")
