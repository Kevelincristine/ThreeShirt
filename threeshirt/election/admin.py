from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.html import format_html
from django.urls import reverse

from accounts.admin import three_shirt_admin

from .concurrency import run_with_sqlite_lock_retry
from .models import ElectionSettings, ShirtProposal


class ElectionSettingsForm(forms.ModelForm):
    class Meta:
        model = ElectionSettings
        fields = (
            "survey_open",
            "voting_open",
            "results_release_authorized",
        )

    def clean(self):
        cleaned_data = super().clean()
        if "voting_open" not in cleaned_data:
            return cleaned_data

        current_settings = (
            ElectionSettings.objects.filter(pk=self.instance.pk).first()
            if self.instance.pk
            else None
        )
        was_open = bool(
            current_settings and current_settings.voting_open
        )
        will_be_open = cleaned_data["voting_open"]

        if will_be_open and not was_open:
            self.instance.voting_started_at = timezone.now()
            self.instance.voting_ended_at = None
        elif was_open and not will_be_open:
            self.instance.voting_ended_at = timezone.now()

        return cleaned_data


class ElectionSettingsAdmin(admin.ModelAdmin):
    form = ElectionSettingsForm
    fields = (
        "survey_open",
        "voting_open",
        "results_release_authorized",
    )
    list_display = (
        "survey_open",
        "voting_open",
        "results_release_authorized",
        "updated_at",
    )

    def has_module_permission(self, request):
        return three_shirt_admin.has_permission(request)

    def has_view_permission(self, request, obj=None):
        return three_shirt_admin.has_permission(request)

    def has_add_permission(self, request):
        return (
            three_shirt_admin.has_permission(request)
            and not ElectionSettings.objects.exists()
        )

    def has_change_permission(self, request, obj=None):
        return (
            three_shirt_admin.has_permission(request)
            and (obj is None or obj.pk == 1)
        )

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not three_shirt_admin.has_permission(request):
            raise PermissionDenied

        def save_settings():
            with transaction.atomic():
                current_settings = (
                    ElectionSettings.objects
                    .select_for_update()
                    .filter(pk=1)
                    .first()
                )
                was_open = bool(
                    current_settings and current_settings.voting_open
                )
                will_be_open = obj.voting_open

                if will_be_open and not was_open:
                    obj.voting_started_at = timezone.now()
                    obj.voting_ended_at = None
                elif was_open and not will_be_open:
                    obj.voting_ended_at = timezone.now()

                obj.full_clean()
                super(ElectionSettingsAdmin, self).save_model(
                    request,
                    obj,
                    form,
                    change,
                )

        run_with_sqlite_lock_retry(save_settings)


three_shirt_admin.register(ElectionSettings, ElectionSettingsAdmin)


class ShirtProposalAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "author",
        "status",
        "created_at",
        "reviewed_at",
    )
    list_filter = ("status",)
    search_fields = ("title", "description", "author__email")
    fields = (
        "author",
        "title",
        "description",
        "image_preview",
        "base_color",
        "related_to_course",
        "animal",
        "additional_colors",
        "has_collar",
        "has_phrase",
        "phrase_text",
        "status",
        "reviewed_at",
        "created_at",
        "updated_at",
    )
    readonly_fields = (
        "author",
        "image_preview",
        "reviewed_at",
        "created_at",
        "updated_at",
    )

    def has_module_permission(self, request):
        return three_shirt_admin.has_permission(request)

    def has_view_permission(self, request, obj=None):
        return three_shirt_admin.has_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return three_shirt_admin.has_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Imagem do design")
    def image_preview(self, obj):
        if obj is None or not obj.pk:
            return "A imagem ficará disponível após salvar a proposta."
        image_url = reverse("proposal-image", args=[obj.pk])
        return format_html(
            '<a href="{}">Abrir imagem protegida</a>',
            image_url,
        )

    def save_model(self, request, obj, form, change):
        if change:
            old_status = (
                ShirtProposal.objects
                .only("status")
                .get(pk=obj.pk)
                .status
            )
            if obj.status != old_status:
                obj.reviewed_at = (
                    timezone.now()
                    if obj.status != ShirtProposal.Status.PENDING
                    else None
                )
        else:
            obj.status = ShirtProposal.Status.PENDING
            obj.reviewed_at = None

        obj.full_clean()
        super().save_model(request, obj, form, change)


three_shirt_admin.register(ShirtProposal, ShirtProposalAdmin)
