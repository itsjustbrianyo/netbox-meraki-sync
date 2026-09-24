from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_meraki_sync", "0002_synclog_ipam_counters"),
    ]

    operations = [
        migrations.AddField(
            model_name="synclog",
            name="static_routes_synced",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
