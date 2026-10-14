
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import ElectionSettings, PreferenceResponse, ShirtProposal, Vote

User = get_user_model()


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
            "image": "shirt_proposals/teste.png",
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
