import hashlib
import hmac
import secrets
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
from django.db import models, transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone
from .constants import MAXIMUM_DEPOSIT_AMOUNT, MINIMUM_MOBILE_MONEY_AMOUNT
from .models import AuditLog, FinancialReport, GameRound, GameSession, PaymentRequest, Profile, RiskAlert, SelfExclusion


HOUSE_EDGE = Decimal('0.0100')
RTP_FACTOR = Decimal('1.0000') - HOUSE_EDGE
MAX_CRASH_POINT = Decimal('100000.00')
URA_GGR_TAX_RATE = Decimal('0.15')


def log_audit(user, action_type, description, request=None, data=None):
    """
    Log an audit event.
    """
    ip_address = None
    user_agent = ''

    if request:
        ip_address = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]

    AuditLog.objects.create(
        user=user,
        action_type=action_type,
        description=description,
        ip_address=ip_address,
        user_agent=user_agent,
        data=data or {},
    )


def get_client_ip(request):
    """
    Get client's real IP address from request.
    """
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def generate_server_seed():
    return secrets.token_hex(32)


def hash_server_seed(server_seed):
    return hashlib.sha256(server_seed.encode('utf-8')).hexdigest()


def build_client_seed(user, request=None):
    profile = getattr(user, 'profile', None) if user else None
    if profile and profile.client_seed:
        return profile.client_seed
    ip = get_client_ip(request) if request else 'local'
    user_identity = f'{user.id}:{user.username}' if user else 'guest'
    raw = f'{user_identity}:{timezone.now().timestamp()}:{ip}:{secrets.token_hex(8)}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def calculate_crash_point(server_seed, client_seed, house_edge=HOUSE_EDGE):
    digest = hmac.new(
        server_seed.encode('utf-8'),
        client_seed.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    random_int = int(digest[:13], 16)
    uniform = Decimal(random_int) / Decimal(16 ** 13)

    if uniform <= 0:
        return Decimal('1.00')

    rtp_factor = Decimal('1.0000') - Decimal(house_edge)
    crash = (rtp_factor / (Decimal('1.0000') - uniform)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    return min(max(crash, Decimal('1.00')), MAX_CRASH_POINT)


def create_game_round(user, request=None):
    server_seed = generate_server_seed()
    client_seed = build_client_seed(user, request)
    seed_hash = hash_server_seed(server_seed)
    crash_point = calculate_crash_point(server_seed, client_seed)
    public_id = secrets.token_urlsafe(12).replace('-', '').replace('_', '')[:18].upper()

    while GameRound.objects.filter(public_id=public_id).exists():
        public_id = secrets.token_urlsafe(12).replace('-', '').replace('_', '')[:18].upper()

    return GameRound.objects.create(
        public_id=public_id,
        created_by=user,
        server_seed_hash=seed_hash,
        server_seed_private=server_seed,
        client_seed=client_seed,
        crash_point=crash_point,
        house_edge=HOUSE_EDGE,
    )


def reveal_game_round(round_obj, server_seed=None):
    server_seed = server_seed or round_obj.server_seed_private
    if not round_obj.server_seed:
        round_obj.server_seed = server_seed
    round_obj.status = 'crashed'
    round_obj.crashed_at = round_obj.crashed_at or timezone.now()
    round_obj.revealed_at = round_obj.revealed_at or timezone.now()
    round_obj.save(update_fields=['server_seed', 'status', 'crashed_at', 'revealed_at'])
    return round_obj


def verify_game_round(server_seed, client_seed, expected_hash, expected_crash_point, house_edge=HOUSE_EDGE):
    if hash_server_seed(server_seed) != expected_hash:
        return False
    return calculate_crash_point(
        server_seed,
        client_seed,
        house_edge,
    ) == Decimal(str(expected_crash_point)).quantize(Decimal('0.01'))


def generate_payment_reference(prefix):
    return f'{prefix}-{timezone.now():%Y%m%d%H%M%S}-{secrets.token_hex(4).upper()}'


def get_daily_deposit_total(user):
    now = timezone.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = PaymentRequest.objects.filter(
        user=user,
        payment_type='deposit',
    ).filter(
        models.Q(status='completed', completed_at__gte=day_start)
        | models.Q(status__in=['pending', 'processing'], created_at__gte=day_start)
    ).aggregate(total=Sum('amount'))['total']
    return total or Decimal('0.00')


def can_deposit(profile, amount):
    if amount < MINIMUM_MOBILE_MONEY_AMOUNT:
        return False, 'Deposits must be at least 500 UGX.'
    if amount > MAXIMUM_DEPOSIT_AMOUNT:
        return False, 'Deposits cannot exceed 300,000 UGX per transaction.'
    if is_self_excluded(profile.user):
        return False, 'Your account is currently self-excluded.'

    daily_total = get_daily_deposit_total(profile.user)
    daily_limit = min(profile.daily_deposit_limit, MAXIMUM_DEPOSIT_AMOUNT)
    if daily_total + amount > daily_limit:
        return False, f'Daily deposit limit exceeded. Remaining today: {daily_limit - daily_total:.2f} {profile.currency}.'

    return True, ''


def get_daily_net_loss(user):
    now = timezone.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    totals = GameSession.objects.filter(
        user=user,
        created_at__gte=day_start,
    ).aggregate(
        stakes=Sum('stake'),
        wins=Sum('won_amount'),
    )
    stakes = totals['stakes'] or Decimal('0.00')
    wins = totals['wins'] or Decimal('0.00')
    return max(stakes - wins, Decimal('0.00'))


def can_place_bets(profile, stake_total):
    if is_self_excluded(profile.user):
        return False, 'Your account is currently self-excluded.'
    daily_loss = get_daily_net_loss(profile.user)
    if daily_loss + stake_total > profile.daily_loss_limit:
        return False, f'Daily loss limit exceeded. Remaining risk today: {profile.daily_loss_limit - daily_loss:.2f} {profile.currency}.'
    return True, ''


def is_self_excluded(user):
    now = timezone.now()
    return SelfExclusion.objects.filter(user=user).filter(
        models.Q(starts_at__lte=now) & (models.Q(permanent=True) | models.Q(ends_at__gt=now))
    ).exists()


def get_active_self_exclusion(user):
    now = timezone.now()
    return SelfExclusion.objects.filter(
        user=user,
        starts_at__lte=now,
    ).filter(
        models.Q(permanent=True) | models.Q(ends_at__gt=now)
    ).order_by('-created_at').first()


def activate_self_exclusion(user, duration, reason=''):
    permanent = duration == 'permanent'
    ends_at = None if permanent else timezone.now() + timedelta(hours=int(duration))

    with db_transaction.atomic():
        Profile.objects.select_for_update().get(user=user)
        active_exclusion = get_active_self_exclusion(user)
        if active_exclusion:
            return None
        return SelfExclusion.objects.create(
            user=user,
            ends_at=ends_at,
            permanent=permanent,
            reason=reason,
        )


def create_risk_alert(user, alert_type, severity, description, data=None):
    existing = RiskAlert.objects.filter(
        user=user,
        alert_type=alert_type,
        status__in=['open', 'reviewing'],
    ).first()
    if existing:
        return existing
    return RiskAlert.objects.create(
        user=user,
        alert_type=alert_type,
        severity=severity,
        description=description,
        data=data or {},
    )


def run_player_risk_checks(user):
    alerts = []
    profile = getattr(user, 'profile', None)
    if not profile:
        return alerts

    daily_total = get_daily_deposit_total(user)
    daily_deposit_limit = min(profile.daily_deposit_limit, MAXIMUM_DEPOSIT_AMOUNT)
    if daily_deposit_limit and daily_total >= daily_deposit_limit * Decimal('0.80'):
        alerts.append(create_risk_alert(
            user,
            'deposit_limit_pressure',
            'medium',
            'Player has used at least 80% of the configured daily deposit limit.',
            {'daily_total': str(daily_total), 'daily_deposit_limit': str(daily_deposit_limit)},
        ))

    daily_loss = get_daily_net_loss(user)
    if profile.daily_loss_limit and daily_loss >= profile.daily_loss_limit * Decimal('0.80'):
        alerts.append(create_risk_alert(
            user,
            'loss_limit_pressure',
            'high',
            'Player has used at least 80% of the configured daily loss limit.',
            {'daily_loss': str(daily_loss), 'daily_loss_limit': str(profile.daily_loss_limit)},
        ))

    return alerts


def generate_financial_report(report_type, period_start, period_end, generated_by=None):
    end_exclusive = period_end + timedelta(days=1)
    payments = PaymentRequest.objects.filter(completed_at__date__gte=period_start, completed_at__date__lt=end_exclusive)
    sessions = GameSession.objects.filter(created_at__date__gte=period_start, created_at__date__lt=end_exclusive)

    total_deposits = payments.filter(payment_type='deposit', status='completed').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_withdrawals = payments.filter(payment_type='withdraw', status='completed').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_bets = sessions.aggregate(total=Sum('stake'))['total'] or Decimal('0.00')
    total_wins = sessions.aggregate(total=Sum('won_amount'))['total'] or Decimal('0.00')
    ggr = max(total_bets - total_wins, Decimal('0.00'))
    ura_tax = (ggr * URA_GGR_TAX_RATE).quantize(Decimal('0.01'))

    report, _ = FinancialReport.objects.update_or_create(
        report_type=report_type,
        period_start=period_start,
        period_end=period_end,
        defaults={
            'total_deposits': total_deposits,
            'total_withdrawals': total_withdrawals,
            'total_bets': total_bets,
            'total_wins': total_wins,
            'gross_gaming_revenue': ggr,
            'ura_ggr_tax': ura_tax,
            'generated_by': generated_by,
        },
    )
    return report
