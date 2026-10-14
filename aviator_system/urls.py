from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include, reverse_lazy
from django.views.generic import RedirectView
from core.forms import AccountPasswordChangeForm, AccountPasswordResetForm, AccountSetPasswordForm

urlpatterns = [
    path('admin/', admin.site.urls),
    path(
        'account/password/',
        auth_views.PasswordChangeView.as_view(
            form_class=AccountPasswordChangeForm,
            template_name='core/password_change.html',
            success_url=reverse_lazy('profile'),
        ),
        name='password_change',
    ),
    path(
        'account/password/reset/',
        auth_views.PasswordResetView.as_view(
            form_class=AccountPasswordResetForm,
            template_name='core/password_reset_form.html',
            email_template_name='core/password_reset_email.txt',
            html_email_template_name='core/password_reset_email.html',
            subject_template_name='core/password_reset_subject.txt',
            success_url=reverse_lazy('password_reset_done'),
        ),
        name='password_reset',
    ),
    path(
        'account/password/reset/sent/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='core/password_reset_done.html',
        ),
        name='password_reset_done',
    ),
    path(
        'account/password/reset/confirm/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            form_class=AccountSetPasswordForm,
            template_name='core/password_reset_confirm.html',
            success_url=reverse_lazy('password_reset_complete'),
        ),
        name='password_reset_confirm',
    ),
    path(
        'account/password/reset/complete/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='core/password_reset_complete.html',
        ),
        name='password_reset_complete',
    ),
    path('', include('core.urls')),
    path('', RedirectView.as_view(pattern_name='home', permanent=False)),
]
