import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REPO = Path(__file__).parent
WORKFLOW = (REPO / '.github/workflows/cut.yml').read_text()
TOKEN = 'fake-callback-token'
X, Y = 4001, 4002
LOG_LINE = re.compile(r'(ok|none|fail:E[1-5]) callback:(ok|fail)')

NODE_VECTOR = {
    'key': '39X8XMYSru9w4CyemNpgp4gCk+0VIQ7ZybtNK9alamc=',
    'c': 'zzZQRpBvo2DJu0GKReHoinLAFckdHaFGOm7QA5OtFqFAwCGmB8xF8dW1bCXfm3VVBXA7SNjP'
         '1g0mYI9I0ABWiT10lyVXoWht54MNZJgioQMgeDGJme_3FRZmmrPB',
    'cell': f'12/{X}/{Y}',
}

STUB_CUTTER = '''
import json, os, sys
with open(os.environ['STUB_RECORD'], 'w') as out:
    json.dump({'argv': sys.argv[1:], 'env': dict(os.environ),
               'log': open(os.environ['STUB_LOG']).read()}, out)
cell = os.environ['OD1_CELL']
print('stderr noise', cell, file=sys.stderr)
with open(sys.argv[sys.argv.index('--err') + 1], 'w') as out:
    out.write(cell)
print(os.environ['STUB_WORD'].replace('CELL', cell))
sys.exit(int(os.environ['STUB_EXIT']))
'''


def encrypt(key, plain):
    iv = secrets.token_bytes(12)
    raw = iv + AESGCM(key).encrypt(iv, json.dumps(plain).encode(), None)
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def fake_plain(**changes):
    return {'z': 12, 'x': X, 'y': Y, 'n': secrets.token_hex(16), **changes}


def without(field):
    plain = fake_plain()
    del plain[field]
    return plain


class Receiver:
    def __init__(self):
        self.codes = []
        self.posts = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                receiver.posts.append((self.headers['Authorization'], json.loads(body)))
                code = receiver.codes.pop(0) if receiver.codes else 200
                self.send_response(code)
                if code == 302:
                    self.send_header('Location', '/elsewhere')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/done/secret-path'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.running = True

    def close(self):
        if self.running:
            self.running = False
            self.server.shutdown()
            self.server.server_close()


@pytest.fixture
def receiver():
    server = Receiver()
    yield server
    server.close()


class Job:
    def __init__(self, tmp_path, receiver):
        self.dir = tmp_path / 'job'
        self.temp = tmp_path / 'runner-temp'
        self.record = tmp_path / 'record.json'
        self.log = tmp_path / 'driver.log'
        self.dir.mkdir()
        self.temp.mkdir()
        shutil.copy(REPO / 'run_job.py', self.dir)
        (self.dir / 'cut_cell.py').write_text(STUB_CUTTER)
        self.key = secrets.token_bytes(32)
        self.receiver = receiver

    def run(self, c, key=None, word='ok', stub_exit=None, drop=()):
        env = {
            'PATH': os.environ['PATH'],
            'RUNNER_TEMP': str(self.temp),
            'OD1_C': c,
            'OD1_CELL_KEY': key or base64.b64encode(self.key).decode(),
            'OD1_CALLBACK_URL': self.receiver.url,
            'OD1_CALLBACK_TOKEN': TOKEN,
            'R2_ACCESS_KEY_ID': 'fake-id',
            'R2_SECRET_ACCESS_KEY': 'fake-secret',
            'R2_ENDPOINT': 'fake-endpoint',
            'STUB_RECORD': str(self.record),
            'STUB_LOG': str(self.log),
            'STUB_WORD': word,
            'STUB_EXIT': str(int(word.startswith('fail')) if stub_exit is None else stub_exit),
        }
        for name in drop:
            del env[name]
        self.env = env
        with open(self.log, 'w') as log:
            result = subprocess.run(
                [sys.executable, str(self.dir / 'run_job.py')], env=env, timeout=60,
                stdout=log, stderr=subprocess.STDOUT)
        self.stdout = self.log.read_text()
        return result

    def use_real_cutter(self):
        shutil.copy(REPO / 'cut_cell.py', self.dir)

    def cutter_ran(self):
        return self.record.exists()

    def recorded(self):
        return json.loads(self.record.read_text())


