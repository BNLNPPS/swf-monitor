"""What's new REST surface (docs/WHATS_NEW.md): JSON in, JSON out, never a
redirect, so every write works through the swf-remote proxy
(EXTERNAL_ACCESS.md). Thin peers over ``help.whats_new``; the authority
gate admits only people who may act (AUTHORITY.md)."""
from django.urls import reverse
from rest_framework.authentication import (
    SessionAuthentication, TokenAuthentication,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from monitor_app.middleware import TunnelAuthentication

from . import whats_new as wn


class _HelpApiView(APIView):
    authentication_classes = [TunnelAuthentication, SessionAuthentication,
                              TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def _run(self, fn):
        try:
            return Response(fn())
        except wn.WhatsNewError as e:
            return Response({'detail': e.detail}, status=e.status)


def _fields(data):
    return {k: data[k] for k in wn.FIELDS if k in data}


def _item_path(slug):
    return reverse('help:whats_new_item', args=[slug])


class ItemCreateView(_HelpApiView):
    def post(self, request):
        """Create an item from its fields; answers the slug and page path."""
        def go():
            entry, _log_id = wn.item_create(
                _fields(request.data), changed_by=request.user.username)
            slug = entry.data['entry_id']
            return {'slug': slug, 'url': _item_path(slug)}
        return self._run(go)


class ItemEditView(_HelpApiView):
    def post(self, request, slug):
        """Edit any of an item's fields."""
        def go():
            wn.item_edit(slug, _fields(request.data),
                         changed_by=request.user.username)
            return {'slug': slug, 'url': _item_path(slug)}
        return self._run(go)


class ItemRestoreView(_HelpApiView):
    def post(self, request, slug):
        """Restore a version (``version``) as the current state."""
        def go():
            try:
                num = int(request.data.get('version'))
            except (TypeError, ValueError):
                raise wn.WhatsNewError('version must be a number')
            wn.item_restore(slug, num, changed_by=request.user.username)
            return {'slug': slug, 'url': _item_path(slug)}
        return self._run(go)


class ItemArchiveView(_HelpApiView):
    def post(self, request, slug):
        """Archive an item (``archived``: true, the default) or return it."""
        def go():
            entry = wn.item_archive(
                slug, changed_by=request.user.username,
                archived=request.data.get('archived', True) not in (False, 'false', 0))
            return {'slug': slug, 'archived': entry.archived}
        return self._run(go)


class ItemVoteView(_HelpApiView):
    def post(self, request, slug):
        """Vote on an item: ``value`` 1, -1, or 0 to withdraw."""
        def go():
            score, mine = wn.vote(slug, username=request.user.username,
                                  value=request.data.get('value'))
            return {'slug': slug, 'score': score, 'my_vote': mine}
        return self._run(go)


class CommentAddView(_HelpApiView):
    def post(self, request, slug):
        """Add a comment (``text``, Markdown) to an item's thread."""
        def go():
            c = wn.comment_add(slug, request.data.get('text'),
                               author=request.user.username)
            return {'id': c.pk, 'slug': slug}
        return self._run(go)


class CommentEditView(_HelpApiView):
    def post(self, request, comment_id):
        """Edit a comment's text; its author only."""
        def go():
            c = wn.comment_edit(comment_id, request.data.get('text'),
                                editor=request.user.username)
            return {'id': c.pk}
        return self._run(go)
