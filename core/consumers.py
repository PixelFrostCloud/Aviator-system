import json
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone
from .models import Room, RoomPlayer, AuditLog


class RoomSyncConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_id = self.scope['url_route']['kwargs']['room_id']
        self.room_group_name = f'room_{self.room_id}'

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            event_type = data.get('type')

            if event_type == 'room_update':
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'room_update',
                        'message': data.get('message'),
                        'crash_point': data.get('crash_point'),
                        'status': data.get('status'),
                    }
                )
            elif event_type == 'player_cashout':
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'player_cashout',
                        'username': data.get('username'),
                        'multiplier': data.get('multiplier'),
                        'won_amount': data.get('won_amount'),
                        'is_winner': data.get('is_winner'),
                    }
                )
            elif event_type == 'player_joined':
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'player_joined',
                        'username': data.get('username'),
                        'player_count': data.get('player_count'),
                    }
                )
        except json.JSONDecodeError:
            pass

    async def room_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'room_update',
            'message': event['message'],
            'crash_point': event.get('crash_point'),
            'status': event.get('status'),
        }))

    async def player_cashout(self, event):
        await self.send(text_data=json.dumps({
            'type': 'player_cashout',
            'username': event['username'],
            'multiplier': event['multiplier'],
            'won_amount': event['won_amount'],
            'is_winner': event['is_winner'],
        }))

    async def player_joined(self, event):
        await self.send(text_data=json.dumps({
            'type': 'player_joined',
            'username': event['username'],
            'player_count': event['player_count'],
        }))
