#!/usr/bin/env python3
"""Standalone Taiko Web multiplayer server with a public health endpoint."""

import argparse
import asyncio
from collections import deque
from http import HTTPStatus
import json
import os
import random
import time
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import serve


CONSONANTS = 'bcdfghjklmnpqrstvwxyz'
server_status = {'waiting': {}, 'users': [], 'invites': {}}
server_config = {
    'max_connections': 100,
    'max_messages_per_second': 120,
    'max_invalid_invites': 5
}


def msgobj(msg_type, value=None, metadata=None):
    message = {'type': msg_type}
    if value is not None:
        message['value'] = value
    if metadata:
        message.update(metadata)
    return json.dumps(message, separators=(',', ':'))


def status_event():
    return msgobj('users', [
        {'id': game_id, 'diff': entry['diff']}
        for game_id, entry in server_status['waiting'].items()
        if entry['user'].get('ws') is not None
    ])


def is_connected(user):
    if not user:
        return False
    websocket = user.get('ws')
    return websocket is not None and not websocket.closed


def at_capacity():
    return len(server_status['users']) >= server_config['max_connections']


def consume_message_rate(user, now=None):
    now = time.monotonic() if now is None else now
    timestamps = user.setdefault('message_times', deque())
    while timestamps and timestamps[0] <= now - 1:
        timestamps.popleft()
    timestamps.append(now)
    return len(timestamps) <= server_config['max_messages_per_second']


async def send_to(user, msg_type, value=None, metadata=None):
    if not is_connected(user):
        return False
    try:
        await user['ws'].send(msgobj(msg_type, value, metadata))
        return True
    except ConnectionClosed:
        return False


async def send_many(*messages):
    if messages:
        await asyncio.gather(*(send_to(*message) for message in messages))


async def notify_status():
    message = status_event()
    users = [user for user in server_status['users'] if user['action'] == 'ready' and is_connected(user)]
    if users:
        await asyncio.gather(*(user['ws'].send(message) for user in users), return_exceptions=True)


def get_invite():
    while True:
        invite = ''.join(random.choice(CONSONANTS) for _ in range(5))
        if invite not in server_status['invites']:
            return invite


def remove_waiting_user(user):
    game_id = user.pop('gameid', None)
    if game_id and server_status['waiting'].get(game_id, {}).get('user') is user:
        server_status['waiting'].pop(game_id, None)


def set_identity(user, value):
    value = value if isinstance(value, dict) else {}
    name = value.get('name')
    user['name'] = name[:25] if isinstance(name, str) else None
    don = value.get('don')
    if isinstance(don, dict):
        user['don'] = {
            key: color[:64]
            for key, color in don.items()
            if key in ('body_fill', 'face_fill') and isinstance(color, str)
        }
    else:
        user['don'] = None


async def start_open_match(user, value):
    if not isinstance(value, dict):
        return
    game_id = value.get('id')
    difficulty = value.get('diff')
    if not game_id or not difficulty:
        return
    set_identity(user, value)
    waiting = server_status['waiting']
    other_entry = waiting.pop(game_id, None)
    if not other_entry or not is_connected(other_entry['user']):
        user['action'] = 'waiting'
        user['gameid'] = game_id
        waiting[game_id] = {'user': user, 'diff': difficulty}
        await send_to(user, 'waiting')
        await notify_status()
        return

    other = other_entry['user']
    user['action'] = other['action'] = 'loading'
    user['other_user'] = other
    other['other_user'] = user
    other['player'] = 1
    user['player'] = 2
    await send_many(
        (user, 'gameload', {'diff': other_entry['diff'], 'player': 2}),
        (other, 'gameload', {'diff': difficulty, 'player': 1}),
        (user, 'name', {'name': other.get('name'), 'don': other.get('don')}),
        (other, 'name', {'name': user.get('name'), 'don': user.get('don')})
    )
    await notify_status()


async def start_invite(user, value):
    if not isinstance(value, dict) or 'id' not in value:
        return
    invite_id = value.get('id')
    if invite_id is None:
        invite = get_invite()
        user['action'] = 'invite'
        user['session'] = invite
        set_identity(user, value)
        server_status['invites'][invite] = user
        await send_to(user, 'invite', invite)
        return

    if not isinstance(invite_id, str) or len(invite_id) != 5 or any(char not in CONSONANTS for char in invite_id):
        user['invalid_invites'] = user.get('invalid_invites', 0) + 1
        if user['invalid_invites'] >= server_config['max_invalid_invites']:
            await user['ws'].close(code=1008, reason='Too many invalid invitation attempts')
        else:
            await send_to(user, 'gameend')
        return

    other = server_status['invites'].pop(invite_id, None)
    if not other or not is_connected(other):
        user['invalid_invites'] = user.get('invalid_invites', 0) + 1
        if user['invalid_invites'] >= server_config['max_invalid_invites']:
            await user['ws'].close(code=1008, reason='Too many invalid invitation attempts')
            return
        await send_to(user, 'gameend')
        return
    user['invalid_invites'] = 0
    set_identity(user, value)
    user['other_user'] = other
    other['other_user'] = user
    user['action'] = other['action'] = 'invite'
    user['session'] = other['session'] = invite_id
    other['player'] = 1
    user['player'] = 2
    await send_many(
        (user, 'session', {'player': 2}),
        (other, 'session', {'player': 1}),
        (user, 'invite'),
        (user, 'name', {'name': other.get('name'), 'don': other.get('don')}),
        (other, 'name', {'name': user.get('name'), 'don': user.get('don')})
    )


