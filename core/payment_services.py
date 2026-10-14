from decimal import Decimal, InvalidOperation
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .constants import MAXIMUM_DEPOSIT_AMOUNT, MINIMUM_MOBILE_MONEY_AMOUNT
from .models import PaymentDispatchLock, PaymentRequest, Profile, Transaction
from .payment_gateway import (
    IotecPayGateway,
    PaymentGatewayConfigurationError,
    PaymentGatewayError,
    PaymentGatewayResult,
)
from .referral_services import credit_first_deposit_referral_reward
from .utils import can_deposit, generate_payment_reference


MOBILE_MONEY_PROVIDERS = {'mtn_momo', 'airtel_money'}
IOTEC_MINIMUM_AMOUNT = MINIMUM_MOBILE_MONEY_AMOUNT
MOBILE_MONEY_COUNTRY_CHOICES = [
    ('256', 'Uganda (+256)'),
    ('254', 'Kenya (+254)'),
    ('255', 'Tanzania (+255)'),
    ('250', 'Rwanda (+250)'),
    ('257', 'Burundi (+257)'),
    ('211', 'South Sudan (+211)'),
    ('243', 'DR Congo (+243)'),
    ('252', 'Somalia (+252)'),
]
MOBILE_MONEY_COUNTRY_CODES = dict(MOBILE_MONEY_COUNTRY_CHOICES)
MOBILE_MONEY_NATIONAL_LENGTHS = {
    '256': (9, 9),
    '254': (9, 9),
    '255': (9, 9),
    '250': (9, 9),
    '257': (8, 8),
    '211': (9, 9),
    '243': (9, 9),
    '252': (7, 9),
}
MOBILE_MONEY_AVAILABILITY_MESSAGE = (
    'We have not started operating in {country} yet. '
    'Deposits and withdrawals are currently available in Uganda only.'
)


def get_mobile_money_country_code(phone_number):
    return next(
        (code for code in sorted(MOBILE_MONEY_COUNTRY_CODES, key=len, reverse=True) if phone_number.startswith(code)),
        None,
    )


def mobile_money_country_availability_error(country_code):
    if country_code == '256':
        return ''
    country_name = MOBILE_MONEY_COUNTRY_CODES.get(country_code)
    if not country_name:
        return 'Select a supported East African country code.'
    return MOBILE_MONEY_AVAILABILITY_MESSAGE.format(country=country_name.split(' (+', 1)[0])


def normalize_mobile_money_phone(phone_number, country_code=None):
    value = re.sub(r'[\s()-]', '', str(phone_number or ''))
    if value.startswith('+'):
        value = value[1:]
    elif value.startswith('00'):
        value = value[2:]

    explicitly_selected_code = country_code is not None and str(country_code) != ''
    selected_code = str(country_code or '256')
    if selected_code not in MOBILE_MONEY_COUNTRY_CODES:
        raise ValueError('Select a supported East African country code.')

    if value.startswith(tuple(MOBILE_MONEY_COUNTRY_CODES)):
        matching_code = next(
            (code for code in sorted(MOBILE_MONEY_COUNTRY_CODES, key=len, reverse=True) if value.startswith(code)),
            None,
        )
        if explicitly_selected_code and matching_code != selected_code:
            raise ValueError('The phone number country code does not match the selected country.')
        selected_code = matching_code
        national_number = value[len(matching_code):]
    else:
        national_number = value[1:] if value.startswith('0') else value

    minimum_length, maximum_length = MOBILE_MONEY_NATIONAL_LENGTHS[selected_code]
    if (
        not national_number.isdigit()
        or not minimum_length <= len(national_number) <= maximum_length
        or len(selected_code + national_number) > 15
    ):
        country_name = MOBILE_MONEY_COUNTRY_CODES[selected_code].split(' (+', 1)[0]
        raise ValueError(f'Enter a valid {country_name} mobile number with its country code.')
    return f'{selected_code}{national_number}'


