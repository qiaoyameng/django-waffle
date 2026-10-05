from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('waffle', '0004_update_everyone_nullbooleanfield'),
    ]

    operations = [
        migrations.AddField(
            model_name='flag',
            name='stable_rollout',
            field=models.BooleanField(
                default=False,
                help_text='Use stable, identity-based bucketing for the percentage instead of per-request random sampling? Bucketing is deterministic across requests and clients.',
                verbose_name='Stable rollout',
            ),
        ),
        migrations.AddField(
            model_name='flag',
            name='salt',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Salt used for stable rollout bucketing. It is set automatically when stable rollout is enabled. Rotate it explicitly to reassign identities to buckets.',
                max_length=64,
                verbose_name='Salt',
            ),
        ),
    ]
