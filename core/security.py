import ipaddress
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.signals import user_login_failed, user_logged_in
from django.core.mail import mail_admins
from django.dispatch import receiver
from django.http import HttpResponse
from django.utils import timezone

from .abuse import login_account_key, login_attempts_exceeded, record_activity
from .models import AuditLog


logger = logging.getLogger('core.security')
_configuration_check_complete = False


def _client_ip(request):
    if request is None:
        return None
    value = request.META.get('REMOTE_ADDR', '')
    try:
        return str(ipaddress.ip_address(value))
    except (TypeError, ValueError):
        return None


def _record_event(event, request=None, details=None):
    user = getattr(request, 'user', None)
    if not getattr(user, 'is_authenticated', False):
        user = None
    return AuditLog.objects.create(
        user=user,
        action_type='risk_alert',
        description=f'Security event: {event}',
        ip_address=_client_ip(request),
        data={'event': event, **(details or {})},
    )


def _notify_admins(subject, message):
    if not settings.ADMINS:
        logger.error('Security alert could not be emailed because DJANGO_ADMINS is not configured.')
        return
    try:
        mail_admins(subject, message, fail_silently=False)
    except Exception:
        logger.exception('Unable to deliver a security alert email.')


@receiver(user_login_failed)
def log_failed_login(sender, credentials, request=None, **kwargs):
    try:
        # Never inspect or persist credentials; Django supplies the submitted password here.
        event = _record_event('login_failed', request, {
            'account_key': login_account_key(credentials.get('username')),
        })
        if event.ip_address:
            now = timezone.now()
            window_start = now - timedelta(seconds=settings.SECURITY_LOGIN_FAILURE_WINDOW_SECONDS)
            recent_failures = AuditLog.objects.filter(
                action_type='risk_alert',
                ip_address=event.ip_address,
                created_at__gte=window_start,
                data__event='login_failed',
            ).count()
            if recent_failures >= settings.SECURITY_LOGIN_FAILURE_THRESHOLD:
                cooldown_start = now - timedelta(seconds=settings.SECURITY_ALERT_COOLDOWN_SECONDS)
                already_alerted = AuditLog.objects.filter(
                    action_type='risk_alert',
                    ip_address=event.ip_address,
                    created_at__gte=cooldown_start,
                    data__event='login_failure_alert',
                ).exists()
                if not already_alerted:
                    _record_event('login_failure_alert', request, {
                        'failure_count': recent_failures,
                        'window_seconds': settings.SECURITY_LOGIN_FAILURE_WINDOW_SECONDS,
                    })
                    _notify_admins(
                        'Pakabet security alert: repeated failed logins',
                        (
                            f'{recent_failures} failed login attempts were recorded from '
                            f'{event.ip_address} within the last '
                            f'{settings.SECURITY_LOGIN_FAILURE_WINDOW_SECONDS} seconds. '
                            'Review the restricted security audit log and take appropriate action.'
                        ),
                    )
    except Exception:
        logger.exception('Unable to record a failed authentication event.')


@receiver(user_logged_in)
def log_successful_login(sender, request, user, **kwargs):
    try:
        _record_event('login_succeeded', request)
    except Exception:
        logger.exception('Unable to record a successful authentication event.')


