import json
import random
import re
import uuid
from hmac import compare_digest
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.core.paginator import Paginator
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core import signing
from django.db import transaction as db_transaction
from django.db.models import Count, F, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from django.utils import timezone
from .forms import (
    RegisterForm,
    PlayForm,
    WalletForm,
    ProfileComplianceForm,
    SelfExclusionForm,
    ChatMessageForm,
    FinancialReportForm,
    RoomCreateForm,
    RoomJoinForm,
    AccountPasswordChangeForm,
)
from .chat_rules import get_chat_rule_violation
from .abuse import (
    detect_account_creation_pattern,
    detect_payment_pattern,
    detect_reward_claim_pattern,
    enforce_rate_limit,
    record_activity,
)
from .models import (
    Profile,
    Referral,
    ChatMessage,
    ChatReaction,
    ChatRain,
    ChatRainRecipient,
    FinancialReport,
    GameSession,
    GameRound,
    PaymentRequest,
    RiskAlert,
    SelfExclusion,
    Transaction,
    Room,
    RoomPlayer,
    SharedGameState,
    RewardClaim,
    RewardEvent,
    RewardTransaction,
    RewardWallet,
)
from .chat_services import prune_chat_messages_if_needed
from .reward_services import (
    RewardError,
    activate_event,
    active_participant_count,
    claim_reward,
    ensure_reward_wallet,
    event_payload,
    reward_statistics,
    record_game_presence,
    remove_game_presence,
    send_chat_rain,
    stop_event,
    wallet_payload,
)
from .payment_gateway import PaymentGatewayError, PaymentGatewayResult
from .constants import MAXIMUM_DEPOSIT_AMOUNT
from .payment_services import apply_provider_result, initiate_deposit, initiate_withdrawal, sync_payment
from .utils import (
    activate_self_exclusion,
    can_deposit,
    can_place_bets,
    create_game_round,
    generate_financial_report,
    generate_payment_reference,
    get_active_self_exclusion,
    get_daily_deposit_total,
    is_self_excluded,
    log_audit,
    reveal_game_round,
    run_player_risk_checks,
    verify_game_round,
)


ROUND_MULTIPLIER_RATE = Decimal('0.10')
ROUND_BETTING_SECONDS = 4


def multiplier_at_elapsed_time(started_at, now=None):
    """Return the authoritative multiplier from elapsed wall-clock time."""
    elapsed = max(((now or timezone.now()) - started_at).total_seconds(), 0)
    return (Decimal('1.00') + (Decimal(str(elapsed)) * ROUND_MULTIPLIER_RATE)).quantize(Decimal('0.01'))


def _start_shared_betting_round(game_state, user, request):
    game_round = create_game_round(user if user.is_authenticated else None, request)
    game_round.status = 'betting'
    game_round.betting_ends_at = timezone.now() + timedelta(seconds=ROUND_BETTING_SECONDS)
    game_round.started_at = game_round.betting_ends_at
    game_round.save(update_fields=['status', 'betting_ends_at', 'started_at'])
    game_state.active_round = game_round
    game_state.save(update_fields=['active_round'])
    return game_round


def _settle_due_auto_cashouts(game_round, current_multiplier):
    eligible_sessions = list(
        GameSession.objects.filter(
            round=game_round,
            cashed_out_at__isnull=True,
            auto_cashout_at__lte=current_multiplier,
            auto_cashout_at__lt=game_round.crash_point,
        ).order_by('id')[:100]
    )
    if not eligible_sessions:
        return

    for session in eligible_sessions:
        target = session.auto_cashout_at
        payout = (session.stake * target).quantize(Decimal('0.01'))
        with db_transaction.atomic():
            settled = GameSession.objects.filter(
                pk=session.pk,
                cashed_out_at__isnull=True,
            ).update(cashed_out_at=target, won_amount=payout)
            if not settled:
                continue

            if payout > 0:
                profile_updated = Profile.objects.filter(user_id=session.user_id).update(
                    balance=F('balance') + payout,
                )
                if not profile_updated:
                    raise RuntimeError(
                        f'Cannot settle auto cashout for user {session.user_id}: profile is missing.'
                    )
                GameRound.objects.filter(pk=game_round.pk).update(
                    winning_bet_count=F('winning_bet_count') + 1,
                )
                Transaction.objects.create(
                    user_id=session.user_id,
                    transaction_type='win',
                    amount=payout,
                    status='completed',
                    reference=game_round.public_id,
                    description=f'Round {game_round.public_id} auto cashout',
                )


def _shared_round_payload(user, request=None):
    game_state = SharedGameState.objects.select_related('active_round').get(pk=1)
    game_round = game_state.active_round

    if not game_round or game_round.status in {'crashed', 'void'}:
        with db_transaction.atomic():
            SharedGameState.objects.filter(pk=1).update(active_round=F('active_round'))
            game_state = SharedGameState.objects.select_for_update().select_related('active_round').get(pk=1)
            game_round = game_state.active_round
            if not game_round or game_round.status in {'crashed', 'void'}:
                game_round = _start_shared_betting_round(game_state, user, request)

    if game_round.status == 'betting':
        if game_round.betting_ends_at is None:
            raise RuntimeError(f'Betting round {game_round.public_id} has no closing time.')
        now = timezone.now()
        if now >= game_round.betting_ends_at:
            GameRound.objects.filter(
                pk=game_round.pk,
                status='betting',
                betting_ends_at__lte=now,
            ).update(
                status='flying',
                started_at=F('betting_ends_at'),
            )
            game_round.refresh_from_db()
        else:
            remaining = max((game_round.betting_ends_at - now).total_seconds(), 0)
            sessions = list(
                GameSession.objects.filter(user=user, round=game_round).order_by('created_at', 'id')
            ) if user.is_authenticated else []
            active_sessions = [session for session in sessions if session.cashed_out_at is None]
            return {
                'success': True,
                'round_id': game_round.public_id,
                'status': 'betting',
                'seconds_remaining': remaining,
                'betting_ends_at': game_round.betting_ends_at.isoformat(),
                'server_time': now.isoformat(),
                'server_seed_hash': game_round.server_seed_hash,
                'session_ids': [session.id for session in active_sessions],
                'sessions': [
                    {
                        'id': session.id,
                        'stake': float(session.stake),
                        'auto_cashout_at': float(session.auto_cashout_at) if session.auto_cashout_at is not None else None,
                        'bet_request_id': session.bet_request_id,
                    }
                    for session in sessions
                ],
            }

    if game_round.status != 'flying':
        return {'success': True, 'status': 'waiting'}

    server_now = timezone.now()
    current_multiplier = multiplier_at_elapsed_time(game_round.started_at, server_now)
    _settle_due_auto_cashouts(game_round, current_multiplier)
    if current_multiplier >= game_round.crash_point:
        reveal_game_round(game_round)
        game_round.refresh_from_db()

    sessions = list(
        GameSession.objects.filter(
            user=user,
            round=game_round,
        ).order_by('created_at', 'id')
    ) if user.is_authenticated else []
    active_sessions = [session for session in sessions if session.cashed_out_at is None]
    payload = {
        'success': True,
        'round_id': game_round.public_id,
        'status': game_round.status,
        'current_multiplier': float(min(current_multiplier, game_round.crash_point)),
        'server_time': server_now.isoformat(),
        'server_seed_hash': game_round.server_seed_hash,
        'session_ids': [session.id for session in active_sessions],
        'sessions': [
            {
                'id': session.id,
                'stake': float(session.stake),
                'auto_cashout_at': float(session.auto_cashout_at) if session.auto_cashout_at is not None else None,
                'cashed_out_at': float(session.cashed_out_at) if session.cashed_out_at is not None else None,
                'won_amount': float(session.won_amount),
                'bet_request_id': session.bet_request_id,
            }
            for session in sessions
        ],
        'flying_since': game_round.started_at.isoformat(),
    }
    if game_round.status == 'crashed':
        payload.update({
            'crash_point': float(game_round.crash_point),
            'server_seed': game_round.server_seed,
            'client_seed': game_round.client_seed,
        })
    return payload


def _round_ledger_payload(user, tab, cursor_data):
    page_size = 50
    if tab == 'mine':
        return _my_bets_ledger_payload(user, cursor_data, page_size)

    game_state = SharedGameState.objects.select_related('active_round').get(pk=1)
    game_round = game_state.active_round
    if not game_round:
        return {
            'success': True,
            'round_id': None,
            'total_count': 0,
            'tab_count': 0,
            'rows': [],
            'has_previous': False,
            'has_next': False,
            'previous_cursor': None,
            'next_cursor': None,
        }

    bets = GameSession.objects.filter(round=game_round)
    if tab == 'mine':
        bets = bets.filter(user=user) if user.is_authenticated else bets.none()
        ordering = ('-stake', 'id')
    elif tab == 'top':
        bets = bets.filter(cashed_out_at__isnull=False, won_amount__gt=0)
        ordering = ('-won_amount', '-stake', 'id')
    else:
        ordering = ('-stake', 'id')

    total_count = game_round.bet_count
    tab_count = (
        total_count
        if tab == 'all'
        else game_round.winning_bet_count
        if tab == 'top'
        else bets.count()
    )
    page_ordering = ordering
    direction = None
    if cursor_data:
        if cursor_data.get('round_id') != game_round.public_id or cursor_data.get('tab') != tab:
            return {
                'success': False,
                'message': 'This bet-list page is no longer available.',
            }
        direction = cursor_data['direction']
        cursor_stake = cursor_data['stake']
        cursor_id = cursor_data['id']
        if tab == 'top':
            cursor_won = cursor_data['won']
            if direction == 'next':
                bets = bets.filter(
                    Q(won_amount__lt=cursor_won)
                    | Q(won_amount=cursor_won) & (
                        Q(stake__lt=cursor_stake)
                        | Q(stake=cursor_stake, id__gt=cursor_id)
                    )
                )
            else:
                bets = bets.filter(
                    Q(won_amount__gt=cursor_won)
                    | Q(won_amount=cursor_won) & (
                        Q(stake__gt=cursor_stake)
                        | Q(stake=cursor_stake, id__lt=cursor_id)
                    )
                )
                page_ordering = ('won_amount', 'stake', '-id')
        elif direction == 'next':
            bets = bets.filter(
                Q(stake__lt=cursor_stake)
                | Q(stake=cursor_stake, id__gt=cursor_id)
            )
        else:
            bets = bets.filter(
                Q(stake__gt=cursor_stake)
                | Q(stake=cursor_stake, id__lt=cursor_id)
            )
            page_ordering = ('stake', '-id')

    selected = list(
        bets.order_by(*page_ordering)
        .values(
            'id',
            'user__username',
            'stake',
            'cashed_out_at',
            'won_amount',
        )[:page_size + 1]
    )
    has_more_in_direction = len(selected) > page_size
    selected = selected[:page_size]
    if direction == 'previous':
        selected.reverse()

    has_previous = direction == 'next' or (
        direction == 'previous' and has_more_in_direction
    )
    has_next = (
        (direction == 'previous' and bool(selected)) or has_more_in_direction
    )

    def make_cursor(row, cursor_direction):
        if not row:
            return None
        return signing.dumps({
            'round_id': game_round.public_id,
            'tab': tab,
            'direction': cursor_direction,
            'stake': str(row['stake']),
            'won': str(row['won_amount']),
            'id': row['id'],
        }, salt='live-round-ledger')

    rows = [
        {
            'user': row['user__username'],
            'avatar': row['user__username'][:1].upper(),
            'bet': float(row['stake']),
            'multiplier': (
                float(row['cashed_out_at'])
                if row['cashed_out_at'] is not None
                else float(game_round.crash_point)
                if game_round.status == 'crashed'
                else None
            ),
            'win': float(row['won_amount']),
            'status': (
                'lost' if row['cashed_out_at'] is None and game_round.status == 'crashed'
                else 'pending' if row['cashed_out_at'] is None
                else 'won' if row['won_amount'] > 0
                else 'lost'
            ),
        }
        for row in selected
    ]

    return {
        'success': True,
        'round_id': game_round.public_id,
        'total_count': total_count,
        'tab_count': tab_count,
        'rows': rows,
        'has_previous': has_previous,
        'has_next': has_next,
        'previous_cursor': make_cursor(selected[0], 'previous') if has_previous and selected else None,
        'next_cursor': make_cursor(selected[-1], 'next') if has_next and selected else None,
    }


