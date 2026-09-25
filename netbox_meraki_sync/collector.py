"""
Meraki Dashboard API collector.

Wraps the official `meraki` Python SDK and returns structured dicts ready for
the syncer.  All API calls are read-only — no write operations are performed.

Supported device families
--------------------------
MS  — switches         (ports, CDP/LLDP neighbours, client MAC table)
MX  — security appliances / SD-WAN  (WAN + LAN ports)
MR  — wireless APs     (radio interfaces)
MG  — cellular gateways
MV  — smart cameras
MT  — sensors
Other Meraki models — basic device record only

Rate limiting
-------------
The Meraki SDK handles back-off automatically.  By default it retries on 429
responses with an exponential back-off.  No additional rate-limiting is needed
in this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import meraki
import meraki.exceptions

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured data classes returned by the collector
# ---------------------------------------------------------------------------

@dataclass
class CollectedPort:
    port_id:     str
    name:        str
    description: str = ""
    enabled:     bool = True
    connected:   bool = False
    speed_mbps:  int  = 0          # 0 = unknown
    is_uplink:   bool = False
    poe_enabled: bool = False      # PoE enabled on this port
    poe_limit_w: int  = 0          # PoE power limit in watts (0 = no limit or unknown)
    # Switch port VLAN configuration
    vlan_mode:   str = ""          # "access" or "trunk"
    vlan_id:     int = 0           # Access VLAN ID (for access ports)
    allowed_vlans: str = ""        # Comma-separated list for trunk ports
    duplex:      str = ""          # "auto", "full", "half"


@dataclass
class CollectedUplink:
    """MX appliance uplink configuration."""
    interface:    str              # e.g., "WAN1", "WAN2", "Cellular"
    ip_address:   str = ""         # Public/upstream IP if configured
    gateway_ip:   str = ""         # Upstream gateway
    failover_role: str = ""        # "primary", "secondary", or ""
    mode:         str = ""         # "Auto", "Manual" or other config mode


@dataclass
class CollectedMac:
    mac:     str
    port_id: str
    vlan:    int = 0


@dataclass
class CollectedNeighbour:
    local_port:  str
    remote_id:   str               # remote serial or hostname
    remote_port: str = ""
    protocol:    str = "lldp"      # "cdp" or "lldp"


@dataclass
class CollectedVlan:
    vlan_id:       int             # 0 = "single LAN" (VLANs not enabled on this network)
    name:          str
    subnet:        str = ""        # CIDR, e.g. "192.168.1.0/24"
    appliance_ip:  str = ""        # MX interface IP for this VLAN/LAN
    dns_nameservers: str = ""
    dhcp_handling: str = ""        # Meraki's dhcpHandling: "Run a DHCP server", "Relay DHCP to another server", "Do not respond to DHCP requests"
    reserved_ip_ranges: list = field(default_factory=list)  # [{"start": "...", "end": "...", "comment": "..."}]


@dataclass
class CollectedStaticRoute:
    name:        str
    subnet:      str = ""          # CIDR
    next_hop_ip: str = ""          # gatewayIp
    enabled:     bool = True
    vlan_id:     int = 0            # Layer 3 VLAN ID (from matching MX VLAN or switch L3 interface)
    vlan_name:   str = ""           # Name of that VLAN/interface, if found


@dataclass
class CollectedSsid:
    number:        int
    name:          str
    enabled:       bool = True
    auth_mode:     str = ""          # Meraki authMode: open, psk, 8021x-radius, ...
    vlan_id:       Optional[int] = None
    band_steering: bool = False      # Band steering enabled
    broadcast:     bool = True       # SSID broadcast enabled
    client_limit:  int = 0           # Max clients (0 = unlimited)


@dataclass
class CollectedStackMembership:
    """Meraki switch stack membership for a single device."""
    stack_id:       str    # Meraki stack ID
    stack_name:     str    # Human-readable stack name
    master_serial:  str    # Serial of the stack master device
    member_role:    str    # "master" or "member"


@dataclass
class CollectedNetworkIpam:
    vlans:         list[CollectedVlan]        = field(default_factory=list)
    static_routes: list[CollectedStaticRoute] = field(default_factory=list)


@dataclass
class CollectedDevice:
    serial:      str
    name:        str
    model:       str
    firmware:    str   = ""
    lan_ip:      str   = ""
    wan1_ip:     str   = ""
    wan2_ip:     str   = ""
    tags:        list[str]              = field(default_factory=list)
    ports:       list[CollectedPort]    = field(default_factory=list)
    macs:        list[CollectedMac]     = field(default_factory=list)
    neighbours:  list[CollectedNeighbour] = field(default_factory=list)
    uplinks:     list[CollectedUplink]  = field(default_factory=list)  # MX appliance uplink config
    stack_membership: Optional[CollectedStackMembership] = None  # Set if device is part of a Meraki switch stack

    @property
    def family(self) -> str:
        """Two-letter Meraki model family (MS, MX, MR, …)."""
        return self.model[:2].upper() if self.model else "??"


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class MerakiCollector:
    """
    Thin wrapper around the Meraki Python SDK for device data collection.

    Usage::

        collector = MerakiCollector(api_key="…", timeout=30)
        devices = collector.collect_network("N_xxxxxxxxxxxx")
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: int = 30,
        proxy: str = "",
    ) -> None:
        kwargs: dict = dict(
            api_key=api_key,
            output_log=False,
            print_console=False,
            suppress_logging=True,
            single_request_timeout=timeout,
            # Retry on 429 up to 10 times with back-off
            nginx_429_retry_wait_time=2,
            wait_on_rate_limit=True,
        )
        if proxy:
            kwargs["requests_proxy"] = proxy

        self.dashboard = meraki.DashboardAPI(**kwargs)

    # ------------------------------------------------------------------
    # High-level entry point
    # ------------------------------------------------------------------

    def collect_network(self, network_id: str) -> list[CollectedDevice]:
        """
        Collect all devices in a Meraki network.

        Note: this does NOT collect Meraki's connected-client data (the
        computers/phones/etc. plugged into switch ports or associated to
        APs) — only the network devices themselves (switches, APs,
        appliances) and their own IPs/interfaces.  Client/MAC collection
        via getNetworkClients was removed on request: it was slow, prone to
        503 timeouts on networks with a large historical client count, and
        the resulting MAC data was for connected end-user devices, not the
        Meraki equipment this plugin is meant to inventory.
        """
        log.info("Meraki: collecting network %s", network_id)

        raw_devices = self._get_network_devices(network_id)
        if not raw_devices:
            log.warning("Meraki: no devices returned for network %s", network_id)
            return []

        # Build an index of stack memberships: serial → CollectedStackMembership
        stack_memberships = self._index_stack_memberships(network_id)

        # Topology neighbours (CDP/LLDP) — unrelated to client data, still
        # used to populate port-to-port neighbour links.
        topology_nbrs = self._build_topology_index(network_id)

        results: list[CollectedDevice] = []
        for raw in raw_devices:
            try:
                serial = raw.get("serial", "")
                dev = self._collect_device(raw, topology_nbrs)
                if serial in stack_memberships:
                    dev.stack_membership = stack_memberships[serial]
                results.append(dev)
            except Exception as exc:
                serial = raw.get("serial", "?")
                log.warning("Meraki: failed to collect device %s: %s", serial, exc)

        log.info(
            "Meraki: collected %d device(s) from network %s",
            len(results), network_id,
        )
        return results

    # ------------------------------------------------------------------
    # Per-device collection
    # ------------------------------------------------------------------

    def _collect_device(
        self,
        raw: dict,
        topology_nbrs: dict[tuple[str, str], list[dict]],
    ) -> CollectedDevice:
        serial  = raw.get("serial", "")
        model   = raw.get("model", "")
        name    = raw.get("name") or raw.get("serial", "")
        lan_ip  = raw.get("lanIp") or ""
        wan1_ip = raw.get("wan1Ip") or ""
        wan2_ip = raw.get("wan2Ip") or ""
        fw      = raw.get("firmware") or ""
        tags    = raw.get("tags") or []

        dev = CollectedDevice(
            serial=serial,
            name=name,
            model=model,
            firmware=fw,
            lan_ip=lan_ip,
            wan1_ip=wan1_ip,
            wan2_ip=wan2_ip,
            tags=tags,
        )

        family = dev.family

        # getNetworkDevices doesn't return lanIp for most MV/MG/MT models —
        # fall back to the per-device management interface endpoint so
        # cameras, cellular gateways, and sensors still get an IP synced.
        if not dev.lan_ip and family in ("MV", "MG", "MT"):
            mgmt = self._get_device_management_interface(serial)
            dev.lan_ip = self._extract_management_ip(mgmt)

        if family == "MS":
            self._collect_switch(dev, topology_nbrs)
        elif family == "MX":
            self._collect_appliance(dev)
        elif family == "MR":
            self._collect_ap(dev, raw)
        else:
            # MG, MV, MT, or unknown — create a single management port
            dev.ports.append(CollectedPort(
                port_id="mgmt", name="Management", enabled=True
            ))

        return dev

    def _collect_switch(
        self,
        dev: CollectedDevice,
        topology_nbrs: dict[tuple[str, str], list[dict]],
    ) -> None:
        ports    = self._get_switch_ports(dev.serial)
        statuses = {
            str(s["portId"]): s
            for s in self._get_switch_port_statuses(dev.serial)
        }

        for raw_port in ports:
            port_id   = str(raw_port.get("portId", ""))
            status    = statuses.get(port_id, {})
            enabled   = raw_port.get("enabled", True)
            connected = status.get("status", "") == "Connected"
            poe_enabled = bool(raw_port.get("poeEnabled", False))
            # Meraki reports power limit in watts; 0 or missing = no limit/not applicable
            poe_limit_w = int(raw_port.get("powerLimit") or 0)
            
            # VLAN configuration
            vlan_mode = ""
            vlan_id = 0
            allowed_vlans = ""
            if raw_port.get("type") == "access":
                vlan_mode = "access"
                vlan_id = int(raw_port.get("vlan") or 0)
            elif raw_port.get("type") == "trunk":
                vlan_mode = "trunk"
                # allowedVlans is a list of VLAN IDs or ranges like [1, "2-10"]
                vlans_list = raw_port.get("allowedVlans", [])
                allowed_vlans = ",".join(str(v) for v in vlans_list) if vlans_list else ""
            
            # Duplex from port status
            duplex = status.get("duplex", "").lower() or ""  # "full", "half", "auto"

            cp = CollectedPort(
                port_id        = port_id,
                name           = port_id,
                description    = raw_port.get("name") or "",
                enabled        = enabled,
                connected      = connected,
                speed_mbps     = _parse_speed_mbps(status.get("speed", "")),
                poe_enabled    = poe_enabled,
                poe_limit_w    = poe_limit_w,
                vlan_mode      = vlan_mode,
                vlan_id        = vlan_id,
                allowed_vlans  = allowed_vlans,
                duplex         = duplex,
            )
            dev.ports.append(cp)

            # CDP / LLDP neighbours from port status
            for proto, key in (("cdp", "cdpInfo"), ("lldp", "lldpInfo")):
                nbr_data = status.get(key)
                if nbr_data:
                    remote_id = (
                        nbr_data.get("systemName")
                        or nbr_data.get("sourcePort")
                        or ""
                    )
                    remote_port = (
                        nbr_data.get("portId")
                        or nbr_data.get("sourcePort")
                        or ""
                    )
                    if remote_id:
                        dev.neighbours.append(CollectedNeighbour(
                            local_port  = port_id,
                            remote_id   = remote_id,
                            remote_port = remote_port,
                            protocol    = proto,
                        ))

            # (MAC table building via connected-client data was removed —
            # see collect_network docstring)

        # Topology-based neighbours (fill gaps from port-status CDP/LLDP)
        existing_nbr_keys = {
            (n.local_port, n.remote_id) for n in dev.neighbours
        }
        for (serial, port_id), nbrs in topology_nbrs.items():
            if serial != dev.serial:
                continue
            for nbr in nbrs:
                key = (port_id, nbr["remote_id"])
                if key not in existing_nbr_keys:
                    dev.neighbours.append(CollectedNeighbour(
                        local_port  = port_id,
                        remote_id   = nbr["remote_id"],
                        remote_port = nbr["remote_port"],
                        protocol    = "lldp",
                    ))

    def _collect_appliance(self, dev: CollectedDevice) -> None:
        """MX appliance — WAN + LAN ports and uplink configuration."""
        if dev.wan1_ip:
            dev.ports.append(CollectedPort(
                port_id="wan1", name="WAN 1",
                description="WAN 1 uplink",
                enabled=True, connected=True, is_uplink=True,
            ))
        if dev.wan2_ip:
            dev.ports.append(CollectedPort(
                port_id="wan2", name="WAN 2",
                description="WAN 2 uplink",
                enabled=True, connected=True, is_uplink=True,
            ))
        if dev.lan_ip:
            dev.ports.append(CollectedPort(
                port_id="lan", name="LAN",
                description="LAN interface",
                enabled=True, connected=True,
            ))
        if not dev.ports:
            dev.ports.append(CollectedPort(
                port_id="mgmt", name="Management", enabled=True,
            ))
        
        # Collect MX uplink configuration (failover settings, etc.)
        self._collect_uplink_config(dev)

    def _collect_uplink_config(self, dev: CollectedDevice) -> None:
        """Collect MX uplink failover/load-balancing configuration."""
        raw_uplinks = self._get_network_appliance_uplink_statuses(dev.serial)
        for raw_uplink in raw_uplinks:
            interface = raw_uplink.get("interface", "")
            if not interface:
                continue
            
            uplink = CollectedUplink(
                interface     = interface,
                ip_address    = raw_uplink.get("ip") or "",
                gateway_ip    = raw_uplink.get("gateway") or "",
                failover_role = raw_uplink.get("role") or "",
                mode          = raw_uplink.get("mode") or "",
            )
            dev.uplinks.append(uplink)

    def _collect_ap(self, dev: CollectedDevice, raw: dict) -> None:
        """MR access point — radio interfaces with wireless config."""
        # Collect radio status/config for each radio on this AP
        radio_status = self._get_device_wireless_status(dev.serial)
        
        # APs typically have radio0 and sometimes radio1 (dual-band)
        # If we have radio_status, use it; otherwise create a default radio0
        if radio_status:
            for radio_data in radio_status.get("radios", []):
                radio_num = radio_data.get("index", 0)
                port = CollectedPort(
                    port_id      = f"radio{radio_num}",
                    name         = f"Radio {radio_num}",
                    description  = raw.get("model", ""),
                    enabled      = True,
                    connected    = True,
                    radio_band   = radio_data.get("band", ""),      # "2.4", "5", "6"
                    radio_channel = str(radio_data.get("channel", "")),  # e.g., "1", "36"
                    radio_power  = int(radio_data.get("txPower") or 0),  # dBm
                )
                dev.ports.append(port)
        else:
            # Fallback if wireless status unavailable
            dev.ports.append(CollectedPort(
                port_id="radio0",
                name="Radio 0",
                description=raw.get("model", ""),
                enabled=True,
                connected=True,
            ))

    # ------------------------------------------------------------------
    # IPAM collection (network-level, not per-device)
    # ------------------------------------------------------------------

    def collect_network_ipam(
        self, network_id: str, switch_serials: Optional[list[str]] = None,
    ) -> CollectedNetworkIpam:
        """
        Collect network-level IPAM information for a Meraki network: VLANs
        (or the single-LAN subnet) from the MX appliance, plus static
        routes.

        If the network has VLANs enabled (getNetworkApplianceVlansSettings),
        every VLAN from getNetworkApplianceVlans is returned.  Otherwise the
        network is on a "single LAN" and the one subnet from
        getNetworkApplianceSingleLan is returned as a CollectedVlan with
        vlan_id=0.

        Static routes (getNetworkApplianceStaticRoutes) point at subnets
        reached via a next-hop gateway rather than a local MX interface —
        typically Layer 3 VLANs configured on an upstream switch (or switch
        stack), not on the MX itself.  To recover the VLAN ID and name for
        these, pass every device serial in this network via switch_serials
        (not just ones with an "MS" model — some Layer 3 switches, such as
        Catalyst switches onboarded for Meraki cloud monitoring, report
        other model strings, so callers should not pre-filter by model):
        each serial's Layer 3 routing interfaces
        (getDeviceSwitchRoutingInterfaces) are fetched and matched to each
        static route by subnet, and the call safely returns nothing for
        devices that aren't Layer 3 switches.  Switch stacks are handled
        separately — stacked switches configure Layer 3 routing on the
        stack itself, not on individual member switches, so per-device
        lookups return nothing for them.  Every switch stack in the network
        is discovered via getNetworkSwitchStacks and its routing interfaces
        are fetched via getNetworkSwitchStackRoutingInterfaces,
        automatically, with no input needed from the caller.  vlan_id/name
        on the CollectedStaticRoute is populated when a match is found from
        either source.

        Networks with no MX appliance (e.g. switch- or AP-only networks)
        simply return empty lists — the underlying API calls 404 and are
        swallowed by the SDK wrappers below.
        """
        log.info("Meraki: collecting IPAM for network %s", network_id)

        vlans_enabled = self._get_vlans_enabled(network_id)

        if vlans_enabled:
            raw_vlans = self._get_network_appliance_vlans(network_id)
            vlans = [self._parse_vlan(v) for v in raw_vlans]
        else:
            raw_lan = self._get_network_appliance_single_lan(network_id)
            vlans = [self._parse_single_lan(raw_lan)] if raw_lan else []

        # Collect Layer 3 routing interfaces from every device serial passed
        # in (not just recognized MS switches — see docstring above), so
        # static routes to switch-side VLANs can be matched to their real
        # VLAN ID/name.  Devices that aren't Layer 3 switches simply return
        # an empty list here.
        switch_l3_interfaces = []
        for serial in (switch_serials or []):
            switch_l3_interfaces.extend(
                self._get_device_switch_routing_interfaces(serial)
            )

        # Also collect Layer 3 routing interfaces from every switch stack in
        # this network — stacked switches configure L3 on the stack itself,
        # so this is the only way to see those interfaces.
        for stack in self._get_network_switch_stacks(network_id):
            stack_id = stack.get("id")
            if not stack_id:
                continue
            switch_l3_interfaces.extend(
                self._get_network_switch_stack_routing_interfaces(
                    network_id, stack_id,
                )
            )

        raw_routes = self._get_network_appliance_static_routes(network_id)
        static_routes = [
            self._parse_static_route(r, vlans, switch_l3_interfaces)
            for r in raw_routes
        ]

        log.info(
            "Meraki: collected %d VLAN/subnet record(s) and %d static "
            "route(s) for network %s",
            len(vlans), len(static_routes), network_id,
        )
        return CollectedNetworkIpam(vlans=vlans, static_routes=static_routes)

    @staticmethod
    def _parse_vlan(raw: dict) -> CollectedVlan:
        nameservers = raw.get("dnsNameservers") or ""
        if isinstance(nameservers, list):
            nameservers = ", ".join(nameservers)
        return CollectedVlan(
            vlan_id             = int(raw.get("id") or 0),
            name                = raw.get("name") or f"VLAN {raw.get('id', '')}",
            subnet              = raw.get("subnet") or "",
            appliance_ip        = raw.get("applianceIp") or "",
            dns_nameservers     = nameservers,
            dhcp_handling       = raw.get("dhcpHandling") or "",
            reserved_ip_ranges  = raw.get("reservedIpRanges") or [],
        )

    @staticmethod
    def _parse_single_lan(raw: dict) -> CollectedVlan:
        nameservers = raw.get("dnsNameservers") or ""
        if isinstance(nameservers, list):
            nameservers = ", ".join(nameservers)
        return CollectedVlan(
            vlan_id             = 0,
            name                = "LAN",
            subnet              = raw.get("subnet") or "",
            appliance_ip        = raw.get("applianceIp") or "",
            dns_nameservers     = nameservers,
            dhcp_handling       = raw.get("dhcpHandling") or "",
            reserved_ip_ranges  = raw.get("reservedIpRanges") or [],
        )

    def _parse_static_route(
        self, raw: dict, vlans: list, switch_l3_interfaces: list,
    ) -> CollectedStaticRoute:
        """
        Parse a static route and match its subnet to a Layer 3 VLAN to
        recover the VLAN ID and name.  Checked in order:
          1. MX appliance VLANs (rare — usually the MX has no VLAN on a
             switch-side subnet)
          2. Switch Layer 3 routing interfaces (the common case — the
             route's subnet lives on a VLAN interface on an upstream switch)
        If no match is found, vlan_id/vlan_name stay unset.
        """
        subnet = raw.get("subnet") or ""
        vlan_id = 0
        vlan_name = ""

        for vlan in vlans:
            if vlan.subnet == subnet:
                vlan_id = vlan.vlan_id
                vlan_name = vlan.name
                break

        if not vlan_id:
            for iface in switch_l3_interfaces:
                if iface.get("subnet") == subnet:
                    vlan_id = int(iface.get("vlanId") or 0)
                    vlan_name = iface.get("name") or ""
                    break

        return CollectedStaticRoute(
            name        = raw.get("name") or subnet or "",
            subnet      = subnet,
            next_hop_ip = raw.get("gatewayIp") or "",
            enabled     = bool(raw.get("enabled", True)),
            vlan_id     = vlan_id,
            vlan_name   = vlan_name,
        )

    # ------------------------------------------------------------------
    # SDK wrappers
    # ------------------------------------------------------------------

    def _get_vlans_enabled(self, network_id: str) -> bool:
        try:
            settings = self.dashboard.appliance.getNetworkApplianceVlansSettings(
                networkId=network_id
            )
            return bool(settings.get("vlansEnabled"))
        except meraki.exceptions.APIError as exc:
            # 404 here typically means the network has no MX appliance
            log.debug(
                "Meraki: getNetworkApplianceVlansSettings failed for %s: %s",
                network_id, exc,
            )
            return False

    def _get_network_appliance_vlans(self, network_id: str) -> list[dict]:
        try:
            return self.dashboard.appliance.getNetworkApplianceVlans(
                networkId=network_id
            )
        except meraki.exceptions.APIError as exc:
            log.warning(
                "Meraki: getNetworkApplianceVlans failed for %s: %s",
                network_id, exc,
            )
            return []

    def _get_network_appliance_single_lan(self, network_id: str) -> Optional[dict]:
        try:
            return self.dashboard.appliance.getNetworkApplianceSingleLan(
                networkId=network_id
            )
        except meraki.exceptions.APIError as exc:
            log.debug(
                "Meraki: getNetworkApplianceSingleLan failed for %s: %s",
                network_id, exc,
            )
            return None

    def _get_network_appliance_static_routes(self, network_id: str) -> list[dict]:
        try:
            return self.dashboard.appliance.getNetworkApplianceStaticRoutes(
                networkId=network_id
            )
        except meraki.exceptions.APIError as exc:
            log.debug(
                "Meraki: getNetworkApplianceStaticRoutes failed for %s: %s",
                network_id, exc,
            )
            return []

    def _get_network_appliance_uplink_statuses(self, network_id: str) -> list[dict]:
        """
        Get uplink status/config for an MX appliance — includes failover role,
        IP configuration, uplink mode.  Returns empty list on error or for
        non-MX devices or if the method is not available in this SDK version.
        """
        try:
            return self.dashboard.appliance.getNetworkApplianceUplinkStatuses(
                networkId=network_id
            )
        except (meraki.exceptions.APIError, AttributeError) as exc:
            log.debug(
                "Meraki: getNetworkApplianceUplinkStatuses failed for %s: %s",
                network_id, exc,
            )
            return []

    def _get_device_wireless_status(self, serial: str) -> Optional[dict]:
        """
        Get wireless radio status for an AP — includes band, channel, TX power,
        and other radio configuration. Returns None on error or for non-AP
        devices or if the method is not available in this SDK version.
        """
        try:
            return self.dashboard.wireless.getDeviceWirelessStatus(serial=serial)
        except (meraki.exceptions.APIError, AttributeError) as exc:
            log.debug(
                "Meraki: getDeviceWirelessStatus failed for %s: %s",
                serial, exc,
            )
            return None

    def _get_device_switch_routing_interfaces(self, serial: str) -> list[dict]:
        """
        Get a standalone switch's Layer 3 routing interfaces (VLAN
        interfaces with an IP address configured), used to recover VLAN
        ID/name for static routes that point at switch-side subnets.
        Devices that aren't Layer 3 switches, have no routing interfaces
        configured, or are members of a switch stack (whose L3 interfaces
        live on the stack, not the member switch — see
        _get_network_switch_stack_routing_interfaces), simply return an
        empty list (the API 404s and is swallowed here).
        """
        try:
            return self.dashboard.switch.getDeviceSwitchRoutingInterfaces(
                serial=serial
            )
        except meraki.exceptions.APIError as exc:
            log.debug(
                "Meraki: getDeviceSwitchRoutingInterfaces failed for %s: %s",
                serial, exc,
            )
            return []

    def _get_network_switch_stacks(self, network_id: str) -> list[dict]:
        """
        List every switch stack in a network, so its Layer 3 routing
        interfaces can be fetched separately from standalone switches.
        Networks with no switch stacks simply return an empty list (the
        API 404s and is swallowed here).
        """
        try:
            return self.dashboard.switch.getNetworkSwitchStacks(
                networkId=network_id
            )
        except meraki.exceptions.APIError as exc:
            log.debug(
                "Meraki: getNetworkSwitchStacks failed for %s: %s",
                network_id, exc,
            )
            return []

    def _get_network_switch_stack_routing_interfaces(
        self, network_id: str, switch_stack_id: str,
    ) -> list[dict]:
        """
        Get a switch stack's Layer 3 routing interfaces.  Stacked switches
        configure routing on the stack itself rather than on individual
        member switches, so this is the only way to see those interfaces —
        getDeviceSwitchRoutingInterfaces on a stack member returns nothing.
        Stacks with no L3 configured simply return an empty list (the API
        404s and is swallowed here).
        """
        try:
            return self.dashboard.switch.getNetworkSwitchStackRoutingInterfaces(
                networkId=network_id, switchStackId=switch_stack_id,
            )
        except meraki.exceptions.APIError as exc:
            log.debug(
                "Meraki: getNetworkSwitchStackRoutingInterfaces failed for "
                "%s/%s: %s",
                network_id, switch_stack_id, exc,
            )
            return []

    # ------------------------------------------------------------------
    # Wireless collection (network-level, not per-device)
    # ------------------------------------------------------------------

    def collect_network_wireless(self, network_id: str) -> list[CollectedSsid]:
        """
        Collect enabled, named SSIDs configured on a Meraki network's MR
        access points via getNetworkWirelessSsids.  Meraki always returns
        15 SSID slots per network whether or not they're used, so disabled
        slots and ones still at their default "Unconfigured SSID N" name
        are filtered out here — only SSIDs someone has actually turned on
        and named are returned.  Networks with no MR APs simply return an
        empty list (the API call 404s and is swallowed below).

        VLAN association is only meaningful for SSIDs in "Bridge mode" (or
        "Layer 3 roaming") with `useVlanTagging` enabled — clients get an
        IP from that VLAN's own DHCP server rather than Meraki's built-in
        NAT-mode DHCP, so there's a real VLAN to record.  Two sources are
        checked, in order:
          1. `defaultVlanId` — the network-wide VLAN, used for APs with no
             tag-specific override
          2. `apTagsAndVlanIds` — per-AP-tag VLAN overrides; if every
             override maps to the same VLAN ID, that's used, otherwise the
             SSID spans multiple VLANs and none is recorded (ambiguous)
        SSIDs left in Meraki's default NAT mode (`ipAssignmentMode ==
        "NAT mode"`) or without VLAN tagging enabled get vlan_id=None,
        which the syncer leaves unset on the WirelessLAN in NetBox.

        Auth mode is recorded but PSKs are not collected — store secrets
        separately outside of NetBox.
        """
        raw_ssids = self._get_network_wireless_ssids(network_id)
        results = []
        for raw in raw_ssids:
            name = (raw.get("name") or "").strip()
            if not raw.get("enabled") or not name or name.startswith("Unconfigured SSID"):
                continue

            results.append(CollectedSsid(
                number         = int(raw.get("number", 0)),
                name           = name,
                enabled        = True,
                auth_mode      = raw.get("authMode") or "",
                vlan_id        = self._extract_ssid_vlan_id(raw),
                band_steering  = raw.get("bandSteeringEnabled", False),
                broadcast      = raw.get("ssidAdminAccessible", True),  # true = SSID broadcast enabled
                client_limit   = raw.get("clientLimitPerAccessPoint", 0),  # 0 = no limit
            ))

        log.info(
            "Meraki: collected %d enabled SSID(s) for network %s",
            len(results), network_id,
        )
        return results

    @staticmethod
    def _extract_ssid_vlan_id(raw: dict) -> Optional[int]:
        """
        Work out the single VLAN ID (if any) a bridged SSID's clients land
        on.  Returns None for NAT-mode SSIDs (Meraki's own DHCP — no VLAN
        to record), SSIDs without VLAN tagging enabled, or SSIDs whose
        per-AP-tag overrides disagree (genuinely multiple VLANs, so no
        single ID is correct).
        """
        if raw.get("ipAssignmentMode") == "NAT mode":
            return None
        if not raw.get("useVlanTagging"):
            return None

        tag_overrides = raw.get("apTagsAndVlanIds") or []
        tag_vlan_ids = {
            int(t["vlanId"]) for t in tag_overrides
            if t.get("vlanId") is not None
        }

        default_vlan = raw.get("defaultVlanId")
        if default_vlan is not None:
            tag_vlan_ids.add(int(default_vlan))

        if len(tag_vlan_ids) == 1:
            return next(iter(tag_vlan_ids))

        # 0 means nothing configured; 2+ means it spans multiple VLANs —
        # either way there's no single correct VLAN ID to record.
        return None

    def _get_network_wireless_ssids(self, network_id: str) -> list[dict]:
        try:
            return self.dashboard.wireless.getNetworkWirelessSsids(
                networkId=network_id
            )
        except meraki.exceptions.APIError as exc:
            log.debug(
                "Meraki: getNetworkWirelessSsids failed for %s: %s",
                network_id, exc,
            )
            return []

    def _get_device_management_interface(self, serial: str) -> dict:
        try:
            return self.dashboard.devices.getDeviceManagementInterface(
                serial=serial
            )
        except meraki.exceptions.APIError as exc:
            log.debug(
                "Meraki: getDeviceManagementInterface failed for %s: %s",
                serial, exc,
            )
            return {}

    @staticmethod
    def _extract_management_ip(mgmt: dict) -> str:
        """
        Pull the first usable IPv4 out of a getDeviceManagementInterface
        response.  Its shape varies by device family — a flat dict for most
        types, or wan1/wan2 sub-dicts for devices with dual uplinks — so
        check the top level and every nested dict for a static or DHCP IP.
        """
        if not mgmt:
            return ""
        candidates = [mgmt] + [v for v in mgmt.values() if isinstance(v, dict)]
        for c in candidates:
            for key in ("staticIp", "ip", "dhcpIp"):
                val = c.get(key)
                if val:
                    return val
        return ""

    def _get_network_devices(self, network_id: str) -> list[dict]:
        try:
            return self.dashboard.networks.getNetworkDevices(networkId=network_id)
        except meraki.exceptions.APIError as exc:
            log.error("Meraki: getNetworkDevices failed for %s: %s", network_id, exc)
            return []

    def _get_switch_ports(self, serial: str) -> list[dict]:
        try:
            return self.dashboard.switch.getDeviceSwitchPorts(serial=serial)
        except meraki.exceptions.APIError:
            return []

    def _get_switch_port_statuses(self, serial: str) -> list[dict]:
        try:
            return self.dashboard.switch.getDeviceSwitchPortsStatuses(serial=serial)
        except meraki.exceptions.APIError:
            return []

    def _get_network_topology(self, network_id: str) -> dict:
        try:
            return self.dashboard.networks.getNetworkTopologyLinkLayer(
                networkId=network_id
            )
        except meraki.exceptions.APIError:
            return {}

    def _index_stack_memberships(
        self, network_id: str
    ) -> dict[str, CollectedStackMembership]:
        """
        Return a dict mapping device serial → CollectedStackMembership for
        all switches that are part of a Meraki switch stack in this network.
        Devices not in a stack are not present in the returned dict.
        """
        memberships: dict[str, CollectedStackMembership] = {}
        for stack in self._get_network_switch_stacks(network_id):
            stack_id = stack.get("id")
            stack_name = stack.get("name", f"Stack {stack_id}")
            master_serial = stack.get("serials", [])[0] if stack.get("serials") else ""

            # In Meraki's API, the first serial in the list is the master,
            # the rest are members. serials_by_tag provides role info.
            serials_by_tag = stack.get("serials_by_tag", {})
            for i, serial in enumerate(stack.get("serials") or []):
                role = "master" if i == 0 else "member"
                memberships[serial] = CollectedStackMembership(
                    stack_id=stack_id,
                    stack_name=stack_name,
                    master_serial=master_serial,
                    member_role=role,
                )
        return memberships

    def _build_topology_index(
        self, network_id: str
    ) -> dict[tuple[str, str], list[dict]]:
        """
        Parse the topology/linkLayer response into:
        (local_serial, local_port) → [{remote_id, remote_port}, …]
        """
        topology = self._get_network_topology(network_id)
        index: dict[tuple[str, str], list[dict]] = {}

        for link in (topology.get("links") or []):
            ends = link.get("ends") or []
            if len(ends) != 2:
                continue
            a, b = ends
            for local, remote in ((a, b), (b, a)):
                serial = (local.get("device") or {}).get("serial", "")
                port   = str(
                    (local.get("discovered") or {}).get("portId")
                    or (local.get("connected") or {}).get("portId")
                    or ""
                )
                r_serial = (remote.get("device") or {}).get("serial", "")
                r_port   = str(
                    (remote.get("discovered") or {}).get("portId")
                    or (remote.get("connected") or {}).get("portId")
                    or ""
                )
                if serial and port and r_serial:
                    index.setdefault((serial, port), []).append({
                        "remote_id":   r_serial,
                        "remote_port": r_port,
                    })

        return index

    # ------------------------------------------------------------------
    # Organisation-level helpers (used by management command)
    # ------------------------------------------------------------------

    def get_organizations(self) -> list[dict]:
        """Return all organisations accessible to the API key."""
        try:
            return self.dashboard.organizations.getOrganizations()
        except meraki.exceptions.APIError as exc:
            log.error("Meraki: getOrganizations failed: %s", exc)
            return []

    def get_organization_networks(self, org_id: str) -> list[dict]:
        """Return all networks in an organisation."""
        try:
            return self.dashboard.organizations.getOrganizationNetworks(
                organizationId=org_id,
                total_pages="all",
            )
        except meraki.exceptions.APIError as exc:
            log.error("Meraki: getOrganizationNetworks(%s) failed: %s", org_id, exc)
            return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_speed_mbps(speed_str: str) -> int:
    """Convert a Meraki speed string ('1 Gbps', '100 Mbps', …) to Mbps."""
    if not speed_str:
        return 0
    s = speed_str.strip().lower()
    try:
        if "gbps" in s:
            return int(float(s.replace("gbps", "").strip()) * 1000)
        if "mbps" in s:
            return int(float(s.replace("mbps", "").strip()))
    except ValueError:
        pass
    return 0


def _normalise_mac(mac: str) -> str:
    """Return a colon-separated lowercase MAC or '' if invalid."""
    digits = mac.lower().replace(":", "").replace("-", "").replace(".", "")
    if len(digits) != 12:
        return ""
    return ":".join(digits[i: i + 2] for i in range(0, 12, 2))
