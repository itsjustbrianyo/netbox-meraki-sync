from netbox.plugins import PluginConfig


class MerakiSyncConfig(PluginConfig):
    name = "netbox_meraki_sync"
    verbose_name = "Meraki Sync"
    description = "Synchronise Cisco Meraki network devices into NetBox"
    version = "1.3.0"
    author = "NetBox Meraki Sync"
    base_url = "meraki"
    min_version = "4.0.0"

    default_settings = {
        # Meraki Dashboard API key (read-only scope is sufficient).
        # Can also be set via the MERAKI_DASHBOARD_API_KEY environment variable.
        "meraki_api_key": "",

        # HTTP/HTTPS proxy for outbound Meraki API calls.
        # "http_proxy": "http://proxy.example.com:3128",
        "http_proxy": None,

        # Meraki API request timeout in seconds.
        "request_timeout": 30,

        # Device role slug used when creating new devices from Meraki data.
        # The role is created automatically if it does not exist.
        "default_device_role": "network",

        # NetBox username that changelog entries are attributed to.
        # Created automatically as an inactive account if it doesn't exist.
        "changelog_username": "meraki-sync",
    }

    def ready(self):
        from . import signals  # noqa: F401 — registers the ready() hook


config = MerakiSyncConfig
