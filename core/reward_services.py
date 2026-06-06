import random
import uuid
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from .models import (
    AuditLog,
    ChatMessage,
    ChatRain,
    ChatRainRecipient,
    Profile,
    RewardClaim,
    RewardEvent,
    RewardTransaction,
    RewardWallet,
    Transaction,
)


REWARD_GROUP = 'rewards'
PARTICIPANT_TTL_SECONDS = 90


class RewardError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status
        super().__init__(message)


def reward_reference(prefix='RWD'):
    return f'{prefix}-{timezone.now().strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:12].upper()}'


def quantize_money(value):
    return Decimal(value).quantize(Decimal('0.01'))


def broadcast_reward_event(event_type, payload):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    async_to_sync(channel_layer.group_send)(
        REWARD_GROUP,
        {
            'type': 'reward_event',
            'event': event_type,
            'payload': payload,
        },
    )


def event_payload(event):
    approved = event.claims.filter(status='approved')
    claim_count = approved.count()
    return {
        'id': str(event.uuid),
        'title': event.title,
        'description': event.description,
        'status': event.status,
        'claim_strategy': event.claim_strategy,
        'reward_amount': float(event.reward_per_claim),
        'remaining_reward_pool': float(event.remaining_reward_pool),
        'remaining_claims': max(event.maximum_claims - claim_count, 0),
        'maximum_claims': event.maximum_claims,
        'starts_at': event.starts_at.isoformat(),
        'ends_at': event.ends_at.isoformat(),
        'claim_count': claim_count,
    }


def reward_statistics(event=None):
    events = RewardEvent.objects.all()
    claims = RewardClaim.objects.filter(status='approved')
    if event is not None:
        events = events.filter(id=event.id)
        claims = claims.filter(reward_event=event)
    aggregate = claims.aggregate(total=Sum('amount_claimed'))
    active_events = events.filter(status='active').count()
    total_claims = claims.count()
    return {
        'active_events': active_events,
        'active_participants': active_participant_count(),
        'rewards_distributed': float(aggregate['total'] or Decimal('0.00')),
        'claims': total_claims,
        'average_claim_value': float((aggregate['total'] or Decimal('0.00')) / total_claims) if total_claims else 0,
        'wallets_credited': RewardWallet.objects.filter(total_rewards_claimed__gt=0).count(),
        'total_transactions': RewardTransaction.objects.count(),
        'events': events.count(),
    }


def ensure_reward_wallet(user):
    wallet, _ = RewardWallet.objects.get_or_create(user=user)
    return wallet


def lock_reward_wallet(user):
    wallet = RewardWallet.objects.select_for_update().filter(user=user).first()
    if wallet is not None:
        return wallet
    wallet = RewardWallet.objects.create(user=user)
    return RewardWallet.objects.select_for_update().get(id=wallet.id)


def calculate_claim_amount(event, wallet):
    if event.claim_strategy == 'equal':
        amount = event.reward_per_claim
    elif event.claim_strategy == 'random':
        maximum = event.maximum_random_reward or event.reward_per_claim
        minimum = min(event.minimum_random_reward, maximum)
        cents_min = int((minimum * 100).to_integral_value())
        cents_max = int((maximum * 100).to_integral_value())
        amount = Decimal(random.randint(cents_min, max(cents_min, cents_max))) / Decimal('100')
    else:
        age_days = max((timezone.now() - wallet.user.date_joined).days, 1)
        weight = Decimal('1.00')
        weight += Decimal(wallet.activity_score) / Decimal('100')
        weight += Decimal(wallet.loyalty_points) / Decimal('1000')
        weight += Decimal(min(age_days, 365)) / Decimal('365')
        weight *= wallet.admin_weight_multiplier
        amount = event.reward_per_claim * min(weight, Decimal('5.00'))
    return min(quantize_money(amount), event.remaining_reward_pool)


