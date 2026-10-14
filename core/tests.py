import json
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO
from itertools import count
from unittest.mock import patch
from urllib.error import HTTPError

from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import authenticate
from django.contrib.auth.models import AnonymousUser, User
from django.core import mail
from django.http import Http404, HttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.template.loader import render_to_string
from django.urls import path, resolve
from django.utils import timezone

from .abuse import (
    authorize_room_socket,
    detect_account_creation_pattern,
    detect_payment_pattern,
    detect_reward_claim_pattern,
)
from .consumers import RoomSyncConsumer
from .forms import (
    AccountPasswordChangeForm,
    AccountPasswordResetForm,
    AccountSetPasswordForm,
    WalletForm,
)
from .models import AuditLog, ChatMessage, ChatReaction, ChatRain, ChatRainRecipient, FinancialReport, GamePresence, GameRound, GameSession, PaymentDispatchLock, PaymentRequest, Profile, Referral, RewardClaim, RewardEvent, RewardTransaction, RewardWallet, RiskAlert, Room, RoomPlayer, SelfExclusion, SharedGameState, Transaction
from .payment_gateway import IotecPayGateway, PaymentGatewayError, PaymentGatewayResult
from .payment_services import apply_provider_result, initiate_deposit, initiate_withdrawal, process_next_queued_withdrawal
from .reward_services import claim_reward
from .utils import activate_self_exclusion, calculate_crash_point, can_deposit, create_game_round, generate_financial_report, hash_server_seed, is_self_excluded, reveal_game_round, verify_game_round
from .views import play, request_payload
from . import security
from .security import SecurityEventMiddleware


_BET_REQUEST_IDS = count(1)


def bet_payload(*stakes):
    return json.dumps([
        {'amount': stake, 'request_id': f'{next(_BET_REQUEST_IDS):032x}'}
        for stake in stakes
    ])


def room_socket_application(user):
    router = URLRouter([
        path('ws/room/<int:room_id>/', RoomSyncConsumer.as_asgi()),
    ])

    async def add_user(scope, receive, send):
        await router({**scope, 'user': user}, receive, send)

    return add_user


