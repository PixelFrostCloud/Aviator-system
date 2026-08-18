import json
import random
from hmac import compare_digest
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction as db_transaction
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from django.utils import timezone
from .forms import (
    RegisterForm,
    PlayForm,
    WalletForm,
    ProfileComplianceForm,
    KYCSubmissionForm,
    SelfExclusionForm,
    ChatMessageForm,
    FinancialReportForm,
    RoomCreateForm,
    RoomJoinForm,
)
from .models import (
    Profile,
    ChatMessage,
    ChatReaction,
    ChatRain,
    ChatRainRecipient,
    FinancialReport,
    GameSession,
    GameRound,
    KYCSubmission,
    PaymentRequest,
    RiskAlert,
    SelfExclusion,
    Transaction,
    Room,
    RoomPlayer,
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
    event_payload,
    reward_statistics,
    send_chat_rain,
    stop_event,
    wallet_payload,
)
from .payment_gateway import PaymentGatewayResult
from .payment_services import apply_provider_result, initiate_deposit, initiate_withdrawal, sync_payment
from .utils import (
    can_deposit,
    can_place_bets,
    create_game_round,
    generate_financial_report,
    generate_payment_reference,
    get_kyc_monthly_deposit_limit,
    get_monthly_deposit_total,
    log_audit,
    reveal_game_round,
    run_player_risk_checks,
)