def _my_bets_ledger_payload(user, cursor_data, page_size):
    if not user.is_authenticated:
        return {
            'success': True,
            'round_id': None,
            'total_count': 0,
            'tab_count': 0,
            'rows': [],
            'has_previous': False,
            'has_next': False,
            'previous_cursor': None,
            'next_cursor': None,
        }

    cutoff = cursor_data['cutoff'] if cursor_data else timezone.now() - timedelta(days=1)
    bets = GameSession.objects.filter(user=user, created_at__gte=cutoff)
    direction = cursor_data['direction'] if cursor_data else None
    page_ordering = ('-created_at', '-id')
    if cursor_data:
        cursor_created_at = cursor_data['created_at']
        cursor_id = cursor_data['id']
        if direction == 'next':
            bets = bets.filter(
                Q(created_at__lt=cursor_created_at)
                | Q(created_at=cursor_created_at, id__lt=cursor_id)
            )
        else:
            bets = bets.filter(
                Q(created_at__gt=cursor_created_at)
                | Q(created_at=cursor_created_at, id__gt=cursor_id)
            )
            page_ordering = ('created_at', 'id')

    total_count = GameSession.objects.filter(user=user, created_at__gte=cutoff).count()
    selected = list(
        bets.order_by(*page_ordering)
        .values(
            'id',
            'user__username',
            'stake',
            'cashed_out_at',
            'won_amount',
            'round__crash_point',
            'round__status',
            'created_at',
        )[:page_size + 1]
    )
    has_more_in_direction = len(selected) > page_size
    selected = selected[:page_size]
    if direction == 'previous':
        selected.reverse()

    has_previous = direction == 'next' or (
        direction == 'previous' and has_more_in_direction
    )
    has_next = (
        (direction == 'previous' and bool(selected)) or has_more_in_direction
    )

    def make_cursor(row, cursor_direction):
        if not row:
            return None
        return signing.dumps({
            'tab': 'mine',
            'direction': cursor_direction,
            'cutoff': cutoff.isoformat(),
            'created_at': row['created_at'].isoformat(),
            'id': row['id'],
        }, salt='live-round-ledger')

    rows = [
        {
            'user': row['user__username'],
            'avatar': row['user__username'][:1].upper(),
            'bet': float(row['stake']),
            'multiplier': (
                float(row['cashed_out_at'])
                if row['cashed_out_at'] is not None
                else float(row['round__crash_point'])
                if row['round__status'] == 'crashed'
                else None
            ),
            'win': float(row['won_amount']),
            'status': (
                'lost' if row['cashed_out_at'] is None and row['round__status'] == 'crashed'
                else 'pending' if row['cashed_out_at'] is None
                else 'won' if row['won_amount'] > 0
                else 'lost'
            ),
        }
        for row in selected
    ]

    return {
        'success': True,
        'round_id': None,
        'total_count': total_count,
        'tab_count': total_count,
        'rows': rows,
        'has_previous': has_previous,
        'has_next': has_next,
        'previous_cursor': make_cursor(selected[0], 'previous') if has_previous and selected else None,
        'next_cursor': make_cursor(selected[-1], 'next') if has_next and selected else None,
    }


def home(request):
    return render(request, 'core/home.html')


def _session_referral_profile(request, *, lock=False):
    referral_code = request.session.get('referral_code')
    if not referral_code:
        return None
    try:
        referral_code = uuid.UUID(str(referral_code))
    except (AttributeError, TypeError, ValueError):
        request.session.pop('referral_code', None)
        return None

    profiles = Profile.objects.select_related('user')
    if lock:
        profiles = profiles.select_for_update()
    return profiles.filter(referral_code=referral_code).first()


def referral_landing(request, referral_code):
    inviter_profile = get_object_or_404(
        Profile.objects.select_related('user'),
        referral_code=referral_code,
    )
    if request.user.is_authenticated:
        messages.info(request, 'Referral links are for new accounts. Share your own link with a friend.')
        return redirect('referrals')

    request.session['referral_code'] = str(inviter_profile.referral_code)
    request.session.set_expiry(60 * 60 * 24 * 30)
    return redirect('register')


def request_payload(request):
    if request.content_type == 'application/json':
        try:
            payload = json.loads(request.body.decode() or '{}')
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}
    return request.POST.dict()


def staff_required_json(request):
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Authentication required.'}, status=401)
    if not request.user.is_staff:
        return JsonResponse({'success': False, 'message': 'Administrator access required.'}, status=403)
    return None


def parse_decimal_payload(data, field, default='0'):
    try:
        return Decimal(str(data.get(field, default))).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError):
        raise RewardError(f'Invalid {field}.', 400)


def parse_datetime_payload(value, fallback=None):
    if not value:
        return fallback
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


@require_POST
def reward_create_api(request):
    denied = staff_required_json(request)
    if denied:
        return denied
    data = request_payload(request)
    try:
        total_pool = parse_decimal_payload(data, 'total_reward_pool')
        reward_per_claim = parse_decimal_payload(data, 'reward_per_claim')
        starts_at = parse_datetime_payload(data.get('starts_at'), timezone.now())
        ends_at = parse_datetime_payload(data.get('ends_at'), starts_at + timedelta(hours=1))
        event = RewardEvent.objects.create(
            title=data.get('title', '').strip() or 'Reward Event',
            description=data.get('description', '').strip(),
            total_reward_pool=total_pool,
            remaining_reward_pool=parse_decimal_payload(data, 'remaining_reward_pool', total_pool),
            maximum_claims=max(int(data.get('maximum_claims') or 1), 1),
            reward_per_claim=reward_per_claim,
            minimum_random_reward=parse_decimal_payload(data, 'minimum_random_reward', '0.01'),
            maximum_random_reward=parse_decimal_payload(data, 'maximum_random_reward', reward_per_claim),
            claim_strategy=data.get('claim_strategy') if data.get('claim_strategy') in dict(RewardEvent.CLAIM_STRATEGIES) else 'equal',
            starts_at=starts_at,
            ends_at=ends_at,
            status=data.get('status') if data.get('status') in dict(RewardEvent.STATUS_CHOICES) else ('scheduled' if starts_at > timezone.now() else 'draft'),
            created_by=request.user,
        )
    except (RewardError, ValueError) as exc:
        return JsonResponse({'success': False, 'message': getattr(exc, 'message', str(exc))}, status=getattr(exc, 'status', 400))
    return JsonResponse({'success': True, 'event': event_payload(event)}, status=201)


@require_http_methods(['PUT', 'POST'])
def reward_update_api(request):
    denied = staff_required_json(request)
    if denied:
        return denied
    data = request_payload(request)
    event = get_object_or_404(RewardEvent, uuid=data.get('id') or data.get('event_id'))
    if event.status == 'completed':
        return JsonResponse({'success': False, 'message': 'Completed events cannot be edited.'}, status=409)
    try:
        for field in ['title', 'description', 'claim_strategy', 'status']:
            if field in data:
                setattr(event, field, data[field])
        for field in ['total_reward_pool', 'remaining_reward_pool', 'reward_per_claim', 'minimum_random_reward', 'maximum_random_reward']:
            if field in data:
                setattr(event, field, parse_decimal_payload(data, field))
        if 'maximum_claims' in data:
            event.maximum_claims = max(int(data['maximum_claims']), 1)
        if 'starts_at' in data:
            event.starts_at = parse_datetime_payload(data.get('starts_at'), event.starts_at)
        if 'ends_at' in data:
            event.ends_at = parse_datetime_payload(data.get('ends_at'), event.ends_at)
        event.save()
    except (RewardError, ValueError) as exc:
        return JsonResponse({'success': False, 'message': getattr(exc, 'message', str(exc))}, status=getattr(exc, 'status', 400))
    return JsonResponse({'success': True, 'event': event_payload(event)})


@require_http_methods(['DELETE', 'POST'])
def reward_delete_api(request):
    denied = staff_required_json(request)
    if denied:
        return denied
    data = request_payload(request)
    event = get_object_or_404(RewardEvent, uuid=data.get('id') or data.get('event_id'))
    if event.claims.filter(status='approved').exists():
        stop_event(event, request.user, status='cancelled')
        return JsonResponse({'success': True, 'event': event_payload(event), 'message': 'Event cancelled because it already has claims.'})
    event.delete()
    return JsonResponse({'success': True})


@require_POST
def reward_start_api(request):
    denied = staff_required_json(request)
    if denied:
        return denied
    data = request_payload(request)
    event = get_object_or_404(RewardEvent, uuid=data.get('id') or data.get('event_id'))
    activate_event(event, request.user)
    return JsonResponse({'success': True, 'event': event_payload(event)})


