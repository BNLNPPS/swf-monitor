"""Authority: the truth table, the two gates, and the declarations.

docs/AUTHORITY.md. Plain assertions over the real modules; the database is
the test database Django gives every TestCase.
"""

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings

from monitor_app import authority as A
from monitor_app.middleware import (AUTHORITY_EXEMPT_URL_NAMES,
                                    AuthorityGateMiddleware,
                                    TunnelAuthentication)
from monitor_app.models import SysConfig, UserPreference


class AuthorityRuleTests(TestCase):

    def test_may_act_truth_table(self):
        cases = [
            ({'eic': True, 'rights': None}, True),
            ({'eic': True, 'rights': 'read'}, False),
            ({'eic': False, 'rights': None}, False),
            ({'eic': False, 'rights': 'basic'}, True),
            ({'eic': None, 'rights': 'basic'}, True),
            ({'eic': None, 'rights': 'ops'}, True),
            ({'eic': None, 'rights': 'read'}, False),
            ({'eic': None, 'rights': None}, False),
        ]
        for record, expected in cases:
            self.assertEqual(A.may_act(record), expected, record)
        self.assertFalse(A.may_act('nobody-here'))

    def test_setters_write_their_own_field_only(self):
        A.set_rights('u', 'basic')
        A.set_eic('u', False, github='u-gh')
        record = A.get_authority('u')
        self.assertEqual((record['eic'], record['rights'], record['github']),
                         (False, 'basic', 'u-gh'))
        A.set_eic('u', None)
        self.assertEqual(A.get_authority('u')['rights'], 'basic')
        with self.assertRaises(A.AuthorityError):
            A.set_rights('u', 'admin')
        with self.assertRaises(ValueError):
            UserPreference.set_pref('u', A.AUTHORITY_KEY, {})

    def test_may_set_priority_needs_pac_or_ops_and_authority(self):
        cases = [
            ({'eic': True, 'rights': None, 'pac': False}, False),
            ({'eic': True, 'rights': None, 'pac': True}, True),
            ({'eic': False, 'rights': None, 'pac': True}, False),
            ({'eic': None, 'rights': 'ops', 'pac': False, 'ops': True}, True),
            ({'eic': True, 'rights': None, 'pac': False, 'ops': True}, True),
            ({'eic': None, 'rights': 'basic', 'pac': False}, False),
            ({'eic': None, 'rights': 'basic', 'pac': True}, True),
            ({'eic': True, 'rights': 'read', 'pac': True}, False),
        ]
        for record, expected in cases:
            self.assertEqual(A.may_set_priority(record), expected, record)

    def test_roles_are_flags_beside_rights(self):
        A.set_rights('p', 'basic')
        A.set_pac('p', True)
        A.set_ops('p', True)
        record = A.get_authority('p')
        self.assertEqual((record['rights'], record['pac'], record['ops']),
                         ('basic', True, True))
        self.assertTrue(A.is_ops(record) and A.may_set_priority(record))
        A.set_pac('p', False)
        self.assertEqual(A.get_authority('p')['rights'], 'basic')
        self.assertFalse(A.get_authority('p')['pac'])
        self.assertTrue(A.get_authority('p')['ops'])

    def test_former_ops_rung_reads_as_the_role_and_clears_to_basic(self):
        A.set_rights('o', 'ops')
        record = A.get_authority('o')
        self.assertTrue(record['ops'] and A.is_ops(record) and A.may_act(record))
        A.set_ops('o', False)
        record = A.get_authority('o')
        self.assertEqual((record['rights'], record['ops']), ('basic', False))


class TunnelIdentityTests(TestCase):

    def test_no_header_means_no_identity(self):
        request = RequestFactory().post('/api/x/', REMOTE_ADDR='127.0.0.1',
                                        HTTP_AUTHORIZATION='Token nope')
        self.assertIsNone(TunnelAuthentication().authenticate(request))
        request = RequestFactory().post('/api/x/', REMOTE_ADDR='127.0.0.1',
                                        HTTP_X_REMOTE_USER='someone')
        user, _ = TunnelAuthentication().authenticate(request)
        self.assertEqual(user.username, 'someone')


