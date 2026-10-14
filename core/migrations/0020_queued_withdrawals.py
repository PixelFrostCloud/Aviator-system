from django.db import migrations, models


def create_dispatch_lock(apps, schema_editor):
    PaymentDispatchLock = apps.get_model('core', 'PaymentDispatchLock')
    PaymentDispatchLock.objects.get_or_create(pk=1)


def remove_dispatch_lock(apps, schema_editor):
    PaymentDispatchLock = apps.get_model('core', 'PaymentDispatchLock')
    PaymentDispatchLock.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_gameround_betting_ends_at_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='paymentrequest',
            name='status',
            field=models.CharField(
                choices=[
                    ('queued', 'Queued'),
                    ('pending', 'Pending'),
                    ('processing', 'Processing'),
                    ('completed', 'Completed'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name='paymentrequest',
            name='idempotency_key',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddConstraint(
            model_name='paymentrequest',
            constraint=models.UniqueConstraint(
                condition=~models.Q(('idempotency_key', '')),
                fields=('user', 'idempotency_key'),
                name='unique_user_payment_idempotency_key',
            ),
        ),
        migrations.CreateModel(
            name='PaymentDispatchLock',
            fields=[
                ('id', models.PositiveSmallIntegerField(
                    default=1,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                )),
                ('lock_token', models.CharField(blank=True, max_length=32)),
                ('locked_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.RunPython(create_dispatch_lock, remove_dispatch_lock),
    ]