@pytest.fixture
def job(tmp_path, receiver):
    return Job(tmp_path, receiver)


def split_log(stdout):
    lines = stdout.splitlines()
    masks = [line for line in lines if line.startswith('::add-mask::')]
    rest = lines[len(masks):]
    assert lines[:len(masks)] == masks, 'a mask came after other output'
    return [line.removeprefix('::add-mask::') for line in masks], rest


def assert_blind(stdout, job, nonce):
    masks, rest = split_log(stdout)
    assert sorted(masks) == sorted(['12', str(X), str(Y), f'12/{X}/{Y}', f'{X}_{Y}',
                                    f'{X}/{Y}', nonce])
    text = '\n'.join(rest)
    key = base64.b64encode(job.key).decode()
    for value in (str(X), str(Y), f'12/{X}/{Y}', f'{X}_{Y}', f'{X}/{Y}', nonce, key,
                  job.receiver.url, TOKEN, 'http'):
        assert value not in text
    assert not re.search(r'\d{3}', text)
    for value in sorted(masks, key=len, reverse=True):
        text = text.replace(value, '***')
    assert len(rest) == 1 and LOG_LINE.fullmatch(text)


@pytest.mark.parametrize('word, stub_exit, codes, line, exit_code, posts', [
    ('ok', None, [200], 'ok callback:ok', 0, 1),
    ('none', None, [200], 'none callback:ok', 0, 1),
    ('fail:E3', None, [200], 'fail:E3 callback:ok', 1, 1),
    ('CELL oops', 0, [200], 'fail:E4 callback:ok', 1, 1),
    ('ok\nCELL', 0, [200], 'fail:E4 callback:ok', 1, 1),
    ('ok', 1, [200], 'fail:E4 callback:ok', 1, 1),
    ('ok', None, [500, 200], 'ok callback:ok', 0, 2),
    ('ok', None, [500, 503, 200], 'ok callback:ok', 0, 3),
    ('ok', None, [500, 500, 500, 200], 'ok callback:fail', 1, 3),
    ('ok', None, [401], 'ok callback:fail', 1, 1),
    ('ok', None, [404], 'ok callback:fail', 1, 1),
    ('ok', None, [302], 'ok callback:fail', 1, 1),
])
def test_cut_and_callback_stay_blind(job, receiver, word, stub_exit, codes, line, exit_code, posts):
    plain = fake_plain()
    c = encrypt(job.key, plain)
    receiver.codes = list(codes)
    result = job.run(c, word=word, stub_exit=stub_exit)
    assert result.returncode == exit_code
    assert_blind(job.stdout, job, plain['n'])
    assert split_log(job.stdout)[1] == [line]
    status = line.split()[0]
    assert receiver.posts == [(f'Bearer {TOKEN}', {'c': c, 'status': status})] * posts
    assert list(job.temp.iterdir()) == []


def test_masks_are_in_the_log_before_the_cutter_starts(job):
    plain = fake_plain()
    job.run(encrypt(job.key, plain))
    logged = job.recorded()['log']
    assert split_log(logged)[1] == []
    assert_blind(logged + 'ok callback:ok\n', job, plain['n'])


def test_cutter_gets_the_cell_only_through_env(job):
    job.run(encrypt(job.key, fake_plain()))
    seen = job.recorded()
    assert seen['env']['OD1_CELL'] == f'12/{X}/{Y}'
    assert not any(str(X) in arg for arg in seen['argv'])
    assert not {'OD1_C', 'OD1_CELL_KEY', 'OD1_CALLBACK_URL', 'OD1_CALLBACK_TOKEN'} & set(seen['env'])
    assert seen['env']['R2_ENDPOINT'] == 'fake-endpoint'


@pytest.mark.parametrize('name', ['R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_ENDPOINT'])
def test_missing_r2_env_reports_e5_from_the_real_cutter(job, receiver, name):
    job.use_real_cutter()
    plain = fake_plain()
    c = encrypt(job.key, plain)
    result = job.run(c, drop=[name])
    assert result.returncode == 1
    assert_blind(job.stdout, job, plain['n'])
    assert split_log(job.stdout)[1] == ['fail:E5 callback:ok']
    assert receiver.posts == [(f'Bearer {TOKEN}', {'c': c, 'status': 'fail:E5'})]