async def leave_waiting_or_loading(user):
    other = user.get('other_user')
    if user.get('session'):
        if is_connected(other):
            user['action'] = 'songsel'
            await send_many((user, 'left'), (other, 'users', []))
        else:
            user['action'] = 'ready'
            user['session'] = False
            await send_many((user, 'gameend'), (user, 'users', json.loads(status_event()).get('value', [])))
    else:
        remove_waiting_user(user)
        user['action'] = 'ready'
        await send_many((user, 'left'))
        await notify_status()


async def process_playing(user, msg_type, value):
    other = user.get('other_user')
    if not is_connected(other):
        user['action'] = 'ready'
        user['session'] = False
        await send_many((user, 'gameend'), (user, 'users', json.loads(status_event()).get('value', [])))
        return
    if msg_type in ('note', 'drumroll', 'branch', 'gameresults'):
        user['relay_seq'] = user.get('relay_seq', 0) + 1
        await send_to(other, msg_type, value, {
            'seq': user['relay_seq'],
            'server_time': int(time.time() * 1000)
        })
    elif msg_type == 'songsel' and user.get('session'):
        user['action'] = other['action'] = 'songsel'
        await send_many((user, 'songsel'), (user, 'users', []), (other, 'songsel'), (other, 'users', []))
    elif msg_type == 'gameend':
        user['action'] = other['action'] = 'ready'
        user['session'] = other['session'] = False
        user.pop('other_user', None)
        other.pop('other_user', None)
        await send_many((user, 'gameend'), (other, 'gameend'))
        await notify_status()


async def process_invite(user, msg_type):
    other = user.get('other_user')
    if msg_type == 'leave':
        if server_status['invites'].get(user.get('session')) is user:
            server_status['invites'].pop(user['session'], None)
        user['action'] = 'ready'
        user['session'] = False
        if is_connected(other):
            other['action'] = 'ready'
            other['session'] = False
            user.pop('other_user', None)
            other.pop('other_user', None)
            await send_many((user, 'left'), (other, 'gameend'))
        else:
            await send_to(user, 'left')
        await notify_status()
    elif msg_type == 'songsel' and is_connected(other):
        user['action'] = other['action'] = 'songsel'
        await send_many((user, 'songsel'), (other, 'songsel'))


async def process_songsel(user, msg_type, value):
    other = user.get('other_user')
    if not is_connected(other):
        user['action'] = 'ready'
        user['session'] = False
        await send_many((user, 'gameend'), (user, 'users', json.loads(status_event()).get('value', [])))
        return
    if msg_type in ('songsel', 'catjump') and isinstance(value, dict) and other['action'] == 'songsel':
        value = dict(value)
        value['player'] = user.get('player', 1)
        await send_many((user, msg_type, value), (other, msg_type, value))
    elif msg_type in ('crowns', 'getcrowns') and other['action'] == 'songsel':
        await send_to(other, msg_type, value)
    elif msg_type == 'join' and isinstance(value, dict) and value.get('id') and value.get('diff'):
        if other['action'] == 'waiting':
            user['action'] = other['action'] = 'loading'
            await send_many(
                (user, 'gameload', {'diff': other.get('gamediff')}),
                (other, 'gameload', {'diff': value.get('diff')})
            )
        else:
            user['action'] = 'waiting'
            user['gamediff'] = value.get('diff')
            await send_to(other, 'users', [{'id': value.get('id'), 'diff': value.get('diff')}])
    elif msg_type == 'gameend':
        user['action'] = other['action'] = 'ready'
        user['session'] = other['session'] = False
        user.pop('other_user', None)
        other.pop('other_user', None)
        await send_many((user, 'gameend'), (other, 'gameend'))
        await notify_status()


async def process_message(user, message):
    try:
        data = json.loads(message)
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    msg_type = data.get('type')
    value = data.get('value')
    if msg_type == 'syncping':
        await send_to(user, 'syncpong', value, {'server_time': int(time.time() * 1000)})
        return
    action = user['action']
    if action == 'ready':
        if msg_type == 'join':
            await start_open_match(user, value)
        elif msg_type == 'invite':
            await start_invite(user, value)
    elif action in ('waiting', 'loading', 'loaded'):
        if msg_type == 'leave':
            await leave_waiting_or_loading(user)
        elif action == 'loading' and msg_type == 'gamestart':
            user['action'] = 'loaded'
            other = user.get('other_user')
            if other and other.get('action') == 'loaded':
                user['action'] = other['action'] = 'playing'
                await send_many((user, 'gamestart'), (other, 'gamestart'))
    elif action == 'playing':
        await process_playing(user, msg_type, value)
    elif action == 'invite':
        await process_invite(user, msg_type)
    elif action == 'songsel':
        await process_songsel(user, msg_type, value)


