"""Snapper episode REST adapters (snapper-ai docs/EPISODES.md).

Thin transports over ``snapper_ai.episodes``. The write endpoints are
token-authenticated — the episode builder agent posts open, append,
and close with its builder identity. The read endpoints are read-open
like the monitor's other read surfaces; errors are explicit JSON.
"""

from dataclasses import asdict

from django.http import JsonResponse
from rest_framework.authentication import (SessionAuthentication,
                                           TokenAuthentication)
from rest_framework.decorators import (api_view, authentication_classes,
                                       permission_classes)
from rest_framework.permissions import IsAuthenticated

from snapper_ai.episodes import (BuilderNotAuthorized, EpisodeClosed,
                                 EpisodeNotFound, InvalidEpisode,
                                 append_events, close_episode,
                                 episode_record, list_episodes,
                                 open_episode)


def _run_write(call):
    try:
        update = call()
    except InvalidEpisode as e:
        return JsonResponse({'error': str(e)}, status=400)
    except EpisodeNotFound as e:
        return JsonResponse({'error': str(e)}, status=404)
    except EpisodeClosed as e:
        return JsonResponse({'error': str(e)}, status=409)
    except BuilderNotAuthorized as e:
        return JsonResponse({'error': str(e)}, status=403)
    return JsonResponse(asdict(update))


@api_view(['POST'])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def episodes_open(request):
    """POST /api/snapper/episodes/open/"""
    body = request.data
    return _run_write(lambda: open_episode(
        scope=body.get('scope'),
        episode_id=body.get('episode_id'),
        builder_identity=body.get('builder_identity'),
        started_at=body.get('started_at'),
        label=body.get('label') or '',
        kind=body.get('kind') or '',
        summary=body.get('summary'),
    ))


@api_view(['POST'])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def episodes_append(request):
    """POST /api/snapper/episodes/append/"""
    body = request.data
    return _run_write(lambda: append_events(
        scope=body.get('scope'),
        episode_id=body.get('episode_id'),
        builder_identity=body.get('builder_identity'),
        events=body.get('events'),
        participants=body.get('participants'),
    ))


@api_view(['POST'])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def episodes_close(request):
    """POST /api/snapper/episodes/close/"""
    body = request.data
    return _run_write(lambda: close_episode(
        scope=body.get('scope'),
        episode_id=body.get('episode_id'),
        builder_identity=body.get('builder_identity'),
        ended_at=body.get('ended_at'),
        summary=body.get('summary'),
    ))


def episodes_list_view(request, scope):
    """GET /api/snapper/<scope>/episodes/?limit=N"""
    try:
        limit = int(request.GET.get('limit') or 50)
    except ValueError:
        return JsonResponse({'error': 'limit must be an integer'}, status=400)
    try:
        episodes = list_episodes(scope, limit=limit)
    except InvalidEpisode as e:
        return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'count': len(episodes), 'episodes': episodes},
                        json_dumps_params={'default': str})


def episode_detail_view(request, scope, episode_id):
    """GET /api/snapper/<scope>/episodes/<episode_id>/"""
    try:
        record = episode_record(scope, episode_id)
    except InvalidEpisode as e:
        return JsonResponse({'error': str(e)}, status=400)
    except EpisodeNotFound as e:
        return JsonResponse({'error': str(e)}, status=404)
    return JsonResponse(record, json_dumps_params={'default': str})
