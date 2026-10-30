#!/usr/bin/env python3
"""Check a deployed ES page and capture its first core plot in Chromium."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time

import requests
import websocket


def credentials():
    wanted = ('SWF_REMOTE_CLAUDE_USER', 'SWF_REMOTE_CLAUDE_PASSWORD')
    values = {key: os.environ.get(key) for key in wanted}
    for line in (Path.home() / '.env').read_text().splitlines():
        key, separator, value = line.strip().removeprefix('export ').partition('=')
        if separator and key in wanted and not values[key]:
            parts = shlex.split(value, comments=True)
            if len(parts) != 1:
                raise RuntimeError('Cannot parse the configured login field ' + key)
            values[key] = parts[0]
    if not all(values.values()):
        raise RuntimeError('The configured read-only browser login is incomplete')
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expect', default='Unit occupancy')
    args = parser.parse_args()
    session = requests.Session()
    response = session.get(args.url, timeout=40)
    response.raise_for_status()
    if args.expect not in response.text:
        token = re.search(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)', response.text)
        if token is None:
            raise RuntimeError('The requested page has neither the expected marker nor a login form')
        auth = credentials()
        response = session.post(response.url, data={
            'username': auth['SWF_REMOTE_CLAUDE_USER'],
            'password': auth['SWF_REMOTE_CLAUDE_PASSWORD'],
            'csrfmiddlewaretoken': token.group(1), 'next': args.url},
            headers={'Referer': response.url}, timeout=40)
        response.raise_for_status()
    if args.expect not in response.text:
        raise RuntimeError('The deployed page did not contain the expected marker after login')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    executable = sorted((Path.home() / '.cache/ms-playwright').glob(
        'chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'))[-1]
    with tempfile.TemporaryDirectory(prefix='es-browser-', dir='/data/swf-tmp') as profile:
        process = subprocess.Popen([str(executable), '--no-sandbox', '--disable-gpu',
            '--remote-debugging-port=0', '--remote-allow-origins=*',
            '--user-data-dir=' + profile, 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            active = Path(profile) / 'DevToolsActivePort'
            deadline = time.monotonic() + 15
            while not active.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Chromium did not expose its local inspection endpoint')
                time.sleep(.1)
            port = active.read_text().splitlines()[0]
            page = requests.get('http://127.0.0.1:' + port + '/json/list', timeout=10).json()[0]
            connection = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=30)
            sequence = 0

            def call(method, params=None):
                nonlocal sequence
                sequence += 1
                connection.send(json.dumps({'id': sequence, 'method': method, 'params': params or {}}))
                while True:
                    answer = json.loads(connection.recv())
                    if answer.get('id') == sequence:
                        if answer.get('error'):
                            raise RuntimeError('Browser inspection failed for ' + method)
                        return answer.get('result', {})

            call('Network.enable')
            call('Network.setCookies', {'cookies': [
                {'name': c.name, 'value': c.value, 'domain': c.domain,
                 'path': c.path, 'secure': c.secure} for c in session.cookies]})
            call('Page.enable')
            call('Emulation.setDeviceMetricsOverride',
                 {'width': 1600, 'height': 1200, 'deviceScaleFactor': 1, 'mobile': False})
            call('Page.navigate', {'url': args.url})
            deadline = time.monotonic() + 30
            expression = ('document.readyState === "complete" && document.body.innerText.includes('
                          + json.dumps(args.expect) + ')')
            while not call('Runtime.evaluate', {'expression': expression})['result'].get('value'):
                if time.monotonic() > deadline:
                    raise RuntimeError('The authenticated browser did not render the expected page')
                time.sleep(.25)
            box = call('Runtime.evaluate', {'returnByValue': True, 'expression':
                '(()=>{const e=document.querySelector(\'svg[aria-label="core occupancy"]\');'
                'if(!e)return null;const r=e.getBoundingClientRect();return {x:Math.max(0,r.x-20),'
                'y:Math.max(0,r.y+window.scrollY-145),width:r.width+40,height:r.height+420,scale:1}})()'
                })['result'].get('value')
            if not box:
                raise RuntimeError('The deployed page has no rendered core plot')
            import base64
            capture = call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True, 'clip': box})
            args.output.write_bytes(base64.b64decode(capture['data']))
            connection.close()
            print(json.dumps({'url': args.url, 'expected_marker': args.expect,
                              'screenshot': str(args.output), 'status': 'passed'}))
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    main()
