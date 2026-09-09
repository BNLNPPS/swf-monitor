"""Authority — who may act on the system, as distinct from who may watch it.

Reading swf-monitor needs a signed-in account and nothing more, at every
level, so collaborators outside the `eic` GitHub organisation keep their
monitoring view. Acting on the system needs authority, held per account as
``UserPreference.prefs['authority']`` in two fields:

``eic``     unset | true | false        written only by swf-remote's sign-in sweep
``rights``  unset | read | basic | ops  written only by a person, in the User admin page

    may act  =  rights != 'read'  and  (eic is True or rights in ('basic', 'ops'))

Provenance is carried by which field a value sits in, not by a set-by marker.
The sweep maintains ``eic`` and nothing else, so an administrative grant
cannot be undone by a later sign-in; a person maintains ``rights`` and nothing
else, so a member who leaves the organisation loses authority at their next
sign-in with no one acting. ``rights: read`` is an explicit veto that outranks
``eic`` — the way to stop a member of the organisation from acting.
``rights: unset`` is the ordinary state of a member, who draws authority from
``eic`` alone.

Each level names what the person may do rather than what they may not,
because they read it on their own account page: ``read``, not "none". Both
fields default to unset, and an account with no record resolves to unable to
act, so a capability nobody declared is refused rather than exposed.

Model and rollout: docs/AUTHORITY.md.
"""

from django.db import transaction

AUTHORITY_KEY = 'authority'

#: Rights levels, least to most. Unset is the absence of the field, and is
#: not a level: an ordinary member holds no rights and acts on ``eic`` alone.
RIGHTS_READ = 'read'
RIGHTS_BASIC = 'basic'
RIGHTS_OPS = 'ops'
RIGHTS_VALUES = (RIGHTS_READ, RIGHTS_BASIC, RIGHTS_OPS)

#: Rights that confer authority on their own, without organisation membership.
RIGHTS_GRANTING = (RIGHTS_BASIC, RIGHTS_OPS)

#: What a person may do at each level, as they read it on their account page.
RIGHTS_MEANING = {
    RIGHTS_READ: 'Read monitoring information. Actions are withheld.',
    RIGHTS_BASIC: 'Act on the production system.',
    RIGHTS_OPS: 'Act on the production system, and administer accounts.',
}

#: Tunnel identity swf-remote presents when it writes the observed field. It
#: is a service name, never a person, so a request carrying a signed-in
#: person's identity cannot write authority even if the endpoint is ever
#: reachable through the proxy.
AUTHORITY_WRITER = 'swf-remote-authority'


class AuthorityError(ValueError):
    """Malformed authority input."""


def empty_authority():
    """The record an account with nothing written resolves to."""
    return {'eic': None, 'rights': None, 'github': '', 'eic_at': ''}


def _record_from(stored):
    record = empty_authority()
    if not isinstance(stored, dict):
        return record
    eic = stored.get('eic')
    if isinstance(eic, bool):
        record['eic'] = eic
    rights = stored.get('rights')
    if rights in RIGHTS_VALUES:
        record['rights'] = rights
    for field in ('github', 'eic_at'):
        value = stored.get(field)
        if isinstance(value, str):
            record[field] = value
    return record


def get_authority(username):
    """The authority record for ``username``; unset fields read as None."""
    from .models import UserPreference

    if not username:
        return empty_authority()
    row = UserPreference.objects.filter(username=username).first()
    return _record_from((row.prefs or {}).get(AUTHORITY_KEY) if row else None)


def all_authority():
    """``{username: record}`` for every account carrying an authority record.

    An account absent from the map has nothing written: both fields unset,
    and unable to act until one of them is.
    """
    from .models import UserPreference

    out = {}
    for username, prefs in UserPreference.objects.values_list(
            'username', 'prefs'):
        stored = (prefs or {}).get(AUTHORITY_KEY)
        if isinstance(stored, dict):
            out[username] = _record_from(stored)
    return out


def may_act(record):
    """Whether an authority record confers authority to act.

    Takes a record from ``get_authority`` or a username.
    """
    if not isinstance(record, dict):
        record = get_authority(record)
    if record.get('rights') == RIGHTS_READ:
        return False
    return record.get('eic') is True or record.get('rights') in RIGHTS_GRANTING


