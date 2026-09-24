from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_meraki_sync", "0003_synclog_static_routes"),
    ]

    operations = [
        migrations.AddField(
            model_name="synclog",
            name="wireless_lans_synced",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
