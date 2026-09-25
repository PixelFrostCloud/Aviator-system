from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_alter_paymentrequest_provider'),
    ]

    operations = [
        migrations.AlterField(
            model_name='paymentrequest',
            name='provider',
            field=models.CharField(
                choices=[
                    ('mtn_momo', 'MTN Mobile Money'),
                    ('airtel_money', 'Airtel Money'),
                ],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name='paymentrequest',
            constraint=models.CheckConstraint(
                check=models.Q(provider__in=['mtn_momo', 'airtel_money']),
                name='paymentrequest_mobile_money_only',
            ),
        ),
    ]
