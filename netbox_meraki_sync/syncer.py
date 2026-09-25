"""
NetBox ORM syncer.

Takes CollectedDevice objects from collector.py and writes them to NetBox
using the Django ORM.  All writes are wrapped in atomic transactions so a
single device failure does not abort the whole network sync.

NetBox objects managed
-----------------------
dcim.Manufacturer         — "Cisco Meraki" (created once, reused)
dcim.DeviceType           — per Meraki model string
dcim.DeviceRole           — "Network" (or plugin default_device_role setting)
dcim.Device               — one per Meraki serial; matched by serial first; primary_ip4 set from LAN/WAN1/WAN2 when sync_ips is on
dcim.Interface            — one per port; type derived from model family
dcim.MACAddress           — one per unique MAC; linked to interface
ipam.IPAddress            — device LAN IPs and MX WAN IPs (/32)
ipam.VLAN                 — optional; created per network VLAN when sync_vlans=True (skipped for single-LAN networks)
ipam.VLANGroup            — one per site ("{site} VLANs"), scoping VLAN IDs; inherits site tags
ipam.Prefix               — optional; created per VLAN subnet, single-LAN subnet, and per enabled static route
ipam.IPRange              — optional; created per VLAN/single-LAN subnet and per enabled static route subnet, spanning usable host addresses (excludes network/broadcast)
extras.Tag                — "meraki" tag applied to every synced device + tags inherited from parent Site; VLANGroups, VLANs, Prefixes, WirelessLANGroups, WirelessLANs, and every IPAddress (device + appliance) inherit the same site tags and tenant
wireless.WirelessLAN      — one per enabled/named Meraki SSID, scoped to a per-site WirelessLANGroup, attached to every synced MR AP's radio interface

Custom fields on dcim.site
---------------------------
  meraki_network_id   — Meraki network ID; used to find which site to assign
  meraki_site_name    — Populated from the Meraki network name during sync

These custom fields are created by signals.py on plugin startup.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from django.db import transaction
from django.utils.text import slugify
from django.contrib.contenttypes.models import ContentType

from .collector import (
    CollectedDevice, CollectedVlan, CollectedStaticRoute,
    CollectedNetworkIpam, CollectedSsid,
)
from .models import SyncLog

log = logging.getLogger(__name__)

# Interface type mapping: Meraki family → NetBox interface type value
_FAMILY_IFACE_TYPE = {
    "MS": "1000base-t",        # refined per-port by speed below
    "MX": "other",
    "MR": "ieee802.11ax",      # WiFi 6; good default for modern MR kit
    "MG": "lte",
    "MV": "other",
    "MT": "other",
}

_SPEED_TO_IFACE_TYPE = {
    100:   "100base-tx",
    1000:  "1000base-t",
    2500:  "2.5gbase-t",
    5000:  "5gbase-t",
    10000: "10gbase-t",
    25000: "25gbase-x-sfp28",
    40000: "40gbase-x-qsfpp",
    100000:"100gbase-x-qsfp28",
}

_MERAKI_MANUFACTURER = "Cisco Meraki"
_MERAKI_TAG_SLUG     = "meraki"
_MERAKI_TAG_COLOR    = "1f7d3a"   # Meraki green (approximate)


class MerakiSyncer:
    """
    Sync collected Meraki device data into NetBox.

    Usage::

        syncer = MerakiSyncer(sync_log=log_obj, dry_run=False, sync_ips=False)
        syncer.sync_devices(devices, site=nb_site)
        syncer.close()   # finalises the SyncLog record
    """

    def __init__(
        self,
        sync_log: SyncLog,
        *,
        dry_run: bool = False,
        sync_ips: bool = False,
        default_role_slug: str = "network",
    ) -> None:
        self.log              = sync_log
        self.dry_run          = dry_run
        self.sync_ips         = sync_ips
        self.default_role_slug = default_role_slug

        # Cached lookups populated on first use
        self._manufacturer   = None
        self._meraki_tag     = None
        self._role_cache: dict[str, object]        = {}
        self._device_type_cache: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_devices(self, devices: list[CollectedDevice], site) -> None:
        """
        Sync a list of CollectedDevice objects into NetBox under the given site.
        Updates self.log counters in place.
        """
        self.log.devices_seen = len(devices)

        for dev in devices:
            try:
                with transaction.atomic():
                    self._sync_device(dev, site)
            except Exception as exc:
                log.warning(
                    "Syncer: failed to sync device %s (%s): %s",
                    dev.serial, dev.name, exc
                )

        # After all devices are synced, create/update virtual chassis for any
        # devices that are part of Meraki switch stacks.
        if not self.dry_run:
            for dev in devices:
                if dev.stack_membership:
                    try:
                        self._sync_virtual_chassis(dev, site)
                    except Exception as exc:
                        log.warning(
                            "Syncer: failed to sync virtual chassis for device "
                            "%s: %s", dev.serial, exc,
                        )

        if not self.dry_run:
            self.log.save(update_fields=[
                "devices_seen", "devices_created", "devices_updated",
                "interfaces_synced", "macs_synced", "ips_synced",
            ])

    def sync_ipam(self, ipam_data: CollectedNetworkIpam, site) -> list[str]:
        """
        Sync a network's VLANs (or single-LAN subnet) and static routes,
        collected from its MX appliance, into NetBox IPAM, scoped to the
        given site.  Each VLAN and the single-LAN subnet creates both an
        ipam.VLAN object and an ipam.Prefix for its subnet.  Static routes
        also create Prefix records. VLANGroups, VLANs, Prefixes, and
        WirelessLANs all inherit the site's tenant and tags, the same way
        synced devices do. All IPAM objects are scoped to a per-site VRF
        named '{site_name} VRF'.

        Returns a list of human-readable error strings for any VLAN/subnet
        or static route that failed, so the caller (the management command)
        can print them instead of them only landing in the debug log.
        """
        errors: list[str] = []
        if site is None:
            return errors

        vlan_group = self._get_vlan_group(site)
        vrf = self._get_or_create_vrf(site)

        for cv in ipam_data.vlans:
            try:
                with transaction.atomic():
                    nb_vlan = self._sync_vlan(cv, site, vlan_group, vrf)
                    self._sync_prefix(cv, site, nb_vlan, vrf)
                    self._sync_vlan_ip(cv, site)
                    self._sync_vlan_ip_range(cv, site)
            except Exception as exc:
                log.exception(
                    "Syncer: failed to sync VLAN/subnet %s (%s) for site %s",
                    cv.vlan_id, cv.name, site,
                )
                errors.append(f"VLAN/subnet {cv.vlan_id} ({cv.name}): {exc!r}")

        for sr in ipam_data.static_routes:
            try:
                with transaction.atomic():
                    self._sync_static_route(sr, site, vrf)
            except Exception as exc:
                log.exception(
                    "Syncer: failed to sync static route %s (%s) for site %s",
                    sr.name, sr.subnet, site,
                )
                errors.append(f"Static route {sr.name!r} ({sr.subnet}): {exc!r}")

        if not self.dry_run:
            self.log.save(update_fields=[
                "vlans_synced", "prefixes_synced", "static_routes_synced",
            ])

        return errors

    def sync_wireless(self, ssids: list[CollectedSsid], site) -> list[str]:
        """
        Sync a network's enabled SSIDs into NetBox as wireless.WirelessLAN
        records, scoped to a per-site WirelessLANGroup (so the same SSID
        name reused at different schools doesn't collide), inheriting the
        site's tenant + tags.  Also attaches each synced WLAN to the radio
        interface of every MR access point already synced at this site,
        since Meraki SSIDs are broadcast network-wide rather than
        configured per-AP.

        Returns human-readable errors the same way sync_ipam does.
        """
        errors: list[str] = []
        if not ssids or site is None:
            return errors

        wlan_group = self._get_wireless_lan_group(site)
        synced_wlans = []

        for cs in ssids:
            try:
                with transaction.atomic():
                    wlan = self._sync_wireless_lan(cs, site, wlan_group)
                    if wlan is not None:
                        synced_wlans.append(wlan)
            except Exception as exc:
                log.exception(
                    "Syncer: failed to sync SSID %s (%s) for site %s",
                    cs.number, cs.name, site,
                )
                errors.append(f"SSID {cs.number} ({cs.name!r}): {exc!r}")

        if synced_wlans and not self.dry_run:
            try:
                self._attach_wireless_lans_to_aps(synced_wlans, site)
            except Exception as exc:
                log.exception(
                    "Syncer: failed to attach SSIDs to APs for site %s", site
                )
                errors.append(f"Attaching SSIDs to APs: {exc!r}")

        if not self.dry_run:
            self.log.save(update_fields=["wireless_lans_synced"])

        return errors

    def _get_wireless_lan_group(self, site):
        """Get-or-create a WirelessLANGroup scoped to this site, mirroring _get_vlan_group."""
        from wireless.models import WirelessLANGroup

        if self.dry_run:
            return None

        name = f"{site.name} WLANs"
        group = WirelessLANGroup.objects.filter(name=name).first()
        if group is not None:
            return group

        field_names = {f.name for f in WirelessLANGroup._meta.get_fields()}
        kwargs: dict = {"name": name, "slug": slugify(name)}

        if {"scope_type", "scope_id"} <= field_names:
            from django.contrib.contenttypes.models import ContentType
            kwargs["scope_type"] = ContentType.objects.get_for_model(site)
            kwargs["scope_id"] = site.pk
        elif self._is_settable_fk(WirelessLANGroup, "site"):
            kwargs["site"] = site

        try:
            group, _ = WirelessLANGroup.objects.get_or_create(name=name, defaults=kwargs)
        except Exception as exc:
            log.warning("Syncer: could not create WirelessLANGroup %s: %s", name, exc)
            return None

        self._apply_site_scope(group, site)
        return group

    def _sync_wireless_lan(self, cs: CollectedSsid, site, wlan_group):
        """
        Get-or-create a wireless.WirelessLAN for one collected SSID.  If the
        SSID is bridged to a single VLAN (see collector._extract_ssid_vlan_id
        — NAT-mode SSIDs and ones without VLAN tagging have vlan_id=None),
        that VLAN is looked up in this site's VLANGroup and attached, so
        NetBox reflects which subnet the SSID's clients actually land on.
        """
        from wireless.models import WirelessLAN

        if self.dry_run:
            self.log.wireless_lans_synced += 1
            return None

        lookup: dict = {"ssid": cs.name}
        if wlan_group is not None:
            lookup["group"] = wlan_group

        wlan = WirelessLAN.objects.filter(**lookup).first()
        auth_type = self._map_meraki_auth_mode(cs.auth_mode)
        vlan = self._find_vlan_by_id(cs.vlan_id, site) if cs.vlan_id else None

        if wlan is None:
            create_kwargs = dict(lookup)
            create_kwargs["status"] = "active"
            # Build description with SSID details
            description_parts = [f"Meraki SSID {cs.number}"]
            if not cs.broadcast:
                description_parts.append("(hidden)")
            if cs.band_steering:
                description_parts.append("band-steering enabled")
            if cs.client_limit > 0:
                description_parts.append(f"max {cs.client_limit} clients")
            create_kwargs["description"] = " ".join(description_parts)
            
            if auth_type:
                create_kwargs["auth_type"] = auth_type
            if vlan is not None:
                create_kwargs["vlan"] = vlan
            wlan = WirelessLAN.objects.create(**create_kwargs)
            self.log.wireless_lans_synced += 1
            log.info("Syncer: created WirelessLAN %s", cs.name)
        else:
            if not self.dry_run and hasattr(wlan, "snapshot"):
                wlan.snapshot()  # pre-change state for changelog diff
            changed = []
            if auth_type and wlan.auth_type != auth_type:
                wlan.auth_type = auth_type
                changed.append("auth_type")
            if wlan.status != "active":
                wlan.status = "active"
                changed.append("status")
            
            # Update description with current SSID details
            description_parts = [f"Meraki SSID {cs.number}"]
            if not cs.broadcast:
                description_parts.append("(hidden)")
            if cs.band_steering:
                description_parts.append("band-steering enabled")
            if cs.client_limit > 0:
                description_parts.append(f"max {cs.client_limit} clients")
            new_description = " ".join(description_parts)
            if wlan.description != new_description:
                wlan.description = new_description
                changed.append("description")
            
            if vlan is not None and wlan.vlan_id != vlan.pk:
                wlan.vlan = vlan
                changed.append("vlan")
            if changed and not self.dry_run:
                wlan.save(update_fields=changed)
            self.log.wireless_lans_synced += 1

        self._apply_site_scope(wlan, site)
        return wlan

    @staticmethod
    def _map_meraki_auth_mode(auth_mode: str) -> str:
        """Best-effort map of Meraki authMode -> NetBox WirelessAuthTypeChoices."""
        if not auth_mode:
            return ""
        if auth_mode == "open":
            return "open"
        if auth_mode == "psk":
            return "wpa-personal"
        if "8021x" in auth_mode or "radius" in auth_mode:
            return "wpa-enterprise"
        return ""

    def _attach_wireless_lans_to_aps(self, wlans: list, site) -> None:
        """
        Attach every synced WirelessLAN to the radio interface of each MR
        access point already synced at this site (Meraki SSIDs broadcast
        network-wide, so there's no per-AP SSID assignment to read from
        the API).
        """
        from dcim.models import Interface

        radios = Interface.objects.filter(
            device__site=site, name="Radio 0",
        )
        for iface in radios:
            iface.wireless_lans.add(*wlans)

    def close(self, *, success: bool = True, message: str = "") -> None:
        """Stamp the SyncLog with a completion time and status."""
        from .choices import SyncStatusChoices
        self.log.completed_at = datetime.now(tz=timezone.utc)
        self.log.status  = (
            SyncStatusChoices.SUCCESS if success else SyncStatusChoices.FAILED
        )
        self.log.message = message
        if not self.dry_run:
            self.log.save()

    # ------------------------------------------------------------------
    # Per-device sync
    # ------------------------------------------------------------------

    def _sync_device(self, dev: CollectedDevice, site) -> None:
        from dcim.models import Device

        manufacturer = self._get_manufacturer()
        device_type  = self._get_device_type(dev.model, manufacturer)
        role         = self._get_role(dev.family)
        tag          = self._get_meraki_tag()

        # Match existing device by serial number first, then fall back to name
        device = (
            Device.objects.filter(serial=dev.serial).first()
            or Device.objects.filter(
                site=site, name=dev.name
            ).first()
        )

        tenant = site.tenant if site else None
        if device is None:
            if not self.dry_run:
                device = Device.objects.create(
                    name        = dev.name,
                    serial      = dev.serial,
                    device_type = device_type,
                    role        = role,
                    site        = site,
                    tenant      = tenant,  # Inherits Tenant from parent Site
                    status      = "active",
                    latitude    = site.latitude if site else None,
                    longitude   = site.longitude if site else None,
                )
            self.log.devices_created += 1
            log.info("Syncer: created device %s (%s)", dev.name, dev.serial)
        else:
            if not self.dry_run and hasattr(device, "snapshot"):
                device.snapshot()  # pre-change state for changelog diff
            changed = []
            if device.name != dev.name:
                device.name = dev.name
                changed.append("name")
            if device.serial != dev.serial:
                device.serial = dev.serial
                changed.append("serial")
            if device.device_type_id != device_type.pk:
                device.device_type = device_type
                changed.append("device_type")
            if device.site_id != site.pk:
                device.site = site
                changed.append("site")
            if device.tenant_id != (tenant.pk if tenant else None):
                device.tenant = tenant
                changed.append("tenant")
            if device.status != "active":
                device.status = "active"
                changed.append("status")
            
            # Sync GPS coordinates from parent Site
            site_lat = site.latitude if site else None
            site_lon = site.longitude if site else None
            if device.latitude != site_lat:
                device.latitude = site_lat
                changed.append("latitude")
            if device.longitude != site_lon:
                device.longitude = site_lon
                changed.append("longitude")
            
            if changed and not self.dry_run:
                device.save(update_fields=changed)
            self.log.devices_updated += 1
            log.debug("Syncer: updated device %s (%s)", dev.name, dev.serial)

        # Apply default "meraki" tag as well as all tags assigned to the parent Site
        if device and not self.dry_run:
            tags_to_add = []
            if tag:
                tags_to_add.append(tag)
            if site and site.tags.exists():
                tags_to_add.extend(list(site.tags.all()))
            if tags_to_add:
                device.tags.add(*tags_to_add)

        if device is None:
            # dry_run — nothing more to do
            return

        # Sync custom fields: firmware, Meraki serial cross-ref
        self._sync_device_custom_fields(device, dev)

        # Sync interfaces, MACs, IPs
        for port in dev.ports:
            iface = self._sync_interface(device, port, dev.family)
            if iface is None:
                continue

            # MACs on this port
            port_macs = [m for m in dev.macs if m.port_id == port.port_id]
            for cm in port_macs:
                self._sync_mac(iface, cm.mac)

            # LAN IP on the first port (for switches and APs)
            if self.sync_ips and dev.lan_ip and port.port_id in ("1", "0", "mgmt", "lan", "radio0"):
                self._sync_ip(iface, dev.lan_ip, site=site)

        # WAN IPs on uplink ports (MX appliances).  _collect_appliance names
        # these interfaces "WAN 1"/"WAN 2" (not the raw port_id "wan1"/
        # "wan2"), so look them up by the same display name it used.
        #
        # WAN uplinks are ISP-assigned public addresses, not part of a LAN
        # subnet Meraki manages — so unlike LAN-side device IPs (which get
        # the subnet's real prefix length), these are always synced as a
        # standalone /32 host address.
        if self.sync_ips:
            wan_map = {"WAN 1": dev.wan1_ip, "WAN 2": dev.wan2_ip}
            for iface_name, ip in wan_map.items():
                if not ip:
                    continue
                iface = self._find_interface(device, iface_name)
                if iface:
                    self._sync_ip(iface, ip, prefix_length=32, site=site)

        # Primary IPv4: prefer the LAN IP, then WAN1, then WAN2 — whichever
        # was actually synced onto one of this device's interfaces above.
        if self.sync_ips and not self.dry_run:
            primary_candidate = dev.lan_ip or dev.wan1_ip or dev.wan2_ip
            if primary_candidate:
                self._set_primary_ip4(device, primary_candidate)

    # ------------------------------------------------------------------
    # Primary IP assignment
    # ------------------------------------------------------------------

    def _set_primary_ip4(self, device, ip_str: str) -> None:
        """
        Point device.primary_ip4 at the IPAddress matching ip_str that is
        assigned to one of this device's interfaces (created earlier in
        _sync_device via _sync_ip).  No-ops if no matching, interface-
        assigned IPAddress exists yet.
        """
        from ipam.models import IPAddress as IPAddr
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType

        iface_ids = list(Interface.objects.filter(device=device).values_list("pk", flat=True))
        if not iface_ids:
            return

        iface_ct = ContentType.objects.get_for_model(Interface)
        candidates = IPAddr.objects.filter(
            assigned_object_type=iface_ct,
            assigned_object_id__in=iface_ids,
        )
        match = next(
            (ip for ip in candidates if str(ip.address).split("/", 1)[0] == ip_str),
            None,
        )
        if match is None:
            return

        if device.primary_ip4_id != match.pk:
            device.primary_ip4 = match
            device.save(update_fields=["primary_ip4"])
            log.debug("Syncer: set primary IPv4 %s on device %s", ip_str, device.name)

    # ------------------------------------------------------------------
    # Interface sync
    # ------------------------------------------------------------------

    def _sync_interface(self, device, port, family: str):
        from dcim.models import Interface

        iface_type = self._iface_type(family, port.speed_mbps)
        iface = Interface.objects.filter(device=device, name=port.name).first()

        if iface is None:
            if not self.dry_run:
                iface = Interface.objects.create(
                    device      = device,
                    name        = port.name,
                    type        = iface_type,
                    enabled     = port.enabled,
                    description = port.description,
                    speed      = self._speed_to_netbox_value(port.speed_mbps),
                    duplex     = port.duplex or "auto",
                    mode       = self._vlan_mode_to_netbox(port.vlan_mode, port.vlan_id),
                    untagged_vlan = self._get_vlan_by_id(port.vlan_id, device.site) if port.vlan_mode == "access" and port.vlan_id else None,
                )
                # Set M2M relationships after creation
                if port.vlan_mode == "trunk" and port.allowed_vlans:
                    tagged = self._parse_allowed_vlans(port.allowed_vlans, device.site)
                    if tagged:
                        iface.tagged_vlans.set(tagged)
            self.log.interfaces_synced += 1
        else:
            if not self.dry_run and hasattr(iface, "snapshot"):
                iface.snapshot()  # pre-change state for changelog diff
            changed = []
            if iface.enabled != port.enabled:
                iface.enabled = port.enabled
                changed.append("enabled")
            if iface.description != port.description:
                iface.description = port.description
                changed.append("description")
            
            # Update speed and duplex from NetBox built-in fields
            nb_speed = self._speed_to_netbox_value(port.speed_mbps)
            if iface.speed != nb_speed:
                iface.speed = nb_speed
                changed.append("speed")
            
            nb_duplex = port.duplex or "auto"
            if iface.duplex != nb_duplex:
                iface.duplex = nb_duplex
                changed.append("duplex")
            
            # Update VLAN mode and tagging
            nb_mode = self._vlan_mode_to_netbox(port.vlan_mode, port.vlan_id)
            if str(iface.mode) != nb_mode:
                iface.mode = nb_mode
                changed.append("mode")
            
            # Update untagged VLAN (scalar field)
            if port.vlan_mode == "access" and port.vlan_id:
                untagged = self._get_vlan_by_id(port.vlan_id, device.site)
                if iface.untagged_vlan != untagged:
                    iface.untagged_vlan = untagged
                    changed.append("untagged_vlan")
            elif port.vlan_mode != "access" and iface.untagged_vlan is not None:
                iface.untagged_vlan = None
                changed.append("untagged_vlan")
            
            if changed and not self.dry_run:
                iface.save(update_fields=changed)
            
            # Update tagged VLANs (M2M field) separately
            if not self.dry_run:
                if port.vlan_mode == "trunk" and port.allowed_vlans:
                    tagged = self._parse_allowed_vlans(port.allowed_vlans, device.site)
                    # Compare by VLAN IDs
                    existing_tagged_ids = set(v.vid for v in iface.tagged_vlans.all()) if iface.tagged_vlans else set()
                    new_tagged_ids = set(v.vid for v in tagged) if tagged else set()
                    if existing_tagged_ids != new_tagged_ids:
                        iface.tagged_vlans.set(tagged)
                elif iface.tagged_vlans.exists():
                    # Clear tagged VLANs if no longer trunk mode
                    iface.tagged_vlans.clear()
            
            self.log.interfaces_synced += 1

        # Write PoE configuration to custom fields
        if not self.dry_run:
            self._sync_interface_poe_fields(iface, port)

        return iface

    def _find_interface(self, device, port_id: str):
        from dcim.models import Interface
        return Interface.objects.filter(device=device, name=port_id).first()

    # ------------------------------------------------------------------
    # MAC address sync
    # ------------------------------------------------------------------

    def _sync_mac(self, iface, mac_str: str) -> None:
        """
        Create a MACAddress record linked to the given interface if it does
        not already exist.  Existing MACs are not modified (they may be
        managed by another tool or have analyst notes).
        """
        from dcim.models import MACAddress
        from django.contrib.contenttypes.models import ContentType

        if not mac_str or self.dry_run:
            return

        iface_ct = ContentType.objects.get_for_model(iface)
        exists = MACAddress.objects.filter(mac_address=mac_str).exists()
        if not exists:
            try:
                MACAddress.objects.create(
                    mac_address         = mac_str,
                    assigned_object_type = iface_ct,
                    assigned_object_id   = iface.pk,
                )
                self.log.macs_synced += 1
            except Exception as exc:
                log.debug("Syncer: could not create MAC %s: %s", mac_str, exc)

    # ------------------------------------------------------------------
    # IP address sync
    # ------------------------------------------------------------------

    def _sync_ip(self, iface, ip_str: str, prefix_length: int = 24, *, site=None) -> None:
        """
        Create an IPAddress record linked to the interface if it does not
        exist.  Uses /24 as the default prefix length when none is
        available from Meraki (callers pass /32 for standalone host
        addresses, e.g. MX WAN uplinks, that aren't part of a Meraki-managed
        LAN subnet).

        Looked up by host address alone (not the full address/mask string),
        so if a previous sync recorded this host with a different mask —
        e.g. before WAN uplinks were corrected from /24 to /32 — the
        existing record's mask is corrected in place instead of leaving the
        old one behind and creating a duplicate.

        Inherits tenant + tags from `site` the same way every other synced
        IPAM object does, when a site is supplied.
        """
        from ipam.models import IPAddress as IPAddr
        from django.contrib.contenttypes.models import ContentType

        if not ip_str or self.dry_run:
            return

        address = f"{ip_str}/{prefix_length}"
        iface_ct = ContentType.objects.get_for_model(iface)

        ip_obj = IPAddr.objects.filter(address__net_host=ip_str).first()
        if ip_obj is None:
            try:
                ip_obj = IPAddr.objects.create(
                    address             = address,
                    status              = "active",
                    assigned_object_type = iface_ct,
                    assigned_object_id   = iface.pk,
                )
                self.log.ips_synced += 1
            except Exception as exc:
                log.debug("Syncer: could not create IP %s: %s", address, exc)
                return
        else:
            if not self.dry_run and hasattr(ip_obj, "snapshot"):
                ip_obj.snapshot()  # pre-change state for changelog diff
            changed = []
            if str(ip_obj.address) != address:
                ip_obj.address = address
                changed.append("address")
            # Assign to this interface if currently unassigned
            if ip_obj.assigned_object_id is None:
                ip_obj.assigned_object_type = iface_ct
                ip_obj.assigned_object_id   = iface.pk
                changed.extend(["assigned_object_type", "assigned_object_id"])
            if changed and not self.dry_run:
                ip_obj.save(update_fields=changed)

        if site is not None:
            self._apply_site_scope(ip_obj, site)

    # ------------------------------------------------------------------
    # Custom fields on Device
    # ------------------------------------------------------------------

    def _sync_device_custom_fields(self, device, dev: CollectedDevice) -> None:
        """
        Write Meraki-specific data into custom fields on the Device record.
        Custom fields are created on first use if absent.
        """
        if self.dry_run:
            return

        updates: dict[str, object] = {}

        if dev.firmware:
            self._ensure_device_cf("meraki_firmware", "Meraki Firmware")
            updates["meraki_firmware"] = dev.firmware

        if dev.serial:
            self._ensure_device_cf("meraki_serial", "Meraki Serial")
            updates["meraki_serial"] = dev.serial

        if dev.tags:
            self._ensure_device_cf("meraki_tags", "Meraki Tags")
            updates["meraki_tags"] = ", ".join(dev.tags)

        if updates:
            device.custom_field_data.update(updates)
            device.save(update_fields=["custom_field_data"])

        # Sync uplink configuration if this is an MX with uplinks
        if dev.uplinks:
            self._sync_device_uplink_fields(device, dev.uplinks)

    def _sync_interface_poe_fields(self, iface, port) -> None:
        """
        Write PoE configuration to custom fields on the Interface record.
        Only updates if PoE is enabled or if limits are set.
        """
        if self.dry_run or not iface or not port.poe_enabled:
            return

        self._ensure_interface_poe_cf()
        if not iface.custom_field_data:
            iface.custom_field_data = {}

        changed = False
        if port.poe_enabled:
            if iface.custom_field_data.get("meraki_poe_enabled") != True:
                iface.custom_field_data["meraki_poe_enabled"] = True
                changed = True
        
        if port.poe_limit_w > 0:
            if iface.custom_field_data.get("meraki_poe_limit_w") != port.poe_limit_w:
                iface.custom_field_data["meraki_poe_limit_w"] = port.poe_limit_w
                changed = True

        if changed:
            iface.save(update_fields=["custom_field_data"])
            log.info(
                "Syncer: updated PoE config for %s.%s (enabled=%s, limit=%sW)",
                iface.device.name, iface.name, port.poe_enabled, port.poe_limit_w,
            )


    def _sync_device_uplink_fields(self, device, uplinks: list) -> None:
        """
        Write MX uplink configuration to device custom fields.  Stores uplink
        modes and failover roles in a JSON format for easy reading.
        """
        if self.dry_run or not device or not uplinks:
            return

        self._ensure_device_uplink_cf()
        if not device.custom_field_data:
            device.custom_field_data = {}

        # Build a summary of uplink config for each interface
        uplink_summary = []
        names = {"wan1": "WAN 1", "wan2": "WAN 2", "cellular": "Cellular"}
        for ul in uplinks:
            summary = names.get(ul.interface, ul.interface)
            details = [d for d in (ul.failover_role, ul.status) if d]
            if details:
                summary += f" ({', '.join(details)})"
            uplink_summary.append(summary)

        if uplink_summary:
            uplink_str = ", ".join(uplink_summary)
            if device.custom_field_data.get("meraki_uplink_config") != uplink_str:
                device.custom_field_data["meraki_uplink_config"] = uplink_str
                device.save(update_fields=["custom_field_data"])
                log.info(
                    "Syncer: updated uplink config for %s: %s",
                    device.name, uplink_str,
                )

    def _sync_virtual_chassis(self, dev: CollectedDevice, site) -> None:
        """
        Create or update a VirtualChassis for a Meraki switch stack.  All
        member switches are linked to the chassis, and custom fields are
        set to track Meraki stack metadata (stack ID, stack name, member
        role).  The chassis name is updated on every run if the stack name
        has changed (e.g., if the Meraki stack was renamed or if a previous
        name was truncated due to length constraints).
        """
        from dcim.models import Device, VirtualChassis

        if self.dry_run or not dev.stack_membership:
            return

        sm = dev.stack_membership

        # Get the master device
        master = Device.objects.filter(serial=sm.master_serial).first()
        if not master:
            log.warning(
                "Syncer: master device %s not found for stack %s; "
                "skipping chassis creation",
                sm.master_serial, sm.stack_id,
            )
            return

        # Get-or-create VirtualChassis linked to the master device
        chassis_name = f"{site.name} - {sm.stack_name}"
        chassis, created = VirtualChassis.objects.get_or_create(
            master=master,
            defaults={"name": chassis_name},
        )

        changed = []
        if created:
            log.info(
                "Syncer: created VirtualChassis %s for Meraki stack %s",
                chassis_name, sm.stack_id,
            )
        else:
            # Update name if it has changed (e.g., stack renamed, or truncation
            # has been corrected)
            if chassis.name != chassis_name:
                old_name = chassis.name
                chassis.name = chassis_name
                changed.append("name")
                log.info(
                    "Syncer: updated VirtualChassis name from '%s' to '%s'",
                    old_name, chassis_name,
                )

        # Ensure this device is in the chassis member list
        member = Device.objects.filter(serial=dev.serial).first()
        if member:
            if member not in chassis.members.all():
                chassis.members.add(member)
                log.info(
                    "Syncer: added device %s to VirtualChassis %s",
                    dev.serial, chassis_name,
                )

        # Save if anything changed
        if changed and not self.dry_run:
            chassis.save(update_fields=changed)

        # Write stack metadata to custom fields on the device
        if not self.dry_run:
            self._ensure_device_cf("meraki_stack_id", "Meraki Stack ID")
            self._ensure_device_cf("meraki_stack_name", "Meraki Stack Name")
            self._ensure_device_cf("meraki_stack_role", "Meraki Stack Role")

            device = Device.objects.filter(serial=dev.serial).first()
            if device:
                device.custom_field_data["meraki_stack_id"] = sm.stack_id
                device.custom_field_data["meraki_stack_name"] = sm.stack_name
                device.custom_field_data["meraki_stack_role"] = sm.member_role
                device.save(update_fields=["custom_field_data"])

    # ------------------------------------------------------------------
    # IPAM sync (network-level VLANs / subnets)
    # ------------------------------------------------------------------

    def _get_vlan_group(self, site):
        """
        Get-or-create a VLANGroup scoped to this site, named "{site} VLANs",
        so that VLAN IDs collected from different Meraki networks/sites
        don't collide.  Uses generic scope (scope_type/scope_id) when the
        running NetBox version supports it; falls back to an unscoped,
        name-only group on older schemas.
        """
        from ipam.models import VLANGroup

        if self.dry_run:
            return None

        name = f"{site.name} VLANs"
        group = VLANGroup.objects.filter(name=name).first()
        if group is not None:
            return group

        field_names = {f.name for f in VLANGroup._meta.get_fields()}
        kwargs: dict = {"name": name, "slug": slugify(name)}

        if {"scope_type", "scope_id"} <= field_names:
            from django.contrib.contenttypes.models import ContentType
            kwargs["scope_type"] = ContentType.objects.get_for_model(site)
            kwargs["scope_id"] = site.pk
        elif self._is_settable_fk(VLANGroup, "site"):
            kwargs["site"] = site

        try:
            group, _ = VLANGroup.objects.get_or_create(
                name=name, defaults=kwargs
            )
        except Exception as exc:
            log.warning("Syncer: could not create VLANGroup %s: %s", name, exc)
            return None

        self._apply_site_scope(group, site)
        return group


    def _get_or_create_vrf(self, site) -> "VRF":
        """Get or create a VRF named '{site_name} VRF' for this site."""
        from ipam.models import VRF
        
        if self.dry_run or site is None:
            return None
        
        vrf_name = f"{site.name} VRF"
        vrf = VRF.objects.filter(name=vrf_name).first()
        if vrf is None:
            vrf = VRF.objects.create(name=vrf_name)
            log.info("Syncer: created VRF %s", vrf_name)
        return vrf


    def _apply_site_scope(self, obj, site) -> None:
        """
        Inherit tenant + tags from the parent Site onto a VLAN or Prefix,
        the same way _sync_device does for synced Devices: the Meraki tag
        plus every tag assigned to the Site, and the Site's Tenant.
        """
        if self.dry_run or obj is None:
            return

        tenant = site.tenant if site else None
        if self._is_settable_fk(type(obj), "tenant") and obj.tenant_id != (
            tenant.pk if tenant else None
        ):
            obj.tenant = tenant
            obj.save(update_fields=["tenant"])

        tags_to_add = []
        tag = self._get_meraki_tag()
        if tag:
            tags_to_add.append(tag)
        if site and site.tags.exists():
            tags_to_add.extend(list(site.tags.all()))
        if tags_to_add:
            obj.tags.add(*tags_to_add)

    def _sync_vlan(self, cv: CollectedVlan, site, vlan_group, vrf=None):
        """
        Get-or-create an ipam.VLAN for this Meraki VLAN.  Networks without
        VLANs enabled ("single LAN", vlan_id=0) don't map to a real 802.1Q
        VLAN, so no VLAN object is created for those — only the Prefix is.
        """
        from ipam.models import VLAN

        if cv.vlan_id == 0:
            return None
        if self.dry_run:
            self.log.vlans_synced += 1
            return None

        lookup: dict = {"vid": cv.vlan_id}
        if vlan_group is not None:
            lookup["group"] = vlan_group
        elif self._is_settable_fk(VLAN, "site"):
            lookup["site"] = site

        vlan = VLAN.objects.filter(**lookup).first()
        if vlan is None:
            create_kwargs = dict(lookup)
            create_kwargs["name"] = cv.name
            create_kwargs["status"] = "active"
            if vlan_group is None and self._is_settable_fk(VLAN, "site"):
                create_kwargs["site"] = site
            if vrf is not None and self._is_settable_fk(VLAN, "vrf"):
                create_kwargs["vrf"] = vrf
            vlan = VLAN.objects.create(**create_kwargs)
            self.log.vlans_synced += 1
            log.info("Syncer: created VLAN %s (%s)", cv.vlan_id, cv.name)
        else:
            if not self.dry_run and hasattr(vlan, "snapshot"):
                vlan.snapshot()  # pre-change state for changelog diff
            changed = []
            if vlan.name != cv.name:
                vlan.name = cv.name
                changed.append("name")
            if vrf is not None and self._is_settable_fk(VLAN, "vrf") and vlan.vrf_id != vrf.pk:
                vlan.vrf = vrf
                changed.append("vrf")
            if changed and not self.dry_run:
                vlan.save(update_fields=changed)
            self.log.vlans_synced += 1

        self._apply_site_scope(vlan, site)
        return vlan

    def _sync_prefix(self, cv: CollectedVlan, site, nb_vlan, vrf=None) -> None:
        """Get-or-create an ipam.Prefix for this VLAN/LAN's subnet."""
        if not cv.subnet or self.dry_run:
            return
        self._get_or_create_prefix(cv.subnet, site, vlan=nb_vlan, vrf=vrf)

    def _sync_static_route(self, sr: CollectedStaticRoute, site, vrf=None) -> None:
        """
        Get-or-create an ipam.Prefix (and matching ipam.IPRange) for a
        Meraki appliance static route's subnet.  If the static route
        matched a Layer 3 VLAN (on the MX or an upstream switch), create
        the VLAN object in NetBox if it doesn't exist, then link the
        Prefix to it.  If a matching VLAN already exists in NetBox (by
        subnet match), the Prefix is linked to that VLAN.  Disabled routes
        are skipped.
        """
        if not sr.subnet or not sr.enabled:
            return

        description = sr.name
        if sr.next_hop_ip:
            description = f"{sr.name} (static route via {sr.next_hop_ip})"

        vlan = None

        if not self.dry_run:
            # If the static route has a VLAN ID (matched from Meraki VLANs
            # or switch L3 interfaces), create or find the VLAN in NetBox —
            # unless the interface/route is generically named "Reserved"
            # (optionally with a trailing number): NetBox's VLAN uniqueness
            # is on (group, name), not vid, so several distinct "Reserved"
            # VLANs at different IDs on the same switch would collide on
            # that shared name.  The Prefix and IP range are still created,
            # just without a VLAN link.
            if sr.vlan_id > 0 and not self._is_reserved_placeholder_name(
                sr.vlan_name or sr.name
            ):
                vlan = self._sync_vlan_for_static_route(sr, site)

            # If no VLAN from route, try to find an existing VLAN for this subnet
            if vlan is None:
                vlan = self._find_vlan_for_subnet(sr.subnet, site)

            prefix = self._get_or_create_prefix(
                sr.subnet, site, vlan=vlan, description=description,
                is_static_route=True, vrf=vrf,
            )
            if prefix is not None:
                self.log.static_routes_synced += 1

            self._sync_ip_range_for_subnet(sr.subnet, site, description=description)

    @staticmethod
    def _is_reserved_placeholder_name(name: str) -> bool:
        """
        True for a generic placeholder name like "Reserved", "Reserved1",
        "Reserved 2", etc. — used to skip VLAN creation for switch
        interfaces/static routes that share this non-unique name across
        multiple distinct VLAN IDs (see _sync_static_route).
        """
        import re
        return bool(re.match(r"(?i)^reserved\s*\d*$", (name or "").strip()))

    def _sync_vlan_for_static_route(self, sr: CollectedStaticRoute, site):
        """
        Get-or-create a VLAN for a static route that matched a Layer 3 VLAN
        (on the MX or an upstream switch) in Meraki.  Named after the VLAN
        interface itself when Meraki supplied one, falling back to the
        static route's own name.
        """
        from ipam.models import VLAN, VLANGroup

        vlan_group = self._get_vlan_group(site)
        vlan_name = sr.vlan_name or sr.name

        try:
            vlan, created = VLAN.objects.get_or_create(
                vid=sr.vlan_id,
                group=vlan_group,
                defaults={
                    "name": vlan_name,
                }
            )
            if created:
                self._apply_site_scope(vlan, site)
                log.info(
                    "Syncer: created VLAN %d (%s) for static route %s",
                    sr.vlan_id, vlan_name, sr.subnet,
                )
            return vlan
        except Exception as exc:
            log.warning(
                "Syncer: failed to create VLAN %d for static route %s: %s",
                sr.vlan_id, sr.subnet, exc,
            )
            return None

    def _find_vlan_by_id(self, vlan_id: int, site) -> Optional[object]:
        """
        Look up a VLAN by its 802.1Q VLAN ID (vid), preferring one scoped to
        this site's VLANGroup (where VLANs/static routes create theirs) and
        falling back to any VLAN with that vid if none is found there.  Used
        to attach a bridged SSID's WirelessLAN to the VLAN its clients
        actually land on.
        """
        from ipam.models import VLAN

        try:
            vlan_group = self._get_vlan_group(site)
            if vlan_group is not None:
                vlan = VLAN.objects.filter(vid=vlan_id, group=vlan_group).first()
                if vlan:
                    return vlan
            return VLAN.objects.filter(vid=vlan_id).first()
        except Exception as exc:
            log.debug(
                "Syncer: failed to find VLAN %s for site %s: %s",
                vlan_id, site, exc,
            )
            return None

    def _find_vlan_for_subnet(self, subnet: str, site) -> Optional[object]:
        """
        Search for an existing VLAN in NetBox that has a Prefix matching
        this subnet.  Used to link static-route Prefixes to upstream Layer 3
        VLANs.  Returns the VLAN if found, None otherwise.
        """
        from ipam.models import Prefix, VLAN

        try:
            # Find a Prefix matching this subnet
            prefix_obj = Prefix.objects.filter(prefix=subnet).first()
            if prefix_obj and prefix_obj.vlan_id:
                return prefix_obj.vlan
            # If no Prefix, try finding a VLAN directly by site scope
            # (in case the VLAN exists but has no Prefix yet)
            vlan = VLAN.objects.filter(
                name__icontains=subnet.split("/")[0]
            ).first()
            if vlan:
                return vlan
        except Exception as exc:
            log.debug(
                "Syncer: failed to find VLAN for subnet %s: %s",
                subnet, exc,
            )
        return None

    def _get_or_create_prefix(
        self, subnet: str, site, *, vlan=None, description: str = "",
        is_static_route: bool = False, vrf=None,
    ):
        """
        Shared get-or-create for ipam.Prefix, used by both VLAN/single-LAN
        subnets and static-route subnets.  Applies site tenant/tags either
        way; only overwrites description/vlan on an existing prefix when a
        static route supplies a description (VLAN subnets never blank out
        a description a static route sync may have set). Prefixes are scoped
        to a per-site VRF if vrf is provided.
        """
        from ipam.models import Prefix

        if self.dry_run:
            if not is_static_route:
                self.log.prefixes_synced += 1
            return None

        lookup_kwargs = {"prefix": subnet}
        if vrf is not None:
            lookup_kwargs["vrf"] = vrf
        prefix = Prefix.objects.filter(**lookup_kwargs).first()
        if prefix is None:
            create_kwargs: dict = {"prefix": subnet, "status": "active"}
            if vlan is not None:
                create_kwargs["vlan"] = vlan
            if description:
                create_kwargs["description"] = description
            if vrf is not None:
                create_kwargs["vrf"] = vrf
            if {"scope_type", "scope_id"} <= {
                f.name for f in Prefix._meta.get_fields()
            }:
                from django.contrib.contenttypes.models import ContentType
                create_kwargs["scope_type"] = ContentType.objects.get_for_model(site)
                create_kwargs["scope_id"] = site.pk
            elif self._is_settable_fk(Prefix, "site"):
                create_kwargs["site"] = site
            # Let creation errors propagate to sync_ipam's per-item try/except
            # so the real cause (bad field name, validation error, etc.) gets
            # surfaced to the console instead of silently vanishing.
            prefix = Prefix.objects.create(**create_kwargs)
            if not is_static_route:
                self.log.prefixes_synced += 1
            log.info("Syncer: created prefix %s", subnet)
        else:
            if not self.dry_run and hasattr(prefix, "snapshot"):
                prefix.snapshot()  # pre-change state for changelog diff
            changed = []
            if vlan is not None and prefix.vlan_id != vlan.pk:
                prefix.vlan = vlan
                changed.append("vlan")
            if description and prefix.description != description:
                prefix.description = description
                changed.append("description")
            if changed and not self.dry_run:
                prefix.save(update_fields=changed)
            if not is_static_route:
                self.log.prefixes_synced += 1

        self._apply_site_scope(prefix, site)
        return prefix

    def _sync_vlan_ip(self, cv: CollectedVlan, site) -> None:
        """
        Create the IPAddress for the MX appliance's interface on this
        VLAN/LAN, and attach it to the matching interface if the MX device
        for this site has already been synced (sync_devices runs first).
        """
        if not cv.appliance_ip or not cv.subnet or self.dry_run:
            return

        from ipam.models import IPAddress as IPAddr
        from dcim.models import Interface, Device
        from django.contrib.contenttypes.models import ContentType

        try:
            prefix_length = cv.subnet.split("/", 1)[1]
        except IndexError:
            prefix_length = "24"
        address = f"{cv.appliance_ip}/{prefix_length}"

        # Interface naming: single-LAN → "LAN", VLAN N → "VLAN{N}" (matches
        # the interface name convention used for MX ports elsewhere in this
        # plugin; adjust here if you rename MX VLAN interfaces).
        iface_name = "LAN" if cv.vlan_id == 0 else f"VLAN{cv.vlan_id}"
        iface = (
            Interface.objects.filter(
                device__site=site, device__device_type__model__startswith="MX",
                name=iface_name,
            ).first()
        )

        ip_obj = IPAddr.objects.filter(address=address).first()
        if ip_obj is None:
            create_kwargs: dict = {"address": address, "status": "active"}
            if iface is not None:
                create_kwargs["assigned_object_type"] = ContentType.objects.get_for_model(iface)
                create_kwargs["assigned_object_id"] = iface.pk
            # Let creation errors propagate — see _get_or_create_prefix.
            ip_obj = IPAddr.objects.create(**create_kwargs)
            self.log.ips_synced += 1
        elif ip_obj.assigned_object_id is None and iface is not None:
            ip_obj.assigned_object_type = ContentType.objects.get_for_model(iface)
            ip_obj.assigned_object_id = iface.pk
            ip_obj.save(update_fields=["assigned_object_type", "assigned_object_id"])

        self._apply_site_scope(ip_obj, site)

    def _sync_vlan_ip_range(self, cv: CollectedVlan, site) -> None:
        """
        Create an ipam.IPRange spanning this VLAN/LAN's usable host
        addresses.  Meraki's dhcpHandling and reservedIpRanges (if any) are
        folded into the range's description for visibility; the reserved
        sub-ranges aren't carved out as separate IPRange objects.
        """
        description = cv.dhcp_handling or ""
        if cv.reserved_ip_ranges:
            reserved_note = f"{len(cv.reserved_ip_ranges)} reserved range(s) in Meraki"
            description = f"{description} ({reserved_note})" if description else reserved_note

        self._sync_ip_range_for_subnet(cv.subnet, site, description=description)

    def _sync_ip_range_for_subnet(
        self, subnet: str, site, *, description: str = "",
    ) -> None:
        """
        Shared get-or-create for an ipam.IPRange spanning a subnet's usable
        host addresses (excluding the network and broadcast addresses), so
        the pool is represented distinctly from individual device
        IPAddress records — per NetBox's own IP Address vs. IP Range
        distinction (single hosts vs. "modeling pools... where individual
        tracking of every single address isn't needed").  Used for both
        VLAN/single-LAN subnets and static-route subnets (including ones
        matched to a Layer 3 VLAN on an upstream switch), so every synced
        Prefix gets a matching IP Range the same way.
        """
        import ipaddress
        import netaddr
        from ipam.models import IPRange

        if not subnet or self.dry_run:
            return

        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            log.debug("Syncer: could not parse subnet %s for IP range", subnet)
            return

        hosts = list(network.hosts())
        if not hosts:
            return  # e.g. /31, /32 — no usable range to represent

        start_address = netaddr.IPNetwork(f"{hosts[0]}/{network.prefixlen}")
        end_address   = netaddr.IPNetwork(f"{hosts[-1]}/{network.prefixlen}")

        ip_range = IPRange.objects.filter(
            start_address=start_address, end_address=end_address,
        ).first()

        if ip_range is None:
            try:
                create_kwargs = {
                    "start_address": start_address,
                    "end_address": end_address,
                    "status": "active",
                }
                if description:
                    create_kwargs["description"] = description
                ip_range = IPRange.objects.create(**create_kwargs)
                log.info(
                    "Syncer: created IP range %s-%s", start_address, end_address,
                )
            except Exception as exc:
                log.warning(
                    "Syncer: failed to create IP range for %s: %s",
                    subnet, exc,
                )
                return
        elif description and ip_range.description != description:
            ip_range.description = description
            ip_range.save(update_fields=["description"])

        self._apply_site_scope(ip_range, site)

    @staticmethod
    def _model_has_field(model, name: str) -> bool:
        return name in {f.name for f in model._meta.get_fields()}

    @staticmethod
    def _is_settable_fk(model, name: str) -> bool:
        """
        True only if `name` is a real forward FK/O2O field that can be
        passed as a constructor kwarg.  `_model_has_field` alone isn't
        enough: Django's `_meta.get_fields()` also returns reverse
        relations and M2M accessors (e.g. NetBox's Prefix/VLANGroup
        `site` in some versions is a reverse/M2M-style accessor, not a
        settable FK) — assigning those raises "Direct assignment to the
        reverse side of a related set is prohibited."
        """
        try:
            field = model._meta.get_field(name)
        except Exception:
            return False
        return bool(getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False))

    # ------------------------------------------------------------------
    # Lazy-loaded shared objects
    # ------------------------------------------------------------------

    def _get_manufacturer(self):
        if self._manufacturer is None:
            from dcim.models import Manufacturer
            self._manufacturer, _ = Manufacturer.objects.get_or_create(
                name=_MERAKI_MANUFACTURER,
                defaults={"slug": slugify(_MERAKI_MANUFACTURER)},
            )
        return self._manufacturer

    def _get_device_type(self, model: str, manufacturer):
        if model not in self._device_type_cache:
            from dcim.models import DeviceType
            dt, _ = DeviceType.objects.get_or_create(
                manufacturer=manufacturer,
                model=model,
                defaults={"slug": slugify(model)},
            )
            self._device_type_cache[model] = dt
        return self._device_type_cache[model]

    def _get_role(self, family: str):
        """
        Return a DeviceRole appropriate for the Meraki product family.
        Roles are created automatically if absent.
        """
        role_map = {
            "MS": ("switch",   "Switch",          "2196f3"),
            "MX": ("firewall", "Firewall",        "f44336"),
            "MR": ("ap",       "Access Point",    "4caf50"),
            "MG": ("router",   "Router",          "ff9800"),
            "MV": ("other",    "Other",           "9e9e9e"),
            "MT": ("other",    "Other",           "9e9e9e"),
        }
        slug, name, color = role_map.get(
            family, (self.default_role_slug, "Network", "0080ff")
        )
        if slug not in self._role_cache:
            from dcim.models import DeviceRole
            role, _ = DeviceRole.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "color": color},
            )
            self._role_cache[slug] = role
        return self._role_cache[slug]

    def _get_meraki_tag(self):
        if self._meraki_tag is None and not self.dry_run:
            from extras.models import Tag
            self._meraki_tag, _ = Tag.objects.get_or_create(
                slug=_MERAKI_TAG_SLUG,
                defaults={"name": "meraki", "color": _MERAKI_TAG_COLOR},
            )
        return self._meraki_tag

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iface_type(family: str, speed_mbps: int) -> str:
        """Derive the best NetBox interface type from family and link speed."""
        if speed_mbps and speed_mbps in _SPEED_TO_IFACE_TYPE:
            return _SPEED_TO_IFACE_TYPE[speed_mbps]
        return _FAMILY_IFACE_TYPE.get(family, "other")

    @staticmethod
    def _speed_to_netbox_value(speed_mbps: int) -> int | None:
        """Convert Meraki speed in Mbps to NetBox speed value (in Mbps)."""
        if not speed_mbps or speed_mbps == 0:
            return None
        return speed_mbps

    @staticmethod
    def _vlan_mode_to_netbox(vlan_mode: str, vlan_id: int) -> str:
        """Map Meraki VLAN mode to NetBox interface mode."""
        if vlan_mode == "access":
            return "access"
        elif vlan_mode == "trunk":
            return "tagged"
        return ""

    def _get_vlan_by_id(self, vlan_id: int, site) -> object:
        """Get a VLAN by ID scoped to the given site."""
        if not vlan_id or vlan_id == 0:
            return None
        from ipam.models import VLAN, VLANGroup
        
        vlan_group = None
        if site:
            vlan_group = VLANGroup.objects.filter(
                name__iexact=f"{site.name} VLANs"
            ).first()
        
        lookup = {"vid": vlan_id}
        if vlan_group:
            lookup["group"] = vlan_group
        
        return VLAN.objects.filter(**lookup).first()

    def _parse_allowed_vlans(self, allowed_vlans_str: str, site) -> list:
        """Parse a comma-separated list of VLAN IDs and return VLAN objects."""
        if not allowed_vlans_str:
            return []
        
        vlans = []
        for vlan_str in allowed_vlans_str.split(","):
            vlan_str = vlan_str.strip()
            if vlan_str.isdigit():
                vlan_id = int(vlan_str)
                vlan = self._get_vlan_by_id(vlan_id, site)
                if vlan:
                    vlans.append(vlan)
        return vlans


    @staticmethod
    def _ensure_device_cf(name: str, label: str) -> None:
        """Create a text custom field on dcim.device if it does not exist."""
        from django.apps import apps
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        try:
            Device = apps.get_model("dcim", "Device")
            device_ct = ContentType.objects.get_for_model(Device)
            cf, _ = CustomField.objects.get_or_create(
                name=name,
                defaults={
                    "label": label,
                    "type": "text",
                    "required": False,
                },
            )
            if device_ct not in cf.object_types.all():
                cf.object_types.add(device_ct)
        except Exception as exc:
            log.debug("Syncer: could not ensure custom field %s: %s", name, exc)

    @staticmethod
    def _ensure_interface_poe_cf() -> None:
        """Create PoE custom fields on dcim.interface if they don't exist."""
        from django.apps import apps
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        try:
            Interface = apps.get_model("dcim", "Interface")
            iface_ct = ContentType.objects.get_for_model(Interface)

            # PoE enabled flag
            cf_enabled, _ = CustomField.objects.get_or_create(
                name="meraki_poe_enabled",
                defaults={
                    "label": "Meraki PoE Enabled",
                    "type": "boolean",
                    "required": False,
                },
            )
            if iface_ct not in cf_enabled.object_types.all():
                cf_enabled.object_types.add(iface_ct)

            # PoE power limit in watts
            cf_limit, _ = CustomField.objects.get_or_create(
                name="meraki_poe_limit_w",
                defaults={
                    "label": "Meraki PoE Limit (W)",
                    "type": "integer",
                    "required": False,
                },
            )
            if iface_ct not in cf_limit.object_types.all():
                cf_limit.object_types.add(iface_ct)
        except Exception as exc:
            log.debug("Syncer: could not ensure PoE custom fields: %s", exc)


    @staticmethod
    def _ensure_device_uplink_cf() -> None:
        """Create MX uplink custom field on dcim.device if it doesn't exist."""
        from django.apps import apps
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        try:
            Device = apps.get_model("dcim", "Device")
            device_ct = ContentType.objects.get_for_model(Device)

            cf, _ = CustomField.objects.get_or_create(
                name="meraki_uplink_config",
                defaults={
                    "label": "Meraki Uplink Config",
                    "type": "text",
                    "required": False,
                },
            )
            if device_ct not in cf.object_types.all():
                cf.object_types.add(device_ct)
        except Exception as exc:
            log.debug("Syncer: could not ensure uplink custom field: %s", exc)


# ---------------------------------------------------------------------------
# Site helpers (used by the management command)
# ---------------------------------------------------------------------------

def get_mapped_sites() -> list:
    """
    Return all NetBox Site objects that have meraki_network_id set.
    Each returned object has .custom_field_data["meraki_network_id"] populated.
    """
    from dcim.models import Site

    return [
        site for site in Site.objects.all()
        if site.custom_field_data.get("meraki_network_id")
    ]


def update_site_meraki_name(site, network_name: str) -> None:
    """Back-fill meraki_site_name on the site if it is blank."""
    if not network_name:
        return
    existing = site.custom_field_data.get("meraki_site_name") or ""
    if existing != network_name:
        site.custom_field_data["meraki_site_name"] = network_name
        site.save(update_fields=["custom_field_data"])