from django.urls import path

from apps.organization.views import AdminAreasView

from .views import (
    ActivationView,
    AdminAccountsView,
    AdminInviteView,
    AdminIssueResetView,
    AdminRecoveryRequestsView,
    AdminResendInvitationView,
    AdminUnlockView,
    LinkPasswordValidationView,
    LinkPreviewView,
    LoginView,
    MeView,
    RecoveryRequestView,
    ResetView,
    csrf_view,
    logout_view,
    refresh_view,
)

urlpatterns = [
    path("csrf/", csrf_view),
    path("login/", LoginView.as_view()),
    path("refresh/", refresh_view),
    path("logout/", logout_view),
    path("me/", MeView.as_view()),
    path("activate/", ActivationView.as_view()),
    path("reset/", ResetView.as_view()),
    path("recovery-requests/", RecoveryRequestView.as_view()),
    path("admin/areas/", AdminAreasView.as_view()),
    path("admin/accounts/", AdminAccountsView.as_view()),
    path("admin/invitations/", AdminInviteView.as_view()),
    path(
        "admin/accounts/<int:account_id>/resend-invitation/",
        AdminResendInvitationView.as_view(),
    ),
    path(
        "admin/accounts/<int:account_id>/unlock/",
        AdminUnlockView.as_view(),
    ),
    path("activate/preview/", LinkPreviewView.as_view(), {"purpose": "activation"}),
    path(
        "activate/validate-password/",
        LinkPasswordValidationView.as_view(),
        {"purpose": "activation"},
    ),
    path("reset/preview/", LinkPreviewView.as_view(), {"purpose": "reset"}),
    path(
        "reset/validate-password/",
        LinkPasswordValidationView.as_view(),
        {"purpose": "reset"},
    ),
    path("admin/recovery-requests/", AdminRecoveryRequestsView.as_view()),
    path("admin/recovery-requests/<int:request_id>/issue/", AdminIssueResetView.as_view()),
]
