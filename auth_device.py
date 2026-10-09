#!/usr/bin/env python3
"""One-time login: prints a code, you confirm it in a browser, token is saved.

    python3 auth_device.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'pylibs'))
from yandex_music import Client

TOKEN = os.path.join(_HERE, 'token')


def on_code(code):
    print('OPEN:', code.verification_url)
    print('CODE:', code.user_code, flush=True)


client = Client()
token = client.device_auth(on_code=on_code, device_name='ymusic-player')
with open(TOKEN, 'w') as f:
    f.write(token.refresh_token or '')
os.chmod(TOKEN, 0o600)
print('saved refresh token to', TOKEN)
