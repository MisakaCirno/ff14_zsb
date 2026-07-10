from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shares', '0019_report_resolution_reason_share_review_feedback_and_more'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AlterField(
                    model_name='announcement',
                    name='content',
                    field=models.TextField(verbose_name='内容'),
                ),
            ],
        ),
    ]