def claim_reward(user, event_uuid, ip_address='', device_identifier='', idempotency_key=''):
    with transaction.atomic():
        event = RewardEvent.objects.select_for_update().get(uuid=event_uuid)
        now = timezone.now()
        if event.status != 'active':
            raise RewardError('Reward event is not active.', 409)
        if event.starts_at > now or event.ends_at < now:
            raise RewardError('Reward event is outside its claim window.', 409)
        if event.claims.filter(status='approved').count() >= event.maximum_claims:
            event.status = 'completed'
            event.save(update_fields=['status', 'updated_at'])
            raise RewardError('Reward event has reached the maximum number of claims.', 409)
        if event.remaining_reward_pool <= 0:
            event.status = 'completed'
            event.save(update_fields=['status', 'updated_at'])
            raise RewardError('Reward pool has been fully claimed.', 409)

        existing = RewardClaim.objects.filter(reward_event=event, user=user).first()
        if existing:
            return existing, False

        wallet = lock_reward_wallet(user)

        amount = calculate_claim_amount(event, wallet)
        if amount <= 0:
            raise RewardError('No reward is available to claim.', 409)

        balance_before = wallet.available_balance
        balance_after = balance_before + amount
        reference = reward_reference('CLAIM')
        try:
            claim = RewardClaim.objects.create(
                reward_event=event,
                user=user,
                amount_claimed=amount,
                ip_address=ip_address or None,
                device_identifier=device_identifier[:160],
                transaction_reference=reference,
                idempotency_key=idempotency_key[:96],
                status='approved',
            )
        except IntegrityError:
            return RewardClaim.objects.get(reward_event=event, user=user), False

        wallet.available_balance = balance_after
        wallet.total_rewards_received += amount
        wallet.total_rewards_claimed += amount
        wallet.save(update_fields=['available_balance', 'total_rewards_received', 'total_rewards_claimed', 'updated_at'])
        RewardTransaction.objects.create(
            wallet=wallet,
            event=event,
            amount=amount,
            transaction_type='credit',
            balance_before=balance_before,
            balance_after=balance_after,
            reference=reference,
        )
        event.remaining_reward_pool = max(event.remaining_reward_pool - amount, Decimal('0.00'))
        if event.remaining_reward_pool <= 0 or event.claims.filter(status='approved').count() >= event.maximum_claims:
            event.status = 'completed'
        event.save(update_fields=['remaining_reward_pool', 'status', 'updated_at'])

    broadcast_reward_event('reward_claimed', {
        'event': event_payload(event),
        'claim': {
            'id': str(claim.uuid),
            'amount': float(claim.amount_claimed),
            'user': user.username,
        },
        'wallet': wallet_payload(wallet),
    })
    broadcast_reward_event('statistics_updated', reward_statistics(event))
    AuditLog.objects.create(user=user, action_type='admin_action', description='Reward claimed', data={
        'event_id': str(event.uuid),
        'claim_id': str(claim.uuid),
        'amount': str(claim.amount_claimed),
    })
    return claim, True


def recent_rain_recipients(sender, player_count):
    recent_user_ids = []
    for user_id in ChatMessage.objects.filter(flagged=False).exclude(user=sender).values_list('user_id', flat=True)[:80]:
        if user_id not in recent_user_ids:
            recent_user_ids.append(user_id)

    candidates = list(User.objects.select_related('profile').filter(id__in=recent_user_ids, profile__isnull=False))
    if len(candidates) < player_count:
        fallback = User.objects.select_related('profile').filter(profile__isnull=False).exclude(id=sender.id)
        known_ids = {user.id for user in candidates}
        candidates.extend([user for user in fallback if user.id not in known_ids])
    if not candidates:
        return []
    return random.sample(candidates, min(player_count, len(candidates)))