@require_POST
def reward_stop_api(request):
    denied = staff_required_json(request)
    if denied:
        return denied
    data = request_payload(request)
    event = get_object_or_404(RewardEvent, uuid=data.get('id') or data.get('event_id'))
    status = 'cancelled' if data.get('cancel') else 'completed'
    stop_event(event, request.user, status=status)
    return JsonResponse({'success': True, 'event': event_payload(event)})


@login_required
def reward_statistics_api(request):
    if not request.user.is_staff:
        return JsonResponse({'success': False, 'message': 'Administrator access required.'}, status=403)
    event_uuid = request.GET.get('event_id')
    event = RewardEvent.objects.filter(uuid=event_uuid).first() if event_uuid else None
    return JsonResponse({'success': True, 'statistics': reward_statistics(event)})


@login_required
def reward_export_api(request):
    if not request.user.is_staff:
        return JsonResponse({'success': False, 'message': 'Administrator access required.'}, status=403)
    event_uuid = request.GET.get('event_id')
    claims = RewardClaim.objects.select_related('reward_event', 'user').filter(status='approved')
    if event_uuid:
        claims = claims.filter(reward_event__uuid=event_uuid)
    return JsonResponse({
        'success': True,
        'claims': [
            {
                'event': str(claim.reward_event.uuid),
                'event_title': claim.reward_event.title,
                'user': claim.user.username,
                'amount': float(claim.amount_claimed),
                'reference': claim.transaction_reference,
                'claim_time': claim.claim_time.isoformat(),
                'ip_address': claim.ip_address,
                'device_identifier': claim.device_identifier,
            }
            for claim in claims[:1000]
        ],
    })


@login_required
def reward_active_api(request):
    now = timezone.now()
    events = RewardEvent.objects.filter(status='active', starts_at__lte=now, ends_at__gte=now).order_by('ends_at')
    return JsonResponse({'success': True, 'events': [event_payload(event) for event in events]})


@require_POST
@login_required
def reward_claim_api(request):
    allowed, _ = enforce_rate_limit(
        request,
        'reward_claim',
        account_limit=10,
        ip_limit=30,
        window_seconds=3600,
    )
    if not allowed:
        return JsonResponse({
            'success': False,
            'message': 'Too many reward claims. Please try again later.',
            'rate_limited': True,
        }, status=429)

    data = request_payload(request)
    event_id = data.get('event_id') or data.get('id')
    if not event_id:
        return JsonResponse({'success': False, 'message': 'event_id is required.'}, status=400)
    try:
        claim, created = claim_reward(
            request.user,
            event_id,
            ip_address=client_ip(request),
            device_identifier=request.META.get('HTTP_USER_AGENT', '')[:160],
            idempotency_key=data.get('idempotency_key') or request.META.get('HTTP_IDEMPOTENCY_KEY', ''),
        )
    except RewardEvent.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Reward event was not found.'}, status=404)
    except RewardError as exc:
        return JsonResponse({'success': False, 'message': exc.message}, status=exc.status)
    if created:
        detect_reward_claim_pattern(request, request.user)
    wallet = ensure_reward_wallet(request.user)
    return JsonResponse({
        'success': True,
        'created': created,
        'claim': {
            'id': str(claim.uuid),
            'event_id': str(claim.reward_event.uuid),
            'amount': float(claim.amount_claimed),
            'status': claim.status,
            'reference': claim.transaction_reference,
            'claim_time': claim.claim_time.isoformat(),
        },
        'wallet': wallet_payload(wallet),
    })


@login_required
def reward_history_api(request):
    claims = RewardClaim.objects.select_related('reward_event').filter(user=request.user)
    return JsonResponse({
        'success': True,
        'claims': [
            {
                'id': str(claim.uuid),
                'event': str(claim.reward_event.uuid),
                'event_title': claim.reward_event.title,
                'amount': float(claim.amount_claimed),
                'status': claim.status,
                'claim_time': claim.claim_time.isoformat(),
            }
            for claim in claims[:100]
        ],
    })


@login_required
def reward_wallet_api(request):
    return JsonResponse({'success': True, 'wallet': wallet_payload(ensure_reward_wallet(request.user))})


@login_required
def reward_transactions_api(request):
    wallet = ensure_reward_wallet(request.user)
    transactions = RewardTransaction.objects.select_related('event').filter(wallet=wallet)[:100]
    return JsonResponse({
        'success': True,
        'transactions': [
            {
                'id': str(txn.uuid),
                'event': str(txn.event.uuid) if txn.event else None,
                'amount': float(txn.amount),
                'transaction_type': txn.transaction_type,
                'balance_before': float(txn.balance_before),
                'balance_after': float(txn.balance_after),
                'reference': txn.reference,
                'created_at': txn.created_at.isoformat(),
            }
            for txn in transactions
        ],
    })


def register(request):
    if request.user.is_authenticated:
        return redirect('play')

    if request.method == 'POST':
        allowed, _ = enforce_rate_limit(
            request,
            'registration',
            account_limit=1,
            ip_limit=settings.SECURITY_REGISTER_IP_LIMIT,
            window_seconds=settings.SECURITY_REGISTER_WINDOW_SECONDS,
        )
        if not allowed:
            return HttpResponse(
                'Too many registration attempts from this connection. Please try again later.',
                status=429,
            )
        form = RegisterForm(request.POST)
        if form.is_valid():
            with db_transaction.atomic():
                inviter_profile = _session_referral_profile(request, lock=True)
                user = form.save()
                Profile.objects.create(user=user, phone_number=form.cleaned_data['phone_number'])
                if inviter_profile and inviter_profile.user_id != user.pk:
                    Referral.objects.create(inviter_id=inviter_profile.user_id, invitee=user)
            request.session.pop('referral_code', None)
            login(request, user)
            log_audit(user, 'user_register', f'User {user.username} registered', request)
            detect_account_creation_pattern(request, user)
            messages.success(request, 'Welcome to Pakabet! Your account is ready.')
            return redirect('play')
    else:
        form = RegisterForm()
    return render(request, 'core/register.html', {
        'form': form,
        'referral_inviter': _session_referral_profile(request),
    })


@login_required
def dashboard(request):
    profile = get_object_or_404(Profile, user=request.user)
    player_sessions = GameSession.objects.filter(user=request.user).select_related('round')
    sessions = player_sessions.order_by('-created_at')[:8]
    settled_filter = Q(cashed_out_at__isnull=False) | Q(round__status__in=['crashed', 'void'])
    settled_stats = player_sessions.aggregate(
        settled_count=Count('id', filter=settled_filter),
        win_count=Count('id', filter=Q(cashed_out_at__lt=F('crash_point'))),
        total_staked=Sum('stake', filter=settled_filter),
        total_returned=Sum('won_amount', filter=settled_filter),
    )
    settled_count = settled_stats['settled_count'] or 0
    total_staked = settled_stats['total_staked'] or Decimal('0.00')
    total_returned = settled_stats['total_returned'] or Decimal('0.00')
    net_result = total_returned - total_staked
    win_rate = (settled_stats['win_count'] or 0) / settled_count * 100 if settled_count else 0
    rooms = Room.objects.filter(status='waiting').annotate(player_count=Count('players')).order_by('-created_at')[:4]
    form = PlayForm()
    return render(request, 'core/dashboard.html', {
        'profile': profile,
        'sessions': sessions,
        'rooms': rooms,
        'form': form,
        'total_bets': player_sessions.count(),
        'total_staked': total_staked,
        'total_returned': total_returned,
        'net_result': net_result,
        'settled_count': settled_count,
        'win_rate': win_rate,
    })


