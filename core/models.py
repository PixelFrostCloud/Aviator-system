import random
import string
import uuid
from decimal import Decimal
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.utils import timezone


def generate_room_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


class Profile(models.Model):
    KYC_STATUS_CHOICES = [
        ('none', 'Not Started'),
        ('tier_1', 'Tier 1 - Phone Verified'),
        ('tier_2', 'Tier 2 - Basic KYC'),
        ('tier_3', 'Tier 3 - Full KYC'),
        ('rejected', 'Rejected'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=1000)
    currency = models.CharField(max_length=3, default='UGX')
    phone_number = models.CharField(max_length=20, blank=True)
    kyc_status = models.CharField(max_length=12, choices=KYC_STATUS_CHOICES, default='none')
    daily_deposit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=500000)
    daily_loss_limit = models.DecimalField(max_digits=12, decimal_places=2, default=200000)
    self_excluded_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} Profile'

    @property
    def total_transactions(self):
        return self.transactions.count()


class GamePresence(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='game_presences')
    presence_key = models.CharField(max_length=32)
    last_seen = models.DateTimeField(db_index=True)

    class Meta:
        unique_together = ('user', 'presence_key')

    def __str__(self):
        return f'{self.user.username} in game'


class Transaction(models.Model):
    TRANSACTION_TYPES = [
        ('deposit', 'Deposit'),
        ('withdraw', 'Withdraw'),
        ('bet', 'Bet Debit'),
        ('win', 'Win Credit'),
        ('refund', 'Refund'),
        ('bonus', 'Bonus'),
    ]

    STATUS_CHOICES = [
        ('completed', 'Completed'),
        ('pending', 'Pending'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='completed')
    reference = models.CharField(max_length=80, blank=True, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username} {self.transaction_type} ${self.amount}'

    @property
    def is_deposit(self):
        return self.transaction_type == 'deposit'


class PaymentRequest(models.Model):
    PAYMENT_TYPES = [
        ('deposit', 'Deposit'),
        ('withdraw', 'Withdraw'),
    ]

    PROVIDERS = [
        ('mtn_momo', 'MTN Mobile Money'),
        ('airtel_money', 'Airtel Money'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payment_requests')
    payment_type = models.CharField(max_length=10, choices=PAYMENT_TYPES)
    provider = models.CharField(max_length=20, choices=PROVIDERS)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    phone_number = models.CharField(max_length=20, blank=True)
    external_reference = models.CharField(max_length=80, unique=True, db_index=True)
    provider_reference = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='pending')
    failure_reason = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['provider', 'status']),
            models.Index(fields=['payment_type', 'status', '-created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(provider__in=['mtn_momo', 'airtel_money']),
                name='paymentrequest_mobile_money_only',
            ),
        ]

    @property
    def is_completed(self):
        return self.status == 'completed'

    def __str__(self):
        return f'{self.get_provider_display()} {self.payment_type} {self.amount}'


class KYCSubmission(models.Model):
    DOCUMENT_TYPES = [
        ('national_id', 'National ID'),
        ('passport', 'Passport'),
        ('driver_license', 'Driver License'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='kyc_submissions')
    document_type = models.CharField(max_length=20, choices=DOCUMENT_TYPES)
    document_number = models.CharField(max_length=80)
    full_name = models.CharField(max_length=160)
    date_of_birth = models.DateField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='pending')
    rejection_reason = models.CharField(max_length=255, blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_kyc_submissions')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username} KYC {self.status}'


class SelfExclusion(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='self_exclusions')
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    permanent = models.BooleanField(default=False)
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def is_active(self):
        return self.permanent or self.ends_at is None or self.ends_at > timezone.now()

    def __str__(self):
        return f'{self.user.username} self-exclusion'


class RiskAlert(models.Model):
    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]

    STATUS_CHOICES = [
        ('open', 'Open'),
        ('reviewing', 'Reviewing'),
        ('resolved', 'Resolved'),
        ('dismissed', 'Dismissed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='risk_alerts')
    alert_type = models.CharField(max_length=50)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='medium')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='open')
    description = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'severity', '-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f'{self.alert_type} ({self.severity})'


class ChatMessage(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_messages')
    message = models.CharField(max_length=280)
    flagged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username}: {self.message[:40]}'


class ChatReaction(models.Model):
    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_reactions')
    reaction = models.CharField(max_length=16, default='heart')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('message', 'user', 'reaction')

    def __str__(self):
        return f'{self.user.username} reacted to message {self.message_id}'


class ChatRain(models.Model):
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_chat_rains')
    message = models.ForeignKey(ChatMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name='rain_events')
    reward_event = models.ForeignKey('RewardEvent', on_delete=models.SET_NULL, null=True, blank=True, related_name='chat_rains')
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    per_player_amount = models.DecimalField(max_digits=12, decimal_places=2)
    recipient_count = models.PositiveSmallIntegerField()
    reference = models.CharField(max_length=80, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Rain {self.reference} by {self.sender.username}'


class ChatRainRecipient(models.Model):
    rain = models.ForeignKey(ChatRain, on_delete=models.CASCADE, related_name='recipients')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_chat_rains')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('rain', 'user')

    def __str__(self):
        return f'{self.user.username} received {self.amount} from {self.rain.reference}'


class RewardEvent(models.Model):
    CLAIM_STRATEGIES = [
        ('equal', 'Equal'),
        ('random', 'Random'),
        ('weighted', 'Weighted'),
    ]

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    total_reward_pool = models.DecimalField(max_digits=14, decimal_places=2)
    remaining_reward_pool = models.DecimalField(max_digits=14, decimal_places=2)
    maximum_claims = models.PositiveIntegerField()
    reward_per_claim = models.DecimalField(max_digits=12, decimal_places=2)
    minimum_random_reward = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.01'))
    maximum_random_reward = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    claim_strategy = models.CharField(max_length=12, choices=CLAIM_STRATEGIES, default='equal')
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_reward_events')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'starts_at', 'ends_at']),
            models.Index(fields=['created_by', '-created_at']),
        ]

    @property
    def claim_count(self):
        return self.claims.filter(status='approved').count()

    @property
    def remaining_claims(self):
        return max(self.maximum_claims - self.claim_count, 0)

    @property
    def is_claimable(self):
        now = timezone.now()
        return self.status == 'active' and self.starts_at <= now <= self.ends_at and self.remaining_reward_pool > 0 and self.remaining_claims > 0

    def __str__(self):
        return f'{self.title} ({self.status})'


class RewardWallet(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='reward_wallet')
    available_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    locked_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_rewards_received = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_rewards_claimed = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    activity_score = models.PositiveIntegerField(default=0)
    loyalty_points = models.PositiveIntegerField(default=0)
    admin_weight_multiplier = models.DecimalField(max_digits=8, decimal_places=2, default=1)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user.username} reward wallet'


