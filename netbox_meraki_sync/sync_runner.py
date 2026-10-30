"""
Shared sync orchestration.

The ``sync_meraki`` management command and the background job behind the
"Run sync" page both call into this module, so the CLI and the web UI always
behave identically.  Progress is reported through a small ``Reporter`` so each
caller can show it its own way: stdout/stderr for the CLI, the NetBox job log
for the web UI.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .change_logging import DEFAULT_USERNAME, change_logging
from .choices import SyncStatusChoices
from .collector import MerakiCollector
from .models import SyncLog
from .signals import _ensure_site_custom_fields
from .syncer import MerakiSyncer, get_mapped_sites

logger = logging.getLogger(__name__)


class SyncRunError(Exception):
    """A run could not start (bad configuration or an unmatched filter)."""


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def plugin_setting(key: str, default=None):
    try:
        from netbox.plugins import get_plugin_config
        return get_plugin_config("netbox_meraki_sync", key) or default
    except Exception:
        return default


def get_api_key() -> str:
    return (
        plugin_setting("meraki_api_key", "")
        or os.environ.get("MERAKI_DASHBOARD_API_KEY", "")
    )


def build_collector() -> MerakiCollector:
    """Create a Meraki collector from the plugin settings."""
    api_key = get_api_key()
    if not api_key:
        raise SyncRunError(
            "No Meraki API key configured.\n"
            "Set meraki_api_key in PLUGINS_CONFIG or the "
            "MERAKI_DASHBOARD_API_KEY environment variable."
        )
    return MerakiCollector(
        api_key=api_key,
        timeout=plugin_setting("request_timeout", 30) or 30,
        proxy=plugin_setting("http_proxy", "") or "",
    )


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

class Reporter:
    """Where run progress goes.  Subclasses decide how it is displayed."""

    def info(self, message: str) -> None:
        raise NotImplementedError

    def success(self, message: str) -> None:
        self.info(message)

    def warning(self, message: str) -> None:
        raise NotImplementedError

    def error(self, message: str) -> None:
        raise NotImplementedError


@dataclass
class SyncTotals:
    networks: int = 0
    devices: int = 0
    created: int = 0
    updated: int = 0
    interfaces: int = 0
    ips: int = 0
    vlans: int = 0
    prefixes: int = 0
    static_routes: int = 0
    wireless_lans: int = 0
    failed_networks: int = 0


# ----------------------------------------------------------------------
# Device type backfill
# ----------------------------------------------------------------------

def run_backfill_device_types(reporter: Reporter) -> tuple[int, int]:
    """
    Enrich existing Meraki DeviceTypes from the NetBox Community Device Type
    Library.  Returns (enriched, skipped).
    """
    if not plugin_setting("enable_devicetype_library", False):
        raise SyncRunError(
            "enable_devicetype_library is not set in PLUGINS_CONFIG. "
            "Enable it before backfilling device types."
        )

    syncer = MerakiSyncer(
        sync_log=SyncLog(),
        enable_devicetype_library=True,
        devicetype_library_cache_days=plugin_setting(
            "devicetype_library_cache_days", 7,
        ),
    )
    reporter.info("Checking existing Meraki device types against the Device Type Library...")
    enriched, skipped = syncer.backfill_device_types()
    reporter.success(
        f"Done — {enriched} device type(s) enriched, "
        f"{skipped} already had data or no match found."
    )
    return enriched, skipped


# ----------------------------------------------------------------------
# Sync
# ----------------------------------------------------------------------

def run_sync(
    reporter: Reporter,
    *,
    network_filter: str | None = None,
    dry_run: bool = False,
    username: str | None = None,
) -> SyncTotals:
    """
    Sync every mapped site (or just the one whose ``meraki_network_id``
    equals ``network_filter``) from Meraki into NetBox.

    Changes are recorded in the NetBox changelog, attributed to ``username``
    (default: the ``changelog_username`` plugin setting).  Raises
    ``SyncRunError`` if the run cannot start; a failure inside one network is
    reported and counted in ``failed_networks`` but does not stop the others.
    """
    collector = build_collector()
    role_slug = plugin_setting("default_device_role", "network") or "network"

    if dry_run:
        reporter.warning("DRY RUN — no database writes")

    # Ensure custom fields exist before any ORM reads
    _ensure_site_custom_fields()

    mapped_sites = get_mapped_sites()
    if not mapped_sites:
        reporter.warning(
            "No NetBox sites have meraki_network_id set.\n"
            "Populate the custom field on a Site to map it to a Meraki network.\n"
            "Use --list-networks to see available network IDs."
        )
        return SyncTotals()

    if network_filter:
        mapped_sites = [
            s for s in mapped_sites
            if s.custom_field_data.get("meraki_network_id") == network_filter
        ]
        if not mapped_sites:
            raise SyncRunError(
                f"No NetBox site has meraki_network_id = {network_filter!r}"
            )

    reporter.info(f"Syncing {len(mapped_sites)} Meraki network(s) into NetBox…")

    totals = SyncTotals(networks=len(mapped_sites))

    changelog_user = (
        username
        or plugin_setting("changelog_username", DEFAULT_USERNAME)
        or DEFAULT_USERNAME
    )
    # All writes below run inside NetBox's change-tracking context so they
    # appear in the changelog (one request ID per sync run).
    with change_logging(changelog_user, enabled=not dry_run):
        for site in mapped_sites:
            network_id = site.custom_field_data["meraki_network_id"]

            reporter.info(f"\n[{network_id}] → site: {site.name}")

            sync_log = SyncLog(
                network_id=network_id,
                site_name=site.name,
                status=SyncStatusChoices.RUNNING,
            )
            if not dry_run:
                sync_log.save()

            syncer = MerakiSyncer(
                sync_log=sync_log,
                dry_run=dry_run,
                sync_ips=True,
                default_role_slug=role_slug,
                enable_devicetype_library=plugin_setting(
                    "enable_devicetype_library", False,
                ),
                devicetype_library_cache_days=plugin_setting(
                    "devicetype_library_cache_days", 7,
                ),
            )

            try:
                devices = collector.collect_network(network_id)
                reporter.info(f"  Collected {len(devices)} device(s) from Meraki")

                syncer.sync_devices(devices, site=site)

                # Try every device, not just ones with an "MS" model prefix —
                # some Layer 3 switches (e.g. Catalyst switches onboarded for
                # Meraki cloud monitoring) report other model strings, and
                # getDeviceSwitchRoutingInterfaces safely 404s/returns empty
                # for anything that isn't a switch, so there's no harm in
                # trying every serial.
                switch_serials = [d.serial for d in devices]
                ipam_data = collector.collect_network_ipam(
                    network_id, site_name=site.name, switch_serials=switch_serials,
                )
                reporter.info(
                    f"  Collected {len(ipam_data.vlans)} VLAN/subnet "
                    f"record(s) and {len(ipam_data.static_routes)} "
                    f"static route(s) from Meraki"
                )
                for err in syncer.sync_ipam(ipam_data, site=site):
                    reporter.error(f"  IPAM error: {err}")

                ssids = collector.collect_network_wireless(network_id)
                if ssids:
                    reporter.info(f"  Collected {len(ssids)} enabled SSID(s) from Meraki")
                    for err in syncer.sync_wireless(ssids, site=site):
                        reporter.error(f"  Wireless error: {err}")

                syncer.close(success=True)

                totals.devices       += sync_log.devices_seen
                totals.created       += sync_log.devices_created
                totals.updated       += sync_log.devices_updated
                totals.interfaces    += sync_log.interfaces_synced
                totals.ips           += sync_log.ips_synced
                totals.vlans         += sync_log.vlans_synced
                totals.prefixes      += sync_log.prefixes_synced
                totals.static_routes += sync_log.static_routes_synced
                totals.wireless_lans += sync_log.wireless_lans_synced

                reporter.success(
                    f"  Done — "
                    f"{sync_log.devices_created} created, "
                    f"{sync_log.devices_updated} updated, "
                    f"{sync_log.interfaces_synced} interfaces, "
                    f"{sync_log.ips_synced} IPs, "
                    f"{sync_log.vlans_synced} VLANs, "
                    f"{sync_log.prefixes_synced} prefixes, "
                    f"{sync_log.static_routes_synced} static routes"
                    + (
                        f", {sync_log.wireless_lans_synced} SSIDs"
                        if sync_log.wireless_lans_synced else ""
                    )
                )

            except Exception as exc:
                logger.exception("Sync failed for network %s: %s", network_id, exc)
                syncer.close(success=False, message=str(exc))
                reporter.error(f"  ERROR: {exc}")
                totals.failed_networks += 1

    reporter.info("")
    if dry_run:
        reporter.warning(
            f"Dry-run complete. Would have synced {totals.devices} device(s) "
            f"across {totals.networks} network(s)."
        )
    else:
        reporter.success(
            f"Sync complete — "
            f"{totals.devices} devices seen, "
            f"{totals.created} created, "
            f"{totals.updated} updated, "
            f"{totals.interfaces} interfaces, {totals.ips} IPs, "
            f"{totals.vlans} VLANs, {totals.prefixes} prefixes, "
            f"{totals.static_routes} static routes"
            + (f", {totals.wireless_lans} SSIDs" if totals.wireless_lans else "")
            + (
                f"  [{totals.failed_networks} network(s) failed]"
                if totals.failed_networks else ""
            )
        )

    return totals