def home(request):
    return render(request, 'core/home.html')


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
            user = form.save()
            Profile.objects.create(user=user)
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
    recent_crashes = GameRound.objects.filter(status='crashed').order_by('-started_at')[:10]
    chat_messages = ChatMessage.objects.select_related('user').annotate(reaction_count=Count('reactions')).filter(flagged=False)[:20]

    if request.method == 'GET':
        return render(request, 'core/play.html', {
            'profile': profile,
            'recent_sessions': recent_sessions,
            'highest_stakers': highest_stakers,
            'recent_crashes': recent_crashes,
            'chat_messages': chat_messages,
            'chat_form': ChatMessageForm(),
        })

    action = request.POST.get('action')
    if action == 'chat':
        form = ChatMessageForm(request.POST)
        if not form.is_valid():
            return JsonResponse({'success': False, 'errors': form.errors}, status=400)

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

        stakes = []
        for value in bets[:2]:
            try:
                stake_value = Decimal(str(value)).quantize(Decimal('0.01'))
            except (InvalidOperation, ValueError):
                continue
            if stake_value > 0:
                stakes.append(stake_value)

        if not stakes:
            return JsonResponse({'success': False, 'message': 'Enter at least one valid bet amount.'}, status=400)

        total_stake = sum(stakes, Decimal('0.00'))
        allowed, reason = can_place_bets(profile, total_stake)
        if not allowed:
            return JsonResponse({'success': False, 'message': reason}, status=403)

        with db_transaction.atomic():
            locked_profile = Profile.objects.select_for_update().get(user=request.user)
            if total_stake > locked_profile.balance:
                return JsonResponse({'success': False, 'message': 'Insufficient balance for all bets.'}, status=400)

            game_round = create_game_round(request.user, request)
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

            created_sessions = []
            for stake in stakes:
                session = GameSession.objects.create(
                    user=request.user,
                    round=game_round,
                    stake=stake,
                    crash_point=game_round.crash_point,
                )
                created_sessions.append(session.id)
            run_player_risk_checks(request.user)

        log_audit(request.user, 'game_start', f'Started {len(created_sessions)} bets totaling {total_stake:.2f} {profile.currency}', request, {
            'stakes': [str(s) for s in stakes],
            'session_ids': created_sessions,
            'round_id': game_round.public_id,
            'server_seed_hash': game_round.server_seed_hash,
            'client_seed': game_round.client_seed,
            'crash_point': str(game_round.crash_point),
        })

        return JsonResponse({
            'success': True,
            'session_ids': created_sessions,
            'round_id': game_round.public_id,
            'server_seed_hash': game_round.server_seed_hash,
            'client_seed': game_round.client_seed,
            'balance': float(locked_profile.balance),
            'stake_total': float(total_stake),
        })

    if action == 'cashout':
        session_ids = request.POST.get('session_ids', '')
        try:
            requested_cashout_multiplier = Decimal(request.POST.get('cashout_multiplier') or '0').quantize(Decimal('0.01'))
        except InvalidOperation:
            return JsonResponse({'success': False, 'message': 'Invalid cashout multiplier.'}, status=400)

        if not session_ids:
            return JsonResponse({'success': False, 'message': 'No active bets available.'}, status=400)

        session_ids = [int(x) for x in session_ids.split(',') if x.isdigit()]
        with db_transaction.atomic():
            sessions = list(GameSession.objects.select_for_update().select_related('round').filter(id__in=session_ids, user=request.user))
            if not sessions:
                return JsonResponse({'success': False, 'message': 'Invalid games to cash out.'}, status=400)

            game_round = sessions[0].round
            if not game_round:
                return JsonResponse({'success': False, 'message': 'This round cannot be verified.'}, status=400)

            elapsed = max((timezone.now() - game_round.started_at).total_seconds(), 0)
            server_multiplier = Decimal('1.00') + Decimal(str(elapsed)).quantize(Decimal('0.01'))
            effective_multiplier = min(requested_cashout_multiplier, server_multiplier).quantize(Decimal('0.01'))
            round_has_crashed = server_multiplier >= game_round.crash_point

            total_won = Decimal('0.00')
            total_stake = Decimal('0.00')
            all_lost = True
            settled_count = 0

            for session in sessions:
                if session.cashed_out_at is not None:
                    continue

                settled_count += 1
                session.cashed_out_at = effective_multiplier
                session.won_amount = Decimal('0.00')
                total_stake += session.stake

                if not round_has_crashed and effective_multiplier < session.crash_point:
                    session.won_amount = (session.stake * effective_multiplier).quantize(Decimal('0.01'))
                    total_won += session.won_amount
                    all_lost = False

                session.save(update_fields=['cashed_out_at', 'won_amount'])

            if settled_count == 0:
                return JsonResponse({'success': False, 'message': 'This bet has already been cashed out.'}, status=409)

            locked_profile = Profile.objects.select_for_update().get(user=request.user)
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
        game_round = get_object_or_404(GameRound.objects.filter(bets__user=request.user).distinct(), public_id=round_id)
        elapsed = max((timezone.now() - game_round.started_at).total_seconds(), 0)
        server_multiplier = Decimal('1.00') + Decimal(str(elapsed)).quantize(Decimal('0.01'))
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
    deposit_form = WalletForm(prefix='deposit')
    withdraw_form = WalletForm(prefix='withdraw')

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
                        deposit_form.add_error(None, error)
                    elif payment.status == 'completed':
                        messages.success(request, f'{amount:.2f} {profile.currency} deposited successfully via {payment.get_provider_display()}.')
                        return redirect('wallet')
                    else:
                        message = f'{amount:.2f} {profile.currency} deposit sent to {payment.get_provider_display()}. Status: {payment.get_status_display()}.'
                        redirect_url = (payment.metadata or {}).get('redirect_url')
                        if redirect_url:
                            message += ' Complete the card step in the provider page.'
                        messages.info(request, message)
                        return redirect(redirect_url or 'wallet')
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
                )
                if error:
                    withdraw_form.add_error(None, error)
                else:
                    log_audit(request.user, 'withdraw', f'Requested withdrawal {amount:.2f} {profile.currency}', request, {
                        'provider': provider,
                        'payment_reference': payment.external_reference,
                        'provider_status': result.status if result else '',
                    })
                    if payment.status == 'completed':
                        messages.success(request, f'{amount:.2f} {profile.currency} withdrawal completed via {payment.get_provider_display()}.')
                    else:
                        messages.success(request, f'{amount:.2f} {profile.currency} withdrawal requested via {payment.get_provider_display()}. Status: {payment.get_status_display()}.')
                    return redirect('wallet')

    transactions = Transaction.objects.filter(user=request.user)
    payment_requests = PaymentRequest.objects.filter(user=request.user)[:10]
    monthly_deposit_total = get_monthly_deposit_total(request.user)
    monthly_deposit_limit = get_kyc_monthly_deposit_limit(profile)
    return render(request, 'core/wallet.html', {
        'profile': profile,
        'deposit_form': deposit_form,
        'withdraw_form': withdraw_form,
        'transactions': transactions,
        'payment_requests': payment_requests,
        'monthly_deposit_total': monthly_deposit_total,
        'monthly_deposit_limit': monthly_deposit_limit,
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
        redirect_url=payload.get('cardRedirectUrl') or payload.get('redirectUrl') or '',
        raw=payload,
    )
    apply_provider_result(payment, result)
    return JsonResponse({'success': True, 'reference': external_reference, 'status': result.local_status})


@login_required
@require_POST
def payment_sync(request, reference):
    payment = get_object_or_404(PaymentRequest, external_reference=reference, user=request.user)
    payment = sync_payment(payment)
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
    pending_withdrawals = PaymentRequest.objects.filter(payment_type='withdraw', status='pending').count()
    kyc_pending = Profile.objects.filter(kyc_status='none').count()
    kyc_reviews = KYCSubmission.objects.filter(status='pending').count()
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
        'kyc_pending': kyc_pending,
        'kyc_reviews': kyc_reviews,
        'open_risk_alerts': open_risk_alerts,
        'latest_reports': latest_reports,
        'report_form': report_form,
        'active_rooms': active_rooms,
        'running_rooms': running_rooms,
        'room_count': room_count,
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
        elapsed = max((timezone.now() - room.started_at).total_seconds(), 0)
        server_multiplier = Decimal('1.00') + Decimal(str(elapsed)).quantize(Decimal('0.01'))
        effective_multiplier = min(requested_cashout_multiplier, server_multiplier).quantize(Decimal('0.01'))
        round_has_crashed = server_multiplier >= room.crash_point

        player.cashed_out_at = effective_multiplier
        player.active = False
        player.won_amount = Decimal('0')
        message = 'Crashed before cash out. Better luck next round.'

        if not round_has_crashed and effective_multiplier < room.crash_point:
            player.won_amount = (player.stake * effective_multiplier).quantize(Decimal('0.01'))
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
        elapsed = max((timezone.now() - room.started_at).total_seconds(), 0)
        server_multiplier = Decimal('1.00') + Decimal(str(elapsed)).quantize(Decimal('0.01'))
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
    kyc_form = KYCSubmissionForm()
    exclusion_form = SelfExclusionForm()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'compliance':
            form = ProfileComplianceForm(request.POST)
            if form.is_valid():
                profile.phone_number = form.cleaned_data['phone_number']
                profile.daily_deposit_limit = form.cleaned_data['daily_deposit_limit']
                profile.daily_loss_limit = form.cleaned_data['daily_loss_limit']
                if profile.phone_number and profile.kyc_status == 'none':
                    profile.kyc_status = 'tier_1'
                profile.save(update_fields=['phone_number', 'daily_deposit_limit', 'daily_loss_limit', 'kyc_status'])
                messages.success(request, 'Compliance preferences updated.')
                return redirect('profile')
        elif action == 'kyc':
            form = ProfileComplianceForm(initial={
                'phone_number': profile.phone_number,
                'daily_deposit_limit': profile.daily_deposit_limit,
                'daily_loss_limit': profile.daily_loss_limit,
            })
            kyc_form = KYCSubmissionForm(request.POST)
            if kyc_form.is_valid():
                submission = kyc_form.save(commit=False)
                submission.user = request.user
                submission.save()
                log_audit(request.user, 'kyc_submit', 'Submitted KYC information', request, {'submission_id': submission.id})
                messages.success(request, 'KYC submission received for operator review.')
                return redirect('profile')
        elif action == 'self_exclude':
            form = ProfileComplianceForm(initial={
                'phone_number': profile.phone_number,
                'daily_deposit_limit': profile.daily_deposit_limit,
                'daily_loss_limit': profile.daily_loss_limit,
            })
            exclusion_form = SelfExclusionForm(request.POST)
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
    kyc_submissions = KYCSubmission.objects.filter(user=request.user)[:5]
    exclusions = SelfExclusion.objects.filter(user=request.user)[:5]
    return render(request, 'core/profile.html', {
        'profile': profile,
        'form': form,
        'kyc_form': kyc_form,
        'exclusion_form': exclusion_form,
        'kyc_submissions': kyc_submissions,
        'exclusions': exclusions,
    })


@login_required
def bet_history(request):
    profile = get_object_or_404(Profile, user=request.user)
    sessions = GameSession.objects.filter(user=request.user).select_related('round').order_by('-created_at')[:100]
    return render(request, 'core/bet_history.html', {
        'profile': profile,
        'sessions': sessions,
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
    if request.method == 'POST':
        form = ProfileComplianceForm(request.POST)
        if form.is_valid():
            profile.phone_number = form.cleaned_data['phone_number']
            profile.daily_deposit_limit = form.cleaned_data['daily_deposit_limit']
            profile.daily_loss_limit = form.cleaned_data['daily_loss_limit']
            profile.save(update_fields=['phone_number', 'daily_deposit_limit', 'daily_loss_limit'])
            messages.success(request, 'Game limits updated.')
            return redirect('game_limits')
    else:
        form = ProfileComplianceForm(initial={
            'phone_number': profile.phone_number,
            'daily_deposit_limit': profile.daily_deposit_limit,
            'daily_loss_limit': profile.daily_loss_limit,
        })
    return render(request, 'core/game_limits.html', {'profile': profile, 'form': form})


@login_required
def free_bets(request):
    profile = get_object_or_404(Profile, user=request.user)
    return render(request, 'core/free_bets.html', {'profile': profile})


@login_required
def vip_club(request):
    profile = get_object_or_404(Profile, user=request.user)
    transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')[:5]
    return render(request, 'core/vip_club.html', {
        'profile': profile,
        'transactions': transactions,
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