def split_mobile_money_phone(phone_number):
    value = re.sub(r'[\s()-]', '', str(phone_number or ''))
    if value.startswith('+'):
        value = value[1:]
    elif value.startswith('00'):
        value = value[2:]

    for code in sorted(MOBILE_MONEY_COUNTRY_CODES, key=len, reverse=True):
        if value.startswith(code):
            return code, value[len(code):]
    return '256', value


def initiate_deposit(
    *,
    user,
    amount,
    provider,
    phone_number='',
    redirect_url='',
    idempotency_key='',
):
    if amount < IOTEC_MINIMUM_AMOUNT:
        return None, None, 'ioTec Pay mobile-money transactions must be at least 500 UGX.'
    if amount > MAXIMUM_DEPOSIT_AMOUNT:
        return None, None, 'Deposits cannot exceed 300,000 UGX per transaction.'
    if provider not in MOBILE_MONEY_PROVIDERS:
        return None, None, 'Only MTN Mobile Money and Airtel Money are supported.'
    try:
        phone_number = normalize_mobile_money_phone(phone_number)
    except ValueError as exc:
        return None, None, str(exc)
    availability_error = mobile_money_country_availability_error(get_mobile_money_country_code(phone_number))
    if availability_error:
        return None, None, availability_error

    gateway = IotecPayGateway()
    idempotency_key, error = _validated_idempotency_key(idempotency_key)
    if error:
        return None, None, error
    external_reference = generate_payment_reference('DEP')
    with db_transaction.atomic():
        locked_profile = Profile.objects.select_for_update().get(user=user)
        existing = PaymentRequest.objects.filter(
            user=user,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            if (
                existing.payment_type != 'deposit'
                or existing.provider != provider
                or existing.amount != amount
                or existing.phone_number != phone_number
            ):
                return None, None, 'This payment request key was already used with different details.'
            return existing, None, ''

        allowed, reason = can_deposit(locked_profile, amount)
        if not allowed:
            return None, None, reason
        payment = PaymentRequest.objects.create(
            user=user,
            payment_type='deposit',
            provider=provider,
            amount=amount,
            phone_number=phone_number,
            external_reference=external_reference,
            idempotency_key=idempotency_key,
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
            raw={'gateway_error_code': exc.provider_code or 'gateway_error'},
        ))
        return payment, None, str(exc)

    payment = apply_provider_result(payment, result)
    payment.refresh_from_db()
    return payment, result, ''


def initiate_withdrawal(
    *,
    user,
    amount,
    provider,
    phone_number='',
    idempotency_key='',
):
    if amount < IOTEC_MINIMUM_AMOUNT:
        return None, None, 'ioTec Pay mobile-money transactions must be at least 500 UGX.'
    if provider not in MOBILE_MONEY_PROVIDERS:
        return None, None, 'Withdrawals are currently available for MTN Mobile Money and Airtel Money.'
    try:
        phone_number = normalize_mobile_money_phone(phone_number)
    except ValueError as exc:
        return None, None, str(exc)
    availability_error = mobile_money_country_availability_error(get_mobile_money_country_code(phone_number))
    if availability_error:
        return None, None, availability_error

    gateway = IotecPayGateway()
    idempotency_key, error = _validated_idempotency_key(idempotency_key)
    if error:
        return None, None, error
    external_reference = generate_payment_reference('WDR')

    with db_transaction.atomic():
        locked_profile = Profile.objects.select_for_update().get(user=user)
        existing = PaymentRequest.objects.filter(
            user=user,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            if (
                existing.payment_type != 'withdraw'
                or existing.provider != provider
                or existing.amount != amount
                or existing.phone_number != phone_number
            ):
                return None, None, 'This payment request key was already used with different details.'
            return existing, None, ''

        if amount > locked_profile.balance:
            return None, None, 'Insufficient balance for withdrawal.'

        queued = not gateway.is_sandbox
        payment = PaymentRequest.objects.create(
            user=user,
            payment_type='withdraw',
            provider=provider,
            amount=amount,
            phone_number=phone_number,
            external_reference=external_reference,
            idempotency_key=idempotency_key,
            status='queued' if queued else 'pending',
            failure_reason=(
                'Payout is waiting for provider funds to become available.'
                if queued else ''
            ),
            metadata={
                'gateway': 'iotec_pay',
                'mode': gateway.mode,
                'wallet_debited': True,
                'dispatch_state': 'queued' if queued else 'submitting',
            },
        )
        locked_profile.balance -= amount
        locked_profile.save(update_fields=['balance'])
        Transaction.objects.create(
            user=user,
            transaction_type='withdraw',
            amount=amount,
            status='pending',
            reference=external_reference,
            description=(
                f'{payment.get_provider_display()} withdrawal queued'
                if queued
                else f'{payment.get_provider_display()} withdrawal request'
            ),
        )

    if queued:
        return payment, None, ''

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
            raw={'gateway_error_code': exc.provider_code or 'gateway_error'},
        )
        payment = apply_provider_result(payment, result)
        return payment, None, str(exc)

    payment = apply_provider_result(payment, result)
    payment.refresh_from_db()
    return payment, result, ''