def play(request):
    profile = get_object_or_404(Profile, user=request.user) if request.user.is_authenticated else None
    recent_sessions = (
        GameSession.objects.filter(user=request.user).order_by('-created_at')[:5]
        if request.user.is_authenticated
        else GameSession.objects.none()
    )
    recent_crashes = GameRound.objects.filter(status='crashed').order_by('-started_at')[:50]
    fairness_round = (
        GameRound.objects.filter(Q(created_by=request.user) | Q(bets__user=request.user)).distinct().first()
        if request.user.is_authenticated
        else None
    )
    recent_chat_messages = (
        ChatMessage.objects.select_related('user')
        .annotate(reaction_count=Count('reactions'))
        .filter(flagged=False)[:100]
    )
    chat_messages = [
        chat for chat in recent_chat_messages
        if not get_chat_rule_violation(chat.message)
    ][:20]

    if request.method == 'GET':
        return render(request, 'core/play.html', {
            'profile': profile,
            'currency': profile.currency if profile else 'UGX',
            'recent_sessions': recent_sessions,
            'recent_crashes': recent_crashes,
            'fairness_round': fairness_round,
            'chat_messages': chat_messages,
            'chat_form': ChatMessageForm(),
            'online_count': active_participant_count(),
        })

    action = request.POST.get('action')
    if action == 'state':
        return JsonResponse(_shared_round_payload(request.user, request))
    if action == 'ledger':
        tab = request.POST.get('tab', 'all')
        if tab not in {'all', 'mine', 'top'}:
            return JsonResponse({'success': False, 'message': 'Invalid bet list view.'}, status=400)
        cursor = request.POST.get('cursor', '')
        cursor_data = None
        if cursor:
            try:
                cursor_data = signing.loads(cursor, salt='live-round-ledger')
                cursor_data['id'] = int(cursor_data['id'])
                if tab == 'mine':
                    cursor_data['cutoff'] = datetime.fromisoformat(cursor_data['cutoff'])
                    cursor_data['created_at'] = datetime.fromisoformat(cursor_data['created_at'])
                else:
                    cursor_data['stake'] = Decimal(cursor_data['stake'])
                    cursor_data['won'] = Decimal(cursor_data['won'])
            except (
                signing.BadSignature,
                signing.SignatureExpired,
                InvalidOperation,
                KeyError,
                TypeError,
                ValueError,
            ):
                return JsonResponse({'success': False, 'message': 'Invalid bet-list cursor.'}, status=400)
            if (
                cursor_data.get('tab') != tab
                or cursor_data.get('direction') not in {'next', 'previous'}
                or cursor_data['id'] < 1
                or (
                    tab == 'mine'
                    and (
                        not timezone.is_aware(cursor_data['cutoff'])
                        or not timezone.is_aware(cursor_data['created_at'])
                    )
                )
                or (
                    tab != 'mine'
                    and (
                        not cursor_data['stake'].is_finite()
                        or not cursor_data['won'].is_finite()
                    )
                )
            ):
                return JsonResponse({'success': False, 'message': 'Invalid bet-list cursor.'}, status=400)
        payload = _round_ledger_payload(request.user, tab, cursor_data)
        return JsonResponse(payload, status=409 if not payload['success'] else 200)

    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Log in to use game features.'}, status=401)

    if action == 'presence':
        presence_key = request.POST.get('presence_key', '')
        if not re.fullmatch(r'[0-9a-f]{32}', presence_key):
            return JsonResponse({'success': False, 'message': 'Invalid game presence key.'}, status=400)
        record_game_presence(request.user, presence_key)
        return JsonResponse({'success': True})

    if action == 'presence_leave':
        presence_key = request.POST.get('presence_key', '')
        if not re.fullmatch(r'[0-9a-f]{32}', presence_key):
            return JsonResponse({'success': False, 'message': 'Invalid game presence key.'}, status=400)
        remove_game_presence(request.user, presence_key)
        return JsonResponse({'success': True})

    if action == 'chat':
        form = ChatMessageForm(request.POST)
        if not form.is_valid():
            submitted_message = request.POST.get('message', '')
            if re.search(r'\b(?:scam|fraud|hack)\w*\b', submitted_message, re.IGNORECASE):
                RiskAlert.objects.create(
                    user=request.user,
                    alert_type='flagged_chat',
                    severity='medium',
                    description='A chat message was blocked by the chat rules.',
                    data={'blocked': True},
                )
            return JsonResponse({
                'success': False,
                'message': form.errors.get('message', ['Please enter a chat message.'])[0],
                'errors': form.errors,
            }, status=400)

        message = form.cleaned_data['message']
        chat = ChatMessage.objects.create(user=request.user, message=message, flagged=False)
        prune_chat_messages_if_needed()
        log_audit(request.user, 'chat_message', 'Posted chat message', request, {'message_id': chat.id, 'flagged': False})
        return JsonResponse({
            'success': True,
            'message': {
                'username': request.user.username,
                'id': chat.id,
                'text': chat.message,
                'created_at': chat.created_at.strftime('%H:%M'),
                'flagged': False,
                'reaction_count': 0,
            },
        })

    if action == 'chat_react':
        try:
            message_id = int(request.POST.get('message_id') or '0')
        except ValueError:
            return JsonResponse({'success': False, 'message': 'Invalid chat message.'}, status=400)

        chat = get_object_or_404(ChatMessage, id=message_id, flagged=False)
        reaction, created = ChatReaction.objects.get_or_create(
            message=chat,
            user=request.user,
            reaction='heart',
        )
        if not created:
            reaction.delete()
        reaction_count = chat.reactions.count()
        log_audit(request.user, 'chat_message', 'Updated chat reaction', request, {
            'message_id': chat.id,
            'active': created,
            'reaction_count': reaction_count,
        })
        return JsonResponse({
            'success': True,
            'active': created,
            'reaction_count': reaction_count,
        })

    if action == 'chat_rain':
        try:
            total_amount = Decimal(request.POST.get('total_amount') or '0').quantize(Decimal('0.01'))
            player_count = int(request.POST.get('players') or '0')
        except (InvalidOperation, ValueError):
            return JsonResponse({'success': False, 'message': 'Invalid rain settings.'}, status=400)

        try:
            result = send_chat_rain(
                request.user,
                total_amount,
                player_count,
                ip_address=client_ip(request),
                device_identifier=request.META.get('HTTP_USER_AGENT', ''),
            )
        except RewardError as exc:
            return JsonResponse({'success': False, 'message': exc.message}, status=exc.status)

        log_audit(request.user, 'chat_message', 'Sent chat rain', request, {
            'message_id': result['chat'].id,
            'reward_event_id': str(result['event'].uuid),
            'total_amount': str(result['actual_total']),
            'players': len(result['recipients']),
        })
        return JsonResponse({
            'success': True,
            'balance': float(result['sender_balance']),
            'reward_event_id': str(result['event'].uuid),
            'message': {
                'username': request.user.username,
                'id': result['chat'].id,
                'text': result['chat'].message,
                'created_at': result['chat'].created_at.strftime('%H:%M:%S'),
                'flagged': False,
                'reaction_count': 0,
                'rain': {
                    'total_amount': float(result['actual_total']),
                    'per_player_amount': float(result['per_player']),
                    'recipient_count': len(result['recipients']),
                    'reference': result['reference'],
                    'reward_event_id': str(result['event'].uuid),
                },
            },
        })

    if action == 'start':
        bet_data = request.POST.get('bet_data')
        if not bet_data:
            return JsonResponse({'success': False, 'message': 'Invalid bet data.'}, status=400)

        allowed, _ = enforce_rate_limit(
            request,
            'bet',
            account_limit=settings.SECURITY_BET_ACCOUNT_LIMIT,
            ip_limit=settings.SECURITY_BET_IP_LIMIT,
            window_seconds=settings.SECURITY_GAME_RATE_WINDOW_SECONDS,
        )
        if not allowed:
            return JsonResponse({
                'success': False,
                'message': 'Too many bet requests. Please wait before trying again.',
                'rate_limited': True,
            }, status=429)

        try:
            bets = json.loads(bet_data)
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'message': 'Invalid bet data.'}, status=400)
        if not isinstance(bets, list) or len(bets) > 2:
            return JsonResponse({'success': False, 'message': 'Invalid bet data.'}, status=400)

        if not bets:
            return JsonResponse(_shared_round_payload(request.user, request))

        parsed_bets = []
        for value in bets:
            try:
                if not isinstance(value, dict):
                    return JsonResponse({
                        'success': False,
                        'message': 'Each bet requires a unique request identifier.',
                    }, status=400)
                if set(value) - {'amount', 'auto_cashout_at', 'request_id'}:
                    return JsonResponse({
                        'success': False,
                        'message': 'Bet data contains unsupported fields.',
                    }, status=400)
                amount = Decimal(str(value.get('amount')))
                raw_auto_cashout = value.get('auto_cashout_at')
                auto_cashout = (
                    Decimal(str(raw_auto_cashout))
                    if raw_auto_cashout not in (None, '')
                    else None
                )
                request_id = str(value.get('request_id') or '')
                if not amount.is_finite() or amount <= 0:
                    return JsonResponse({'success': False, 'message': 'Enter valid bet amounts.'}, status=400)
                stake = amount.quantize(Decimal('0.01'))
                if stake <= 0 or stake > Decimal('999999.99'):
                    return JsonResponse({'success': False, 'message': 'Enter a bet amount between 0.01 and 999999.99.'}, status=400)
                if not re.fullmatch(r'[0-9a-f]{32}', request_id):
                    return JsonResponse({'success': False, 'message': 'Invalid bet request identifier.'}, status=400)
                if auto_cashout is not None and (
                    not auto_cashout.is_finite()
                    or auto_cashout < Decimal('1.01')
                    or auto_cashout > Decimal('999999.99')
                ):
                    return JsonResponse({'success': False, 'message': 'Invalid auto-cashout settings.'}, status=400)
            except (InvalidOperation, TypeError, ValueError):
                return JsonResponse({'success': False, 'message': 'Enter valid bet amounts.'}, status=400)
            parsed_bets.append({
                'stake': stake,
                'auto_cashout_at': auto_cashout,
                'request_id': request_id,
            })

        request_ids = [bet['request_id'] for bet in parsed_bets if bet['request_id']]
        if len(request_ids) != len(set(request_ids)):
            return JsonResponse({'success': False, 'message': 'Each bet must have a unique request identifier.'}, status=400)

        with db_transaction.atomic():
            SharedGameState.objects.filter(pk=1).update(active_round=F('active_round'))
            game_state = SharedGameState.objects.select_for_update().select_related('active_round').get(pk=1)
            locked_profile = Profile.objects.select_for_update().get(user=request.user)
            if is_self_excluded(request.user):
                return JsonResponse({'success': False, 'message': 'Your account is currently self-excluded.'}, status=403)

            game_round = game_state.active_round
            if not game_round or game_round.status in {'crashed', 'void'}:
                game_round = _start_shared_betting_round(game_state, request.user, request)
            if game_round.status == 'betting' and game_round.betting_ends_at and timezone.now() >= game_round.betting_ends_at:
                game_round.status = 'flying'
                game_round.started_at = game_round.betting_ends_at
                game_round.save(update_fields=['status', 'started_at'])
            if game_round.status == 'flying' and multiplier_at_elapsed_time(game_round.started_at) >= game_round.crash_point:
                reveal_game_round(game_round)
            if game_round.status != 'betting':
                return JsonResponse({
                    'success': False,
                    'code': 'betting_closed',
                    'message': 'Betting is closed for this round. Your balance was not changed.',
                    'round_id': game_round.public_id,
                    'status': game_round.status,
                }, status=409)

            existing_by_request = {
                session.bet_request_id: session
                for session in GameSession.objects.filter(
                    user=request.user,
                    bet_request_id__in=request_ids,
                )
            }
            if any(session.round_id != game_round.id for session in existing_by_request.values()):
                record_activity('bet_request_conflict', request, details={
                    'reason': 'request_id_reused_across_rounds',
                    'bet_count': len(parsed_bets),
                })
                return JsonResponse({
                    'success': False,
                    'message': 'This bet request was already used for a different round.',
                }, status=409)
            if any(
                session and (
                    session.stake != bet['stake']
                    or session.auto_cashout_at != bet['auto_cashout_at']
                )
                for bet in parsed_bets
                if bet['request_id']
                for session in [existing_by_request.get(bet['request_id'])]
            ):
                record_activity('bet_request_conflict', request, details={
                    'reason': 'request_id_reused_with_changed_settings',
                    'bet_count': len(parsed_bets),
                })
                return JsonResponse({
                    'success': False,
                    'message': 'This bet request was already used with different bet settings.',
                }, status=409)

            new_bets = [
                bet for bet in parsed_bets
                if not bet['request_id'] or bet['request_id'] not in existing_by_request
            ]
            existing_round_bets = GameSession.objects.filter(
                user=request.user,
                round=game_round,
            ).count()
            if existing_round_bets + len(new_bets) > 2:
                return JsonResponse({
                    'success': False,
                    'message': 'You can place at most two bets per round.',
                }, status=409)
            new_total = sum((bet['stake'] for bet in new_bets), Decimal('0.00'))
            if new_total > locked_profile.balance:
                    return JsonResponse({
                        'success': False,
                        'code': 'insufficient_balance',
                        'message': 'Insufficient balance for all bets.',
                        'balance': float(locked_profile.balance),
                    }, status=400)
            allowed, reason = can_place_bets(locked_profile, new_total)
            if not allowed:
                return JsonResponse({'success': False, 'message': reason}, status=403)

            created_sessions = []
            for bet in parsed_bets:
                existing_session = existing_by_request.get(bet['request_id']) if bet['request_id'] else None
                if existing_session:
                    created_sessions.append(existing_session.id)
                    continue
                session = GameSession.objects.create(
                    user=request.user,
                    round=game_round,
                    stake=bet['stake'],
                    crash_point=game_round.crash_point,
                    auto_cashout_at=bet['auto_cashout_at'],
                    bet_request_id=bet['request_id'],
                )
                created_sessions.append(session.id)

            if new_total:
                locked_profile.balance -= new_total
                locked_profile.save(update_fields=['balance'])
                Transaction.objects.create(
                    user=request.user,
                    transaction_type='bet',
                    amount=new_total,
                    status='completed',
                    reference=game_round.public_id,
                    description=f'Round {game_round.public_id} bet debit',
                )
                GameRound.objects.filter(pk=game_round.pk).update(bet_count=F('bet_count') + len(new_bets))
                game_round.bet_count += len(new_bets)
                run_player_risk_checks(request.user)

            active_sessions = list(
                GameSession.objects.filter(
                    user=request.user,
                    round=game_round,
                ).order_by('created_at')
            )
            seconds_remaining = max(
                (game_round.betting_ends_at - timezone.now()).total_seconds(),
                0,
            )

        log_audit(request.user, 'game_start', f'Placed {len(new_bets)} bets totaling {new_total:.2f} {profile.currency}', request, {
            'stakes': [str(bet['stake']) for bet in new_bets],
            'session_ids': created_sessions,
            'round_id': game_round.public_id,
            'server_seed_hash': game_round.server_seed_hash,
            'client_seed': game_round.client_seed,
        })

        return JsonResponse({
            'success': True,
            'session_ids': [session.id for session in active_sessions if session.cashed_out_at is None],
            'sessions': [
                {
                    'id': session.id,
                    'stake': float(session.stake),
                    'auto_cashout_at': float(session.auto_cashout_at) if session.auto_cashout_at is not None else None,
                    'cashed_out_at': float(session.cashed_out_at) if session.cashed_out_at is not None else None,
                    'won_amount': float(session.won_amount),
                    'bet_request_id': session.bet_request_id,
                }
                for session in active_sessions
            ],
            'round_id': game_round.public_id,
            'server_seed_hash': game_round.server_seed_hash,
            'client_seed': game_round.client_seed,
            'status': 'betting',
            'betting_ends_at': game_round.betting_ends_at.isoformat(),
            'seconds_remaining': seconds_remaining,
            'balance': float(locked_profile.balance),
            'stake_total': float(new_total),
        })

    if action == 'cashout':
        allowed, _ = enforce_rate_limit(
            request,
            'cashout',
            account_limit=settings.SECURITY_CASHOUT_ACCOUNT_LIMIT,
            ip_limit=settings.SECURITY_CASHOUT_IP_LIMIT,
            window_seconds=settings.SECURITY_GAME_RATE_WINDOW_SECONDS,
        )
        if not allowed:
            return JsonResponse({
                'success': False,
                'message': 'Too many cash-out requests. Please wait before trying again.',
                'rate_limited': True,
            }, status=429)

        raw_session_ids = request.POST.get('session_ids', '').split(',')
        if (
            not raw_session_ids
            or len(raw_session_ids) > 2
            or any(not value.isdigit() for value in raw_session_ids)
            or len(set(raw_session_ids)) != len(raw_session_ids)
        ):
            return JsonResponse({'success': False, 'message': 'No active bets available.'}, status=400)

        session_ids = [int(value) for value in raw_session_ids]
        with db_transaction.atomic():
            sessions = list(GameSession.objects.select_for_update().select_related('round').filter(id__in=session_ids, user=request.user))
            if len(sessions) != len(session_ids):
                return JsonResponse({'success': False, 'message': 'Invalid games to cash out.'}, status=400)

            game_round = sessions[0].round
            if not game_round or any(session.round != game_round for session in sessions):
                return JsonResponse({'success': False, 'message': 'This round cannot be verified.'}, status=400)

            server_multiplier = multiplier_at_elapsed_time(game_round.started_at)
            round_has_crashed = server_multiplier >= game_round.crash_point
            if game_round.status == 'betting':
                return JsonResponse({
                    'success': False,
                    'message': 'Cash out is not available before the flight starts.',
                }, status=409)

            total_won = Decimal('0.00')
            total_stake = Decimal('0.00')
            all_lost = True
            settled_count = 0
            winning_bet_count = 0
            settled_multipliers = []

            for session in sessions:
                if session.cashed_out_at is not None:
                    continue

                auto_cashout = session.auto_cashout_at
                settled_count += 1
                effective_multiplier = (
                    auto_cashout
                    if auto_cashout is not None and server_multiplier >= auto_cashout
                    else server_multiplier
                )
                settled_multipliers.append(effective_multiplier)
                session.cashed_out_at = effective_multiplier
                session.won_amount = Decimal('0.00')
                total_stake += session.stake

                can_win = (
                    effective_multiplier < session.crash_point
                    and (auto_cashout is not None or not round_has_crashed)
                )
                if can_win:
                    session.won_amount = (session.stake * effective_multiplier).quantize(Decimal('0.01'))
                    total_won += session.won_amount
                    all_lost = False
                    if session.won_amount > 0:
                        winning_bet_count += 1

                session.save(update_fields=['cashed_out_at', 'won_amount'])

            if settled_count == 0:
                if all(session.cashed_out_at is not None for session in sessions):
                    record_activity('cashout_replay', request, details={
                        'session_count': len(session_ids),
                    })
                    locked_profile = Profile.objects.select_for_update().get(user=request.user)
                    total_won = sum((session.won_amount for session in sessions), Decimal('0.00'))
                    return JsonResponse({
                        'success': True,
                        'message': 'This bet was already settled.',
                        'round_id': game_round.public_id,
                        'crash_point': float(game_round.crash_point) if round_has_crashed else None,
                        'cashout_multiplier': float(max(session.cashed_out_at for session in sessions)),
                        'won_amount': float(total_won),
                        'balance': float(locked_profile.balance),
                        'status': 'won' if total_won > 0 else 'lost',
                        'fairness': {
                            'server_seed_hash': game_round.server_seed_hash,
                            'client_seed': game_round.client_seed,
                            'server_seed': game_round.server_seed,
                            'revealed': game_round.is_revealed,
                        },
                    })
                return JsonResponse({
                    'success': False,
                    'message': 'Auto cashout target has not been reached yet or this bet was already settled.',
                    'server_multiplier': float(server_multiplier),
                }, status=409)

            locked_profile = Profile.objects.select_for_update().get(user=request.user)
            if winning_bet_count:
                GameRound.objects.filter(pk=game_round.pk).update(
                    winning_bet_count=F('winning_bet_count') + winning_bet_count,
                )
            if total_won > 0:
                locked_profile.balance += total_won
                locked_profile.save(update_fields=['balance'])
                Transaction.objects.create(
                    user=request.user,
                    transaction_type='win',
                    amount=total_won,
                    status='completed',
                    reference=game_round.public_id,
                    description=f'Round {game_round.public_id} win credit',
                )
                message = f'You cashed out {total_won:.2f} {profile.currency} successfully!'
            else:
                message = 'The flight crashed before cash out. Better luck next round.'

            if round_has_crashed:
                reveal_game_round(game_round)

        cashout_multiplier = max(settled_multipliers)
        log_audit(request.user, 'game_cashout', f'Cashed out {settled_count} bets at {cashout_multiplier:.2f}x', request, {
            'session_ids': session_ids,
            'round_id': game_round.public_id,
            'server_multiplier': str(server_multiplier),
            'cashout_multiplier': str(cashout_multiplier),
            'total_won': str(total_won),
            'all_lost': all_lost,
        })

        return JsonResponse({
            'success': True,
            'message': message,
            'round_id': game_round.public_id,
            'crash_point': float(game_round.crash_point) if round_has_crashed else None,
            'cashout_multiplier': float(cashout_multiplier),
            'won_amount': float(total_won),
            'balance': float(locked_profile.balance),
            'status': 'lost' if all_lost else 'won',
            'fairness': {
                'server_seed_hash': game_round.server_seed_hash,
                'client_seed': game_round.client_seed,
                'server_seed': game_round.server_seed,
                'revealed': game_round.is_revealed,
            },
        })

    if action == 'settle_crash':
        round_id = request.POST.get('round_id', '')
        game_round = get_object_or_404(GameRound, public_id=round_id)
        is_shared_round = SharedGameState.objects.filter(pk=1, active_round=game_round).exists()
        if not is_shared_round and not GameRound.objects.filter(
            Q(created_by=request.user) | Q(bets__user=request.user),
            pk=game_round.pk,
        ).exists():
            raise Http404
        server_multiplier = multiplier_at_elapsed_time(game_round.started_at)
        if server_multiplier < game_round.crash_point:
            return JsonResponse({
                'success': False,
                'message': 'Round is still flying.',
                'server_multiplier': float(server_multiplier),
            }, status=409)

        reveal_game_round(game_round)

        log_audit(request.user, 'round_reveal', f'Revealed round {game_round.public_id}', request, {
            'round_id': game_round.public_id,
            'server_seed_hash': game_round.server_seed_hash,
            'client_seed': game_round.client_seed,
            'server_seed': game_round.server_seed,
            'crash_point': str(game_round.crash_point),
        })

        return JsonResponse({
            'success': True,
            'round_id': game_round.public_id,
            'crash_point': float(game_round.crash_point),
            'fairness': {
                'server_seed_hash': game_round.server_seed_hash,
                'client_seed': game_round.client_seed,
                'server_seed': game_round.server_seed,
                'revealed': True,
            },
        })

    return JsonResponse({'success': False, 'message': 'Invalid action.'}, status=400)


