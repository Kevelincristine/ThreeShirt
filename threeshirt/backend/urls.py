from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # Área administrativa do Django.
    path('admin/', admin.site.urls),

    # Endpoints relacionados às contas do ThreeShirt.
    path('api/accounts/', include('accounts.urls')),
]