
from datetime import timedelta
from io import BytesIO
from importlib import import_module
import json
from pathlib import Path
from decimal import Decimal
import threading
import tempfile
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import (
    DatabaseError,
    IntegrityError,
    OperationalError,
    close_old_connections,
    connections,
    transaction,
)
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import FileSystemStorage
from django.http import FileResponse
from django.test import (
    Client,
    RequestFactory,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone
from django.urls import reverse
from PIL import Image

from accounts.admin import three_shirt_admin
from accounts.models import UserProfile

from .admin import ElectionSettingsAdmin, ShirtProposalAdmin
from .models import ElectionSettings, PreferenceResponse, ShirtProposal, Vote
from .services import (
    calculate_compatibility,
    get_approved_proposal_compatibilities,
    get_approved_proposals,
    get_compatibility_tally,
    get_pending_proposal_descriptions,
    get_preference_response,
    get_vote_tally,
    is_survey_open,
    is_results_release_authorized,
    cast_vote,
    save_preference_response,
    submit_shirt_proposal,
)
from .views import proposal_image

User = get_user_model()


def make_test_image(image_format="PNG", name=None, contents=None):
    if contents is None:
        output = BytesIO()
        Image.new("RGB", (2, 2), color=(20, 100, 180)).save(
            output,
            format=image_format,
        )
        contents = output.getvalue()

    extension = {
        "JPEG": "jpg",
        "PNG": "png",
        "WEBP": "webp",
        "GIF": "gif",
    }[image_format]
    filename = name or f"design.{extension}"
    return SimpleUploadedFile(
        filename,
        contents,
        content_type=f"image/{'jpeg' if image_format == 'JPEG' else image_format.lower()}",
    )


def make_proposal_data(image=None, **overrides):
    data = {
        "title": "Proposta de teste",
        "description": "Descrição detalhada da proposta de camiseta.",
        "image": image or make_test_image(),
        "base_color": "BLACK",
        "related_to_course": True,
        "animal": "",
        "additional_colors": ["BLUE"],
        "has_collar": True,
        "has_phrase": False,
        "phrase_text": "",
    }
    data.update(overrides)
    return data


@override_settings(ADMIN_EMAILS=["dashboard_admin@escola.com"])
class StudentDashboardStatusApiTests(TestCase):
    dashboard_url = "/api/election/student/dashboard/"

    def setUp(self):
        self.student = User.objects.create_user(
            username="dashboard_student",
            email="dashboard_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.client = Client()

    def get_dashboard(self):
        return self.client.get(self.dashboard_url)

    def test_anonymous_request_is_rejected_with_json_401(self):
        response = self.get_dashboard()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"error": "Autenticação necessária."},
        )

    def test_student_without_preferences_or_vote_receives_default_status(self):
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            response.json(),
            {
                "survey_open": False,
                "voting_open": False,
                "results_release_authorized": False,
                "has_preferences": False,
                "has_voted": False,
            },
        )

    def test_student_with_own_preferences_is_reported(self):
        PreferenceResponse.objects.create(
            student=self.student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_preferences"])
        self.assertFalse(response.json()["has_voted"])

    def test_student_who_voted_is_reported_without_vote_details(self):
        author = User.objects.create_user(username="dashboard_author")
        proposal = ShirtProposal.objects.create(
            author=author,
            title="Proposta aprovada",
            description="Descrição da proposta",
            image="shirt_proposals/dashboard.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        Vote.objects.create(student=self.student, proposal=proposal)
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_voted"])
        self.assertNotIn("proposal", response.json())
        self.assertNotIn("vote", response.json())

    def test_active_admin_receives_general_status_without_personal_data(self):
        admin = User.objects.create_user(
            username="dashboard_admin",
            email="dashboard_admin@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        PreferenceResponse.objects.create(
            student=admin,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        self.client.force_login(admin)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "survey_open": False,
                "voting_open": False,
                "results_release_authorized": False,
                "has_preferences": False,
                "has_voted": False,
            },
        )
        admin.refresh_from_db()
        self.assertTrue(admin.is_staff)
        self.assertFalse(admin.is_superuser)

    def test_inactive_student_session_is_rejected_as_anonymous(self):
        self.student.is_active = False
        self.student.save(update_fields=["is_active"])
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"error": "Autenticação necessária."},
        )

    def test_dashboard_does_not_expose_other_students_data(self):
        other_student = User.objects.create_user(
            username="dashboard_other",
            email="other-private@escola.com",
        )
        UserProfile.objects.create(
            user=other_student,
            role=UserProfile.Role.STUDENT,
        )
        PreferenceResponse.objects.create(
            student=other_student,
            base_color="WHITE",
            related_to_course=False,
            wants_animal=True,
            preferred_animal="Animal privado",
            preferred_colors=["RED"],
            wants_collar=False,
            wants_phrase=True,
            intends_to_buy=False,
        )
        self.client.force_login(self.student)

        response = self.get_dashboard()
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(data),
            {
                "survey_open",
                "voting_open",
                "results_release_authorized",
                "has_preferences",
                "has_voted",
            },
        )
        self.assertNotIn("other-private@escola.com", response.content.decode())
        self.assertNotIn("Animal privado", response.content.decode())

    def test_survey_open_and_voting_closed_status(self):
        ElectionSettings.objects.create(survey_open=True)
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["survey_open"])
        self.assertFalse(response.json()["voting_open"])

    def test_voting_open_and_survey_closed_status(self):
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["survey_open"])
        self.assertTrue(response.json()["voting_open"])

    def test_results_release_flag_follows_domain_service(self):
        ElectionSettings.objects.create(results_release_authorized=True)
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["results_release_authorized"])
        self.assertNotIn("results", response.json())
        self.assertNotIn("winners", response.json())

    def test_database_failure_returns_generic_json_500(self):
        self.client.force_login(self.student)

        with patch(
            "election.views.get_preference_response",
            side_effect=DatabaseError("database details"),
        ), self.assertLogs("election.views", level="ERROR"):
            response = self.get_dashboard()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {"error": "Não foi possível consultar o painel."},
        )
        self.assertNotIn("database details", response.content.decode())


class StudentPreferencesApiTests(TestCase):
    preferences_url = "/api/election/student/preferences/"

    def setUp(self):
        self.student = User.objects.create_user(
            username="preferences_api_student",
            email="preferences_api_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.client = Client()

    def create_preferences(self, student, **overrides):
        data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE", "RED"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }
        data.update(overrides)
        return PreferenceResponse.objects.create(student=student, **data)

    def test_anonymous_visitor_receives_json_401(self):
        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"error": "Autenticação necessária."},
        )

    def test_student_without_saved_preferences_receives_empty_json_state(self):
        self.client.force_login(self.student)

        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            response.json(),
            {
                "has_preferences": False,
                "preferences": None,
            },
        )

    def test_student_receives_own_preference_fields_and_json_types(self):
        self.create_preferences(self.student)
        self.client.force_login(self.student)

        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "has_preferences": True,
                "preferences": {
                    "base_color": "BLACK",
                    "related_to_course": True,
                    "wants_animal": False,
                    "preferred_animal": "",
                    "preferred_colors": ["BLUE", "RED"],
                    "wants_collar": True,
                    "wants_phrase": False,
                    "intends_to_buy": True,
                },
            },
        )
        preferences = response.json()["preferences"]
        self.assertIsInstance(preferences["base_color"], str)
        self.assertIsInstance(preferences["related_to_course"], bool)
        self.assertIsInstance(preferences["preferred_colors"], list)
        self.assertIsInstance(preferences["intends_to_buy"], bool)

    def test_admin_profile_is_rejected_with_json_403(self):
        admin = User.objects.create_user(username="preferences_api_admin")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        self.client.force_login(admin)

        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"error": "Acesso permitido apenas a estudantes ativos."},
        )

    def test_query_parameters_cannot_select_another_students_preferences(self):
        other_student = User.objects.create_user(
            username="other_preferences_student",
            email="private-preferences@escola.com",
        )
        UserProfile.objects.create(
            user=other_student,
            role=UserProfile.Role.STUDENT,
        )
        self.create_preferences(
            self.student,
            preferred_animal="Animal próprio",
        )
        self.create_preferences(
            other_student,
            wants_animal=True,
            preferred_animal="Animal privado",
        )
        self.client.force_login(self.student)

        response = self.client.get(
            f"{self.preferences_url}?user_id={other_student.pk}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["preferences"]["preferred_animal"],
            "Animal próprio",
        )
        self.assertNotIn("private-preferences@escola.com", response.content.decode())
        self.assertNotIn("Animal privado", response.content.decode())

    def test_response_contains_only_authorized_preference_fields(self):
        self.create_preferences(self.student)
        self.client.force_login(self.student)

        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()),
            {"has_preferences", "preferences"},
        )
        self.assertEqual(
            set(response.json()["preferences"]),
            {
                "base_color",
                "related_to_course",
                "wants_animal",
                "preferred_animal",
                "preferred_colors",
                "wants_collar",
                "wants_phrase",
                "intends_to_buy",
            },
        )
        body = response.content.decode()
        self.assertNotIn("student", body)
        self.assertNotIn("created_at", body)
        self.assertNotIn("updated_at", body)

    def test_database_failure_returns_generic_json_500(self):
        self.client.force_login(self.student)

        with patch(
            "election.views.get_preference_response",
            side_effect=DatabaseError("database details"),
        ), self.assertLogs("election.views", level="ERROR"):
            response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {"error": "Não foi possível consultar suas preferências."},
        )
        self.assertNotIn("database details", response.content.decode())


