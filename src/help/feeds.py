"""The What's new RSS feed (docs/WHATS_NEW.md, The feed).

RSS 2.0, newest shipped first, from the same list the page shows. Links
point at the face the reader came through: epic-devcloud.org for a
request through the swf-remote proxy, the monitor's own address
otherwise.
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.contrib.syndication.views import Feed
from django.urls import reverse

from monitor_app.middleware import is_tunnel_request
from monitor_app.models import external_face_base_url

from . import whats_new as wn

ITEMS = 50
_EASTERN = ZoneInfo('America/New_York')


class WhatsNewFeed(Feed):
    title = "What's new in ePIC workflow and production"
    description = ('Developments in the ePIC workflow and production system '
                   'that users, operators and developers should know about, '
                   'newest first.')

    # One Feed instance serves every request, so the request travels as the
    # feed's object and each item carries its own link.
    def get_object(self, request, *args, **kwargs):
        return request

    @staticmethod
    def _url(request, name, *args):
        path = reverse(name, args=args)
        if is_tunnel_request(request):
            # The internal script prefix means nothing on the external face.
            script = request.META.get('SCRIPT_NAME', '')
            if script and path.startswith(script):
                path = path[len(script):]
            return f"{external_face_base_url().rstrip('/')}/prod{path}"
        return request.build_absolute_uri(path)

    def link(self, request):
        return self._url(request, 'help:whats_new')

    def feed_url(self, request):
        return self._url(request, 'help:whats_new_feed')

    def items(self, request):
        rows = wn.list_items()[:ITEMS]
        for row in rows:
            row['link'] = self._url(request, 'help:whats_new_item', row['slug'])
        return rows

    def item_title(self, item):
        return item['title']

    def item_description(self, item):
        level = wn.IMPORTANCE_LABELS.get(item['importance'], item['importance'])
        return (f"{item['summary']}\n\nImportance: {level}. "
                f"Tags: {' '.join(item['tags'])}. Contact: {item['contact']}.")

    def item_link(self, item):
        return item['link']

    def item_pubdate(self, item):
        return datetime.combine(datetime.fromisoformat(item['shipped']).date(),
                                time(12, 0), tzinfo=_EASTERN)

    def item_categories(self, item):
        return item['tags']
