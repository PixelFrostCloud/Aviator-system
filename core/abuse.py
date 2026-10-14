import ipaddress
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import AuditLog


def connection_ip(request):
    try:
        return str(ipaddress.ip_address(request.META.get('REMOTE_ADDR', '')))
    except (AttributeError, TypeError, ValueError):
        return None


def record_activity(event, request=None, *, user=None, details=None, ip_address=None):
    if user is None and request is not None:
        candidate = getattr(request, 'user', None)
        if getattr(candidate, 'is_authenticated', False):
            user = candidate
    return AuditLog.objects.create(
        user=user,
        action_type='risk_alert',
        description=f'Security event: {event}',
        ip_address=connection_ip(request) if request else ip_address,
        data={'event': event, **(details or {})},
    )


def enforce_rate_limit(request, action, *, account_limit, ip_limit, window_seconds):
    now = timezone.now()
    window_start = now - timedelta(seconds=window_seconds)
    ip_address = connection_ip(request)
    user = getattr(request, 'user', None)
    user_id = user.pk if getattr(user, 'is_authenticated', False) else None
    event = f'{action}_attempt'
    attempts = AuditLog.objects.filter(
        action_type='risk_alert',
        data__event=event,
        created_at__gte=window_start,
    )
    scopes = []
    if user_id is not None:
        account_attempts = attempts.filter(user_id=user_id).count()
        if account_attempts >= account_limit:
            scopes.append('account')
    if ip_address is not None:
        ip_attempts = attempts.filter(ip_address=ip_address).count()
        if ip_attempts >= ip_limit:
            scopes.append('ip')

    record_activity(
        'rate_limit_exceeded' if scopes else event,
        request,
        details={
            'action': action,
            'scopes': scopes,
            'window_seconds': window_seconds,
        } if scopes else {'action': action},
    )
    return not scopes, scopes


def authorize_room_socket(room_id, user, ip_address):
    from .models import Room, RoomPlayer

    room_exists = Room.objects.filter(pk=room_id).exists()
    if not room_exists or not getattr(user, 'is_authenticated', False):
        return False
    authorized = (
        Room.objects.filter(pk=room_id, host_id=user.pk).exists()
        or RoomPlayer.objects.filter(room_id=room_id, user_id=user.pk).exists()
    )
    if not authorized:
        record_activity(
            'unauthorized_room_socket',
            user=user,
            ip_address=ip_address,
            details={'room_id': room_id},
        )
        return False

    now = timezone.now()
    recent_connects = AuditLog.objects.filter(
        action_type='risk_alert',
        data__event='room_socket_connected',
        created_at__gte=now - timedelta(minutes=5),
    )
    account_count = recent_connects.filter(user_id=user.pk).count()
    ip_count = recent_connects.filter(ip_address=ip_address).count() if ip_address else 0
    if account_count >= 20 or (ip_address and ip_count >= 60):
        record_activity(
            'room_socket_reconnect_limit',
            user=user,
            ip_address=ip_address,
            details={
                'account_connects': account_count,
                'ip_connects': ip_count if ip_address else None,
                'window_seconds': 300,
            },
        )
        return False

    record_activity(
        'room_socket_connected',
        user=user,
        ip_address=ip_address,
        details={'room_id': room_id},
    )
    return True


def detect_account_creation_pattern(request, user):
    ip_address = connection_ip(request)
    if not ip_address:
        return
    start = timezone.now() - timedelta(hours=1)
    count = AuditLog.objects.filter(
        action_type='risk_alert',
        ip_address=ip_address,
        data__event='account_created',
        created_at__gte=start,
    ).count()
    record_activity('account_created', request, user=user)
    if count >= 3:
        record_activity('unusual_account_creation_volume', request, user=user, details={
            'accounts_from_ip': count + 1,
            'window_seconds': 3600,
        })


def detect_reward_claim_pattern(request, user):
    from .models import RewardClaim

    ip_address = connection_ip(request)
    if not ip_address:
        return
    window_start = timezone.now() - timedelta(hours=24)
    shared_ip_users = RewardClaim.objects.filter(
        ip_address=ip_address,
        claim_time__gte=window_start,
    ).values('user_id').distinct().count()
    user_agent = (request.META.get('HTTP_USER_AGENT') or '')[:160]
    same_device_users = RewardClaim.objects.filter(
        device_identifier=user_agent,
        claim_time__gte=window_start,
    ).exclude(device_identifier='').values('user_id').distinct().count()
    if shared_ip_users >= 3 or same_device_users >= 3:
        record_activity('shared_reward_claim_identity', request, user=user, details={
            'distinct_ip_accounts': shared_ip_users,
            'distinct_device_accounts': same_device_users,
            'window_seconds': 86400,
        })


def detect_payment_pattern(request, user):
    ip_address = connection_ip(request)
    start = timezone.now() - timedelta(hours=1)
    recent_actions = AuditLog.objects.filter(
        action_type='risk_alert',
        user=user,
        data__event='payment_request_observed',
        created_at__gte=start,
    ).count()
    recent_ip_actions = (
        AuditLog.objects.filter(
            action_type='risk_alert',
            ip_address=ip_address,
            data__event='payment_request_observed',
            created_at__gte=start,
        ).count()
        if ip_address
        else 0
    )
    record_activity('payment_request_observed', request, user=user)
    if recent_actions >= 8 or recent_ip_actions >= 20:
        record_activity('unusual_payment_request_volume', request, user=user, details={
            'account_requests': recent_actions,
            'ip_requests': recent_ip_actions if ip_address else None,
            'window_seconds': 3600,
        })


def login_account_key(username):
    import hashlib
    import hmac

    normalized = str(username or '').strip().casefold()
    if not normalized:
        return ''
    return hmac.new(
        settings.SECRET_KEY.encode(),
        normalized.encode(),
        hashlib.sha256,
    ).hexdigest()


def login_attempts_exceeded(request):
    from .models import AuditLog

    now = timezone.now()
    window_start = now - timedelta(seconds=settings.SECURITY_LOGIN_RATE_WINDOW_SECONDS)
    failures = AuditLog.objects.filter(
        action_type='risk_alert',
        data__event='login_failed',
        created_at__gte=window_start,
    )
    ip_address = connection_ip(request)
    username = request.POST.get('username', '')
    key = login_account_key(username)
    ip_exceeded = bool(ip_address) and failures.filter(ip_address=ip_address).count() >= settings.SECURITY_LOGIN_IP_LIMIT
    account_exceeded = bool(key) and failures.filter(data__account_key=key).count() >= settings.SECURITY_LOGIN_ACCOUNT_LIMIT
    return ip_exceeded or account_exceeded


def authorize_reward_socket(user, ip_address):
    if not getattr(user, 'is_authenticated', False):
        return False
    window_start = timezone.now() - timedelta(minutes=5)
    recent_connects = AuditLog.objects.filter(
        action_type='risk_alert',
        data__event='reward_socket_connected',
        created_at__gte=window_start,
    )
    account_count = recent_connects.filter(user_id=user.pk).count()
    ip_count = recent_connects.filter(ip_address=ip_address).count() if ip_address else 0
    if account_count >= 20 or (ip_address and ip_count >= 60):
        record_activity(
            'reward_socket_reconnect_limit',
            user=user,
            ip_address=ip_address,
            details={
                'account_connects': account_count,
                'ip_connects': ip_count if ip_address else None,
                'window_seconds': 300,
            },
        )
        return False
    record_activity('reward_socket_connected', user=user, ip_address=ip_address)
    return True