class AuthenticationTests(TestCase):
    def test_auth_pages_display_clear_placeholders_and_forgot_password_link(self):
        from .forms import PhoneOrUsernameAuthenticationForm, RegisterForm

        login_page = render_to_string('core/login.html', {
            'form': PhoneOrUsernameAuthenticationForm(),
            'next': '',
        })
        register_page = render_to_string('core/register.html', {
            'form': RegisterForm(),
        })

        self.assertIn('Username or 07XXXXXXXX', login_page)
        self.assertIn('Enter your password', login_page)
        self.assertIn('Forgot password?', login_page)
        self.assertIn('Choose a username', register_page)
        self.assertIn('name@example.com', register_page)
        self.assertIn('Create a password', register_page)

    def test_password_forms_have_professional_placeholders_and_autocomplete(self):
        user = User.objects.create_user(username='placeholder-user', password='strong-test-pass')

        change_form = AccountPasswordChangeForm(user)
        reset_form = AccountPasswordResetForm()
        set_password_form = AccountSetPasswordForm(user)

        self.assertEqual(
            change_form.fields['old_password'].widget.attrs['placeholder'],
            'Enter your current password',
        )
        self.assertEqual(
            change_form.fields['new_password1'].widget.attrs['autocomplete'],
            'new-password',
        )
        self.assertEqual(
            reset_form.fields['email'].widget.attrs['placeholder'],
            'Enter the email on your account',
        )
        self.assertEqual(
            set_password_form.fields['new_password2'].widget.attrs['placeholder'],
            'Enter your new password again',
        )
        reset_page = render_to_string('core/password_reset_form.html', {
            'form': reset_form,
        })
        reset_confirm_page = render_to_string('core/password_reset_confirm.html', {
            'form': set_password_form,
            'validlink': True,
        })
        self.assertIn('Enter the email on your account', reset_page)
        self.assertIn('Choose a new password', reset_confirm_page)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_password_reset_sends_one_time_link_and_completes_end_to_end(self):
        user = User.objects.create_user(
            username='reset-player',
            email='reset-player@example.com',
            password='strong-test-pass',
        )

        request = RequestFactory().post(
            '/account/password/reset/',
            {'email': 'reset-player@example.com'},
            HTTP_HOST='testserver',
        )
        request._dont_enforce_csrf_checks = True
        response = resolve('/account/password/reset/').func(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/account/password/reset/sent/')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Reset your Pakabet password', mail.outbox[0].subject)
        self.assertIn('one hour', mail.outbox[0].body)
        reset_path = mail.outbox[0].body.split('http://testserver', 1)[1].split()[0]
        uidb64, token = reset_path.rstrip('/').split('/')[-2:]
        reset_link_view = resolve(reset_path).func
        confirm_get = RequestFactory().get(reset_path, HTTP_HOST='testserver')
        confirm_get.session = {}
        confirm_response = reset_link_view(confirm_get, uidb64=uidb64, token=token)
        self.assertEqual(confirm_response.status_code, 302)
        reset_path = confirm_response['Location']

        reset_post = RequestFactory().post(reset_path, {
            'new_password1': 'New-reset-password-2026!',
            'new_password2': 'New-reset-password-2026!',
        }, HTTP_HOST='testserver')
        reset_post._dont_enforce_csrf_checks = True
        reset_post.session = confirm_get.session
        reset = resolve(reset_path).func(reset_post, uidb64=uidb64, token='set-password')

        self.assertEqual(reset.status_code, 302)
        self.assertEqual(reset['Location'], '/account/password/reset/complete/')
        user.refresh_from_db()
        self.assertTrue(user.check_password('New-reset-password-2026!'))
        complete_request = RequestFactory().get('/account/password/reset/complete/')
        complete_page = resolve('/account/password/reset/complete/').func(complete_request)
        self.assertIn('Password updated', complete_page.render().content.decode())
        invalid_request = RequestFactory().get(reset_path)
        invalid_request.session = confirm_get.session
        invalid_link = resolve(reset_path).func(invalid_request, uidb64=uidb64, token='set-password')
        self.assertIn('no longer valid', invalid_link.render().content.decode())

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_password_reset_response_does_not_disclose_unknown_email(self):
        missing_request = RequestFactory().post(
            '/account/password/reset/',
            {'email': 'no-account@example.com'},
            HTTP_HOST='testserver',
        )
        missing_request._dont_enforce_csrf_checks = True
        existing = resolve('/account/password/reset/').func(missing_request)
        missing_account_mail_count = len(mail.outbox)

        user = User.objects.create_user(
            username='existing-reset-player',
            email='existing-reset@example.com',
            password='strong-test-pass',
        )
        matching_request = RequestFactory().post(
            '/account/password/reset/',
            {'email': user.email},
            HTTP_HOST='testserver',
        )
        matching_request._dont_enforce_csrf_checks = True
        matching = resolve('/account/password/reset/').func(matching_request)

        self.assertEqual(existing.status_code, 302)
        self.assertEqual(existing['Location'], '/account/password/reset/sent/')
        self.assertEqual(matching.status_code, 302)
        self.assertEqual(matching['Location'], '/account/password/reset/sent/')
        self.assertEqual(missing_account_mail_count, 0)
        self.assertEqual(len(mail.outbox), 1)

        User.objects.create_user(
            username='duplicate-reset-player',
            email=user.email,
            password='strong-test-pass',
        )
        duplicate_request = RequestFactory().post(
            '/account/password/reset/',
            {'email': user.email},
            HTTP_HOST='testserver',
        )
        duplicate_request._dont_enforce_csrf_checks = True
        duplicate_response = resolve('/account/password/reset/').func(duplicate_request)

        self.assertEqual(duplicate_response.status_code, 302)
        self.assertEqual(duplicate_response['Location'], '/account/password/reset/sent/')
        self.assertEqual(len(mail.outbox), 1)

    def test_protected_account_pages_redirect_to_project_login(self):
        response = self.client.get('/profile/')

        self.assertRedirects(response, '/login/?next=/profile/')

    def test_guest_game_actions_require_login(self):
        bet_response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })

        self.assertEqual(bet_response.status_code, 401)
        self.assertFalse(GameRound.objects.exists())

    def test_wallet_requires_login_and_rooms_page_returns_to_game(self):
        wallet_response = self.client.get('/wallet/')
        rooms_response = self.client.get('/rooms/')

        self.assertEqual(wallet_response.status_code, 302)
        self.assertEqual(wallet_response.url, '/login/?next=/wallet/')
        self.assertEqual(rooms_response.status_code, 302)
        self.assertEqual(rooms_response.url, '/play/')

    def test_registration_requires_phone_number(self):
        response = self.client.post('/register/', {
            'username': 'new-player',
            'email': 'new-player@example.com',
            'password1': 'strong-test-pass',
            'password2': 'strong-test-pass',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This field is required.')
        self.assertFalse(User.objects.filter(username='new-player').exists())

    def test_user_can_register_and_login_with_phone_number(self):
        registration = self.client.post('/register/', {
            'username': 'phone-player',
            'phone_number': '0700 000 001',
            'email': 'phone-player@example.com',
            'password1': 'strong-test-pass',
            'password2': 'strong-test-pass',
        })

        self.assertRedirects(registration, '/play/')
        user = User.objects.get(username='phone-player')
        self.assertEqual(user.profile.phone_number, '256700000001')

        self.client.logout()
        login_response = self.client.post('/login/', {
            'username': '0700000001',
            'password': 'strong-test-pass',
        })

        self.assertRedirects(login_response, '/play/')
        self.assertEqual(int(self.client.session['_auth_user_id']), user.id)

    def test_registration_saves_selected_east_african_country_code(self):
        registration = self.client.post('/register/', {
            'username': 'kenya-player',
            'phone_number': '712345678',
            'country_code': '254',
            'email': 'kenya-player@example.com',
            'password1': 'strong-test-pass',
            'password2': 'strong-test-pass',
        })

        self.assertRedirects(registration, '/play/')
        self.assertEqual(User.objects.get(username='kenya-player').profile.phone_number, '254712345678')


class SecurityMonitoringTests(TestCase):
    def setUp(self):
        security._configuration_check_complete = False

    def tearDown(self):
        security._configuration_check_complete = False

    @override_settings(
        ADMINS=[('Security', 'security@example.com')],
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        SECURITY_LOGIN_FAILURE_THRESHOLD=2,
        SECURITY_LOGIN_FAILURE_WINDOW_SECONDS=600,
        SECURITY_ALERT_COOLDOWN_SECONDS=900,
    )
    def test_repeated_failed_logins_alert_without_recording_password(self):
        submitted_password = 'never-store-this-password'
        request = RequestFactory().post('/login/', REMOTE_ADDR='203.0.113.24')
        authenticate(request=request, username='unknown-player', password=submitted_password)
        authenticate(request=request, username='unknown-player', password=submitted_password)

        login_failures = AuditLog.objects.filter(
            action_type='risk_alert',
            data__event='login_failed',
        )
        self.assertEqual(login_failures.count(), 2)
        self.assertEqual(AuditLog.objects.filter(
            action_type='risk_alert',
            data__event='login_failure_alert',
        ).count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('repeated failed logins', mail.outbox[0].subject)
        self.assertNotIn(submitted_password, mail.outbox[0].body)
        for entry in AuditLog.objects.filter(action_type='risk_alert'):
            self.assertNotIn(submitted_password, str(entry.data))
            self.assertNotIn(submitted_password, entry.description)

    def test_forbidden_responses_are_logged_with_route_not_request_data(self):
        request = RequestFactory().post(
            '/wallet/',
            {'payment_token': 'do-not-log-this'},
            REMOTE_ADDR='203.0.113.12',
        )
        request.user = AnonymousUser()
        request.resolver_match = resolve('/wallet/')
        response = SecurityEventMiddleware(
            lambda _request: HttpResponse(status=403),
        )(request)

        self.assertEqual(response.status_code, 403)
        event = AuditLog.objects.get(
            action_type='risk_alert',
            data__event='permission_denied',
        )
        self.assertEqual(event.ip_address, '203.0.113.12')
        self.assertEqual(event.data['route'], 'wallet/')
        self.assertNotIn('payment_token', str(event.data))
        self.assertNotIn('do-not-log-this', str(event.data))

    @override_settings(
        ADMINS=[('Security', 'security@example.com')],
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        IOTEC_PAY_CALLBACK_TOKEN='do-not-log-this-callback-token',
    )
    def test_security_configuration_changes_alert_without_recording_secrets(self):
        middleware = SecurityEventMiddleware(lambda _request: HttpResponse(status=200))
        request = RequestFactory().get('/', REMOTE_ADDR='203.0.113.20')
        request.user = AnonymousUser()
        middleware(request)

        security._configuration_check_complete = False
        with override_settings(DEBUG=not settings.DEBUG):
            middleware(request)

        snapshot = AuditLog.objects.filter(
            action_type='risk_alert',
            data__event='configuration_snapshot',
        ).order_by('-created_at').first()
        self.assertIn('changed_settings', snapshot.data)
        self.assertIn('debug', snapshot.data['changed_settings'])
        self.assertNotIn('do-not-log-this-callback-token', str(snapshot.data))
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn('do-not-log-this-callback-token', mail.outbox[0].body)

    def test_audit_logs_are_visible_only_to_superusers_and_never_editable(self):
        log_admin = admin.site._registry[AuditLog]
        request = RequestFactory().get('/admin/core/auditlog/')
        staff_user = User(is_staff=True, is_superuser=False)
        request.user = staff_user

        self.assertFalse(log_admin.has_view_permission(request))
        self.assertFalse(log_admin.has_add_permission(request))
        self.assertFalse(log_admin.has_change_permission(request))
        self.assertFalse(log_admin.has_delete_permission(request))


class ReferralFeatureTests(TestCase):
    def setUp(self):
        self.inviter = User.objects.create_user(
            username='inviter-player',
            email='inviter@example.com',
            password='strong-test-pass',
        )
        self.inviter_profile = Profile.objects.create(
            user=self.inviter,
            balance=Decimal('1000.00'),
        )

    def create_invitee(self, username='invited-player'):
        invitee = User.objects.create_user(
            username=username,
            email=f'{username}@example.com',
            password='strong-test-pass',
        )
        profile = Profile.objects.create(user=invitee, balance=Decimal('1000.00'))
        return invitee, profile

    def test_invite_link_attributes_signup_without_manual_referral_code(self):
        landing = self.client.get(f'/invite/{self.inviter_profile.referral_code}/')

        self.assertRedirects(landing, '/register/')
        self.assertEqual(
            self.client.session['referral_code'],
            str(self.inviter_profile.referral_code),
        )
        registration_form = self.client.get('/register/')
        self.assertContains(registration_form, 'inviter-player')

        registration = self.client.post('/register/', {
            'username': 'new-invitee',
            'phone_number': '0700000002',
            'email': 'new-invitee@example.com',
            'password1': 'strong-test-pass',
            'password2': 'strong-test-pass',
        })

        self.assertRedirects(registration, '/play/')
        referral = Referral.objects.get(invitee__username='new-invitee')
        self.assertEqual(referral.inviter, self.inviter)
        self.assertFalse(self.client.session.get('referral_code'))

    def test_referred_account_can_see_inviter_username(self):
        invitee, _ = self.create_invitee()
        Referral.objects.create(inviter=self.inviter, invitee=invitee)
        self.client.force_login(invitee)

        response = self.client.get('/profile/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Invited by:')
        self.assertContains(response, self.inviter.username)

    def test_referral_page_shows_personal_link_invited_count_and_rewards(self):
        invitee, _ = self.create_invitee()
        referral = Referral.objects.create(inviter=self.inviter, invitee=invitee)
        reward = Transaction.objects.create(
            user=self.inviter,
            transaction_type='bonus',
            amount=Decimal('25.00'),
            status='completed',
            reference=f'REFERRAL-{referral.pk}',
            description='Referral reward funded by Pakabet',
        )
        referral.reward_amount = Decimal('25.00')
        referral.reward_transaction = reward
        referral.rewarded_at = timezone.now()
        referral.save(update_fields=['reward_amount', 'reward_transaction', 'rewarded_at'])
        self.client.force_login(self.inviter)

        response = self.client.get('/referrals/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_invited'], 1)
        self.assertEqual(response.context['total_earned'], Decimal('25.00'))
        self.assertContains(response, f'/invite/{self.inviter_profile.referral_code}/')
        self.assertContains(response, invitee.username)
        self.assertContains(response, 'Reward paid')

    def test_first_successful_deposit_funds_single_withdrawable_inviter_reward(self):
        invitee, invitee_profile = self.create_invitee()
        referral = Referral.objects.create(inviter=self.inviter, invitee=invitee)
        first_deposit = PaymentRequest.objects.create(
            user=invitee,
            payment_type='deposit',
            provider='mtn_momo',
            amount=Decimal('1801.00'),
            phone_number='256700000000',
            external_reference='DEP-REFERRAL-001',
            status='pending',
            metadata={'gateway': 'iotec_pay'},
        )
        success = PaymentGatewayResult(
            provider_reference='provider-referral-001',
            status='Success',
            status_code='success',
            status_message='Completed',
        )

        apply_provider_result(first_deposit, success)
        apply_provider_result(first_deposit, success)

        self.inviter_profile.refresh_from_db()
        invitee_profile.refresh_from_db()
        referral.refresh_from_db()
        bonus = Transaction.objects.get(reference=f'REFERRAL-{referral.pk}')
        self.assertEqual(self.inviter_profile.balance, Decimal('1090.05'))
        self.assertEqual(invitee_profile.balance, Decimal('2801.00'))
        self.assertEqual(bonus.amount, Decimal('90.05'))
        self.assertEqual(bonus.transaction_type, 'bonus')
        self.assertIn('funded by Pakabet', bonus.description)
        self.assertEqual(referral.first_deposit, first_deposit)
        self.assertEqual(referral.reward_transaction, bonus)

        second_deposit = PaymentRequest.objects.create(
            user=invitee,
            payment_type='deposit',
            provider='mtn_momo',
            amount=Decimal('500.00'),
            phone_number='256700000000',
            external_reference='DEP-REFERRAL-002',
            status='pending',
            metadata={'gateway': 'iotec_pay'},
        )
        apply_provider_result(second_deposit, PaymentGatewayResult(
            provider_reference='provider-referral-002',
            status='Success',
            status_code='success',
            status_message='Completed',
        ))
        self.inviter_profile.refresh_from_db()
        self.assertEqual(self.inviter_profile.balance, Decimal('1090.05'))
        self.assertEqual(
            Transaction.objects.filter(user=self.inviter, transaction_type='bonus').count(),
            1,
        )


class IotecGatewayRequestTests(TestCase):
    @patch('core.payment_gateway.request.urlopen', side_effect=TimeoutError('timed out'))
    def test_socket_timeout_is_retryable(self, urlopen):
        gateway = IotecPayGateway()
        gateway.token_cache = {'access_token': 'test-token', 'expires_at': 9999999999}

        with self.assertRaises(PaymentGatewayError) as raised:
            gateway._json_request('https://pay.example.test/status', data=None, headers={})

        self.assertTrue(raised.exception.retryable)
        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-token')

    @patch(
        'core.payment_gateway.request.urlopen',
        side_effect=HTTPError(
            'https://pay.example.test/disburse',
            400,
            'Bad Request',
            {},
            BytesIO(b'{"code":"InsufficientBalance","message":"Insufficient funds"}'),
        ),
    )
    def test_insufficient_balance_error_is_classified_without_exposing_response_body(self, urlopen):
        gateway = IotecPayGateway()

        with self.assertRaises(PaymentGatewayError) as raised:
            gateway._json_request(
                'https://pay.example.test/disburse',
                data=b'{}',
                headers={},
                auth=False,
            )

        self.assertTrue(raised.exception.insufficient_balance)
        self.assertEqual(raised.exception.provider_code, 'InsufficientBalance')
        self.assertNotIn('Insufficient funds', str(raised.exception))
        urlopen.assert_called_once()

    @override_settings(
        IOTEC_PAY_MODE='live',
        IOTEC_PAY_WALLET_ID='wallet-001',
    )
    @patch.object(IotecPayGateway, '_post')
    def test_collection_uses_iotec_mobile_money_endpoint(self, post):
        post.return_value = {
            'id': 'provider-collection-001',
            'status': 'Pending',
            'statusCode': 'pending',
        }

        result = IotecPayGateway().collect_mobile_money(
            external_id='DEP-001',
            amount=Decimal('500.00'),
            currency='UGX',
            payer='256700000001',
            payer_name='Phone Player',
        )

        self.assertEqual(result.status, 'Pending')
        post.assert_called_once_with('/api/collections/collect', {
            'category': 'MobileMoney',
            'currency': 'UGX',
            'walletId': 'wallet-001',
            'externalId': 'DEP-001',
            'payer': '256700000001',
            'payerName': 'Phone Player',
            'payerNote': 'Pakabet wallet deposit',
            'amount': 500.0,
            'payeeNote': 'Pakabet deposit DEP-001',
            'channel': 'Api',
            'transactionChargesCategory': 'ChargeCustomer',
        })

    @override_settings(
        IOTEC_PAY_MODE='live',
        IOTEC_PAY_WALLET_ID='wallet-001',
    )
    @patch.object(IotecPayGateway, '_post')
    def test_disbursement_uses_iotec_mobile_money_endpoint(self, post):
        post.return_value = {
            'id': 'provider-disbursement-001',
            'status': 'Pending',
            'statusCode': 'pending',
        }

        result = IotecPayGateway().disburse_mobile_money(
            external_id='WDR-001',
            amount=Decimal('500.00'),
            currency='UGX',
            payee='256750000001',
            payee_name='Phone Player',
            payee_email='phone-player@example.com',
        )

        self.assertEqual(result.status, 'Pending')
        post.assert_called_once_with('/api/disbursements/disburse', {
            'category': 'MobileMoney',
            'currency': 'UGX',
            'walletId': 'wallet-001',
            'externalId': 'WDR-001',
            'payee': '256750000001',
            'payeeName': 'Phone Player',
            'payeeEmail': 'phone-player@example.com',
            'amount': 500.0,
            'payerNote': 'Pakabet withdrawal WDR-001',
            'payeeNote': 'Pakabet withdrawal',
            'channel': 'Api',
        })


class ProvablyFairTests(TestCase):
    def test_crash_point_is_deterministic_and_verifiable(self):
        server_seed = 'server-seed-for-test'
        client_seed = 'client-seed-for-test'
        crash_point = calculate_crash_point(server_seed, client_seed)

        self.assertEqual(crash_point, calculate_crash_point(server_seed, client_seed))
        self.assertTrue(
            verify_game_round(
                server_seed,
                client_seed,
                hash_server_seed(server_seed),
                crash_point,
            )
        )

    def test_round_verification_uses_the_rounds_recorded_house_edge(self):
        server_seed = 'server-seed-with-custom-edge'
        client_seed = 'client-seed-with-custom-edge'
        house_edge = Decimal('0.0250')
        crash_point = calculate_crash_point(server_seed, client_seed, house_edge)

        self.assertTrue(
            verify_game_round(
                server_seed,
                client_seed,
                hash_server_seed(server_seed),
                crash_point,
                house_edge,
            )
        )

    def test_saved_client_seed_is_used_for_new_rounds(self):
        user = User.objects.create_user(username='seed-player', password='strong-test-pass')
        profile = Profile.objects.create(user=user, client_seed='custom-seed-2026')

        game_round = create_game_round(user)

        self.assertEqual(game_round.client_seed, profile.client_seed)

    def test_round_verification_page_displays_real_round_evidence(self):
        user = User.objects.create_user(username='verify-player', password='strong-test-pass')
        Profile.objects.create(user=user, balance=Decimal('1000.00'), client_seed='verify-seed')
        game_round = create_game_round(user)
        GameSession.objects.create(
            user=user,
            round=game_round,
            stake=Decimal('10.00'),
            crash_point=game_round.crash_point,
        )
        reveal_game_round(game_round)
        self.client.force_login(user)

        response = self.client.get('/provably-fair/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['rounds'][0].verified)
        self.assertContains(response, game_round.server_seed_hash)
        self.assertContains(response, 'verify-seed')
        self.assertContains(response, game_round.server_seed)
        self.assertContains(response, 'Verified')


class PlayEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='player',
            email='player@example.com',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(user=self.user, balance=Decimal('1000.00'))
        self.client.login(username='player', password='strong-test-pass')

    def test_online_count_endpoint_counts_unique_recent_players_from_server_presence(self):
        other_user = User.objects.create_user(
            username='other-player',
            password='strong-test-pass',
        )
        active_time = timezone.now()
        GamePresence.objects.create(
            user=self.user,
            presence_key='a' * 32,
            last_seen=active_time,
        )
        GamePresence.objects.create(
            user=self.user,
            presence_key='b' * 32,
            last_seen=active_time,
        )
        GamePresence.objects.create(
            user=other_user,
            presence_key='c' * 32,
            last_seen=active_time,
        )
        GamePresence.objects.create(
            user=other_user,
            presence_key='d' * 32,
            last_seen=active_time - timedelta(minutes=5),
        )

        response = self.client.get('/api/online-count/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'online_count': 2})

    def test_presence_leave_removes_only_the_current_browser_presence(self):
        presence_key = 'a' * 32
        GamePresence.objects.create(
            user=self.user,
            presence_key=presence_key,
            last_seen=timezone.now(),
        )
        GamePresence.objects.create(
            user=self.user,
            presence_key='b' * 32,
            last_seen=timezone.now(),
        )

        response = self.client.post('/play/', {
            'action': 'presence_leave',
            'presence_key': presence_key,
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            GamePresence.objects.filter(
                user=self.user,
                presence_key=presence_key,
            ).exists()
        )
        self.assertTrue(
            GamePresence.objects.filter(
                user=self.user,
                presence_key='b' * 32,
            ).exists()
        )

    def test_play_page_renders_actual_initial_online_count(self):
        GamePresence.objects.create(
            user=self.user,
            presence_key='a' * 32,
            last_seen=timezone.now(),
        )

        with patch('core.views.render', return_value=HttpResponse('ok')) as render_page:
            response = self.client.get('/play/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(render_page.call_args.args[2]['online_count'], 1)

    def test_fairness_modal_shows_saved_seed_and_players_latest_commitment(self):
        game_round = create_game_round(self.user)
        GameSession.objects.create(
            user=self.user,
            round=game_round,
            stake=Decimal('10.00'),
            crash_point=game_round.crash_point,
        )

        response = self.client.get('/play/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'value="{self.profile.client_seed}"')
        self.assertContains(response, game_round.server_seed_hash)
        self.assertContains(response, 'data-client-seed-url="/api/provably-fair/client-seed/"')
        self.assertContains(response, 'View and verify your completed rounds')

    def test_start_round_commits_seed_and_debits_wallet(self):
        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10, 20),
        })

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.balance, Decimal('970.00'))
        self.assertEqual(len(payload['session_ids']), 2)
        self.assertEqual(payload['client_seed'], self.profile.client_seed)
        self.assertEqual(GameRound.objects.get(public_id=payload['round_id']).server_seed, '')
        self.assertTrue(payload['server_seed_hash'])
        self.assertEqual(Transaction.objects.get(reference=payload['round_id'], transaction_type='bet').amount, Decimal('30.00'))

    def test_client_seed_can_be_updated_and_invalid_values_are_rejected(self):
        response = self.client.post('/api/provably-fair/client-seed/', {
            'client_seed': 'my-custom-seed:2026',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['client_seed'], 'my-custom-seed:2026')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.client_seed, 'my-custom-seed:2026')

        invalid_response = self.client.post('/api/provably-fair/client-seed/', {
            'client_seed': 'invalid seed with spaces',
        })
        self.assertEqual(invalid_response.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.client_seed, 'my-custom-seed:2026')

    def test_start_round_does_not_reveal_crash_point(self):
        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('crash_point', response.json())

    @patch('core.utils.calculate_crash_point', return_value=Decimal('3.21'))
    def test_bet_ignores_client_balance_and_round_odds(self, _calculate_crash_point):
        forged_bet = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([{
                'amount': 10,
                'crash_point': '99999.99',
                'odds': '99999.99',
            }]),
        })
        self.assertEqual(forged_bet.status_code, 400)

        response = self.client.post('/play/', {
            'action': 'start',
            'balance': '999999999.99',
            'crash_point': '99999.99',
            'bet_data': json.dumps([{
                'amount': 10,
                'request_id': 'f' * 32,
            }]),
        })

        self.assertEqual(response.status_code, 200)
        game_round = GameRound.objects.get(public_id=response.json()['round_id'])
        session = GameSession.objects.get(id=response.json()['session_ids'][0])
        self.profile.refresh_from_db()

        self.assertEqual(game_round.crash_point, Decimal('3.21'))
        self.assertEqual(session.crash_point, game_round.crash_point)
        self.assertEqual(self.profile.balance, Decimal('990.00'))

    @patch('core.utils.calculate_crash_point', return_value=Decimal('3.21'))
    def test_sql_injection_shaped_bet_and_cashout_inputs_are_rejected(self, _calculate_crash_point):
        original_balance = self.profile.balance
        invalid_bet = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps(['10); UPDATE core_profile SET balance=999999']),
        })
        self.assertEqual(invalid_bet.status_code, 400)

        valid_bet = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })
        self.assertEqual(valid_bet.status_code, 200)
        session = GameSession.objects.get(id=valid_bet.json()['session_ids'][0])
        balance_after_bet = original_balance - Decimal('10.00')

        invalid_cashout = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': f'{session.id} OR 1=1',
        })

        session.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(invalid_cashout.status_code, 400)
        self.assertIsNone(session.cashed_out_at)
        self.assertEqual(self.profile.balance, balance_after_bet)

    def test_state_opens_one_shared_betting_window_and_flight_starts_at_its_deadline(self):
        first_state = self.client.post('/play/', {'action': 'state'}).json()
        second_state = self.client.post('/play/', {'action': 'state'}).json()
        game_round = GameRound.objects.get(public_id=first_state['round_id'])

        self.assertEqual(first_state['status'], 'betting')
        self.assertEqual(second_state['round_id'], first_state['round_id'])
        self.assertEqual(second_state['status'], 'betting')
        self.assertIn('server_time', first_state)
        self.assertEqual(GameRound.objects.count(), 1)

        game_round.crash_point = Decimal('40.00')
        game_round.betting_ends_at = timezone.now() - timedelta(milliseconds=1)
        game_round.save(update_fields=['crash_point', 'betting_ends_at'])
        flight_state = self.client.post('/play/', {'action': 'state'}).json()

        game_round.refresh_from_db()
        self.assertEqual(flight_state['status'], 'flying')
        self.assertEqual(game_round.status, 'flying')
        self.assertEqual(game_round.started_at, game_round.betting_ends_at)
        self.assertIn('server_time', flight_state)
        snapshot_time = datetime.fromisoformat(flight_state['server_time'])
        elapsed = max((snapshot_time - game_round.started_at).total_seconds(), 0)
        expected_multiplier = (
            Decimal('1.00') + Decimal(str(elapsed)) * Decimal('0.10')
        ).quantize(Decimal('0.01'))
        self.assertEqual(Decimal(str(flight_state['current_multiplier'])), expected_multiplier)

    def test_guest_state_starts_a_shared_round_after_the_previous_round_crashes(self):
        crashed_round = create_game_round(self.user)
        crashed_round.status = 'crashed'
        crashed_round.server_seed = crashed_round.server_seed_private
        crashed_round.save(update_fields=['status', 'server_seed'])
        SharedGameState.objects.create(active_round=crashed_round)
        self.client.logout()

        state = self.client.post('/play/', {'action': 'state'}).json()

        self.assertEqual(state['status'], 'betting')
        new_round = GameRound.objects.get(public_id=state['round_id'])
        self.assertIsNone(new_round.created_by)
        self.assertEqual(SharedGameState.objects.get(pk=1).active_round, new_round)

    def test_bet_request_is_idempotent_within_the_shared_betting_window(self):
        request_id = 'a' * 32
        bet_data = json.dumps([{
            'amount': 25,
            'auto_cashout_at': 2,
            'request_id': request_id,
        }])

        first = self.client.post('/play/', {'action': 'start', 'bet_data': bet_data})
        retry = self.client.post('/play/', {'action': 'start', 'bet_data': bet_data})

        self.profile.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(first.json()['session_ids'], retry.json()['session_ids'])
        self.assertEqual(GameSession.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self.profile.balance, Decimal('975.00'))
        session = GameSession.objects.get(user=self.user)
        self.assertEqual(session.auto_cashout_at, Decimal('2.00'))

        changed_retry = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([{
                'amount': 30,
                'auto_cashout_at': 2,
                'request_id': request_id,
            }]),
        })
        self.profile.refresh_from_db()
        self.assertEqual(changed_retry.status_code, 409)
        self.assertEqual(self.profile.balance, Decimal('975.00'))

    def test_bet_request_requires_a_request_identifier(self):
        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([10]),
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(GameSession.objects.filter(user=self.user).count(), 0)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('1000.00'))

    @override_settings(
        SECURITY_BET_ACCOUNT_LIMIT=1,
        SECURITY_BET_IP_LIMIT=100,
        SECURITY_GAME_RATE_WINDOW_SECONDS=60,
    )
    def test_bet_rate_limit_records_signal_and_rejects_excess_request(self):
        first = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })
        second = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertTrue(second.json()['rate_limited'])
        self.assertEqual(GameSession.objects.filter(user=self.user).count(), 1)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('990.00'))
        self.assertTrue(AuditLog.objects.filter(
            action_type='risk_alert',
            data__event='rate_limit_exceeded',
            data__action='bet',
            user=self.user,
        ).exists())

    @override_settings(
        SECURITY_BET_ACCOUNT_LIMIT=100,
        SECURITY_BET_IP_LIMIT=1,
        SECURITY_GAME_RATE_WINDOW_SECONDS=60,
    )
    def test_bet_rate_limit_is_shared_across_accounts_at_the_same_ip(self):
        first = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })
        other_user = User.objects.create_user(
            username='same-ip-player',
            email='same-ip@example.com',
            password='strong-test-pass',
        )
        other_profile = Profile.objects.create(user=other_user, balance=Decimal('1000.00'))
        self.client.force_login(other_user)
        second = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(GameSession.objects.filter(user=other_user).count(), 0)
        other_profile.refresh_from_db()
        self.assertEqual(other_profile.balance, Decimal('1000.00'))

    def test_insufficient_balance_bet_returns_specific_code_without_debit(self):
        self.profile.balance = Decimal('10.00')
        self.profile.save(update_fields=['balance'])

        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(25),
        })

        self.profile.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'insufficient_balance')
        self.assertEqual(response.json()['balance'], 10.0)
        self.assertEqual(self.profile.balance, Decimal('10.00'))
        self.assertFalse(GameSession.objects.filter(user=self.user).exists())

    def test_invalid_or_repeated_bet_requests_do_not_create_zero_value_bets(self):
        tiny_bet = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([{
                'amount': 0.001,
                'request_id': 'c' * 32,
            }]),
        })
        duplicate_ids = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([
                {'amount': 5, 'request_id': 'd' * 32},
                {'amount': 10, 'request_id': 'd' * 32},
            ]),
        })

        self.profile.refresh_from_db()
        self.assertEqual(tiny_bet.status_code, 400)
        self.assertEqual(duplicate_ids.status_code, 400)
        self.assertEqual(self.profile.balance, Decimal('1000.00'))
        self.assertFalse(GameSession.objects.filter(user=self.user).exists())

    def test_bet_after_shared_deadline_is_rejected_without_debit(self):
        state = self.client.post('/play/', {'action': 'state'}).json()
        game_round = GameRound.objects.get(public_id=state['round_id'])
        game_round.crash_point = Decimal('40.00')
        game_round.betting_ends_at = timezone.now() - timedelta(milliseconds=1)
        game_round.save(update_fields=['crash_point', 'betting_ends_at'])
        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })

        self.profile.refresh_from_db()
        game_round.refresh_from_db()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'betting_closed')
        self.assertEqual(game_round.status, 'flying')
        self.assertEqual(self.profile.balance, Decimal('1000.00'))
        self.assertFalse(GameSession.objects.filter(user=self.user).exists())

    def test_server_starts_and_tracks_no_bet_rounds_without_exposing_the_outcome(self):
        balance_before = self.profile.balance

        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([]),
        })

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        game_round = GameRound.objects.get(public_id=payload['round_id'])
        self.profile.refresh_from_db()
        self.assertEqual(payload['session_ids'], [])
        self.assertNotIn('crash_point', payload)
        self.assertEqual(self.profile.balance, balance_before)
        self.assertEqual(game_round.created_by, self.user)
        self.assertFalse(GameSession.objects.filter(round=game_round).exists())

    def test_start_joins_the_shared_round_while_it_is_flying(self):
        first = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([]),
        })
        first_round = GameRound.objects.get(public_id=first.json()['round_id'])
        first_round.crash_point = Decimal('40.00')
        first_round.status = 'flying'
        first_round.started_at = timezone.now()
        first_round.save(update_fields=['crash_point', 'status', 'started_at'])

        second = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([]),
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['round_id'], first.json()['round_id'])
        self.assertEqual(GameRound.objects.filter(created_by=self.user).count(), 1)

    def test_different_players_share_one_round_and_refresh_restores_it(self):
        first = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })
        first_round = GameRound.objects.get(public_id=first.json()['round_id'])
        first_round.crash_point = Decimal('40.00')
        first_round.save(update_fields=['crash_point'])

        other_user = User.objects.create_user(
            username='other-player',
            email='other@example.com',
            password='strong-test-pass',
        )
        Profile.objects.create(user=other_user, balance=Decimal('1000.00'))
        self.client.force_login(other_user)
        second = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(20),
        })

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['round_id'], first.json()['round_id'])
        self.assertEqual(GameRound.objects.count(), 1)
        self.assertEqual(GameSession.objects.filter(round=first_round).count(), 2)
        self.assertEqual(SharedGameState.objects.get(pk=1).active_round, first_round)

        refreshed_state = self.client.post('/play/', {'action': 'state'}).json()
        self.assertEqual(refreshed_state['round_id'], first.json()['round_id'])
        self.assertEqual(refreshed_state['status'], 'betting')
        self.assertNotIn('crash_point', refreshed_state)
        self.assertEqual(refreshed_state['session_ids'], second.json()['session_ids'])
        self.assertEqual(refreshed_state['sessions'][0]['stake'], 20.0)
        self.client.logout()
        guest_state = self.client.post('/play/', {'action': 'state'}).json()
        self.assertEqual(guest_state['round_id'], first.json()['round_id'])
        self.assertEqual(guest_state['session_ids'], [])

    def test_shared_round_state_reveals_crash_after_the_flight_ends(self):
        started = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([]),
        })
        game_round = GameRound.objects.get(public_id=started.json()['round_id'])
        game_round.crash_point = Decimal('1.01')
        game_round.status = 'flying'
        game_round.started_at = timezone.now() - timedelta(seconds=1)
        game_round.save(update_fields=['crash_point', 'status', 'started_at'])

        state = self.client.post('/play/', {'action': 'state'}).json()

        game_round.refresh_from_db()
        self.assertEqual(state['round_id'], game_round.public_id)
        self.assertEqual(state['status'], 'crashed')
        self.assertEqual(state['crash_point'], 1.01)
        self.assertTrue(game_round.is_revealed)

    def test_live_ledger_uses_current_round_bets_and_pages_with_a_fixed_limit(self):
        started = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(1),
        })
        game_round = GameRound.objects.get(public_id=started.json()['round_id'])
        players = User.objects.bulk_create([
            User(username=f'ledger-player-{index}')
            for index in range(50)
        ])
        sessions = [
            GameSession(
                user=player,
                round=game_round,
                stake=Decimal(index + 2),
                crash_point=game_round.crash_point,
            )
            for index, player in enumerate(players)
        ]
        sessions[20].cashed_out_at = Decimal('2.00')
        sessions[20].won_amount = Decimal('75.00')
        sessions[49].cashed_out_at = Decimal('2.00')
        sessions[49].won_amount = Decimal('110.00')
        GameSession.objects.bulk_create(sessions)
        game_round.bet_count = 51
        game_round.winning_bet_count = 2
        game_round.save(update_fields=['bet_count', 'winning_bet_count'])

        all_first_page = self.client.post('/play/', {
            'action': 'ledger',
            'tab': 'all',
        }).json()
        all_second_page = self.client.post('/play/', {
            'action': 'ledger',
            'tab': 'all',
            'cursor': all_first_page['next_cursor'],
        }).json()
        top = self.client.post('/play/', {
            'action': 'ledger',
            'tab': 'top',
        }).json()
        mine = self.client.post('/play/', {
            'action': 'ledger',
            'tab': 'mine',
        }).json()

        self.assertEqual(all_first_page['total_count'], 51)
        self.assertEqual(all_first_page['tab_count'], 51)
        self.assertEqual(len(all_first_page['rows']), 50)
        self.assertEqual(all_first_page['rows'][0]['bet'], 51.0)
        self.assertEqual(all_first_page['rows'][0]['user'], 'ledger-player-49')
        self.assertTrue(all_first_page['has_next'])
        self.assertIsNone(all_first_page['previous_cursor'])
        self.assertEqual(len(all_second_page['rows']), 1)
        self.assertEqual(all_second_page['rows'][0]['bet'], 1.0)
        self.assertTrue(all_second_page['has_previous'])
        self.assertFalse(all_second_page['has_next'])
        all_previous_page = self.client.post('/play/', {
            'action': 'ledger',
            'tab': 'all',
            'cursor': all_second_page['previous_cursor'],
        }).json()
        self.assertEqual(all_previous_page['rows'], all_first_page['rows'])
        self.assertEqual([row['win'] for row in top['rows']], [110.0, 75.0])
        self.assertEqual(top['tab_count'], 2)
        self.assertEqual([row['bet'] for row in mine['rows']], [1.0])

    def test_my_bets_lists_all_rounds_for_24_hours_and_keeps_old_records(self):
        game_round = create_game_round(self.user)
        previous_round = create_game_round(self.user)
        now = timezone.now()
        recent_sessions = [
            GameSession(
                user=self.user,
                round=game_round,
                stake=Decimal(index + 1),
                crash_point=game_round.crash_point,
                created_at=now - timedelta(minutes=index),
            )
            for index in range(51)
        ]
        recent_sessions[0].round = previous_round
        GameSession.objects.bulk_create(recent_sessions)
        expired_session = GameSession.objects.create(
            user=self.user,
            round=game_round,
            stake=Decimal('99.00'),
            crash_point=game_round.crash_point,
            created_at=now - timedelta(days=1, seconds=1),
        )

        first_page = self.client.post('/play/', {
            'action': 'ledger',
            'tab': 'mine',
        }).json()
        second_page = self.client.post('/play/', {
            'action': 'ledger',
            'tab': 'mine',
            'cursor': first_page['next_cursor'],
        }).json()
        previous_page = self.client.post('/play/', {
            'action': 'ledger',
            'tab': 'mine',
            'cursor': second_page['previous_cursor'],
        }).json()

        self.assertEqual(first_page['total_count'], 51)
        self.assertEqual(first_page['tab_count'], 51)
        self.assertEqual(len(first_page['rows']), 50)
        self.assertEqual(len(second_page['rows']), 1)
        self.assertEqual(second_page['total_count'], 51)
        self.assertEqual(previous_page['rows'], first_page['rows'])
        self.assertTrue(GameSession.objects.filter(pk=expired_session.pk).exists())

    def test_owner_can_reveal_a_completed_no_bet_round(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([]),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        game_round.crash_point = Decimal('1.01')
        game_round.started_at = timezone.now() - timedelta(seconds=1)
        game_round.save(update_fields=['crash_point', 'started_at'])

        response = self.client.post('/play/', {
            'action': 'settle_crash',
            'round_id': game_round.public_id,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['crash_point'], 1.01)
        game_round.refresh_from_db()
        self.assertTrue(game_round.is_revealed)

    def test_user_cannot_reveal_another_users_no_bet_round(self):
        other_user = User.objects.create_user(
            username='other-player',
            email='other@example.com',
            password='strong-test-pass',
        )
        Profile.objects.create(user=other_user, balance=Decimal('100.00'))
        other_round = create_game_round(other_user)

        request = RequestFactory().post('/play/', {
            'action': 'settle_crash',
            'round_id': other_round.public_id,
        })
        request.user = self.user

        with self.assertRaises(Http404):
            play(request)
        other_round.refresh_from_db()
        self.assertFalse(other_round.server_seed)

    @patch('core.views.multiplier_at_elapsed_time', return_value=Decimal('5.00'))
    def test_server_auto_cashout_credits_the_configured_target(self, _multiplier):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([{
                'amount': 10,
                'auto_cashout_at': 2,
                'request_id': 'b' * 32,
            }]),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        session = GameSession.objects.get(id=start['session_ids'][0])
        game_round.crash_point = Decimal('10.00')
        game_round.status = 'flying'
        game_round.started_at = timezone.now() - timedelta(seconds=10)
        game_round.save(update_fields=['crash_point', 'status', 'started_at'])

        state = self.client.post('/play/', {'action': 'state'}).json()
        repeated_state = self.client.post('/play/', {'action': 'state'}).json()

        session.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(state['status'], 'flying')
        self.assertEqual(repeated_state['status'], 'flying')
        self.assertEqual(session.cashed_out_at, Decimal('2.00'))
        self.assertEqual(session.won_amount, Decimal('20.00'))
        self.assertEqual(self.profile.balance, Decimal('1010.00'))
        self.assertEqual(
            Transaction.objects.filter(
                user=self.user,
                reference=game_round.public_id,
                transaction_type='win',
            ).count(),
            1,
        )

    def test_successful_cashout_before_crash_does_not_reveal_crash_point(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        session = GameSession.objects.get(id=start['session_ids'][0])
        game_round.crash_point = Decimal('10.00')
        game_round.status = 'flying'
        game_round.started_at = timezone.now()
        game_round.save(update_fields=['crash_point', 'status', 'started_at'])
        session.crash_point = game_round.crash_point
        session.save(update_fields=['crash_point'])

        response = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': ','.join(str(session_id) for session_id in start['session_ids']),
            'cashout_multiplier': '1.05',
        })

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'won')
        self.assertIsNone(payload['crash_point'])
        game_round.refresh_from_db()
        self.assertEqual(game_round.winning_bet_count, 1)

    @patch('core.views.multiplier_at_elapsed_time', return_value=Decimal('1.25'))
    def test_manual_cashout_before_auto_target_uses_current_server_multiplier(self, _multiplier):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([{
                'amount': 10,
                'auto_cashout_at': 2,
                'request_id': 'e' * 32,
            }]),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        session = GameSession.objects.get(id=start['session_ids'][0])
        game_round.crash_point = Decimal('10.00')
        game_round.status = 'flying'
        game_round.started_at = timezone.now()
        game_round.save(update_fields=['crash_point', 'status', 'started_at'])
        session.crash_point = game_round.crash_point
        session.save(update_fields=['crash_point'])

        response = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': str(start['session_ids'][0]),
        })

        session.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['cashout_multiplier'], 1.25)
        self.assertEqual(session.cashed_out_at, Decimal('1.25'))
        self.assertEqual(session.won_amount, Decimal('12.50'))

    @patch('core.views.multiplier_at_elapsed_time', return_value=Decimal('1.25'))
    def test_cashout_ignores_client_multiplier_and_uses_server_multiplier(self, _multiplier):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        game_round.crash_point = Decimal('10.00')
        game_round.status = 'flying'
        game_round.started_at = timezone.now()
        game_round.save(update_fields=['crash_point', 'status', 'started_at'])
        session = GameSession.objects.get(id=start['session_ids'][0])
        session.crash_point = Decimal('10.00')
        session.save(update_fields=['crash_point'])

        response = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': ','.join(str(session_id) for session_id in start['session_ids']),
            'cashout_multiplier': '100.00',
        })

        session.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['cashout_multiplier'], 1.25)
        self.assertEqual(session.cashed_out_at, Decimal('1.25'))
        self.assertEqual(session.won_amount, Decimal('12.50'))
        self.assertEqual(self.profile.balance, Decimal('1002.50'))

        retry = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': str(session.id),
            'cashout_multiplier': '100.00',
        })
        self.profile.refresh_from_db()
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()['won_amount'], 12.5)
        self.assertEqual(self.profile.balance, Decimal('1002.50'))
        self.assertEqual(
            Transaction.objects.filter(
                user=self.user,
                reference=game_round.public_id,
                transaction_type='win',
            ).count(),
            1,
        )

    def test_cashout_rejects_sessions_from_different_rounds(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        }).json()
        other_round = create_game_round(self.user)
        other_session = GameSession.objects.create(
            user=self.user,
            round=other_round,
            stake=Decimal('10.00'),
            crash_point=other_round.crash_point,
        )

        response = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': f"{start['session_ids'][0]},{other_session.id}",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(GameSession.objects.get(id=start['session_ids'][0]).cashed_out_at)
        self.assertIsNone(GameSession.objects.get(id=other_session.id).cashed_out_at)

    def test_game_presence_endpoint_records_authenticated_page_presence(self):
        response = self.client.post('/play/', {
            'action': 'presence',
            'presence_key': 'a' * 32,
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(GamePresence.objects.filter(user=self.user, presence_key='a' * 32).exists())

    def test_game_presence_endpoint_rejects_invalid_presence_key(self):
        response = self.client.post('/play/', {
            'action': 'presence',
            'presence_key': 'not-a-valid-presence-key',
        })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(GamePresence.objects.exists())

    @patch('core.reward_services.random.SystemRandom')
    def test_chat_rain_debits_sender_and_credits_active_chat_player(self, system_random):
        recipient = User.objects.create_user(
            username='rain-recipient',
            email='rain-recipient@example.com',
            password='strong-test-pass',
        )
        recipient_profile = Profile.objects.create(user=recipient, balance=Decimal('20.00'))
        ChatMessage.objects.create(user=recipient, message='ready for rain')
        GamePresence.objects.create(user=self.user, presence_key='a' * 32, last_seen=timezone.now())
        GamePresence.objects.create(user=recipient, presence_key='b' * 32, last_seen=timezone.now())
        inactive_user = User.objects.create_user(
            username='inactive-chat-player',
            email='inactive-chat-player@example.com',
            password='strong-test-pass',
        )
        inactive_profile = Profile.objects.create(user=inactive_user, balance=Decimal('50.00'))
        ChatMessage.objects.create(user=inactive_user, message='not currently in game')
        system_random.return_value.sample.return_value = [recipient]

        response = self.client.post('/play/', {
            'action': 'chat_rain',
            'total_amount': '100.00',
            'players': '1',
        })

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        recipient_profile.refresh_from_db()
        inactive_profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('900.00'))
        self.assertEqual(recipient_profile.balance, Decimal('120.00'))
        self.assertEqual(inactive_profile.balance, Decimal('50.00'))
        system_random.return_value.sample.assert_called_once_with([recipient], 1)
        self.assertIn('Sent rain', response.json()['message']['text'])
        rain = ChatRain.objects.get(sender=self.user)
        self.assertEqual(rain.total_amount, Decimal('100.00'))
        self.assertEqual(rain.recipient_count, 1)
        self.assertIsNotNone(rain.reward_event)
        self.assertEqual(rain.reward_event.status, 'completed')
        self.assertEqual(rain.reward_event.total_reward_pool, Decimal('100.00'))
        self.assertEqual(ChatRainRecipient.objects.get(rain=rain, user=recipient).amount, Decimal('100.00'))
        reward_claim = RewardClaim.objects.get(reward_event=rain.reward_event, user=recipient)
        reward_wallet = RewardWallet.objects.get(user=recipient)
        self.assertEqual(reward_claim.amount_claimed, Decimal('100.00'))
        self.assertEqual(reward_wallet.available_balance, Decimal('100.00'))
        self.assertEqual(RewardTransaction.objects.get(event=rain.reward_event, wallet=reward_wallet).amount, Decimal('100.00'))

    @patch('core.reward_services.random.SystemRandom')
    def test_chat_rain_randomly_selects_the_requested_number_of_active_players(self, system_random):
        recipients = []
        for number in range(3):
            user = User.objects.create_user(
                username=f'rain-player-{number}',
                email=f'rain-player-{number}@example.com',
                password='strong-test-pass',
            )
            Profile.objects.create(user=user, balance=Decimal('20.00'))
            GamePresence.objects.create(
                user=user,
                presence_key=f'{number + 1:032x}',
                last_seen=timezone.now(),
            )
            recipients.append(user)
        system_random.return_value.sample.return_value = recipients[:2]

        response = self.client.post('/play/', {
            'action': 'chat_rain',
            'total_amount': '100.00',
            'players': '2',
        })

        self.assertEqual(response.status_code, 200)
        system_random.return_value.sample.assert_called_once()
        self.assertEqual(system_random.return_value.sample.call_args.args[1], 2)
        rain = ChatRain.objects.get(sender=self.user)
        self.assertEqual(
            set(rain.recipients.values_list('user_id', flat=True)),
            {user.id for user in recipients[:2]},
        )
        self.assertNotIn(self.user.id, rain.recipients.values_list('user_id', flat=True))
        self.assertEqual(response.json()['message']['rain']['recipient_count'], 2)

    def test_chat_rain_rejects_when_requested_active_players_are_unavailable(self):
        inactive_user = User.objects.create_user(
            username='inactive-rain-player',
            email='inactive-rain-player@example.com',
            password='strong-test-pass',
        )
        Profile.objects.create(user=inactive_user, balance=Decimal('20.00'))
        ChatMessage.objects.create(user=inactive_user, message='not online')
        GamePresence.objects.create(user=self.user, presence_key='a' * 32, last_seen=timezone.now())
        GamePresence.objects.create(
            user=inactive_user,
            presence_key='b' * 32,
            last_seen=timezone.now() - timedelta(seconds=91),
        )

        response = self.client.post('/play/', {
            'action': 'chat_rain',
            'total_amount': '100.00',
            'players': '1',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['message'],
            'Only 0 other players are currently in the game; 1 is needed.',
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('1000.00'))
        self.assertFalse(ChatRain.objects.filter(sender=self.user).exists())

    def test_chat_rain_rejects_insufficient_wallet_balance(self):
        recipient = User.objects.create_user(
            username='rain-balance-recipient',
            email='rain-balance-recipient@example.com',
            password='strong-test-pass',
        )
        Profile.objects.create(user=recipient, balance=Decimal('20.00'))
        ChatMessage.objects.create(user=recipient, message='ready for rain')
        GamePresence.objects.create(user=recipient, presence_key='a' * 32, last_seen=timezone.now())

        response = self.client.post('/play/', {
            'action': 'chat_rain',
            'total_amount': '5000.00',
            'players': '1',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['message'], 'You do not have sufficient balance on your wallet.')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('1000.00'))
        self.assertFalse(ChatRain.objects.filter(sender=self.user).exists())
        self.assertFalse(RewardEvent.objects.filter(title__startswith='Chat Rain').exists())

    def test_chat_reaction_is_persisted_and_toggles(self):
        message = ChatMessage.objects.create(user=self.user, message='react to this')

        first = self.client.post('/play/', {
            'action': 'chat_react',
            'message_id': message.id,
        })
        second = self.client.post('/play/', {
            'action': 'chat_react',
            'message_id': message.id,
        })

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['active'])
        self.assertEqual(first.json()['reaction_count'], 1)
        self.assertEqual(ChatReaction.objects.filter(message=message, user=self.user).count(), 0)
        self.assertFalse(second.json()['active'])
        self.assertEqual(second.json()['reaction_count'], 0)

    def test_chat_prunes_oldest_messages_when_total_reaches_limit(self):
        oldest = ChatMessage.objects.create(user=self.user, message='oldest message')
        for index in range(98):
            ChatMessage.objects.create(user=self.user, message=f'old message {index}')

        response = self.client.post('/play/', {
            'action': 'chat',
            'message': 'fresh message',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ChatMessage.objects.count(), 30)
        self.assertFalse(ChatMessage.objects.filter(id=oldest.id).exists())
        self.assertTrue(ChatMessage.objects.filter(message='fresh message').exists())

    def test_chat_rejects_phone_numbers_links_and_questions(self):
        prohibited_messages = [
            ('Call me at +1 (234) 567-8901', 'Phone numbers'),
            ('Call me at zero seven seven one two three four five six seven', 'Phone numbers'),
            ('Call me at o seven seven two three four five six seven', 'Phone numbers'),
            ('Call me at double seven two three four five six seven eight', 'Phone numbers'),
            ('Visit https://example.com for details', 'Links'),
            ('Visit example dot com for details', 'Links'),
            ('What time does the game start?', 'Questions'),
            ('Please send me the details', 'Questions'),
            ('Send me money for the next round', 'Questions'),
            ('fuck you', 'Abusive language'),
            ('fuckyou', 'Abusive language'),
            ('f.u.c.k you', 'Abusive language'),
            ('silly', 'Abusive language'),
            ('You are a b!tch', 'Abusive language'),
            ('You are a sh!t player', 'Abusive language'),
            ('THIS MESSAGE IS ALL IN CAPITALS', 'Excessive capital letters'),
            ('hello hello hello hello', 'spam'),
            ('Can you lend me money?', 'Questions'),
            ('This is a fraud scheme', 'Scams'),
            ('I am selling game accounts', 'Advertising'),
        ]

        for message, expected_error in prohibited_messages:
            with self.subTest(message=message):
                response = self.client.post('/play/', {
                    'action': 'chat',
                    'message': message,
                })

                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()['success'])
                self.assertIn(expected_error, response.json()['message'])
                self.assertFalse(ChatMessage.objects.filter(message=message).exists())

    def test_chat_feed_hides_legacy_messages_violating_rules(self):
        ChatMessage.objects.create(user=self.user, message='A friendly greeting')
        ChatMessage.objects.create(user=self.user, message='Call me at 1234567890')
        ChatMessage.objects.create(user=self.user, message='Visit https://example.com')

        request = RequestFactory().get('/play/')
        request.user = self.user

        with patch('core.views.render', return_value=HttpResponse()) as render_template:
            response = play(request)

        self.assertEqual(response.status_code, 200)
        chat_messages = render_template.call_args.args[2]['chat_messages']
        self.assertEqual([message.message for message in chat_messages], ['A friendly greeting'])

    def test_round_seed_cannot_be_revealed_before_server_crash_time(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        game_round.crash_point = Decimal('10.00')
        game_round.save(update_fields=['crash_point'])

        early = self.client.post('/play/', {
            'action': 'settle_crash',
            'round_id': start['round_id'],
        })

        self.assertEqual(early.status_code, 409)

    def test_round_reveals_after_server_crash_time_and_verifies(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        game_round.started_at = timezone.now() - timedelta(seconds=(float(game_round.crash_point) - 1) / 0.10 + 1)
        game_round.save(update_fields=['started_at'])

        response = self.client.post('/play/', {
            'action': 'settle_crash',
            'round_id': start['round_id'],
        })

        self.assertEqual(response.status_code, 200)
        game_round.refresh_from_db()
        self.assertTrue(game_round.server_seed)
        self.assertTrue(
            verify_game_round(
                game_round.server_seed,
                game_round.client_seed,
                game_round.server_seed_hash,
                game_round.crash_point,
            )
        )

    def test_daily_loss_limit_blocks_new_bets(self):
        self.profile.daily_loss_limit = Decimal('5.00')
        self.profile.save(update_fields=['daily_loss_limit'])

        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })

        self.assertEqual(response.status_code, 403)
        self.assertIn('Daily loss limit', response.json()['message'])


class RoomEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='room-host',
            email='room-host@example.com',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(user=self.user, balance=Decimal('1000.00'))
        self.room = Room.objects.create(name='Test Room', host=self.user)
        RoomPlayer.objects.create(room=self.room, user=self.user, stake=Decimal('10.00'))
        self.client.login(username='room-host', password='strong-test-pass')

    def test_room_start_and_running_status_do_not_reveal_crash_point(self):
        start = self.client.post(f'/rooms/{self.room.id}/action/', {
            'action': 'start',
        })

        self.assertEqual(start.status_code, 200)
        self.assertNotIn('crash_point', start.json())

        status = self.client.get(f'/rooms/{self.room.id}/status/')

        self.assertEqual(status.status_code, 200)
        payload = status.json()
        self.assertEqual(payload['room']['status'], 'running')
        self.assertIsNone(payload['room']['crash_point'])
        self.assertGreaterEqual(payload['room']['current_multiplier'], 1.0)

    @patch('core.views.multiplier_at_elapsed_time', return_value=Decimal('1.50'))
    def test_room_cashout_ignores_client_multiplier_and_uses_server_multiplier(self, _multiplier):
        self.room.crash_point = Decimal('10.00')
        self.room.started_at = timezone.now()
        self.room.status = 'running'
        self.room.save(update_fields=['crash_point', 'started_at', 'status'])

        response = self.client.post(f'/rooms/{self.room.id}/action/', {
            'action': 'cashout',
            'cashout_multiplier': '100.00',
        })

        player = RoomPlayer.objects.get(room=self.room, user=self.user)
        self.profile.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['cashout_multiplier'], 1.5)
        self.assertEqual(player.cashed_out_at, Decimal('1.50'))
        self.assertEqual(player.won_amount, Decimal('15.00'))
        self.assertEqual(self.profile.balance, Decimal('1015.00'))

        replay = self.client.post(f'/rooms/{self.room.id}/action/', {
            'action': 'cashout',
            'cashout_multiplier': '100.00',
        })
        self.profile.refresh_from_db()
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(self.profile.balance, Decimal('1015.00'))
        self.assertTrue(AuditLog.objects.filter(
            action_type='risk_alert',
            data__event='room_cashout_replay',
            user=self.user,
        ).exists())

    def test_self_exclusion_blocks_joining_a_room(self):
        room = Room.objects.create(name='Join Room', host=self.user)
        SelfExclusion.objects.create(
            user=self.user,
            ends_at=timezone.now() + timedelta(hours=72),
            reason='Cooling off',
        )

        response = self.client.post(f'/rooms/{room.id}/action/', {
            'action': 'join',
            'stake': '10.00',
        })

        self.assertEqual(response.status_code, 403)
        self.assertIn('self-excluded', response.json()['message'])
        self.assertFalse(RoomPlayer.objects.filter(room=room, user=self.user).exists())

    def test_self_exclusion_blocks_host_from_starting_a_room(self):
        SelfExclusion.objects.create(
            user=self.user,
            ends_at=timezone.now() + timedelta(hours=72),
            reason='Cooling off',
        )

        response = self.client.post(f'/rooms/{self.room.id}/action/', {
            'action': 'start',
        })

        self.assertEqual(response.status_code, 403)
        self.assertIn('self-excluded', response.json()['message'])
        self.room.refresh_from_db()
        self.assertEqual(self.room.status, 'waiting')


class AbuseAndWebSocketTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='abuse-player',
            email='abuse@example.com',
            password='strong-test-pass',
        )
        self.room = Room.objects.create(name='Socket Room', host=self.user)
        RoomPlayer.objects.create(room=self.room, user=self.user, stake=Decimal('10.00'))

    def test_account_creation_and_payment_volume_are_investigation_signals(self):
        request = RequestFactory().post('/register/', REMOTE_ADDR='203.0.113.31')
        request.user = self.user
        for _ in range(4):
            detect_account_creation_pattern(request, self.user)
        detect_payment_pattern(request, self.user)
        for _ in range(8):
            detect_payment_pattern(request, self.user)

        self.assertTrue(AuditLog.objects.filter(
            data__event='unusual_account_creation_volume',
            user=self.user,
        ).exists())
        self.assertTrue(AuditLog.objects.filter(
            data__event='unusual_payment_request_volume',
            user=self.user,
        ).exists())
        self.assertEqual(AuditLog.objects.filter(
            data__event='unusual_account_creation_volume',
            user=self.user,
        ).count(), 1)

    def test_shared_reward_ip_and_user_agent_are_recorded_as_weak_signals(self):
        now = timezone.now()
        event = RewardEvent.objects.create(
            title='Shared identity test',
            description='',
            total_reward_pool=Decimal('100.00'),
            remaining_reward_pool=Decimal('100.00'),
            maximum_claims=10,
            reward_per_claim=Decimal('10.00'),
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(hours=1),
            created_by=self.user,
        )
        for index in range(3):
            claimant = User.objects.create_user(
                username=f'reward-claimant-{index}',
                email=f'reward-claimant-{index}@example.com',
                password='strong-test-pass',
            )
            RewardClaim.objects.create(
                reward_event=event,
                user=claimant,
                amount_claimed=Decimal('10.00'),
                ip_address='203.0.113.33',
                device_identifier='SharedBrowser/1.0',
                transaction_reference=f'reward-claim-{index}',
            )

        request = RequestFactory().post(
            '/api/rewards/claim/',
            REMOTE_ADDR='203.0.113.33',
            HTTP_USER_AGENT='SharedBrowser/1.0',
        )
        request.user = self.user
        detect_reward_claim_pattern(request, self.user)

        signal = AuditLog.objects.get(
            data__event='shared_reward_claim_identity',
            user=self.user,
        )
        self.assertEqual(signal.data['distinct_ip_accounts'], 3)
        self.assertEqual(signal.data['distinct_device_accounts'], 3)

    def test_room_socket_reconnect_limit_records_and_rejects_excess_connections(self):
        for _ in range(20):
            self.assertTrue(authorize_room_socket(
                self.room.pk,
                self.user,
                '203.0.113.32',
            ))

        self.assertFalse(authorize_room_socket(
            self.room.pk,
            self.user,
            '203.0.113.32',
        ))
        self.assertTrue(AuditLog.objects.filter(
            data__event='room_socket_reconnect_limit',
            user=self.user,
        ).exists())

    def test_room_socket_accepts_heartbeat_and_rejects_client_game_outcomes(self):
        async def exercise_socket():
            communicator = WebsocketCommunicator(
                room_socket_application(self.user),
                f'/ws/room/{self.room.pk}/',
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            await communicator.send_json_to({'type': 'heartbeat'})
            self.assertEqual(
                await communicator.receive_json_from(),
                {'type': 'heartbeat'},
            )

            await communicator.send_json_to({
                'type': 'room_update',
                'crash_point': 99999,
                'status': 'ended',
            })
            close_event = await communicator.receive_output()
            self.assertEqual(close_event['type'], 'websocket.close')
            self.assertEqual(close_event['code'], 1008)
            await communicator.wait()

        async_to_sync(exercise_socket)()
        event = AuditLog.objects.get(
            data__event='invalid_websocket_message',
            user=self.user,
        )
        self.assertEqual(event.data['socket'], 'room')
        self.assertNotIn('99999', str(event.data))

    def test_room_socket_rejects_oversized_messages(self):
        async def exercise_socket():
            communicator = WebsocketCommunicator(
                room_socket_application(self.user),
                f'/ws/room/{self.room.pk}/',
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            await communicator.send_to(text_data=' ' * 4097)
            close_event = await communicator.receive_output()
            self.assertEqual(close_event['type'], 'websocket.close')
            self.assertEqual(close_event['code'], 1008)
            await communicator.wait()

        async_to_sync(exercise_socket)()
        self.assertTrue(AuditLog.objects.filter(
            data__event='invalid_websocket_message',
            user=self.user,
            data__socket='room',
        ).exists())

    def test_anonymous_room_socket_is_rejected(self):
        async def exercise_socket():
            communicator = WebsocketCommunicator(
                room_socket_application(AnonymousUser()),
                f'/ws/room/{self.room.pk}/',
            )
            connected, close_code = await communicator.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4403)
            await communicator.wait()

        async_to_sync(exercise_socket)()

    def test_non_member_room_socket_is_rejected_and_recorded(self):
        other_user = User.objects.create_user(
            username='socket-outsider',
            email='socket-outsider@example.com',
            password='strong-test-pass',
        )

        async def exercise_socket():
            communicator = WebsocketCommunicator(
                room_socket_application(other_user),
                f'/ws/room/{self.room.pk}/',
            )
            connected, close_code = await communicator.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4403)
            await communicator.wait()

        async_to_sync(exercise_socket)()
        self.assertTrue(AuditLog.objects.filter(
            data__event='unauthorized_room_socket',
            user=other_user,
        ).exists())

    def test_json_payload_parser_rejects_non_object_payloads(self):
        request = RequestFactory().post(
            '/api/rewards/claim/',
            data='[]',
            content_type='application/json',
        )

        self.assertEqual(request_payload(request), {})


class WalletComplianceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='wallet-player',
            email='wallet@example.com',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(user=self.user, balance=Decimal('1000.00'))
        self.client.login(username='wallet-player', password='strong-test-pass')

    def test_wallet_amount_ranges_are_different_for_deposits_and_withdrawals(self):
        deposit = WalletForm(data={
            'deposit-amount': '300000.00',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '256',
            'deposit-phone_number': '700000000',
        }, prefix='deposit')
        excessive_deposit = WalletForm(data={
            'deposit-amount': '300000.01',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '256',
            'deposit-phone_number': '700000000',
        }, prefix='deposit')
        large_withdrawal = WalletForm(data={
            'withdraw-amount': '300001.00',
            'withdraw-provider': 'mtn_momo',
            'withdraw-country_code': '256',
            'withdraw-phone_number': '700000000',
        }, prefix='withdraw')
        below_minimum_withdrawal = WalletForm(data={
            'withdraw-amount': '499.99',
            'withdraw-provider': 'mtn_momo',
            'withdraw-country_code': '256',
            'withdraw-phone_number': '700000000',
        }, prefix='withdraw')

        self.assertTrue(deposit.is_valid(), deposit.errors)
        self.assertFalse(excessive_deposit.is_valid())
        self.assertTrue(large_withdrawal.is_valid(), large_withdrawal.errors)
        self.assertFalse(below_minimum_withdrawal.is_valid())

    def test_account_daily_deposit_limit_defaults_to_global_maximum(self):
        self.assertEqual(self.profile.daily_deposit_limit, Decimal('300000.00'))

    def test_payment_services_enforce_deposit_bounds_and_leave_withdrawals_balance_limited(self):
        payment, result, error = initiate_deposit(
            user=self.user,
            amount=Decimal('300000.01'),
            provider='mtn_momo',
            phone_number='256700000000',
        )
        self.assertIsNone(payment)
        self.assertIsNone(result)
        self.assertIn('300,000 UGX', error)
        self.assertFalse(PaymentRequest.objects.filter(user=self.user).exists())

        self.profile.balance = Decimal('400000.00')
        self.profile.save(update_fields=['balance'])
        withdrawal, _, withdrawal_error = initiate_withdrawal(
            user=self.user,
            amount=Decimal('300001.00'),
            provider='mtn_momo',
            phone_number='256700000000',
        )
        self.assertEqual(withdrawal_error, '')
        self.assertEqual(withdrawal.amount, Decimal('300001.00'))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('99999.00'))

    def test_self_exclusion_blocks_deposit_and_is_active_only_for_its_period(self):
        exclusion = SelfExclusion.objects.create(
            user=self.user,
            ends_at=timezone.now() + timedelta(hours=72),
            reason='Cooling off',
        )

        self.assertTrue(is_self_excluded(self.user))
        payment, _, error = initiate_deposit(
            user=self.user,
            amount=Decimal('500.00'),
            provider='mtn_momo',
            phone_number='256700000000',
        )
        self.assertIsNone(payment)
        self.assertIn('self-excluded', error)
        self.assertFalse(PaymentRequest.objects.filter(user=self.user, payment_type='deposit').exists())

        exclusion.ends_at = timezone.now() - timedelta(seconds=1)
        exclusion.save(update_fields=['ends_at'])
        self.assertFalse(is_self_excluded(self.user))

    def test_self_exclusion_activation_is_persisted_and_cannot_be_duplicated(self):
        exclusion = activate_self_exclusion(self.user, '72', 'Taking a break')

        self.assertIsNotNone(exclusion)
        self.assertTrue(exclusion.is_active)
        self.assertTrue(is_self_excluded(self.user))
        self.assertEqual(exclusion.reason, 'Taking a break')
        self.assertIsNone(activate_self_exclusion(self.user, '168'))
        self.assertEqual(SelfExclusion.objects.filter(user=self.user).count(), 1)

    def test_deposit_creates_payment_request_and_credits_wallet(self):
        response = self.client.post('/wallet/', {
            'action': 'deposit',
            'deposit-amount': '5000',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '256',
            'deposit-phone_number': '256700000000',
        })

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        payment = PaymentRequest.objects.get(user=self.user, payment_type='deposit')

        self.assertEqual(self.profile.balance, Decimal('6000.00'))
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(Transaction.objects.get(reference=payment.external_reference).transaction_type, 'deposit')
        deposit_audit = AuditLog.objects.get(user=self.user, action_type='deposit')
        self.assertEqual(deposit_audit.data, {'provider': 'mtn_momo'})
        self.assertNotIn(payment.external_reference, str(deposit_audit.data))
        self.assertNotIn('5000', deposit_audit.description)

    def test_deposit_idempotency_key_prevents_duplicate_credit_and_detail_reuse(self):
        first_payment, _, first_error = initiate_deposit(
            user=self.user,
            amount=Decimal('500.00'),
            provider='mtn_momo',
            phone_number='256700000000',
            idempotency_key='0123456789abcdef0123456789abcdef',
        )
        repeated_payment, repeated_result, repeated_error = initiate_deposit(
            user=self.user,
            amount=Decimal('500.00'),
            provider='mtn_momo',
            phone_number='256700000000',
            idempotency_key='0123456789abcdef0123456789abcdef',
        )
        self.assertEqual(first_error, '')
        self.assertEqual(repeated_error, '')
        self.assertIsNone(repeated_result)
        self.assertEqual(first_payment.pk, repeated_payment.pk)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('1500.00'))
        self.assertEqual(
            PaymentRequest.objects.filter(user=self.user, payment_type='deposit').count(),
            1,
        )

        changed_payment, _, changed_error = initiate_deposit(
            user=self.user,
            amount=Decimal('600.00'),
            provider='mtn_momo',
            phone_number='256700000000',
            idempotency_key='0123456789abcdef0123456789abcdef',
        )
        self.assertIsNone(changed_payment)
        self.assertIn('already used with different details', changed_error)
        self.assertEqual(
            PaymentRequest.objects.filter(user=self.user, payment_type='deposit').count(),
            1,
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('1500.00'))

    @override_settings(IOTEC_PAY_MODE='live')
    def test_live_withdrawal_queues_and_submits_only_after_funds_are_confirmed(self):
        payment, result, error = initiate_withdrawal(
            user=self.user,
            amount=Decimal('500.00'),
            provider='mtn_momo',
            phone_number='256700000000',
            idempotency_key='1123456789abcdef0123456789abcdef',
        )
        self.assertEqual(error, '')
        self.assertIsNone(result)
        self.assertEqual(payment.status, 'queued')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('500.00'))
        self.assertTrue(PaymentDispatchLock.objects.filter(pk=1).exists())

        provider_result = PaymentGatewayResult(
            provider_reference='provider-payout-1',
            status='Pending',
        )
        with (
            patch(
                'core.payment_services.IotecPayGateway.provider_wallet_balance',
                return_value={'currency': 'UGX', 'availableBalance': '500.00'},
            ),
            patch(
                'core.payment_services.IotecPayGateway.disburse_mobile_money',
                return_value=provider_result,
            ) as submit,
        ):
            outcome = process_next_queued_withdrawal()

        self.assertEqual(outcome, 'submitted')
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(payment.metadata['dispatch_state'], 'submitted')
        submit.assert_called_once_with(
            external_id=payment.external_reference,
            amount=Decimal('500.00'),
            currency='UGX',
            payee='256700000000',
            payee_name=self.user.username,
            payee_email=self.user.email,
        )

    @override_settings(IOTEC_PAY_MODE='live')
    def test_queued_withdrawal_stays_held_if_available_balance_is_insufficient(self):
        payment, _, error = initiate_withdrawal(
            user=self.user,
            amount=Decimal('500.00'),
            provider='mtn_momo',
            phone_number='256700000000',
            idempotency_key='2123456789abcdef0123456789abcdef',
        )
        self.assertEqual(error, '')

        with (
            patch(
                'core.payment_services.IotecPayGateway.provider_wallet_balance',
                return_value={
                    'currency': 'UGX',
                    'actualBalance': '10000.00',
                    'availableBalance': '499.99',
                },
            ),
            patch('core.payment_services.IotecPayGateway.disburse_mobile_money') as submit,
        ):
            outcome = process_next_queued_withdrawal()

        self.assertEqual(outcome, 'deferred')
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'queued')
        self.assertEqual(payment.metadata['queue_reason'], 'insufficient_provider_balance')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('500.00'))
        submit.assert_not_called()

    def test_queued_withdrawal_cannot_be_manually_synced(self):
        payment = PaymentRequest.objects.create(
            user=self.user,
            payment_type='withdraw',
            provider='mtn_momo',
            amount=Decimal('500.00'),
            phone_number='256700000000',
            external_reference='WDR-QUEUED-SYNC',
            status='queued',
            metadata={'dispatch_state': 'queued'},
        )

        with patch('core.views.sync_payment') as sync_mock:
            response = self.client.post(
                f'/wallet/payments/{payment.external_reference}/sync/',
            )

        self.assertEqual(response.status_code, 302)
        sync_mock.assert_not_called()

    def test_deposit_rejects_selected_country_where_service_is_not_available(self):
        response = self.client.post('/wallet/', {
            'action': 'deposit',
            'deposit-amount': '500',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '254',
            'deposit-phone_number': '712345678',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'We have not started operating in Kenya yet')
        self.assertFalse(PaymentRequest.objects.filter(user=self.user, payment_type='deposit').exists())

    def test_wallet_form_warns_for_country_without_service(self):
        form = WalletForm(data={
            'amount': '500',
            'provider': 'mtn_momo',
            'country_code': '254',
            'phone_number': '712345678',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('We have not started operating in Kenya yet', str(form.errors))

    def test_payment_services_reject_unavailable_countries_before_creating_requests(self):
        deposit, _, deposit_error = initiate_deposit(
            user=self.user,
            amount=Decimal('500.00'),
            provider='mtn_momo',
            phone_number='254712345678',
        )
        withdrawal, _, withdrawal_error = initiate_withdrawal(
            user=self.user,
            amount=Decimal('500.00'),
            provider='airtel_money',
            phone_number='255712345678',
        )

        self.assertIsNone(deposit)
        self.assertIn('We have not started operating in Kenya yet', deposit_error)
        self.assertIsNone(withdrawal)
        self.assertIn('We have not started operating in Tanzania yet', withdrawal_error)
        self.assertFalse(PaymentRequest.objects.filter(user=self.user).exists())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('1000.00'))

    def test_withdrawal_rejects_selected_country_where_service_is_not_available(self):
        response = self.client.post('/wallet/', {
            'action': 'withdraw',
            'withdraw-amount': '500',
            'withdraw-provider': 'mtn_momo',
            'withdraw-country_code': '255',
            'withdraw-phone_number': '712345678',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'We have not started operating in Tanzania yet')
        self.assertFalse(PaymentRequest.objects.filter(user=self.user, payment_type='withdraw').exists())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('1000.00'))

    def test_deposit_rejects_a_phone_country_code_that_conflicts_with_selection(self):
        response = self.client.post('/wallet/', {
            'action': 'deposit',
            'deposit-amount': '500',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '254',
            'deposit-phone_number': '256700000000',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'does not match the selected country')
        self.assertFalse(PaymentRequest.objects.filter(user=self.user, payment_type='deposit').exists())

    @patch(
        'core.payment_services.IotecPayGateway.collect_mobile_money',
        side_effect=PaymentGatewayError('gateway timeout', retryable=True),
    )
    def test_deposit_timeout_stays_pending_without_crediting_balance(self, collect):
        response = self.client.post('/wallet/', {
            'action': 'deposit',
            'deposit-amount': '500',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '256',
            'deposit-phone_number': '256700000000',
        })

        self.assertEqual(response.status_code, 302)
        payment = PaymentRequest.objects.get(user=self.user, payment_type='deposit')
        self.profile.refresh_from_db()
        txn = Transaction.objects.get(reference=payment.external_reference)
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(txn.status, 'pending')
        self.assertEqual(self.profile.balance, Decimal('1000.00'))
        collect.assert_called_once()

    def test_deposit_limit_is_enforced(self):
        self.profile.daily_deposit_limit = Decimal('100.00')
        self.profile.save(update_fields=['daily_deposit_limit'])

        allowed, reason = can_deposit(self.profile, Decimal('5000.00'))

        self.assertFalse(allowed)
        self.assertIn('Daily deposit limit', reason)

    def test_withdrawal_creates_pending_payment_and_holds_balance(self):
        response = self.client.post('/wallet/', {
            'action': 'withdraw',
            'withdraw-amount': '500',
            'withdraw-provider': 'airtel_money',
            'withdraw-country_code': '256',
            'withdraw-phone_number': '256750000000',
        })

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        payment = PaymentRequest.objects.get(user=self.user, payment_type='withdraw')
        txn = Transaction.objects.get(reference=payment.external_reference)

        self.assertEqual(self.profile.balance, Decimal('500.00'))
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(txn.status, 'pending')

    @patch(
        'core.payment_services.IotecPayGateway.disburse_mobile_money',
        side_effect=PaymentGatewayError('gateway timeout', retryable=True),
    )
    def test_withdrawal_timeout_keeps_hold_pending(self, disburse):
        response = self.client.post('/wallet/', {
            'action': 'withdraw',
            'withdraw-amount': '500',
            'withdraw-provider': 'airtel_money',
            'withdraw-country_code': '256',
            'withdraw-phone_number': '256750000000',
        })

        self.assertEqual(response.status_code, 302)
        payment = PaymentRequest.objects.get(user=self.user, payment_type='withdraw')
        self.profile.refresh_from_db()
        txn = Transaction.objects.get(reference=payment.external_reference)
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(txn.status, 'pending')
        self.assertEqual(self.profile.balance, Decimal('500.00'))
        self.assertFalse(Transaction.objects.filter(reference=payment.external_reference, transaction_type='refund').exists())
        disburse.assert_called_once()

    @patch(
        'core.payment_services.IotecPayGateway.disburse_mobile_money',
        side_effect=PaymentGatewayError('provider rejected request'),
    )
    def test_definitive_withdrawal_rejection_releases_hold(self, disburse):
        response = self.client.post('/wallet/', {
            'action': 'withdraw',
            'withdraw-amount': '500',
            'withdraw-provider': 'airtel_money',
            'withdraw-country_code': '256',
            'withdraw-phone_number': '256750000000',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The provider rejected the withdrawal request')
        payment = PaymentRequest.objects.get(user=self.user, payment_type='withdraw')
        self.profile.refresh_from_db()
        self.assertEqual(payment.status, 'failed')
        self.assertEqual(self.profile.balance, Decimal('1000.00'))
        self.assertTrue(Transaction.objects.filter(reference=payment.external_reference, transaction_type='refund').exists())
        disburse.assert_called_once()

    @patch(
        'core.payment_services.IotecPayGateway.collect_mobile_money',
        side_effect=PaymentGatewayError('provider rejected request'),
    )
    def test_definitive_deposit_rejection_is_reported_as_failure(self, collect):
        response = self.client.post('/wallet/', {
            'action': 'deposit',
            'deposit-amount': '500',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '256',
            'deposit-phone_number': '256700000000',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The provider rejected the deposit request')
        payment = PaymentRequest.objects.get(user=self.user, payment_type='deposit')
        self.profile.refresh_from_db()
        self.assertEqual(payment.status, 'failed')
        self.assertEqual(self.profile.balance, Decimal('1000.00'))
        collect.assert_called_once()

    def test_wallet_preferences_link_to_supported_controls(self):
        response = self.client.get('/wallet/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Manage limits')
        self.assertContains(response, 'Change password')
        self.assertNotContains(response, 'Odds Format')
        self.assertNotContains(response, 'Security</span><strong>Enabled')

    def test_payment_sync_calls_provider_service(self):
        payment = PaymentRequest.objects.create(
            user=self.user,
            payment_type='deposit',
            provider='mtn_momo',
            amount=Decimal('500.00'),
            phone_number='256700000000',
            external_reference='DEP-SYNC-001',
            status='pending',
            metadata={'gateway': 'iotec_pay'},
        )

        with patch('core.views.sync_payment', return_value=payment) as sync_mock:
            response = self.client.post(f'/wallet/payments/{payment.external_reference}/sync/')

        self.assertEqual(response.status_code, 302)
        sync_mock.assert_called_once_with(payment)

    def test_payment_sync_provider_error_returns_to_wallet(self):
        payment = PaymentRequest.objects.create(
            user=self.user,
            payment_type='withdraw',
            provider='airtel_money',
            amount=Decimal('500.00'),
            phone_number='256750000000',
            external_reference='WDR-SYNC-ERROR',
            status='pending',
            metadata={'gateway': 'iotec_pay'},
        )

        with patch('core.views.sync_payment', side_effect=PaymentGatewayError('provider unavailable')):
            response = self.client.post(
                f'/wallet/payments/{payment.external_reference}/sync/',
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The provider could not return a payment update')
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'pending')

    @override_settings(IOTEC_PAY_CALLBACK_TOKEN='', DEBUG=True)
    def test_payment_callback_rejects_success_when_token_is_unconfigured(self):
        payment = PaymentRequest.objects.create(
            user=self.user,
            payment_type='deposit',
            provider='mtn_momo',
            amount=Decimal('250.00'),
            phone_number='256700000000',
            external_reference='DEP-CALLBACK-NO-TOKEN',
            status='pending',
            metadata={'gateway': 'iotec_pay'},
        )

        response = self.client.post(
            '/api/payments/callback/',
            data=json.dumps({
                'externalId': payment.external_reference,
                'status': 'Success',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 403)
        payment.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(self.profile.balance, Decimal('1000.00'))
        self.assertFalse(Transaction.objects.filter(
            reference=payment.external_reference,
            transaction_type='deposit',
        ).exists())

    def test_password_change_updates_authenticated_account(self):
        response = self.client.post('/account/password/', {
            'old_password': 'strong-test-pass',
            'new_password1': 'Distinctive-new-pass-2026!',
            'new_password2': 'Distinctive-new-pass-2026!',
        })

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Distinctive-new-pass-2026!'))

    def test_card_deposit_is_rejected(self):
        response = self.client.post('/wallet/', {
            'action': 'deposit',
            'deposit-amount': '500',
            'deposit-provider': 'card',
            'deposit-phone_number': '256700000000',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select a valid choice')
        self.assertFalse(PaymentRequest.objects.filter(user=self.user).exists())

    def test_amount_below_iotec_minimum_is_rejected(self):
        response = self.client.post('/wallet/', {
            'action': 'deposit',
            'deposit-amount': '499',
            'deposit-provider': 'mtn_momo',
            'deposit-phone_number': '256700000000',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ensure this value is greater than or equal to 500')
        self.assertFalse(PaymentRequest.objects.filter(user=self.user).exists())

    @override_settings(IOTEC_PAY_CALLBACK_TOKEN='test-token')
    def test_payment_callback_completes_deposit_once(self):
        payment = PaymentRequest.objects.create(
            user=self.user,
            payment_type='deposit',
            provider='mtn_momo',
            amount=Decimal('250.00'),
            phone_number='256700000000',
            external_reference='DEP-CALLBACK-001',
            status='pending',
            metadata={'gateway': 'iotec_pay'},
        )

        payload = {
            'id': 'provider-transaction-001',
            'externalId': payment.external_reference,
            'status': 'Success',
            'statusCode': 'success',
            'statusMessage': 'Completed',
        }

        first_response = self.client.post(
            '/api/payments/callback/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_PAKABET_WEBHOOK_TOKEN='test-token',
        )
        second_response = self.client.post(
            '/api/payments/callback/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_PAKABET_WEBHOOK_TOKEN='test-token',
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.profile.refresh_from_db()
        payment.refresh_from_db()

        self.assertEqual(self.profile.balance, Decimal('1250.00'))
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(Transaction.objects.filter(reference=payment.external_reference, transaction_type='deposit').count(), 1)

        stale_payload = {
            **payload,
            'status': 'Failed',
            'statusCode': 'failed',
            'statusMessage': 'Stale failure update',
        }
        stale_response = self.client.post(
            '/api/payments/callback/',
            data=json.dumps(stale_payload),
            content_type='application/json',
            HTTP_X_PAKABET_WEBHOOK_TOKEN='test-token',
        )

        self.profile.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(stale_response.json()['status'], 'completed')
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(self.profile.balance, Decimal('1250.00'))


class RewardDistributionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='reward-admin',
            email='reward-admin@example.com',
            password='strong-test-pass',
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username='reward-player',
            email='reward-player@example.com',
            password='strong-test-pass',
        )
        Profile.objects.create(user=self.user, balance=Decimal('100.00'))
        self.starts_at = timezone.now() - timedelta(minutes=1)
        self.ends_at = timezone.now() + timedelta(minutes=30)

    def create_event(self):
        return RewardEvent.objects.create(
            title='Morning Rewards',
            total_reward_pool=Decimal('100.00'),
            remaining_reward_pool=Decimal('100.00'),
            maximum_claims=2,
            reward_per_claim=Decimal('25.00'),
            claim_strategy='equal',
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            status='active',
            created_by=self.admin,
        )

    def test_staff_can_create_reward_event_api(self):
        self.client.login(username='reward-admin', password='strong-test-pass')
        response = self.client.post('/api/rewards/create/', {
            'title': 'API Rewards',
            'total_reward_pool': '80.00',
            'reward_per_claim': '20.00',
            'maximum_claims': '4',
            'starts_at': self.starts_at.isoformat(),
            'ends_at': self.ends_at.isoformat(),
            'status': 'active',
        })

        self.assertEqual(response.status_code, 201)
        event = RewardEvent.objects.get(title='API Rewards')
        self.assertEqual(event.remaining_reward_pool, Decimal('80.00'))
        self.assertEqual(response.json()['event']['remaining_claims'], 4)

    def test_non_staff_cannot_create_reward_event(self):
        self.client.login(username='reward-player', password='strong-test-pass')
        response = self.client.post('/api/rewards/create/', {
            'title': 'Blocked',
            'total_reward_pool': '80.00',
            'reward_per_claim': '20.00',
        })

        self.assertEqual(response.status_code, 403)

    def test_claim_reward_creates_wallet_transaction_and_reduces_pool(self):
        event = self.create_event()

        claim, created = claim_reward(self.user, event.uuid, ip_address='127.0.0.1', device_identifier='test-device')

        self.assertTrue(created)
        event.refresh_from_db()
        wallet = RewardWallet.objects.get(user=self.user)
        self.assertEqual(claim.amount_claimed, Decimal('25.00'))
        self.assertEqual(wallet.available_balance, Decimal('25.00'))
        self.assertEqual(event.remaining_reward_pool, Decimal('75.00'))
        self.assertEqual(RewardTransaction.objects.get(wallet=wallet).balance_after, Decimal('25.00'))

    def test_duplicate_claim_returns_existing_claim_without_double_credit(self):
        event = self.create_event()

        first, first_created = claim_reward(self.user, event.uuid)
        second, second_created = claim_reward(self.user, event.uuid)

        wallet = RewardWallet.objects.get(user=self.user)
        event.refresh_from_db()
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(wallet.available_balance, Decimal('25.00'))
        self.assertEqual(event.remaining_reward_pool, Decimal('75.00'))
        self.assertEqual(RewardClaim.objects.filter(reward_event=event, user=self.user).count(), 1)

    def test_claim_api_is_idempotent_for_same_user(self):
        event = self.create_event()
        self.client.login(username='reward-player', password='strong-test-pass')

        first = self.client.post('/api/rewards/claim/', {
            'event_id': str(event.uuid),
            'device_identifier': 'device-1',
            'idempotency_key': 'claim-once',
        })
        second = self.client.post('/api/rewards/claim/', {
            'event_id': str(event.uuid),
            'device_identifier': 'device-1',
            'idempotency_key': 'claim-once',
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.json()['created'])
        self.assertFalse(second.json()['created'])
        self.assertEqual(RewardWallet.objects.get(user=self.user).available_balance, Decimal('25.00'))


class ComplianceFeatureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='compliance-player',
            email='compliance@example.com',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(user=self.user, balance=Decimal('1000.00'))
        self.client.login(username='compliance-player', password='strong-test-pass')

    def test_self_exclusion_blocks_bets(self):
        response = self.client.post('/profile/', {
            'action': 'self_exclude',
            'duration': '72',
            'reason': 'Cooling off',
            'confirm_exclusion': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SelfExclusion.objects.filter(user=self.user).exists())

        play = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })

        self.assertEqual(play.status_code, 403)
        self.assertIn('self-excluded', play.json()['message'])

    def test_profile_self_exclusion_requires_confirmation(self):
        response = self.client.post('/profile/', {
            'action': 'self_exclude',
            'duration': '72',
            'reason': 'Cooling off',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Confirm that you understand this action')
        self.assertFalse(SelfExclusion.objects.filter(user=self.user).exists())

    def test_settings_self_exclusion_requires_confirmation(self):
        response = self.client.post('/game-limits/', {
            'action': 'self_exclude',
            'duration': '72',
            'reason': 'Cooling off',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Confirm that you understand this action')
        self.assertFalse(SelfExclusion.objects.filter(user=self.user).exists())

    def test_settings_self_exclusion_blocks_bets(self):
        response = self.client.post('/game-limits/', {
            'action': 'self_exclude',
            'duration': '72',
            'reason': 'Cooling off',
            'confirm_exclusion': 'on',
        })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SelfExclusion.objects.filter(user=self.user).exists())

        play = self.client.post('/play/', {
            'action': 'start',
            'bet_data': bet_payload(10),
        })

        self.assertEqual(play.status_code, 403)
        self.assertIn('self-excluded', play.json()['message'])

    def test_settings_can_update_daily_limits(self):
        response = self.client.post('/game-limits/', {
            'action': 'limits',
            'phone_number': '',
            'daily_deposit_limit': '250000.00',
            'daily_loss_limit': '75000.00',
        })

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.daily_deposit_limit, Decimal('250000.00'))
        self.assertEqual(self.profile.daily_loss_limit, Decimal('75000.00'))

    def test_settings_password_change_preserves_authenticated_session(self):
        response = self.client.post('/game-limits/', {
            'action': 'password',
            'old_password': 'strong-test-pass',
            'new_password1': 'Distinctive-settings-pass-2026!',
            'new_password2': 'Distinctive-settings-pass-2026!',
        })

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Distinctive-settings-pass-2026!'))
        account_page = self.client.get('/profile/')
        self.assertEqual(account_page.status_code, 200)

    def test_support_page_links_to_real_account_actions(self):
        response = self.client.get('/how-to-play/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Verify a round')
        self.assertContains(response, 'Open responsible play settings')

    def test_game_rules_page_uses_account_layout_and_links_related_controls(self):
        response = self.client.get('/game-rules/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Game Rules')
        self.assertContains(response, 'Server-verified results')
        self.assertContains(response, 'Verify a round')
        self.assertContains(response, 'Manage play settings')
        self.assertContains(response, "href='/game-rules/'")
        self.assertNotContains(response, 'Built with Django, HTML, CSS, and JavaScript')

    def test_scam_related_chat_is_blocked_and_creates_risk_alert(self):
        response = self.client.post('/play/', {
            'action': 'chat',
            'message': 'This looks like fraud',
        })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ChatMessage.objects.filter(user=self.user).exists())
        self.assertIn('Scams', response.json()['message'])
        self.assertTrue(RiskAlert.objects.filter(user=self.user, alert_type='flagged_chat').exists())


class GameDataPageTests(TestCase):
    def setUp(self):
        from .models import GameSession

        self.user = User.objects.create_user(
            username='game-data-player',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(user=self.user, balance=Decimal('600.00'))
        self.client.login(username='game-data-player', password='strong-test-pass')
        self.won_round = GameRound.objects.create(
            public_id='DATA-WON',
            server_seed_hash='a' * 64,
            client_seed='data-test',
            crash_point=Decimal('2.00'),
            status='crashed',
        )
        self.lost_round = GameRound.objects.create(
            public_id='DATA-LOST',
            server_seed_hash='b' * 64,
            client_seed='data-test',
            crash_point=Decimal('1.50'),
            status='crashed',
        )
        self.live_round = GameRound.objects.create(
            public_id='DATA-LIVE',
            server_seed_hash='c' * 64,
            client_seed='data-test',
            crash_point=Decimal('3.00'),
            status='flying',
        )
        GameSession.objects.create(
            user=self.user,
            round=self.won_round,
            stake=Decimal('100.00'),
            crash_point=Decimal('2.00'),
            cashed_out_at=Decimal('1.50'),
            won_amount=Decimal('150.00'),
        )
        GameSession.objects.create(
            user=self.user,
            round=self.lost_round,
            stake=Decimal('50.00'),
            crash_point=Decimal('1.50'),
        )
        GameSession.objects.create(
            user=self.user,
            round=self.live_round,
            stake=Decimal('25.00'),
            crash_point=Decimal('3.00'),
        )

    def test_dashboard_aggregates_real_sessions(self):
        response = self.client.get('/dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_bets'], 3)
        self.assertEqual(response.context['settled_count'], 2)
        self.assertEqual(response.context['total_staked'], Decimal('150.00'))
        self.assertEqual(response.context['total_returned'], Decimal('150.00'))
        self.assertEqual(response.context['net_result'], Decimal('0.00'))
        self.assertEqual(response.context['win_rate'], 50)

    def test_bet_history_filters_use_round_outcomes(self):
        won_response = self.client.get('/bet-history/?outcome=won')
        lost_response = self.client.get('/bet-history/?outcome=lost')
        live_response = self.client.get('/bet-history/?outcome=live')

        self.assertEqual(won_response.context['page_obj'].paginator.count, 1)
        self.assertEqual(won_response.context['sessions'][0].round.public_id, 'DATA-WON')
        self.assertEqual(lost_response.context['page_obj'].paginator.count, 1)
        self.assertEqual(lost_response.context['sessions'][0].round.public_id, 'DATA-LOST')
        self.assertEqual(live_response.context['page_obj'].paginator.count, 1)
        self.assertEqual(live_response.context['sessions'][0].round.public_id, 'DATA-LIVE')

    def test_bet_history_api_returns_live_game_records_and_current_balance(self):
        response = self.client.get('/api/bet-history/?outcome=all')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        data = response.json()
        self.assertEqual(data['total_bets'], 3)
        self.assertEqual(data['settled_bets'], 2)
        self.assertEqual(data['balance'], str(self.profile.balance))
        self.assertEqual(data['total_staked'], '150.00')
        self.assertEqual(data['total_returned'], '150.00')
        self.assertEqual(data['filter_counts'], {
            'all': 3,
            'won': 1,
            'lost': 1,
            'live': 1,
            'void': 0,
        })
        live_bet = next(session for session in data['sessions'] if session['round_id'] == 'DATA-LIVE')
        self.assertEqual(live_bet['status'], 'live')
        self.assertEqual(live_bet['multiplier'], '1.00')
        self.assertNotIn('crash_point', live_bet)

    def test_bet_history_reveals_overdue_rounds_through_game_settlement(self):
        overdue_round = GameRound.objects.create(
            public_id='DATA-OVERDUE',
            server_seed_hash='d' * 64,
            server_seed_private='overdue-test-seed',
            client_seed='data-test',
            crash_point=Decimal('1.50'),
            status='flying',
            started_at=timezone.now() - timedelta(seconds=20),
        )
        GameSession.objects.create(
            user=self.user,
            round=overdue_round,
            stake=Decimal('10.00'),
            crash_point=overdue_round.crash_point,
        )

        response = self.client.get('/api/bet-history/?outcome=live')

        self.assertIn('DATA-OVERDUE', response.json()['settle_rounds'])
        settle_response = self.client.post('/play/', {
            'action': 'settle_crash',
            'round_id': overdue_round.public_id,
        })

        self.assertEqual(settle_response.status_code, 200)
        overdue_round.refresh_from_db()
        self.assertEqual(overdue_round.status, 'crashed')
        self.assertTrue(overdue_round.is_revealed)
        updated_response = self.client.get('/api/bet-history/?outcome=lost')
        self.assertEqual(updated_response.json()['filter_counts']['lost'], 2)

    def test_bet_history_page_uses_full_width_live_activity_layout(self):
        response = self.client.get('/bet-history/?outcome=live')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-history-url="/api/bet-history/"')
        self.assertContains(response, 'data-history-stat="total_returned"')
        self.assertContains(response, 'data-history-filter="live"')
        self.assertContains(response, 'bet_history.js')
        self.assertContains(response, 'Live bets update automatically.')


class FinancialReportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='report-player',
            password='strong-test-pass',
        )
        Profile.objects.create(user=self.user)

    def test_financial_report_calculates_ggr_and_tax(self):
        today = timezone.now().date()
        GameRound.objects.create(
            public_id='REPORTROUND',
            server_seed_hash='a' * 64,
            client_seed='client',
            crash_point=Decimal('2.00'),
            status='crashed',
        )
        Transaction.objects.create(user=self.user, transaction_type='bet', amount=Decimal('100.00'), status='completed')
        from .models import GameSession
        GameSession.objects.create(
            user=self.user,
            stake=Decimal('100.00'),
            crash_point=Decimal('2.00'),
            cashed_out_at=Decimal('1.50'),
            won_amount=Decimal('50.00'),
        )

        report = generate_financial_report('daily', today, today, self.user)

        self.assertEqual(report.gross_gaming_revenue, Decimal('50.00'))
        self.assertEqual(report.ura_ggr_tax, Decimal('7.50'))
