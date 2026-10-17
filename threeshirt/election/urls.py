from django.urls import path

from . import views

urlpatterns = [
    path(
        "proposals/<int:proposal_id>/image/",
        views.proposal_image,
        name="proposal-image",
    ),
]
