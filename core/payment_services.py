from decimal import Decimal
import re

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from .models import PaymentRequest, Profile, Transaction
from .payment_gateway import IotecPayGateway, PaymentGatewayError, PaymentGatewayResult
from .utils import generate_payment_reference


MOBILE_MONEY_PROVIDERS = {'mtn_momo', 'airtel_money'}
IOTEC_MINIMUM_AMOUNT = Decimal('500')


def normalize_mobile_money_phone(phone_number):
    value = re.sub(r'[\s()-]', '', str(phone_number or ''))
    if value.startswith('+'):
        value = value[1:]
    if value.startswith('0'):
        value = f'256{value[1:]}'
    if not re.fullmatch(r'256\d{9}', value):
        raise ValueError('Enter a valid Uganda mobile number, for example 0700000000 or 256700000000.')
    return value


def initiate_deposit(*, user, amount, provider, phone_number='', redirect_url=''):
    if amount < IOTEC_MINIMUM_AMOUNT:
        return None, None, 'ioTec Pay mobile-money transactions must be at least 500 UGX.'
    if provider not in MOBILE_MONEY_PROVIDERS:
        return None, None, 'Only MTN Mobile Money and Airtel Money are supported.'
    try:
        phone_number = normalize_mobile_money_phone(phone_number)
    except ValueError as exc:
        return None, None, str(exc)

    gateway = IotecPayGateway()
    external_reference = generate_payment_reference('DEP')

    payment = PaymentRequest.objects.create(
        user=user,
        payment_type='deposit',
        provider=provider,
        amount=amount,
        phone_number=phone_number,
        external_reference=external_reference,
        status='pending',
        metadata={'gateway': 'iotec_pay', 'mode': gateway.mode},
    )

    try:
        result = gateway.collect_mobile_money(
            external_id=external_reference,
            amount=amount,
            currency=_currency(user),
            payer=phone_number,
            payer_name=user.get_full_name() or user.username,
            redirect_url=redirect_url,
        )
    except PaymentGatewayError as exc:
        payment = apply_provider_result(payment, PaymentGatewayResult(
            provider_reference=payment.provider_reference or external_reference,
            status='Pending' if exc.retryable else 'Failed',
            status_code='gateway_error',
            status_message='Provider response is delayed.' if exc.retryable else 'Provider rejected the request.',
            raw={'gateway_error': str(exc)},
        ))
        return payment, None, str(exc)

    payment = apply_provider_result(payment, result)
    payment.refresh_from_db()
    return payment, result, ''


def initiate_withdrawal(*, user, amount, provider, phone_number=''):
    if amount < IOTEC_MINIMUM_AMOUNT:
        return None, None, 'ioTec Pay mobile-money transactions must be at least 500 UGX.'
    if provider not in MOBILE_MONEY_PROVIDERS:
        return None, None, 'Withdrawals are currently available for MTN Mobile Money and Airtel Money.'
    try:
        phone_number = normalize_mobile_money_phone(phone_number)
    except ValueError as exc:
        return None, None, str(exc)

    gateway = IotecPayGateway()
    external_reference = generate_payment_reference('WDR')

    with db_transaction.atomic():
        locked_profile = Profile.objects.select_for_update().get(user=user)
        if amount > locked_profile.balance:
            return None, None, 'Insufficient balance for withdrawal.'

        payment = PaymentRequest.objects.create(
            user=user,
            payment_type='withdraw',
            provider=provider,
            amount=amount,
            phone_number=phone_number,
            external_reference=external_reference,
            status='pending',
            metadata={'gateway': 'iotec_pay', 'mode': gateway.mode, 'wallet_debited': True},
        )
        locked_profile.balance -= amount
        locked_profile.save(update_fields=['balance'])
        Transaction.objects.create(
            user=user,
            transaction_type='withdraw',
            amount=amount,
            status='pending',
            reference=external_reference,
            description=f'{payment.get_provider_display()} withdrawal request',
        )

    try:
        result = gateway.disburse_mobile_money(
            external_id=external_reference,
            amount=amount,
            currency=_currency(user),
            payee=phone_number,
            payee_name=user.get_full_name() or user.username,
            payee_email=user.email,
        )
    except PaymentGatewayError as exc:
        payment.refresh_from_db()
        result = PaymentGatewayResult(
            provider_reference=payment.provider_reference or external_reference,
            status='Pending' if exc.retryable else 'Failed',
            status_code='gateway_error',
            status_message='Provider response is delayed.' if exc.retryable else 'Provider rejected the request.',
            raw={'gateway_error': str(exc)},
        )
        payment = apply_provider_result(payment, result)
        return payment, None, str(exc)

    payment = apply_provider_result(payment, result)
    payment.refresh_from_db()
    return payment, result, ''