@override_settings(ROOT_URLCONF='swf_monitor_project.urls')
class RestGateTests(TestCase):

    def setUp(self):
        self.gate = AuthorityGateMiddleware(lambda request: 'passed')
        self.user = get_user_model().objects.create(username='person')
        SysConfig.update_config({A.ENFORCE_KEY: True}, username='test')

    def _request(self, method, path, user=None, **extra):
        request = getattr(RequestFactory(), method)(path, **extra)
        request.user = user or self.user
        return request

    def test_person_without_authority_is_refused_on_writes_only(self):
        self.assertEqual(self.gate(self._request('get', '/pcs/api/x/')),
                         'passed')
        response = self.gate(self._request('post', '/pcs/api/x/'))
        self.assertEqual(response.status_code, 403)
        self.assertIn('Join GitHub', response.content.decode())

    def test_person_with_authority_passes(self):
        A.set_rights('person', 'basic')
        self.assertEqual(self.gate(self._request('post', '/pcs/api/x/')),
                         'passed')

    def test_read_veto_outranks_membership(self):
        A.set_eic('person', True)
        A.set_rights('person', 'read')
        self.assertEqual(
            self.gate(self._request('post', '/pcs/api/x/')).status_code, 403)

    def test_no_person_is_left_to_the_view(self):
        from django.contrib.auth.models import AnonymousUser
        request = self._request('post', '/api/logs/', user=AnonymousUser(),
                                HTTP_AUTHORIZATION='Token whatever')
        self.assertEqual(self.gate(request), 'passed')

    def test_exempt_writes_pass(self):
        request = self._request('post', '/api/user-rights/')
        self.assertEqual(self.gate(request), 'passed')
        self.assertIn('user-rights', AUTHORITY_EXEMPT_URL_NAMES)

    def test_observing_logs_and_passes(self):
        SysConfig.update_config({A.ENFORCE_KEY: False}, username='test')
        with self.assertLogs('monitor_app.middleware', level='WARNING') as cm:
            self.assertEqual(self.gate(self._request('post', '/pcs/api/x/')),
                             'passed')
        self.assertIn('would refuse', cm.output[0])


@override_settings(ROOT_URLCONF='swf_monitor_project.urls')
class UserAdminCsrfTests(TestCase):
    """The internal face is a session with CSRF in force; the Save button
    must carry the token it was given."""

    def test_rights_write_needs_and_accepts_the_csrf_token(self):
        from django.test import Client
        from django.urls import reverse
        staff = get_user_model().objects.create(username='admin-here',
                                                is_staff=True)
        # A non-localhost address: the internal face. Localhost would be the
        # tunnel, which the middleware exempts from CSRF.
        client = Client(enforce_csrf_checks=True, REMOTE_ADDR='10.42.0.9')
        client.force_login(staff)
        page = client.get(reverse('monitor_app:user_admin'))
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'csrfmiddlewaretoken', page.content)
        token = client.cookies['csrftoken'].value
        url = reverse('monitor_app:user-rights')
        body = '{"username": "someone", "rights": "basic"}'
        refused = client.post(url, body, content_type='application/json')
        self.assertEqual(refused.status_code, 403)
        accepted = client.post(url, body, content_type='application/json',
                               HTTP_X_CSRFTOKEN=token)
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(A.get_authority('someone')['rights'], 'basic')


class McpDeclarationTests(TestCase):

    def test_every_write_tool_is_guarded_and_declared(self):
        from monitor_app.mcp import mcp
        from monitor_app.mcp.common import AUTHORITY_GUARDED_TOOLS
        import monitor_app.mcp.ai_content  # noqa: F401  registers tools
        import monitor_app.mcp.ai_memory  # noqa: F401
        import monitor_app.mcp.system  # noqa: F401
        import monitor_app.mcp.workflows  # noqa: F401
        try:
            import swf_epicprod.mcp_tools.pcs  # noqa: F401
            import swf_epicprod.mcp_tools.proposals  # noqa: F401
        except ImportError:
            pass
        registered = {name: tool.fn
                      for name, tool in mcp._tool_manager._tools.items()}
        guarded = {name for name, fn in registered.items()
                   if getattr(fn, '__authority_guarded__', False)}
        # Guarded and declared are the same set: a write tool cannot be
        # added without declaring it, and a declared name cannot go stale.
        self.assertEqual(guarded, AUTHORITY_GUARDED_TOOLS & set(registered))
        self.assertFalse(AUTHORITY_GUARDED_TOOLS - set(registered),
                         'declared but not registered')