class RewardClaim(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('failed', 'Failed'),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    reward_event = models.ForeignKey(RewardEvent, on_delete=models.CASCADE, related_name='claims')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reward_claims')
    amount_claimed = models.DecimalField(max_digits=12, decimal_places=2)
    claim_time = models.DateTimeField(default=timezone.now)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_identifier = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='approved')
    transaction_reference = models.CharField(max_length=96, db_index=True)
    idempotency_key = models.CharField(max_length=96, blank=True, db_index=True)

    class Meta:
        ordering = ['-claim_time']
        unique_together = ('user', 'reward_event')
        indexes = [
            models.Index(fields=['reward_event', 'status', '-claim_time']),
            models.Index(fields=['user', '-claim_time']),
            models.Index(fields=['idempotency_key']),
        ]

    def __str__(self):
        return f'{self.user.username} claimed {self.amount_claimed} from {self.reward_event.title}'


class RewardTransaction(models.Model):
    TRANSACTION_TYPES = [
        ('credit', 'Credit'),
        ('debit', 'Debit'),
        ('lock', 'Lock'),
        ('unlock', 'Unlock'),
        ('adjustment', 'Adjustment'),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    wallet = models.ForeignKey(RewardWallet, on_delete=models.CASCADE, related_name='transactions')
    event = models.ForeignKey(RewardEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=12, choices=TRANSACTION_TYPES)
    balance_before = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    reference = models.CharField(max_length=96, unique=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['wallet', '-created_at']),
            models.Index(fields=['event', '-created_at']),
            models.Index(fields=['transaction_type', '-created_at']),
        ]

    def __str__(self):
        return f'{self.transaction_type} {self.amount} ({self.reference})'


class FinancialReport(models.Model):
    REPORT_TYPES = [
        ('daily', 'Daily'),
        ('monthly', 'Monthly'),
    ]

    report_type = models.CharField(max_length=10, choices=REPORT_TYPES)
    period_start = models.DateField()
    period_end = models.DateField()
    total_deposits = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_withdrawals = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_bets = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_wins = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gross_gaming_revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    ura_ggr_tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    generated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_start']
        unique_together = ('report_type', 'period_start', 'period_end')

    def __str__(self):
        return f'{self.report_type} report {self.period_start} - {self.period_end}'


