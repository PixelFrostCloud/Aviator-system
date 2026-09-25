import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import ChatMessage, ChatReaction, ChatRain, ChatRainRecipient, FinancialReport, GameRound, KYCSubmission, PaymentRequest, Profile, RewardClaim, RewardEvent, RewardTransaction, RewardWallet, RiskAlert, Room, RoomPlayer, SelfExclusion, Transaction
from .payment_gateway import IotecPayGateway
from .reward_services import claim_reward
from .utils import calculate_crash_point, can_deposit, generate_financial_report, hash_server_seed, verify_game_round


class AuthenticationTests(TestCase):
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


class IotecGatewayRequestTests(TestCase):
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


class PlayEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='player',
            email='player@example.com',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(user=self.user, balance=Decimal('1000.00'))
        self.client.login(username='player', password='strong-test-pass')

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

    def test_chat_rain_debits_sender_and_credits_recent_chat_player(self):
        recipient = User.objects.create_user(
            username='rain-recipient',
            email='rain-recipient@example.com',
            password='strong-test-pass',
        )
        recipient_profile = Profile.objects.create(user=recipient, balance=Decimal('20.00'))
        ChatMessage.objects.create(user=recipient, message='ready for rain')

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

    def test_chat_rain_rejects_insufficient_wallet_balance(self):
        recipient = User.objects.create_user(
            username='rain-balance-recipient',
            email='rain-balance-recipient@example.com',
            password='strong-test-pass',
        )
        Profile.objects.create(user=recipient, balance=Decimal('20.00'))
        ChatMessage.objects.create(user=recipient, message='ready for rain')

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


class WalletComplianceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='wallet-player',
            email='wallet@example.com',
            password='strong-test-pass',
        )
        self.profile = Profile.objects.create(user=self.user, balance=Decimal('1000.00'))
        self.client.login(username='wallet-player', password='strong-test-pass')

    def test_deposit_creates_payment_request_and_credits_wallet(self):
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
        self.assertEqual(self.profile.kyc_status, 'tier_1')
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(Transaction.objects.get(reference=payment.external_reference).transaction_type, 'deposit')

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
            'withdraw-phone_number': '256750000000',
        })

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        payment = PaymentRequest.objects.get(user=self.user, payment_type='withdraw')
        txn = Transaction.objects.get(reference=payment.external_reference)

        self.assertEqual(self.profile.balance, Decimal('500.00'))
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(txn.status, 'pending')

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

    def test_kyc_submission_is_created_from_profile(self):
        response = self.client.post('/profile/', {
            'action': 'kyc',
            'document_type': 'national_id',
            'document_number': 'CM12345678',
            'full_name': 'Compliance Player',
            'date_of_birth': '1990-01-01',
        })

        self.assertEqual(response.status_code, 302)
        submission = KYCSubmission.objects.get(user=self.user)
        self.assertEqual(submission.status, 'pending')

    def test_self_exclusion_blocks_bets(self):
        response = self.client.post('/profile/', {
            'action': 'self_exclude',
            'duration': '72',
            'reason': 'Cooling off',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SelfExclusion.objects.filter(user=self.user).exists())

        play = self.client.post('/play/', {
            'action': 'start',
            'bet_data': json.dumps([10]),
        })

        self.assertEqual(play.status_code, 403)
        self.assertIn('self-excluded', play.json()['message'])

    def test_flagged_chat_creates_risk_alert(self):
        response = self.client.post('/play/', {
            'action': 'chat',
            'message': 'This looks like fraud',
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(ChatMessage.objects.get(user=self.user).flagged)
        self.assertTrue(RiskAlert.objects.filter(user=self.user, alert_type='flagged_chat').exists())

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