def is_ops(record):
    """Whether a record carries operations rights."""
    if not isinstance(record, dict):
        record = get_authority(record)
    return record.get('rights') == RIGHTS_OPS


#: The SysConfig knob that turns refusal on. Until it is true the gates
#: observe: a refusal they would have made is logged and the request goes
#: through, so the rule is proven against live traffic before it bites.
ENFORCE_KEY = 'authority_enforce'

JOIN_URL = 'https://eic.github.io/documentation/getstarted.html'


def enforcing():
    """Whether the gates refuse, or only observe."""
    from .models import SysConfig

    return bool(SysConfig.get_setting(ENFORCE_KEY, False))


def refusal_text(username):
    """What a person who may not act is told, with the way forward.

    GitHub has no self-service join, so the text names the login that was
    checked and links the procedure rather than leaving a bare refusal.
    """
    record = get_authority(username)
    login = record['github'] or username
    if record['rights'] == RIGHTS_READ:
        return ('Your account is set to read on this system: you may read '
                'monitoring information, and actions against the production '
                'system are withheld. An administrator can change that on '
                'the User admin page.')
    if record['eic'] is False:
        return (f'Your GitHub account {login} is not a member of the eic '
                'organization. ePIC production monitoring requires membership '
                'for actions against the production system; reading '
                'monitoring information does not. The joining procedure is on '
                f'the ePIC Software & Computing Get Started page, {JOIN_URL} '
                '(see "Join GitHub"). Once you have been added, sign in again '
                'and your account will work.')
    return (f'No eic organization membership has been recorded for {login}. '
            'Membership is checked when you sign in through '
            'epic-devcloud.org; reading monitoring information needs no '
            'membership, actions against the production system do. The '
            'joining procedure is on the ePIC Software & Computing Get '
            f'Started page, {JOIN_URL} (see "Join GitHub").')


def _write(username, changes):
    from .models import UserPreference

    if not username:
        raise AuthorityError('username is required')
    with transaction.atomic():
        row, _ = UserPreference.objects.select_for_update().get_or_create(
            username=username, defaults={'prefs': {}})
        prefs = dict(row.prefs or {})
        stored = prefs.get(AUTHORITY_KEY)
        record = dict(stored) if isinstance(stored, dict) else {}
        for field, value in changes.items():
            if value is None:
                record.pop(field, None)
            else:
                record[field] = value
        prefs[AUTHORITY_KEY] = record
        row.prefs = prefs
        row.save(update_fields=['prefs', 'updated_at'])
    return _record_from(prefs[AUTHORITY_KEY])


def set_eic(username, eic, github=None):
    """Record observed `eic` organisation membership. The sweep's setter.

    Writes the observed field and the GitHub login it was observed for, and
    nothing else: a sign-in cannot alter granted rights. ``eic=None`` clears
    the field back to unset.

    The account need not exist: the record is keyed by username string, so an
    account created later already carries what was written for the name.
    """
    from django.utils import timezone

    if eic is not None and not isinstance(eic, bool):
        raise AuthorityError(
            f'eic must be true, false or null, got {type(eic).__name__}')
    # The sweep runs at sign-in, so the moment membership was last observed
    # is the moment the person last signed in — the only sign-in time this
    # side can know, since a proxied request never opens a session here.
    changes = {'eic': eic, 'eic_at': timezone.now().isoformat()}
    if github is not None:
        if not isinstance(github, str):
            raise AuthorityError(
                f'github must be a string, got {type(github).__name__}')
        changes['github'] = github or None
    return _write(username, changes)


def set_rights(username, rights):
    """Grant, restrict, or clear an account's rights. A person's setter.

    Writes the granted field and nothing else: an administrative decision
    cannot assert a GitHub fact. ``rights=None`` clears the field back to
    unset, returning the account to drawing authority from ``eic`` alone.
    """
    if rights is not None and rights not in RIGHTS_VALUES:
        raise AuthorityError(
            f"rights must be one of {', '.join(RIGHTS_VALUES)}, or null; "
            f'got {rights!r}')
    return _write(username, {'rights': rights})