@require_http_methods(['GET'])
def online_count(request):
    return JsonResponse({'online_count': active_participant_count()})


@login_required
def wallet(request):
    profile = get_object_or_404(Profile, user=request.user)
    initial_phone = {'phone_number': profile.phone_number}
    deposit_form = WalletForm(prefix='deposit', initial=initial_phone)
    withdraw_form = WalletForm(prefix='withdraw', initial=initial_phone)
    deposit_idempotency_key = request.POST.get(
        'deposit-idempotency_key',
        uuid.uuid4().hex,
    )
    withdraw_idempotency_key = request.POST.get(
        'withdraw-idempotency_key',
        uuid.uuid4().hex,
    )

    if request.method == 'POST':
        action = request.POST.get('action')
        if action in {'deposit', 'withdraw'}:
            allowed, _ = enforce_rate_limit(
                request,
                'payment',
                account_limit=settings.SECURITY_PAYMENT_ACCOUNT_LIMIT,
                ip_limit=settings.SECURITY_PAYMENT_IP_LIMIT,
                window_seconds=settings.SECURITY_PAYMENT_WINDOW_SECONDS,
            )
            if not allowed:
                return HttpResponse(
                    'Too many payment requests. Please try again later.',
                    status=429,
                )
        if action == 'deposit':
            deposit_form = WalletForm(request.POST, prefix='deposit')
            if deposit_form.is_valid():
                amount = deposit_form.cleaned_data['amount']
                provider = deposit_form.cleaned_data['provider']
                phone_number = deposit_form.cleaned_data['phone_number']

                allowed, reason = can_deposit(profile, amount)
                if not allowed:
                    deposit_form.add_error('amount', reason)
                else:
                    payment, result, error = initiate_deposit(
                        user=request.user,
                        amount=amount,
                        provider=provider,
                        phone_number=phone_number,
                        redirect_url=request.build_absolute_uri('/wallet/'),
                        idempotency_key=deposit_idempotency_key,
                    )
                    log_audit(request.user, 'deposit', 'Requested a deposit', request, {
                        'provider': provider,
                    })
                    detect_payment_pattern(request, request.user)
                    run_player_risk_checks(request.user)
                    if error:
                        if payment:
                            if payment.status in ['pending', 'processing']:
                                messages.warning(
                                    request,
                                    f'Deposit {payment.external_reference} is being confirmed by the provider. Do not submit it again; refresh its status below.',
                                )
                            else:
                                messages.error(request, 'The provider rejected the deposit request. No deposit was credited; review the request details before trying again.')
                            return redirect('wallet')
                        deposit_form.add_error(None, error)
                    elif payment.status == 'completed':
                        messages.success(request, f'{amount:.2f} {profile.currency} deposited successfully via {payment.get_provider_display()}.')
                        return redirect('wallet')
                    elif payment.status in ['failed', 'cancelled']:
                        messages.error(request, 'The provider did not complete the deposit. No funds were credited; review the payment request for details.')
                        return redirect('wallet')
                    else:
                        message = f'{amount:.2f} {profile.currency} deposit sent to {payment.get_provider_display()}. Status: {payment.get_status_display()}.'
                        if payment.status in ['pending', 'processing']:
                            message += ' Check your phone and approve the ioTec Pay prompt with your PIN.'
                        messages.info(request, message)
                        return redirect('wallet')
        elif action == 'withdraw':
            withdraw_form = WalletForm(request.POST, prefix='withdraw')
            if withdraw_form.is_valid():
                amount = withdraw_form.cleaned_data['amount']
                provider = withdraw_form.cleaned_data['provider']
                phone_number = withdraw_form.cleaned_data['phone_number']
                payment, result, error = initiate_withdrawal(
                    user=request.user,
                    amount=amount,
                    provider=provider,
                    phone_number=phone_number,
                    idempotency_key=withdraw_idempotency_key,
                )
                if error:
                    if payment:
                        log_audit(request.user, 'withdraw', 'Requested a withdrawal', request, {
                            'provider': provider,
                        })
                        detect_payment_pattern(request, request.user)
                        if payment.status == 'queued':
                            messages.info(
                                request,
                                f'Withdrawal {payment.external_reference} is queued while provider funds are checked. The amount remains on hold and will be retried automatically.',
                            )
                        elif payment.status in ['pending', 'processing']:
                            messages.warning(
                                request,
                                f'Withdrawal {payment.external_reference} is awaiting provider confirmation. The amount remains on hold; do not submit it again.',
                            )
                        else:
                            messages.error(request, 'The provider rejected the withdrawal request. The amount has been returned to your balance.')
                        return redirect('wallet')
                    withdraw_form.add_error(None, error)
                else:
                    log_audit(request.user, 'withdraw', 'Requested a withdrawal', request, {
                        'provider': provider,
                    })
                    detect_payment_pattern(request, request.user)
                    if payment.status == 'completed':
                        messages.success(request, f'{amount:.2f} {profile.currency} withdrawal completed via {payment.get_provider_display()}.')
                    elif payment.status in ['failed', 'cancelled']:
                        messages.error(request, 'The provider did not complete the withdrawal. The held funds have been returned to your balance.')
                    elif payment.status == 'queued':
                        messages.info(
                            request,
                            f'Withdrawal {payment.external_reference} is queued while provider funds are checked. The amount remains on hold and will be retried automatically.',
                        )
                    else:
                        messages.info(request, f'{amount:.2f} {profile.currency} withdrawal {payment.external_reference} requested via {payment.get_provider_display()}. Status: {payment.get_status_display()}.')
                    return redirect('wallet')

    transactions = Transaction.objects.filter(user=request.user)
    payment_requests = PaymentRequest.objects.filter(user=request.user)[:10]
    daily_deposit_total = get_daily_deposit_total(request.user)
    payment_mode = getattr(settings, 'IOTEC_PAY_MODE', 'sandbox').lower()
    return render(request, 'core/wallet.html', {
        'profile': profile,
        'active_exclusion': get_active_self_exclusion(request.user),
        'deposit_form': deposit_form,
        'withdraw_form': withdraw_form,
        'transactions': transactions,
        'payment_requests': payment_requests,
        'daily_deposit_total': daily_deposit_total,
        'maximum_deposit_amount': MAXIMUM_DEPOSIT_AMOUNT,
        'payment_mode': payment_mode,
        'deposit_idempotency_key': deposit_idempotency_key,
        'withdraw_idempotency_key': withdraw_idempotency_key,
    })


