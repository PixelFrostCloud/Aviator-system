from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction as db_transaction
from django.utils import timezone

from .constants import REFERRAL_REWARD_RATE
from .models import PaymentRequest, Profile, Referral, Transaction


def credit_first_deposit_referral_reward(payment):
    if payment.payment_type != 'deposit' or payment.status != 'completed':
        return None

    with db_transaction.atomic():
        referral = (
            Referral.objects.select_for_update()
            .select_related('inviter', 'invitee')
            .filter(
                invitee_id=payment.user_id,
                first_deposit__isnull=True,
                reward_transaction__isnull=True,
            )
            .first()
        )
        if referral is None:
            return None

        first_completed_deposit = PaymentRequest.objects.filter(
            user_id=payment.user_id,
            payment_type='deposit',
            status='completed',
        ).order_by('completed_at', 'created_at', 'pk').first()
        if first_completed_deposit is None or first_completed_deposit.pk != payment.pk:
            return None

        reward_amount = (payment.amount * REFERRAL_REWARD_RATE).quantize(
            Decimal('0.01'),
            rounding=ROUND_HALF_UP,
        )
        if reward_amount <= 0:
            return None

        inviter_profile = Profile.objects.select_for_update().get(user_id=referral.inviter_id)
        transaction_reference = f'REFERRAL-{referral.pk}'
        reward_transaction, created = Transaction.objects.get_or_create(
            user_id=referral.inviter_id,
            reference=transaction_reference,
            defaults={
                'transaction_type': 'bonus',
                'amount': reward_amount,
                'status': 'completed',
                'description': (
                    f'Referral reward: 5% of {referral.invitee.username}\'s first deposit, '
                    'funded by Pakabet'
                ),
            },
        )
        if created:
            inviter_profile.balance += reward_amount
            inviter_profile.save(update_fields=['balance'])

        referral.first_deposit = payment
        referral.reward_amount = reward_amount
        referral.reward_transaction = reward_transaction
        referral.rewarded_at = timezone.now()
        referral.save(update_fields=[
            'first_deposit',
            'reward_amount',
            'reward_transaction',
            'rewarded_at',
        ])
        return reward_transaction
