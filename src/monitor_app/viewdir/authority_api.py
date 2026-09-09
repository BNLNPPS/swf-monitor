"""User-authority REST surface — the interface swf-remote's sign-in sweep writes.

swf-remote resolves `eic` GitHub organisation membership at sign-in and posts
it here; swf-monitor stores it and, from step 3 of the rollout, enforces on
it. JSON in, JSON out, never a redirect, so the call survives the swf-remote
hop (swf-monitor docs/EXTERNAL_ACCESS.md).

The endpoint writes the observed field only. ``rights`` is granted by a person
in the User admin page and is refused here, so a sign-in cannot alter an
administrative decision and the field's provenance holds by construction
rather than by convention.

Writing authority is not a user capability. The caller must be the swf-remote
authority writer arriving over the localhost tunnel — a service identity,
never a person — or an authenticated superuser on this side. Anything else is
refused, including a signed-in user acting for themselves, so the endpoint
stays closed even if it is ever reachable through the proxy.

Model and rollout: docs/AUTHORITY.md.
"""

from django.http import JsonResponse
from rest_framework.authentication import (SessionAuthentication,
                                           TokenAuthentication)
from rest_framework.decorators import (api_view, authentication_classes,
                                       permission_classes)
from rest_framework.permissions import AllowAny

from monitor_app.authority import AUTHORITY_WRITER, AuthorityError, set_eic
from monitor_app.middleware import TunnelAuthentication, is_tunnel_request

_AUTH = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]


def _is_authority_writer(request):
    """True for the swf-remote authority writer, or a superuser on this side."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return False
    if is_tunnel_request(request) and user.username == AUTHORITY_WRITER:
        return True
    return bool(getattr(user, 'is_superuser', False))


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes([AllowAny])
def user_authority(request):
    """POST /api/user-authority/

    ``{"username": "...", "authority": {"eic": true, "github": "..."}}``

    Records observed organisation membership for the account, and the GitHub
    login it was observed for — several accounts have a username unlike their
    GitHub login, and an administrator deciding a grant needs to see the
    identity that was checked. ``eic: null`` clears the field to unset.

    Returns ``{"username": ..., "authority": {"eic": …, "rights": …,
    "github": …}}``, the full record including the rights this endpoint does
    not write.
    """
    if not _is_authority_writer(request):
        return JsonResponse(
            {'error': 'not authorized to write user authority'}, status=403)

    body = request.data if isinstance(request.data, dict) else {}
    username = (body.get('username') or '').strip()
    if not username:
        return JsonResponse({'error': 'username is required'}, status=400)

    values = body.get('authority')
    if not isinstance(values, dict):
        return JsonResponse(
            {'error': 'authority must be an object'}, status=400)
    if 'rights' in values or 'rights' in body:
        return JsonResponse(
            {'error': 'rights is granted by a person in the User admin page '
                      'and cannot be written here'}, status=400)
    unknown = sorted(set(values) - {'eic', 'github'})
    if unknown:
        return JsonResponse(
            {'error': f"unknown authority field(s): {', '.join(unknown)}; "
                      'this endpoint writes eic and github'}, status=400)
    if 'eic' not in values:
        return JsonResponse({'error': 'eic is required'}, status=400)

    github = values.get('github', body.get('github'))
    try:
        record = set_eic(username, values.get('eic'), github=github)
    except AuthorityError as e:
        return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'username': username, 'authority': record})