@csrf_exempt
@require_POST
def payment_callback(request):
    expected_token = getattr(settings, 'IOTEC_PAY_CALLBACK_TOKEN', '')
    provided_token = request.headers.get('X-Pakabet-Webhook-Token', '') or request.headers.get('X-IOTEC-Webhook-Token', '')
    if not expected_token:
        return JsonResponse({'success': False, 'message': 'Callback token is required.'}, status=403)
    if not compare_digest(expected_token, provided_token):
        return JsonResponse({'success': False, 'message': 'Invalid callback token.'}, status=403)

    payload = request_payload(request)
    external_reference = payload.get('externalId') or payload.get('external_id') or payload.get('reference')
    if not external_reference:
        return JsonResponse({'success': False, 'message': 'Missing externalId.'}, status=400)

    payment = get_object_or_404(PaymentRequest, external_reference=external_reference)
    result = PaymentGatewayResult(
        provider_reference=str(payload.get('id') or payload.get('transactionId') or payment.provider_reference or external_reference),
        status=payload.get('status') or 'Pending',
        status_code=payload.get('statusCode') or '',
        status_message=payload.get('statusMessage') or '',
        redirect_url=payload.get('redirectUrl') or '',
        raw=payload,
    )
    payment = apply_provider_result(payment, result)
    return JsonResponse({'success': True, 'reference': external_reference, 'status': payment.status})


@login_required
@require_POST
def payment_sync(request, reference):
    payment = get_object_or_404(PaymentRequest, external_reference=reference, user=request.user)
    if payment.payment_type == 'withdraw' and (
        payment.status == 'queued'
        or (
            payment.status == 'processing'
            and (payment.metadata or {}).get('dispatch_state') == 'checking_balance'
        )
    ):
        messages.info(
            request,
            f'Withdrawal {payment.external_reference} is still queued and has not been submitted to the provider.',
        )
        return redirect('wallet')
    try:
        payment = sync_payment(payment)
    except PaymentGatewayError:
        messages.error(request, 'The provider could not return a payment update. Please try again shortly.')
    else:
        messages.info(request, f'Payment {payment.external_reference} is now {payment.get_status_display()}.')
    return redirect('wallet')


@login_required
def admin_dashboard(request):
    if not request.user.is_staff:
        return redirect('dashboard')

    report_form = FinancialReportForm()
    if request.method == 'POST':
        report_form = FinancialReportForm(request.POST)
        if report_form.is_valid():
            report = generate_financial_report(
                report_form.cleaned_data['report_type'],
                report_form.cleaned_data['period_start'],
                report_form.cleaned_data['period_end'],
                request.user,
            )
            log_audit(request.user, 'financial_report', f'Generated report {report.id}', request, {
                'report_type': report.report_type,
                'period_start': str(report.period_start),
                'period_end': str(report.period_end),
            })
            messages.success(request, 'Financial report generated.')
            return redirect('admin_dashboard')

    total_users = User.objects.filter(is_active=True).count()
    total_balance = Profile.objects.aggregate(total=Sum('balance'))['total'] or Decimal('0.00')
    total_deposits = Transaction.objects.filter(transaction_type='deposit', status='completed').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_withdrawals = Transaction.objects.filter(transaction_type='withdraw', status='completed').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_bets = GameSession.objects.aggregate(total=Sum('stake'))['total'] or Decimal('0.00')
    total_games = GameSession.objects.count()
    pending_deposits = PaymentRequest.objects.filter(payment_type='deposit', status='pending').count()
    pending_withdrawals = PaymentRequest.objects.filter(
        payment_type='withdraw',
        status__in=['queued', 'pending', 'processing'],
    ).count()
    active_self_exclusions = SelfExclusion.objects.filter(
        starts_at__lte=timezone.now(),
    ).filter(
        Q(permanent=True) | Q(ends_at__gt=timezone.now())
    ).count()
    open_risk_alerts = RiskAlert.objects.filter(status='open').count()
    latest_reports = FinancialReport.objects.order_by('-created_at')[:5]
    active_rooms = Room.objects.filter(status='waiting').count()
    running_rooms = Room.objects.filter(status='running').count()
    room_count = Room.objects.count()

    return render(request, 'core/admin_dashboard.html', {
        'total_users': total_users,
        'total_balance': total_balance,
        'total_deposits': total_deposits,
        'total_withdrawals': total_withdrawals,
        'total_bets': total_bets,
        'total_games': total_games,
        'pending_deposits': pending_deposits,
        'pending_withdrawals': pending_withdrawals,
        'active_self_exclusions': active_self_exclusions,
        'open_risk_alerts': open_risk_alerts,
        'latest_reports': latest_reports,
        'report_form': report_form,
        'active_rooms': active_rooms,
        'running_rooms': running_rooms,
        'room_count': room_count,
    })


def room_list(request):
    return redirect('play')


