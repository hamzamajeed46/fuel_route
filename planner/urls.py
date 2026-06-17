from django.urls import path
from .views import RoutePlanView

urlpatterns = [
    path("route/", RoutePlanView.as_view(), name="route-plan"),
]
