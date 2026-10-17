
from django.urls import include, path
from accounts.admin import three_shirt_admin

urlpatterns = [
    path("admin/", three_shirt_admin.urls),
    path("api/accounts/", include("accounts.urls")),
    path("", include("election.urls")),
]
