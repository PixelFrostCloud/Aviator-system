from .models import PlatformSettings


def platform_branding(request):
    settings = PlatformSettings.objects.filter(pk=1).first()
    return {
        'platform_branding': settings,
        'platform_name': settings.site_name if settings else 'Pakabet',
        'platform_tagline': settings.site_tagline if settings else '',
    }