@override_settings(ADMIN_EMAILS=["approved_admin@escola.com"])
class ApprovedStudentProposalsApiTests(TestCase):
    proposals_url = "/api/election/student/proposals/"

    def setUp(self):
        self.student = User.objects.create_user(
            username="approved_proposals_student",
            email="approved_proposals_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.client = Client()

    def create_proposal(self, title, status, author=None):
        author = author or User.objects.create_user(
            username=f"author_{title.lower().replace(' ', '_')}",
            email="proposal-author-private@escola.com",
        )
        return ShirtProposal.objects.create(
            author=author,
            title=title,
            description=f"Descrição visual de {title}.",
            image=f"shirt_proposals/{title.lower().replace(' ', '-')}.png",
            base_color=ShirtProposal.BaseColor.BLACK,
            related_to_course=True,
            animal="Raposa",
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=True,
            phrase_text="Nossa turma",
            status=status,
        )

    def test_anonymous_visitor_is_rejected_with_json_401(self):
        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"error": "Autenticação necessária."},
        )

    def test_active_student_receives_approved_proposals(self):
        proposal = self.create_proposal(
            "Proposta aprovada",
            ShirtProposal.Status.APPROVED,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(
            response.json()["proposals"][0]["id"],
            proposal.pk,
        )

    def test_active_admin_can_read_approved_proposals(self):
        proposal = self.create_proposal(
            "Proposta aprovada para admin",
            ShirtProposal.Status.APPROVED,
        )
        admin = User.objects.create_user(
            username="approved_proposals_admin",
            email="approved_admin@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        self.client.force_login(admin)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()["proposals"]],
            [proposal.pk],
        )
        admin.refresh_from_db()
        self.assertFalse(admin.is_superuser)

    def test_pending_and_rejected_proposals_are_not_listed(self):
        self.create_proposal(
            "Aprovada",
            ShirtProposal.Status.APPROVED,
        )
        self.create_proposal(
            "Pendente",
            ShirtProposal.Status.PENDING,
        )
        self.create_proposal(
            "Rejeitada",
            ShirtProposal.Status.REJECTED,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["title"] for item in response.json()["proposals"]],
            ["Aprovada"],
        )

    def test_empty_list_has_consistent_json_shape(self):
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"proposals": []})

    def test_response_fields_use_model_values_and_protected_image_route(self):
        proposal = self.create_proposal(
            "Camisa da turma",
            ShirtProposal.Status.APPROVED,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)
        item = response.json()["proposals"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            item,
            {
                "id": proposal.pk,
                "title": "Camisa da turma",
                "description": "Descrição visual de Camisa da turma.",
                "image_url": reverse("proposal-image", args=[proposal.pk]),
                "base_color": ShirtProposal.BaseColor.BLACK,
                "related_to_course": True,
                "animal": "Raposa",
                "additional_colors": ["BLUE"],
                "has_collar": True,
                "has_phrase": True,
                "phrase_text": "Nossa turma",
            },
        )
        self.assertIsInstance(item["id"], int)
        self.assertIsInstance(item["related_to_course"], bool)
        self.assertIsInstance(item["additional_colors"], list)

    def test_response_does_not_expose_author_or_internal_image_data(self):
        proposal = self.create_proposal(
            "Proposta privada",
            ShirtProposal.Status.APPROVED,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)
        item = response.json()["proposals"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(item),
            {
                "id",
                "title",
                "description",
                "image_url",
                "base_color",
                "related_to_course",
                "animal",
                "additional_colors",
                "has_collar",
                "has_phrase",
                "phrase_text",
            },
        )
        self.assertNotIn(
            "proposal-author-private@escola.com",
            response.content.decode(),
        )
        self.assertNotIn(proposal.image.name, response.content.decode())

    def test_results_are_newest_first_with_stable_id_tiebreaker(self):
        older = self.create_proposal(
            "Mais antiga",
            ShirtProposal.Status.APPROVED,
        )
        newer = self.create_proposal(
            "Mais recente",
            ShirtProposal.Status.APPROVED,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()["proposals"]],
            [newer.pk, older.pk],
        )

    def test_service_rejects_inactive_and_non_student_users(self):
        admin = User.objects.create_user(username="service_non_student")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        inactive = User.objects.create_user(
            username="service_inactive_student",
            is_active=False,
        )
        UserProfile.objects.create(
            user=inactive,
            role=UserProfile.Role.STUDENT,
        )

        for user in (admin, inactive):
            with self.subTest(user=user):
                with self.assertRaises(PermissionDenied):
                    get_approved_proposals(user)


class PreferenceResponseTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="estudante1",
            email="estudante1@escola.com",
            password="senha-de-teste",
        )

    def make_response(self, **overrides):
        data = {
            "student": self.student,
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }
        data.update(overrides)
        return PreferenceResponse(**data)

    def test_valid_preferences_pass_validation(self):
        self.make_response().full_clean()

    def test_animal_is_required_when_selected(self):
        response = self.make_response(wants_animal=True)
        with self.assertRaises(ValidationError):
            response.full_clean()

    def test_animal_must_be_empty_when_not_selected(self):
        response = self.make_response(preferred_animal="Gato")
        with self.assertRaises(ValidationError):
            response.full_clean()

    def test_invalid_colors_are_rejected(self):
        response = self.make_response(
            preferred_colors=["BLUE", "INVALID_COLOR"]
        )
        with self.assertRaises(ValidationError):
            response.full_clean()


class ShirtProposalTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username="autor1",
            email="autor1@escola.com",
            password="senha-de-teste",
        )

    def make_proposal(self, **overrides):
        data = {
            "author": self.author,
            "title": "Camiseta teste",
            "description": "Proposta para teste",
            "image": make_test_image(),
            "base_color": "BLACK",
            "related_to_course": True,
            "animal": "",
            "additional_colors": ["BLUE"],
            "has_collar": True,
            "has_phrase": False,
            "phrase_text": "",
        }
        data.update(overrides)
        return ShirtProposal(**data)

    def test_valid_proposal_passes_validation(self):
        self.make_proposal().full_clean()

    def test_phrase_is_required_when_selected(self):
        proposal = self.make_proposal(has_phrase=True)
        with self.assertRaises(ValidationError):
            proposal.full_clean()

    def test_phrase_must_be_empty_when_not_selected(self):
        proposal = self.make_proposal(phrase_text="Nossa turma")
        with self.assertRaises(ValidationError):
            proposal.full_clean()


class VoteTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="votante1",
            email="votante1@escola.com",
            password="senha-de-teste",
        )
        self.author = User.objects.create_user(
            username="autor2",
            email="autor2@escola.com",
            password="senha-de-teste",
        )
        self.proposal = ShirtProposal.objects.create(
            author=self.author,
            title="Proposta aprovada",
            description="Proposta para teste",
            image="shirt_proposals/teste.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )

    def test_only_approved_proposals_can_receive_votes(self):
        self.proposal.status = ShirtProposal.Status.PENDING
        vote = Vote(student=self.student, proposal=self.proposal)

        with self.assertRaises(ValidationError):
            vote.full_clean()

    def test_student_cannot_vote_twice(self):
        Vote.objects.create(
            student=self.student,
            proposal=self.proposal,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Vote.objects.create(
                    student=self.student,
                    proposal=self.proposal,
                )


class VoteServiceTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="student_voter_service",
            email="student_voter_service@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.author = User.objects.create_user(
            username="voter_proposal_author",
            email="voter_proposal_author@escola.com",
        )
        self.proposal = ShirtProposal.objects.create(
            author=self.author,
            title="Proposta de votação",
            description="Proposta aprovada para votação",
            image="shirt_proposals/vote.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )

    def test_student_can_vote_for_approved_proposal(self):
        vote = cast_vote(self.student, self.proposal.pk)

        self.assertEqual(vote.student, self.student)
        self.assertEqual(vote.proposal, self.proposal)
        self.assertEqual(Vote.objects.count(), 1)

    def test_student_cannot_vote_twice_through_service(self):
        cast_vote(self.student, self.proposal.pk)

        with self.assertRaises(ValidationError):
            cast_vote(self.student, self.proposal.pk)

        self.assertEqual(Vote.objects.count(), 1)

    def test_closed_or_missing_voting_settings_reject_votes(self):
        ElectionSettings.objects.filter(pk=1).update(voting_open=False)
        with self.assertRaises(ValidationError):
            cast_vote(self.student, self.proposal.pk)

        ElectionSettings.objects.all().delete()
        with self.assertRaises(ValidationError):
            cast_vote(self.student, self.proposal.pk)

        self.assertEqual(Vote.objects.count(), 0)

    def test_pending_and_rejected_proposals_cannot_receive_votes(self):
        for status in (
            ShirtProposal.Status.PENDING,
            ShirtProposal.Status.REJECTED,
        ):
            with self.subTest(status=status):
                self.proposal.status = status
                self.proposal.save(update_fields=["status"])

                with self.assertRaises(ValidationError):
                    cast_vote(self.student, self.proposal.pk)

        self.assertEqual(Vote.objects.count(), 0)

    def test_anonymous_inactive_missing_profile_and_admin_are_rejected(self):
        inactive_student = User.objects.create_user(
            username="inactive_voter",
            is_active=False,
        )
        missing_profile = User.objects.create_user(
            username="profileless_voter",
        )
        admin = User.objects.create_user(username="admin_voter")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)

        for user in (
            AnonymousUser(),
            inactive_student,
            missing_profile,
            admin,
        ):
            with self.subTest(user=user):
                with self.assertRaises(PermissionDenied):
                    cast_vote(user, self.proposal.pk)

        self.assertEqual(Vote.objects.count(), 0)

    def test_vote_database_constraint_rejects_duplicate_student(self):
        Vote.objects.create(student=self.student, proposal=self.proposal)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Vote.objects.create(
                    student=self.student,
                    proposal=self.proposal,
                )

    def test_unrelated_database_operational_error_is_not_a_duplicate_vote(self):
        with patch(
            "election.services.Vote.objects.create",
            side_effect=OperationalError("disk I/O error"),
        ):
            with self.assertRaises(OperationalError):
                cast_vote(self.student, self.proposal.pk)


@override_settings(ADMIN_EMAILS=["admin@escola.com"])
class VoteConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.student = User.objects.create_user(
            username="student_concurrent_voter",
            email="student_concurrent_voter@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.author = User.objects.create_user(
            username="concurrent_voter_author",
            email="concurrent_voter_author@escola.com",
        )
        self.admin_user = User.objects.create_user(
            username="concurrent_vote_admin",
            email="admin@escola.com",
            is_staff=True,
            is_superuser=True,
        )
        self.proposal = ShirtProposal.objects.create(
            author=self.author,
            title="Proposta de votação concorrente",
            description="Proposta aprovada para o teste concorrente",
            image="shirt_proposals/concurrent-vote.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )

    def test_simultaneous_votes_use_independent_connections_and_store_one(self):
        start = threading.Barrier(3)
        outcomes = []
        connection_ids = []
        outcome_lock = threading.Lock()

        def submit_vote():
            close_old_connections()
            try:
                connections["default"].ensure_connection()
                connection_ids.append(connections["default"].connection)
                start.wait(timeout=10)
                student = User.objects.get(pk=self.student.pk)
                try:
                    cast_vote(student, self.proposal.pk)
                    outcome = "created"
                except ValidationError:
                    outcome = "duplicate"
                with outcome_lock:
                    outcomes.append(outcome)
            except Exception as exc:
                with outcome_lock:
                    outcomes.append(exc)
            finally:
                connections["default"].close()

        workers = [
            threading.Thread(target=submit_vote),
            threading.Thread(target=submit_vote),
        ]
        for worker in workers:
            worker.start()
        start.wait(timeout=10)
        for worker in workers:
            worker.join(timeout=20)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(set(connection_ids)), 2)
        self.assertCountEqual(outcomes, ["created", "duplicate"])
        self.assertEqual(Vote.objects.filter(student=self.student).count(), 1)

    def test_vote_in_progress_is_serialized_before_admin_closes_voting(self):
        vote_transaction_started = threading.Event()
        close_started = threading.Event()
        outcomes = []
        outcome_lock = threading.Lock()

        def submit_vote_in_transaction():
            close_old_connections()
            try:
                with transaction.atomic():
                    ElectionSettings.objects.get(pk=1)
                    vote_transaction_started.set()
                    if not close_started.wait(timeout=10):
                        raise TimeoutError("Admin close did not start.")
                    cast_vote(
                        User.objects.get(pk=self.student.pk),
                        self.proposal.pk,
                    )
                with outcome_lock:
                    outcomes.append("vote-created")
            except Exception as exc:
                with outcome_lock:
                    outcomes.append(exc)
            finally:
                connections["default"].close()

        def close_voting():
            close_old_connections()
            try:
                if not vote_transaction_started.wait(timeout=10):
                    raise TimeoutError("Vote transaction did not start.")
                request = RequestFactory().post(
                    "/admin/election/electionsettings/"
                )
                request.user = User.objects.get(pk=self.admin_user.pk)
                settings = ElectionSettings.objects.get(pk=1)
                settings.voting_open = False
                close_started.set()
                ElectionSettingsAdmin(
                    ElectionSettings,
                    three_shirt_admin,
                ).save_model(
                    request,
                    settings,
                    form=None,
                    change=True,
                )
                with outcome_lock:
                    outcomes.append("voting-closed")
            except Exception as exc:
                close_started.set()
                with outcome_lock:
                    outcomes.append(exc)
            finally:
                connections["default"].close()

        workers = [
            threading.Thread(target=submit_vote_in_transaction),
            threading.Thread(target=close_voting),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertCountEqual(outcomes, ["vote-created", "voting-closed"])
        self.assertTrue(
            Vote.objects.filter(student=self.student).exists()
        )
        settings = ElectionSettings.objects.get(pk=1)
        self.assertFalse(settings.voting_open)
        self.assertIsNotNone(settings.voting_ended_at)

        proposal = ShirtProposal.objects.get(pk=self.proposal.pk)
        original_title = proposal.title
        proposal.title = "Alteração após corrida de voto e fechamento"
        request = RequestFactory().post("/admin/election/shirtproposal/")
        request.user = User.objects.get(pk=self.admin_user.pk)
        with self.assertRaises(ValidationError):
            ShirtProposalAdmin(
                ShirtProposal,
                three_shirt_admin,
            ).save_model(
                request,
                proposal,
                form=None,
                change=True,
            )
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, original_title)


class ElectionSettingsTests(TestCase):
    def test_survey_and_voting_cannot_be_open_together(self):
        settings = ElectionSettings(
            survey_open=True,
            voting_open=True,
        )

        with self.assertRaises(ValidationError):
            settings.full_clean()

    def test_open_voting_requires_start_time(self):
        settings = ElectionSettings(
            survey_open=False,
            voting_open=True,
        )

        with self.assertRaises(ValidationError):
            settings.full_clean()


@override_settings(ADMIN_EMAILS=["admin@escola.com"])
class ElectionSettingsAdminTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite(name="test-admin")
        self.model_admin = ElectionSettingsAdmin(
            ElectionSettings,
            three_shirt_admin,
        )
        self.authorized_admin = User.objects.create_user(
            username="admin_config",
            email="admin@escola.com",
            is_staff=True,
            is_superuser=True,
        )
        self.student = User.objects.create_user(
            username="student_config",
            email="student@escola.com",
            is_staff=True,
        )
        self.unauthorized_staff = User.objects.create_user(
            username="staff_config",
            email="staff@escola.com",
            is_staff=True,
            is_superuser=True,
        )

    def make_request(self, user):
        request = self.factory.get("/admin/election/electionsettings/")
        request.user = user
        return request

    def test_authorized_admin_can_add_singleton_and_change_survey_state(self):
        request = self.make_request(self.authorized_admin)

        self.assertTrue(self.model_admin.has_add_permission(request))
        self.assertTrue(self.model_admin.has_change_permission(request))

        settings = ElectionSettings(survey_open=True)
        self.model_admin.save_model(request, settings, form=None, change=False)

        settings.refresh_from_db()
        self.assertEqual(settings.pk, 1)
        self.assertTrue(settings.survey_open)
        self.assertFalse(self.model_admin.has_add_permission(request))

        settings.survey_open = False
        self.model_admin.save_model(request, settings, form=None, change=True)
        settings.refresh_from_db()
        self.assertFalse(settings.survey_open)

    def test_only_survey_state_is_exposed_for_editing(self):
        request = self.make_request(self.authorized_admin)

        self.assertEqual(
            self.model_admin.get_fields(request),
            (
                "survey_open",
                "voting_open",
                "results_release_authorized",
            ),
        )

    def test_authorized_admin_can_open_and_close_voting(self):
        request = self.make_request(self.authorized_admin)
        form_class = self.model_admin.get_form(request)
        open_form = form_class(data={
            "survey_open": False,
            "voting_open": True,
            "results_release_authorized": False,
        })
        self.assertTrue(open_form.is_valid(), open_form.errors)

        settings = open_form.save(commit=False)
        self.model_admin.save_model(
            request,
            settings,
            form=open_form,
            change=False,
        )
        settings.refresh_from_db()
        self.assertTrue(settings.voting_open)
        self.assertIsNotNone(settings.voting_started_at)
        self.assertIsNone(settings.voting_ended_at)

        close_form = form_class(
            data={
                "survey_open": False,
                "voting_open": False,
                "results_release_authorized": False,
            },
            instance=settings,
        )
        self.assertTrue(close_form.is_valid(), close_form.errors)
        settings = close_form.save(commit=False)
        self.model_admin.save_model(
            request,
            settings,
            form=close_form,
            change=True,
        )
        settings.refresh_from_db()
        self.assertFalse(settings.voting_open)
        self.assertIsNotNone(settings.voting_ended_at)

    def test_admin_can_reopen_voting_when_no_votes_exist(self):
        settings = ElectionSettings.objects.create(
            voting_open=False,
            voting_started_at=timezone.now() - timedelta(days=2),
            voting_ended_at=timezone.now() - timedelta(days=1),
        )
        request = self.make_request(self.authorized_admin)
        form_class = self.model_admin.get_form(request)
        form = form_class(data={
            "survey_open": False,
            "voting_open": True,
            "results_release_authorized": False,
        }, instance=settings)

        self.assertTrue(form.is_valid(), form.errors)
        settings = form.save(commit=False)
        self.model_admin.save_model(request, settings, form, change=True)

        settings.refresh_from_db()
        self.assertTrue(settings.voting_open)
        self.assertIsNotNone(settings.voting_started_at)
        self.assertIsNone(settings.voting_ended_at)

    def test_reopening_is_revalidated_after_stale_form_and_preserves_timestamps(self):
        previous_start = timezone.now() - timedelta(days=2)
        previous_end = timezone.now() - timedelta(days=1)
        settings = ElectionSettings.objects.create(
            voting_open=False,
            voting_started_at=previous_start,
            voting_ended_at=previous_end,
        )
        request = self.make_request(self.authorized_admin)
        form_class = self.model_admin.get_form(request)
        form = form_class(data={
            "survey_open": False,
            "voting_open": True,
            "results_release_authorized": False,
        }, instance=settings)
        self.assertTrue(form.is_valid(), form.errors)
        settings = form.save(commit=False)

        author = User.objects.create_user(username="reopen_vote_author")
        proposal = ShirtProposal.objects.create(
            author=author,
            title="Proposta votada",
            description="Proposta aprovada para voto",
            image="shirt_proposals/reopen-vote.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        Vote.objects.create(student=self.student, proposal=proposal)

        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                settings,
                form=form,
                change=True,
            )

        self.assertEqual(settings.voting_started_at, previous_start)
        self.assertEqual(settings.voting_ended_at, previous_end)
        settings.refresh_from_db()
        self.assertFalse(settings.voting_open)
        self.assertEqual(settings.voting_started_at, previous_start)
        self.assertEqual(settings.voting_ended_at, previous_end)

        fresh_form = form_class(data={
            "survey_open": False,
            "voting_open": True,
            "results_release_authorized": False,
        }, instance=settings)
        self.assertFalse(fresh_form.is_valid())
        self.assertIn("voting_open", fresh_form.errors)

    def test_admin_controls_results_release_without_changing_voting_state(self):
        settings = ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        request = self.make_request(self.authorized_admin)
        form_class = self.model_admin.get_form(request)

        self.assertFalse(is_results_release_authorized())

        settings.results_release_authorized = True
        form = form_class(
            data={
                "survey_open": False,
                "voting_open": True,
                "results_release_authorized": True,
            },
            instance=settings,
        )
        self.assertTrue(form.is_valid(), form.errors)
        settings = form.save(commit=False)
        self.model_admin.save_model(request, settings, form, change=True)

        settings.refresh_from_db()
        self.assertTrue(settings.results_release_authorized)
        self.assertTrue(settings.voting_open)
        self.assertTrue(is_results_release_authorized())

        settings.results_release_authorized = False
        form = form_class(
            data={
                "survey_open": False,
                "voting_open": True,
                "results_release_authorized": False,
            },
            instance=settings,
        )
        self.assertTrue(form.is_valid(), form.errors)
        settings = form.save(commit=False)
        self.model_admin.save_model(request, settings, form, change=True)

        settings.refresh_from_db()
        self.assertFalse(settings.results_release_authorized)
        self.assertTrue(settings.voting_open)

    def test_results_release_is_disabled_by_default(self):
        settings = ElectionSettings.objects.create(survey_open=False)

        self.assertFalse(settings.results_release_authorized)
        self.assertFalse(is_results_release_authorized())

    def test_unauthorized_admin_cannot_open_or_close_voting(self):
        settings = ElectionSettings.objects.create(survey_open=False)
        request = self.make_request(self.unauthorized_staff)
        settings.voting_open = True

        self.assertFalse(self.model_admin.has_change_permission(request))
        with self.assertRaises(PermissionDenied):
            self.model_admin.save_model(
                request,
                settings,
                form=None,
                change=True,
            )
        settings.refresh_from_db()
        self.assertFalse(settings.voting_open)

    def test_unauthorized_user_cannot_change_results_release(self):
        settings = ElectionSettings.objects.create(survey_open=False)
        request = self.make_request(self.unauthorized_staff)
        settings.results_release_authorized = True

        with self.assertRaises(PermissionDenied):
            self.model_admin.save_model(
                request,
                settings,
                form=None,
                change=True,
            )

        settings.refresh_from_db()
        self.assertFalse(settings.results_release_authorized)

    def test_student_cannot_add_view_or_change_settings(self):
        request = self.make_request(self.student)

        self.assertFalse(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_module_permission(request))
        self.assertFalse(self.model_admin.has_view_permission(request))
        self.assertFalse(self.model_admin.has_add_permission(request))
        self.assertFalse(self.model_admin.has_change_permission(request))

    def test_staff_with_unauthorized_email_cannot_change_settings(self):
        request = self.make_request(self.unauthorized_staff)

        self.assertFalse(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_change_permission(request))

    def test_inactive_authorized_admin_cannot_change_settings(self):
        self.authorized_admin.is_active = False
        self.authorized_admin.save(update_fields=["is_active"])
        request = self.make_request(self.authorized_admin)

        self.assertFalse(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_change_permission(request))

    def test_settings_cannot_be_deleted_from_admin(self):
        request = self.make_request(self.authorized_admin)

        self.assertFalse(self.model_admin.has_delete_permission(request))

    def test_incompatible_survey_opening_is_not_saved(self):
        settings = ElectionSettings.objects.create(
            survey_open=False,
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        settings.survey_open = True
        request = self.make_request(self.authorized_admin)

        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                settings,
                form=None,
                change=True,
            )

        settings.refresh_from_db()
        self.assertFalse(settings.survey_open)
        self.assertTrue(settings.voting_open)


class ElectionResultsTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username="results_author",
            email="results_author@escola.com",
        )
        self.proposal_a = self.make_proposal("Proposta A")
        self.proposal_b = self.make_proposal("Proposta B")
        self.pending_proposal = self.make_proposal(
            "Proposta pendente",
            status=ShirtProposal.Status.PENDING,
        )

    def make_proposal(self, title, status=ShirtProposal.Status.APPROVED):
        return ShirtProposal.objects.create(
            author=self.author,
            title=title,
            description="Proposta para teste de apuração",
            image=f"shirt_proposals/{title.lower().replace(' ', '-')}.png",
            base_color="BLACK",
            related_to_course=True,
            animal="",
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=status,
        )

    def make_student(self, username):
        student = User.objects.create_user(
            username=username,
            email=f"{username}@escola.com",
        )
        UserProfile.objects.create(
            user=student,
            role=UserProfile.Role.STUDENT,
        )
        return student

    def make_response(self, student, **overrides):
        data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }
        data.update(overrides)
        return PreferenceResponse.objects.create(
            student=student,
            **data,
        )

    def make_vote(self, username, proposal):
        student = self.make_student(username)
        return Vote.objects.create(student=student, proposal=proposal)

    def result_for(self, result, proposal):
        return next(
            item for item in result["proposals"]
            if item["proposal_id"] == proposal.pk
        )

    def test_vote_tally_counts_votes_and_calculates_percentages(self):
        self.make_vote("results_voter_1", self.proposal_a)
        self.make_vote("results_voter_2", self.proposal_a)
        self.make_vote("results_voter_3", self.proposal_b)

        result = get_vote_tally()
        proposal_a = self.result_for(result, self.proposal_a)
        proposal_b = self.result_for(result, self.proposal_b)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["total_votes"], 3)
        self.assertEqual(proposal_a["vote_count"], 2)
        self.assertEqual(proposal_b["vote_count"], 1)
        self.assertAlmostEqual(float(proposal_a["percentage"]), 66.6667, places=3)
        self.assertAlmostEqual(float(proposal_b["percentage"]), 33.3333, places=3)
        self.assertAlmostEqual(
            float(proposal_a["percentage"] + proposal_b["percentage"]),
            100,
            places=10,
        )
        self.assertEqual([item["proposal_id"] for item in result["winners"]], [
            self.proposal_a.pk,
        ])

    def test_no_votes_has_no_percentages_or_winner(self):
        result = get_vote_tally()

        self.assertEqual(result["status"], "no_votes")
        self.assertEqual(result["total_votes"], 0)
        self.assertEqual(result["winners"], [])
        self.assertFalse(result["is_tie"])
        self.assertTrue(
            all(item["percentage"] is None for item in result["proposals"])
        )

    def test_vote_tally_returns_all_proposals_tied_for_first(self):
        self.make_vote("tie_voter_1", self.proposal_a)
        self.make_vote("tie_voter_2", self.proposal_b)

        result = get_vote_tally()

        self.assertTrue(result["is_tie"])
        self.assertCountEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_a.pk, self.proposal_b.pk],
        )

    def test_votes_for_nonapproved_proposals_are_reported_but_not_counted(self):
        self.make_vote("valid_tally_voter", self.proposal_a)
        self.make_vote("historical_pending_voter", self.pending_proposal)
        self.pending_proposal.status = ShirtProposal.Status.REJECTED
        self.pending_proposal.save(update_fields=["status"])

        result = get_vote_tally()

        self.assertEqual(result["total_votes"], 1)
        self.assertEqual(result["excluded_votes"], 1)
        self.assertEqual(result["excluded_proposals"], [{
            "proposal_id": self.pending_proposal.pk,
            "title": self.pending_proposal.title,
            "status": ShirtProposal.Status.REJECTED,
            "vote_count": 1,
        }])
        self.assertEqual(Vote.objects.count(), 2)
        self.assertEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_a.pk],
        )

    def test_compatibility_is_mean_of_valid_individual_scores(self):
        matching_student = self.make_student("matching_results_student")
        mismatching_student = self.make_student("mismatching_results_student")
        self.make_student("no_response_results_student")
        self.make_response(matching_student)
        self.make_response(
            mismatching_student,
            base_color="WHITE",
            related_to_course=False,
            preferred_colors=["RED"],
            wants_collar=False,
            wants_phrase=True,
        )

        result = get_compatibility_tally()
        proposal_result = self.result_for(result, self.proposal_a)

        self.assertEqual(proposal_result["valid_assessments"], 2)
        self.assertEqual(
            proposal_result["average_compatibility"],
            Decimal("50"),
        )
        self.assertEqual(proposal_result["status"], "available")
        self.assertEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_a.pk, self.proposal_b.pk],
        )

    def test_insufficient_individual_scores_are_not_counted_as_zero(self):
        student_with_data = self.make_student("partial_results_student")
        student_with_insufficient_data = self.make_student(
            "insufficient_results_student"
        )
        self.make_response(student_with_data)
        self.make_response(student_with_insufficient_data)

        with patch(
            "election.services.calculate_compatibility",
            side_effect=[(80.0, 4), (None, 2), (80.0, 4), (None, 2)],
        ):
            result = get_compatibility_tally()

        proposal_result = self.result_for(result, self.proposal_a)
        self.assertEqual(proposal_result["valid_assessments"], 1)
        self.assertEqual(
            proposal_result["average_compatibility"],
            Decimal("80"),
        )

    def test_no_valid_compatibility_data_is_not_reported_as_zero(self):
        result = get_compatibility_tally()

        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["winners"], [])
        for proposal_result in result["proposals"]:
            self.assertEqual(proposal_result["valid_assessments"], 0)
            self.assertIsNone(proposal_result["average_compatibility"])
            self.assertEqual(proposal_result["status"], "insufficient_data")

    def test_only_active_student_profiles_are_in_compatibility_average(self):
        eligible_student = self.make_student("eligible_results_student")
        inactive_student = self.make_student("inactive_results_student")
        inactive_student.is_active = False
        inactive_student.save(update_fields=["is_active"])
        admin = User.objects.create_user(
            username="admin_results_profile",
            email="admin_results_profile@escola.com",
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        profileless_user = User.objects.create_user(
            username="profileless_results_user",
            email="profileless_results_user@escola.com",
        )
        self.make_response(eligible_student)
        self.make_response(inactive_student)
        self.make_response(admin)
        self.make_response(profileless_user)

        result = get_compatibility_tally()

        for proposal in (self.proposal_a, self.proposal_b):
            proposal_result = self.result_for(result, proposal)
            self.assertEqual(proposal_result["valid_assessments"], 1)
            self.assertEqual(
                proposal_result["average_compatibility"],
                Decimal("100"),
            )

    def test_compatibility_uses_existing_individual_calculator(self):
        student = self.make_student("calculator_reuse_student")
        self.make_response(student)

        with patch(
            "election.services.calculate_compatibility",
            wraps=calculate_compatibility,
        ) as calculator:
            get_compatibility_tally()

        self.assertEqual(calculator.call_count, 2)

    def test_only_approved_proposals_are_in_compatibility_tally(self):
        result = get_compatibility_tally()

        self.assertEqual(
            {item["proposal_id"] for item in result["proposals"]},
            {self.proposal_a.pk, self.proposal_b.pk},
        )

    def test_single_most_compatible_proposal_is_selected(self):
        student = self.make_student("unique_compatibility_student")
        self.make_response(student)
        self.proposal_b.base_color = "WHITE"
        self.proposal_b.related_to_course = False
        self.proposal_b.additional_colors = ["RED"]
        self.proposal_b.has_collar = False
        self.proposal_b.has_phrase = True
        self.proposal_b.save()

        result = get_compatibility_tally()

        self.assertFalse(result["is_tie"])
        self.assertEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_a.pk],
        )

    def test_internal_results_do_not_authorize_release(self):
        self.make_vote("internal_only_voter", self.proposal_a)

        result = get_vote_tally()

        self.assertEqual(result["total_votes"], 1)
        self.assertFalse(is_results_release_authorized())


class PreferenceResponseServiceTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="student_service",
            email="student_service@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        ElectionSettings.objects.create(survey_open=True)
        self.valid_data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }

    def test_creates_and_reads_student_response(self):
        created = save_preference_response(self.student, self.valid_data)

        stored = PreferenceResponse.objects.get(student=self.student)
        self.assertEqual(created.pk, stored.pk)
        self.assertEqual(stored.base_color, "BLACK")
        self.assertEqual(stored.preferred_colors, ["BLUE"])
        self.assertEqual(PreferenceResponse.objects.count(), 1)

    def test_edits_existing_response_while_survey_is_open(self):
        save_preference_response(self.student, self.valid_data)

        updated = save_preference_response(
            self.student,
            {"base_color": "WHITE"},
        )

        updated.refresh_from_db()
        self.assertEqual(updated.base_color, "WHITE")
        self.assertEqual(updated.preferred_colors, ["BLUE"])
        self.assertEqual(PreferenceResponse.objects.count(), 1)

    def test_invalid_data_is_not_persisted(self):
        invalid_data = {
            **self.valid_data,
            "preferred_colors": ["INVALID"],
        }

        with self.assertRaises(ValidationError):
            save_preference_response(self.student, invalid_data)

        self.assertFalse(
            PreferenceResponse.objects.filter(student=self.student).exists()
        )

    def test_invalid_edit_does_not_partially_change_response(self):
        existing = save_preference_response(self.student, self.valid_data)

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {
                    "base_color": "WHITE",
                    "preferred_colors": ["INVALID"],
                },
            )

        existing.refresh_from_db()
        self.assertEqual(existing.base_color, "BLACK")
        self.assertEqual(existing.preferred_colors, ["BLUE"])

    def test_wrong_field_type_is_rejected_without_partial_update(self):
        existing = save_preference_response(self.student, self.valid_data)

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {
                    "base_color": "WHITE",
                    "preferred_animal": None,
                },
            )

        existing.refresh_from_db()
        self.assertEqual(existing.base_color, "BLACK")
        self.assertEqual(existing.preferred_animal, "")

    def test_closed_survey_rejects_creation(self):
        ElectionSettings.objects.filter(pk=1).update(survey_open=False)

        with self.assertRaises(ValidationError):
            save_preference_response(self.student, self.valid_data)

        self.assertFalse(
            PreferenceResponse.objects.filter(student=self.student).exists()
        )

    def test_closed_survey_rejects_edit_without_changing_response(self):
        existing = save_preference_response(self.student, self.valid_data)
        ElectionSettings.objects.filter(pk=1).update(survey_open=False)

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {"base_color": "WHITE"},
            )

        existing.refresh_from_db()
        self.assertEqual(existing.base_color, "BLACK")

    def test_missing_settings_rejects_creation(self):
        ElectionSettings.objects.all().delete()

        with self.assertRaises(ValidationError):
            save_preference_response(self.student, self.valid_data)

        self.assertFalse(
            PreferenceResponse.objects.filter(student=self.student).exists()
        )

    def test_missing_settings_rejects_edit_without_changing_response(self):
        existing = save_preference_response(self.student, self.valid_data)
        ElectionSettings.objects.all().delete()

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {"base_color": "WHITE"},
            )

        existing.refresh_from_db()
        self.assertEqual(existing.base_color, "BLACK")

    def test_inactive_user_is_rejected(self):
        self.student.is_active = False
        self.student.save(update_fields=["is_active"])

        with self.assertRaises(PermissionDenied):
            save_preference_response(self.student, self.valid_data)

    def test_user_without_profile_is_rejected(self):
        user_without_profile = User.objects.create_user(
            username="no_profile",
            email="no_profile@escola.com",
        )

        with self.assertRaises(PermissionDenied):
            save_preference_response(user_without_profile, self.valid_data)

    def test_admin_profile_is_rejected(self):
        self.student.profile.role = UserProfile.Role.ADMIN
        self.student.profile.save(update_fields=["role"])

        with self.assertRaises(PermissionDenied):
            save_preference_response(self.student, self.valid_data)

    def test_payload_cannot_assign_another_student(self):
        other_student = User.objects.create_user(
            username="other_student",
            email="other_student@escola.com",
        )
        UserProfile.objects.create(
            user=other_student,
            role=UserProfile.Role.STUDENT,
        )
        other_response = save_preference_response(
            other_student,
            self.valid_data,
        )

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {
                    "student": other_student.pk,
                    "base_color": "WHITE",
                },
            )

        other_response.refresh_from_db()
        self.assertEqual(other_response.base_color, "BLACK")
        self.assertFalse(
            PreferenceResponse.objects.filter(student=self.student).exists()
        )


class PreferenceQueryServiceTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="student_queries",
            email="student_queries@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )

    def test_get_preference_response_returns_own_response(self):
        response = PreferenceResponse.objects.create(
            student=self.student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )

        self.assertEqual(get_preference_response(self.student), response)

    def test_get_preference_response_returns_none_if_not_submitted(self):
        self.assertIsNone(get_preference_response(self.student))

    def test_get_preference_response_never_returns_another_students_response(self):
        other_student = User.objects.create_user(
            username="student_other_response",
            email="student_other_response@escola.com",
        )
        UserProfile.objects.create(
            user=other_student,
            role=UserProfile.Role.STUDENT,
        )
        other_response = PreferenceResponse.objects.create(
            student=other_student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )

        self.assertIsNone(get_preference_response(self.student))
        self.assertEqual(
            get_preference_response(other_student),
            other_response,
        )

    def test_protected_queries_reject_anonymous_user(self):
        for query in (
            get_preference_response,
            get_approved_proposal_compatibilities,
        ):
            with self.subTest(query=query.__name__):
                with self.assertRaises(PermissionDenied):
                    query(AnonymousUser())

    def test_protected_queries_reject_user_without_profile(self):
        user_without_profile = User.objects.create_user(
            username="student_no_profile_query",
            email="student_no_profile_query@escola.com",
        )

        for query in (
            get_preference_response,
            get_approved_proposal_compatibilities,
        ):
            with self.subTest(query=query.__name__):
                with self.assertRaises(PermissionDenied):
                    query(user_without_profile)

    def test_protected_queries_reject_inactive_user(self):
        self.student.is_active = False
        self.student.save(update_fields=["is_active"])

        for query in (
            get_preference_response,
            get_approved_proposal_compatibilities,
        ):
            with self.subTest(query=query.__name__):
                with self.assertRaises(PermissionDenied):
                    query(self.student)

    def test_protected_queries_reject_admin_profile(self):
        self.student.profile.role = UserProfile.Role.ADMIN
        self.student.profile.save(update_fields=["role"])

        for query in (
            get_preference_response,
            get_approved_proposal_compatibilities,
        ):
            with self.subTest(query=query.__name__):
                with self.assertRaises(PermissionDenied):
                    query(self.student)

    def test_survey_state_reports_open_closed_and_missing_configuration(self):
        self.assertFalse(is_survey_open())

        ElectionSettings.objects.create(survey_open=True)
        self.assertTrue(is_survey_open())

        ElectionSettings.objects.filter(pk=1).update(survey_open=False)
        self.assertFalse(is_survey_open())

        ElectionSettings.objects.all().delete()
        self.assertFalse(is_survey_open())

    def test_compatibilities_are_empty_when_student_has_no_response(self):
        self.assertEqual(
            get_approved_proposal_compatibilities(self.student),
            [],
        )

    def test_compatibilities_include_only_approved_proposals(self):
        PreferenceResponse.objects.create(
            student=self.student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        author = User.objects.create_user(
            username="proposal_author_queries",
            email="proposal_author_queries@escola.com",
        )
        proposal_data = {
            "author": author,
            "title": "Proposta",
            "description": "Descrição",
            "image": "shirt_proposals/teste.png",
            "base_color": "BLACK",
            "related_to_course": True,
            "animal": "",
            "additional_colors": ["BLUE"],
            "has_collar": True,
            "has_phrase": False,
            "phrase_text": "",
        }
        approved = ShirtProposal.objects.create(
            **proposal_data,
            status=ShirtProposal.Status.APPROVED,
        )
        ShirtProposal.objects.create(
            **{**proposal_data, "title": "Pendente"},
            status=ShirtProposal.Status.PENDING,
        )
        ShirtProposal.objects.create(
            **{**proposal_data, "title": "Rejeitada"},
            status=ShirtProposal.Status.REJECTED,
        )

        results = get_approved_proposal_compatibilities(self.student)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], (approved, 100.0, 5))


