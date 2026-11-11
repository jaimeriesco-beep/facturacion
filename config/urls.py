from django.contrib.auth.views import LoginView
from django.urls import include, path

from core.forms import IngresoForm

urlpatterns = [
    path("cuenta/login/", LoginView.as_view(authentication_form=IngresoForm), name="login"),
    path("cuenta/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]
