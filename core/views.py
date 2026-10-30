import json
import random
import re
import uuid
import csv
import io
from hmac import compare_digest
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.utils.crypto import get_random_string
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import CharField, Count, Exists, F, OuterRef, Q, Sum
from django.db.models.functions import Cast
from django.http import Http404, JsonResponse, HttpResponse
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
    PlatformBrandingForm,
    PlatformGameSettingsForm,
    AdminAccountCreationForm,
    RoomCreateForm,
    RoomJoinForm,
)
from .chat_rules import find_abusive_chat_term
from .models import (
    AuditLog,
    Profile,
    PlatformSettings,
    GamePresence,
    Referral,
    ChatMessage,
    ChatModerationTerm,
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
    claim_reward,
    ensure_reward_wallet,
    active_participant_count,
    event_payload,
    reward_statistics,
    record_game_presence,
    remove_game_presence,
    send_chat_rain,
    stop_event,
    wallet_payload,
)
from .payment_gateway import PaymentGatewayError, PaymentGatewayResult
from .constants import MAXIMUM_BET_AMOUNT, MAXIMUM_WIN_AMOUNT
from .payment_services import (
    apply_provider_result,
    initiate_deposit,
    initiate_withdrawal,
    sync_payment,
    normalize_mobile_money_phone,
)
from .utils import (
    can_deposit,
    can_place_bets,
    create_game_round,
    generate_financial_report,
    generate_payment_reference,
    log_audit,
    reveal_game_round,
    run_player_risk_checks,
)


ROUND_MULTIPLIER_RATE = Decimal('0.10')
ROUND_BETTING_SECONDS = 4
ROUND_CRASH_DISPLAY_SECONDS = 3


def _recent_chat_messages():
    messages = list(
        ChatMessage.objects.select_related('user')
        .annotate(reaction_count=Count('reactions'))
        .filter(flagged=False)
        .order_by('-created_at', '-id')[:20]
    )
    messages.reverse()
    return messages


def multiplier_at_elapsed_time(started_at, now=None):
    """Return the authoritative multiplier from elapsed wall-clock time."""
    elapsed = max(((now or timezone.now()) - started_at).total_seconds(), 0)
    return (Decimal('1.00') + (Decimal(str(elapsed)) * ROUND_MULTIPLIER_RATE)).quantize(Decimal('0.01'))


def get_platform_settings():
    platform_settings, _ = PlatformSettings.objects.get_or_create(pk=1)
    return platform_settings


def _start_shared_betting_round(game_state, user, request):
    platform_settings = get_platform_settings()
    game_round = create_game_round(
        user if user.is_authenticated else None,
        request,
        max_crash_point=platform_settings.maximum_crash_point,
    )
    game_round.status = 'betting'
    game_round.betting_ends_at = timezone.now() + timedelta(seconds=platform_settings.betting_seconds)
    game_round.started_at = game_round.betting_ends_at
    game_round.save(update_fields=['status', 'betting_ends_at', 'started_at'])
    game_state.active_round = game_round
    game_state.save(update_fields=['active_round'])
    return game_round


def _settle_due_auto_cashouts(game_round, current_multiplier):
    sessions = GameSession.objects.filter(
        round=game_round,
        cashed_out_at__isnull=True,
        auto_cashout_at__lte=current_multiplier,
        auto_cashout_at__lt=game_round.crash_point,
    ).order_by('id')[:100]
    for session in sessions:
        payout = min(
            session.stake * session.auto_cashout_at,
            MAXIMUM_WIN_AMOUNT,
        ).quantize(Decimal('0.01'))
        with db_transaction.atomic():
            Profile.objects.select_for_update().get(user_id=session.user_id)
            current_session = GameSession.objects.select_for_update().get(pk=session.pk)
            if current_session.cashed_out_at is not None:
                continue
            paid_so_far = GameSession.objects.filter(
                round=game_round,
                user_id=session.user_id,
                cashed_out_at__isnull=False,
            ).aggregate(total=Sum('won_amount'))['total'] or Decimal('0.00')
            payout = min(payout, max(MAXIMUM_WIN_AMOUNT - paid_so_far, Decimal('0.00')))
            settled = GameSession.objects.filter(pk=session.pk, cashed_out_at__isnull=True).update(
                cashed_out_at=session.auto_cashout_at,
                won_amount=payout,
            )
            if not settled:
                continue
            if payout > 0:
                Profile.objects.filter(user_id=session.user_id).update(balance=F('balance') + payout)
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


def _recent_round_history_payload():
    return [
        {
            'round_id': public_id,
            'crash_point': float(crash_point),
        }
        for public_id, crash_point in GameRound.objects.filter(
            status='crashed',
        ).order_by('-crashed_at', '-started_at').values_list(
            'public_id',
            'crash_point',
        )[:50]
    ]


