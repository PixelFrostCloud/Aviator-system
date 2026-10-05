from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include, reverse_lazy
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    path(
        'account/password/',
        auth_views.PasswordChangeView.as_view(
            template_name='core/password_change.html',
            success_url=reverse_lazy('profile'),
        ),
        name='password_change',
    ),
    path('', include('core.urls')),
    path('', RedirectView.as_view(pattern_name='home', permanent=False)),
]
