#!/usr/bin/env python3
"""Decrypt one dispatched cell, cut it and report the status word.

Stdout carries workflow mask commands, then one fixed status line. Stderr is
discarded. Nothing derived from the plaintext reaches either stream unmasked.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CUTTER = Path(__file__).with_name('cut_cell.py')
CELL_Z = 12
MAX_INDEX = (1 << CELL_Z) - 1
CIPHERTEXT = re.compile(r'[A-Za-z0-9_-]{38,512}={0,2}')
NONCE = re.compile(r'[0-9a-fA-F]{32}')
STATUS = re.compile(r'ok|none|fail:E[1-5]')
PRIVATE_ENV = ('OD1_C', 'OD1_CELL_KEY', 'OD1_CALLBACK_URL', 'OD1_CALLBACK_TOKEN')
CALLBACK_TIMEOUT_S = 15
CALLBACK_RETRIES = 2


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def is_index(value):
    return type(value) is int and 0 <= value <= MAX_INDEX


def decrypt_cell(c):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = base64.b64decode(os.environ['OD1_CELL_KEY'], validate=True)
    if len(key) != 32 or not CIPHERTEXT.fullmatch(c):
        raise ValueError
    body = c.rstrip('=')
    raw = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4))
    plain = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None).decode('utf-8'))
    z, x, y, nonce = plain['z'], plain['x'], plain['y'], plain['n']
    if type(z) is not int or z != CELL_Z or not is_index(x) or not is_index(y):
        raise ValueError
    if type(nonce) is not str or not NONCE.fullmatch(nonce):
        raise ValueError
    return x, y, nonce


def mask(x, y, nonce):
    values = (CELL_Z, x, y, f'{CELL_Z}/{x}/{y}', f'{x}_{y}', f'{x}/{y}', nonce)
    print(''.join(f'::add-mask::{value}\n' for value in values), end='', flush=True)


def cut(x, y):
    env = {k: v for k, v in os.environ.items() if k not in PRIVATE_ENV}
    env['OD1_CELL'] = f'{CELL_Z}/{x}/{y}'
    with tempfile.TemporaryDirectory(dir=os.environ.get('RUNNER_TEMP') or None) as work:
        result = subprocess.run(
            [sys.executable, str(CUTTER), '--err', os.path.join(work, 'cut.err')],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    word = result.stdout.decode('ascii', 'replace').strip()
    if STATUS.fullmatch(word) and word.startswith('fail') == (result.returncode != 0):
        return word
    return 'fail:E4'


def report(c, status):
    url = os.environ.get('OD1_CALLBACK_URL')
    token = os.environ.get('OD1_CALLBACK_TOKEN')
    if not url or not token:
        return False
    opener = urllib.request.build_opener(NoRedirect)
    for attempt in range(CALLBACK_RETRIES + 1):
        time.sleep(attempt)
        try:
            request = urllib.request.Request(
                url, data=json.dumps({'c': c, 'status': status}).encode(), method='POST',
                headers={'Authorization': f'Bearer {token}',
                         'Content-Type': 'application/json',
                         'User-Agent': 'naip-cutter'})
            with opener.open(request, timeout=CALLBACK_TIMEOUT_S):
                return True
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code < 500:
                return False
        except Exception:
            pass
    return False


def main():
    os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
    c = os.environ.get('OD1_C', '')
    try:
        x, y, nonce = decrypt_cell(c)
        mask(x, y, nonce)
    except Exception:
        status = 'fail:E1'
    else:
        try:
            status = cut(x, y)
        except Exception:
            status = 'fail:E4'
    reported = report(c, status)
    print(status, 'callback:ok' if reported else 'callback:fail', flush=True)
    return 0 if reported and not status.startswith('fail') else 1


if __name__ == '__main__':
    sys.exit(main())
