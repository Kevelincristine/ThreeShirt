
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import UserProfile, EmailLoginCode

User = get_user_model()


class RestrictedAdminSite(admin.AdminSite):
    site_header = "ThreeShirt — Administração"
    site_title = "ThreeShirt"
    index_title = "Gerenciamento do sistema"

    def has_permission(self, request):
        user = request.user
        allowed_emails = {
            email.strip().lower()
            for email in settings.ADMIN_EMAILS
        }

        return (
            user.is_active
            and user.is_staff
            and user.email.strip().lower() in allowed_emails
        )


three_shirt_admin = RestrictedAdminSite(name="admin")


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    extra = 0
def has_change_permission(self, request, obj=None):
    return True

def get_readonly_fields(self, request, obj=None):
    return ("user", "full_name", "role")

class CustomUserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)
    list_display = ("username", "email", "is_active", "is_staff")
    search_fields = ("username", "email")


three_shirt_admin.register(User, CustomUserAdmin)
three_shirt_admin.register(UserProfile)


class EmailLoginCodeAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at", "expires_at", "used_at", "attempts")
    search_fields = ("email",)
    readonly_fields = (
        "email",
        "code_hash",
        "created_at",
        "expires_at",
        "used_at",
        "attempts",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


three_shirt_admin.register(EmailLoginCode, EmailLoginCodeAdmin)