async def disconnect(user):
    user['ws'] = None
    if user in server_status['users']:
        server_status['users'].remove(user)
    remove_waiting_user(user)
    if server_status['invites'].get(user.get('session')) is user:
        server_status['invites'].pop(user.get('session'), None)
    other = user.pop('other_user', None)
    if is_connected(other):
        other['action'] = 'ready'
        other['session'] = False
        other.pop('other_user', None)
        await send_many((other, 'gameend'), (other, 'users', json.loads(status_event()).get('value', [])))
    await notify_status()


async def connection(websocket):
    if urlsplit(websocket.path).path != '/':
        await websocket.close(code=1008, reason='WebSocket endpoint is /')
        return
    if at_capacity():
        await websocket.close(code=1013, reason='Multiplayer server is at capacity')
        return
    user = {
        'ws': websocket,
        'action': 'ready',
        'session': False,
        'name': None,
        'don': None,
        'message_times': deque(),
        'invalid_invites': 0,
        'relay_seq': 0
    }
    server_status['users'].append(user)
    try:
        await websocket.send(status_event())
        async for message in websocket:
            if not consume_message_rate(user):
                await websocket.close(code=1008, reason='Message rate limit exceeded')
                break
            try:
                await process_message(user, message)
            except Exception as exc:
                # A malformed client message must never bring down the shared server.
                print('Ignoring multiplayer message error: {}'.format(exc), flush=True)
                await send_to(user, 'gameend')
    except ConnectionClosed:
        pass
    except Exception as exc:
        print('Recovering from connection error: {}'.format(exc), flush=True)
    finally:
        try:
            await disconnect(user)
        except Exception as exc:
            # Keep one cleanup failure from affecting the event-loop service.
            print('Ignoring disconnect cleanup error: {}'.format(exc), flush=True)


async def process_request(path, _headers):
    requested_path = urlsplit(path).path
    if requested_path == '/health':
        body = json.dumps({
            'status': 'ok',
            'connections': len(server_status['users']),
            'max_connections': server_config['max_connections'],
            'accepting_connections': not at_capacity()
        }).encode('utf-8')
        return HTTPStatus.OK, [('Content-Type', 'application/json'), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store')], body
    if requested_path != '/':
        body = b'Not found\n'
        return HTTPStatus.NOT_FOUND, [('Content-Type', 'text/plain; charset=utf-8'), ('Content-Length', str(len(body)))], body
    if at_capacity():
        body = b'Multiplayer server is at capacity\n'
        return HTTPStatus.SERVICE_UNAVAILABLE, [('Content-Type', 'text/plain; charset=utf-8'), ('Content-Length', str(len(body))), ('Retry-After', '15')], body
    return None


def parse_origins(value):
    origins = [origin.strip() for origin in (value or '').split(',') if origin.strip()]
    if not origins:
        raise ValueError('ALLOWED_ORIGINS must contain at least one exact site origin.')
    if '*' in origins:
        raise ValueError('ALLOWED_ORIGINS must use exact origins; wildcard origins are not allowed.')
    return origins


def parse_args():
    parser = argparse.ArgumentParser(description='Run the Taiko Web multiplayer server.')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '34802')))
    parser.add_argument('--bind-address', default=os.environ.get('BIND_ADDRESS', '0.0.0.0'))
    parser.add_argument('--allowed-origins', default=os.environ.get('ALLOWED_ORIGINS', ''))
    parser.add_argument('--max-connections', type=int, default=int(os.environ.get('MAX_CONNECTIONS', '100')))
    parser.add_argument(
        '--max-messages-per-second',
        type=int,
        default=int(os.environ.get('MAX_MESSAGES_PER_SECOND', '120'))
    )
    args = parser.parse_args()
    if not 1 <= args.max_connections <= 100000:
        parser.error('--max-connections must be between 1 and 100000')
    if not 20 <= args.max_messages_per_second <= 1000:
        parser.error('--max-messages-per-second must be between 20 and 1000')
    return args


async def run_server(args):
    origins = parse_origins(args.allowed_origins)
    server_config['max_connections'] = args.max_connections
    server_config['max_messages_per_second'] = args.max_messages_per_second
    async with serve(
        connection,
        args.bind_address,
        args.port,
        origins=origins,
        process_request=process_request,
        ping_interval=10,
        ping_timeout=10,
        max_size=64 * 1024,
        max_queue=16,
        close_timeout=5
    ):
        print(
            'Multiplayer server listening on {}:{} for {} (hard connection limit: {})'.format(
                args.bind_address, args.port, ', '.join(origins), args.max_connections
            ),
            flush=True
        )
        await asyncio.Future()


def main():
    args = parse_args()
    asyncio.run(run_server(args))


if __name__ == '__main__':
    main()
