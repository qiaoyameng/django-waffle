from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('waffle', '0004_update_everyone_nullbooleanfield'),
    ]

    operations = [
        migrations.AddField(
            model_name='flag',
            name='prerequisites',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    'Boolean expression of flags that must be active before this '
                    "flag is evaluated. Examples: \"base\", "
                    '{"flag": "base", "active": false}, '
                    '{"all": ["base", {"any": [{"flag": "a", "active": false}, "b"]}]}.'
                ),
                verbose_name='Prerequisites',
            ),
        ),
    ]
