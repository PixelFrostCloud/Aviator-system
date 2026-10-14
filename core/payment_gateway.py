import json
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib import error, parse, request

from django.conf import settings


SUCCESS_STATUSES = {'Success'}
FAILED_STATUSES = {'Failed', 'Cancelled', 'Rejected', 'RolledBack'}
PROCESSING_STATUSES = {'Pending', 'SentToVendor', 'AwaitingApproval', 'Scheduled'}

PAYMENT_STATUS_MAP = {
    'Success': 'completed',
    'Failed': 'failed',
    'Cancelled': 'cancelled',
    'Rejected': 'failed',
    'RolledBack': 'failed',
    'Pending': 'pending',
    'SentToVendor': 'processing',
    'AwaitingApproval': 'processing',
    'Scheduled': 'processing',
}


class PaymentGatewayError(Exception):
    """Raised when ioTec Pay rejects a request or cannot be reached."""

    def __init__(
        self,
        message,
        *,
        retryable=False,
        provider_code='',
        insufficient_balance=False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.provider_code = provider_code
        self.insufficient_balance = insufficient_balance


class PaymentGatewayConfigurationError(PaymentGatewayError):
    """Raised when live credentials are required but missing."""


@dataclass(frozen=True)
class PaymentGatewayResult:
    provider_reference: str
    status: str
    status_code: str = ''
    status_message: str = ''
    redirect_url: str = ''
    raw: dict | None = None

    @property
    def local_status(self):
        return PAYMENT_STATUS_MAP.get(self.status, 'pending')

    @property
    def is_completed(self):
        return self.status in SUCCESS_STATUSES

    @property
    def is_failed(self):
        return self.status in FAILED_STATUSES


class IotecPayGateway:
    token_cache = {'access_token': '', 'expires_at': 0}

    def __init__(self):
        self.mode = getattr(settings, 'IOTEC_PAY_MODE', 'sandbox').lower()
        self.api_base_url = getattr(settings, 'IOTEC_PAY_API_BASE_URL', 'https://pay.iotec.io').rstrip('/')
        self.token_url = getattr(settings, 'IOTEC_PAY_TOKEN_URL', 'https://id.iotec.io/connect/token')
        self.client_id = getattr(settings, 'IOTEC_PAY_CLIENT_ID', '')
        self.client_secret = getattr(settings, 'IOTEC_PAY_CLIENT_SECRET', '')
        self.wallet_id = getattr(settings, 'IOTEC_PAY_WALLET_ID', '')
        self.timeout = int(getattr(settings, 'IOTEC_PAY_TIMEOUT_SECONDS', 20))
        self.charge_category = getattr(settings, 'IOTEC_PAY_CHARGE_CATEGORY', 'ChargeCustomer')
        self.channel = getattr(settings, 'IOTEC_PAY_CHANNEL', 'Api')

    @property
    def is_sandbox(self):
        return self.mode != 'live'

    def collect_mobile_money(self, *, external_id, amount, currency, payer, payer_name='', redirect_url=''):
        if self.is_sandbox:
            return self._sandbox_result(external_id, payer)

        body = {
            'category': 'MobileMoney',
            'currency': currency,
            'walletId': self._wallet_id(),
            'externalId': external_id,
            'payer': payer,
            'payerName': payer_name,
            'payerNote': 'Pakabet wallet deposit',
            'amount': self._amount(amount),
            'payeeNote': f'Pakabet deposit {external_id}',
            'channel': self.channel,
            'transactionChargesCategory': self.charge_category,
        }
        if redirect_url:
            body['redirectUrl'] = redirect_url
        return self._result_from_payload(self._post('/api/collections/collect', body), external_id)

    def disburse_mobile_money(self, *, external_id, amount, currency, payee, payee_name='', payee_email=''):
        if self.is_sandbox:
            return self._sandbox_result(external_id, payee, default_status='Pending')

        body = {
            'category': 'MobileMoney',
            'currency': currency,
            'walletId': self._wallet_id(),
            'externalId': external_id,
            'payee': payee,
            'payeeName': payee_name,
            'payeeEmail': payee_email,
            'amount': self._amount(amount),
            'payerNote': f'Pakabet withdrawal {external_id}',
            'payeeNote': 'Pakabet withdrawal',
            'channel': self.channel,
        }
        return self._result_from_payload(self._post('/api/disbursements/disburse', body), external_id)

    def sync_collection(self, external_id):
        if self.is_sandbox:
            return self._sandbox_result(external_id, external_id)
        return self._result_from_payload(self._get(f'/api/collections/external-id/{external_id}'), external_id)

    def sync_disbursement(self, external_id):
        if self.is_sandbox:
            return self._sandbox_result(external_id, external_id, default_status='Pending')
        return self._result_from_payload(self._get(f'/api/disbursements/external-id/{external_id}'), external_id)

    def provider_wallet_balance(self):
        if self.is_sandbox:
            return {
                'id': self.wallet_id or 'sandbox-wallet',
                'currency': getattr(settings, 'IOTEC_PAY_CURRENCY', 'UGX'),
                'actualBalance': 0,
                'availableBalance': 0,
                'sandbox': True,
            }
        return self._get(f'/api/wallet-balance/{self._wallet_id()}')

    def _wallet_id(self):
        if not self.wallet_id:
            raise PaymentGatewayConfigurationError('IOTEC_PAY_WALLET_ID is required for live ioTec Pay requests.')
        return self.wallet_id

    def _access_token(self):
        if self.token_cache['access_token'] and self.token_cache['expires_at'] > time.time() + 20:
            return self.token_cache['access_token']

        if not self.client_id or not self.client_secret:
            raise PaymentGatewayConfigurationError('IOTEC_PAY_CLIENT_ID and IOTEC_PAY_CLIENT_SECRET are required.')

        form = parse.urlencode({
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'client_credentials',
        }).encode()
        token_payload = self._json_request(
            self.token_url,
            data=form,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            auth=False,
        )
        token = token_payload.get('access_token')
        if not token:
            raise PaymentGatewayError('ioTec Pay did not return an access token.')

        self.token_cache = {
            'access_token': token,
            'expires_at': time.time() + int(token_payload.get('expires_in') or 300),
        }
        return token

    def _post(self, path, body):
        return self._json_request(
            f'{self.api_base_url}{path}',
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'},
        )

    def _get(self, path):
        return self._json_request(f'{self.api_base_url}{path}', data=None, headers={})

    def _json_request(self, url, *, data, headers, auth=True):
        headers = dict(headers)
        headers.setdefault('Accept', 'application/json')
        if auth:
            headers['Authorization'] = f'Bearer {self._access_token()}'

        if auth:
            headers['Authorization'] = f'Bearer {self._access_token()}'
        http_request = request.Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                raw = response.read().decode() or '{}'
        except TimeoutError as exc:
            raise PaymentGatewayError(
                'Could not confirm the ioTec Pay request before the timeout.',
                retryable=True,
            ) from exc
        except error.HTTPError as exc:
            body = exc.read().decode(errors='replace')
            retryable = exc.code >= 500 or exc.code in {408, 429}
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, list):
                payload = payload[0] if payload and isinstance(payload[0], dict) else {}
            if not isinstance(payload, dict):
                payload = {}
            provider_code = str(
                payload.get('statusCode')
                or payload.get('code')
                or payload.get('errorCode')
                or ''
            )[:80]
            provider_message = ' '.join(str(
                payload.get('statusMessage')
                or payload.get('message')
                or payload.get('error')
                or ''
            ).lower().split())
            code_text = provider_code.lower().replace('_', '').replace('-', '')
            insufficient_balance = (
                code_text in {
                    'insufficientbalance',
                    'insufficientfunds',
                    'walletbalanceinsufficient',
                }
                or 'insufficient balance' in provider_message
                or 'insufficient funds' in provider_message
                or 'not enough balance' in provider_message
            )
            raise PaymentGatewayError(
                f'ioTec Pay returned HTTP {exc.code}.',
                retryable=retryable,
                provider_code=provider_code,
                insufficient_balance=insufficient_balance,
            ) from exc
        except error.URLError as exc:
            raise PaymentGatewayError(
                f'Could not reach ioTec Pay: {exc.reason}',
                retryable=True,
            ) from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PaymentGatewayError(
                'ioTec Pay returned an invalid JSON response.',
                retryable=True,
            ) from exc

    def _result_from_payload(self, payload, fallback_reference):
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        provider_reference = str(
            payload.get('id')
            or payload.get('requestId')
            or payload.get('transactionId')
            or payload.get('externalId')
            or fallback_reference
        )
        return PaymentGatewayResult(
            provider_reference=provider_reference,
            status=payload.get('status') or 'Pending',
            status_code=payload.get('statusCode') or '',
            status_message=payload.get('statusMessage') or '',
            redirect_url=payload.get('redirectUrl') or '',
            raw=payload,
        )

    def _sandbox_result(self, external_id, test_value, default_status='Success'):
        test_value = str(test_value)
        status = default_status
        if '011177799' in test_value:
            status = 'Failed'
        elif '011177778' in test_value:
            status = 'Pending'
        elif '011177779' in test_value:
            status = 'SentToVendor'
        return PaymentGatewayResult(
            provider_reference=f'SBX-{external_id}',
            status=status,
            status_code=status.lower(),
            status_message=f'Sandbox ioTec {status} response.',
            raw={'sandbox': True, 'externalId': external_id, 'status': status},
        )

    @staticmethod
    def _amount(amount):
        return float(Decimal(amount).quantize(Decimal('0.01')))
