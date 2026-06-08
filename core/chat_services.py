from .models import ChatMessage


CHAT_MESSAGE_PRUNE_THRESHOLD = 100
CHAT_MESSAGE_PRUNE_BATCH = 70


def prune_chat_messages_if_needed():
    """Delete the oldest chat messages once the chat history reaches the limit."""
    total_messages = ChatMessage.objects.count()
    if total_messages < CHAT_MESSAGE_PRUNE_THRESHOLD:
        return 0

    stale_ids = list(
        ChatMessage.objects.order_by('created_at', 'id')
        .values_list('id', flat=True)[:CHAT_MESSAGE_PRUNE_BATCH]
    )
    if not stale_ids:
        return 0

    deleted, _ = ChatMessage.objects.filter(id__in=stale_ids).delete()
    return deleted
