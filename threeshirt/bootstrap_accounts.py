
import hashlib
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q

from accounts.access import normalize_authorized_email
from accounts.models import UserProfile, AccountAdminAudit


STUDENT_EMAILS = [
    "ana.azevedo.godoy@escola.pr.gov.br",
    "andro.eustache@escola.pr.gov.br",
    "arthur.mendes.cordeiro@escola.pr.gov.br",
    "brayan.bianconi.silva@escola.pr.gov.br",
    "brenda.ferraz.silva@escola.pr.gov.br",
    "daniel.batista.moura@escola.pr.gov.br",
    "d.aleixo@escola.pr.gov.br",
    "david.william.borges@escola.pr.gov.br",
    "davi.sola@escola.pr.gov.br",
    "edilson.iglesias.junior@escola.pr.gov.br",
    "eliel.mineguetti@escola.pr.gov.br",
    "gomes.souza.eric@escola.pr.gov.br",
    "erik.francisco.souza@escola.pr.gov.br",
    "giovanna.goncalves08@escola.pr.gov.br",
    "rocha.freitas.gustavo@escola.pr.gov.br",
    "henrique.lopes.miranda@escola.pr.gov.br",
    "ian.oura@escola.pr.gov.br",
    "joao.costa.souza16@escola.pr.gov.br",
    "julya.pinheiro.silva@escola.pr.gov.br",
    "kamily.barros.santos@escola.pr.gov.br",
    "martini.karina@escola.pr.gov.br",
    "kawan.martins.silva@escola.pr.gov.br",
    "kevelin.cristine.santos@escola.pr.gov.br",
    "leandro.freitas.barros@escola.pr.gov.br",
    "letycia.dorea@escola.pr.gov.br",
    "luiz.campitelli@escola.pr.gov.br",
    "miqueias.araujo.souza@escola.pr.gov.br",
    "paulo.mello.ribeiro@escola.pr.gov.br",
    "menezes.pietro@escola.pr.gov.br",
    "samuel.ferreira.mendonca@escola.pr.gov.br",
    "suellen.gelinski@escola.pr.gov.br",
    "vitor.fernandes.silva18@escola.pr.gov.br",
    "souza.silva.wendell@escola.pr.gov.br",
    "ygor.bueno@escola.pr.gov.br",
    "kaue.oliveira09@escola.pr.gov.br",
]

ADMIN_EMAIL = "leandro.valk@escola.pr.gov.br"
PROTECTED_ADMIN_EMAIL = "kevelincristine82@gmail.com"
EXCLUDED_EMAIL = "santoskevelin0@gmail.com"

EXPECTED_STUDENTS = 35
EXPECTED_TOTAL = 36


# Segurança: por padrão, somente simula.
DRY_RUN = os.environ.get("THREESHIRT_BOOTSTRAP_DRY_RUN", "1") != "0"
def normalize_list(*values):
    result = set()

    for value in values:
        if value is None:
            continue

        # Se receber uma lista, conjunto ou tupla, percorre os itens.
        if isinstance(value, (list, tuple, set)):
            items = value
        # Se receber texto separado por vírgulas, divide.
        elif isinstance(value, str):
            items = value.split(",")
        else:
            items = [value]

        for item in items:
            normalized = normalize_authorized_email(item)

            if not normalized:
                raise ValueError(
                    f"E-mail inválido na configuração: {item!r}"
                )

            result.add(normalized)

    return result

def generated_username(email):
    digest = hashlib.sha256(
        email.encode("utf-8")
    ).hexdigest()

    return "ts-" + digest[:32]


def model_snapshot(instance):
    """Captura os campos concretos para verificar se houve alteração."""
    return {
        field.attname: getattr(instance, field.attname)
        for field in instance._meta.concrete_fields
    }


students = [
    normalize_authorized_email(email)
    for email in STUDENT_EMAILS
]

if any(email is None for email in students):
    raise ValueError(
        "Existe e-mail inválido na lista de estudantes."
    )