def _shared_round_payload(user, request=None):
    platform_settings = get_platform_settings()
    SharedGameState.objects.get_or_create(pk=1)
    with db_transaction.atomic():
        SharedGameState.objects.filter(pk=1).update(active_round=F('active_round'))
        game_state = SharedGameState.objects.select_for_update().select_related('active_round').get(pk=1)
        game_round = game_state.active_round
        now = timezone.now()
        crash_display_finished = (
            game_round
            and game_round.status == 'crashed'
            and game_round.crashed_at
            and now >= game_round.crashed_at + timedelta(seconds=ROUND_CRASH_DISPLAY_SECONDS)
        )
        if not game_round or game_round.status == 'void' or crash_display_finished:
            game_round = _start_shared_betting_round(game_state, user, request)

        now = timezone.now()
        if game_round.status == 'betting' and game_round.betting_ends_at:
            if now >= game_round.betting_ends_at:
                GameRound.objects.filter(
                    pk=game_round.pk,
                    status='betting',
                    betting_ends_at__lte=now,
                ).update(status='flying', started_at=F('betting_ends_at'))
                game_round.refresh_from_db()
            else:
                sessions = list(
                    GameSession.objects.filter(user=user, round=game_round).order_by('created_at', 'id')
                ) if user.is_authenticated else []
                return {
                    'success': True,
                    'round_id': game_round.public_id,
                    'status': 'betting',
                    'seconds_remaining': max((game_round.betting_ends_at - now).total_seconds(), 0),
                    'betting_ends_at': game_round.betting_ends_at.isoformat(),
                    'server_time': now.isoformat(),
                    'server_seed_hash': game_round.server_seed_hash,
                    'multiplier_rate': float(ROUND_MULTIPLIER_RATE),
                    'max_crash_point': float(platform_settings.maximum_crash_point),
                    'recent_crashes': _recent_round_history_payload(),
                    'session_ids': [session.id for session in sessions if session.cashed_out_at is None],
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

        if game_round.status == 'crashed':
            sessions = list(
                GameSession.objects.filter(user=user, round=game_round).order_by('created_at', 'id')
            ) if user.is_authenticated else []
            return {
                'success': True,
                'round_id': game_round.public_id,
                'status': 'crashed',
                'current_multiplier': float(game_round.crash_point),
                'server_time': now.isoformat(),
                'server_seed_hash': game_round.server_seed_hash,
                'multiplier_rate': float(ROUND_MULTIPLIER_RATE),
                'max_crash_point': float(platform_settings.maximum_crash_point),
                'recent_crashes': _recent_round_history_payload(),
                'session_ids': [],
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
                'crash_point': float(game_round.crash_point),
                'server_seed': game_round.server_seed,
                'client_seed': game_round.client_seed,
            }

        if game_round.status != 'flying':
            return {
                'success': True,
                'status': 'waiting',
                'multiplier_rate': float(ROUND_MULTIPLIER_RATE),
                'max_crash_point': float(platform_settings.maximum_crash_point),
                'recent_crashes': _recent_round_history_payload(),
            }

        current_multiplier = multiplier_at_elapsed_time(game_round.started_at, now)
        _settle_due_auto_cashouts(game_round, current_multiplier)
        if current_multiplier >= game_round.crash_point:
            reveal_game_round(game_round)
            game_round.refresh_from_db()

        sessions = list(
            GameSession.objects.filter(user=user, round=game_round).order_by('created_at', 'id')
        ) if user.is_authenticated else []
        payload = {
            'success': True,
            'round_id': game_round.public_id,
            'status': game_round.status,
            'current_multiplier': float(min(current_multiplier, game_round.crash_point)),
            'server_time': now.isoformat(),
            'server_seed_hash': game_round.server_seed_hash,
            'multiplier_rate': float(ROUND_MULTIPLIER_RATE),
            'max_crash_point': float(platform_settings.maximum_crash_point),
            'recent_crashes': _recent_round_history_payload(),
            'session_ids': [session.id for session in sessions if session.cashed_out_at is None],
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


def _round_ledger_payload(user, tab):
    if tab == 'mine':
        bets = GameSession.objects.filter(user=user).select_related('user', 'round')
        total_count = bets.count()
        rows = [
            {
                'user': session.user.username,
                'avatar': session.user.username[:1].upper(),
                'bet': float(session.stake),
                'multiplier': (
                    float(session.cashed_out_at) if session.cashed_out_at is not None
                    else float(session.round.crash_point)
                    if session.round and session.round.status == 'crashed'
                    else None
                ),
                'win': float(session.stake if session.admin_refunded_at else session.won_amount),
                'status': (
                    'refunded' if session.admin_refunded_at
                    else 'void' if session.round and session.round.status == 'void'
                    else 'lost' if session.round and session.round.status == 'crashed' and session.cashed_out_at is None
                    else 'pending' if session.cashed_out_at is None
                    else 'won' if session.won_amount > 0
                    else 'lost'
                ),
            }
            for session in bets.order_by('-created_at', '-id')[:30]
        ]
        state = SharedGameState.objects.select_related('active_round').filter(pk=1).first()
        return {
            'success': True,
            'round_id': state.active_round.public_id if state and state.active_round else None,
            'total_count': total_count,
            'tab_count': min(total_count, 30),
            'rows': rows,
            'has_previous': False,
            'has_next': False,
            'previous_cursor': None,
            'next_cursor': None,
        }

    state = SharedGameState.objects.select_related('active_round').filter(pk=1).first()
    game_round = state.active_round if state else None
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

    bets = GameSession.objects.filter(round=game_round).select_related('user')
    if tab == 'top':
        bets = bets.filter(cashed_out_at__isnull=False, won_amount__gt=0)
        ordering = ('-won_amount', '-stake', 'id')
    else:
        ordering = ('-stake', 'id')

    total_count = game_round.bet_count
    tab_count = total_count if tab == 'all' else game_round.winning_bet_count
    rows = [
        {
            'user': session.user.username,
            'avatar': session.user.username[:1].upper(),
            'bet': float(session.stake),
            'multiplier': (
                float(session.cashed_out_at) if session.cashed_out_at is not None
                else float(game_round.crash_point) if game_round.status == 'crashed'
                else None
            ),
            'win': float(session.stake if session.admin_refunded_at else session.won_amount),
            'status': (
                'refunded' if session.admin_refunded_at
                else 'void' if game_round.status == 'void'
                else 'lost' if session.cashed_out_at is None and game_round.status == 'crashed'
                else 'pending' if session.cashed_out_at is None
                else 'won' if session.won_amount > 0
                else 'lost'
            ),
        }
        for session in bets.order_by(*ordering)[:50]
    ]
    return {
        'success': True,
        'round_id': game_round.public_id,
        'total_count': total_count,
        'tab_count': tab_count,
        'rows': rows,
        'has_previous': False,
        'has_next': False,
        'previous_cursor': None,
        'next_cursor': None,
    }


def home(request):
    return render(request, 'core/home.html')


def online_count(request):
    return JsonResponse({'online_count': active_participant_count()})


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
            return json.loads(request.body.decode() or '{}')
        except json.JSONDecodeError:
            return {}
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
    data = request_payload(request)
    event_id = data.get('event_id') or data.get('id')
    if not event_id:
        return JsonResponse({'success': False, 'message': 'event_id is required.'}, status=400)
    try:
        claim, created = claim_reward(
            request.user,
            event_id,
            ip_address=client_ip(request),
            device_identifier=data.get('device_identifier') or request.META.get('HTTP_USER_AGENT', ''),
            idempotency_key=data.get('idempotency_key') or request.META.get('HTTP_IDEMPOTENCY_KEY', ''),
        )
    except RewardEvent.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Reward event was not found.'}, status=404)
    except RewardError as exc:
        return JsonResponse({'success': False, 'message': exc.message}, status=exc.status)
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
            messages.success(request, 'Welcome to Pakabet! Your account is ready.')
            return redirect('play')
    else:
        form = RegisterForm()
    return render(request, 'core/register.html', {'form': form})


@login_required
def dashboard(request):
    profile = get_object_or_404(Profile, user=request.user)
    sessions = GameSession.objects.filter(user=request.user).order_by('-created_at')[:8]
    rooms = Room.objects.filter(status='waiting').order_by('-created_at')[:4]
    form = PlayForm()
    return render(request, 'core/dashboard.html', {
        'profile': profile,
        'sessions': sessions,
        'rooms': rooms,
        'form': form,
    })


@login_required
def play(request):
    profile = get_object_or_404(Profile, user=request.user)
    highest_stakers = GameSession.objects.filter(stake__gt=0).order_by('-stake')[:6]
    recent_sessions = GameSession.objects.filter(user=request.user).order_by('-created_at')[:5]
    recent_crashes = GameRound.objects.filter(status='crashed').order_by('-crashed_at', '-started_at')[:10]
    chat_messages = _recent_chat_messages()

    if request.method == 'GET':
        return render(request, 'core/play.html', {
            'profile': profile,
            'maximum_bet': MAXIMUM_BET_AMOUNT,
            'recent_sessions': recent_sessions,
            'highest_stakers': highest_stakers,
            'recent_crashes': recent_crashes,
            'chat_messages': chat_messages,
            'chat_form': ChatMessageForm(),
        })

    action = request.POST.get('action')
    if action == 'state':
        return JsonResponse(_shared_round_payload(request.user, request))

    if action == 'ledger':
        tab = request.POST.get('tab', 'all')
        if tab not in {'all', 'mine', 'top'}:
            return JsonResponse({'success': False, 'message': 'Invalid bet list view.'}, status=400)
        return JsonResponse(_round_ledger_payload(request.user, tab))

    if action == 'presence':
        presence_key = request.POST.get('presence_key', '')[:32]
        if not presence_key:
            return JsonResponse({'success': False, 'message': 'Presence key is required.'}, status=400)
        record_game_presence(request.user, presence_key)
        return JsonResponse({'success': True})

    if action == 'presence_leave':
        remove_game_presence(request.user, request.POST.get('presence_key', '')[:32])
        return JsonResponse({'success': True})

    if action == 'chat':
        form = ChatMessageForm(request.POST)
        if not form.is_valid():
            message = request.POST.get('message', '')
            matched_term = find_abusive_chat_term(message)
            if matched_term:
                RiskAlert.objects.create(
                    user=request.user,
                    alert_type='chat_abuse_blocked',
                    severity='medium',
                    description='A chat message containing a configured abusive term was blocked.',
                    data={
                        'message': message[:280],
                        'matched_term': matched_term,
                    },
                )
                log_audit(
                    request.user,
                    'chat_message',
                    'Blocked an abusive chat message.',
                    request,
                    {'matched_term': matched_term},
                )
                return JsonResponse({
                    'success': False,
                    'message': 'Abusive language and profanity are not allowed in chat. Your message was not sent.',
                }, status=400)
            errors = form.errors.get('message')
            return JsonResponse({
                'success': False,
                'message': str(errors[0]) if errors else 'Please enter a valid chat message.',
            }, status=400)

        message = form.cleaned_data['message']
        flagged = any(term in message.lower() for term in ['scam', 'fraud', 'hack'])
        chat = ChatMessage.objects.create(user=request.user, message=message, flagged=flagged)
        prune_chat_messages_if_needed()
        if flagged:
            RiskAlert.objects.create(
                user=request.user,
                alert_type='flagged_chat',
                severity='medium',
                description='Chat message was flagged for operator review.',
                data={'message_id': chat.id, 'message': message},
            )
        log_audit(request.user, 'chat_message', 'Posted chat message', request, {'message_id': chat.id, 'flagged': flagged})
        return JsonResponse({
            'success': True,
            'message': {
                'username': request.user.username,
                'id': chat.id,
                'text': chat.message,
                'created_at': chat.created_at.strftime('%H:%M'),
                'flagged': chat.flagged,
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
            return JsonResponse({'success': False, 'message': 'Please place at least one bet.'}, status=400)

        try:
            bets = json.loads(bet_data)
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'message': 'Invalid bet data.'}, status=400)

        if not isinstance(bets, list):
            return JsonResponse({'success': False, 'message': 'Invalid bet data.'}, status=400)

        parsed_bets = []
        for value in bets[:2]:
            try:
                if isinstance(value, dict):
                    stake_value = Decimal(str(value.get('amount'))).quantize(Decimal('0.01'))
                    request_id = str(value.get('request_id') or '').strip()
                    auto_cashout = value.get('auto_cashout_at')
                    auto_cashout_at = (
                        Decimal(str(auto_cashout)).quantize(Decimal('0.01'))
                        if auto_cashout not in (None, '')
                        else None
                    )
                else:
                    stake_value = Decimal(str(value)).quantize(Decimal('0.01'))
                    request_id = ''
                    auto_cashout_at = None
            except (InvalidOperation, ValueError):
                continue
            if stake_value > 0:
                if request_id and len(request_id) > 32:
                    return JsonResponse({'success': False, 'message': 'Invalid bet request identifier.'}, status=400)
                if auto_cashout_at is not None and auto_cashout_at < Decimal('1.01'):
                    return JsonResponse({'success': False, 'message': 'Auto cashout must be at least 1.01x.'}, status=400)
                parsed_bets.append({
                    'stake': stake_value,
                    'request_id': request_id,
                    'auto_cashout_at': auto_cashout_at,
                })

        if not parsed_bets:
            return JsonResponse({'success': False, 'message': 'Enter at least one valid bet amount.'}, status=400)
        request_ids = [bet['request_id'] for bet in parsed_bets if bet['request_id']]
        if len(request_ids) != len(set(request_ids)):
            return JsonResponse({'success': False, 'message': 'Each bet must have a unique request identifier.'}, status=400)

        with db_transaction.atomic():
            SharedGameState.objects.get_or_create(pk=1)
            SharedGameState.objects.filter(pk=1).update(active_round=F('active_round'))
            game_state = SharedGameState.objects.select_for_update().select_related('active_round').get(pk=1)
            game_round = game_state.active_round
            if not game_round or game_round.status in {'crashed', 'void'}:
                game_round = _start_shared_betting_round(game_state, request.user, request)
            if game_round.status == 'betting' and game_round.betting_ends_at and timezone.now() >= game_round.betting_ends_at:
                GameRound.objects.filter(pk=game_round.pk, status='betting').update(
                    status='flying',
                    started_at=F('betting_ends_at'),
                )
                return JsonResponse({
                    'success': False,
                    'code': 'betting_closed',
                    'message': 'Betting is closed for this round. Your balance was not changed.',
                    'round_id': game_round.public_id,
                    'status': 'flying',
                }, status=409)
            if game_round.status != 'betting':
                return JsonResponse({
                    'success': False,
                    'code': 'betting_closed',
                    'message': 'Betting is closed for this round. Your balance was not changed.',
                    'round_id': game_round.public_id,
                    'status': game_round.status,
                }, status=409)

            locked_profile = Profile.objects.select_for_update().get(user=request.user)
            existing_sessions = {
                session.bet_request_id: session
                for session in GameSession.objects.filter(
                    user=request.user,
                    bet_request_id__in=request_ids,
                )
            }
            bet_sessions = []
            new_bets = []
            for bet in parsed_bets:
                existing = existing_sessions.get(bet['request_id']) if bet['request_id'] else None
                if existing:
                    if (
                        existing.round_id != game_round.id
                        or existing.stake != bet['stake']
                        or existing.auto_cashout_at != bet['auto_cashout_at']
                    ):
                        return JsonResponse({
                            'success': False,
                            'message': 'This bet request was already used for a different bet.',
                        }, status=409)
                    bet_sessions.append(existing)
                else:
                    bet_sessions.append(None)
                    new_bets.append(bet)

            total_stake = sum((bet['stake'] for bet in new_bets), Decimal('0.00'))
            existing_round_stake = GameSession.objects.filter(
                user=request.user,
                round=game_round,
            ).aggregate(total=Sum('stake'))['total'] or Decimal('0.00')
            if existing_round_stake + total_stake > MAXIMUM_BET_AMOUNT:
                return JsonResponse({
                    'success': False,
                    'code': 'bet_limit_exceeded',
                    'message': f'The maximum combined bet per round is {MAXIMUM_BET_AMOUNT:,.2f} {locked_profile.currency}.',
                    'maximum_bet': float(MAXIMUM_BET_AMOUNT),
                    'round_stake': float(existing_round_stake),
                }, status=400)
            allowed, reason = can_place_bets(locked_profile, total_stake)
            if new_bets and not allowed:
                return JsonResponse({'success': False, 'message': reason}, status=403)
            if total_stake > locked_profile.balance:
                return JsonResponse({
                    'success': False,
                    'code': 'insufficient_balance',
                    'message': 'Insufficient balance for this bet.',
                    'balance': float(locked_profile.balance),
                }, status=400)

            if new_bets:
                locked_profile.balance -= total_stake
                locked_profile.save(update_fields=['balance'])
                Transaction.objects.create(
                    user=request.user,
                    transaction_type='bet',
                    amount=total_stake,
                    status='completed',
                    reference=game_round.public_id,
                    description=f'Round {game_round.public_id} bet debit',
                )
                new_bet_index = 0
                for index, bet in enumerate(parsed_bets):
                    if bet_sessions[index] is not None:
                        continue
                    bet_sessions[index] = GameSession.objects.create(
                        user=request.user,
                        round=game_round,
                        stake=bet['stake'],
                        crash_point=game_round.crash_point,
                        auto_cashout_at=bet['auto_cashout_at'],
                        bet_request_id=bet['request_id'],
                    )
                    new_bet_index += 1
                GameRound.objects.filter(pk=game_round.pk).update(bet_count=F('bet_count') + new_bet_index)
                game_round.refresh_from_db()
                run_player_risk_checks(request.user)

        created_sessions = [session.id for session in bet_sessions]
        log_audit(request.user, 'game_start', f'Placed {len(new_bets)} bets totaling {total_stake:.2f} {profile.currency}', request, {
            'stakes': [str(bet['stake']) for bet in parsed_bets],
            'session_ids': created_sessions,
            'round_id': game_round.public_id,
            'server_seed_hash': game_round.server_seed_hash,
            'client_seed': game_round.client_seed,
        })
        seconds_remaining = max((game_round.betting_ends_at - timezone.now()).total_seconds(), 0)

        return JsonResponse({
            'success': True,
            'session_ids': created_sessions,
            'sessions': [
                {
                    'id': session.id,
                    'stake': float(session.stake),
                    'auto_cashout_at': float(session.auto_cashout_at) if session.auto_cashout_at is not None else None,
                    'cashed_out_at': float(session.cashed_out_at) if session.cashed_out_at is not None else None,
                    'won_amount': float(session.won_amount),
                    'bet_request_id': session.bet_request_id,
                }
                for session in bet_sessions
            ],
            'round_id': game_round.public_id,
            'server_seed_hash': game_round.server_seed_hash,
            'client_seed': game_round.client_seed,
            'balance': float(locked_profile.balance),
            'stake_total': float(total_stake),
            'status': 'betting',
            'betting_ends_at': game_round.betting_ends_at.isoformat(),
            'seconds_remaining': seconds_remaining,
        })

    if action == 'cashout':
        session_ids = request.POST.get('session_ids', '')
        requested_cashout_value = request.POST.get('cashout_multiplier')
        try:
            requested_cashout_multiplier = (
                Decimal(requested_cashout_value).quantize(Decimal('0.01'))
                if requested_cashout_value
                else None
            )
        except InvalidOperation:
            return JsonResponse({'success': False, 'message': 'Invalid cashout multiplier.'}, status=400)

        if not session_ids:
            return JsonResponse({'success': False, 'message': 'No active bets available.'}, status=400)

        session_ids = [int(x) for x in session_ids.split(',') if x.isdigit()]
        with db_transaction.atomic():
            candidate_round_ids = set(
                GameSession.objects.filter(id__in=session_ids, user=request.user)
                .values_list('round_id', flat=True)
            )
            if len(candidate_round_ids) != 1 or None in candidate_round_ids:
                return JsonResponse({'success': False, 'message': 'These bets do not belong to one verifiable round.'}, status=400)
            game_round = GameRound.objects.select_for_update().filter(
                pk=candidate_round_ids.pop(),
            ).first()
            if not game_round:
                return JsonResponse({'success': False, 'message': 'This round cannot be verified.'}, status=400)
            if game_round.status == 'void':
                return JsonResponse({'success': False, 'message': 'This round was voided and open stakes were refunded.'}, status=409)

            locked_profile = Profile.objects.select_for_update().get(user=request.user)
            sessions = list(
                GameSession.objects.select_for_update()
                .filter(id__in=session_ids, user=request.user, round=game_round)
            )
            if not sessions:
                return JsonResponse({'success': False, 'message': 'Invalid games to cash out.'}, status=400)

            server_multiplier = multiplier_at_elapsed_time(game_round.started_at)
            if requested_cashout_multiplier is None:
                requested_cashout_multiplier = server_multiplier
            effective_multiplier = min(requested_cashout_multiplier, server_multiplier).quantize(Decimal('0.01'))
            round_has_crashed = server_multiplier >= game_round.crash_point

            total_won = Decimal('0.00')
            total_stake = Decimal('0.00')
            all_lost = True
            settled_count = 0
            winning_count = 0
            paid_so_far = GameSession.objects.filter(
                round=game_round,
                user=request.user,
                cashed_out_at__isnull=False,
            ).aggregate(total=Sum('won_amount'))['total'] or Decimal('0.00')
            remaining_win_cap = max(MAXIMUM_WIN_AMOUNT - paid_so_far, Decimal('0.00'))

            for session in sessions:
                if session.cashed_out_at is not None:
                    continue

                settled_count += 1
                session.cashed_out_at = effective_multiplier
                session.won_amount = Decimal('0.00')
                total_stake += session.stake

                if not round_has_crashed and effective_multiplier < session.crash_point:
                    session.won_amount = min(
                        session.stake * effective_multiplier,
                        remaining_win_cap,
                    ).quantize(Decimal('0.01'))
                    total_won += session.won_amount
                    remaining_win_cap -= session.won_amount
                    if session.won_amount > 0:
                        all_lost = False
                        winning_count += 1

                session.save(update_fields=['cashed_out_at', 'won_amount'])

            if settled_count == 0:
                return JsonResponse({'success': False, 'message': 'This bet has already been cashed out.'}, status=409)

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
                GameRound.objects.filter(pk=game_round.pk).update(
                    winning_bet_count=F('winning_bet_count') + winning_count,
                )
                message = f'You cashed out {total_won:.2f} {profile.currency} successfully!'
            else:
                message = 'The flight crashed before cash out. Better luck next round.'

            if round_has_crashed:
                reveal_game_round(game_round)

        log_audit(request.user, 'game_cashout', f'Cashed out {len(session_ids)} bets at {effective_multiplier:.2f}x', request, {
            'session_ids': session_ids,
            'round_id': game_round.public_id,
            'requested_cashout_multiplier': str(requested_cashout_multiplier),
            'server_multiplier': str(server_multiplier),
            'cashout_multiplier': str(effective_multiplier),
            'total_won': str(total_won),
            'all_lost': all_lost,
        })

        return JsonResponse({
            'success': True,
            'message': message,
            'round_id': game_round.public_id,
            'crash_point': float(game_round.crash_point) if round_has_crashed else None,
            'cashout_multiplier': float(effective_multiplier),
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
        with db_transaction.atomic():
            game_round = get_object_or_404(
                GameRound.objects.select_for_update(),
                public_id=round_id,
            )
            is_shared_round = SharedGameState.objects.filter(pk=1, active_round=game_round).exists()
            if not is_shared_round and not GameRound.objects.filter(
                Q(created_by=request.user) | Q(bets__user=request.user),
                pk=game_round.pk,
            ).exists():
                raise Http404
            if game_round.status == 'void':
                return JsonResponse({
                    'success': False,
                    'message': 'This round was voided and open stakes were refunded.',
                }, status=409)
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


@login_required
def wallet(request):
    profile = get_object_or_404(Profile, user=request.user)
    initial_phone = {'phone_number': profile.phone_number}
    deposit_form = WalletForm(prefix='deposit', initial=initial_phone)
    withdraw_form = WalletForm(prefix='withdraw', initial=initial_phone)

    if request.method == 'POST':
        action = request.POST.get('action')
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
                    )
                    log_audit(request.user, 'deposit', f'Requested deposit {amount:.2f} {profile.currency}', request, {
                        'provider': provider,
                        'payment_reference': payment.external_reference if payment else '',
                        'provider_status': result.status if result else '',
                    })
                    run_player_risk_checks(request.user)
                    if error:
                        if payment:
                            if payment.status in ['pending', 'processing']:
                                messages.warning(request, 'Your deposit request is being confirmed by the provider. Do not submit it again; refresh its status below.')
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
            withdraw_form = WalletForm(request.POST, prefix='withdraw', initial=initial_phone)
            if withdraw_form.is_valid():
                amount = withdraw_form.cleaned_data['amount']
                provider = withdraw_form.cleaned_data['provider']
                phone_number = withdraw_form.cleaned_data['phone_number']
                payment, result, error = initiate_withdrawal(
                    user=request.user,
                    amount=amount,
                    provider=provider,
                    phone_number=phone_number,
                )
                if error:
                    if payment:
                        log_audit(request.user, 'withdraw', f'Requested withdrawal {amount:.2f} {profile.currency}', request, {
                            'provider': provider,
                            'payment_reference': payment.external_reference,
                            'provider_status': result.status if result else payment.status,
                        })
                        if payment.status in ['pending', 'processing']:
                            messages.warning(request, 'Your withdrawal request is awaiting provider confirmation. The amount remains on hold; do not submit it again.')
                        else:
                            messages.error(request, 'The provider rejected the withdrawal request. The amount has been returned to your balance.')
                        return redirect('wallet')
                    withdraw_form.add_error(None, error)
                else:
                    log_audit(request.user, 'withdraw', f'Requested withdrawal {amount:.2f} {profile.currency}', request, {
                        'provider': provider,
                        'payment_reference': payment.external_reference,
                        'provider_status': result.status if result else '',
                    })
                    if payment.status == 'completed':
                        messages.success(request, f'{amount:.2f} {profile.currency} withdrawal completed via {payment.get_provider_display()}.')
                    elif payment.status in ['failed', 'cancelled']:
                        messages.error(request, 'The provider did not complete the withdrawal. The held funds have been returned to your balance.')
                    else:
                        messages.info(request, f'{amount:.2f} {profile.currency} withdrawal requested via {payment.get_provider_display()}. Status: {payment.get_status_display()}.')
                    return redirect('wallet')

    transactions = Transaction.objects.filter(user=request.user)
    payment_requests = PaymentRequest.objects.filter(user=request.user)[:10]
    payment_mode = getattr(settings, 'IOTEC_PAY_MODE', 'sandbox').lower()
    return render(request, 'core/wallet.html', {
        'profile': profile,
        'deposit_form': deposit_form,
        'withdraw_form': withdraw_form,
        'transactions': transactions,
        'payment_requests': payment_requests,
        'payment_mode': payment_mode,
    })


@csrf_exempt
@require_POST
def payment_callback(request):
    expected_token = getattr(settings, 'IOTEC_PAY_CALLBACK_TOKEN', '')
    provided_token = request.headers.get('X-Pakabet-Webhook-Token', '') or request.headers.get('X-IOTEC-Webhook-Token', '')
    if expected_token and not compare_digest(expected_token, provided_token):
        return JsonResponse({'success': False, 'message': 'Invalid callback token.'}, status=403)
    if not expected_token and not settings.DEBUG:
        return JsonResponse({'success': False, 'message': 'Callback token is required in production.'}, status=403)

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
        action = request.POST.get('action')
        if action == 'add_chat_term':
            term = ' '.join(request.POST.get('term', '').split())
            if (
                not term
                or len(term) > 120
                or not re.search(r'[a-z0-9]', term, re.IGNORECASE)
                or any(ord(char) < 32 for char in term)
            ):
                messages.error(request, 'Enter a moderation term of 1 to 120 characters.')
            else:
                existing_term = ChatModerationTerm.objects.filter(term__iexact=term).first()
                if existing_term:
                    if not existing_term.is_active:
                        existing_term.is_active = True
                        existing_term.save(update_fields=['is_active', 'updated_at'])
                    messages.info(request, 'That moderation term is already configured and active.')
                else:
                    ChatModerationTerm.objects.create(term=term)
                    messages.success(request, 'Chat moderation term added.')
                    log_audit(request.user, 'admin_action', 'Added chat moderation term.', request)
            return redirect('admin_dashboard')
        if action == 'toggle_chat_term':
            term = get_object_or_404(ChatModerationTerm, pk=request.POST.get('term_id'))
            term.is_active = not term.is_active
            term.save(update_fields=['is_active', 'updated_at'])
            messages.success(request, f'Moderation term {"activated" if term.is_active else "deactivated"}.')
            log_audit(request.user, 'admin_action', 'Changed chat moderation term status.', request, {
                'term_id': term.pk,
                'is_active': term.is_active,
            })
            return redirect('admin_dashboard')
        if action == 'resolve_chat_alert':
            alert = get_object_or_404(
                RiskAlert,
                pk=request.POST.get('alert_id'),
                alert_type='chat_abuse_blocked',
            )
            if alert.status == 'open':
                alert.status = 'resolved'
                alert.resolved_at = timezone.now()
                alert.save(update_fields=['status', 'resolved_at'])
                log_audit(request.user, 'admin_action', 'Resolved chat moderation alert.', request, {
                    'alert_id': alert.pk,
                })
            messages.info(request, 'Chat moderation alert resolved.')
            return redirect('admin_dashboard')
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
    pending_withdrawals = PaymentRequest.objects.filter(payment_type='withdraw', status='pending').count()
    open_risk_alerts = RiskAlert.objects.filter(status='open').count()
    open_chat_abuse_alerts = RiskAlert.objects.filter(
        status='open',
        alert_type='chat_abuse_blocked',
    ).count()
    recent_chat_abuse_alerts = RiskAlert.objects.filter(
        alert_type='chat_abuse_blocked',
    ).select_related('user').order_by('-created_at')[:10]
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
        'open_risk_alerts': open_risk_alerts,
        'open_chat_abuse_alerts': open_chat_abuse_alerts,
        'recent_chat_abuse_alerts': recent_chat_abuse_alerts,
        'chat_moderation_terms': ChatModerationTerm.objects.all(),
        'latest_reports': latest_reports,
        'report_form': report_form,
        'active_rooms': active_rooms,
        'running_rooms': running_rooms,
        'room_count': room_count,
    })


def revoke_admin_sessions(user):
    session_keys = []
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator(chunk_size=200):
        if str(session.get_decoded().get('_auth_user_id', '')) == str(user.pk):
            session_keys.append(session.session_key)
    if session_keys:
        Session.objects.filter(session_key__in=session_keys).delete()


def export_admin_records(section, rows):
    columns = {
        'users': ('username', 'phone', 'wallet_balance', 'currency', 'account_status', 'account_note', 'created_at'),
        'payments': ('created_at', 'username', 'payment_type', 'provider', 'amount', 'status', 'external_reference', 'provider_reference'),
        'transactions': ('created_at', 'username', 'transaction_type', 'amount', 'status', 'reference', 'description'),
        'players': ('created_at', 'username', 'round_id', 'stake', 'cashed_out_at', 'won_amount', 'player_online'),
        'rounds': ('started_at', 'public_id', 'status', 'crash_point', 'bet_count', 'winning_bet_count', 'server_seed_hash', 'revealed_at'),
        'live': ('started_at', 'public_id', 'status', 'crash_point', 'bet_count', 'winning_bet_count', 'server_seed_hash', 'revealed_at'),
        'reports': ('report_type', 'period_start', 'period_end', 'total_deposits', 'total_withdrawals', 'total_bets', 'total_wins', 'gross_gaming_revenue', 'ura_ggr_tax', 'generated_by', 'created_at'),
        'risk': ('created_at', 'username', 'alert_type', 'severity', 'status', 'description', 'data'),
        'safeguards': ('created_at', 'username', 'starts_at', 'ends_at', 'permanent', 'reason'),
        'security': ('created_at', 'event', 'username', 'ip_address', 'description', 'data'),
        'admins': ('username', 'phone', 'is_superuser', 'is_active', 'last_login', 'date_joined'),
    }
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="admin-{section}-{timezone.localdate().isoformat()}.csv"'
    response.write('\ufeff')
    writer = csv.writer(response)
    fields = columns[section]
    writer.writerow(fields)

    def safe_cell(value):
        if value is None:
            return ''
        value = str(value)
        if value.lstrip(' \t\r\n').startswith(('=', '+', '-', '@')):
            return "'" + value
        return value

    for record in rows:
        profile = getattr(record, 'profile', None) if section == 'admins' else None
        values = {
            'phone': record.phone_number if section == 'users' else profile.phone_number if profile else '',
            'wallet_balance': record.balance if section == 'users' else '',
            'currency': record.currency if section == 'users' else '',
            'account_status': record.get_account_status_display() if section == 'users' else '',
            'account_note': record.account_note if section == 'users' else '',
            'created_at': getattr(record, 'created_at', ''),
            'payment_type': record.get_payment_type_display() if section == 'payments' else '',
            'provider': record.get_provider_display() if section == 'payments' else '',
            'amount': getattr(record, 'amount', ''),
            'status': record.get_status_display() if section in {'payments', 'risk', 'rounds', 'live'} else getattr(record, 'status', ''),
            'external_reference': getattr(record, 'external_reference', ''),
            'provider_reference': getattr(record, 'provider_reference', ''),
            'transaction_type': record.get_transaction_type_display() if section == 'transactions' else '',
            'reference': getattr(record, 'reference', ''),
            'description': getattr(record, 'description', ''),
            'round_id': record.round.public_id if section == 'players' and record.round else '',
            'stake': getattr(record, 'stake', ''),
            'cashed_out_at': getattr(record, 'cashed_out_at', ''),
            'won_amount': getattr(record, 'won_amount', ''),
            'player_online': getattr(record, 'player_online', ''),
            'started_at': getattr(record, 'started_at', ''),
            'public_id': getattr(record, 'public_id', ''),
            'crash_point': getattr(record, 'crash_point', ''),
            'bet_count': getattr(record, 'bet_count', ''),
            'winning_bet_count': getattr(record, 'winning_bet_count', ''),
            'server_seed_hash': getattr(record, 'server_seed_hash', ''),
            'revealed_at': getattr(record, 'revealed_at', ''),
            'report_type': record.get_report_type_display() if section == 'reports' else '',
            'period_start': getattr(record, 'period_start', ''),
            'period_end': getattr(record, 'period_end', ''),
            'total_deposits': getattr(record, 'total_deposits', ''),
            'total_withdrawals': getattr(record, 'total_withdrawals', ''),
            'total_bets': getattr(record, 'total_bets', ''),
            'total_wins': getattr(record, 'total_wins', ''),
            'gross_gaming_revenue': getattr(record, 'gross_gaming_revenue', ''),
            'ura_ggr_tax': getattr(record, 'ura_ggr_tax', ''),
            'generated_by': record.generated_by.username if section == 'reports' and record.generated_by else '',
            'username': record.user.username if section in {'users', 'payments', 'transactions', 'players', 'risk', 'safeguards'} else record.username if section == 'admins' else record.user.username if section == 'security' and record.user else '',
            'alert_type': getattr(record, 'alert_type', ''),
            'severity': record.get_severity_display() if section == 'risk' else '',
            'data': json.dumps(getattr(record, 'data', {}), sort_keys=True),
            'starts_at': getattr(record, 'starts_at', ''),
            'ends_at': getattr(record, 'ends_at', ''),
            'permanent': getattr(record, 'permanent', ''),
            'event': record.data.get('event', '') if section == 'security' else '',
            'ip_address': getattr(record, 'ip_address', ''),
            'is_superuser': getattr(record, 'is_superuser', ''),
            'is_active': getattr(record, 'is_active', ''),
            'last_login': getattr(record, 'last_login', ''),
            'date_joined': getattr(record, 'date_joined', ''),
        }
        writer.writerow([safe_cell(values.get(field, '')) for field in fields])
    return response


@login_required
def admin_console_section(request, section):
    if not request.user.is_staff:
        return redirect('dashboard')

    section_config = {
        'users': ('Player accounts', 'Account access, phone numbers, and wallet balances.'),
        'payments': ('Payment operations', 'Provider requests and their latest recorded status.'),
        'transactions': ('Transaction ledger', 'Immutable wallet and game transaction records.'),
        'players': ('Round players', 'Inspect player sessions and account activity for a selected round.'),
        'live': ('Live game monitor', 'Observe the current shared flight and recent round history.'),
        'rounds': ('Game integrity', 'Round outcomes and public fairness commitments.'),
        'reports': ('Financial reports', 'Generated GGR and URA reporting periods.'),
        'risk': ('Risk review', 'Investigate and disposition open platform alerts.'),
        'safeguards': ('Player safeguards', 'Self-exclusion requests and their active periods.'),
        'security': ('Security operations', 'Authentication events and current security posture.'),
        'admins': ('Administrator access', 'Create and review platform operations accounts.'),
        'settings': ('Platform settings', 'Manage branding and approved game-round operating limits.'),
    }
    if section not in section_config:
        raise Http404

    branding_form = None
    game_settings_form = None
    admin_account_form = None
    platform_settings = None
    if section == 'settings':
        platform_settings = get_platform_settings()
        branding_form = PlatformBrandingForm(instance=platform_settings)
        game_settings_form = PlatformGameSettingsForm(instance=platform_settings)
        if request.method == 'POST':
            action = request.POST.get('action')
            if action == 'save_branding':
                branding_form = PlatformBrandingForm(
                    request.POST,
                    request.FILES,
                    instance=platform_settings,
                )
                if branding_form.is_valid():
                    branding_form.save()
                    log_audit(request.user, 'admin_action', 'Updated platform branding.', request, {
                        'site_name': branding_form.cleaned_data['site_name'],
                        'logo_updated': bool(request.FILES.get('logo')),
                        'favicon_updated': bool(request.FILES.get('favicon')),
                    })
                    messages.success(request, 'Platform branding saved.')
                    return redirect('admin_console_section', section=section)
            elif action == 'save_game_settings':
                game_settings_form = PlatformGameSettingsForm(
                    request.POST,
                    instance=platform_settings,
                )
                if game_settings_form.is_valid():
                    game_settings_form.save()
                    log_audit(request.user, 'admin_action', 'Updated game operating settings.', request, {
                        'maximum_crash_point': str(game_settings_form.cleaned_data['maximum_crash_point']),
                        'betting_seconds': game_settings_form.cleaned_data['betting_seconds'],
                    })
                    messages.success(request, 'Game settings saved. They apply to new shared rounds.')
                    return redirect('admin_console_section', section=section)
    if section == 'admins':
        admin_account_form = AdminAccountCreationForm(
            allow_superuser=request.user.is_superuser,
        )
        if request.method == 'POST':
            action = request.POST.get('action')
            if action == 'create_admin':
                admin_account_form = AdminAccountCreationForm(
                    request.POST,
                    allow_superuser=request.user.is_superuser,
                )
                if admin_account_form.is_valid():
                    role = admin_account_form.cleaned_data['role']
                    if role == 'superuser' and not request.user.is_superuser:
                        return JsonResponse({'success': False, 'message': 'Superuser access is required.'}, status=403)
                    phone_number = admin_account_form.cleaned_data['phone_number']
                    username = admin_account_form.cleaned_data['username']
                    with db_transaction.atomic():
                        created = User.objects.create_user(
                            username=username,
                            password=admin_account_form.cleaned_data['password1'],
                            is_staff=True,
                            is_superuser=role == 'superuser',
                        )
                        Profile.objects.create(user=created, phone_number=phone_number)
                    log_audit(request.user, 'admin_action', 'Created a staff account.', request, {
                        'target_user_id': created.pk,
                        'role': role,
                    })
                    messages.success(request, 'Administrator account created.')
                    return redirect('admin_console_section', section=section)
            elif action in {'revoke_admin', 'promote_superuser'}:
                target_id = request.POST.get('user_id', '')
                if not request.user.is_superuser:
                    return JsonResponse({'success': False, 'message': 'Superuser access is required.'}, status=403)
                if not target_id.isdecimal():
                    messages.error(request, 'Select a valid administrator account.')
                else:
                    target = get_object_or_404(User, pk=target_id, is_staff=True)
                    if target.pk == request.user.pk:
                        messages.error(request, 'You cannot revoke or change your own administrator account.')
                    elif action == 'promote_superuser':
                        target.is_superuser = True
                        target.is_staff = True
                        target.save(update_fields=['is_superuser', 'is_staff'])
                        log_audit(request.user, 'admin_action', 'Promoted a staff account to superuser.', request, {
                            'target_user_id': target.pk,
                        })
                        messages.success(request, 'Super administrator access granted.')
                    else:
                        if target.is_superuser and User.objects.filter(is_superuser=True, is_active=True).count() <= 1:
                            messages.error(request, 'The last active superuser cannot be revoked.')
                        else:
                            target.is_staff = False
                            target.is_superuser = False
                            target.save(update_fields=['is_staff', 'is_superuser'])
                            revoke_admin_sessions(target)
                            log_audit(request.user, 'admin_action', 'Revoked staff-console access.', request, {
                                'target_user_id': target.pk,
                            })
                            messages.success(request, 'Staff-console access revoked.')
                return redirect('admin_console_section', section=section)

    if request.method == 'POST':
        if section == 'live' and request.POST.get('action') == 'emergency_void_round':
            if not request.user.is_superuser:
                return JsonResponse({'success': False, 'message': 'Superuser access required.'}, status=403)

            round_id = request.POST.get('round_id', '').strip()
            confirmation = request.POST.get('confirmation', '').strip()
            reason = request.POST.get('reason', '').strip()[:255]
            if not round_id or confirmation != f'VOID {round_id}' or len(reason) < 5:
                messages.error(request, 'Enter the current round ID, type VOID followed by that ID, and provide a clear reason.')
                return redirect('admin_console_section', section=section)

            emergency_result = None
            with db_transaction.atomic():
                game_state = SharedGameState.objects.select_for_update().filter(pk=1).first()
                if not game_state or not game_state.active_round_id:
                    emergency_result = ('error', 'There is no active shared round to void.')
                elif game_state.active_round.public_id != round_id:
                    emergency_result = ('error', 'The active round changed. Refresh the live monitor and try again.')
                else:
                    game_round = GameRound.objects.select_for_update().get(pk=game_state.active_round_id)
                    if game_round.status != 'flying':
                        emergency_result = ('error', 'Only a currently flying round can be voided.')
                    else:
                        now = timezone.now()
                        current_multiplier = multiplier_at_elapsed_time(game_round.started_at, now)
                        if current_multiplier >= game_round.crash_point:
                            reveal_game_round(game_round)
                            emergency_result = (
                                'warning',
                                'The scheduled crash point was already reached. The normal round outcome was finalized; no bets were refunded.',
                            )
                        else:
                            open_bets = list(
                                GameSession.objects.select_for_update()
                                .filter(round=game_round, cashed_out_at__isnull=True)
                                .order_by('user_id', 'id')
                            )
                            invalid_refund = any(
                                bet.admin_refunded_at is not None
                                or bet.won_amount != Decimal('0.00')
                                or bet.stake <= Decimal('0.00')
                                or Transaction.objects.filter(
                                    reference=f'EMERGENCY-VOID-{game_round.public_id}-{bet.pk}'
                                ).exists()
                                for bet in open_bets
                            )
                            if invalid_refund:
                                emergency_result = (
                                    'error',
                                    'The round contains a bet that cannot be safely refunded. No changes were made.',
                                )
                            else:
                                profiles = {
                                    profile.user_id: profile
                                    for profile in Profile.objects.select_for_update()
                                    .filter(user_id__in={bet.user_id for bet in open_bets})
                                    .order_by('user_id')
                                }
                                if len(profiles) != len({bet.user_id for bet in open_bets}):
                                    emergency_result = (
                                        'error',
                                        'A player wallet could not be found. No changes were made.',
                                    )
                                else:
                                    refunded_total = Decimal('0.00')
                                    for bet in open_bets:
                                        profile = profiles[bet.user_id]
                                        profile.balance += bet.stake
                                        refunded_total += bet.stake
                                        refund_reference = f'EMERGENCY-VOID-{game_round.public_id}-{bet.pk}'
                                        Transaction.objects.create(
                                            user_id=bet.user_id,
                                            transaction_type='refund',
                                            amount=bet.stake,
                                            status='completed',
                                            reference=refund_reference,
                                            description=f'Emergency void refund: {reason}',
                                        )
                                        bet.admin_refunded_at = now
                                        bet.save(update_fields=['admin_refunded_at'])

                                    for profile in profiles.values():
                                        profile.save(update_fields=['balance'])

                                    game_round.status = 'void'
                                    game_round.save(update_fields=['status'])
                                    log_audit(
                                        request.user,
                                        'admin_action',
                                        'Voided a flying shared round and refunded unsettled stakes.',
                                        request,
                                        {
                                            'round_id': game_round.public_id,
                                            'round_pk': game_round.pk,
                                            'current_multiplier': str(current_multiplier),
                                            'refund_count': len(open_bets),
                                            'refund_total': str(refunded_total),
                                            'reason': reason,
                                        },
                                    )
                                    emergency_result = (
                                        'success',
                                        f'Round {game_round.public_id} voided at {current_multiplier:.2f}x. '
                                        f'{len(open_bets)} open bet(s) refunded ({refunded_total:.2f} total). '
                                        'Completed cashouts were not changed.',
                                    )

            result_level, result_message = emergency_result
            getattr(messages, result_level)(request, result_message)
            return redirect('admin_console_section', section=section)

        if section == 'users' and request.POST.get('action') in {
            'lock_user', 'ban_user', 'unlock_user', 'revoke_sessions', 'anonymize_user',
        }:
            user_id = request.POST.get('user_id', '')
            if not user_id.isdecimal():
                messages.error(request, 'Select a valid player account.')
                return redirect('admin_console_section', section=section)
            profile = get_object_or_404(
                Profile.objects.select_related('user'),
                user_id=user_id,
            )
            target = profile.user
            action = request.POST.get('action')
            reason = request.POST.get('reason', '').strip()[:255]
            if target.pk == request.user.pk or target.is_superuser or (target.is_staff and not request.user.is_superuser):
                messages.error(request, 'This account is protected from your current administrator role.')
            elif action in {'lock_user', 'ban_user'} and len(reason) < 3:
                messages.error(request, 'Enter a clear reason of at least 3 characters.')
            elif action == 'anonymize_user' and request.POST.get('confirmation') != f'ANONYMIZE {target.pk}':
                messages.error(request, 'The anonymization confirmation did not match.')
            elif action == 'lock_user':
                profile.account_status = 'locked'
                profile.account_note = reason
                profile.save(update_fields=['account_status', 'account_note'])
                revoke_admin_sessions(target)
                messages.success(request, 'Account locked and active sign-ins revoked.')
            elif action == 'ban_user':
                profile.account_status = 'banned'
                profile.account_note = reason
                profile.save(update_fields=['account_status', 'account_note'])
                revoke_admin_sessions(target)
                messages.success(request, 'Account banned and active sign-ins revoked.')
            elif action == 'unlock_user':
                profile.account_status = 'active'
                profile.account_note = ''
                profile.save(update_fields=['account_status', 'account_note'])
                messages.success(request, 'Account access restored.')
            elif action == 'revoke_sessions':
                revoke_admin_sessions(target)
                messages.success(request, 'All active sign-ins for this account were revoked.')
            elif action == 'anonymize_user':
                target.username = f'anonymized-player-{target.pk}-{get_random_string(6)}'
                target.email = ''
                target.first_name = ''
                target.last_name = ''
                target.is_active = False
                target.is_staff = False
                target.is_superuser = False
                target.set_password(get_random_string(64))
                target.save(update_fields=[
                    'username', 'email', 'first_name', 'last_name', 'is_active',
                    'is_staff', 'is_superuser', 'password',
                ])
                profile.phone_number = ''
                profile.account_status = 'anonymized'
                profile.account_note = 'Identity anonymized at administrator request.'
                profile.save(update_fields=['phone_number', 'account_status', 'account_note'])
                revoke_admin_sessions(target)
                messages.success(request, 'Account identity anonymized; financial and gameplay records were retained.')
            log_audit(request.user, 'admin_action', 'Updated player access or identity.', request, {
                'target_user_id': target.pk,
                'action': action,
                'reason': reason,
            })
            return redirect('admin_console_section', section=section)

        if section == 'players' and request.POST.get('action') == 'refund_lost_bet':
            session_id = request.POST.get('session_id', '')
            reason = request.POST.get('reason', '').strip()[:255]
            if not session_id.isdecimal() or len(reason) < 3:
                messages.error(request, 'Select a bet and enter a refund reason.')
                return redirect('admin_console_section', section=section)
            with db_transaction.atomic():
                bet = get_object_or_404(
                    GameSession.objects.select_for_update().select_related('round'),
                    pk=session_id,
                )
                refund_reference = f'ADMIN-BET-REFUND-{bet.pk}'
                if (
                    not bet.round
                    or bet.round.status != 'crashed'
                    or bet.cashed_out_at is not None
                    or bet.won_amount > 0
                    or bet.stake <= Decimal('0.00')
                    or bet.admin_refunded_at is not None
                    or Transaction.objects.filter(reference=refund_reference).exists()
                ):
                    messages.error(request, 'This bet is not eligible for an administrator wallet refund.')
                else:
                    locked_profile = Profile.objects.select_for_update().get(user=bet.user)
                    locked_profile.balance += bet.stake
                    locked_profile.save(update_fields=['balance'])
                    Transaction.objects.create(
                        user=bet.user,
                        transaction_type='refund',
                        amount=bet.stake,
                        status='completed',
                        reference=refund_reference,
                        description=f'Administrator wallet refund: {reason}',
                    )
                    bet.admin_refunded_at = timezone.now()
                    bet.save(update_fields=['admin_refunded_at'])
                    log_audit(request.user, 'admin_action', 'Refunded a lost bet to the player wallet.', request, {
                        'target_user_id': bet.user_id,
                        'session_id': bet.pk,
                        'round_id': bet.round.public_id,
                        'amount': str(bet.stake),
                        'reference': refund_reference,
                        'reason': reason,
                    })
                    messages.success(request, f'{bet.stake:.2f} {locked_profile.currency} credited to the player wallet.')
            return redirect(f"{reverse('admin_console_section', kwargs={'section': section})}?round_id={request.GET.get('round_id', '')}")

        if section == 'risk' and request.POST.get('action') == 'update_alert':
            alert_id = request.POST.get('alert_id', '')
            if not alert_id.isdecimal():
                messages.error(request, 'Select a valid risk alert.')
                return redirect('admin_console_section', section=section)
            alert = get_object_or_404(RiskAlert, pk=alert_id)
            new_status = request.POST.get('status')
            if new_status not in {'reviewing', 'resolved', 'dismissed'}:
                messages.error(request, 'Select a valid alert status.')
            else:
                alert.status = new_status
                alert.resolved_at = timezone.now() if new_status in {'resolved', 'dismissed'} else None
                alert.save(update_fields=['status', 'resolved_at'])
                log_audit(request.user, 'admin_action', 'Updated a risk alert.', request, {
                    'alert_id': alert.pk,
                    'status': alert.status,
                })
                messages.success(request, 'Risk alert status updated.')
            return redirect('admin_console_section', section=section)

        if section not in {'admins', 'settings'}:
            return JsonResponse({'success': False, 'message': 'Unsupported admin action.'}, status=400)

    search = request.GET.get('q', '').strip()[:100]
    now = timezone.now()
    status_filter = request.GET.get('status', '').strip()
    type_filter = request.GET.get('type', '').strip()
    rows = None
    if section == 'users':
        rows = Profile.objects.select_related('user').order_by('-created_at')
        if search:
            rows = rows.filter(
                Q(user__username__icontains=search)
                | Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search)
                | Q(phone_number__icontains=search)
            )
        account_status = request.GET.get('account_status', '').strip()
        if account_status in {'active', 'locked', 'banned', 'anonymized'}:
            rows = rows.filter(account_status=account_status)
    elif section == 'payments':
        rows = PaymentRequest.objects.select_related('user').order_by('-created_at')
        if status_filter in {'queued', 'pending', 'processing', 'completed', 'failed', 'cancelled'}:
            rows = rows.filter(status=status_filter)
        if type_filter in {'deposit', 'withdraw'}:
            rows = rows.filter(payment_type=type_filter)
        if search:
            rows = rows.annotate(searchable_amount=Cast('amount', output_field=CharField()))
            rows = rows.filter(
                Q(user__username__icontains=search)
                | Q(payment_type__icontains=search)
                | Q(provider__icontains=search)
                | Q(external_reference__icontains=search)
                | Q(provider_reference__icontains=search)
                | Q(status__icontains=search)
                | Q(phone_number__icontains=search)
                | Q(failure_reason__icontains=search)
                | Q(searchable_amount__icontains=search)
            )
    elif section == 'transactions':
        rows = Transaction.objects.select_related('user').order_by('-created_at')
        transaction_status = request.GET.get('status', '').strip()
        if transaction_status in {'completed', 'pending', 'failed'}:
            rows = rows.filter(status=transaction_status)
        transaction_type = request.GET.get('type', '').strip()
        if transaction_type in {'deposit', 'withdraw', 'bet', 'win', 'refund', 'bonus'}:
            rows = rows.filter(transaction_type=transaction_type)
        if search:
            rows = rows.annotate(searchable_amount=Cast('amount', output_field=CharField()))
            rows = rows.filter(
                Q(user__username__icontains=search)
                | Q(reference__icontains=search)
                | Q(transaction_type__icontains=search)
                | Q(status__icontains=search)
                | Q(description__icontains=search)
                | Q(searchable_amount__icontains=search)
            )
    elif section == 'rounds':
        rows = GameRound.objects.order_by('-started_at')
        round_status = request.GET.get('status', '').strip()
        if round_status in {'betting', 'flying', 'crashed', 'void'}:
            rows = rows.filter(status=round_status)
        if search:
            rows = rows.annotate(searchable_crash_point=Cast('crash_point', output_field=CharField()))
            rows = rows.filter(
                Q(public_id__icontains=search)
                | Q(status__icontains=search)
                | Q(server_seed_hash__icontains=search)
                | Q(searchable_crash_point__icontains=search)
            )
    elif section == 'players':
        round_choices = list(GameRound.objects.order_by('-started_at')[:50])
        selected_round_id = request.GET.get('round_id', '').strip()
        if not selected_round_id.isdecimal() or not GameRound.objects.filter(pk=selected_round_id).exists():
            active_round = SharedGameState.objects.select_related('active_round').filter(pk=1).first()
            selected_round_id = str(active_round.active_round_id) if active_round and active_round.active_round_id else (
                str(round_choices[0].pk) if round_choices else ''
            )
        online_cutoff = now - timedelta(seconds=90)
        rows = GameSession.objects.filter(round_id=selected_round_id).select_related(
            'user',
            'round',
        ).annotate(
            player_online=Exists(
                GamePresence.objects.filter(
                    user_id=OuterRef('user_id'),
                    last_seen__gte=online_cutoff,
                )
            ),
        ).order_by('-created_at') if selected_round_id else GameSession.objects.none()
        if search:
            rows = rows.filter(Q(user__username__icontains=search) | Q(bet_request_id__icontains=search))
        player_section_data = {
            'round_choices': round_choices,
            'selected_round_id': selected_round_id,
            'online_player_count': GamePresence.objects.filter(
                user_id__in=rows.values('user_id'),
                last_seen__gte=online_cutoff,
            ).values('user_id').distinct().count(),
        }
    elif section == 'live':
        rows = GameRound.objects.order_by('-started_at')
    elif section == 'reports':
        rows = FinancialReport.objects.select_related('generated_by').order_by('-period_start')
    elif section == 'risk':
        rows = RiskAlert.objects.select_related('user').order_by('-created_at')
        if status_filter in {'open', 'reviewing', 'resolved', 'dismissed'}:
            rows = rows.filter(status=status_filter)
        if search:
            rows = rows.filter(
                Q(user__username__icontains=search)
                | Q(alert_type__icontains=search)
                | Q(status__icontains=search)
                | Q(description__icontains=search)
                | Q(data__message__icontains=search)
            )
    elif section == 'safeguards':
        rows = SelfExclusion.objects.select_related('user').order_by('-created_at')
        if search:
            rows = rows.filter(Q(user__username__icontains=search) | Q(reason__icontains=search))
    elif section == 'security':
        rows = AuditLog.objects.filter(action_type='risk_alert').order_by('-created_at')
        if search:
            rows = rows.filter(
                Q(action_type__icontains=search)
                | Q(user__username__icontains=search)
                | Q(description__icontains=search)
                | Q(ip_address__icontains=search)
                | Q(data__event__icontains=search)
                | Q(data__account_key__icontains=search)
                | Q(user_agent__icontains=search)
            )
    elif section == 'admins':
        rows = User.objects.filter(is_staff=True).order_by('-is_superuser', 'username')
        if search:
            rows = rows.filter(Q(username__icontains=search) | Q(profile__phone_number__icontains=search))
    elif section == 'settings':
        rows = None

    if request.GET.get('export') == 'csv' and rows is not None:
        return export_admin_records(section, rows)

    page_obj = Paginator(rows, 30).get_page(request.GET.get('page')) if rows is not None else None
    pagination_params = request.GET.copy()
    pagination_params.pop('page', None)
    pagination_params.pop('export', None)
    metrics = []
    if section == 'users':
        metrics = [
            {'label': 'Total accounts', 'value': User.objects.count(), 'note': 'Registered player accounts'},
            {'label': 'Active accounts', 'value': Profile.objects.filter(
                account_status='active',
                user__is_active=True,
            ).count(), 'note': 'Can currently sign in'},
            {'label': 'Restricted accounts', 'value': Profile.objects.filter(
                Q(account_status__in=['locked', 'banned', 'anonymized']) | Q(user__is_active=False)
            ).count(), 'note': 'Locked, banned, or disabled'},
        ]
    elif section == 'payments':
        metrics = [
            {'label': 'Pending', 'value': PaymentRequest.objects.filter(status='pending').count(), 'note': 'Awaiting provider updates'},
            {'label': 'Queued', 'value': PaymentRequest.objects.filter(status='queued').count(), 'note': 'Waiting for processing'},
            {'label': 'Processing', 'value': PaymentRequest.objects.filter(status='processing').count(), 'note': 'Currently in progress'},
        ]
    elif section == 'transactions':
        transaction_totals = Transaction.objects.aggregate(
            total=Sum('amount'),
            pending=Count('id', filter=Q(status='pending')),
        )
        metrics = [
            {'label': 'Ledger entries', 'value': Transaction.objects.count(), 'note': 'Recorded wallet movements'},
            {'label': 'Pending entries', 'value': transaction_totals['pending'], 'note': 'Not marked complete'},
            {'label': 'Total volume', 'value': f"{(transaction_totals['total'] or Decimal('0.00')):,.2f}", 'note': 'All recorded amounts'},
        ]
    elif section in {'rounds', 'live'}:
        metrics = [
            {'label': 'Total rounds', 'value': GameRound.objects.count(), 'note': 'All retained round records'},
            {'label': 'Currently flying', 'value': GameRound.objects.filter(status='flying').count(), 'note': 'Active flight records'},
            {'label': 'Completed rounds', 'value': GameRound.objects.filter(status='crashed').count(), 'note': 'Outcome recorded'},
        ]
    elif section == 'players':
        metrics = [
            {'label': 'Players in round', 'value': rows.count(), 'note': 'Recorded bets for selected round'},
            {'label': 'Recently online', 'value': player_section_data['online_player_count'], 'note': 'Presence observed in the last 90 seconds'},
            {'label': 'Selected round', 'value': player_section_data['selected_round_id'] or '—', 'note': 'Round identifier'},
        ]
    elif section == 'reports':
        metrics = [
            {'label': 'Saved reports', 'value': FinancialReport.objects.count(), 'note': 'Generated financial periods'},
            {'label': 'Daily reports', 'value': FinancialReport.objects.filter(report_type='daily').count(), 'note': 'Generated daily periods'},
            {'label': 'Monthly reports', 'value': FinancialReport.objects.filter(report_type='monthly').count(), 'note': 'Generated monthly periods'},
        ]
    elif section == 'risk':
        metrics = [
            {'label': 'Open alerts', 'value': RiskAlert.objects.filter(status='open').count(), 'note': 'Awaiting staff action'},
            {'label': 'Critical alerts', 'value': RiskAlert.objects.filter(status='open', severity='critical').count(), 'note': 'Highest severity open'},
            {'label': 'Reviewing', 'value': RiskAlert.objects.filter(status='reviewing').count(), 'note': 'Claimed for investigation'},
        ]
    elif section == 'safeguards':
        active_exclusions = SelfExclusion.objects.filter(starts_at__lte=now).filter(
            Q(permanent=True) | Q(ends_at__gt=now)
        )
        metrics = [
            {'label': 'Active exclusions', 'value': active_exclusions.count(), 'note': 'Player protections currently in force'},
            {'label': 'Permanent exclusions', 'value': active_exclusions.filter(permanent=True).count(), 'note': 'No scheduled end date'},
            {'label': 'Total requests', 'value': SelfExclusion.objects.count(), 'note': 'All historical records'},
        ]
    elif section == 'admins':
        metrics = [
            {'label': 'Staff accounts', 'value': User.objects.filter(is_staff=True).count(), 'note': 'Can access the staff console'},
            {'label': 'Super administrators', 'value': User.objects.filter(is_superuser=True, is_active=True).count(), 'note': 'Full platform authority'},
            {'label': 'Active staff', 'value': User.objects.filter(is_staff=True, is_active=True).count(), 'note': 'Currently enabled'},
        ]
    elif section == 'settings':
        metrics = [
            {'label': 'Platform name', 'value': platform_settings.site_name, 'note': 'Displayed across public pages'},
            {'label': 'Flight cap', 'value': f'{platform_settings.maximum_crash_point:.2f}x', 'note': 'Maximum for newly generated rounds'},
            {'label': 'Betting window', 'value': f'{platform_settings.betting_seconds}s', 'note': 'Before each shared flight'},
        ]
    context = {
        'section': section,
        'section_title': section_config[section][0],
        'section_description': section_config[section][1],
        'search': search,
        'status_filter': status_filter,
        'type_filter': type_filter,
        'pagination_query': pagination_params.urlencode(),
        'export_query': pagination_params.urlencode(),
        'page_obj': page_obj,
        'metrics': metrics,
        'account_status_filter': request.GET.get('account_status', ''),
        'branding_form': branding_form,
        'game_settings_form': game_settings_form,
        'admin_account_form': admin_account_form,
        'platform_settings': platform_settings,
        'is_superuser': request.user.is_superuser,
        **(player_section_data if section == 'players' else {}),
        'security_snapshot': {
            'debug': settings.DEBUG,
            'https_redirect': settings.SECURE_SSL_REDIRECT,
            'secure_session_cookie': settings.SESSION_COOKIE_SECURE,
            'secure_csrf_cookie': settings.CSRF_COOKIE_SECURE,
            'login_failure_threshold': settings.SECURITY_LOGIN_FAILURE_THRESHOLD,
            'login_failure_window': settings.SECURITY_LOGIN_FAILURE_WINDOW_SECONDS,
        } if section == 'security' else None,
        'security_failure_count': AuditLog.objects.filter(
            action_type='risk_alert',
            data__event='login_failed',
            created_at__gte=now - timedelta(hours=24),
        ).count() if section == 'security' else 0,
        'security_alert_count': RiskAlert.objects.filter(status='open').count() if section == 'security' else 0,
    }
    if section == 'live':
        context['live_state_url'] = reverse('admin_live_round_state')
    return render(request, 'core/admin_section.html', context)


@login_required
@require_http_methods(['GET'])
def admin_live_round_state(request):
    if not request.user.is_staff:
        return JsonResponse({'success': False, 'message': 'Staff access required.'}, status=403)

    now = timezone.now()
    game_state = SharedGameState.objects.select_related('active_round').filter(pk=1).first()
    game_round = game_state.active_round if game_state else None
    if game_round is None:
        return JsonResponse({
            'success': True,
            'status': 'waiting',
            'server_time': now.isoformat(),
        })

    status = game_round.status
    multiplier = Decimal('1.00')
    if status == 'flying':
        multiplier = min(multiplier_at_elapsed_time(game_round.started_at, now), game_round.crash_point)
        if multiplier >= game_round.crash_point:
            status = 'crashed'
    elif status == 'crashed':
        multiplier = game_round.crash_point

    stakes = GameSession.objects.filter(round=game_round).aggregate(
        total=Sum('stake'),
        open_bets=Count('id', filter=Q(cashed_out_at__isnull=True)),
        cashed_out=Count('id', filter=Q(cashed_out_at__isnull=False)),
    )
    if status == 'void':
        emergency_audit = AuditLog.objects.filter(
            action_type='admin_action',
            description='Voided a flying shared round and refunded unsettled stakes.',
            data__round_id=game_round.public_id,
        ).first()
        if emergency_audit:
            multiplier = Decimal(emergency_audit.data['current_multiplier'])
    return JsonResponse({
        'success': True,
        'round_id': game_round.public_id,
        'status': status,
        'current_multiplier': float(multiplier),
        'started_at': game_round.started_at.isoformat(),
        'server_time': now.isoformat(),
        'server_seed_hash': game_round.server_seed_hash,
        'bet_count': game_round.bet_count,
        'open_bets': stakes['open_bets'] if status in {'betting', 'flying'} else 0,
        'cashed_out': stakes['cashed_out'],
        'total_stakes': float(stakes['total'] or Decimal('0.00')),
        'final_multiplier': float(game_round.crash_point) if status == 'crashed' else None,
    })


@login_required
def room_list(request):
    rooms = Room.objects.filter(status__in=['waiting', 'running']).order_by('-created_at')[:12]
    create_form = RoomCreateForm()
    return render(request, 'core/room_list.html', {
        'rooms': rooms,
        'create_form': create_form,
    })


@login_required
def create_room(request):
    if request.method != 'POST':
        return redirect('room_list')

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
        allowed, reason = can_place_bets(profile, stake)
        if not allowed:
            return JsonResponse({'success': False, 'message': reason}, status=403)
        if stake > profile.balance:
            return JsonResponse({'success': False, 'message': 'Insufficient balance to join.'}, status=400)

        profile.balance -= stake
        profile.save()
        RoomPlayer.objects.create(room=room, user=request.user, stake=stake)

        return JsonResponse({
            'success': True,
            'message': 'Joined room successfully.',
            'balance': float(profile.balance),
        })

    if action == 'start':
        if request.user != room.host:
            return JsonResponse({'success': False, 'message': 'Only the host can start this game.'}, status=403)

        if room.status != 'waiting':
            return JsonResponse({'success': False, 'message': 'Game has already started.'}, status=400)

        if room.players.count() < 1:
            return JsonResponse({'success': False, 'message': 'At least one player must join to start.'}, status=400)

        room.crash_point = Decimal(str(round(random.uniform(1.5, 12.0), 2)))
        room.status = 'running'
        room.started_at = timezone.now()
        room.save()

        return JsonResponse({
            'success': True,
            'message': 'Game started.',
        })

    if action == 'cashout':
        player = get_object_or_404(RoomPlayer, room=room, user=request.user)
        if room.status != 'running':
            return JsonResponse({'success': False, 'message': 'The game is not running.'}, status=400)

        if player.cashed_out_at is not None:
            return JsonResponse({'success': False, 'message': 'You already cashed out.'}, status=400)

        requested_cashout_multiplier = Decimal(request.POST.get('cashout_multiplier') or '0')
        server_multiplier = multiplier_at_elapsed_time(room.started_at)
        effective_multiplier = min(requested_cashout_multiplier, server_multiplier).quantize(Decimal('0.01'))
        round_has_crashed = server_multiplier >= room.crash_point

        player.cashed_out_at = effective_multiplier
        player.active = False
        player.won_amount = Decimal('0')
        message = 'Crashed before cash out. Better luck next round.'

        if not round_has_crashed and effective_multiplier < room.crash_point:
            player.won_amount = min(
                player.stake * effective_multiplier,
                MAXIMUM_WIN_AMOUNT,
            ).quantize(Decimal('0.01'))
            profile.balance += player.won_amount
            profile.save()
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
    exclusion_form = SelfExclusionForm()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'compliance':
            form = ProfileComplianceForm(request.POST, initial={
                'phone_number': profile.phone_number,
                'daily_deposit_limit': profile.daily_deposit_limit,
                'daily_loss_limit': profile.daily_loss_limit,
            })
            if form.is_valid():
                profile.daily_deposit_limit = form.cleaned_data['daily_deposit_limit']
                profile.daily_loss_limit = form.cleaned_data['daily_loss_limit']
                profile.save(update_fields=['daily_deposit_limit', 'daily_loss_limit'])
                messages.success(request, 'Compliance preferences updated.')
                return redirect('profile')
        elif action == 'self_exclude':
            form = ProfileComplianceForm(initial={
                'phone_number': profile.phone_number,
                'daily_deposit_limit': profile.daily_deposit_limit,
                'daily_loss_limit': profile.daily_loss_limit,
            })
            exclusion_form = SelfExclusionForm(request.POST)
            if not request.POST.get('confirm_exclusion'):
                exclusion_form.add_error(None, 'Confirm that you understand this action before continuing.')
            if exclusion_form.is_valid():
                duration = exclusion_form.cleaned_data['duration']
                permanent = duration == 'permanent'
                ends_at = None if permanent else timezone.now() + timedelta(hours=int(duration))
                exclusion = SelfExclusion.objects.create(
                    user=request.user,
                    ends_at=ends_at,
                    permanent=permanent,
                    reason=exclusion_form.cleaned_data['reason'],
                )
                profile.self_excluded_until = None if permanent else ends_at
                profile.save(update_fields=['self_excluded_until'])
                log_audit(request.user, 'self_exclude', 'Created self-exclusion', request, {'exclusion_id': exclusion.id, 'permanent': permanent})
                messages.warning(request, 'Self-exclusion is now active.')
                return redirect('profile')
        else:
            form = ProfileComplianceForm()
    else:
        form = ProfileComplianceForm(initial={
            'phone_number': profile.phone_number,
            'daily_deposit_limit': profile.daily_deposit_limit,
            'daily_loss_limit': profile.daily_loss_limit,
        })
    exclusions = SelfExclusion.objects.filter(user=request.user)[:5]
    transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')[:5]
    return render(request, 'core/profile.html', {
        'profile': profile,
        'form': form,
        'exclusion_form': exclusion_form,
        'exclusions': exclusions,
        'transactions': transactions,
    })


def _bet_history_data(user, profile, requested_outcome='all', requested_page=None):
    all_sessions = GameSession.objects.filter(user=user).select_related('round').order_by('-created_at')
    if requested_outcome == 'won':
        filtered_sessions = all_sessions.filter(cashed_out_at__lt=F('crash_point'))
    elif requested_outcome == 'lost':
        filtered_sessions = all_sessions.filter(
            Q(cashed_out_at__gte=F('crash_point'))
            | Q(cashed_out_at__isnull=True, round__status='crashed')
        )
    elif requested_outcome == 'live':
        filtered_sessions = all_sessions.filter(cashed_out_at__isnull=True, round__status__in=['betting', 'flying'])
    elif requested_outcome == 'void':
        filtered_sessions = all_sessions.filter(round__status='void')
    else:
        requested_outcome = 'all'
        filtered_sessions = all_sessions

    winning_filter = Q(cashed_out_at__lt=F('crash_point'))
    losing_filter = Q(cashed_out_at__gte=F('crash_point')) | Q(
        cashed_out_at__isnull=True,
        round__status='crashed',
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
        void_refunds=Sum('stake', filter=Q(round__status='void', admin_refunded_at__isnull=False)),
    )
    filter_counts = {
        'all': summary['total_bets'] or 0,
        'won': summary['winning_bets'] or 0,
        'lost': summary['losing_bets'] or 0,
        'live': summary['live_bets'] or 0,
        'void': summary['void_bets'] or 0,
    }
    total_staked = summary['total_staked'] or Decimal('0.00')
    total_returned = (summary['total_returned'] or Decimal('0.00')) + (
        summary['void_refunds'] or Decimal('0.00')
    )
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
            session.history_status = 'refunded' if session.admin_refunded_at else 'void'
            session.history_multiplier = None
            session.history_payout = session.stake if session.admin_refunded_at else session.won_amount
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
        'outcome': requested_outcome,
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
                'multiplier': format(session.history_multiplier, '.2f') if session.history_multiplier is not None else None,
                'payout': format(session.history_payout, '.2f') if session.history_payout is not None else None,
            }
            for session in history['sessions']
        ],
    })
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
@require_POST
def update_client_seed(request):
    client_seed = request.POST.get('client_seed', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', client_seed):
        return JsonResponse({
            'success': False,
            'message': 'Use 1 to 128 letters, numbers, periods, underscores, colons, or hyphens.',
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
    exclusion_form = SelfExclusionForm()
    password_form = PasswordChangeForm(request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'password':
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                password_form.save()
                update_session_auth_hash(request, request.user)
                messages.success(request, 'Your password has been changed.')
                return redirect('game_limits')
            form = ProfileComplianceForm(initial={
                'phone_number': profile.phone_number,
                'daily_deposit_limit': profile.daily_deposit_limit,
                'daily_loss_limit': profile.daily_loss_limit,
            })
        elif action == 'self_exclude':
            exclusion_form = SelfExclusionForm(request.POST)
            if not request.POST.get('confirm_exclusion'):
                exclusion_form.add_error(None, 'Confirm that you understand this action before continuing.')
            if exclusion_form.is_valid():
                duration = exclusion_form.cleaned_data['duration']
                permanent = duration == 'permanent'
                ends_at = None if permanent else timezone.now() + timedelta(hours=int(duration))
                exclusion = SelfExclusion.objects.create(
                    user=request.user,
                    ends_at=ends_at,
                    permanent=permanent,
                    reason=exclusion_form.cleaned_data['reason'],
                )
                profile.self_excluded_until = None if permanent else ends_at
                profile.save(update_fields=['self_excluded_until'])
                log_audit(request.user, 'self_exclude', 'Created self-exclusion', request, {
                    'exclusion_id': exclusion.id,
                    'permanent': permanent,
                })
                messages.warning(request, 'Self-exclusion is now active.')
                return redirect('game_limits')
        else:
            form = ProfileComplianceForm(request.POST, initial={
                'phone_number': profile.phone_number,
                'daily_deposit_limit': profile.daily_deposit_limit,
                'daily_loss_limit': profile.daily_loss_limit,
            })
            if form.is_valid():
                profile.daily_deposit_limit = form.cleaned_data['daily_deposit_limit']
                profile.daily_loss_limit = form.cleaned_data['daily_loss_limit']
                profile.save(update_fields=['daily_deposit_limit', 'daily_loss_limit'])
                messages.success(request, 'Your limits have been updated.')
                return redirect('game_limits')
    else:
        form = ProfileComplianceForm(initial={
            'phone_number': profile.phone_number,
            'daily_deposit_limit': profile.daily_deposit_limit,
            'daily_loss_limit': profile.daily_loss_limit,
        })
    if request.method == 'POST' and request.POST.get('action') == 'self_exclude':
        form = ProfileComplianceForm(initial={
            'phone_number': profile.phone_number,
            'daily_deposit_limit': profile.daily_deposit_limit,
            'daily_loss_limit': profile.daily_loss_limit,
        })
    if request.method == 'POST' and request.POST.get('action') == 'password':
        form = ProfileComplianceForm(initial={
            'phone_number': profile.phone_number,
            'daily_deposit_limit': profile.daily_deposit_limit,
            'daily_loss_limit': profile.daily_loss_limit,
        })
    now = timezone.now()
    active_exclusion = SelfExclusion.objects.filter(user=request.user).filter(
        Q(permanent=True) | Q(ends_at__isnull=True) | Q(ends_at__gt=now)
    ).order_by('-created_at').first()
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
    return render(request, 'core/game_rules.html')


@login_required
def provably_fair(request):
    rounds = GameRound.objects.filter(bets__user=request.user).distinct().order_by('-started_at')[:50]
    return render(request, 'core/provably_fair.html', {'rounds': rounds})
