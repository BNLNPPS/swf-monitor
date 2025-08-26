"""
Integration test for the SSE stream endpoint using the production-style setup.

Requirements for this test to run (will skip otherwise):
- SWF_MONITOR_URL and SWF_API_TOKEN set in environment (e.g., via ~/.env)
- Redis available and REDIS_URL configured so Channels group fanout works
- Monitor web service reachable at SWF_MONITOR_URL and configured with TokenAuthentication

This test opens the SSE stream, publishes a message to the Channels group,
and verifies the message is received over the SSE connection.
"""

import json
import os
import time
import threading
from pathlib import Path

import requests
import urllib3
from django.conf import settings
from django.test import TestCase
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync


# Disable SSL warnings for self-signed certificates in tests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class TestSSEStreamIntegration(TestCase):
    maxDiff = None

    def setUp(self):
        # Load ~/.env to mirror how other integration tests prepare the environment
        env_file = Path.home() / ".env"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        if line.startswith('export '):
                            line = line[7:]
                        key, value = line.split('=', 1)
                        os.environ[key] = value.strip('\"\'')

        # Clear proxies to match agent behavior
        for proxy_var in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY']:
            os.environ.pop(proxy_var, None)

        # Required env (default to IPv4 loopback to avoid localhost IPv6 stalls)
        self.monitor_url = os.getenv('SWF_MONITOR_URL', 'https://127.0.0.1:8443')
        self.api_token = os.getenv('SWF_API_TOKEN')
        self.redis_url = os.getenv('REDIS_URL')

        if not self.api_token:
            self.skipTest('SWF_API_TOKEN not set; skipping SSE integration test')
        if not self.redis_url:
            self.skipTest('REDIS_URL not set; SSE relay requires Redis; skipping')

        # Prepare HTTP session
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Token {self.api_token}'})
        self.session.verify = False
        self.session.proxies = {'http': None, 'https': None}

        # Channels layer (must be Redis-backed)
        self.channel_layer = get_channel_layer()
        if self.channel_layer is None:
            self.skipTest('No channel layer configured; skipping')

        # Fast preflight probe: ensure SSE status is reachable with auth
        try:
            status_url = f"{self.monitor_url}/api/messages/stream/status/"
            status_resp = self.session.get(status_url, timeout=5)
            if status_resp.status_code != 200:
                self.fail(
                    f"SSE status check failed ({status_resp.status_code}); ensure SWF_API_TOKEN and server are correct: {status_url}"
                )
        except requests.exceptions.RequestException as e:
            self.fail(f"SSE status preflight failed: {e}")

    def test_sse_receives_broadcast_payload(self):
        group = getattr(settings, 'SSE_CHANNEL_GROUP', 'workflow_events')

        # Unique marker to find our event
        correlation_id = f"test-{int(time.time()*1000)}"
        payload = {
            'msg_type': 'test_event',
            'processed_by': 'sse-test',
            'run_id': 'run-xyz',
            'correlation_id': correlation_id,
            'message': 'hello from pytest'
        }

        # Open SSE stream
        stream_url = f"{self.monitor_url}/api/messages/stream/?msg_types=test_event&agents=sse-test"

        try:
            # Use (connect, read) timeouts to avoid hanging on TLS/connect
            resp = self.session.get(stream_url, stream=True, timeout=(5, 30))
        except requests.exceptions.ConnectionError as e:
            self.skipTest(f"Monitor not reachable at {self.monitor_url}: {e}")

        self.assertIn(resp.status_code, (200,))
        # Background reader to capture first matching event
        event_result = {
            'received': False,
            'event': None,
            'error': None,
        }

        def reader():
            try:
                buffer = []
                start = time.time()
                saw_connected = False
                for line in resp.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    # Timeout guard
                    if time.time() - start > 25:
                        event_result['error'] = 'Timeout waiting for SSE data'
                        break
                    line = line.strip()
                    if not line:
                        # End of one SSE event
                        if buffer:
                            # Track initial connected event and continue
                            if not saw_connected and any(
                                l.startswith('event: ') and 'connected' in l for l in buffer
                            ):
                                saw_connected = True
                                buffer = []
                                continue
                            data_lines = [l[6:] for l in buffer if l.startswith('data: ')]
                            if data_lines:
                                try:
                                    obj = json.loads('\n'.join(data_lines))
                                    if obj.get('correlation_id') == correlation_id:
                                        event_result['received'] = True
                                        event_result['event'] = obj
                                        break
                                except Exception as e:
                                    event_result['error'] = f'JSON parse error: {e}'
                                    break
                        buffer = []
                        continue
                    # Collect lines for current event
                    buffer.append(line)
            except Exception as e:
                event_result['error'] = str(e)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        # Give the server time to register the SSE client and join the Channels group
        time.sleep(1.5)

        # Publish payload to the Channels group (this is what the ActiveMQ listener does in production)
        try:
            async_to_sync(self.channel_layer.group_send)(
                group,
                {'type': 'broadcast', 'payload': payload}
            )
        except Exception as e:
            resp.close()
            self.fail(f"Failed to publish to channel layer: {e}")

        # Wait for the reader to capture the event or timeout
        t.join(timeout=30)
        try:
            resp.close()
        except Exception:
            pass

        if event_result['error']:
            self.fail(event_result['error'])

        self.assertTrue(event_result['received'], 'Did not receive SSE event with expected correlation_id')
        self.assertIsNotNone(event_result['event'])
        self.assertEqual(event_result['event'].get('processed_by'), 'sse-test')
