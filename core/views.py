import json
import random
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction as db_transaction
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
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
)
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
            messages.success(request, 'Welcome to Aviator! Your account is ready.')
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
    chat_messages = ChatMessage.objects.select_related('user').filter(flagged=False)[:20]

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
                'text': chat.message,
                'created_at': chat.created_at.strftime('%H:%M'),
                'flagged': chat.flagged,
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
            'crash_point': float(game_round.crash_point),
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
            'crash_point': float(game_round.crash_point),
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
                    with db_transaction.atomic():
                        locked_profile = Profile.objects.select_for_update().get(user=request.user)
                        payment = PaymentRequest.objects.create(
                            user=request.user,
                            payment_type='deposit',
                            provider=provider,
                            amount=amount,
                            phone_number=phone_number,
                            external_reference=generate_payment_reference('DEP'),
                            provider_reference=generate_payment_reference(provider.upper()[:4]),
                            status='completed',
                            completed_at=timezone.now(),
                            metadata={'sandbox': True, 'message': 'Sandbox provider auto-completed deposit.'},
                        )
                        locked_profile.balance += amount
                        if phone_number and not locked_profile.phone_number:
                            locked_profile.phone_number = phone_number
                            locked_profile.kyc_status = 'tier_1'
                            locked_profile.save(update_fields=['balance', 'phone_number', 'kyc_status'])
                        else:
                            locked_profile.save(update_fields=['balance'])
                        Transaction.objects.create(
                            user=request.user,
                            transaction_type='deposit',
                            amount=amount,
                            status='completed',
                            reference=payment.external_reference,
                            description=f'{payment.get_provider_display()} deposit',
                        )
                    log_audit(request.user, 'deposit', f'Deposited {amount:.2f} {profile.currency}', request, {
                        'provider': provider,
                        'payment_reference': payment.external_reference,
                        'sandbox': True,
                    })
                    run_player_risk_checks(request.user)
                    messages.success(request, f'{amount:.2f} {profile.currency} deposited successfully via {payment.get_provider_display()}.')
                    return redirect('wallet')
        elif action == 'withdraw':
            withdraw_form = WalletForm(request.POST, prefix='withdraw')
            if withdraw_form.is_valid():
                amount = withdraw_form.cleaned_data['amount']
                provider = withdraw_form.cleaned_data['provider']
                phone_number = withdraw_form.cleaned_data['phone_number']
                withdrawal_created = False
                if amount > profile.balance:
                    withdraw_form.add_error('amount', 'Insufficient balance for withdrawal.')
                else:
                    with db_transaction.atomic():
                        locked_profile = Profile.objects.select_for_update().get(user=request.user)
                        if amount > locked_profile.balance:
                            withdraw_form.add_error('amount', 'Insufficient balance for withdrawal.')
                        else:
                            payment = PaymentRequest.objects.create(
                                user=request.user,
                                payment_type='withdraw',
                                provider=provider,
                                amount=amount,
                                phone_number=phone_number,
                                external_reference=generate_payment_reference('WDR'),
                                status='pending',
                                metadata={'requires_operator_approval': True},
                            )
                            locked_profile.balance -= amount
                            locked_profile.save(update_fields=['balance'])
                            Transaction.objects.create(
                                user=request.user,
                                transaction_type='withdraw',
                                amount=amount,
                                status='pending',
                                reference=payment.external_reference,
                                description=f'{payment.get_provider_display()} withdrawal request',
                            )
                            log_audit(request.user, 'withdraw', f'Requested withdrawal {amount:.2f} {profile.currency}', request, {
                                'provider': provider,
                                'payment_reference': payment.external_reference,
                            })
                            messages.success(request, f'{amount:.2f} {profile.currency} withdrawal requested. Operator approval is pending.')
                            withdrawal_created = True
                    if withdrawal_created:
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
            'crash_point': float(room.crash_point),
        })

    if action == 'cashout':
        player = get_object_or_404(RoomPlayer, room=room, user=request.user)
        if room.status != 'running':
            return JsonResponse({'success': False, 'message': 'The game is not running.'}, status=400)

        if player.cashed_out_at is not None:
            return JsonResponse({'success': False, 'message': 'You already cashed out.'}, status=400)

        cashout_multiplier = Decimal(request.POST.get('cashout_multiplier') or '0')
        player.cashed_out_at = cashout_multiplier
        player.active = False
        player.won_amount = Decimal('0')
        message = 'Crashed before cash out. Better luck next round.'

        if cashout_multiplier < room.crash_point:
            player.won_amount = (player.stake * cashout_multiplier).quantize(Decimal('0.01'))
            profile.balance += player.won_amount
            profile.save()
            message = f'Cashed out at {cashout_multiplier:.2f}x and won {player.won_amount:.2f} {profile.currency}.'

        player.save()
        return JsonResponse({
            'success': True,
            'message': message,
            'balance': float(profile.balance),
            'won_amount': float(player.won_amount),
            'cashout_multiplier': float(cashout_multiplier),
            'crash_point': float(room.crash_point),
            'status': 'won' if player.is_winner else 'lost',
        })

    return JsonResponse({'success': False, 'message': 'Invalid action.'}, status=400)


@login_required
def room_status(request, room_id):
    room = get_object_or_404(Room, id=room_id)
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
            'crash_point': float(room.crash_point) if room.crash_point else None,
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
def how_to_play(request):
    return render(request, 'core/how_to_play.html')


@login_required
def game_rules(request):
    return render(request, 'core/game_rules.html')


@login_required
def provably_fair(request):
    rounds = GameRound.objects.filter(bets__user=request.user).distinct().order_by('-started_at')[:50]
    return render(request, 'core/provably_fair.html', {'rounds': rounds})
