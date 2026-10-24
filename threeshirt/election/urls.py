from django.urls import path

from . import views

urlpatterns = [
    path(
        "api/election/student/preferences/",
        views.student_preferences,
        name="student-preferences",
    ),
    path(
        "api/election/student/proposals/",
        views.approved_student_proposals,
        name="student-approved-proposals",
    ),
    path(
        "api/election/student/dashboard/",
        views.student_dashboard_status,
        name="student-dashboard-status",
    ),
    path(
        "proposals/<int:proposal_id>/image/",
        views.proposal_image,
        name="proposal-image",
    ),
]