def process_next_queued_withdrawal():
    lock_token = uuid.uuid4().hex
    now = timezone.now()
    lock_expiry = now - timedelta(seconds=120)
    acquired = PaymentDispatchLock.objects.filter(pk=1).filter(
        Q(locked_at__isnull=True) | Q(locked_at__lt=lock_expiry),
    ).update(lock_token=lock_token, locked_at=now)
    if not acquired:
        return 'locked'

    try:
        stale_check_cutoff = timezone.now() - timedelta(minutes=2)
        with db_transaction.atomic():
            PaymentRequest.objects.filter(
                payment_type='withdraw',
                status='processing',
                metadata__dispatch_state='checking_balance',
                metadata__dispatch_started_at__lt=stale_check_cutoff.isoformat(),
            ).update(
                status='queued',
                failure_reason='Payout is waiting for provider funds to become available.',
            )
            payment = PaymentRequest.objects.select_for_update().filter(
                payment_type='withdraw',
                status='queued',
            ).order_by('created_at', 'id').first()
            if payment is None:
                return 'empty'
            metadata = dict(payment.metadata or {})
            metadata['dispatch_state'] = 'checking_balance'
            metadata['dispatch_started_at'] = timezone.now().isoformat()
            payment.status = 'processing'
            payment.failure_reason = ''
            payment.metadata = metadata
            payment.save(update_fields=['status', 'failure_reason', 'metadata', 'updated_at'])

        gateway = IotecPayGateway()
        try:
            wallet_balance = gateway.provider_wallet_balance()
        except PaymentGatewayConfigurationError:
            _return_withdrawal_to_queue(
                payment.pk,
                reason='provider_not_configured',
                user_message='Payout is waiting for the payment provider to become available.',
            )
            return 'deferred'
        except PaymentGatewayError:
            _return_withdrawal_to_queue(
                payment.pk,
                reason='provider_balance_unavailable',
                user_message='Payout is waiting for the payment provider to become available.',
            )
            return 'deferred'

        if not isinstance(wallet_balance, dict):
            _return_withdrawal_to_queue(
                payment.pk,
                reason='provider_balance_invalid',
                user_message='Payout is waiting for the payment provider to become available.',
            )
            return 'deferred'

        currency = _currency(payment.user)
        provider_currency = str(wallet_balance.get('currency') or '').upper()
        available_value = wallet_balance.get('availableBalance')
        try:
            available_balance = Decimal(str(available_value))
        except (InvalidOperation, TypeError, ValueError):
            available_balance = Decimal('-1')
        if (
            provider_currency != currency.upper()
            or not available_balance.is_finite()
            or available_balance < 0
        ):
            _return_withdrawal_to_queue(
                payment.pk,
                reason='provider_balance_invalid',
                user_message='Payout is waiting for the payment provider to become available.',
            )
            return 'deferred'

        reserved = PaymentRequest.objects.filter(
            payment_type='withdraw',
            status__in=['pending', 'processing'],
        ).exclude(pk=payment.pk).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        if available_balance - reserved < payment.amount:
            _return_withdrawal_to_queue(
                payment.pk,
                reason='insufficient_provider_balance',
                user_message='Payout is waiting for provider funds to become available.',
            )
            return 'deferred'

        with db_transaction.atomic():
            locked_payment = PaymentRequest.objects.select_for_update().get(pk=payment.pk)
            if locked_payment.status != 'processing':
                return 'changed'
            metadata = dict(locked_payment.metadata or {})
            metadata['dispatch_state'] = 'submitting'
            metadata['dispatch_started_at'] = timezone.now().isoformat()
            locked_payment.metadata = metadata
            locked_payment.save(update_fields=['metadata', 'updated_at'])

        try:
            result = gateway.disburse_mobile_money(
                external_id=payment.external_reference,
                amount=payment.amount,
                currency=currency,
                payee=payment.phone_number,
                payee_name=payment.user.get_full_name() or payment.user.username,
                payee_email=payment.user.email,
            )
        except PaymentGatewayError as exc:
            if exc.insufficient_balance:
                _return_withdrawal_to_queue(
                    payment.pk,
                    reason='insufficient_provider_balance',
                    user_message='Payout is waiting for provider funds to become available.',
                    provider_code=exc.provider_code,
                )
                return 'deferred'
            result = PaymentGatewayResult(
                provider_reference=payment.provider_reference or payment.external_reference,
                status='Pending' if exc.retryable else 'Failed',
                status_code=exc.provider_code or 'gateway_error',
                status_message=(
                    'Provider confirmation is pending.'
                    if exc.retryable else 'Provider rejected the request.'
                ),
                raw={
                    'gateway_error_code': exc.provider_code or 'gateway_error',
                    'response_uncertain': exc.retryable,
                },
            )

        apply_provider_result(payment, result)
        return 'submitted'
    finally:
        PaymentDispatchLock.objects.filter(
            pk=1,
            lock_token=lock_token,
        ).update(lock_token='', locked_at=None)