if len(students) != EXPECTED_STUDENTS:
    raise ValueError(
        f"Esperados {EXPECTED_STUDENTS} estudantes; "
        f"encontrados {len(students)}."
    )

if len(set(students)) != len(students):
    raise ValueError(
        "A lista contém e-mails duplicados."
    )


admin_email = normalize_authorized_email(ADMIN_EMAIL)
protected_email = normalize_authorized_email(
    PROTECTED_ADMIN_EMAIL
)
excluded_email = normalize_authorized_email(EXCLUDED_EMAIL)


if excluded_email in students or excluded_email == admin_email:
    raise ValueError(
        "O e-mail excluído apareceu entre os cadastros."
    )

if protected_email in students or protected_email == admin_email:
    raise ValueError(
        "A conta administradora protegida apareceu como nova conta."
    )


targets = [
    (email, UserProfile.Role.STUDENT, False)
    for email in students
]

targets.append(
    (admin_email, UserProfile.Role.ADMIN, True)
)


if (
    len(targets) != EXPECTED_TOTAL
    or len({item[0] for item in targets}) != EXPECTED_TOTAL
):
    raise ValueError(
        "A lista final não contém exatamente 36 e-mails únicos."
    )


allowed_emails = normalize_list(
    settings.AUTHORIZED_LOGIN_EMAILS
)

admin_emails = normalize_list(
    settings.ADMIN_EMAILS
)


missing_login = [
    email
    for email, _, _ in targets
    if email not in allowed_emails
]

if missing_login:
    raise ValueError(
        "E-mails ausentes em AUTHORIZED_LOGIN_EMAILS: "
        + ", ".join(missing_login)
    )


if admin_email not in admin_emails:
    raise ValueError(
        "Leandro não está autorizado em ADMIN_EMAILS."
    )


if excluded_email in allowed_emails:
    raise ValueError(
        "O e-mail excluído ainda consta em AUTHORIZED_LOGIN_EMAILS."
    )


User = get_user_model()


# Verifica a conta administradora que deve ser preservada.
protected_users = list(
    User.objects.filter(email__iexact=protected_email)
)

if len(protected_users) != 1:
    raise ValueError(
        "A conta administradora protegida não foi encontrada "
        "exatamente uma vez."
    )


protected_user = protected_users[0]

protected_profiles = list(
    UserProfile.objects.filter(user=protected_user)
)

if (
    not protected_user.is_active
    or not protected_user.is_staff
    or not protected_user.is_superuser
    or len(protected_profiles) != 1
    or protected_profiles[0].role != UserProfile.Role.ADMIN
):
    raise ValueError(
        "A conta administradora protegida não está "
        "no estado esperado."
    )


# Guarda o estado inicial da conta protegida.
protected_profile = protected_profiles[0]

protected_user_snapshot = model_snapshot(protected_user)
protected_profile_snapshot = model_snapshot(protected_profile)


def check_targets():
    errors = []

    for email, role, is_admin in targets:
        existing = User.objects.filter(
            email__iexact=email
        ).exists()

        if existing:
            errors.append(
                f"Conta já existente: {email}"
            )

        username = generated_username(email)

        if User.objects.filter(username=username).exists():
            errors.append(
                f"Username já utilizado para: {email}"
            )

    if errors:
        raise ValueError("\n".join(errors))


check_targets()


print("\n=== SIMULAÇÃO DO CADASTRO THREESHIRT ===")
print(
    "Modo:",
    "DRY RUN (SEM GRAVAÇÃO)" if DRY_RUN else "CADASTRO REAL"
)
print("Estudantes:", len(students))
print("Administradores novos:", 1)
print("Total de contas novas:", len(targets))
print("Conta administradora existente: preservada")
print("E-mail excluído: não incluído")
print("\nContas que seriam criadas:")


for email, role, is_admin in targets:
    print(
        f"- {email} | perfil={role} | "
        f"is_staff={is_admin} | is_superuser=False"
    )