@login_required
def create_room(request):
    if request.method != 'POST':
        return redirect('room_list')

    allowed, _ = enforce_rate_limit(
        request,
        'room_create',
        account_limit=settings.SECURITY_ROOM_CREATE_ACCOUNT_LIMIT,
        ip_limit=settings.SECURITY_ROOM_CREATE_IP_LIMIT,
        window_seconds=settings.SECURITY_ROOM_CREATE_WINDOW_SECONDS,
    )
    if not allowed:
        return HttpResponse(
            'Too many room creation requests. Please try again later.',
            status=429,
        )

    form = RoomCreateForm(request.POST)
    if form.is_valid():
        room = form.save(commit=False)
        room.host = request.user
        room.save()
        messages.success(request, 'Room created successfully.')
        return redirect('room_detail', room_id=room.id)

    rooms = Room.objects.filter(status__in=['waiting', 'running']).order_by('-created_at')[:12]
    return render(request, 'core/room_list.html', {
        'rooms': rooms,
        'create_form': form,
    })


@login_required
def room_detail(request, room_id):
    room = get_object_or_404(Room, id=room_id)
    player = RoomPlayer.objects.filter(room=room, user=request.user).first()
    join_form = RoomJoinForm()
    return render(request, 'core/room_detail.html', {
        'room': room,
        'player': player,
        'join_form': join_form,
    })


@login_required
def room_action(request, room_id):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method.'}, status=405)

    room = get_object_or_404(Room, id=room_id)
    profile = get_object_or_404(Profile, user=request.user)
    action = request.POST.get('action')

    if action in {'join', 'start', 'cashout'}:
        allowed, _ = enforce_rate_limit(
            request,
            f'room_{action}',
            account_limit=20,
            ip_limit=100,
            window_seconds=60,
        )
        if not allowed:
            return JsonResponse({
                'success': False,
                'message': 'Too many room requests. Please wait before trying again.',
                'rate_limited': True,
            }, status=429)

    if action == 'join':
        if room.status != 'waiting':
            return JsonResponse({'success': False, 'message': 'Room is not accepting new entries.'}, status=400)

        if RoomPlayer.objects.filter(room=room, user=request.user).exists():
            return JsonResponse({'success': False, 'message': 'You already joined this room.'}, status=400)

        if room.players.count() >= room.max_players:
            return JsonResponse({'success': False, 'message': 'Room is full.'}, status=400)

        form = RoomJoinForm(request.POST)
        if not form.is_valid():
            return JsonResponse({'success': False, 'errors': form.errors}, status=400)

        stake = form.cleaned_data['stake']
        with db_transaction.atomic():
            locked_room = Room.objects.select_for_update().get(pk=room.pk)
            if locked_room.status != 'waiting':
                return JsonResponse({'success': False, 'message': 'Room is not accepting new entries.'}, status=400)
            if RoomPlayer.objects.filter(room=locked_room, user=request.user).exists():
                return JsonResponse({'success': False, 'message': 'You already joined this room.'}, status=400)
            if locked_room.players.count() >= locked_room.max_players:
                return JsonResponse({'success': False, 'message': 'Room is full.'}, status=400)

            locked_profile = Profile.objects.select_for_update().get(user=request.user)
            allowed, reason = can_place_bets(locked_profile, stake)
            if not allowed:
                return JsonResponse({'success': False, 'message': reason}, status=403)
            if stake > locked_profile.balance:
                return JsonResponse({'success': False, 'message': 'Insufficient balance to join.'}, status=400)

            locked_profile.balance -= stake
            locked_profile.save(update_fields=['balance'])
            RoomPlayer.objects.create(room=locked_room, user=request.user, stake=stake)

        return JsonResponse({
            'success': True,
            'message': 'Joined room successfully.',
            'balance': float(locked_profile.balance),
        })

    if action == 'start':
        if request.user != room.host:
            return JsonResponse({'success': False, 'message': 'Only the host can start this game.'}, status=403)

        with db_transaction.atomic():
            locked_room = Room.objects.select_for_update().get(pk=room.pk)
            Profile.objects.select_for_update().get(user=request.user)
            if is_self_excluded(request.user):
                return JsonResponse({'success': False, 'message': 'Your account is currently self-excluded.'}, status=403)
            if locked_room.status != 'waiting':
                return JsonResponse({'success': False, 'message': 'Game has already started.'}, status=400)
            if locked_room.players.count() < 1:
                return JsonResponse({'success': False, 'message': 'At least one player must join to start.'}, status=400)

            locked_room.crash_point = Decimal(str(round(random.uniform(1.5, 12.0), 2)))
            locked_room.status = 'running'
            locked_room.started_at = timezone.now()
            locked_room.save()

        return JsonResponse({
            'success': True,
            'message': 'Game started.',
        })

    if action == 'cashout':
        with db_transaction.atomic():
            room = Room.objects.select_for_update().get(pk=room.pk)
            profile = Profile.objects.select_for_update().get(user=request.user)
            player = get_object_or_404(
                RoomPlayer.objects.select_for_update(),
                room=room,
                user=request.user,
            )
            if room.status != 'running':
                return JsonResponse({'success': False, 'message': 'The game is not running.'}, status=400)

            if player.cashed_out_at is not None:
                record_activity('room_cashout_replay', request, details={'room_id': room.pk})
                return JsonResponse({'success': False, 'message': 'You already cashed out.'}, status=409)

            server_multiplier = multiplier_at_elapsed_time(room.started_at)
            effective_multiplier = server_multiplier
            round_has_crashed = server_multiplier >= room.crash_point

            player.cashed_out_at = effective_multiplier
            player.active = False
            player.won_amount = Decimal('0')
            message = 'Crashed before cash out. Better luck next round.'

            if not round_has_crashed and effective_multiplier < room.crash_point:
                player.won_amount = (player.stake * effective_multiplier).quantize(Decimal('0.01'))
                profile.balance += player.won_amount
                profile.save(update_fields=['balance'])
                message = f'Cashed out at {effective_multiplier:.2f}x and won {player.won_amount:.2f} {profile.currency}.'
            elif round_has_crashed:
                room.status = 'ended'
                room.ended_at = room.ended_at or timezone.now()
                room.save(update_fields=['status', 'ended_at'])

            player.save()
        return JsonResponse({
            'success': True,
            'message': message,
            'balance': float(profile.balance),
            'won_amount': float(player.won_amount),
            'cashout_multiplier': float(effective_multiplier),
            'crash_point': float(room.crash_point) if round_has_crashed else None,
            'status': 'won' if player.is_winner else 'lost',
        })

    return JsonResponse({'success': False, 'message': 'Invalid action.'}, status=400)


@login_required
def room_status(request, room_id):
    room = get_object_or_404(Room, id=room_id)
    server_multiplier = None
    if room.status == 'running' and room.started_at and room.crash_point is not None:
        server_multiplier = multiplier_at_elapsed_time(room.started_at)
        if server_multiplier >= room.crash_point:
            room.status = 'ended'
            room.ended_at = room.ended_at or timezone.now()
            room.save(update_fields=['status', 'ended_at'])
    players = room.players.select_related('user').all()
    participants = []
    for player in players:
        status = 'active' if player.cashed_out_at is None else ('won' if player.is_winner else 'lost')
        participants.append({
            'username': player.user.username,
            'stake': float(player.stake),
            'cashed_out_at': float(player.cashed_out_at) if player.cashed_out_at is not None else None,
            'won_amount': float(player.won_amount),
            'status': status,
        })

    return JsonResponse({
        'success': True,
        'room': {
            'status': room.status,
            'crash_point': float(room.crash_point) if room.status == 'ended' and room.crash_point else None,
            'current_multiplier': float(server_multiplier) if room.status == 'running' else None,
            'host': room.host.username,
            'code': room.code,
            'participants': participants,
            'player_count': players.count(),
            'max_players': room.max_players,
        },
    })


@login_required
def profile_view(request):
    profile = get_object_or_404(Profile, user=request.user)
    compliance_initial = {
        'phone_number': profile.phone_number,
        'daily_deposit_limit': profile.daily_deposit_limit,
        'daily_loss_limit': profile.daily_loss_limit,
    }
    form = ProfileComplianceForm(initial=compliance_initial)
    exclusion_form = SelfExclusionForm()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'compliance':
            form = ProfileComplianceForm(request.POST)
            if form.is_valid():
                profile.phone_number = form.cleaned_data['phone_number']
                profile.daily_deposit_limit = form.cleaned_data['daily_deposit_limit']
                profile.daily_loss_limit = form.cleaned_data['daily_loss_limit']
                profile.save(update_fields=['phone_number', 'daily_deposit_limit', 'daily_loss_limit'])
                messages.success(request, 'Compliance preferences updated.')
                return redirect('profile')
        elif action == 'self_exclude':
            exclusion_form = SelfExclusionForm(request.POST)
            if not request.POST.get('confirm_exclusion'):
                exclusion_form.add_error(None, 'Confirm that you understand this action before continuing.')
            if exclusion_form.is_valid():
                duration = exclusion_form.cleaned_data['duration']
                exclusion = activate_self_exclusion(
                    request.user,
                    duration,
                    exclusion_form.cleaned_data['reason'],
                )
                if exclusion:
                    log_audit(request.user, 'self_exclude', 'Created self-exclusion', request, {
                        'exclusion_id': exclusion.id,
                        'permanent': exclusion.permanent,
                    })
                    messages.warning(request, 'Self-exclusion is now active. Deposits and new bets are blocked for the selected period.')
                    return redirect('profile')
                exclusion_form.add_error(None, 'Self-exclusion is already active on your account.')
        else:
            form = ProfileComplianceForm(request.POST)
            exclusion_form = SelfExclusionForm()
    active_exclusion = get_active_self_exclusion(request.user)
    exclusions = SelfExclusion.objects.filter(user=request.user)[:5]
    transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')[:5]
    referral_attribution = Referral.objects.filter(invitee=request.user).select_related('inviter').first()
    return render(request, 'core/profile.html', {
        'profile': profile,
        'form': form,
        'exclusion_form': exclusion_form,
        'active_exclusion': active_exclusion,
        'exclusions': exclusions,
        'transactions': transactions,
        'referral_attribution': referral_attribution,
    })


