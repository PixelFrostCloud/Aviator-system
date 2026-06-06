try:
    from celery import shared_task
except ImportError:  # Local development fallback when Celery is not installed.
    def shared_task(func=None, **_kwargs):
        def decorator(inner):
            inner.delay = inner
            return inner
        return decorator(func) if func else decorator

from django.utils import timezone

from .models import RewardEvent
from .reward_services import expire_due_events, reward_statistics


@shared_task
def activate_scheduled_reward_events():
    return expire_due_events()


@shared_task
def deactivate_expired_reward_events():
    now = timezone.now()
    return RewardEvent.objects.filter(status='active', ends_at__lte=now).update(status='completed', updated_at=now)


@shared_task
def clean_inactive_reward_sessions():
    # Redis/cache expiry handles participant TTL automatically in production.
    return {'cleaned': 0}


@shared_task
def expire_pending_rewards():
    return RewardEvent.objects.filter(status__in=['draft', 'scheduled'], ends_at__lte=timezone.now()).update(status='completed', updated_at=timezone.now())


@shared_task
def generate_reward_report(event_id=None):
    event = RewardEvent.objects.filter(uuid=event_id).first() if event_id else None
    return reward_statistics(event)


@shared_task
def archive_completed_reward_events():
    # Placeholder hook for cold-storage archival jobs.
    return {'archived': RewardEvent.objects.filter(status='completed').count()}


@shared_task
def recalculate_reward_analytics():
    return reward_statistics()