def test_missing_key_reports_e1_without_cutting(job, receiver):
    c = encrypt(job.key, fake_plain())
    result = job.run(c, drop=['OD1_CELL_KEY'])
    assert result.returncode == 1
    assert job.stdout == 'fail:E1 callback:ok\n'
    assert not job.cutter_ran()
    assert receiver.posts == [(f'Bearer {TOKEN}', {'c': c, 'status': 'fail:E1'})]


@pytest.mark.parametrize('name', ['OD1_CALLBACK_URL', 'OD1_CALLBACK_TOKEN'])
def test_missing_callback_secret_skips_callback(job, receiver, name):
    plain = fake_plain()
    result = job.run(encrypt(job.key, plain), drop=[name])
    assert result.returncode == 1
    assert_blind(job.stdout, job, plain['n'])
    assert split_log(job.stdout)[1] == ['ok callback:fail']
    assert receiver.posts == []


def test_missing_key_and_callback_secret(job):
    result = job.run(encrypt(job.key, fake_plain()), drop=['OD1_CELL_KEY', 'OD1_CALLBACK_URL'])
    assert (result.returncode, job.stdout) == (1, 'fail:E1 callback:fail\n')


def test_unreachable_callback_fails_silently(job, receiver):
    plain = fake_plain()
    c = encrypt(job.key, plain)
    receiver.close()
    result = job.run(c)
    assert result.returncode == 1
    assert_blind(job.stdout, job, plain['n'])
    assert split_log(job.stdout)[1] == ['ok callback:fail']


def tampered(c):
    middle = len(c) // 2
    return c[:middle] + ('A' if c[middle] != 'A' else 'B') + c[middle + 1:]


@pytest.mark.parametrize('make_c', [
    pytest.param(lambda key: tampered(encrypt(key, fake_plain())), id='tampered'),
    pytest.param(lambda key: encrypt(secrets.token_bytes(32), fake_plain()), id='wrong-key'),
    pytest.param(lambda key: encrypt(key, fake_plain(z=11)), id='wrong-z'),
    pytest.param(lambda key: encrypt(key, fake_plain(z=True)), id='bool-z'),
    pytest.param(lambda key: encrypt(key, fake_plain(z=12.0)), id='float-z'),
    pytest.param(lambda key: encrypt(key, without('n')), id='missing-n'),
    pytest.param(lambda key: encrypt(key, without('x')), id='missing-x'),
    pytest.param(lambda key: encrypt(key, fake_plain(n='abc')), id='short-n'),
    pytest.param(lambda key: encrypt(key, fake_plain(n='g' * 32)), id='non-hex-n'),
    pytest.param(lambda key: encrypt(key, fake_plain(n=1)), id='int-n'),
    pytest.param(lambda key: encrypt(key, fake_plain(x=4096)), id='x-high'),
    pytest.param(lambda key: encrypt(key, fake_plain(y=-1)), id='y-negative'),
    pytest.param(lambda key: encrypt(key, fake_plain(x=True)), id='bool-x'),
    pytest.param(lambda key: encrypt(key, fake_plain(y='7')), id='string-y'),
    pytest.param(lambda key: encrypt(key, [1, 2]), id='json-list'),
    pytest.param(lambda key: '', id='empty'),
    pytest.param(lambda key: 'not base64url!' * 4, id='bad-alphabet'),
    pytest.param(lambda key: 'A' * 37, id='too-short'),
    pytest.param(lambda key: 'A' * 513, id='too-long'),
    pytest.param(lambda key: 'A' * 41, id='impossible-length'),
])
def test_bad_ciphertext_reports_e1(job, receiver, make_c):
    c = make_c(job.key)
    result = job.run(c)
    assert (result.returncode, job.stdout) == (1, 'fail:E1 callback:ok\n')
    assert not job.cutter_ran()
    assert receiver.posts == [(f'Bearer {TOKEN}', {'c': c, 'status': 'fail:E1'})]