def apply_provider_result(payment, result):
    next_status = result.local_status

    with db_transaction.atomic():
        locked_payment = PaymentRequest.objects.select_for_update().get(pk=payment.pk)
        previous_status = locked_payment.status
        terminal_statuses = {'completed', 'failed', 'cancelled'}
        if previous_status in terminal_statuses and next_status != previous_status:
            return locked_payment
        metadata = dict(locked_payment.metadata or {})
        metadata['gateway'] = 'iotec_pay'
        metadata['provider_status'] = result.status
        metadata['provider_status_code'] = result.status_code
        metadata['provider_response'] = result.raw or {}
        if result.redirect_url:
            metadata['redirect_url'] = result.redirect_url

        locked_payment.provider_reference = result.provider_reference[:100]
        locked_payment.status = next_status
        locked_payment.failure_reason = result.status_message[:255] if result.is_failed else ''
        if result.raw and result.raw.get('gateway_error'):
            metadata['gateway_error'] = result.raw['gateway_error'][:500]
            locked_payment.metadata = metadata
        locked_payment.metadata = metadata
        if next_status == 'completed':
            locked_payment.completed_at = locked_payment.completed_at or timezone.now()
        locked_payment.save(update_fields=[
            'provider_reference',
            'status',
            'failure_reason',
            'metadata',
            'completed_at',
            'updated_at',
        ])

        if locked_payment.payment_type == 'deposit':
            _apply_deposit_result(locked_payment, previous_status)
        elif locked_payment.payment_type == 'withdraw':
            _apply_withdrawal_result(locked_payment, previous_status)

    return locked_payment


def sync_payment(payment):
    gateway = IotecPayGateway()
    if payment.payment_type == 'deposit':
        result = gateway.sync_collection(payment.external_reference)
    else:
        result = gateway.sync_disbursement(payment.external_reference)
    return apply_provider_result(payment, result)


def _apply_deposit_result(payment, previous_status):
    txn, _ = Transaction.objects.get_or_create(
        reference=payment.external_reference,
        user=payment.user,
        defaults={
            'transaction_type': 'deposit',
            'amount': payment.amount,
            'status': _transaction_status(payment.status),
            'description': f'{payment.get_provider_display()} deposit',
        },
    )

    if payment.status == 'completed' and previous_status != 'completed':
        profile = Profile.objects.select_for_update().get(user=payment.user)
        profile.balance += payment.amount
        if payment.phone_number and not profile.phone_number:
            profile.phone_number = payment.phone_number
            profile.kyc_status = 'tier_1'
            profile.save(update_fields=['balance', 'phone_number', 'kyc_status'])
        else:
            profile.save(update_fields=['balance'])

    txn.status = _transaction_status(payment.status)
    txn.description = f'{payment.get_provider_display()} deposit'
    txn.save(update_fields=['status', 'description'])


def _apply_withdrawal_result(payment, previous_status):
    Transaction.objects.filter(reference=payment.external_reference, user=payment.user).update(
        status=_transaction_status(payment.status),
        description=f'{payment.get_provider_display()} withdrawal request',
    )

    if payment.status in ['failed', 'cancelled'] and previous_status not in ['failed', 'cancelled']:
        profile = Profile.objects.select_for_update().get(user=payment.user)
        profile.balance += payment.amount
        profile.save(update_fields=['balance'])
        Transaction.objects.create(
            user=payment.user,
            transaction_type='refund',
            amount=payment.amount,
            status='completed',
            reference=payment.external_reference,
            description='Withdrawal hold refunded after provider failure',
        )


def _transaction_status(payment_status):
    if payment_status == 'completed':
        return 'completed'
    if payment_status in ['failed', 'cancelled']:
        return 'failed'
    return 'pending'


def _currency(user):
    profile = Profile.objects.get(user=user)
    return profile.currency or getattr(settings, 'IOTEC_PAY_CURRENCY', 'UGX')
