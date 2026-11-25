from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TeamViewSet

router = DefaultRouter()
router.register(r"teams", TeamViewSet, basename="team")

urlpatterns = [
    path("mine/", TeamViewSet.as_view({"get": "mine"})),
    path("", include(router.urls)),
]
