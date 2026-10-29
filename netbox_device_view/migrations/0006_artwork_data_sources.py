import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_datasource_type_remove_choices"),
        ("netbox_device_view", "0005_artwork_revisions"),
    ]

    operations = [
        migrations.AddField(
            model_name="visualdefinition",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="core.datasource",
            ),
        ),
        migrations.RenameField(
            model_name="revisionimport",
            old_name="bundle_name",
            new_name="source_name",
        ),
        migrations.AddField(
            model_name="revisionimport",
            name="data_source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="core.datasource",
            ),
        ),
        migrations.AddField(
            model_name="revisionimport",
            name="last_synced",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="SourceSyncRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("started", models.DateTimeField()),
                ("finished", models.DateTimeField(blank=True, null=True)),
                (
                    "source",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="core.datasource",
                    ),
                ),
            ],
        ),
    ]
