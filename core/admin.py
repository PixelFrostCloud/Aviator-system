from django.contrib import admin
from django.utils import timezone
from .models import (
    ChatMessage,
    ChatReaction,
    ChatRain,
    ChatRainRecipient,
    FinancialReport,
    KYCSubmission,
    Profile,
    GameRound,
    GameSession,
    PaymentRequest,
    RiskAlert,
    SelfExclusion,
    Transaction,
    Room,
    RoomPlayer,
    AuditLog,
    RewardClaim,
    RewardEvent,
    RewardTransaction,
    RewardWallet,
)
from .reward_services import activate_event, stop_event


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'balance', 'created_at')
    search_fields = ('user__username', 'user__email')


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'transaction_type', 'amount', 'status', 'reference', 'created_at')
    list_filter = ('transaction_type', 'status', 'created_at')
    search_fields = ('user__username', 'description', 'reference')


@admin.register(PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = ('external_reference', 'user', 'payment_type', 'provider', 'amount', 'status', 'created_at', 'completed_at')
    list_filter = ('payment_type', 'provider', 'status', 'created_at')
    search_fields = ('external_reference', 'provider_reference', 'user__username', 'phone_number')
    readonly_fields = ('external_reference', 'provider_reference', 'created_at', 'updated_at', 'completed_at', 'metadata')
    actions = ('mark_completed', 'mark_failed')

    @admin.action(description='Mark selected payment requests completed')
    def mark_completed(self, request, queryset):
        for payment in queryset.filter(status__in=['pending', 'processing']):
            payment.status = 'completed'
            payment.completed_at = timezone.now()
            payment.save(update_fields=['status', 'completed_at', 'updated_at'])
            Transaction.objects.filter(reference=payment.external_reference, status='pending').update(status='completed')

    @admin.action(description='Mark selected payment requests failed and refund withdrawal holds')
    def mark_failed(self, request, queryset):
        for payment in queryset.filter(status__in=['pending', 'processing']):
            payment.status = 'failed'
            payment.failure_reason = payment.failure_reason or 'Marked failed by operator.'
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
            Transaction.objects.filter(reference=payment.external_reference, status='pending').update(status='failed')
            if payment.payment_type == 'withdraw':
                profile = Profile.objects.get(user=payment.user)
                profile.balance += payment.amount
                profile.save(update_fields=['balance'])
                Transaction.objects.create(
                    user=payment.user,
                    transaction_type='refund',
                    amount=payment.amount,
                    status='completed',
                    reference=payment.external_reference,
                    description='Withdrawal hold refunded after failed payment request',
                )


@admin.register(KYCSubmission)
class KYCSubmissionAdmin(admin.ModelAdmin):
    list_display = ('user', 'document_type', 'full_name', 'status', 'created_at', 'reviewed_at')
    list_filter = ('status', 'document_type', 'created_at')
    search_fields = ('user__username', 'full_name', 'document_number')
    actions = ('approve_tier_2', 'reject_selected')

    @admin.action(description='Approve selected KYC submissions as Tier 2')
    def approve_tier_2(self, request, queryset):
        for submission in queryset.filter(status='pending'):
            submission.status = 'approved'
            submission.reviewed_by = request.user
            submission.reviewed_at = timezone.now()
            submission.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
            profile = Profile.objects.get(user=submission.user)
            profile.kyc_status = 'tier_2'
            profile.save(update_fields=['kyc_status'])

    @admin.action(description='Reject selected KYC submissions')
    def reject_selected(self, request, queryset):
        for submission in queryset.filter(status='pending'):
            submission.status = 'rejected'
            submission.reviewed_by = request.user
            submission.reviewed_at = timezone.now()
            submission.rejection_reason = submission.rejection_reason or 'Rejected by operator.'
            submission.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'rejection_reason'])


@admin.register(SelfExclusion)
class SelfExclusionAdmin(admin.ModelAdmin):
    list_display = ('user', 'starts_at', 'ends_at', 'permanent', 'created_at')
    list_filter = ('permanent', 'created_at')
    search_fields = ('user__username', 'reason')


@admin.register(RiskAlert)
class RiskAlertAdmin(admin.ModelAdmin):
    list_display = ('user', 'alert_type', 'severity', 'status', 'created_at', 'resolved_at')
    list_filter = ('severity', 'status', 'alert_type', 'created_at')
    search_fields = ('user__username', 'description')
    actions = ('mark_reviewing', 'mark_resolved')

    @admin.action(description='Mark selected alerts reviewing')
    def mark_reviewing(self, request, queryset):
        queryset.filter(status='open').update(status='reviewing')

    @admin.action(description='Mark selected alerts resolved')
    def mark_resolved(self, request, queryset):
        queryset.exclude(status='resolved').update(status='resolved', resolved_at=timezone.now())


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ('user', 'message', 'flagged', 'created_at')
    list_filter = ('flagged', 'created_at')
    search_fields = ('user__username', 'message')


@admin.register(ChatReaction)
class ChatReactionAdmin(admin.ModelAdmin):
    list_display = ('message', 'user', 'reaction', 'created_at')
    list_filter = ('reaction', 'created_at')
    search_fields = ('message__message', 'user__username')


class ChatRainRecipientInline(admin.TabularInline):
    model = ChatRainRecipient
    extra = 0
    readonly_fields = ('user', 'amount', 'created_at')
    can_delete = False