@pytest.mark.parametrize('key', [' ', 'not base64!', base64.b64encode(b'short').decode(),
                                 base64.b64encode(bytes(33)).decode()])
def test_bad_key_reports_e1(job, key):
    result = job.run(encrypt(job.key, fake_plain()), key=key)
    assert (result.returncode, job.stdout) == (1, 'fail:E1 callback:ok\n')
    assert not job.cutter_ran()


def test_non_json_plaintext_reports_e1(job):
    iv = secrets.token_bytes(12)
    raw = iv + AESGCM(job.key).encrypt(iv, b'\xff not json', None)
    result = job.run(base64.urlsafe_b64encode(raw).decode().rstrip('='))
    assert (result.returncode, job.stdout) == (1, 'fail:E1 callback:ok\n')


def test_padded_and_unpadded_ciphertext_decrypt(job, receiver):
    bare = next(c for c in iter(lambda: encrypt(job.key, fake_plain()), None) if len(c) % 4)
    padded = bare + '=' * (-len(bare) % 4)
    for c in (bare, padded):
        receiver.posts.clear()
        assert job.run(c).returncode == 0
        assert receiver.posts == [(f'Bearer {TOKEN}', {'c': c, 'status': 'ok'})]


def test_node_vector_decrypts_to_the_same_cell(job, receiver):
    result = job.run(NODE_VECTOR['c'], key=NODE_VECTOR['key'])
    assert result.returncode == 0
    assert job.recorded()['env']['OD1_CELL'] == NODE_VECTOR['cell']
    assert receiver.posts[0][1]['c'] == NODE_VECTOR['c']


def run_blocks(text):
    blocks, lines = [], text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r'( *)(- )?run: *(.*)', line)
        if match:
            indent = len(match[1]) + len(match[2] or '')
            body = [match[3]]
            for following in lines[index + 1:]:
                if following.strip() and len(following) - len(following.lstrip()) <= indent:
                    break
                body.append(following)
            blocks.append('\n'.join(body))
    return blocks


def test_workflow_trigger_is_dispatch_only():
    block = re.search(r'^on:\n((?:[ ].*\n)+)', WORKFLOW, re.M)[1]
    assert block == '  repository_dispatch:\n    types: [cut]\n'


def test_workflow_has_no_token_permissions():
    assert re.search(r'^permissions: \{\}$', WORKFLOW, re.M)
    assert WORKFLOW.count('permissions:') == 1


@pytest.mark.parametrize('banned', [
    'upload-artifact', 'cache', 'GITHUB_STEP_SUMMARY', 'GITHUB_ENV', 'GITHUB_OUTPUT',
    'pull_request', 'workflow_dispatch', 'schedule', 'push', 'matrix', 'concurrency',
    'cancel-in-progress',
])
def test_workflow_avoids(banned):
    assert banned not in WORKFLOW


def test_run_blocks_never_interpolate():
    blocks = run_blocks(WORKFLOW)
    assert len(blocks) == 2
    assert not any('${{' in block for block in blocks)
    assert all('set +x' in block for block in blocks)


def test_run_blocks_helper_catches_interpolation():
    text = '    - run: |\n        echo ${{ github.event.x }}\n    - name: next\n'
    assert '${{' in run_blocks(text)[0]


def test_actions_are_pinned_to_full_shas():
    uses = re.findall(r'uses: *(\S+)', WORKFLOW)
    assert len(uses) == 2
    assert all(re.fullmatch(r'[\w./-]+@[0-9a-f]{40}', ref) for ref in uses)


def test_timeouts_are_set():
    assert re.search(r'^    timeout-minutes: 25$', WORKFLOW, re.M)
    assert re.search(r'^        timeout-minutes: 20$', WORKFLOW, re.M)


def test_checkout_drops_credentials():
    assert 'persist-credentials: false' in WORKFLOW


def test_secrets_appear_only_in_the_cut_step():
    cut_step = WORKFLOW[WORKFLOW.index('- name: cut'):]
    assert 'secrets.' not in WORKFLOW[:WORKFLOW.index('- name: cut')]
    assert cut_step.count('secrets.') == 6
