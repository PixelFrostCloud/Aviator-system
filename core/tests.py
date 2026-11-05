import json
from io import BytesIO
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from .forms import PhoneNumberAuthenticationForm, PlatformBrandingForm, ProfileComplianceForm, WalletForm
from .models import AuditLog, ChatMessage, ChatModerationTerm, ChatReaction, ChatRain, ChatRainRecipient, FinancialReport, GamePresence, GameRound, GameSession, PaymentRequest, PlatformSettings, Profile, RewardClaim, RewardEvent, RewardTransaction, RewardWallet, RiskAlert, Room, RoomPlayer, SelfExclusion, SharedGameState, Transaction
from .payment_gateway import IotecPayGateway, PaymentGatewayError, PaymentGatewayResult
from .payment_services import initiate_deposit, initiate_withdrawal
from .reward_services import claim_reward
from .admin_profiles import admin_player_profile
from .utils import CRASH_DISTRIBUTION_POWER, MAX_CRASH_POINT, LEGACY_MAX_CRASH_POINT, RTP_FACTOR, calculate_crash_point, can_deposit, generate_financial_report, hash_server_seed, verify_game_round
from .views import _recent_chat_messages, _start_shared_betting_round, admin_console_section, admin_dashboard, admin_live_round_state, wallet


class AuthenticationTests(TestCase):
    def test_protected_account_pages_redirect_to_project_login(self):
        response = self.client.get('/profile/')

        self.assertRedirects(response, '/login/?next=/profile/')

    def test_registration_requires_phone_number(self):
        response = self.client.post('/register/', {
            'username': 'new-player',
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
            'password1': 'strong-test-pass',
            'password2': 'strong-test-pass',
        })

        self.assertEqual(registration.status_code, 302)
        self.assertEqual(registration.url, '/play/')
        user = User.objects.get(username='phone-player')
        self.assertEqual(user.profile.phone_number, '256700000001')
        self.assertEqual(user.profile.balance, Decimal('0.00'))
        self.assertEqual(user.email, '')

        self.client.logout()
        login_response = self.client.post('/login/', {
            'username': '0700000001',
            'password': 'strong-test-pass',
        }, follow=False)

        self.assertEqual(login_response.status_code, 302)
        self.assertEqual(login_response.url, '/play/')
        self.assertEqual(int(self.client.session['_auth_user_id']), user.id)

    def test_login_rejects_username_and_email_identifiers(self):
        user = User.objects.create_user(
            username='identifier-player',
            email='identifier-player@example.com',
            password='strong-test-pass',
        )
        Profile.objects.create(user=user, phone_number='256700000002')

        for identifier in ('identifier-player', 'identifier-player@example.com'):
            self.client.logout()
            form = PhoneNumberAuthenticationForm(data={
                'username': identifier,
                'password': 'strong-test-pass',
            })

            self.assertFalse(form.is_valid())

    def test_login_accepts_formatted_phone_number(self):
        user = User.objects.create_user(
            username='formatted-phone-player',
            password='strong-test-pass',
        )
        Profile.objects.create(user=user, phone_number='256700000003')

        form = PhoneNumberAuthenticationForm(data={
            'username': '+256 700 000 003',
            'password': 'strong-test-pass',
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.get_user().id, user.id)


class IotecGatewayRequestTests(TestCase):
    @override_settings(
        IOTEC_PAY_CLIENT_ID='test-client-id',
        IOTEC_PAY_CLIENT_SECRET='test-client-secret',
    )
    @patch('core.payment_gateway.request.urlopen')
    def test_oauth_client_credentials_token_is_used_for_api_request(self, urlopen):
        token_response = MagicMock()
        token_response.__enter__.return_value.read.return_value = (
            b'{"access_token":"unit-test-token","expires_in":300}'
        )
        api_response = MagicMock()
        api_response.__enter__.return_value.read.return_value = b'{}'
        urlopen.side_effect = [token_response, api_response]
        gateway = IotecPayGateway()
        gateway.token_cache = {'access_token': '', 'expires_at': 0}

        gateway._get('/api/wallet-balance/wallet-001')

        token_request, api_request = (call.args[0] for call in urlopen.call_args_list)
        self.assertIn(b'grant_type=client_credentials', token_request.data)
        self.assertIsNone(token_request.get_header('Authorization'))
        self.assertEqual(api_request.get_header('Authorization'), 'Bearer unit-test-token')

    @patch('core.payment_gateway.request.urlopen')
    def test_authenticated_requests_use_cached_bearer_access_token(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = b'{}'
        gateway = IotecPayGateway()
        gateway.token_cache = {
            'access_token': 'live-access-token',
            'expires_at': 9999999999,
        }

        gateway._get('/api/wallet-balance/wallet-001')

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer live-access-token')

    @patch('core.payment_gateway.request.urlopen', side_effect=TimeoutError('timed out'))
    def test_socket_timeout_is_retryable(self, urlopen):
        gateway = IotecPayGateway()
        gateway.token_cache = {'access_token': 'test-token', 'expires_at': 9999999999}

        with self.assertRaises(PaymentGatewayError) as raised:
            gateway._json_request('https://pay.example.test/status', data=None, headers={})

        self.assertTrue(raised.exception.retryable)
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
    def test_new_crash_distribution_targets_five_percent_at_ten_x(self):
        survival_at_10x = (RTP_FACTOR / Decimal('10')) ** (
            Decimal('1') / CRASH_DISTRIBUTION_POWER
        )
        survival_at_20x = (RTP_FACTOR / Decimal('20')) ** (
            Decimal('1') / CRASH_DISTRIBUTION_POWER
        )
        survival_at_50x = (RTP_FACTOR / Decimal('50')) ** (
            Decimal('1') / CRASH_DISTRIBUTION_POWER
        )

        self.assertAlmostEqual(float(survival_at_10x), 0.05, places=10)
        self.assertAlmostEqual(float(survival_at_20x), 0.0203715, places=7)
        self.assertAlmostEqual(float(survival_at_50x), 0.0062164, places=7)

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

    @patch('core.utils.hmac.new')
    def test_new_crash_points_are_capped_and_still_verifiable(self, mocked_hmac):
        mocked_hmac.return_value.hexdigest.return_value = 'f' * 64

        crash_point = calculate_crash_point('server-seed', 'client-seed')

        self.assertEqual(MAX_CRASH_POINT, Decimal('286.00'))
        self.assertEqual(crash_point, MAX_CRASH_POINT)
        self.assertTrue(verify_game_round(
            'server-seed',
            'client-seed',
            hash_server_seed('server-seed'),
            crash_point,
        ))

    @patch('core.utils.hmac.new')
    def test_legacy_round_crash_points_remain_verifiable(self, mocked_hmac):
        mocked_hmac.return_value.hexdigest.return_value = 'f' * 64
        legacy_crash_point = calculate_crash_point(
            'server-seed',
            'client-seed',
            max_crash_point=LEGACY_MAX_CRASH_POINT,
        )

        self.assertGreater(legacy_crash_point, MAX_CRASH_POINT)
        self.assertTrue(verify_game_round(
            'server-seed',
            'client-seed',
            hash_server_seed('server-seed'),
            legacy_crash_point,
        ))

    @patch('core.utils.hmac.new')
    def test_rounds_using_previous_distribution_remain_verifiable(self, mocked_hmac):
        mocked_hmac.return_value.hexdigest.return_value = '8' * 64
        uniform = Decimal(int('8' * 13, 16)) / Decimal(16 ** 13)
        legacy_crash_point = (
            (RTP_FACTOR / (Decimal('1.0000') - uniform))
            .quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        )

        self.assertTrue(verify_game_round(
            'server-seed',
            'client-seed',
            hash_server_seed('server-seed'),
            legacy_crash_point,
        ))


class PlayEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='player',
            email='player@example.com',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(user=self.user, balance=Decimal('1000.00'))
        self.client.login(username='player', password='strong-test-pass')

    def test_game_limits_show_the_configured_maximum_win(self):
        response = self.client.get('/play/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '1,000,000,000.00')

    def test_shared_state_opens_a_betting_window(self):
        response = self.client.post('/play/', {'action': 'state'})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'betting')
        self.assertGreater(payload['seconds_remaining'], 0)
        self.assertTrue(payload['round_id'])

    def test_online_counter_tracks_and_releases_authenticated_presence(self):
        presence = self.client.post('/play/', {
            'action': 'presence',
            'presence_key': 'test-online-counter',
        })

        self.assertEqual(presence.status_code, 200)
        self.assertEqual(presence.json()['online_count'], 1)

        count = self.client.get('/api/online-count/')
        self.assertEqual(count.status_code, 200)
        self.assertEqual(count.json()['online_count'], 1)

        release = self.client.post('/play/', {
            'action': 'presence_leave',
            'presence_key': 'test-online-counter',
        })
        self.assertEqual(release.status_code, 200)
        self.assertEqual(release.json()['online_count'], 0)

    def test_shared_state_reports_live_multiplier_and_persisted_round_history(self):
        first_state = self.client.post('/play/', {'action': 'state'}).json()
        game_round = GameRound.objects.get(public_id=first_state['round_id'])
        now = timezone.now()
        game_round.status = 'flying'
        game_round.crash_point = Decimal('286.00')
        game_round.started_at = now
        game_round.save(update_fields=['status', 'crash_point', 'started_at'])
        completed_round = GameRound.objects.create(
            public_id='HISTORYROUND',
            server_seed_hash='0' * 64,
            client_seed='history-seed',
            crash_point=Decimal('2.34'),
            status='crashed',
            started_at=now - timedelta(minutes=1),
            crashed_at=now - timedelta(minutes=1),
        )

        with patch('core.views.timezone.now', return_value=now + timedelta(seconds=2)):
            first_response = self.client.post('/play/', {'action': 'state'}).json()
        with patch('core.views.timezone.now', return_value=now + timedelta(seconds=4)):
            second_response = self.client.post('/play/', {'action': 'state'}).json()

        self.assertEqual(first_response['multiplier_rate'], 0.16)
        self.assertEqual(first_response['max_crash_point'], 286.0)
        self.assertEqual(first_response['current_multiplier'], 1.32)
        self.assertGreater(second_response['current_multiplier'], first_response['current_multiplier'])
        self.assertIn(
            {'round_id': completed_round.public_id, 'crash_point': 2.34},
            first_response['recent_crashes'],
        )

    def test_shared_state_reveals_crash_data_for_round_history(self):
        state = self.client.post('/play/', {'action': 'state'}).json()
        game_round = GameRound.objects.get(public_id=state['round_id'])
        game_round.status = 'flying'
        game_round.crash_point = Decimal('1.01')
        game_round.started_at = timezone.now() - timedelta(seconds=5)
        game_round.save(update_fields=['status', 'crash_point', 'started_at'])

        response = self.client.post('/play/', {'action': 'state'})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'crashed')
        self.assertEqual(payload['round_id'], game_round.public_id)
        self.assertEqual(payload['crash_point'], 1.01)
        self.assertTrue(payload['server_seed'])
        self.assertIn(
            {'round_id': game_round.public_id, 'crash_point': 1.01},
            payload['recent_crashes'],
        )

    def test_round_history_payload_is_newest_crash_first(self):
        oldest = GameRound.objects.create(
            public_id='HISTORYOLD',
            server_seed_hash='a' * 64,
            client_seed='history-old',
            crash_point=Decimal('1.06'),
            status='crashed',
            started_at=timezone.now() - timedelta(minutes=3),
            crashed_at=timezone.now() - timedelta(minutes=3),
        )
        newest = GameRound.objects.create(
            public_id='HISTORYNEW',
            server_seed_hash='b' * 64,
            client_seed='history-new',
            crash_point=Decimal('1.17'),
            status='crashed',
            started_at=timezone.now() - timedelta(minutes=1),
            crashed_at=timezone.now() - timedelta(minutes=1),
        )

        payload = self.client.post('/play/', {'action': 'state'}).json()
        history_ids = [round_data['round_id'] for round_data in payload['recent_crashes']]

        self.assertLess(history_ids.index(newest.public_id), history_ids.index(oldest.public_id))

    def test_live_ledger_returns_the_active_round(self):
        state = self.client.post('/play/', {'action': 'state'}).json()
        response = self.client.post('/play/', {'action': 'ledger', 'tab': 'all'})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['round_id'], state['round_id'])
        self.assertEqual(payload['rows'], [])

    def test_my_bets_ledger_returns_latest_30_across_rounds(self):
        older_round = GameRound.objects.create(
            public_id='MYHISTORYOLD',
            server_seed_hash='c' * 64,
            client_seed='my-history-old-seed',
            crash_point=Decimal('1.17'),
            status='crashed',
            started_at=timezone.now() - timedelta(minutes=2),
            crashed_at=timezone.now() - timedelta(minutes=1),
        )
        newer_round = GameRound.objects.create(
            public_id='MYHISTORYNEW',
            server_seed_hash='d' * 64,
            client_seed='my-history-new-seed',
            crash_point=Decimal('1.42'),
            status='crashed',
            started_at=timezone.now() - timedelta(seconds=30),
            crashed_at=timezone.now() - timedelta(seconds=20),
        )
        for stake in range(1, 33):
            GameSession.objects.create(
                user=self.user,
                round=older_round if stake <= 16 else newer_round,
                stake=Decimal(stake),
                crash_point=older_round.crash_point if stake <= 16 else newer_round.crash_point,
            )

        response = self.client.post('/play/', {'action': 'ledger', 'tab': 'mine'})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['total_count'], 32)
        self.assertEqual(payload['tab_count'], 30)
        self.assertEqual(len(payload['rows']), 30)
        self.assertEqual(payload['rows'][0]['bet'], 32.0)
        self.assertEqual(payload['rows'][-1]['bet'], 3.0)
        self.assertEqual(payload['rows'][0]['multiplier'], 1.42)
        self.assertEqual(payload['rows'][0]['status'], 'lost')

    def test_viewer_without_a_bet_can_check_shared_round_settlement(self):
        state = self.client.post('/play/', {'action': 'state'}).json()

        response = self.client.post('/play/', {
            'action': 'settle_crash',
            'round_id': state['round_id'],
        })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['message'], 'Round is still flying.')

    def test_frontend_bet_request_is_idempotent_in_shared_round(self):
        self.client.post('/play/', {'action': 'state'})
        bet_data = json.dumps([{
            'amount': 10,
            'auto_cashout_at': 2,
            'request_id': 'bet-request-1',
        }])

        first = self.client.post('/play/', {'action': 'start', 'bet_data': bet_data})
        second = self.client.post('/play/', {'action': 'start', 'bet_data': bet_data})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['session_ids'], second.json()['session_ids'])
        self.assertEqual(first.json()['balance'], 990.0)
        self.assertEqual(second.json()['balance'], 990.0)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('990.00'))
        session = GameSession.objects.get(bet_request_id='bet-request-1')
        self.assertEqual(session.auto_cashout_at, Decimal('2.00'))

    def test_two_bet_options_debit_the_exact_combined_stake(self):
        self.profile.balance = Decimal('5000.00')
        self.profile.save(update_fields=['balance'])

        first = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([{
                'amount': 2000,
                'request_id': 'two-slot-bet-1',
            }]),
        })
        second = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([{
                'amount': 2000,
                'request_id': 'two-slot-bet-2',
            }]),
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['balance'], 3000.0)
        self.assertEqual(second.json()['balance'], 1000.0)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('1000.00'))
        self.assertEqual(
            Transaction.objects.filter(
                reference=first.json()['round_id'],
                transaction_type='bet',
            ).aggregate(total=Sum('amount'))['total'],
            Decimal('4000.00'),
        )

    def test_shared_state_returns_balance_after_automatic_cashout(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([{
                'amount': 10,
                'auto_cashout_at': 2,
                'request_id': 'auto-cashout-balance',
            }]),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        game_round.status = 'flying'
        game_round.crash_point = Decimal('5.00')
        game_round.started_at = timezone.now()
        game_round.save(update_fields=['status', 'crash_point', 'started_at'])

        with patch('core.views.multiplier_at_elapsed_time', return_value=Decimal('2.00')):
            response = self.client.post('/play/', {'action': 'state'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['balance'], 1010.0)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('1010.00'))
        session = GameSession.objects.get(bet_request_id='auto-cashout-balance')
        self.assertEqual(session.cashed_out_at, Decimal('2.00'))
        self.assertEqual(session.won_amount, Decimal('20.00'))

    def test_start_round_commits_seed_and_debits_wallet(self):
        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([10, 20]),
        })

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.balance, Decimal('970.00'))
        self.assertEqual(len(payload['session_ids']), 2)
        self.assertEqual(GameRound.objects.get(public_id=payload['round_id']).server_seed, '')
        self.assertTrue(payload['server_seed_hash'])
        self.assertEqual(Transaction.objects.get(reference=payload['round_id'], transaction_type='bet').amount, Decimal('30.00'))

    def test_combined_bets_can_reach_the_per_round_limit(self):
        self.profile.balance = Decimal('500000.00')
        self.profile.daily_loss_limit = Decimal('1000000.00')
        self.profile.save(update_fields=['balance', 'daily_loss_limit'])

        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([180000, 180000]),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(str(response.json()['stake_total'])), Decimal('360000.00'))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('140000.00'))

    def test_combined_bet_limit_applies_across_both_bet_options(self):
        self.profile.balance = Decimal('500000.00')
        self.profile.daily_loss_limit = Decimal('1000000.00')
        self.profile.save(update_fields=['balance', 'daily_loss_limit'])
        self.client.post('/play/', {'action': 'state'})

        first = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([200000]),
        })
        second = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([160000.01]),
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()['code'], 'bet_limit_exceeded')
        self.assertEqual(second.json()['maximum_bet'], 360000.0)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('300000.00'))
        self.assertEqual(GameSession.objects.filter(user=self.user).count(), 1)

    def test_single_bet_cannot_exceed_the_per_round_limit(self):
        self.profile.balance = Decimal('500000.00')
        self.profile.daily_loss_limit = Decimal('1000000.00')
        self.profile.save(update_fields=['balance', 'daily_loss_limit'])

        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([360000.01]),
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'bet_limit_exceeded')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('500000.00'))
        self.assertFalse(GameSession.objects.filter(user=self.user).exists())

    def test_start_round_does_not_reveal_crash_point(self):
        response = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([10]),
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('crash_point', response.json())

    def test_successful_cashout_before_crash_does_not_reveal_crash_point(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([10]),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        game_round.crash_point = Decimal('10.00')
        game_round.started_at = timezone.now()
        game_round.save(update_fields=['crash_point', 'started_at'])

        response = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': ','.join(str(session_id) for session_id in start['session_ids']),
            'cashout_multiplier': '1.05',
        })

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'won')
        self.assertIsNone(payload['crash_point'])

    def test_cashout_uses_server_multiplier_when_client_omits_it(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([10]),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        game_round.crash_point = Decimal('10.00')
        game_round.started_at = timezone.now() - timedelta(seconds=2)
        game_round.save(update_fields=['crash_point', 'started_at'])
        GameSession.objects.filter(round=game_round).update(crash_point=Decimal('10.00'))

        response = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': str(start['session_ids'][0]),
        })

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreater(payload['cashout_multiplier'], 1.00)
        self.assertGreater(payload['won_amount'], 10.00)
        self.assertEqual(
            Decimal(str(payload['balance'])),
            Decimal('990.00') + Decimal(str(payload['won_amount'])),
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal(str(payload['balance'])))

    def test_voided_round_rejects_cashout_and_shows_refunded_stake_in_history(self):
        game_round = GameRound.objects.create(
            public_id='VOIDCASHOUTROUND',
            server_seed_hash='d' * 64,
            client_seed='void-cashout-seed',
            crash_point=Decimal('200.00'),
            status='void',
            started_at=timezone.now(),
        )
        bet = GameSession.objects.create(
            user=self.user,
            round=game_round,
            stake=Decimal('25.00'),
            crash_point=game_round.crash_point,
            admin_refunded_at=timezone.now(),
        )
        SharedGameState.objects.update_or_create(pk=1, defaults={'active_round': game_round})
        original_balance = self.profile.balance

        cashout = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': str(bet.pk),
            'cashout_multiplier': '2.00',
        })
        history = self.client.get('/api/bet-history/').json()

        self.profile.refresh_from_db()
        bet.refresh_from_db()
        self.assertEqual(cashout.status_code, 409)
        self.assertEqual(self.profile.balance, original_balance)
        self.assertIsNone(bet.cashed_out_at)
        self.assertEqual(history['total_returned'], '25.00')
        self.assertEqual(history['net_result'], '0.00')
        self.assertEqual(history['sessions'][0]['status'], 'refunded')
        self.assertEqual(history['sessions'][0]['payout'], '25.00')

    def test_combined_round_winnings_are_capped_at_one_billion(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([10, 10]),
        }).json()
        game_round = GameRound.objects.get(public_id=start['round_id'])
        game_round.crash_point = Decimal('100000.00')
        game_round.started_at = timezone.now() - timedelta(seconds=20000)
        game_round.save(update_fields=['crash_point', 'started_at'])
        sessions = list(GameSession.objects.filter(id__in=start['session_ids']))
        GameSession.objects.filter(id__in=start['session_ids']).update(
            stake=Decimal('999999.99'),
            crash_point=Decimal('100000.00'),
        )

        response = self.client.post('/play/', {
            'action': 'cashout',
            'session_ids': ','.join(str(session.id) for session in sessions),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(str(response.json()['won_amount'])), Decimal('1000000000.00'))
        self.assertEqual(
            GameSession.objects.filter(id__in=start['session_ids']).aggregate(total=Sum('won_amount'))['total'],
            Decimal('1000000000.00'),
        )

    def test_chat_rain_debits_sender_and_credits_active_player(self):
        recipient = User.objects.create_user(
            username='rain-recipient',
            email='rain-recipient@example.com',
            password='strong-test-pass',
        )
        recipient_profile = Profile.objects.create(user=recipient, balance=Decimal('20.00'))
        ChatMessage.objects.create(user=recipient, message='ready for rain')
        GamePresence.objects.create(
            user=recipient,
            presence_key='rain-recipient-presence',
            last_seen=timezone.now(),
        )

        response = self.client.post('/play/', {
            'action': 'chat_rain',
            'total_amount': '100.00',
            'players': '1',
        })

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        recipient_profile.refresh_from_db()
        self.assertEqual(self.profile.balance, Decimal('900.00'))
        self.assertEqual(recipient_profile.balance, Decimal('120.00'))
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

    def test_chat_rain_credits_requested_number_of_random_active_players(self):
        active_recipients = []
        for index in range(3):
            user = User.objects.create_user(
                username=f'rain-active-{index}',
                email=f'rain-active-{index}@example.com',
                password='strong-test-pass',
            )
            profile = Profile.objects.create(user=user, balance=Decimal('20.00'))
            GamePresence.objects.create(
                user=user,
                presence_key=f'rain-active-presence-{index}',
                last_seen=timezone.now(),
            )
            active_recipients.append((user, profile))

        inactive_user = User.objects.create_user(
            username='rain-inactive',
            email='rain-inactive@example.com',
            password='strong-test-pass',
        )
        inactive_profile = Profile.objects.create(user=inactive_user, balance=Decimal('20.00'))
        ChatMessage.objects.create(user=inactive_user, message='chat history is not presence')

        response = self.client.post('/play/', {
            'action': 'chat_rain',
            'total_amount': '100.00',
            'players': '2',
        })

        self.assertEqual(response.status_code, 200)
        rain = ChatRain.objects.get(sender=self.user)
        recipients = list(ChatRainRecipient.objects.filter(rain=rain))
        self.assertEqual(len(recipients), 2)
        self.assertEqual(len({recipient.user_id for recipient in recipients}), 2)
        self.assertTrue({recipient.user_id for recipient in recipients}.issubset(
            {user.id for user, _ in active_recipients}
        ))
        self.assertEqual(
            sum((recipient.amount for recipient in recipients), Decimal('0.00')),
            Decimal('100.00'),
        )
        for user, profile in active_recipients:
            profile.refresh_from_db()
            was_selected = any(recipient.user_id == user.id for recipient in recipients)
            self.assertEqual(profile.balance, Decimal('70.00') if was_selected else Decimal('20.00'))
        inactive_profile.refresh_from_db()
        self.assertEqual(inactive_profile.balance, Decimal('20.00'))

    def test_chat_rain_rejects_insufficient_wallet_balance(self):
        recipient = User.objects.create_user(
            username='rain-balance-recipient',
            email='rain-balance-recipient@example.com',
            password='strong-test-pass',
        )
        Profile.objects.create(user=recipient, balance=Decimal('20.00'))
        ChatMessage.objects.create(user=recipient, message='ready for rain')
        GamePresence.objects.create(
            user=recipient,
            presence_key='rain-balance-recipient-presence',
            last_seen=timezone.now(),
        )

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

    def test_chat_message_remains_visible_after_page_refresh_in_chronological_order(self):
        earlier_message = ChatMessage.objects.create(user=self.user, message='earlier message')
        sent = self.client.post('/play/', {
            'action': 'chat',
            'message': 'message survives refresh',
        })

        self.assertEqual(sent.status_code, 200)
        saved_message = ChatMessage.objects.get(pk=sent.json()['message']['id'])
        self.assertEqual(saved_message.message, 'message survives refresh')

        history = _recent_chat_messages()
        self.assertEqual(
            [message.id for message in history],
            [earlier_message.id, saved_message.id],
        )

    def test_abusive_chat_message_is_blocked_and_recorded_for_staff_review(self):
        responses = [
            self.client.post('/play/', {
                'action': 'chat',
                'message': message,
            })
            for message in ('That was st.u.p.i.d', 'That was stupid!')
        ]

        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.json()['success'])
            self.assertIn('Your message was not sent', response.json()['message'])
        self.assertFalse(ChatMessage.objects.filter(user=self.user).exists())
        self.assertEqual(RiskAlert.objects.filter(
            user=self.user,
            alert_type='chat_abuse_blocked',
        ).count(), 2)
        alert = RiskAlert.objects.filter(
            user=self.user,
            alert_type='chat_abuse_blocked',
        ).order_by('pk').first()
        self.assertEqual(alert.data['message'], 'That was st.u.p.i.d')
        self.assertEqual(alert.data['matched_term'].casefold(), 'stupid')
        self.assertEqual(alert.status, 'open')

    def test_custom_admin_can_manage_chat_moderation_terms_and_resolve_alerts(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])

        added = self.client.post('/admin-analytics/', {
            'action': 'add_chat_term',
            'term': '  custom   phrase  ',
        })
        self.assertEqual(added.status_code, 302)
        term = ChatModerationTerm.objects.get(term='custom phrase')

        blocked = self.client.post('/play/', {
            'action': 'chat',
            'message': 'This is a custom phrase.',
        })
        self.assertEqual(blocked.status_code, 400)
        alert = RiskAlert.objects.get(user=self.user, alert_type='chat_abuse_blocked')

        disabled = self.client.post('/admin-analytics/', {
            'action': 'toggle_chat_term',
            'term_id': term.pk,
        })
        self.assertEqual(disabled.status_code, 302)
        term.refresh_from_db()
        self.assertFalse(term.is_active)

        allowed = self.client.post('/play/', {
            'action': 'chat',
            'message': 'This is a custom phrase.',
        })
        self.assertEqual(allowed.status_code, 200)

        resolved = self.client.post('/admin-analytics/', {
            'action': 'resolve_chat_alert',
            'alert_id': alert.pk,
        })
        self.assertEqual(resolved.status_code, 302)
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'resolved')

    def test_chat_moderation_is_available_in_the_custom_staff_dashboard(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        alert = RiskAlert.objects.create(
            user=self.user,
            alert_type='chat_abuse_blocked',
            description='Blocked abuse in chat.',
            data={'message': 'moderation alert text', 'matched_term': 'sample term'},
        )
        request = RequestFactory().get('/admin-analytics/')
        request.user = self.user
        request.session = self.client.session
        request._messages = FallbackStorage(request)

        response = admin_dashboard(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Blocked-message alerts')
        self.assertContains(response, 'Abusive words and phrases')
        self.assertContains(response, alert.data['message'])

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

    def test_round_seed_cannot_be_revealed_before_server_crash_time(self):
        start = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([10]),
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
            'bet_data': json.dumps([10]),
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
            'bet_data': json.dumps([10]),
        })

        self.assertEqual(response.status_code, 403)
        self.assertIn('Daily loss limit', response.json()['message'])


class CustomAdminConsoleTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='console-staff', password='staff-password', is_staff=True)
        Profile.objects.create(user=self.staff, phone_number='0758000001')
        self.factory = RequestFactory()

    def make_request(self, path, method='get', data=None):
        request = getattr(self.factory, method)(path, data or {})
        request.user = self.staff
        request.session = self.client.session
        request._messages = FallbackStorage(request)
        return request

    def test_staff_console_sections_render_without_django_admin_permissions(self):
        sections = ('users', 'payments', 'transactions', 'players', 'live', 'rounds', 'reports', 'risk', 'safeguards', 'security', 'admins', 'settings')
        for section in sections:
            with self.subTest(section=section):
                response = admin_console_section(
                    self.make_request(f'/admin-analytics/{section}/'),
                    section,
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Authorized staff access only')
                if section == 'live':
                    self.assertContains(response, 'data-live-monitor')
                    self.assertContains(response, 'Emergency void controls are available to superusers only.')
                    self.assertNotContains(response, 'data-emergency-void-form')

    def test_staff_console_pages_include_mobile_navigation_drawer_controls(self):
        dashboard = admin_dashboard(self.make_request('/admin-analytics/'))
        section = admin_console_section(self.make_request('/admin-analytics/users/'), 'users')

        for page_name, response in (('dashboard', dashboard), ('section', section)):
            with self.subTest(page=page_name):
                self.assertContains(response, 'id="admin-menu-toggle"')
                self.assertContains(response, 'class="admin-nav-backdrop"')
                self.assertContains(response, 'admin_nav.js')
                self.assertContains(response, 'aria-controls="admin-navigation"')

    def test_only_superusers_can_emergency_void_the_active_flight(self):
        game_round = GameRound.objects.create(
            public_id='emergency-void-denied',
            server_seed_hash='a' * 64,
            client_seed='emergency-void-seed',
            crash_point=Decimal('200.00'),
            status='flying',
            started_at=timezone.now(),
        )
        SharedGameState.objects.update_or_create(pk=1, defaults={'active_round': game_round})

        response = admin_console_section(
            self.make_request('/admin-analytics/live/', 'post', {
                'action': 'emergency_void_round',
                'round_id': game_round.public_id,
                'confirmation': f'VOID {game_round.public_id}',
                'reason': 'Flight is stuck.',
            }),
            'live',
        )

        game_round.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(game_round.status, 'flying')

        superuser = User.objects.create_superuser(
            username='emergency-ui-superuser',
            password='strong-admin-password',
        )
        superuser_request = self.make_request('/admin-analytics/live/')
        superuser_request.user = superuser
        superuser_page = admin_console_section(superuser_request, 'live')
        self.assertEqual(superuser_page.status_code, 200)
        self.assertContains(superuser_page, 'data-emergency-void-form')
        self.assertContains(superuser_page, 'Void flight and refund open bets')

    def test_superuser_emergency_void_refunds_open_bets_and_preserves_cashouts(self):
        superuser = User.objects.create_superuser(
            username='emergency-superuser',
            password='strong-admin-password',
        )
        Profile.objects.create(user=superuser, phone_number='0758000098')
        open_player = User.objects.create_user(username='emergency-open-player', password='player-password')
        open_profile = Profile.objects.create(
            user=open_player,
            phone_number='0758000099',
            balance=Decimal('80.00'),
        )
        cashed_player = User.objects.create_user(username='emergency-cashed-player', password='player-password')
        cashed_profile = Profile.objects.create(
            user=cashed_player,
            phone_number='0758000100',
            balance=Decimal('140.00'),
        )
        game_round = GameRound.objects.create(
            public_id='emergency-void-success',
            server_seed_hash='b' * 64,
            client_seed='emergency-void-seed',
            crash_point=Decimal('200.00'),
            status='flying',
            started_at=timezone.now(),
        )
        SharedGameState.objects.update_or_create(pk=1, defaults={'active_round': game_round})
        open_bet = GameSession.objects.create(
            user=open_player,
            round=game_round,
            stake=Decimal('20.00'),
            crash_point=game_round.crash_point,
        )
        cashed_bet = GameSession.objects.create(
            user=cashed_player,
            round=game_round,
            stake=Decimal('20.00'),
            crash_point=game_round.crash_point,
            cashed_out_at=Decimal('2.00'),
            won_amount=Decimal('40.00'),
        )
        original_crash_point = game_round.crash_point
        request = self.make_request('/admin-analytics/live/', 'post', {
            'action': 'emergency_void_round',
            'round_id': game_round.public_id,
            'confirmation': f'VOID {game_round.public_id}',
            'reason': 'Flight stopped progressing.',
        })
        request.user = superuser

        response = admin_console_section(request, 'live')

        game_round.refresh_from_db()
        open_profile.refresh_from_db()
        cashed_profile.refresh_from_db()
        open_bet.refresh_from_db()
        cashed_bet.refresh_from_db()
        refund_reference = f'EMERGENCY-VOID-{game_round.public_id}-{open_bet.pk}'
        self.assertEqual(response.status_code, 302)
        self.assertEqual(game_round.status, 'void')
        self.assertEqual(game_round.crash_point, original_crash_point)
        self.assertEqual(open_profile.balance, Decimal('100.00'))
        self.assertIsNotNone(open_bet.admin_refunded_at)
        self.assertEqual(cashed_profile.balance, Decimal('140.00'))
        self.assertEqual(cashed_bet.cashed_out_at, Decimal('2.00'))
        self.assertEqual(cashed_bet.won_amount, Decimal('40.00'))
        self.assertEqual(Transaction.objects.filter(reference=refund_reference).count(), 1)
        audit = AuditLog.objects.get(
            action_type='admin_action',
            description='Voided a flying shared round and refunded unsettled stakes.',
        )
        self.assertEqual(audit.data['round_id'], game_round.public_id)
        self.assertEqual(audit.data['refund_total'], '20.00')
        live_state = admin_live_round_state(self.make_request('/admin-analytics/api/live-round/'))
        live_payload = json.loads(live_state.content)
        self.assertEqual(live_payload['status'], 'void')
        self.assertEqual(
            live_payload['current_multiplier'],
            float(Decimal(audit.data['current_multiplier'])),
        )

    def test_emergency_void_does_not_change_a_round_past_its_scheduled_crash(self):
        superuser = User.objects.create_superuser(
            username='emergency-timing-superuser',
            password='strong-admin-password',
        )
        Profile.objects.create(user=superuser, phone_number='0758000101')
        player = User.objects.create_user(username='emergency-timing-player', password='player-password')
        profile = Profile.objects.create(
            user=player,
            phone_number='0758000102',
            balance=Decimal('80.00'),
        )
        game_round = GameRound.objects.create(
            public_id='emergency-void-already-crashed',
            server_seed_private='private-emergency-seed',
            server_seed_hash='c' * 64,
            client_seed='emergency-void-seed',
            crash_point=Decimal('1.01'),
            status='flying',
            started_at=timezone.now() - timedelta(seconds=2),
        )
        SharedGameState.objects.update_or_create(pk=1, defaults={'active_round': game_round})
        bet = GameSession.objects.create(
            user=player,
            round=game_round,
            stake=Decimal('20.00'),
            crash_point=game_round.crash_point,
        )
        request = self.make_request('/admin-analytics/live/', 'post', {
            'action': 'emergency_void_round',
            'round_id': game_round.public_id,
            'confirmation': f'VOID {game_round.public_id}',
            'reason': 'Checking scheduled crash protection.',
        })
        request.user = superuser

        response = admin_console_section(request, 'live')

        game_round.refresh_from_db()
        profile.refresh_from_db()
        bet.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(game_round.status, 'crashed')
        self.assertTrue(game_round.is_revealed)
        self.assertEqual(profile.balance, Decimal('80.00'))
        self.assertIsNone(bet.admin_refunded_at)
        self.assertFalse(Transaction.objects.filter(reference__startswith='EMERGENCY-VOID-').exists())

    def test_non_staff_are_redirected_from_console_sections(self):
        player = User.objects.create_user(username='console-player', password='player-password')
        request = self.make_request('/admin-analytics/users/')
        request.user = player

        response = admin_console_section(request, 'users')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/dashboard/')

    def test_staff_can_lock_player_account_with_audit_record(self):
        player = User.objects.create_user(username='console-target', password='player-password')
        Profile.objects.create(user=player, phone_number='0758000002')
        request = self.make_request('/admin-analytics/users/', 'post', {
            'action': 'lock_user',
            'user_id': player.pk,
            'reason': 'Repeated abusive chat.',
        })

        response = admin_console_section(request, 'users')

        player.refresh_from_db()
        player.profile.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(player.is_active)
        self.assertEqual(player.profile.account_status, 'locked')
        blocked_login = PhoneNumberAuthenticationForm(data={
            'username': '0758000002',
            'password': 'player-password',
        })
        self.assertFalse(blocked_login.is_valid())
        self.assertTrue(self.staff.audit_logs.filter(
            action_type='admin_action',
            data__target_user_id=player.pk,
        ).exists())

    def test_player_list_links_to_a_dedicated_profile_instead_of_inline_controls(self):
        player = User.objects.create_user(username='profile-target', password='player-password')
        Profile.objects.create(user=player, phone_number='0758000008')

        list_response = admin_console_section(
            self.make_request('/admin-analytics/users/'),
            'users',
        )
        self.assertContains(
            list_response,
            f'/admin-analytics/users/{player.pk}/',
        )
        self.assertNotContains(list_response, 'Reason for lock or ban')
        self.assertNotContains(list_response, 'Delete / anonymize')

        profile_response = admin_player_profile(
            self.make_request(f'/admin-analytics/users/{player.pk}/'),
            player.pk,
        )
        self.assertEqual(profile_response.status_code, 200)
        self.assertContains(profile_response, 'Manage player access')
        self.assertContains(profile_response, 'Anonymize identity')
        self.assertContains(profile_response, 'Wallet ledger')

    def test_player_profile_actions_are_bound_to_the_profile_user(self):
        first_player = User.objects.create_user(username='first-profile-player', password='player-password')
        first_profile = Profile.objects.create(user=first_player, phone_number='0758000009')
        other_player = User.objects.create_user(username='other-profile-player', password='player-password')
        other_profile = Profile.objects.create(user=other_player, phone_number='0758000010')

        response = admin_player_profile(
            self.make_request(
                f'/admin-analytics/users/{first_player.pk}/',
                'post',
                {
                    'action': 'lock_user',
                    'user_id': other_player.pk,
                    'reason': 'Policy review required.',
                },
            ),
            first_player.pk,
        )

        first_profile.refresh_from_db()
        other_profile.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f'/admin-analytics/users/{first_player.pk}/')
        self.assertEqual(first_profile.account_status, 'locked')
        self.assertEqual(other_profile.account_status, 'active')

    def test_profile_wallet_refund_is_limited_to_that_players_bet_and_idempotent(self):
        player = User.objects.create_user(username='profile-refund-player', password='player-password')
        profile = Profile.objects.create(
            user=player,
            phone_number='0758000011',
            balance=Decimal('300.00'),
        )
        game_round = GameRound.objects.create(
            public_id='profile-refund-round',
            server_seed_hash='f' * 64,
            client_seed='profile-refund-seed',
            crash_point=Decimal('2.00'),
            status='crashed',
        )
        bet = GameSession.objects.create(
            user=player,
            round=game_round,
            stake=Decimal('50.00'),
            crash_point=Decimal('2.00'),
        )
        unrelated_player = User.objects.create_user(username='profile-other-player', password='player-password')
        unrelated_bet = GameSession.objects.create(
            user=unrelated_player,
            round=game_round,
            stake=Decimal('25.00'),
            crash_point=Decimal('2.00'),
        )

        def refund_request(session):
            return admin_player_profile(
                self.make_request(
                    f'/admin-analytics/users/{player.pk}/',
                    'post',
                    {
                        'action': 'refund_lost_bet',
                        'session_id': session.pk,
                        'reason': 'Settlement adjustment.',
                    },
                ),
                player.pk,
            )

        self.assertEqual(refund_request(bet).status_code, 302)
        with self.assertRaises(Http404):
            refund_request(unrelated_bet)
        self.assertEqual(refund_request(bet).status_code, 302)

        profile.refresh_from_db()
        self.assertEqual(profile.balance, Decimal('350.00'))
        self.assertEqual(
            Transaction.objects.filter(reference=f'ADMIN-BET-REFUND-{bet.pk}').count(),
            1,
        )
        self.assertFalse(
            Transaction.objects.filter(reference=f'ADMIN-BET-REFUND-{unrelated_bet.pk}').exists()
        )

    def test_unlock_does_not_reactivate_a_previously_disabled_user(self):
        player = User.objects.create_user(
            username='disabled-target',
            password='player-password',
            is_active=False,
        )
        Profile.objects.create(
            user=player,
            phone_number='0758000006',
            account_status='locked',
        )
        response = admin_console_section(
            self.make_request('/admin-analytics/users/', 'post', {
                'action': 'unlock_user',
                'user_id': player.pk,
            }),
            'users',
        )

        player.refresh_from_db()
        player.profile.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(player.is_active)
        self.assertEqual(player.profile.account_status, 'active')

    def test_anonymize_retains_financial_and_game_records(self):
        player = User.objects.create_user(
            username='anonymize-target',
            email='anonymize@example.test',
            password='player-password',
        )
        Profile.objects.create(user=player, phone_number='0758000007')
        transaction = Transaction.objects.create(
            user=player,
            transaction_type='bet',
            amount=Decimal('30.00'),
            reference='ANONYMIZE-TXN-001',
        )
        bet = GameSession.objects.create(
            user=player,
            stake=Decimal('30.00'),
            crash_point=Decimal('2.00'),
        )

        response = admin_console_section(
            self.make_request('/admin-analytics/users/', 'post', {
                'action': 'anonymize_user',
                'user_id': player.pk,
                'confirmation': f'ANONYMIZE {player.pk}',
            }),
            'users',
        )

        player.refresh_from_db()
        player.profile.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(player.is_active)
        self.assertEqual(player.profile.phone_number, '')
        self.assertEqual(player.profile.account_status, 'anonymized')
        self.assertNotEqual(player.username, 'anonymize-target')
        self.assertTrue(Transaction.objects.filter(pk=transaction.pk, user=player).exists())
        self.assertTrue(GameSession.objects.filter(pk=bet.pk, user=player).exists())

    def test_branding_upload_validation_rejects_non_image_files(self):
        image_buffer = BytesIO()
        Image.new('RGB', (1, 1), color='black').save(image_buffer, format='PNG')
        valid_form = PlatformBrandingForm(
            data={'site_name': 'Test Brand', 'site_tagline': 'Test tagline'},
            files={'logo': SimpleUploadedFile('logo.png', image_buffer.getvalue(), content_type='image/png')},
        )
        self.assertTrue(valid_form.is_valid(), valid_form.errors)

        invalid_form = PlatformBrandingForm(
            data={'site_name': 'Test Brand', 'site_tagline': 'Test tagline'},
            files={'logo': SimpleUploadedFile('broken.png', b'not an image', content_type='image/png')},
        )
        self.assertFalse(invalid_form.is_valid())

    def test_staff_can_create_phone_login_operations_account(self):
        response = admin_console_section(
            self.make_request('/admin-analytics/admins/', 'post', {
                'action': 'create_admin',
                'username': 'ops-manager',
                'phone_number': '0758000003',
                'role': 'staff',
                'password1': 'Admin-Strong-Password-948!',
                'password2': 'Admin-Strong-Password-948!',
            }),
            'admins',
        )

        created = User.objects.get(username='ops-manager')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(created.is_staff)
        self.assertFalse(created.is_superuser)
        self.assertEqual(created.profile.phone_number, '256758000003')
        self.assertTrue(created.check_password('Admin-Strong-Password-948!'))

    def test_staff_cannot_grant_superuser_access(self):
        response = admin_console_section(
            self.make_request('/admin-analytics/admins/', 'post', {
                'action': 'create_admin',
                'username': 'blocked-super-admin',
                'phone_number': '0758000004',
                'role': 'superuser',
                'password1': 'Admin-Strong-Password-948!',
                'password2': 'Admin-Strong-Password-948!',
            }),
            'admins',
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='blocked-super-admin').exists())

    def test_game_settings_are_validated_and_apply_to_new_rounds(self):
        response = admin_console_section(
            self.make_request('/admin-analytics/settings/', 'post', {
                'action': 'save_game_settings',
                'maximum_crash_point': '270.00',
                'betting_seconds': '6',
            }),
            'settings',
        )

        self.assertEqual(response.status_code, 302)
        game_settings = PlatformSettings.objects.get(pk=1)
        self.assertEqual(game_settings.maximum_crash_point, Decimal('270.00'))
        self.assertEqual(game_settings.betting_seconds, 6)

        state = SharedGameState.objects.create()
        round_request = self.make_request('/play/')
        game_round = _start_shared_betting_round(state, self.staff, round_request)
        self.assertEqual(game_round.status, 'betting')
        self.assertLessEqual(game_round.crash_point, Decimal('270.00'))
        self.assertGreaterEqual(
            (game_round.betting_ends_at - timezone.now()).total_seconds(),
            5,
        )

        invalid_response = admin_console_section(
            self.make_request('/admin-analytics/settings/', 'post', {
                'action': 'save_game_settings',
                'maximum_crash_point': '286.01',
                'betting_seconds': '6',
            }),
            'settings',
        )
        self.assertEqual(invalid_response.status_code, 200)
        game_settings.refresh_from_db()
        self.assertEqual(game_settings.maximum_crash_point, Decimal('270.00'))

    def test_branding_settings_save_and_reject_invalid_names(self):
        response = admin_console_section(
            self.make_request('/admin-analytics/settings/', 'post', {
                'action': 'save_branding',
                'site_name': 'Charcoal Aviator',
                'site_tagline': 'A new player experience',
            }),
            'settings',
        )

        self.assertEqual(response.status_code, 302)
        settings_record = PlatformSettings.objects.get(pk=1)
        self.assertEqual(settings_record.site_name, 'Charcoal Aviator')
        self.assertEqual(settings_record.site_tagline, 'A new player experience')

        invalid_response = admin_console_section(
            self.make_request('/admin-analytics/settings/', 'post', {
                'action': 'save_branding',
                'site_name': '',
                'site_tagline': 'A new player experience',
            }),
            'settings',
        )
        self.assertEqual(invalid_response.status_code, 200)
        settings_record.refresh_from_db()
        self.assertEqual(settings_record.site_name, 'Charcoal Aviator')

    def test_refund_credits_lost_bet_once_and_keeps_ledger(self):
        player = User.objects.create_user(username='refund-target', password='player-password')
        profile = Profile.objects.create(
            user=player,
            phone_number='0758000005',
            balance=Decimal('500.00'),
        )
        game_round = GameRound.objects.create(
            public_id='refund-round-1',
            server_seed_hash='c' * 64,
            client_seed='refund-client-seed',
            crash_point=Decimal('2.00'),
            status='crashed',
        )
        bet = GameSession.objects.create(
            user=player,
            round=game_round,
            stake=Decimal('100.00'),
            crash_point=Decimal('2.00'),
        )

        for _ in range(2):
            response = admin_console_section(
                self.make_request(
                    f'/admin-analytics/players/?round_id={game_round.pk}',
                    'post',
                    {
                        'action': 'refund_lost_bet',
                        'session_id': bet.pk,
                        'reason': 'Settlement correction.',
                    },
                ),
                'players',
            )
            self.assertEqual(response.status_code, 302)

        profile.refresh_from_db()
        bet.refresh_from_db()
        self.assertEqual(profile.balance, Decimal('600.00'))
        self.assertIsNotNone(bet.admin_refunded_at)
        self.assertEqual(
            Transaction.objects.filter(reference=f'ADMIN-BET-REFUND-{bet.pk}').count(),
            1,
        )

    def test_round_players_filter_handles_no_bets_and_recent_presence(self):
        game_round = GameRound.objects.create(
            public_id='players-round-1',
            server_seed_hash='d' * 64,
            client_seed='players-client-seed',
            crash_point=Decimal('2.00'),
        )
        response = admin_console_section(
            self.make_request(f'/admin-analytics/players/?round_id={game_round.pk}'),
            'players',
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No bets are recorded for this round.')

        bet = GameSession.objects.create(
            user=self.staff,
            round=game_round,
            stake=Decimal('25.00'),
            crash_point=Decimal('2.00'),
        )
        GamePresence.objects.create(
            user=self.staff,
            presence_key='admin-test-presence',
            last_seen=timezone.now(),
        )
        online_response = admin_console_section(
            self.make_request(f'/admin-analytics/players/?round_id={game_round.pk}'),
            'players',
        )
        self.assertContains(online_response, 'Recently online')
        self.assertContains(online_response, f'Bet #{bet.pk}')

    def test_admin_console_search_and_filters_cover_record_fields(self):
        player = User.objects.create_user(
            username='search-target-player',
            first_name='Searchable',
            last_name='Player',
            password='player-password',
        )
        Profile.objects.create(
            user=player,
            phone_number='0758123456',
            account_status='locked',
        )
        PaymentRequest.objects.create(
            user=player,
            payment_type='withdraw',
            provider='airtel_money',
            amount=Decimal('900.00'),
            phone_number='0758765432',
            external_reference='SEARCH-PAY-001',
            provider_reference='PROVIDER-SEARCH-001',
            status='failed',
            failure_reason='Searchable gateway failure',
        )
        Transaction.objects.create(
            user=player,
            transaction_type='refund',
            amount=Decimal('45.00'),
            status='pending',
            reference='SEARCH-TXN-001',
            description='Searchable adjustment',
        )
        game_round = GameRound.objects.create(
            public_id='SEARCH-ROUND-001',
            server_seed_hash='f' * 64,
            client_seed='search-round-seed',
            crash_point=Decimal('45.67'),
            status='crashed',
        )
        alert = RiskAlert.objects.create(
            user=player,
            alert_type='searchable_alert',
            severity='high',
            status='reviewing',
            description='Risk review record',
            data={'message': 'Searchable message detail'},
        )
        AuditLog.objects.create(
            user=player,
            action_type='risk_alert',
            description='Security event for search.',
            ip_address='192.0.2.45',
            data={'event': 'searchable_event', 'account_key': 'target-account-key'},
        )
        admin = User.objects.create_user(
            username='search-target-admin',
            password='admin-password',
            is_staff=True,
        )
        Profile.objects.create(user=admin, phone_number='0758999000')

        cases = (
            ('users', '?q=0758123456&account_status=locked', 'search-target-player'),
            ('payments', '?q=0758765432&status=failed&type=withdraw', 'SEARCH-PAY-001'),
            ('transactions', '?q=Searchable+adjustment&status=pending&type=refund', 'SEARCH-TXN-001'),
            ('rounds', '?q=45.67&status=crashed', 'SEARCH-ROUND-001'),
            ('risk', '?q=Searchable+message+detail&status=reviewing', 'searchable_alert'),
            ('security', '?q=target-account-key', 'searchable_event'),
            ('admins', '?q=0758999000', 'search-target-admin'),
        )
        for section, query, expected in cases:
            with self.subTest(section=section):
                response = admin_console_section(
                    self.make_request(f'/admin-analytics/{section}/{query}'),
                    section,
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected)

        other_player = User.objects.create_user(
            username='search-other-player',
            password='player-password',
        )
        GameSession.objects.create(
            user=player,
            round=game_round,
            stake=Decimal('10.00'),
            crash_point=game_round.crash_point,
        )
        GameSession.objects.create(
            user=other_player,
            round=game_round,
            stake=Decimal('20.00'),
            crash_point=game_round.crash_point,
        )
        players_response = admin_console_section(
            self.make_request(
                f'/admin-analytics/players/?round_id={game_round.pk}&q=search-target-player',
            ),
            'players',
        )
        self.assertContains(players_response, 'search-target-player')
        self.assertNotContains(players_response, 'search-other-player')
        self.assertEqual(players_response.content.count(b'name="q"'), 1)

    def test_csv_exports_cover_console_sections_and_escape_formula_cells(self):
        game_round = GameRound.objects.create(
            public_id='export-round-1',
            server_seed_hash='e' * 64,
            client_seed='export-client-seed',
            crash_point=Decimal('2.00'),
            status='crashed',
        )
        PaymentRequest.objects.create(
            user=self.staff,
            payment_type='deposit',
            provider='mtn_momo',
            amount=Decimal('500.00'),
            external_reference='EXPORT-PAY-001',
        )
        Transaction.objects.create(
            user=self.staff,
            transaction_type='refund',
            amount=Decimal('25.00'),
            reference='EXPORT-TXN-001',
        )
        GameSession.objects.create(
            user=self.staff,
            round=game_round,
            stake=Decimal('25.00'),
            crash_point=Decimal('2.00'),
        )
        FinancialReport.objects.create(
            report_type='daily',
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            generated_by=self.staff,
        )
        RiskAlert.objects.create(
            user=self.staff,
            alert_type='export-test',
            description='CSV export test alert.',
        )
        SelfExclusion.objects.create(user=self.staff, reason='CSV export test.')
        AuditLog.objects.create(
            user=self.staff,
            action_type='risk_alert',
            description='   =HYPERLINK("https://example.test")',
            data={'event': '=unsafe-event'},
        )

        for section in (
            'users', 'payments', 'transactions', 'players', 'rounds',
            'live', 'reports', 'risk', 'safeguards', 'security', 'admins',
        ):
            with self.subTest(section=section):
                request = self.make_request(
                    f'/admin-analytics/{section}/?export=csv',
                )
                response = admin_console_section(request, section)
                self.assertEqual(response.status_code, 200)
                self.assertIn('text/csv', response['Content-Type'])

        security_response = admin_console_section(
            self.make_request('/admin-analytics/security/?export=csv'),
            'security',
        )
        csv_content = security_response.content.decode('utf-8-sig')
        self.assertIn("'   =HYPERLINK", csv_content)
        self.assertIn("'=unsafe-event", csv_content)

    def test_staff_can_update_risk_alert_status(self):
        alert = RiskAlert.objects.create(
            user=self.staff,
            alert_type='test_alert',
            description='Console review test.',
        )
        request = self.make_request('/admin-analytics/risk/', 'post', {
            'action': 'update_alert',
            'alert_id': alert.pk,
            'status': 'resolved',
        })

        response = admin_console_section(request, 'risk')

        alert.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(alert.status, 'resolved')
        self.assertIsNotNone(alert.resolved_at)

    def test_round_console_displays_commitment_without_exposing_private_seed(self):
        private_seed = 'do-not-display-this-seed'
        game_round = GameRound.objects.create(
            public_id='console-round-1',
            server_seed_hash='a' * 64,
            server_seed_private=private_seed,
            server_seed='revealed-server-seed',
            client_seed='console-client-seed',
            crash_point=Decimal('2.45'),
            status='crashed',
        )
        response = admin_console_section(
            self.make_request('/admin-analytics/rounds/'),
            'rounds',
        )

        self.assertContains(response, game_round.public_id)
        self.assertContains(response, game_round.server_seed_hash)
        self.assertNotContains(response, private_seed)
        self.assertNotContains(response, game_round.server_seed)

    def test_console_renders_payment_report_safeguard_and_security_records(self):
        payment = PaymentRequest.objects.create(
            user=self.staff,
            payment_type='deposit',
            provider='mtn_momo',
            amount=Decimal('500.00'),
            external_reference='CONSOLE-PAY-001',
        )
        Transaction.objects.create(
            user=self.staff,
            transaction_type='deposit',
            amount=Decimal('500.00'),
            reference='CONSOLE-TXN-001',
        )
        FinancialReport.objects.create(
            report_type='daily',
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            generated_by=self.staff,
        )
        SelfExclusion.objects.create(user=self.staff, reason='Player requested a break.')
        AuditLog.objects.create(
            user=self.staff,
            action_type='risk_alert',
            description='Security event: login_failed',
            data={'event': 'login_failed', 'account_key': '0758000001'},
        )
        AuditLog.objects.create(
            user=None,
            action_type='risk_alert',
            description='Security event without an associated account.',
            data={'event': 'configuration_changed'},
        )

        pages = (
            ('payments', 'CONSOLE-PAY-001'),
            ('transactions', 'CONSOLE-TXN-001'),
            ('reports', 'Daily'),
            ('safeguards', 'Player requested a break.'),
            ('security', 'login_failed'),
        )
        for section, expected in pages:
            with self.subTest(section=section):
                response = admin_console_section(
                    self.make_request(f'/admin-analytics/{section}/'),
                    section,
                )
                self.assertContains(response, expected)

    def test_staff_live_monitor_reports_round_state_without_revealing_outcome(self):
        private_seed = 'private-until-the-round-is-over'
        game_round = GameRound.objects.create(
            public_id='console-live-1',
            server_seed_hash='b' * 64,
            server_seed_private=private_seed,
            server_seed='',
            client_seed='monitor-seed',
            crash_point=Decimal('12.00'),
            status='flying',
            started_at=timezone.now() - timedelta(seconds=5),
        )
        SharedGameState.objects.get_or_create(pk=1)
        SharedGameState.objects.filter(pk=1).update(active_round=game_round)
        GameSession.objects.create(
            user=self.staff,
            round=game_round,
            stake=Decimal('20.00'),
            crash_point=Decimal('12.00'),
        )
        request = self.make_request('/admin-analytics/api/live-round/')

        response = admin_live_round_state(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload['round_id'], game_round.public_id)
        self.assertEqual(payload['status'], 'flying')
        self.assertAlmostEqual(payload['current_multiplier'], 1.5, places=1)
        self.assertEqual(payload['open_bets'], 1)
        self.assertEqual(payload['total_stakes'], 20.0)
        self.assertNotIn('crash_point', payload)
        self.assertNotIn('server_seed', payload)
        game_round.refresh_from_db()
        self.assertEqual(game_round.status, 'flying')
        self.assertEqual(game_round.server_seed_private, private_seed)

    def test_non_staff_cannot_read_live_monitor(self):
        player = User.objects.create_user(username='monitor-player', password='player-password')
        request = self.make_request('/admin-analytics/api/live-round/')
        request.user = player

        response = admin_live_round_state(request)

        self.assertEqual(response.status_code, 403)


class RoomEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='room-host',
            email='room-host@example.com',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(
            user=self.user,
            balance=Decimal('1000.00'),
            phone_number='256750000000',
        )
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


class WalletComplianceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='wallet-player',
            email='wallet@example.com',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(
            user=self.user,
            balance=Decimal('1000.00'),
            phone_number='256750000000',
        )
        self.client.login(username='wallet-player', password='strong-test-pass')

    def test_deposit_creates_payment_request_and_credits_wallet(self):
        self.profile.daily_deposit_limit = Decimal('10000000.00')
        self.profile.save(update_fields=['daily_deposit_limit'])

        response = self.client.post('/wallet/', {
            'action': 'deposit',
            'deposit-amount': '5000',
            'deposit-provider': 'mtn_momo',
            'deposit-phone_number': '256700000000',
        })

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        payment = PaymentRequest.objects.get(user=self.user, payment_type='deposit')

        self.assertEqual(self.profile.balance, Decimal('6000.00'))
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.phone_number, '256700000000')
        self.assertEqual(Transaction.objects.get(reference=payment.external_reference).transaction_type, 'deposit')

    def test_wallet_forms_render_distinct_payment_idempotency_keys(self):
        with patch('core.views.render', return_value=HttpResponse('')) as render:
            response = self.client.get('/wallet/')

        self.assertEqual(response.status_code, 200)
        context = render.call_args.args[2]
        deposit_key = context['deposit_idempotency_key']
        withdraw_key = context['withdraw_idempotency_key']
        self.assertEqual(len(deposit_key), 32)
        self.assertEqual(len(withdraw_key), 32)
        self.assertNotEqual(deposit_key, withdraw_key)

    @patch('core.payment_services.IotecPayGateway.collect_mobile_money')
    def test_repeated_deposit_submission_does_not_repeat_provider_request(self, collect):
        collect.return_value = PaymentGatewayResult(
            provider_reference='provider-pending-001',
            status='Pending',
        )
        self.profile.daily_deposit_limit = Decimal('10000000.00')
        self.profile.save(update_fields=['daily_deposit_limit'])
        post_data = {
            'action': 'deposit',
            'deposit-idempotency_key': 'a' * 32,
            'deposit-amount': '500',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '256',
            'deposit-phone_number': '256700000000',
        }

        first_response = self._submit_wallet_post(post_data)
        self.assertEqual(first_response.status_code, 302)
        second_response = self._submit_wallet_post(post_data)
        self.assertEqual(second_response.status_code, 302)

        payment = PaymentRequest.objects.get(user=self.user, payment_type='deposit')
        self.assertEqual(payment.idempotency_key, 'a' * 32)
        self.assertEqual(PaymentRequest.objects.filter(user=self.user, payment_type='deposit').count(), 1)
        collect.assert_called_once()

    @patch('core.payment_services.IotecPayGateway.disburse_mobile_money')
    def test_repeated_withdrawal_submission_holds_balance_only_once(self, disburse):
        disburse.return_value = PaymentGatewayResult(
            provider_reference='provider-pending-002',
            status='Pending',
        )
        post_data = {
            'action': 'withdraw',
            'withdraw-idempotency_key': 'b' * 32,
            'withdraw-amount': '500',
            'withdraw-provider': 'airtel_money',
            'withdraw-phone_number': self.profile.phone_number,
        }

        self._submit_wallet_post(post_data)
        self._submit_wallet_post(post_data)

        payment = PaymentRequest.objects.get(user=self.user, payment_type='withdraw')
        self.profile.refresh_from_db()
        self.assertEqual(payment.idempotency_key, 'b' * 32)
        self.assertEqual(PaymentRequest.objects.filter(user=self.user, payment_type='withdraw').count(), 1)
        self.assertEqual(self.profile.balance, Decimal('500.00'))
        disburse.assert_called_once()

    def _submit_wallet_post(self, post_data):
        request = RequestFactory().post('/wallet/', data=post_data)
        request.user = self.user
        request.session = self.client.session
        request._messages = FallbackStorage(request)
        return wallet(request)

    def test_deposit_form_accepts_amounts_up_to_ten_million(self):
        form = WalletForm(data={
            'deposit-amount': '10000000',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '256',
            'deposit-phone_number': '700000000',
        }, prefix='deposit')

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['amount'], Decimal('10000000.00'))
        self.assertEqual(form.cleaned_data['phone_number'], '256700000000')

        over_limit = WalletForm(data={
            'deposit-amount': '10000000.01',
            'deposit-provider': 'mtn_momo',
            'deposit-country_code': '256',
            'deposit-phone_number': '700000000',
        }, prefix='deposit')
        self.assertFalse(over_limit.is_valid())
        self.assertIn('amount', over_limit.errors)

    @patch(
        'core.payment_services.IotecPayGateway.collect_mobile_money',
        side_effect=PaymentGatewayError('gateway timeout', retryable=True),
    )
    def test_ten_million_deposit_is_accepted_when_daily_limit_allows_it(self, collect):
        self.profile.daily_deposit_limit = Decimal('10000000.00')
        self.profile.save(update_fields=['daily_deposit_limit'])

        payment, result, error = initiate_deposit(
            user=self.user,
            amount=Decimal('10000000.00'),
            provider='mtn_momo',
            phone_number='256700000000',
        )

        self.assertIsNone(result)
        self.assertEqual(error, 'gateway timeout')
        self.assertIsNotNone(payment)
        self.assertEqual(payment.amount, Decimal('10000000.00'))
        self.assertEqual(payment.phone_number, '256700000000')
        collect.assert_called_once()

    @patch(
        'core.payment_services.IotecPayGateway.collect_mobile_money',
        side_effect=PaymentGatewayError('gateway timeout', retryable=True),
    )
    def test_deposit_timeout_stays_pending_without_crediting_balance(self, collect):
        response = self.client.post('/wallet/', {
            'action': 'deposit',
            'deposit-amount': '500',
            'deposit-provider': 'mtn_momo',
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
        self.profile.daily_deposit_limit = Decimal('500.00')
        self.profile.save(update_fields=['daily_deposit_limit'])

        allowed, reason = can_deposit(self.profile, Decimal('5000.00'))

        self.assertFalse(allowed)
        self.assertIn('Daily deposit limit', reason)

    def test_ten_million_daily_limit_matches_maximum_single_deposit(self):
        self.profile.daily_deposit_limit = Decimal('10000000.00')
        self.profile.save(update_fields=['daily_deposit_limit'])

        allowed, reason = can_deposit(self.profile, Decimal('10000000.00'))

        self.assertTrue(allowed, reason)

    def test_withdrawal_creates_pending_payment_and_holds_balance(self):
        response = self.client.post('/wallet/', {
            'action': 'withdraw',
            'withdraw-amount': '500',
            'withdraw-provider': 'airtel_money',
            'withdraw-phone_number': '256750000000',
        })

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        payment = PaymentRequest.objects.get(user=self.user, payment_type='withdraw')
        txn = Transaction.objects.get(reference=payment.external_reference)

        self.assertEqual(self.profile.balance, Decimal('500.00'))
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(payment.phone_number, self.profile.phone_number)
        self.assertEqual(txn.status, 'pending')

    def test_withdrawal_service_rejects_any_number_other_than_the_registered_phone(self):
        payment, result, error = initiate_withdrawal(
            user=self.user,
            amount=Decimal('500.00'),
            provider='airtel_money',
            phone_number='256700000000',
        )

        self.assertIsNone(payment)
        self.assertIsNone(result)
        self.assertIn('registered on your account', error)
        self.assertFalse(PaymentRequest.objects.filter(user=self.user, payment_type='withdraw').exists())

    def test_withdrawal_form_ignores_a_replaced_phone_number(self):
        response = self.client.post('/wallet/', {
            'action': 'withdraw',
            'withdraw-amount': '500',
            'withdraw-provider': 'airtel_money',
            'withdraw-phone_number': '256700000000',
        })

        self.assertEqual(response.status_code, 302)
        payment = PaymentRequest.objects.get(user=self.user, payment_type='withdraw')
        self.assertEqual(payment.phone_number, self.profile.phone_number)

    @patch(
        'core.payment_services.IotecPayGateway.disburse_mobile_money',
        side_effect=PaymentGatewayError('gateway timeout', retryable=True),
    )
    def test_withdrawal_timeout_keeps_hold_pending(self, disburse):
        response = self.client.post('/wallet/', {
            'action': 'withdraw',
            'withdraw-amount': '500',
            'withdraw-provider': 'airtel_money',
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
        self.profile = Profile.objects.create(
            user=self.user,
            balance=Decimal('1000.00'),
            phone_number='256750000000',
        )
        self.client.login(username='compliance-player', password='strong-test-pass')

    def test_profile_does_not_offer_kyc_submission(self):
        response = self.client.get('/profile/')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'KYC')

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
            'bet_data': json.dumps([10]),
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
            'bet_data': json.dumps([10]),
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

    def test_settings_accepts_daily_deposit_limit_through_ten_million(self):
        response = self.client.post('/game-limits/', {
            'action': 'limits',
            'country_code': '256',
            'phone_number': '700000000',
            'daily_deposit_limit': '10000000.00',
            'daily_loss_limit': '75000.00',
        })

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.daily_deposit_limit, Decimal('10000000.00'))
        self.assertEqual(self.profile.phone_number, '256750000000')

    def test_settings_accepts_minimum_daily_deposit_limit(self):
        response = self.client.post('/game-limits/', {
            'action': 'limits',
            'phone_number': '',
            'daily_deposit_limit': '500.00',
            'daily_loss_limit': '75000.00',
        })

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.daily_deposit_limit, Decimal('500.00'))

    def test_settings_rejects_daily_deposit_limits_outside_range(self):
        for value in ('499.99', '10000000.01'):
            with self.subTest(value=value):
                form = ProfileComplianceForm(data={
                    'country_code': '256',
                    'phone_number': '',
                    'daily_deposit_limit': value,
                    'daily_loss_limit': '75000.00',
                })
                self.assertFalse(form.is_valid())
                self.assertIn('daily_deposit_limit', form.errors)

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

    def test_flagged_chat_creates_risk_alert(self):
        response = self.client.post('/play/', {
            'action': 'chat',
            'message': 'This looks like fraud',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('Your message was not sent', response.json()['message'])
        self.assertFalse(ChatMessage.objects.filter(user=self.user).exists())
        self.assertTrue(RiskAlert.objects.filter(user=self.user, alert_type='chat_abuse_blocked').exists())

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