def _security_configuration():
    return {
        'debug': settings.DEBUG,
        'allowed_hosts': sorted(settings.ALLOWED_HOSTS),
        'ssl_redirect': settings.SECURE_SSL_REDIRECT,
        'proxy_ssl_header': list(settings.SECURE_PROXY_SSL_HEADER or ()),
        'csrf_trusted_origins': sorted(settings.CSRF_TRUSTED_ORIGINS),
        'csrf_cookie_secure': settings.CSRF_COOKIE_SECURE,
        'csrf_cookie_httponly': settings.CSRF_COOKIE_HTTPONLY,
        'csrf_cookie_samesite': settings.CSRF_COOKIE_SAMESITE,
        'session_cookie_secure': settings.SESSION_COOKIE_SECURE,
        'session_cookie_httponly': settings.SESSION_COOKIE_HTTPONLY,
        'session_cookie_samesite': settings.SESSION_COOKIE_SAMESITE,
        'hsts_seconds': settings.SECURE_HSTS_SECONDS,
        'hsts_include_subdomains': settings.SECURE_HSTS_INCLUDE_SUBDOMAINS,
        'hsts_preload': settings.SECURE_HSTS_PRELOAD,
        'content_type_nosniff': settings.SECURE_CONTENT_TYPE_NOSNIFF,
        'frame_options': settings.X_FRAME_OPTIONS,
        'payment_mode': settings.IOTEC_PAY_MODE,
        'payment_client_id_configured': bool(settings.IOTEC_PAY_CLIENT_ID),
        'payment_client_secret_configured': bool(settings.IOTEC_PAY_CLIENT_SECRET),
        'payment_wallet_id_configured': bool(settings.IOTEC_PAY_WALLET_ID),
        'payment_callback_token_configured': bool(settings.IOTEC_PAY_CALLBACK_TOKEN),
        'email_backend': settings.EMAIL_BACKEND,
        'email_host_password_configured': bool(settings.EMAIL_HOST_PASSWORD),
        'admin_recipient_count': len(settings.ADMINS),
        'login_failure_threshold': settings.SECURITY_LOGIN_FAILURE_THRESHOLD,
        'login_failure_window_seconds': settings.SECURITY_LOGIN_FAILURE_WINDOW_SECONDS,
        'login_account_limit': settings.SECURITY_LOGIN_ACCOUNT_LIMIT,
        'login_ip_limit': settings.SECURITY_LOGIN_IP_LIMIT,
        'login_rate_window_seconds': settings.SECURITY_LOGIN_RATE_WINDOW_SECONDS,
        'bet_account_limit': settings.SECURITY_BET_ACCOUNT_LIMIT,
        'bet_ip_limit': settings.SECURITY_BET_IP_LIMIT,
        'cashout_account_limit': settings.SECURITY_CASHOUT_ACCOUNT_LIMIT,
        'cashout_ip_limit': settings.SECURITY_CASHOUT_IP_LIMIT,
        'game_rate_window_seconds': settings.SECURITY_GAME_RATE_WINDOW_SECONDS,
        'register_ip_limit': settings.SECURITY_REGISTER_IP_LIMIT,
        'register_window_seconds': settings.SECURITY_REGISTER_WINDOW_SECONDS,
        'payment_account_limit': settings.SECURITY_PAYMENT_ACCOUNT_LIMIT,
        'payment_ip_limit': settings.SECURITY_PAYMENT_IP_LIMIT,
        'payment_window_seconds': settings.SECURITY_PAYMENT_WINDOW_SECONDS,
        'room_create_account_limit': settings.SECURITY_ROOM_CREATE_ACCOUNT_LIMIT,
        'room_create_ip_limit': settings.SECURITY_ROOM_CREATE_IP_LIMIT,
        'room_create_window_seconds': settings.SECURITY_ROOM_CREATE_WINDOW_SECONDS,
    }


def check_security_configuration():
    global _configuration_check_complete
    if _configuration_check_complete:
        return

    try:
        current = _security_configuration()
        previous_event = AuditLog.objects.filter(
            action_type='risk_alert',
            data__event='configuration_snapshot',
        ).order_by('-created_at').first()
        if previous_event is None:
            _record_event('configuration_snapshot', details={'configuration': current})
        else:
            previous = previous_event.data.get('configuration', {})
            changes = {
                key: {'previous': previous.get(key), 'current': value}
                for key, value in current.items()
                if previous.get(key) != value
            }
            if changes:
                _record_event('configuration_snapshot', details={
                    'configuration': current,
                    'changed_settings': changes,
                })
                changed_names = ', '.join(sorted(changes))
                _notify_admins(
                    'Pakabet security alert: security configuration changed',
                    (
                        f'Security-sensitive settings changed: {changed_names}. '
                        'Review the restricted security audit log and confirm the change was authorized.'
                    ),
                )
        _configuration_check_complete = True
    except Exception:
        logger.exception('Unable to check or record the security configuration snapshot.')
        _configuration_check_complete = True


class SecurityEventMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        check_security_configuration()
        if request.path_info == '/login/' and request.method == 'POST':
            try:
                if login_attempts_exceeded(request):
                    record_activity('login_rate_limited', request)
                    return HttpResponse(
                        'Too many sign-in attempts. Please wait before trying again.',
                        status=429,
                    )
            except Exception:
                logger.exception('Unable to check the authentication rate limit.')
        response = self.get_response(request)
        if response.status_code == 403:
            route = getattr(getattr(request, 'resolver_match', None), 'route', '<unresolved>')
            try:
                _record_event('permission_denied', request, {
                    'route': route,
                    'method': request.method,
                })
            except Exception:
                logger.exception('Unable to record a permission-denied event.')
        return response