class CompatibilityCalculationTests(TestCase):
    def make_pair(self, preference_overrides=None, proposal_overrides=None):
        preference_data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
        }
        proposal_data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "animal": "",
            "additional_colors": ["BLUE"],
            "has_collar": True,
            "has_phrase": False,
        }
        preference_data.update(preference_overrides or {})
        proposal_data.update(proposal_overrides or {})
        return (
            PreferenceResponse(**preference_data),
            ShirtProposal(**proposal_data),
        )

    def test_matching_criteria_return_full_compatibility(self):
        preferences, proposal = self.make_pair()

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 5),
        )

    def test_mismatching_criteria_return_zero_compatibility(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "base_color": "WHITE",
                "related_to_course": False,
                "wants_collar": False,
                "wants_phrase": True,
            },
            proposal_overrides={
                "additional_colors": ["RED"],
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (0.0, 5),
        )

    def test_colors_use_jaccard_similarity(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "preferred_colors": ["BLUE", "RED"],
            },
            proposal_overrides={
                "additional_colors": ["BLUE", "GREEN"],
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (86.67, 5),
        )

    def test_animal_names_match_without_case_or_outer_space(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "wants_animal": True,
                "preferred_animal": " Gato ",
            },
            proposal_overrides={
                "animal": "gAtO",
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 6),
        )

    def test_different_animal_names_are_a_mismatch(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "wants_animal": True,
                "preferred_animal": "Gato",
            },
            proposal_overrides={
                "animal": "Cachorro",
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (83.33, 6),
        )

    def test_unspecified_animal_in_proposal_is_not_comparable(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "wants_animal": True,
                "preferred_animal": "Gato",
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 5),
        )

    def test_empty_preferred_color_list_is_omitted(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "preferred_colors": [],
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 4),
        )

    def test_empty_proposal_color_list_scores_zero_when_preferred(self):
        preferences, proposal = self.make_pair(
            proposal_overrides={
                "additional_colors": [],
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (80.0, 5),
        )

    def test_missing_or_invalid_fields_are_omitted(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "base_color": None,
                "related_to_course": None,
                "wants_animal": None,
                "preferred_colors": None,
                "wants_collar": None,
                "wants_phrase": None,
            },
            proposal_overrides={
                "base_color": "INVALID",
                "related_to_course": None,
                "animal": None,
                "additional_colors": None,
                "has_collar": None,
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (None, 0),
        )

    def test_no_comparable_criteria_returns_no_score(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "base_color": None,
                "related_to_course": None,
                "wants_animal": True,
                "preferred_animal": "",
                "preferred_colors": [],
                "wants_collar": None,
                "wants_phrase": None,
            },
            proposal_overrides={
                "base_color": None,
                "related_to_course": None,
                "animal": "",
                "additional_colors": [],
                "has_collar": None,
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (None, 0),
        )

    def test_fewer_than_three_criteria_returns_no_score_and_count(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "base_color": None,
                "related_to_course": None,
                "preferred_colors": [],
                "wants_collar": True,
                "wants_phrase": False,
            },
            proposal_overrides={
                "base_color": None,
                "related_to_course": None,
                "additional_colors": [],
                "has_collar": True,
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (None, 1),
        )

    def test_exactly_two_comparable_criteria_return_no_score(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "preferred_colors": [],
                "wants_collar": None,
                "wants_phrase": None,
            },
            proposal_overrides={
                "additional_colors": [],
                "has_collar": None,
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (None, 2),
        )

    def test_three_comparable_criteria_are_enough_to_score(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "preferred_colors": [],
                "wants_phrase": None,
            },
            proposal_overrides={
                "additional_colors": [],
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 3),
        )


class ShirtProposalSubmissionTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.temp_dir.name),
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.student = User.objects.create_user(
            username="proposal_student",
            email="proposal_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )

    def submit(self, data=None):
        return submit_shirt_proposal(
            self.student,
            data or make_proposal_data(),
        )

    def test_valid_submission_sets_author_and_pending_status(self):
        proposal = self.submit()

        self.assertEqual(proposal.author, self.student)
        self.assertEqual(proposal.status, ShirtProposal.Status.PENDING)
        self.assertIsNotNone(proposal.pk)

    def test_payload_cannot_choose_author_or_status(self):
        for field, value in (
            ("author", User.objects.create_user(username="other_author")),
            ("status", ShirtProposal.Status.APPROVED),
        ):
            with self.subTest(field=field):
                data = make_proposal_data()
                data[field] = value
                with self.assertRaises(ValidationError):
                    self.submit(data)

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_blank_or_whitespace_title_and_description_are_rejected(self):
        invalid_values = (
            {"title": ""},
            {"title": "   \t"},
            {"description": ""},
            {"description": " \n "},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self.submit(make_proposal_data(**overrides))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_title_and_description_model_limits_are_enforced(self):
        for overrides in (
            {"title": "T" * 101},
            {"description": "D" * 1001},
        ):
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(ValidationError):
                    self.submit(make_proposal_data(**overrides))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_existing_visual_field_validation_is_preserved(self):
        invalid_values = (
            {"additional_colors": ["NOT_A_COLOR"]},
            {"additional_colors": ["BLUE", "BLUE"]},
            {"has_phrase": True, "phrase_text": ""},
            {"has_phrase": False, "phrase_text": "Unrequested phrase"},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self.submit(make_proposal_data(**overrides))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_png_jpeg_and_webp_images_are_accepted(self):
        for image_format in ("PNG", "JPEG", "WEBP"):
            with self.subTest(image_format=image_format):
                proposal = self.submit(
                    make_proposal_data(
                        image=make_test_image(image_format=image_format)
                    )
                )
                self.assertEqual(
                    proposal.status,
                    ShirtProposal.Status.PENDING,
                )

        self.assertEqual(ShirtProposal.objects.count(), 3)

    def test_image_content_is_checked_instead_of_filename(self):
        proposal = self.submit(
            make_proposal_data(
                image=make_test_image(name="design.gif"),
            )
        )
        self.assertEqual(proposal.status, ShirtProposal.Status.PENDING)

        disguised_gif = make_test_image(
            image_format="GIF",
            name="design.png",
        )
        with self.assertRaises(ValidationError):
            self.submit(make_proposal_data(image=disguised_gif))

        self.assertEqual(ShirtProposal.objects.count(), 1)

    def test_non_image_and_corrupted_image_are_rejected(self):
        invalid_files = (
            SimpleUploadedFile(
                "not-image.png",
                b"plain text, not an image",
                content_type="image/png",
            ),
            make_test_image(
                image_format="PNG",
                contents=b"\x89PNG\r\n\x1a\ncorrupted",
            ),
        )
        for uploaded_file in invalid_files:
            with self.subTest(name=uploaded_file.name):
                with self.assertRaises(ValidationError):
                    self.submit(make_proposal_data(image=uploaded_file))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_image_larger_than_five_megabytes_is_rejected(self):
        valid_png = make_test_image().read()
        oversized_png = make_test_image(
            contents=valid_png + b"x" * (5 * 1024 * 1024)
        )

        with self.assertRaises(ValidationError):
            self.submit(make_proposal_data(image=oversized_png))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_failed_validation_does_not_persist_proposal_or_file(self):
        with self.assertRaises(ValidationError):
            self.submit(make_proposal_data(title="  "))

        self.assertEqual(ShirtProposal.objects.count(), 0)
        self.assertEqual(list(Path(self.temp_dir.name).rglob("*")), [])

    def test_anonymous_inactive_missing_profile_and_admin_are_rejected(self):
        inactive_student = User.objects.create_user(
            username="inactive_proposer",
            is_active=False,
        )
        no_profile = User.objects.create_user(username="no_profile_proposer")
        admin = User.objects.create_user(username="admin_proposer")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)

        rejected_users = (
            AnonymousUser(),
            inactive_student,
            no_profile,
            admin,
        )
        for user in rejected_users:
            with self.subTest(user=user):
                with self.assertRaises(PermissionDenied):
                    submit_shirt_proposal(user, make_proposal_data())

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_private_storage_has_no_public_url(self):
        proposal = self.submit()
        stored_path = Path(proposal.image.storage.path(proposal.image.name))

        self.assertTrue(
            stored_path.is_relative_to(Path(self.temp_dir.name).resolve())
        )
        with self.assertRaises(ValueError):
            proposal.image.storage.url(proposal.image.name)


class PendingProposalDescriptionTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="proposal_reader",
            email="proposal_reader@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )

    def test_students_can_read_pending_description_without_image_data(self):
        proposal = ShirtProposal.objects.create(
            **make_proposal_data(image="shirt_proposals/teste.png"),
            author=self.student,
        )

        descriptions = get_pending_proposal_descriptions(self.student)

        self.assertEqual(
            descriptions,
            [{
                "id": proposal.pk,
                "title": proposal.title,
                "description": proposal.description,
            }],
        )
        self.assertNotIn("image", descriptions[0])
        self.assertNotIn("image_url", descriptions[0])

    def test_pending_description_query_rejects_unauthorized_users(self):
        for user in (
            AnonymousUser(),
            User.objects.create_user(username="proposal_reader_no_profile"),
        ):
            with self.subTest(user=user):
                with self.assertRaises(PermissionDenied):
                    get_pending_proposal_descriptions(user)


@override_settings(ADMIN_EMAILS=["proposal_admin@escola.com"])
class ShirtProposalAdminTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.temp_dir.name),
            ADMIN_EMAILS=["proposal_admin@escola.com"],
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.request_factory = RequestFactory()
        self.model_admin = ShirtProposalAdmin(
            ShirtProposal,
            three_shirt_admin,
        )
        self.authorized_admin = User.objects.create_user(
            username="proposal_admin",
            email="proposal_admin@escola.com",
            is_staff=True,
        )
        self.unauthorized_admin = User.objects.create_user(
            username="proposal_unauthorized",
            email="proposal_unauthorized@escola.com",
            is_staff=True,
        )
        self.student = User.objects.create_user(
            username="proposal_admin_student",
            email="proposal_admin_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.proposal = submit_shirt_proposal(
            self.student,
            make_proposal_data(),
        )

    def request_for(self, user):
        request = self.request_factory.post("/admin/election/shirtproposal/")
        request.user = user
        return request

    def test_authorized_admin_can_approve_and_reject_proposals(self):
        request = self.request_for(self.authorized_admin)
        self.assertTrue(self.model_admin.has_change_permission(request))

        self.proposal.status = ShirtProposal.Status.APPROVED
        self.model_admin.save_model(
            request,
            self.proposal,
            form=None,
            change=True,
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ShirtProposal.Status.APPROVED)
        self.assertIsNotNone(self.proposal.reviewed_at)

        self.proposal.status = ShirtProposal.Status.REJECTED
        self.model_admin.save_model(
            request,
            self.proposal,
            form=None,
            change=True,
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ShirtProposal.Status.REJECTED)

    def test_proposal_without_votes_can_be_edited_after_voting_closes(self):
        ElectionSettings.objects.create(voting_open=False)
        request = self.request_for(self.authorized_admin)
        self.proposal.title = "Título revisado"
        self.proposal.status = ShirtProposal.Status.APPROVED

        self.assertTrue(
            self.model_admin.has_change_permission(request, self.proposal)
        )
        self.model_admin.save_model(
            request,
            self.proposal,
            form=None,
            change=True,
        )

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.title, "Título revisado")
        self.assertEqual(self.proposal.status, ShirtProposal.Status.APPROVED)

    def test_proposal_cannot_be_edited_or_moderated_while_voting_is_open(self):
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        request = self.request_for(self.authorized_admin)
        original_title = self.proposal.title
        self.proposal.title = "Título alterado durante a votação"
        self.proposal.status = ShirtProposal.Status.APPROVED

        self.assertFalse(
            self.model_admin.has_change_permission(request, self.proposal)
        )
        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                self.proposal,
                form=None,
                change=True,
            )

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.title, original_title)
        self.assertEqual(self.proposal.status, ShirtProposal.Status.PENDING)

    def test_stale_proposal_edit_is_rejected_if_voting_opens_before_save(self):
        ElectionSettings.objects.create(voting_open=False)
        stale_proposal = ShirtProposal.objects.get(pk=self.proposal.pk)
        stale_proposal.title = "Edição de formulário antigo"
        ElectionSettings.objects.filter(pk=1).update(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        request = self.request_for(self.authorized_admin)

        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                stale_proposal,
                form=None,
                change=True,
            )

        self.proposal.refresh_from_db()
        self.assertNotEqual(self.proposal.title, "Edição de formulário antigo")

    def test_proposal_with_votes_cannot_change_content_or_status_after_close(self):
        ElectionSettings.objects.create(voting_open=False)
        request = self.request_for(self.authorized_admin)
        self.proposal.status = ShirtProposal.Status.APPROVED
        self.model_admin.save_model(
            request,
            self.proposal,
            form=None,
            change=True,
        )
        Vote.objects.create(student=self.student, proposal=self.proposal)
        original_title = self.proposal.title

        self.proposal.title = "Título alterado depois do voto"
        self.proposal.status = ShirtProposal.Status.REJECTED
        self.assertFalse(
            self.model_admin.has_change_permission(request, self.proposal)
        )
        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                self.proposal,
                form=None,
                change=True,
            )

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.title, original_title)
        self.assertEqual(self.proposal.status, ShirtProposal.Status.APPROVED)
        self.assertEqual(Vote.objects.filter(proposal=self.proposal).count(), 1)

    def test_unauthorized_staff_and_student_cannot_moderate(self):
        for user in (self.unauthorized_admin, self.student):
            with self.subTest(user=user):
                request = self.request_for(user)
                self.assertFalse(
                    self.model_admin.has_change_permission(request)
                )

    def test_inactive_authorized_admin_cannot_moderate(self):
        self.authorized_admin.is_active = False
        self.authorized_admin.save(update_fields=["is_active"])
        request = self.request_for(self.authorized_admin)

        self.assertFalse(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_change_permission(request))

    def test_author_is_read_only_and_status_is_moderatable(self):
        request = self.request_for(self.authorized_admin)
        readonly_fields = self.model_admin.get_readonly_fields(
            request,
            self.proposal,
        )

        self.assertIn("author", readonly_fields)
        self.assertNotIn("status", readonly_fields)

    def test_admin_cannot_add_or_delete_proposals(self):
        request = self.request_for(self.authorized_admin)

        self.assertTrue(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_add_permission(request))
        self.assertFalse(self.model_admin.has_delete_permission(request))

    def test_invalid_status_is_rejected_before_persistence(self):
        request = self.request_for(self.authorized_admin)
        self.proposal.status = "INVALID"

        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                self.proposal,
                form=None,
                change=True,
            )

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ShirtProposal.Status.PENDING)


class ProposalImageAccessTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.temp_dir.name),
            ADMIN_EMAILS=["image_admin@escola.com"],
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.client = Client()
        self.student = User.objects.create_user(
            username="image_student",
            email="image_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.admin = User.objects.create_user(
            username="image_admin",
            email="image_admin@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(
            user=self.admin,
            role=UserProfile.Role.ADMIN,
        )
        self.author = User.objects.create_user(
            username="image_author",
            email="image_author@escola.com",
        )
        UserProfile.objects.create(
            user=self.author,
            role=UserProfile.Role.STUDENT,
        )

    def create_proposal(self, status=ShirtProposal.Status.PENDING):
        proposal = submit_shirt_proposal(
            self.author,
            make_proposal_data(),
        )
        if status != ShirtProposal.Status.PENDING:
            proposal.status = status
            proposal.save(update_fields=["status"])
        return proposal

    def image_url(self, proposal):
        return reverse("proposal-image", args=[proposal.pk])

    def test_admin_can_view_pending_image_for_moderation(self):
        proposal = self.create_proposal()
        self.client.force_login(self.admin)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(
            b"".join(response.streaming_content)[:8],
            b"\x89PNG\r\n\x1a\n",
        )

    def test_student_cannot_view_pending_image(self):
        proposal = self.create_proposal()
        self.client.force_login(self.student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_unauthorized_staff_cannot_view_pending_image(self):
        proposal = self.create_proposal()
        unauthorized_staff = User.objects.create_user(
            username="image_unauthorized",
            email="image_unauthorized@escola.com",
            is_staff=True,
        )
        self.client.force_login(unauthorized_staff)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_anonymous_user_cannot_view_pending_image(self):
        proposal = self.create_proposal()

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_authorized_staff_without_admin_profile_cannot_view_pending_image(
        self,
    ):
        proposal = self.create_proposal()
        admin_without_profile = User.objects.create_user(
            username="image_admin_no_profile",
            email="image_admin@escola.com",
            is_staff=True,
        )
        self.client.force_login(admin_without_profile)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_authenticated_student_can_view_approved_image(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)
        self.client.force_login(self.student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(
            b"".join(response.streaming_content).startswith(
                b"\x89PNG\r\n\x1a\n"
            )
        )

    def test_anonymous_user_cannot_view_approved_image(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_inactive_student_cannot_view_approved_image(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)
        inactive_student = User.objects.create_user(
            username="inactive_image_student",
            is_active=False,
        )
        UserProfile.objects.create(
            user=inactive_student,
            role=UserProfile.Role.STUDENT,
        )
        self.client.force_login(inactive_student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_student_profile_cannot_grant_admin_access_to_pending_image(self):
        proposal = self.create_proposal()
        staff_student = User.objects.create_user(
            username="image_staff_student",
            email="image_admin@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(
            user=staff_student,
            role=UserProfile.Role.STUDENT,
        )
        self.client.force_login(staff_student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_authorized_admin_can_view_approved_image(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)
        request = RequestFactory().get(self.image_url(proposal))
        request.user = self.admin

        response = proposal_image(request, proposal.pk)

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.status_code, 200)
        image_file = response.file_to_stream
        self.assertFalse(image_file.closed)
        response.close()
        self.assertTrue(image_file.closed)

    def test_invalid_image_content_is_not_delivered(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)
        image_path = Path(
            proposal.image.storage.path(proposal.image.name)
        )
        image_path.write_bytes(b"not an image")
        self.client.force_login(self.student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 404)

    def test_rejected_image_is_not_delivered_even_to_admin(self):
        proposal = self.create_proposal(ShirtProposal.Status.REJECTED)
        self.client.force_login(self.admin)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 404)

    def test_changing_proposal_id_does_not_bypass_pending_image_permission(self):
        proposal = self.create_proposal()
        self.client.force_login(self.student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_image_cannot_be_fetched_from_public_media_url(self):
        proposal = self.create_proposal()

        with self.assertRaises(ValueError):
            proposal.image.url

        response = self.client.get(
            f"/media/{proposal.image.name}",
        )
        self.assertEqual(response.status_code, 404)


class PrivateImageStorageMigrationTests(TestCase):
    def test_existing_public_proposal_image_is_moved_to_private_storage(self):
        public_dir = tempfile.TemporaryDirectory()
        private_dir = tempfile.TemporaryDirectory()
        self.addCleanup(public_dir.cleanup)
        self.addCleanup(private_dir.cleanup)
        settings_override = override_settings(
            MEDIA_ROOT=Path(public_dir.name),
            PRIVATE_MEDIA_ROOT=Path(private_dir.name),
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        author = User.objects.create_user(username="legacy_image_author")
        image_name = "shirt_proposals/legacy.png"
        public_storage = FileSystemStorage(location=public_dir.name)
        public_storage.save(image_name, make_test_image())
        proposal = ShirtProposal.objects.create(
            author=author,
            title="Proposta legada",
            description="Imagem armazenada antes do armazenamento privado.",
            image=image_name,
            base_color="BLACK",
            related_to_course=True,
            has_collar=True,
            has_phrase=False,
        )

        migration = import_module(
            "election.migrations.0004_alter_shirtproposal_image"
        )
        app_registry = type(
            "MigrationApps",
            (),
            {"get_model": staticmethod(lambda app, model: ShirtProposal)},
        )()
        schema_editor = type(
            "SchemaEditor",
            (),
            {"connection": type("Connection", (), {"alias": "default"})()},
        )()

        migration.move_proposal_images_to_private_storage(
            app_registry,
            schema_editor,
        )

        migration.move_proposal_images_to_private_storage(
            app_registry,
            schema_editor,
        )

        proposal.refresh_from_db()
        private_file = Path(
            proposal.image.storage.path(proposal.image.name)
        )
        self.assertTrue(private_file.is_file())
        self.assertFalse(public_storage.exists(image_name))
        self.assertEqual(
            private_file.read_bytes()[:8],
            b"\x89PNG\r\n\x1a\n",
        )
        private_files = [
            path
            for path in Path(private_dir.name).rglob("*")
            if path.is_file()
        ]
        self.assertEqual(private_files, [private_file])

        with self.assertRaisesRegex(RuntimeError, "armazenamento público"):
            migration.restore_proposal_images_to_public_storage(
                app_registry,
                schema_editor,
            )

        self.assertFalse(public_storage.exists(image_name))
        self.assertTrue(private_file.is_file())
        self.assertEqual(
            len([
                path
                for path in Path(private_dir.name).rglob("*")
                if path.is_file()
            ]),
            1,
        )


@override_settings(ADMIN_EMAILS=["admin@escola.com"])
class AdminElectionApiPermissionTests(TestCase):
    dashboard_url = "/api/election/admin/dashboard/"
    user_role_url = "/api/election/admin/users/{}/role/"
    user_status_url = "/api/election/admin/users/{}/status/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="api_admin",
            email="admin@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(user=self.admin, role=UserProfile.Role.ADMIN)
        self.student = User.objects.create_user(
            username="api_student",
            email="student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )

    def test_admin_can_access_admin_dashboard_without_superuser_status(self):
        self.client.force_login(self.admin)

        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["counts"]["admins_total"])
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_superuser)

    def test_student_and_anonymous_user_cannot_access_admin_dashboard(self):
        student_response = self.client.get(self.dashboard_url)
        self.assertEqual(student_response.status_code, 403)

        self.client.force_login(self.student)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 403)

    def test_promoting_a_user_to_admin_does_not_grant_superuser(self):
        self.client.force_login(self.admin)
        target = User.objects.create_user(
            username="promoted_admin",
            email="promoted@escola.com",
        )
        UserProfile.objects.create(user=target, role=UserProfile.Role.STUDENT)

        response = self.client.post(
            self.user_role_url.format(target.pk),
            data=json.dumps({"role": UserProfile.Role.ADMIN}),
            content_type="application/json",
        )

        target.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(target.profile.role, UserProfile.Role.ADMIN)
        self.assertTrue(target.is_staff)
        self.assertFalse(target.is_superuser)

    def test_only_active_admin_can_keep_admin_role_and_status(self):
        self.client.force_login(self.admin)

        role_response = self.client.post(
            self.user_role_url.format(self.admin.pk),
            data=json.dumps({"role": UserProfile.Role.STUDENT}),
            content_type="application/json",
        )
        status_response = self.client.post(
            self.user_status_url.format(self.admin.pk),
            data=json.dumps({"is_active": False}),
            content_type="application/json",
        )

        self.admin.refresh_from_db()
        self.assertEqual(role_response.status_code, 400)
        self.assertEqual(status_response.status_code, 400)
        self.assertTrue(self.admin.is_active)
        self.assertEqual(self.admin.profile.role, UserProfile.Role.ADMIN)


class StudentElectionActionApiTests(TestCase):
    preferences_url = "/api/election/student/preferences/"
    submissions_url = "/api/election/student/submissions/"
    vote_url = "/api/election/student/vote/"
    results_url = "/api/election/student/results/"

    def setUp(self):
        self.student = User.objects.create_user(
            username="student_action_api",
            email="student_action_api@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.client = Client()

    def preference_data(self, **overrides):
        data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }
        data.update(overrides)
        return data

    def create_proposal(self, status=ShirtProposal.Status.APPROVED):
        author = User.objects.create_user(
            username=f"action_proposal_author_{ShirtProposal.objects.count()}",
        )
        return ShirtProposal.objects.create(
            author=author,
            title="Proposta aprovada para voto",
            description="Descrição da proposta.",
            image="shirt_proposals/action-api.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=status,
        )

    def submission_data(self, **overrides):
        data = make_proposal_data(**overrides)
        for field in ("related_to_course", "has_collar", "has_phrase"):
            data[field] = "true" if data[field] else "false"
        data["additional_colors"] = json.dumps(data["additional_colors"])
        return data

    def test_preferences_post_requires_authentication_and_open_survey(self):
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(self.preference_data()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

        self.client.force_login(self.student)
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(self.preference_data()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("survey", response.json()["fields"])
        self.assertFalse(
            PreferenceResponse.objects.filter(student=self.student).exists()
        )

    def test_preferences_post_saves_and_updates_only_own_response(self):
        ElectionSettings.objects.create(survey_open=True)
        self.client.force_login(self.student)
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(self.preference_data()),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_preferences"])
        saved = PreferenceResponse.objects.get(student=self.student)
        self.assertEqual(saved.base_color, "BLACK")

        changed = self.preference_data(base_color="WHITE")
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(changed),
            content_type="application/json",
        )
        saved.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved.base_color, "WHITE")
        self.assertEqual(PreferenceResponse.objects.count(), 1)

    def test_preferences_post_rejects_invalid_fields_and_nonstudent(self):
        ElectionSettings.objects.create(survey_open=True)
        self.client.force_login(self.student)
        invalid = self.preference_data(base_color="PURPLE")
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(invalid),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            PreferenceResponse.objects.filter(student=self.student).exists()
        )

        admin = User.objects.create_user(username="action_api_admin")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(self.preference_data()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_student_mutations_require_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.student)
        payload = json.dumps(self.preference_data())

        rejected = client.post(
            self.preferences_url,
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 403)

        csrf_response = client.get("/api/accounts/csrf/")
        token = csrf_response.json()["csrfToken"]
        ElectionSettings.objects.create(survey_open=True)
        accepted = client.post(
            self.preferences_url,
            data=payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(accepted.status_code, 200)

    def test_submission_post_sets_pending_and_list_is_scoped_to_author(self):
        with tempfile.TemporaryDirectory() as temp_dir, override_settings(
            PRIVATE_MEDIA_ROOT=Path(temp_dir),
        ):
            self.client.force_login(self.student)
            payload = self.submission_data()
            response = self.client.post(self.submissions_url, payload)

            self.assertEqual(response.status_code, 201)
            proposal = ShirtProposal.objects.get(pk=response.json()["proposal"]["id"])
            self.assertEqual(proposal.author, self.student)
            self.assertEqual(proposal.status, ShirtProposal.Status.PENDING)

            other = User.objects.create_user(username="other_submission_owner")
            ShirtProposal.objects.create(
                **make_proposal_data(image="shirt_proposals/other.png"),
                author=other,
            )
            response = self.client.get(self.submissions_url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [item["id"] for item in response.json()["proposals"]],
                [proposal.pk],
            )
            self.assertNotIn("image", response.json()["proposals"][0])

    def test_submission_rejects_anonymous_admin_and_invalid_image(self):
        response = self.client.get(self.submissions_url)
        self.assertEqual(response.status_code, 401)

        admin = User.objects.create_user(username="submission_api_admin")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.get(self.submissions_url)
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.student)
        invalid_payload = self.submission_data(
            image=SimpleUploadedFile(
                "invalid.png",
                b"not an image",
                content_type="image/png",
            )
        )
        response = self.client.post(self.submissions_url, invalid_payload)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            ShirtProposal.objects.filter(author=self.student).exists()
        )

    def test_vote_api_enforces_authentication_settings_approval_and_uniqueness(self):
        proposal = self.create_proposal()
        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

        self.client.force_login(self.student)
        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Vote.objects.filter(student=self.student).exists())

        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        rejected = self.create_proposal(ShirtProposal.Status.PENDING)
        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": rejected.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Vote.objects.filter(student=self.student).exists())

        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_voted"])
        self.assertEqual(Vote.objects.filter(student=self.student).count(), 1)

        duplicate = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(Vote.objects.filter(student=self.student).count(), 1)

    def test_results_api_requires_release_and_closed_voting_and_returns_aggregates(self):
        proposal = self.create_proposal()
        response = self.client.get(self.results_url)
        self.assertEqual(response.status_code, 401)

        self.client.force_login(self.student)
        response = self.client.get(self.results_url)
        self.assertEqual(response.status_code, 403)

        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
            results_release_authorized=True,
        )
        response = self.client.get(self.results_url)
        self.assertEqual(response.status_code, 409)

        ElectionSettings.objects.filter(pk=1).update(voting_open=False)
        Vote.objects.create(student=self.student, proposal=proposal)
        response = self.client.get(self.results_url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["total_votes"], 1)
        self.assertEqual(data["proposals"][0]["proposal_id"], proposal.pk)
        self.assertNotIn("student", response.content.decode())
        self.assertNotIn("email", response.content.decode())
        self.assertNotIn("excluded_proposals", data)

    def test_results_without_votes_reports_no_votes(self):
        ElectionSettings.objects.create(results_release_authorized=True)
        self.client.force_login(self.student)

        response = self.client.get(self.results_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_votes")
        self.assertEqual(response.json()["total_votes"], 0)
        self.assertEqual(response.json()["winners"], [])

    def test_student_participation_journey_across_election_endpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir, override_settings(
            PRIVATE_MEDIA_ROOT=Path(temp_dir),
        ):
            ElectionSettings.objects.create(survey_open=True)
            self.client.force_login(self.student)

            preferences_response = self.client.post(
                self.preferences_url,
                data=json.dumps(self.preference_data()),
                content_type="application/json",
            )
            self.assertEqual(preferences_response.status_code, 200)

            submission_response = self.client.post(
                self.submissions_url,
                self.submission_data(title="Jornada completa"),
            )
            self.assertEqual(submission_response.status_code, 201)
            proposal_id = submission_response.json()["proposal"]["id"]

            own_submissions = self.client.get(self.submissions_url)
            self.assertEqual(
                own_submissions.json()["proposals"][0]["status"],
                ShirtProposal.Status.PENDING,
            )

            proposal = ShirtProposal.objects.get(pk=proposal_id)
            proposal.status = ShirtProposal.Status.APPROVED
            proposal.save(update_fields=["status"])
            gallery = self.client.get("/api/election/student/proposals/")
            self.assertEqual(
                [item["id"] for item in gallery.json()["proposals"]],
                [proposal_id],
            )

            ElectionSettings.objects.filter(pk=1).update(
                survey_open=False,
                voting_open=True,
                voting_started_at=timezone.now(),
            )
            vote_response = self.client.post(
                self.vote_url,
                data=json.dumps({"proposal_id": proposal_id}),
                content_type="application/json",
            )
            self.assertEqual(vote_response.status_code, 200)
            self.assertTrue(vote_response.json()["has_voted"])

            ElectionSettings.objects.filter(pk=1).update(
                voting_open=False,
                results_release_authorized=True,
                voting_ended_at=timezone.now(),
            )
            results = self.client.get(self.results_url)
            self.assertEqual(results.status_code, 200)
            self.assertEqual(results.json()["total_votes"], 1)
            self.assertEqual(
                results.json()["winners"][0]["proposal_id"],
                proposal_id,
            )

            logout_response = self.client.post("/api/accounts/logout/")
            self.assertEqual(logout_response.status_code, 200)
            session_response = self.client.get("/api/accounts/session/")
            self.assertEqual(
                session_response.json(),
                {"authenticated": False},
            )
