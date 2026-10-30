from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.db import transaction as db_transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import get_random_string

from .models import GameSession, PaymentRequest, Profile, RiskAlert, Transaction
from .utils import log_audit


def _revoke_user_sessions(user):
    session_keys = []
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator(chunk_size=200):
        if str(session.get_decoded().get('_auth_user_id', '')) == str(user.pk):
            session_keys.append(session.session_key)
    if session_keys:
        Session.objects.filter(session_key__in=session_keys).delete()


@login_required
def admin_player_profile(request, user_id):
    if not request.user.is_staff:
        return redirect('dashboard')

    profile = get_object_or_404(
        Profile.objects.select_related('user'),
        user_id=user_id,
    )
    target = profile.user
    if request.method == 'POST':
        action = request.POST.get('action', '')
        reason = request.POST.get('reason', '').strip()[:255]

        if action in {'lock_user', 'ban_user', 'unlock_user', 'revoke_sessions', 'anonymize_user'}:
            if target.pk == request.user.pk or target.is_superuser or (
                target.is_staff and not request.user.is_superuser
            ):
                messages.error(request, 'This account is protected from your current administrator role.')
            elif action in {'lock_user', 'ban_user'} and len(reason) < 3:
                messages.error(request, 'Enter a clear reason of at least 3 characters.')
            elif action == 'anonymize_user' and request.POST.get('confirmation') != f'ANONYMIZE {target.pk}':
                messages.error(request, 'The anonymization confirmation did not match.')
            elif action in {'lock_user', 'ban_user'}:
                profile.account_status = 'locked' if action == 'lock_user' else 'banned'
                profile.account_note = reason
                profile.save(update_fields=['account_status', 'account_note'])
                _revoke_user_sessions(target)
                messages.success(request, 'Account access restricted and active sign-ins revoked.')
            elif action == 'unlock_user':
                profile.account_status = 'active'
                profile.account_note = ''
                profile.save(update_fields=['account_status', 'account_note'])
                messages.success(request, 'Account access restored.')
            elif action == 'revoke_sessions':
                _revoke_user_sessions(target)
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
                _revoke_user_sessions(target)
                messages.success(
                    request,
                    'Account identity anonymized; financial and gameplay records were retained.',
                )

            log_audit(request.user, 'admin_action', 'Updated player access or identity.', request, {
                'target_user_id': target.pk,
                'action': action,
                'reason': reason,
            })
            return redirect('admin_player_profile', user_id=target.pk)

        if action == 'refund_lost_bet':
            session_id = request.POST.get('session_id', '')
            if not session_id.isdecimal() or len(reason) < 3:
                messages.error(request, 'Select a bet and enter a refund reason.')
                return redirect('admin_player_profile', user_id=target.pk)

            with db_transaction.atomic():
                bet = get_object_or_404(
                    GameSession.objects.select_for_update().select_related('round'),
                    pk=session_id,
                    user=target,
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
                    locked_profile = Profile.objects.select_for_update().get(user=target)
                    locked_profile.balance += bet.stake
                    locked_profile.save(update_fields=['balance'])
                    Transaction.objects.create(
                        user=target,
                        transaction_type='refund',
                        amount=bet.stake,
                        status='completed',
                        reference=refund_reference,
                        description=f'Administrator wallet refund: {reason}',
                    )
                    bet.admin_refunded_at = timezone.now()
                    bet.save(update_fields=['admin_refunded_at'])
                    log_audit(request.user, 'admin_action', 'Refunded a lost bet to the player wallet.', request, {
                        'target_user_id': target.pk,
                        'session_id': bet.pk,
                        'round_id': bet.round.public_id,
                        'amount': str(bet.stake),
                        'reference': refund_reference,
                        'reason': reason,
                    })
                    messages.success(
                        request,
                        f'{bet.stake:.2f} {locked_profile.currency} credited to the player wallet.',
                    )
            return redirect('admin_player_profile', user_id=target.pk)

        messages.error(request, 'Unsupported player profile action.')
        return redirect('admin_player_profile', user_id=target.pk)

    return render(request, 'core/admin_section.html', {
        'section': 'user_profile',
        'section_title': 'Player profile',
        'section_description': 'Review account details, gameplay, wallet activity, and access controls.',
        'metrics': [],
        'page_obj': None,
        'search': '',
        'pagination_query': '',
        'export_query': '',
        'is_superuser': request.user.is_superuser,
        'profile': profile,
        'player_bets': GameSession.objects.filter(user=target).select_related(
            'round',
        ).order_by('-created_at')[:10],
        'player_payments': PaymentRequest.objects.filter(
            user=target,
        ).order_by('-created_at')[:5],
        'player_transactions': Transaction.objects.filter(
            user=target,
        ).order_by('-created_at')[:8],
        'player_risk_alerts': RiskAlert.objects.filter(
            user=target,
        ).order_by('-created_at')[:5],
        'player_bet_count': GameSession.objects.filter(user=target).count(),
        'player_transaction_count': Transaction.objects.filter(user=target).count(),
        'can_manage_player': (
            target.pk != request.user.pk
            and not target.is_superuser
            and (not target.is_staff or request.user.is_superuser)
            and profile.account_status != 'anonymized'
        ),
    })
