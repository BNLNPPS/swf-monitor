"""User admin — the page where rights are granted, and the account's own view.

Two surfaces over `monitor_app.authority`:

- ``user_admin_page``, under the System menu, open to staff and to accounts
  holding ``rights == 'ops'``. It lists accounts with the GitHub login their
  membership was observed for, that membership, their rights, and whether
  they may act. ``eic`` is display-only: it is observed from GitHub and a
  hand edit would be overwritten at the next sign-in.
- ``user_rights_set``, the JSON endpoint the page writes through, and the
  only path by which ``rights`` is written. Keeping it apart from
  ``/api/user-authority/`` is what makes the split structural: the endpoint
  every sign-in goes through cannot touch rights at all.

The write is a JSON POST rather than a page-view POST because the page is
served to external users through the swf-remote proxy, which carries no
session or CSRF and cannot relay a redirect (docs/EXTERNAL_ACCESS.md).

Model: docs/AUTHORITY.md.
"""

import json

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from monitor_app.authority import (OPS_MEANING, PAC_MEANING, RIGHTS_BASIC,
                                   RIGHTS_MEANING, RIGHTS_READ,
                                   AuthorityError, all_authority,
                                   get_authority, is_ops, may_act,
                                   may_set_priority, set_ops, set_pac,
                                   set_rights)

#: The access levels the page offers; operations is a role beside them.
ACCESS_VALUES = (RIGHTS_READ, RIGHTS_BASIC)


def may_administer(user):
    """Staff on this side, or an account holding operations rights."""
    if not user or not user.is_authenticated:
        return False
    return bool(user.is_staff) or is_ops(user.username)


def _origin(record):
    """Where the account's identity comes from, as far as the record says.

    A GitHub identity is one the sweep has observed membership for. Rights
    without one is an account established inside the BNL perimeter through
    the account sync. Neither is an account nothing has been written for
    yet, and the column says nothing rather than guessing.
    """
    if record['github'] or record['eic'] is not None:
        return 'GitHub'
    if record['rights']:
        return 'account sync'
    return ''


def _rows():
    """Every account known on this side, with its authority."""
    User = get_user_model()
    records = all_authority()
    logins = dict(User.objects.values_list('username', 'last_login'))
    staff = set(User.objects.filter(is_staff=True)
                .values_list('username', flat=True))
    rows = []
    for username in sorted(set(records) | set(logins), key=str.lower):
        record = records.get(username) or get_authority(username)
        rows.append({
            'username': username,
            'github': record['github'],
            'origin': _origin(record),
            'eic': record['eic'],
            'rights': (RIGHTS_BASIC if record['rights'] == 'ops'
                       else record['rights']),
            'pac': record['pac'],
            'ops': record['ops'],
            'may_act': may_act(record),
            'may_set_priority': may_set_priority(record),
            'last_seen': record['eic_at'] or '',
            'last_login': logins.get(username),
            'is_staff': username in staff,
        })
    return rows


@require_http_methods(['GET'])
def user_admin_page(request):
    """GET users/admin/ — the account list, and where rights are set."""
    if not may_administer(request.user):
        return render(request, 'monitor_app/user_admin.html',
                      {'forbidden': True}, status=403)
    rows = _rows()
    return render(request, 'monitor_app/user_admin.html', {
        'rows': rows,
        'rights_values': ACCESS_VALUES,
        'rights_levels': [{'value': v, 'meaning': RIGHTS_MEANING[v]}
                          for v in ACCESS_VALUES],
        'pac_meaning': PAC_MEANING,
        'ops_meaning': OPS_MEANING,
        'acting_count': sum(1 for r in rows if r['may_act']),
        'pac_count': sum(1 for r in rows if r['pac']),
        'ops_count': sum(1 for r in rows if r['ops']),
    })


@require_http_methods(['POST'])
def user_rights_set(request):
    """POST /api/user-rights/ ``{"username": "...", "rights": "basic"}``,
    ``{"username": "...", "pac": true}``, ``{"username": "...", "ops": true}``,
    or any of them together.

    ``rights: null`` clears the grant, returning the account to drawing
    authority from ``eic`` alone; ``pac: false`` and ``ops: false`` clear
    the roles. All are a person's fields, written through this one
    endpoint and never by the sign-in sweep. Returns the account's full
    record.
    """
    from monitor_app.authority import AUTHORITY_WRITER
    from monitor_app.middleware import is_tunnel_request

    user = request.user
    writer = (user.is_authenticated and is_tunnel_request(request)
              and user.username == AUTHORITY_WRITER)
    if not (may_administer(user) or writer):
        return JsonResponse(
            {'error': 'not authorized to set rights'}, status=403)

    try:
        body = json.loads(request.body or b'{}')
    except ValueError as e:
        return JsonResponse({'error': f'malformed JSON: {e}'}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({'error': 'body must be an object'}, status=400)

    username = (body.get('username') or '').strip()
    if not username:
        return JsonResponse({'error': 'username is required'}, status=400)
    if 'eic' in body:
        return JsonResponse(
            {'error': 'eic is observed at sign-in and cannot be set here'},
            status=400)

    if not any(k in body for k in ('rights', 'pac', 'ops')):
        return JsonResponse({'error': 'rights, pac or ops is required'},
                            status=400)
    try:
        record = None
        if 'rights' in body:
            rights = body.get('rights')
            if rights == '':
                rights = None
            record = set_rights(username, rights)
        if 'pac' in body:
            record = set_pac(username, bool(body.get('pac')))
        if 'ops' in body:
            record = set_ops(username, bool(body.get('ops')))
    except AuthorityError as e:
        return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'username': username, 'authority': record,
                         'may_act': may_act(record),
                         'may_set_priority': may_set_priority(record)})
