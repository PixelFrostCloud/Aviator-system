import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import ChatMessage, FinancialReport, GameRound, KYCSubmission, PaymentRequest, Profile, RiskAlert, SelfExclusion, Transaction
from .utils import calculate_crash_point, can_deposit, generate_financial_report, hash_server_seed, verify_game_round


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
        game_round.started_at = timezone.now() - timedelta(seconds=float(game_round.crash_point) + 1)
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
            'withdraw-amount': '250',
            'withdraw-provider': 'airtel_money',
            'withdraw-phone_number': '256750000000',
        })

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        payment = PaymentRequest.objects.get(user=self.user, payment_type='withdraw')
        txn = Transaction.objects.get(reference=payment.external_reference)

        self.assertEqual(self.profile.balance, Decimal('750.00'))
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(txn.status, 'pending')


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