class GameRound(models.Model):
    STATUS_CHOICES = [
        ('betting', 'Betting'),
        ('flying', 'Flying'),
        ('crashed', 'Crashed'),
        ('void', 'Void'),
    ]

    public_id = models.CharField(max_length=24, unique=True, db_index=True)
    server_seed_hash = models.CharField(max_length=64)
    server_seed_private = models.CharField(max_length=128, blank=True)
    server_seed = models.CharField(max_length=128, blank=True)
    client_seed = models.CharField(max_length=128)
    crash_point = models.DecimalField(max_digits=8, decimal_places=2)
    house_edge = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal('0.0100'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='flying')
    started_at = models.DateTimeField(default=timezone.now)
    crashed_at = models.DateTimeField(null=True, blank=True)
    revealed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['-started_at']),
            models.Index(fields=['status', '-started_at']),
        ]

    @property
    def is_revealed(self):
        return bool(self.server_seed)

    def __str__(self):
        return f'Round {self.public_id} ({self.crash_point}x)'


class GameSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    round = models.ForeignKey(GameRound, on_delete=models.SET_NULL, null=True, blank=True, related_name='bets')
    stake = models.DecimalField(max_digits=8, decimal_places=2)
    crash_point = models.DecimalField(max_digits=8, decimal_places=2)
    cashed_out_at = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    won_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=timezone.now)

    @property
    def is_winner(self):
        return self.cashed_out_at is not None and self.cashed_out_at < self.crash_point

    def __str__(self):
        return f'GameSession({self.user.username}, stake={self.stake}, crash={self.crash_point})'


class Room(models.Model):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('running', 'Running'),
        ('ended', 'Ended'),
    ]

    code = models.CharField(max_length=8, unique=True, editable=False)
    name = models.CharField(max_length=100)
    host = models.ForeignKey(User, on_delete=models.CASCADE, related_name='hosted_rooms')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='waiting')
    crash_point = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    max_players = models.PositiveSmallIntegerField(default=8)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.code:
            code = generate_room_code()
            while Room.objects.filter(code=code).exists():
                code = generate_room_code()
            self.code = code
        super().save(*args, **kwargs)

    @property
    def participants(self):
        return self.players.select_related('user')

    @property
    def is_open(self):
        return self.status == 'waiting'

    @property
    def active_players(self):
        return self.players.filter(cashed_out_at__isnull=True)

    def __str__(self):
        return f'Room {self.code} ({self.name})'


class RoomPlayer(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='players')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='room_entries')
    stake = models.DecimalField(max_digits=8, decimal_places=2)
    joined_at = models.DateTimeField(auto_now_add=True)
    cashed_out_at = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    won_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('room', 'user')
        ordering = ['joined_at']

    @property
    def is_winner(self):
        return self.cashed_out_at is not None and self.room.crash_point is not None and self.cashed_out_at < self.room.crash_point

    @property
    def has_cashed_out(self):
        return self.cashed_out_at is not None

    def __str__(self):
        return f'{self.user.username} in {self.room.code}'


class AuditLog(models.Model):
    ACTION_TYPES = [
        ('user_register', 'User Registration'),
        ('user_login', 'User Login'),
        ('user_logout', 'User Logout'),
        ('deposit', 'Deposit'),
        ('withdraw', 'Withdrawal'),
        ('kyc_submit', 'KYC Submitted'),
        ('kyc_review', 'KYC Reviewed'),
        ('self_exclude', 'Self Exclusion'),
        ('risk_alert', 'Risk Alert'),
        ('chat_message', 'Chat Message'),
        ('financial_report', 'Financial Report'),
        ('game_start', 'Game Started'),
        ('game_cashout', 'Game Cashout'),
        ('game_crash', 'Game Crashed'),
        ('round_commit', 'Round Commitment'),
        ('round_reveal', 'Round Reveal'),
        ('room_create', 'Room Created'),
        ('room_join', 'Room Joined'),
        ('room_start', 'Room Started'),
        ('room_cashout', 'Room Cashout'),
        ('admin_action', 'Admin Action'),
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    action_type = models.CharField(max_length=50, choices=ACTION_TYPES)
    description = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['action_type', '-created_at']),
        ]

    def __str__(self):
        return f'{self.action_type} - {self.user} ({self.created_at})'
