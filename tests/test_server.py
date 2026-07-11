import asyncio
from http import HTTPStatus
import json
import pathlib
import sys
import unittest
from websockets.legacy.client import connect
from websockets.legacy.server import serve
from websockets.exceptions import InvalidStatusCode

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from server import (
    connection,
    consume_message_rate,
    parse_origins,
    process_message,
    process_playing,
    process_request,
    server_config,
    server_status,
    set_identity
)


class RecordingWebSocket:
    def __init__(self):
        self.closed = False
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class MultiplayerServerTests(unittest.TestCase):
    def setUp(self):
        server_status['waiting'].clear()
        server_status['users'].clear()
        server_status['invites'].clear()
        server_config['max_connections'] = 100
        server_config['max_messages_per_second'] = 120

    def test_origins_must_be_explicit(self):
        self.assertEqual(parse_origins('https://a.example, https://b.example'), ['https://a.example', 'https://b.example'])
        with self.assertRaises(ValueError):
            parse_origins('*')
        with self.assertRaises(ValueError):
            parse_origins('')

    def test_health_response_has_no_room_data(self):
        status, headers, body = asyncio.run(process_request('/health', {}))
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(dict(headers)['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(body), {
            'status': 'ok',
            'connections': 0,
            'max_connections': 100,
            'accepting_connections': True
        })

    def test_unknown_http_path_is_not_found(self):
        status, _headers, _body = asyncio.run(process_request('/private', {}))
        self.assertEqual(status, HTTPStatus.NOT_FOUND)

    def test_two_clients_can_create_and_join_an_invite_room(self):
        asyncio.run(self._invite_room())

    def test_unlisted_origin_is_rejected(self):
        asyncio.run(self._reject_unlisted_origin())

    def test_hard_capacity_rejects_new_websocket_handshakes(self):
        asyncio.run(self._reject_at_capacity())

    def test_message_flood_limit_is_per_connection(self):
        user = {}
        self.assertTrue(all(consume_message_rate(user, now=1) for _ in range(120)))
        self.assertFalse(consume_message_rate(user, now=1))
        self.assertTrue(consume_message_rate(user, now=2.1))

    def test_identity_fields_are_bounded_and_filtered(self):
        user = {}
        set_identity(user, {
            'name': 'x' * 100,
            'don': {'body_fill': '#123456', 'face_fill': '#abcdef', 'unexpected': 'ignored'}
        })
        self.assertEqual(user['name'], 'x' * 25)
        self.assertEqual(user['don'], {'body_fill': '#123456', 'face_fill': '#abcdef'})

    def test_play_messages_have_monotonic_relay_sequences(self):
        async def scenario():
            first_ws = RecordingWebSocket()
            second_ws = RecordingWebSocket()
            first = {'ws': first_ws, 'action': 'playing', 'relay_seq': 0}
            second = {'ws': second_ws, 'action': 'playing', 'relay_seq': 0}
            first['other_user'] = second
            second['other_user'] = first
            await process_playing(first, 'note', {'score': 450})
            await process_playing(first, 'branch', 'master')
            self.assertEqual([message['seq'] for message in second_ws.messages], [1, 2])
            self.assertTrue(all('server_time' in message for message in second_ws.messages))

        asyncio.run(scenario())

    def test_latency_probe_works_in_every_room_state(self):
        async def scenario():
            websocket = RecordingWebSocket()
            user = {'ws': websocket, 'action': 'songsel'}
            await process_message(user, json.dumps({
                'type': 'syncping',
                'value': {'sentAt': 1234}
            }))
            self.assertEqual(websocket.messages[0]['type'], 'syncpong')
            self.assertEqual(websocket.messages[0]['value']['sentAt'], 1234)
            self.assertIn('server_time', websocket.messages[0])

        asyncio.run(scenario())

    def test_ready_messages_are_idempotent(self):
        async def scenario():
            first_ws = RecordingWebSocket()
            second_ws = RecordingWebSocket()
            first = {'ws': first_ws, 'action': 'loading'}
            second = {'ws': second_ws, 'action': 'loading'}
            first['other_user'] = second
            second['other_user'] = first

            ready = json.dumps({'type': 'gamestart'})
            await process_message(first, ready)
            await process_message(first, ready)
            self.assertEqual(first['action'], 'loaded')
            self.assertEqual(second['action'], 'loading')

            await process_message(second, ready)
            self.assertEqual(first['action'], 'playing')
            self.assertEqual(second['action'], 'playing')
            self.assertEqual(first_ws.messages[-1]['type'], 'gamestart')
            self.assertEqual(second_ws.messages[-1]['type'], 'gamestart')

        asyncio.run(scenario())

    async def _invite_room(self):
        listener = await serve(
            connection,
            '127.0.0.1',
            0,
            origins=['https://taiko.example'],
            process_request=process_request
        )
        port = listener.sockets[0].getsockname()[1]
        uri = 'ws://127.0.0.1:{}'.format(port)
        try:
            async with connect(uri, origin='https://taiko.example') as host:
                self.assertEqual(json.loads(await host.recv())['type'], 'users')
                await host.send(json.dumps({'type': 'invite', 'value': {'id': None, 'name': 'Host'}}))
                invitation = json.loads(await host.recv())
                self.assertEqual(invitation['type'], 'invite')
                code = invitation['value']

                async with connect(uri, origin='https://taiko.example') as guest:
                    self.assertEqual(json.loads(await guest.recv())['type'], 'users')
                    await guest.send(json.dumps({'type': 'invite', 'value': {'id': code, 'name': 'Guest'}}))
                    self.assertEqual((await self._next_type(host, 'session'))['value']['player'], 1)
                    self.assertEqual((await self._next_type(guest, 'session'))['value']['player'], 2)
        finally:
            listener.close()
            await listener.wait_closed()

    async def _next_type(self, websocket, expected):
        for _ in range(4):
            message = json.loads(await asyncio.wait_for(websocket.recv(), 1))
            if message.get('type') == expected:
                return message
        self.fail('Expected {} message'.format(expected))

    async def _reject_unlisted_origin(self):
        listener = await serve(connection, '127.0.0.1', 0, origins=['https://taiko.example'])
        port = listener.sockets[0].getsockname()[1]
        try:
            with self.assertRaises(InvalidStatusCode):
                await connect('ws://127.0.0.1:{}'.format(port), origin='https://other.example')
        finally:
            listener.close()
            await listener.wait_closed()

    async def _reject_at_capacity(self):
        server_config['max_connections'] = 1
        listener = await serve(connection, '127.0.0.1', 0, origins=['https://taiko.example'], process_request=process_request)
        port = listener.sockets[0].getsockname()[1]
        uri = 'ws://127.0.0.1:{}'.format(port)
        try:
            async with connect(uri, origin='https://taiko.example') as first:
                self.assertEqual(json.loads(await first.recv())['type'], 'users')
                with self.assertRaises(InvalidStatusCode):
                    await connect(uri, origin='https://taiko.example')
        finally:
            listener.close()
            await listener.wait_closed()


if __name__ == '__main__':
    unittest.main()
