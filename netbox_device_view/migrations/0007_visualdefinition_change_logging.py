from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_device_view", "0006_artwork_data_sources"),
    ]

    operations = [
        migrations.AddField(
            model_name="visualdefinition",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
        migrations.AddField(
            model_name="visualdefinition",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True),
        ),
    ]
