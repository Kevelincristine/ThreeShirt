
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db import connection
from django.http import HttpResponseNotAllowed, HttpResponseServerError
from django.template.response import TemplateResponse
from django.urls import path

from .models import AccountAdminAudit, UserProfile

User = get_user_model()


class RestrictedAdminSite(admin.AdminSite):
    site_header = "ThreeShirt — Administração"
    site_title = "ThreeShirt"
    index_title = "Gerenciamento do sistema"

    def get_urls(self):
        custom_urls = [
            path(
                "db-role-check/",
                self.admin_view(self.db_role_check),
                name="db-role-check",
            ),
        ]
        return custom_urls + super().get_urls()

    def db_role_check(self, request):
        if request.method not in {"GET", "POST"}:
            return HttpResponseNotAllowed(["GET", "POST"])

        result = None
        if request.method == "POST":
            if connection.vendor != "postgresql":
                return HttpResponseServerError(
                    "A conexão padrão não é PostgreSQL."
                )

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_user, r.rolsuper, r.rolbypassrls "
                    "FROM pg_roles r "
                    "WHERE r.rolname = current_user;"
                )
                row = cursor.fetchone()

            if row is None:
                return HttpResponseServerError(
                    "A consulta não retornou uma linha."
                )

            result = {
                "current_user": row[0],
                "rolsuper": row[1],
                "rolbypassrls": row[2],
            }

        return TemplateResponse(
            request,
            "admin/db_role_check.html",
            {
                **self.each_context(request),
                "title": "Verificar conexão PostgreSQL",
                "result": result,
            },
        )

    def has_permission(self, request):
        user = request.user

        if not user.is_active or not user.is_staff:
            return False

        authorized_emails = {
            email.strip().lower()
            for email in getattr(settings, "ADMIN_EMAILS", [])
            if email and email.strip()
        }

        user_email = (user.email or "").strip().lower()

        return user_email in authorized_emails


three_shirt_admin = RestrictedAdminSite(name="admin")


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    extra = 0

    def has_change_permission(self, request, obj=None):
        return True

    def get_readonly_fields(self, request, obj=None):
        return ("user", "full_name", "role", "credential_revision")


class CustomUserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)
    list_display = ("username", "email", "is_active", "is_staff")
    search_fields = ("username", "email")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return True


three_shirt_admin.register(User, CustomUserAdmin)
class ReadOnlyModelAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class AuditEventAdmin(ReadOnlyModelAdmin):
    list_display = ("action", "actor_identifier", "target_identifier", "created_at")
    list_filter = ("action",)
    readonly_fields = tuple(field.name for field in AccountAdminAudit._meta.fields)


three_shirt_admin.register(UserProfile, ReadOnlyModelAdmin)
three_shirt_admin.register(AccountAdminAudit, AuditEventAdmin)
