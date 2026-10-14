import ipaddress
import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from .abuse import authorize_reward_socket, authorize_room_socket, record_activity
from .reward_services import REWARD_GROUP, mark_participant


MAX_MESSAGE_BYTES = 4096


def _scope_ip(scope):
    client = scope.get('client')
    if not client:
        return None
    try:
        return str(ipaddress.ip_address(client[0]))
    except (TypeError, ValueError):
        return None


@database_sync_to_async
def _authorize_room(room_id, user, ip_address):
    return authorize_room_socket(room_id, user, ip_address)


@database_sync_to_async
def _authorize_rewards(user, ip_address):
    return authorize_reward_socket(user, ip_address)


@database_sync_to_async
def _record_socket_event(event, user, ip_address, socket_name):
    return record_activity(
        event,
        user=user,
        ip_address=ip_address,
        details={'socket': socket_name},
    )


@database_sync_to_async
def _mark_reward_participant(user, channel_name, session_key):
    mark_participant(user, channel_name, session_key)


class ValidatedWebSocketConsumer(AsyncWebsocketConsumer):
    socket_name = 'unknown'

    async def _reject_message(self, reason):
        if not getattr(self, 'invalid_message_recorded', False):
            self.invalid_message_recorded = True
            await _record_socket_event(
                'invalid_websocket_message',
                self.scope.get('user'),
                self.peer_ip,
                self.socket_name,
            )
        await self.close(code=1008, reason=reason)

    async def _parse_heartbeat(self, text_data, bytes_data):
        if bytes_data is not None or text_data is None:
            await self._reject_message('Only JSON heartbeat messages are accepted.')
            return False
        if len(text_data.encode('utf-8')) > MAX_MESSAGE_BYTES:
            await self._reject_message('Message is too large.')
            return False
        try:
            payload = json.loads(text_data)
        except (TypeError, json.JSONDecodeError):
            await self._reject_message('Invalid JSON message.')
            return False
        if not isinstance(payload, dict) or payload != {'type': 'heartbeat'}:
            await self._reject_message('Unsupported message.')
            return False
        return True


class RoomSyncConsumer(ValidatedWebSocketConsumer):
    socket_name = 'room'

    async def connect(self):
        self.invalid_message_recorded = False
        self.peer_ip = _scope_ip(self.scope)
        self.room_id = self.scope['url_route']['kwargs']['room_id']
        self.room_group_name = f'room_{self.room_id}'
        user = self.scope.get('user')
        if not await _authorize_room(self.room_id, user, self.peer_ip):
            await self.close(code=4403)
            return
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if await self._parse_heartbeat(text_data, bytes_data):
            await self.send(text_data=json.dumps({'type': 'heartbeat'}))


class RewardConsumer(ValidatedWebSocketConsumer):
    socket_name = 'rewards'

    async def connect(self):
        self.invalid_message_recorded = False
        self.peer_ip = _scope_ip(self.scope)
        self.user = self.scope.get('user')
        if not await _authorize_rewards(self.user, self.peer_ip):
            await self.close(code=4403)
            return
        await self.channel_layer.group_add(REWARD_GROUP, self.channel_name)
        await self.accept()
        session = self.scope.get('session')
        await _mark_reward_participant(
            self.user,
            self.channel_name,
            session.session_key if session else '',
        )

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(REWARD_GROUP, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not await self._parse_heartbeat(text_data, bytes_data):
            return
        session = self.scope.get('session')
        await _mark_reward_participant(
            self.user,
            self.channel_name,
            session.session_key if session else '',
        )
        await self.send(text_data=json.dumps({
            'type': 'participant_joined',
            'user_id': self.user.id,
            'last_activity': timezone.now().isoformat(),
        }))

    async def reward_event(self, event):
        await self.send(text_data=json.dumps({
            'type': event.get('event'),
            **event.get('payload', {}),
        }))
