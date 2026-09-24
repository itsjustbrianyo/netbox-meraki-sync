from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_meraki_sync", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="synclog",
            name="vlans_synced",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="synclog",
            name="prefixes_synced",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
