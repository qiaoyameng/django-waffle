from django.db import migrations, models

import waffle.models


class Migration(migrations.Migration):

    dependencies = [
        ('test_app', '0002_auto_20220717_1934'),
    ]

    operations = [
        migrations.AddField(
            model_name='companyawareflag',
            name='salt',
            field=models.CharField(
                default=waffle.models._generate_salt,
                editable=False,
                help_text=(
                    'Per-flag salt used by stable percentage rollout. Only '
                    'changes when the salt is rotated explicitly, which '
                    'reassigns all identities to new buckets.'
                ),
                max_length=64,
                verbose_name='Salt',
            ),
        ),
    ]
