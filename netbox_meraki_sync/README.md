# netbox-meraki-sync

A NetBox 4.x plugin that synchronises Cisco Meraki inventory into NetBox using the official [Meraki Python SDK](https://github.com/meraki/dashboard-api-python). It pulls devices, interfaces, switch stacks, VLANs, subnets, static routes and SSIDs from the Meraki Dashboard and writes them into NetBox DCIM, IPAM and Wireless. A read-only Meraki API key is all that is required.

Sync is one-way: Meraki is the source of truth and nothing is ever written back to Meraki.

---

## What gets synced

### DCIM

| NetBox object | Source / behaviour |
|---|---|
| `dcim.Manufacturer` | "Cisco Meraki", created once and reused |
| `dcim.DeviceType` | Meraki model string (e.g. `MS425-32`), created automatically |
| `dcim.DeviceRole` | Derived from model family (Switch, Firewall, Access Point, …) |
| `dcim.Device` | One per Meraki serial. Matched by serial, then by name within the site. Name updates when renamed in Meraki. |
| `dcim.Interface` | One per switch port, MX WAN/LAN port, AP radio or management port |
| `dcim.VirtualChassis` | One per Meraki switch stack, named `{site} - {stack name}`, with master and members linked |

### Interface details (NetBox built-in fields)

| Meraki data | NetBox field |
|---|---|
| Port name | `description` |
| Port enabled/disabled | `enabled` |
| Link speed | `speed` and interface `type` |
| Duplex | `duplex` |
| Access / trunk mode | `mode` (`access` / `tagged`) |
| Access VLAN | `untagged_vlan` |
| Trunk allowed VLANs | `tagged_vlans` |

VLAN assignments are only made when the matching VLAN already exists in the site's VLAN group, so they populate once IPAM has synced.

### IPAM

| NetBox object | Source / behaviour |
|---|---|
| `ipam.VLANGroup` | One per site, named `{site} VLANs`, scoped to the site |
| `ipam.VLAN` | MX appliance VLANs, plus Layer 3 VLANs found on switches and switch stacks |
| `ipam.Prefix` | Each VLAN subnet, single-LAN subnet and enabled static route, linked to its VLAN where one exists |
| `ipam.IPRange` | Usable host range of each synced subnet (network and broadcast excluded) |
| `ipam.IPAddress` | Device LAN IPs at the subnet's prefix length; MX WAN IPs as `/32` |

Notes:

- Networks without VLANs enabled ("single LAN") get a Prefix and IP Range but no VLAN object.
- Static routes are matched to Layer 3 interfaces on switches and switch stacks so their Prefix is linked to the correct VLAN (e.g. `172.17.205.0/24` → VLAN 205).
- Layer 3 interfaces named `Reserved`, `Reserved1`, `Reserved 2`, etc. don't create VLANs, to avoid name collisions. Their Prefix and IP Range are still created.

### Wireless

| NetBox object | Source / behaviour |
|---|---|
| `wireless.WirelessLANGroup` | One per site, named `{site} WLANs` |
| `wireless.WirelessLAN` | One per enabled, named SSID. Unused "Unconfigured SSID" slots are skipped. |

For each SSID the plugin records:

- **Auth type** mapped to NetBox (`open`, `wpa-personal`, `wpa-enterprise`)
- **VLAN**, for bridged SSIDs with VLAN tagging. NAT-mode SSIDs, or SSIDs that map to different VLANs per AP tag, are left without a VLAN.
- **Description** with the SSID number, plus hidden SSID, band steering and client limit where set, e.g. `Meraki SSID 1 (hidden) band-steering enabled max 25 clients`

Pre-shared keys and other secrets are never collected.

### Inherited from the NetBox Site

Every synced object inherits these from its parent Site:

- **Tenant**
- **Tags**, plus a `meraki` tag applied to every synced object
- **GPS coordinates**: the Site's latitude and longitude are copied onto each Device and kept in sync if the Site changes

### Custom fields

Created automatically on first use.

**`dcim.Site`**

| Field | Description |
|---|---|
| `meraki_network_id` | Meraki network ID. Set this to enable sync for the site. |
| `meraki_site_name` | Meraki network name |

**`dcim.Device`**

| Field | Description |
|---|---|
| `meraki_serial` | Meraki serial number |
| `meraki_firmware` | Installed firmware version |
| `meraki_tags` | Meraki Dashboard tags (comma-separated) |
| `meraki_stack_id` | Meraki switch stack ID |
| `meraki_stack_name` | Meraki switch stack name |
| `meraki_stack_role` | `master` or `member` |
| `meraki_uplink_config` | MX uplinks with role and status, e.g. `WAN 1 (primary, active), WAN 2 (secondary, ready)` |

**`dcim.Interface`**

| Field | Description |
|---|---|
| `meraki_poe_enabled` | PoE enabled on the switch port |
| `meraki_poe_limit_w` | PoE power limit in watts |

### Supported Meraki product families

| Family | Devices | Interfaces |
|---|---|---|
| MS | Switches | One per switch port; type from link speed |
| MX | Security appliances / SD-WAN | WAN 1, WAN 2, LAN |
| MR | Access points | Radio 0 |
| MG | Cellular gateways | Management |
| MV | Smart cameras | Management |
| MT | Sensors | Management |

Catalyst switches onboarded to the Meraki Dashboard for cloud monitoring are also checked for Layer 3 interfaces.

---

## Change logging

Every create and update made by the sync is recorded in the NetBox changelog, with before/after diffs, just like a change made in the UI. Changes are attributed to a service user (`meraki-sync` by default) and each sync run shares one request ID, so everything a run changed can be viewed together under **Operations → Change Log**.

The service user is created automatically as an **inactive** account with no usable password. It cannot log in; it only owns the changes. Use `--user` or the `changelog_username` setting to attribute changes to a different account.

Because the sync goes through NetBox's event system, any webhooks or event rules you have configured will fire for synced changes. Keep this in mind for a large first sync.

---

## Requirements

- NetBox 4.0 or later
- Python 3.10+
- `meraki` Python SDK (installed automatically)
- A Cisco Meraki Dashboard API key with read-only access

---

## Installation

### 1. Install the package

Install into the Python environment that runs NetBox (usually `/opt/netbox/venv`):

```bash
source /opt/netbox/venv/bin/activate
pip install --upgrade --no-deps git+https://github.com/itsjustbrianyo/netbox-meraki-sync
```

The same command upgrades an existing install. `--no-deps` stops pip from touching packages NetBox depends on. On a fresh install, make sure the Meraki SDK is present:

```bash
pip install meraki
```

### 2. Enable the plugin

Edit `configuration.py` (typically `/opt/netbox/netbox/netbox/configuration.py`):

```python
PLUGINS = [
    "netbox_meraki_sync",
]
```

### 3. Configure the plugin

Only `meraki_api_key` is required:

```python
PLUGINS_CONFIG = {
    "netbox_meraki_sync": {
        # Meraki Dashboard API key (read-only is sufficient).
        # Can also be set via the MERAKI_DASHBOARD_API_KEY environment variable.
        "meraki_api_key": "your-api-key-here",

        # HTTP/HTTPS proxy for outbound Meraki API calls (optional).
        "http_proxy": None,

        # Meraki API request timeout in seconds.
        "request_timeout": 30,

        # Device role slug for new devices (created if missing).
        "default_device_role": "network",

        # NetBox user that changelog entries are attributed to.
        "changelog_username": "meraki-sync",
    }
}
```

### 4. Run migrations

```bash
cd /opt/netbox/netbox
python3 manage.py migrate netbox_meraki_sync
```

### 5. Restart NetBox

```bash
sudo systemctl restart netbox netbox-rq
```

The plugin appears under **Plugins → Meraki Sync** in the navigation menu.

---

## Mapping sites to Meraki networks

Each NetBox Site must be linked to a Meraki network before it will sync.

### 1. Find your Meraki network IDs

```bash
python3 manage.py sync_meraki --list-networks
```

### 2. Set the ID on the NetBox Site

In the UI, edit the Site and set **Meraki Network ID** under Custom Fields. Or via the API:

```bash
curl -X PATCH https://netbox.example.com/api/dcim/sites/42/ \
  -H "Authorization: Token YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"custom_fields": {"meraki_network_id": "L_123456789012345678"}}'
```

While you're on the Site, set its **Tenant**, **Tags** and **latitude/longitude**. The sync copies these onto everything it creates for that site.

---

## Running a sync

```bash
cd /opt/netbox/netbox

# Sync all mapped sites
python3 manage.py sync_meraki

# Sync a single network (good for testing)
python3 manage.py sync_meraki --network L_123456789012345678

# Dry run: collect from Meraki but write nothing to NetBox
python3 manage.py sync_meraki --network L_123456789012345678 --dry-run

# Attribute changelog entries to a specific NetBox user
python3 manage.py sync_meraki --user jsmith
```

| Option | Description |
|---|---|
| `--network ID` | Sync only this Meraki network |
| `--dry-run` | Collect data without writing to NetBox |
| `--user NAME` | NetBox username for changelog entries |
| `--list-networks` | List networks visible to the API key and exit |

### Scheduling

```
# /etc/cron.d/netbox-meraki-sync — sync every 4 hours
0 */4 * * * netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py sync_meraki >> /var/log/netbox/meraki_sync.log 2>&1
```

---

## Viewing sync results

**Plugins → Meraki Sync → Sync Logs** shows the history of sync runs with per-network counts of devices, interfaces, IPs, VLANs, prefixes, static routes and SSIDs.

Sync logs are also available through the REST API:

```
GET /api/plugins/meraki/sync-logs/
GET /api/plugins/meraki/sync-logs/<id>/
```

For object-level detail of what changed, use the NetBox changelog (see [Change logging](#change-logging)).

---

## Starting fresh

`clear_meraki_netbox_standalone.py` removes everything the plugin has created so you can re-sync from a clean slate. It runs from any computer with Python and only needs the NetBox URL and an API token:

```bash
# Preview what would be deleted
python3 clear_meraki_netbox_standalone.py --url https://netbox.example.com --token YOUR_TOKEN --dry-run

# Delete
python3 clear_meraki_netbox_standalone.py --url https://netbox.example.com --token YOUR_TOKEN
```

It deletes Meraki-tagged Devices, WirelessLANs, VLANs, Prefixes and IP Ranges, plus Virtual Chassis, `* VLANs` / `* WLANs` groups and sync logs. Back up your database and always run `--dry-run` first. VLAN/WLAN groups are matched by name and Virtual Chassis are not filtered by tag, so review the dry-run output carefully if you have any that weren't created by this plugin.

---

## Meraki API key permissions

The plugin only reads data. A read-only organisation-level API key covers everything it uses:

- Organisation networks and devices
- Device management interfaces
- Switch ports, port statuses, stacks and Layer 3 routing interfaces
- Appliance VLANs, static routes, uplink statuses and uplink selection
- Wireless SSIDs

---

## Notes

- Devices are never deleted from NetBox by the sync. Removing a device from Meraki leaves its NetBox record in place.
- Device matching: Meraki serial first, then device name within the same site.
- Interface types are inferred from model family and link speed and can be edited in NetBox afterwards. Values managed by the sync (description, enabled, speed, duplex, VLAN mode and VLANs) are overwritten on the next run.
- The Meraki SDK automatically retries rate-limited (HTTP 429) requests with back-off.
- Syncs run per network. A failure on one network is logged and the sync continues with the next.

---

## Project structure

```
netbox_meraki_sync/
├── __init__.py              PluginConfig and default settings
├── collector.py             Meraki SDK wrapper (read-only)
├── syncer.py                Writes NetBox objects via the Django ORM
├── change_logging.py        Changelog context for syncs run outside a web request
├── signals.py               Creates Site custom fields on startup
├── choices.py
├── filtersets.py
├── navigation.py
├── urls.py
├── management/commands/
│   └── sync_meraki.py       CLI entry point
├── models/
│   └── sync_log.py          SyncLog (one record per network per run)
├── migrations/
├── api/                     Read-only REST API for sync logs
├── forms/
├── tables/
├── views/
└── templates/netbox_meraki_sync/
```
