from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('test_app', '0002_auto_20220717_1934'),
    ]

    operations = [
        migrations.AddField(
            model_name='companyawareflag',
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
