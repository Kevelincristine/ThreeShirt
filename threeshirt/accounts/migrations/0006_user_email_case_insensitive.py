from django.conf import settings
from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower


INDEX_NAME = "auth_user_email_ci_uq"


def create_case_insensitive_email_index(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    database = schema_editor.connection.alias
    duplicates = (
        User.objects.using(database)
        .exclude(email="")
        .values(normalized_email=Lower("email"))
        .annotate(total=Count("pk"))
        .filter(total__gt=1)
    )
    if duplicates.exists():
        raise RuntimeError(
            "Case-insensitive duplicate user emails must be reconciled "
            "before applying the ThreeShirt email uniqueness migration."
        )

    if schema_editor.connection.vendor not in {"sqlite", "postgresql"}:
        raise RuntimeError(
            "The case-insensitive email constraint supports SQLite and PostgreSQL."
        )

    quote = schema_editor.quote_name
    table = quote(User._meta.db_table)
    column = quote(User._meta.get_field("email").column)
    schema_editor.execute(
        f"CREATE UNIQUE INDEX {quote(INDEX_NAME)} "
        f"ON {table} (LOWER({column})) WHERE {column} <> ''"
    )


def drop_case_insensitive_email_index(apps, schema_editor):
    schema_editor.execute(
        f"DROP INDEX IF EXISTS {schema_editor.quote_name(INDEX_NAME)}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_accountadminaudit_authratelimitbucket_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(
            create_case_insensitive_email_index,
            drop_case_insensitive_email_index,
        ),
    ]