@admin.register(ChatRain)
class ChatRainAdmin(admin.ModelAdmin):
    list_display = ('reference', 'sender', 'reward_event', 'total_amount', 'per_player_amount', 'recipient_count', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('reference', 'sender__username', 'reward_event__title', 'reward_event__uuid')
    readonly_fields = ('reference', 'sender', 'message', 'reward_event', 'total_amount', 'per_player_amount', 'recipient_count', 'created_at')
    inlines = (ChatRainRecipientInline,)


class RewardClaimInline(admin.TabularInline):
    model = RewardClaim
    extra = 0
    readonly_fields = ('uuid', 'user', 'amount_claimed', 'claim_time', 'ip_address', 'device_identifier', 'status', 'transaction_reference')
    can_delete = False


@admin.register(RewardEvent)
class RewardEventAdmin(admin.ModelAdmin):
    list_display = ('title', 'status', 'claim_strategy', 'total_reward_pool', 'remaining_reward_pool', 'maximum_claims', 'starts_at', 'ends_at')
    list_filter = ('status', 'claim_strategy', 'starts_at', 'ends_at')
    search_fields = ('title', 'description', 'uuid', 'created_by__username')
    readonly_fields = ('uuid', 'created_at', 'updated_at')
    actions = ('activate_selected', 'pause_selected', 'resume_selected', 'cancel_selected', 'complete_selected')
    inlines = (RewardClaimInline,)

    @admin.action(description='Activate selected reward events')
    def activate_selected(self, request, queryset):
        for event in queryset.exclude(status__in=['completed', 'cancelled']):
            activate_event(event, request.user)

    @admin.action(description='Pause selected reward events')
    def pause_selected(self, request, queryset):
        queryset.filter(status='active').update(status='paused', updated_at=timezone.now())

    @admin.action(description='Resume selected reward events')
    def resume_selected(self, request, queryset):
        for event in queryset.filter(status='paused'):
            activate_event(event, request.user)

    @admin.action(description='Cancel selected reward events')
    def cancel_selected(self, request, queryset):
        for event in queryset.exclude(status='completed'):
            stop_event(event, request.user, status='cancelled')

    @admin.action(description='Complete selected reward events')
    def complete_selected(self, request, queryset):
        for event in queryset.exclude(status='cancelled'):
            stop_event(event, request.user, status='completed')


@admin.register(RewardClaim)
class RewardClaimAdmin(admin.ModelAdmin):
    list_display = ('reward_event', 'user', 'amount_claimed', 'status', 'claim_time', 'transaction_reference')
    list_filter = ('status', 'claim_time', 'reward_event__status')
    search_fields = ('uuid', 'reward_event__title', 'user__username', 'transaction_reference', 'device_identifier', 'ip_address')
    readonly_fields = ('uuid', 'reward_event', 'user', 'amount_claimed', 'claim_time', 'transaction_reference', 'idempotency_key')


@admin.register(RewardWallet)
class RewardWalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'available_balance', 'locked_balance', 'total_rewards_received', 'total_rewards_claimed', 'updated_at')
    search_fields = ('user__username', 'uuid')
    readonly_fields = ('uuid', 'updated_at')


@admin.register(RewardTransaction)
class RewardTransactionAdmin(admin.ModelAdmin):
    list_display = ('reference', 'wallet', 'event', 'transaction_type', 'amount', 'balance_before', 'balance_after', 'created_at')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('reference', 'wallet__user__username', 'event__title', 'uuid')
    readonly_fields = ('uuid', 'wallet', 'event', 'amount', 'transaction_type', 'balance_before', 'balance_after', 'reference', 'created_at')


@admin.register(FinancialReport)
class FinancialReportAdmin(admin.ModelAdmin):
    list_display = ('report_type', 'period_start', 'period_end', 'gross_gaming_revenue', 'ura_ggr_tax', 'created_at')
    list_filter = ('report_type', 'created_at')
    search_fields = ('period_start', 'period_end')


@admin.register(GameRound)
class GameRoundAdmin(admin.ModelAdmin):
    list_display = ('public_id', 'crash_point', 'status', 'server_seed_hash', 'started_at', 'revealed_at')
    list_filter = ('status', 'started_at', 'revealed_at')
    search_fields = ('public_id', 'server_seed_hash', 'client_seed')
    readonly_fields = ('public_id', 'server_seed_hash', 'server_seed', 'client_seed', 'crash_point', 'started_at', 'crashed_at', 'revealed_at')
    exclude = ('server_seed_private',)


@admin.register(GameSession)
class GameSessionAdmin(admin.ModelAdmin):
    list_display = ('user', 'round', 'stake', 'crash_point', 'cashed_out_at', 'won_amount', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('user__username', 'round__public_id')


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'host', 'status', 'created_at', 'started_at')
    list_filter = ('status', 'created_at')
    search_fields = ('code', 'name', 'host__username')


@admin.register(RoomPlayer)
class RoomPlayerAdmin(admin.ModelAdmin):
    list_display = ('room', 'user', 'stake', 'cashed_out_at', 'won_amount', 'active')
    list_filter = ('active', 'room__status')
    search_fields = ('user__username', 'room__code')


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('action_type', 'user', 'ip_address', 'created_at')
    list_filter = ('action_type', 'created_at')
    search_fields = ('user__username', 'ip_address', 'description')
    readonly_fields = ('created_at', 'data')
