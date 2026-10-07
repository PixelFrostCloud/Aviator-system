import uuid
from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def populate_referral_codes(apps, schema_editor):
    Profile = apps.get_model('core', 'Profile')
    database = schema_editor.connection.alias
    for profile in Profile.objects.using(database).filter(referral_code__isnull=True).iterator():
        Profile.objects.using(database).filter(pk=profile.pk).update(referral_code=uuid.uuid4())


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0012_remove_kyc_and_set_payment_limits'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='referral_code',
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(populate_referral_codes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='profile',
            name='referral_code',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.CreateModel(
            name='Referral',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('reward_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('rewarded_at', models.DateTimeField(blank=True, null=True)),
                ('first_deposit', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='referral_rewards',
                    to='core.paymentrequest',
                )),
                ('invitee', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='referral_attribution',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('inviter', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='referrals_sent',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('reward_transaction', models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='referral_reward',
                    to='core.transaction',
                )),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['inviter', '-created_at'], name='core_referr_inviter_b626b8_idx'),
                ],
                'constraints': [
                    models.CheckConstraint(
                        check=~models.Q(inviter=models.F('invitee')),
                        name='referral_inviter_cannot_be_invitee',
                    ),
                ],
            },
        ),
    ]
