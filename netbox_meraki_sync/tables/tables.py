import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from ..models import SyncLog


class SyncLogTable(NetBoxTable):
    network_id   = tables.Column(verbose_name="Network ID", linkify=True)
    network_name = tables.Column(verbose_name="Network Name")
    site_name    = tables.Column(verbose_name="NetBox Site")
    started_at   = tables.DateTimeColumn(verbose_name="Started")
    completed_at = tables.DateTimeColumn(verbose_name="Completed", orderable=True)
    status       = columns.ChoiceFieldColumn(verbose_name="Status")
    devices_seen    = tables.Column(verbose_name="Devices")
    devices_created = tables.Column(verbose_name="Created")
    devices_updated = tables.Column(verbose_name="Updated")
    interfaces_synced = tables.Column(verbose_name="Interfaces")
    macs_synced       = tables.Column(verbose_name="MACs")
    vlans_synced      = tables.Column(verbose_name="VLANs")
    prefixes_synced   = tables.Column(verbose_name="Prefixes")
    static_routes_synced = tables.Column(verbose_name="Static Routes")
    wireless_lans_synced = tables.Column(verbose_name="SSIDs")

    class Meta(NetBoxTable.Meta):
        model = NetBoxTable.Meta.model if hasattr(NetBoxTable.Meta, "model") else None
        model = SyncLog
        fields = (
            "pk",
            "network_id",
            "network_name",
            "site_name",
            "status",
            "started_at",
            "completed_at",
            "devices_seen",
            "devices_created",
            "devices_updated",
            "interfaces_synced",
            "macs_synced",
            "vlans_synced",
            "prefixes_synced",
            "static_routes_synced",
            "wireless_lans_synced",
        )
        default_columns = (
            "network_name",
            "site_name",
            "status",
            "started_at",
            "devices_seen",
            "devices_created",
            "macs_synced",
        )