def _return_withdrawal_to_queue(payment_id, *, reason, user_message, provider_code=''):
    with db_transaction.atomic():
        payment = PaymentRequest.objects.select_for_update().get(pk=payment_id)
        if payment.status != 'processing':
            return
        metadata = dict(payment.metadata or {})
        metadata['dispatch_state'] = 'queued'
        metadata['queue_reason'] = reason
        if provider_code:
            metadata['provider_error_code'] = provider_code[:80]
        payment.status = 'queued'
        payment.failure_reason = user_message
        payment.metadata = metadata
        payment.save(update_fields=['status', 'failure_reason', 'metadata', 'updated_at'])
        Transaction.objects.filter(
            user=payment.user,
            reference=payment.external_reference,
            transaction_type='withdraw',
        ).update(description=f'{payment.get_provider_display()} withdrawal queued')


def _validated_idempotency_key(key):
    key = str(key or '')
    if not key:
        key = uuid.uuid4().hex
    if not re.fullmatch(r'[0-9a-f]{32}', key):
        return '', 'Invalid payment request identifier. Refresh the page and try again.'
    return key, ''


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
        if locked_payment.payment_type == 'withdraw':
            metadata['dispatch_state'] = 'submitted'
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
    if payment.payment_type == 'withdraw' and (
        payment.status == 'queued'
        or (
            payment.status == 'processing'
            and (payment.metadata or {}).get('dispatch_state') == 'checking_balance'
        )
    ):
        raise PaymentGatewayError(
            'This withdrawal has not been submitted to the provider yet.',
        )
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
            profile.save(update_fields=['balance', 'phone_number'])
        else:
            profile.save(update_fields=['balance'])
        credit_first_deposit_referral_reward(payment)

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