def send_chat_rain(sender, total_amount, player_count, ip_address='', device_identifier=''):
    total_amount = quantize_money(total_amount)
    player_count = min(max(int(player_count), 1), 20)
    if total_amount < Decimal('1.00') or player_count < 1:
        raise RewardError('Enter a valid rain amount and player count.', 400)

    per_player = quantize_money(total_amount / Decimal(player_count))
    actual_total = quantize_money(per_player * Decimal(player_count))
    if per_player <= 0:
        raise RewardError('Rain amount per player is too low.', 400)

    with transaction.atomic():
        sender_profile = Profile.objects.select_for_update().get(user=sender)
        if sender_profile.balance < actual_total:
            raise RewardError('You do not have sufficient balance on your wallet.', 400)

        recipients = recent_rain_recipients(sender, player_count)
        if not recipients:
            raise RewardError('No eligible chat players are available for rain yet.', 400)

        actual_total = quantize_money(per_player * Decimal(len(recipients)))
        if sender_profile.balance < actual_total:
            raise RewardError('You do not have sufficient balance on your wallet.', 400)

        now = timezone.now()
        reference = reward_reference('CHAT-RAIN')
        event = RewardEvent.objects.create(
            title=f'Chat Rain by {sender.username}',
            description=f'Immediate chat rain distributed to {len(recipients)} players.',
            total_reward_pool=actual_total,
            remaining_reward_pool=actual_total,
            maximum_claims=len(recipients),
            reward_per_claim=per_player,
            claim_strategy='equal',
            starts_at=now,
            ends_at=now + timedelta(minutes=5),
            status='active',
            created_by=sender,
        )

        sender_profile.balance -= actual_total
        sender_profile.save(update_fields=['balance'])
        Transaction.objects.create(
            user=sender,
            transaction_type='bet',
            amount=actual_total,
            status='completed',
            reference=reference,
            description=f'Chat rain reward event sent to {len(recipients)} players',
        )

        claims = []
        for recipient in recipients:
            recipient_profile = Profile.objects.select_for_update().get(user=recipient)
            recipient_profile.balance += per_player
            recipient_profile.save(update_fields=['balance'])
            Transaction.objects.create(
                user=recipient,
                transaction_type='bonus',
                amount=per_player,
                status='completed',
                reference=reference,
                description=f'Chat rain received from {sender.username}',
            )

            wallet = lock_reward_wallet(recipient)
            balance_before = wallet.available_balance
            balance_after = balance_before + per_player
            claim_reference = f'{reference}-{recipient.id}'
            claim = RewardClaim.objects.create(
                reward_event=event,
                user=recipient,
                amount_claimed=per_player,
                ip_address=ip_address or None,
                device_identifier=device_identifier[:160],
                status='approved',
                transaction_reference=claim_reference,
            )
            RewardTransaction.objects.create(
                wallet=wallet,
                event=event,
                amount=per_player,
                transaction_type='credit',
                balance_before=balance_before,
                balance_after=balance_after,
                reference=claim_reference,
            )
            wallet.available_balance = balance_after
            wallet.total_rewards_received += per_player
            wallet.total_rewards_claimed += per_player
            wallet.save(update_fields=['available_balance', 'total_rewards_received', 'total_rewards_claimed', 'updated_at'])
            claims.append(claim)

        event.remaining_reward_pool = Decimal('0.00')
        event.status = 'completed'
        event.save(update_fields=['remaining_reward_pool', 'status', 'updated_at'])

        chat = ChatMessage.objects.create(
            user=sender,
            message=f'Sent rain: {actual_total:.2f} {sender_profile.currency} to {len(recipients)} players.',
        )
        rain = ChatRain.objects.create(
            sender=sender,
            message=chat,
            reward_event=event,
            total_amount=actual_total,
            per_player_amount=per_player,
            recipient_count=len(recipients),
            reference=reference,
        )
        ChatRainRecipient.objects.bulk_create([
            ChatRainRecipient(rain=rain, user=recipient, amount=per_player)
            for recipient in recipients
        ])

    broadcast_reward_event('reward_created', event_payload(event))
    broadcast_reward_event('reward_claimed', {
        'event': event_payload(event),
        'claims': len(claims),
        'total_amount': float(actual_total),
    })
    broadcast_reward_event('wallet_updated', {
        'event_id': str(event.uuid),
        'recipient_count': len(recipients),
    })
    broadcast_reward_event('statistics_updated', reward_statistics(event))
    AuditLog.objects.create(user=sender, action_type='chat_message', description='Sent chat rain through reward system', data={
        'event_id': str(event.uuid),
        'rain_id': rain.id,
        'message_id': chat.id,
        'total_amount': str(actual_total),
        'players': len(recipients),
        'reference': reference,
    })
    return {
        'event': event,
        'chat': chat,
        'rain': rain,
        'recipients': recipients,
        'actual_total': actual_total,
        'per_player': per_player,
        'reference': reference,
        'sender_balance': sender_profile.balance,
    }


def wallet_payload(wallet):
    return {
        'id': str(wallet.uuid),
        'available_balance': float(wallet.available_balance),
        'locked_balance': float(wallet.locked_balance),
        'total_rewards_received': float(wallet.total_rewards_received),
        'total_rewards_claimed': float(wallet.total_rewards_claimed),
    }


def activate_event(event, actor=None):
    event.status = 'active'
    now = timezone.now()
    if event.starts_at > now:
        event.starts_at = now
    event.save(update_fields=['status', 'starts_at', 'updated_at'])
    broadcast_reward_event('reward_started', event_payload(event))
    if actor:
        AuditLog.objects.create(user=actor, action_type='admin_action', description='Reward event activated', data={'event_id': str(event.uuid)})
    return event


def stop_event(event, actor=None, status='completed'):
    event.status = status
    event.save(update_fields=['status', 'updated_at'])
    broadcast_reward_event('reward_completed' if status == 'completed' else 'reward_cancelled', event_payload(event))
    if actor:
        AuditLog.objects.create(user=actor, action_type='admin_action', description=f'Reward event {status}', data={'event_id': str(event.uuid)})
    return event


def mark_participant(user, channel_name, session_id=''):
    key = f'rewards:participant:{user.id}:{session_id or channel_name}'
    cache.set(key, {
        'user_id': user.id,
        'websocket_channel': channel_name,
        'last_activity': timezone.now().isoformat(),
        'session_id': session_id,
    }, PARTICIPANT_TTL_SECONDS)


def active_participant_count():
    # LocMemCache cannot enumerate keys. Keep this conservative for local dev.
    return 0


def expire_due_events():
    now = timezone.now()
    activated = []
    for event in RewardEvent.objects.filter(status='scheduled', starts_at__lte=now, ends_at__gt=now):
        activated.append(activate_event(event))
    completed = RewardEvent.objects.filter(status='active', ends_at__lte=now).update(status='completed', updated_at=now)
    if completed:
        broadcast_reward_event('reward_completed', {'completed_count': completed})
    return {'activated': len(activated), 'completed': completed}
