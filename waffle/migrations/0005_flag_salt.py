from django.db import migrations, models

import waffle.models


class Migration(migrations.Migration):

    dependencies = [
        ('waffle', '0004_update_everyone_nullbooleanfield'),
    ]

    operations = [
        migrations.AddField(
            model_name='flag',
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
