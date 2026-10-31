
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_login_request_rate_bucket"),
    ]

    operations = [
        migrations.AlterField(
            model_name="userprofile",
            name="role",
            field=models.CharField(
                choices=[
                    ("STUDENT", "Estudante"),
                    ("STUDENT_ADMIN", "Estudante-administrador"),
                    ("ADMIN", "Administrador"),
                ],
                default="STUDENT",
                max_length=13,
            ),
        ),
    ]