if DRY_RUN:
    print(
        "\nSIMULAÇÃO CONCLUÍDA. Nenhuma conta foi criada."
    )

else:
    with transaction.atomic():

        # Reconfere dentro da transação antes de gravar.
        check_targets()

        created_users = []

        # Criação das contas, perfis e registros de auditoria.
        for email, role, is_admin in targets:

            user = User.objects.create_user(
                username=generated_username(email),
                email=email,
                password=None,
                is_active=True,
                is_staff=is_admin,
                is_superuser=False,
            )

            created_users.append(user)

            UserProfile.objects.create(
                user=user,
                role=role,
            )

            AccountAdminAudit.objects.create(
                actor=None,
                actor_identifier=0,
                target=user,
                target_identifier=user.pk,
                action=AccountAdminAudit.Action.ACCOUNT_CREATED,
                reason=(
                    "Cadastro inicial em lote de contas autorizadas."
                ),
                details={
                    "operation": "bootstrap",
                    "email": email,
                    "role": role,
                },
            )

        # ==========================================
        # PÓS-VALIDAÇÃO: AINDA DENTRO DA TRANSAÇÃO
        # ==========================================

        post_errors = []

        expected_by_email = {
            email: (role, is_admin)
            for email, role, is_admin in targets
        }

        expected_student_emails = {
            email
            for email, role, _ in targets
            if role == UserProfile.Role.STUDENT
        }

        created_user_ids = [
            user.pk for user in created_users
        ]

        if len(expected_student_emails) != EXPECTED_STUDENTS:
            post_errors.append(
                f"Esperados {EXPECTED_STUDENTS} estudantes; "
                f"encontrados {len(expected_student_emails)}."
            )

        if len(created_users) != EXPECTED_TOTAL:
            post_errors.append(
                f"Esperadas {EXPECTED_TOTAL} contas criadas; "
                f"encontradas {len(created_users)}."
            )

        if len(set(created_user_ids)) != EXPECTED_TOTAL:
            post_errors.append(
                "Os IDs das contas criadas não são únicos."
            )

        verified_users = list(
            User.objects.filter(pk__in=created_user_ids)
        )

        if len(verified_users) != EXPECTED_TOTAL:
            post_errors.append(
                f"Esperadas {EXPECTED_TOTAL} contas persistidas; "
                f"encontradas {len(verified_users)}."
            )

        users_by_email = {}

        for account in verified_users:
            normalized_email = normalize_authorized_email(
                account.email
            )

            if normalized_email is None:
                post_errors.append(
                    f"Conta pk={account.pk}: "
                    "e-mail inválido após a criação."
                )

            elif normalized_email in users_by_email:
                post_errors.append(
                    f"{normalized_email}: "
                    "mais de uma conta criada no lote."
                )

            else:
                users_by_email[normalized_email] = account

        for email in expected_by_email:
            if email not in users_by_email:
                post_errors.append(
                    f"{email}: conta esperada não encontrada "
                    "entre as criadas."
                )

        for email in users_by_email:
            if email not in expected_by_email:
                post_errors.append(
                    f"{email}: conta criada não pertence "
                    "à lista do lote."
                )

        # Verifica os perfis das contas criadas.
        verified_profiles = list(
            UserProfile.objects.filter(
                user_id__in=created_user_ids
            )
        )

        if len(verified_profiles) != EXPECTED_TOTAL:
            post_errors.append(
                f"Esperados {EXPECTED_TOTAL} perfis; "
                f"encontrados {len(verified_profiles)}."
            )

        profiles_by_user_id = {
            profile.user_id: profile
            for profile in verified_profiles
        }

        # Verifica os dados de cada conta.
        for email, (expected_role, is_admin) in expected_by_email.items():

            account = users_by_email.get(email)

            if account is None:
                continue

            if not account.is_active:
                post_errors.append(
                    f"{email}: conta não está ativa."
                )

            if account.has_usable_password():
                post_errors.append(
                    f"{email}: conta iniciou com senha utilizável."
                )

            if account.is_staff != is_admin:
                post_errors.append(
                    f"{email}: is_staff deveria ser {is_admin}."
                )

            if account.is_superuser:
                post_errors.append(
                    f"{email}: is_superuser deveria ser False."
                )

            profile = profiles_by_user_id.get(account.pk)

            if profile is None:
                post_errors.append(
                    f"{email}: perfil não foi criado."
                )

            elif profile.role != expected_role:
                post_errors.append(
                    f"{email}: perfil esperado {expected_role}; "
                    f"encontrado {profile.role}."
                )

        # Confirma que o e-mail excluído não entrou no lote.
        if (
            excluded_email in expected_by_email
            or any(
                normalize_authorized_email(account.email)
                == excluded_email
                for account in verified_users
            )
        ):
            post_errors.append(
                f"{excluded_email}: "
                "conta excluída foi incluída no lote."
            )

        # Confirma que a conta administradora protegida
        # continua exatamente como estava.
        protected_after_users = list(
            User.objects.filter(
                email__iexact=protected_email
            )
        )

        if len(protected_after_users) != 1:
            post_errors.append(
                f"{protected_email}: a conta protegida não existe "
                "exatamente uma vez após o cadastro."
            )

        else:
            protected_after_user = protected_after_users[0]

            if (
                protected_after_user.pk != protected_user.pk
                or model_snapshot(protected_after_user)
                != protected_user_snapshot
            ):
                post_errors.append(
                    f"{protected_email}: "
                    "os dados da conta foram alterados."
                )

            protected_after_profiles = list(
                UserProfile.objects.filter(
                    user=protected_after_user
                )
            )

            if len(protected_after_profiles) != 1:
                post_errors.append(
                    f"{protected_email}: o perfil protegido "
                    "não existe exatamente uma vez."
                )

            elif (
                model_snapshot(protected_after_profiles[0])
                != protected_profile_snapshot
            ):
                post_errors.append(
                    f"{protected_email}: "
                    "os dados do perfil protegido foram alterados."
                )

        # Verifica as auditorias, conferindo tanto o ID do alvo
        # quanto o identificador registrado.
        audit_rows = list(
            AccountAdminAudit.objects.filter(
                action=AccountAdminAudit.Action.ACCOUNT_CREATED
            ).filter(
                Q(target_identifier__in=created_user_ids)
                | Q(target_id__in=created_user_ids)
            ).values_list(
                "target_identifier",
                "target_id",
            )
        )

        if len(audit_rows) != EXPECTED_TOTAL:
            post_errors.append(
                f"Esperados {EXPECTED_TOTAL} registros "
                f"ACCOUNT_CREATED do lote; "
                f"encontrados {len(audit_rows)}."
            )

        for account in verified_users:

            matching_audits = [
                (target_identifier, target_id)
                for target_identifier, target_id in audit_rows
                if (
                    target_identifier == account.pk
                    or target_id == account.pk
                )
            ]

            if len(matching_audits) != 1:
                post_errors.append(
                    f"{account.email}: esperada 1 auditoria "
                    f"ACCOUNT_CREATED; encontradas "
                    f"{len(matching_audits)}."
                )

            elif matching_audits[0] != (
                account.pk,
                account.pk,
            ):
                post_errors.append(
                    f"{account.email}: a auditoria não referencia "
                    "corretamente a conta criada."
                )

        # Qualquer erro interrompe a transação e provoca rollback.
        if post_errors:
            raise RuntimeError(
                "Pós-validação do lote falhou; "
                "a transação será revertida:\n- "
                + "\n- ".join(post_errors)
            )

    # Só chega aqui se a transação foi confirmada.
    print(
        "\nCADASTRO CONCLUÍDO. Total criado:",
        len(targets),
    )
    print("Pós-validação concluída com sucesso.")
