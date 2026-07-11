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

from server import connection, parse_origins, process_request, server_config, server_status


class MultiplayerServerTests(unittest.TestCase):
    def setUp(self):
        server_status['waiting'].clear()
        server_status['users'].clear()
        server_status['invites'].clear()
        server_config['max_connections'] = 100

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
