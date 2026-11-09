from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_account_created_audit_action"),
    ]

    operations = [
        migrations.AlterField(
            model_name="accountadminaudit",
            name="action",
            field=models.CharField(
                choices=[
                    ("PASSWORD_REQUEST_APPROVED", "Solicitação aprovada"),
                    ("PASSWORD_REQUEST_REJECTED", "Solicitação rejeitada"),
                    ("FIRST_ACCESS_COMPLETED", "Primeiro acesso concluído"),
                    (
                        "FIRST_ACCESS_REJECTED",
                        "Tentativa de primeiro acesso rejeitada",
                    ),
                    ("PASSWORD_CHANGED", "Senha alterada"),
                    ("ACCOUNT_BLOCKED", "Conta bloqueada"),
                    ("ACCOUNT_REACTIVATED", "Conta reativada"),
                    ("ACCOUNT_RESET", "Conta reinicializada"),
                    ("ROLE_CHANGED", "Papel alterado"),
                    ("ACCOUNT_CREATED", "Conta criada"),
                    (
                        "FIRST_ACCESS_ISSUED",
                        "Credencial de primeiro acesso emitida",
                    ),
                ],
                max_length=32,
            ),
        ),
    ]