@login_required
def referrals_view(request):
    profile = get_object_or_404(Profile, user=request.user)
    referrals = Referral.objects.filter(inviter=request.user).select_related(
        'invitee',
        'first_deposit',
    ).order_by('-created_at')
    stats = referrals.aggregate(
        total_invited=Count('id'),
        total_earned=Sum('reward_amount', filter=Q(reward_transaction__isnull=False)),
    )
    invite_path = reverse('referral_landing', kwargs={'referral_code': profile.referral_code})
    return render(request, 'core/referrals.html', {
        'profile': profile,
        'referrals': referrals[:100],
        'total_invited': stats['total_invited'],
        'total_earned': stats['total_earned'] or Decimal('0.00'),
        'invite_link': request.build_absolute_uri(invite_path),
    })


@login_required
def bet_history(request):
    profile = get_object_or_404(Profile, user=request.user)
    history = _bet_history_data(request.user, profile, request.GET.get('outcome', 'all'), request.GET.get('page'))
    return render(request, 'core/bet_history.html', history)


def _bet_history_data(user, profile, requested_outcome='all', requested_page=None):
    all_sessions = GameSession.objects.filter(user=user).select_related('round').order_by('-created_at')
    outcome = requested_outcome
    if outcome == 'won':
        filtered_sessions = all_sessions.filter(cashed_out_at__lt=F('crash_point'))
    elif outcome == 'lost':
        filtered_sessions = all_sessions.filter(
            Q(cashed_out_at__gte=F('crash_point'))
            | Q(cashed_out_at__isnull=True, round__status='crashed')
        )
    elif outcome == 'live':
        filtered_sessions = all_sessions.filter(cashed_out_at__isnull=True, round__status__in=['betting', 'flying'])
    elif outcome == 'void':
        filtered_sessions = all_sessions.filter(round__status='void')
    else:
        outcome = 'all'
        filtered_sessions = all_sessions

    winning_filter = Q(cashed_out_at__lt=F('crash_point'))
    losing_filter = (
        Q(cashed_out_at__gte=F('crash_point'))
        | Q(cashed_out_at__isnull=True, round__status='crashed')
    )
    live_filter = Q(cashed_out_at__isnull=True, round__status__in=['betting', 'flying'])
    void_filter = Q(round__status='void')
    settled_filter = Q(cashed_out_at__isnull=False) | Q(round__status__in=['crashed', 'void'])
    summary = all_sessions.aggregate(
        total_bets=Count('id'),
        settled_bets=Count('id', filter=settled_filter),
        winning_bets=Count('id', filter=winning_filter),
        losing_bets=Count('id', filter=losing_filter),
        live_bets=Count('id', filter=live_filter),
        void_bets=Count('id', filter=void_filter),
        total_staked=Sum('stake', filter=settled_filter),
        total_returned=Sum('won_amount', filter=settled_filter),
    )
    filter_counts = {
        'all': summary['total_bets'] or 0,
        'won': summary['winning_bets'] or 0,
        'lost': summary['losing_bets'] or 0,
        'live': summary['live_bets'] or 0,
        'void': summary['void_bets'] or 0,
    }
    total_staked = summary['total_staked'] or Decimal('0.00')
    total_returned = summary['total_returned'] or Decimal('0.00')
    settled_bets = summary['settled_bets'] or 0
    winning_bets = summary['winning_bets'] or 0
    page_obj = Paginator(filtered_sessions, 20).get_page(requested_page)
    sessions = list(page_obj.object_list)
    settle_rounds = set()
    for session in sessions:
        round_obj = session.round
        if session.cashed_out_at is not None:
            session.history_status = 'won' if session.is_winner else 'lost'
            session.history_multiplier = session.cashed_out_at
            session.history_payout = session.won_amount
        elif not round_obj:
            session.history_status = 'unavailable'
            session.history_multiplier = None
            session.history_payout = None
        elif round_obj.status == 'crashed':
            session.history_status = 'lost'
            session.history_multiplier = round_obj.crash_point
            session.history_payout = session.won_amount
        elif round_obj.status == 'void':
            session.history_status = 'void'
            session.history_multiplier = None
            session.history_payout = session.won_amount
        else:
            session.history_status = 'live'
            session.history_multiplier = (
                multiplier_at_elapsed_time(round_obj.started_at)
                if round_obj.status == 'flying'
                else Decimal('1.00')
            )
            session.history_payout = None

    active_rounds = (
        all_sessions.filter(cashed_out_at__isnull=True, round__status='flying')
        .order_by('-round__started_at')
        .values_list('round__public_id', 'round__started_at', 'round__crash_point')
        .distinct()[:20]
    )
    for round_id, started_at, crash_point in active_rounds:
        if multiplier_at_elapsed_time(started_at) >= crash_point:
            settle_rounds.add(round_id)

    return {
        'profile': profile,
        'sessions': sessions,
        'page_obj': page_obj,
        'outcome': outcome,
        'filter_counts': filter_counts,
        'settle_rounds': sorted(settle_rounds),
        'total_bets': summary['total_bets'] or 0,
        'settled_bets': settled_bets,
        'winning_bets': winning_bets,
        'total_staked': total_staked,
        'total_returned': total_returned,
        'net_result': total_returned - total_staked,
        'win_rate': winning_bets / settled_bets * 100 if settled_bets else 0,
    }


@login_required
@require_http_methods(['GET'])
def bet_history_data(request):
    profile = get_object_or_404(Profile, user=request.user)
    history = _bet_history_data(
        request.user,
        profile,
        request.GET.get('outcome', 'all'),
        request.GET.get('page'),
    )
    response = JsonResponse({
        'success': True,
        'outcome': history['outcome'],
        'page': history['page_obj'].number,
        'pages': history['page_obj'].paginator.num_pages,
        'has_previous': history['page_obj'].has_previous(),
        'has_next': history['page_obj'].has_next(),
        'currency': profile.currency,
        'balance': format(profile.balance, '.2f'),
        'total_bets': history['total_bets'],
        'settled_bets': history['settled_bets'],
        'win_rate': round(history['win_rate'], 1),
        'total_staked': format(history['total_staked'], '.2f'),
        'total_returned': format(history['total_returned'], '.2f'),
        'net_result': format(history['net_result'], '.2f'),
        'filter_counts': history['filter_counts'],
        'settle_rounds': history['settle_rounds'],
        'sessions': [
            {
                'id': session.id,
                'created_at': timezone.localtime(session.created_at).strftime('%b %d, %H:%M'),
                'stake': str(session.stake),
                'round_id': session.round.public_id if session.round else None,
                'status': session.history_status,
                'multiplier': (
                    format(session.history_multiplier, '.2f')
                    if session.history_multiplier is not None
                    else None
                ),
                'payout': (
                    format(session.history_payout, '.2f')
                    if session.history_payout is not None
                    else None
                ),
            }
            for session in history['sessions']
        ],
    })
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
def transactions_view(request):
    profile = get_object_or_404(Profile, user=request.user)
    transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')[:100]
    return render(request, 'core/transactions.html', {
        'profile': profile,
        'transactions': transactions,
    })


@login_required
def game_limits(request):
    profile = get_object_or_404(Profile, user=request.user)
    compliance_initial = {
        'phone_number': profile.phone_number,
        'daily_deposit_limit': profile.daily_deposit_limit,
        'daily_loss_limit': profile.daily_loss_limit,
    }
    form = ProfileComplianceForm(initial=compliance_initial)
    exclusion_form = SelfExclusionForm()
    password_form = AccountPasswordChangeForm(request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'password':
            password_form = AccountPasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                password_form.save()
                update_session_auth_hash(request, request.user)
                messages.success(request, 'Your password has been changed.')
                return redirect('game_limits')
        elif action == 'self_exclude':
            exclusion_form = SelfExclusionForm(request.POST)
            if not request.POST.get('confirm_exclusion'):
                exclusion_form.add_error(None, 'Confirm that you understand this action before continuing.')
            if exclusion_form.is_valid():
                duration = exclusion_form.cleaned_data['duration']
                exclusion = activate_self_exclusion(
                    request.user,
                    duration,
                    exclusion_form.cleaned_data['reason'],
                )
                if exclusion:
                    log_audit(request.user, 'self_exclude', 'Created self-exclusion', request, {
                        'exclusion_id': exclusion.id,
                        'permanent': exclusion.permanent,
                    })
                    messages.warning(request, 'Self-exclusion is now active. Deposits and new bets are blocked for the selected period.')
                    return redirect('game_limits')
                exclusion_form.add_error(None, 'Self-exclusion is already active on your account.')
        elif action == 'limits':
            form = ProfileComplianceForm(request.POST)
            if form.is_valid():
                profile.phone_number = form.cleaned_data['phone_number']
                profile.daily_deposit_limit = form.cleaned_data['daily_deposit_limit']
                profile.daily_loss_limit = form.cleaned_data['daily_loss_limit']
                profile.save(update_fields=['phone_number', 'daily_deposit_limit', 'daily_loss_limit'])
                messages.success(request, 'Your limits have been updated.')
                return redirect('game_limits')
    active_exclusion = get_active_self_exclusion(request.user)
    return render(request, 'core/game_limits.html', {
        'profile': profile,
        'form': form,
        'exclusion_form': exclusion_form,
        'active_exclusion': active_exclusion,
        'password_form': password_form,
    })


@login_required
def how_to_play(request):
    profile = get_object_or_404(Profile, user=request.user)
    return render(request, 'core/how_to_play.html', {'profile': profile})


@login_required
def game_rules(request):
    profile = get_object_or_404(Profile, user=request.user)
    return render(request, 'core/game_rules.html', {'profile': profile})


@login_required
def provably_fair(request):
    rounds = GameRound.objects.filter(
        Q(created_by=request.user) | Q(bets__user=request.user),
    ).distinct().order_by('-started_at')[:50]
    rounds = list(rounds)
    for game_round in rounds:
        game_round.verified = (
            game_round.is_revealed
            and verify_game_round(
                game_round.server_seed,
                game_round.client_seed,
                game_round.server_seed_hash,
                game_round.crash_point,
                game_round.house_edge,
            )
        )
    profile = get_object_or_404(Profile, user=request.user)
    return render(request, 'core/provably_fair.html', {
        'rounds': rounds,
        'profile': profile,
    })


@login_required
@require_POST
def update_client_seed(request):
    client_seed = request.POST.get('client_seed', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', client_seed):
        return JsonResponse({
            'success': False,
            'message': 'Use 1–128 letters, numbers, periods, underscores, colons, or hyphens.',
        }, status=400)

    with db_transaction.atomic():
        profile = Profile.objects.select_for_update().get(user=request.user)
        profile.client_seed = client_seed
        profile.save(update_fields=['client_seed'])

    return JsonResponse({
        'success': True,
        'client_seed': profile.client_seed,
        'message': 'Client seed saved. It will be used for your next round.',
    })
