from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.html import format_html
from django.urls import reverse

from accounts.admin import three_shirt_admin

from .concurrency import run_with_sqlite_lock_retry
from .models import ElectionSettings, ShirtProposal, Vote


class ElectionSettingsForm(forms.ModelForm):
    class Meta:
        model = ElectionSettings
        fields = (
            "survey_open",
            "proposal_submission_open",
            "voting_open",
            "results_release_authorized",
        )

    def clean(self):
        cleaned_data = super().clean()
        if "voting_open" not in cleaned_data:
            return cleaned_data

        self._timestamps_before_validation = (
            self.instance.voting_started_at,
            self.instance.voting_ended_at,
        )
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
            if Vote.objects.exists():
                self.add_error(
                    "voting_open",
                    "A votação não pode ser reaberta após o registro de votos.",
                )
            else:
                self.instance.voting_started_at = timezone.now()
                self.instance.voting_ended_at = None

        return cleaned_data

    def _post_clean(self):
        try:
            super()._post_clean()
        finally:
            timestamps = getattr(
                self,
                "_timestamps_before_validation",
                None,
            )
            if timestamps is not None:
                (
                    self.instance.voting_started_at,
                    self.instance.voting_ended_at,
                ) = timestamps


class ElectionSettingsAdmin(admin.ModelAdmin):
    form = ElectionSettingsForm
    fields = (
        "survey_open",
        "proposal_submission_open",
        "voting_open",
        "results_release_authorized",
    )
    list_display = (
        "survey_open",
        "proposal_submission_open",
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
                    if Vote.objects.exists():
                        raise ValidationError({
                            "voting_open": (
                                "A votação não pode ser reaberta após "
                                "o registro de votos."
                            )
                        })
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


class ShirtProposalForm(forms.ModelForm):
    def clean(self):
        cleaned_data = super().clean()
        if not self.instance.pk:
            return cleaned_data

        settings = ElectionSettings.objects.filter(pk=1).first()
        if settings and settings.voting_open:
            raise ValidationError(
                "As propostas não podem ser alteradas durante a votação."
            )
        if Vote.objects.filter(proposal=self.instance).exists():
            raise ValidationError(
                "Uma proposta com votos não pode ser alterada."
            )
        return cleaned_data


class ShirtProposalAdmin(admin.ModelAdmin):
    form = ShirtProposalForm
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
        if not three_shirt_admin.has_permission(request):
            return False

        settings = ElectionSettings.objects.filter(pk=1).first()
        if settings and settings.voting_open:
            return False

        return obj is None or not Vote.objects.filter(
            proposal=obj
        ).exists()

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
            if not three_shirt_admin.has_permission(request):
                raise PermissionDenied

            def save_proposal():
                with transaction.atomic():
                    settings = (
                        ElectionSettings.objects
                        .select_for_update()
                        .filter(pk=1)
                        .first()
                    )
                    if settings and settings.voting_open:
                        raise ValidationError(
                            "As propostas não podem ser alteradas "
                            "durante a votação."
                        )

                    current_proposal = (
                        ShirtProposal.objects
                        .select_for_update()
                        .get(pk=obj.pk)
                    )
                    if Vote.objects.filter(
                        proposal=current_proposal
                    ).exists():
                        raise ValidationError(
                            "Uma proposta com votos não pode ser alterada."
                        )

                    if obj.status != current_proposal.status:
                        obj.reviewed_at = (
                            timezone.now()
                            if obj.status != ShirtProposal.Status.PENDING
                            else None
                        )

                    obj.full_clean()
                    super(
                        ShirtProposalAdmin,
                        self,
                    ).save_model(request, obj, form, change)

            run_with_sqlite_lock_retry(save_proposal)
            return
        else:
            obj.status = ShirtProposal.Status.PENDING
            obj.reviewed_at = None

        obj.full_clean()
        super().save_model(request, obj, form, change)


three_shirt_admin.register(ShirtProposal, ShirtProposalAdmin)
