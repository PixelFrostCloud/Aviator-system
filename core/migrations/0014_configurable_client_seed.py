import core.models
import uuid
from django.db import migrations, models


def populate_client_seeds(apps, schema_editor):
    Profile = apps.get_model('core', 'Profile')
    database = schema_editor.connection.alias
    for profile in Profile.objects.using(database).filter(client_seed__isnull=True).iterator():
        Profile.objects.using(database).filter(pk=profile.pk).update(
            client_seed=uuid.uuid4().hex,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_referral_program'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='client_seed',
            field=models.CharField(max_length=128, null=True),
        ),
        migrations.RunPython(populate_client_seeds, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='profile',
            name='client_seed',
            field=models.CharField(default=core.models.generate_client_seed, max_length=128),
        ),
    ]
