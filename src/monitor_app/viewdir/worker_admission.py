"""Worker starts behind the production dashboard's slots/hour link."""
from django.shortcuts import render
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from monitor_app.models import PandaQueue
from monitor_app.short_names import short_name
from monitor_app.worker_admission import product, startup_evidence
from swf_epicprod.regulation import RETIRED_QUEUES


@api_view(['GET'])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def osg_image_demand(request):
    from monitor_app.worker_admission import osg_image_demand as collect
    try:
        limits = {key: int(request.GET[key]) for key in ('memory_mb', 'cores')}
        if any(value < 1 or value > 2**31 - 1 for value in limits.values()):
            raise ValueError('invalid limits')
    except (KeyError, ValueError):
        return Response({'detail': 'positive memory_mb and cores are required'}, status=400)
    return Response(collect(**limits))


def worker_admission(request):
    selected = request.GET.get('site') or 'BNL_OSG_PanDA_1'
    queues = [name.strip() for name in selected.split(',')
              if name.strip() and name.strip() not in RETIRED_QUEUES]
    queues = queues or ['BNL_OSG_PanDA_1']
    try:
        hours = int(request.GET.get('hours', 1))
    except (TypeError, ValueError):
        hours = 1
    if hours not in (1, 3, 6, 12):
        hours = 1
    result = product(queues, hours, refresh=request.GET.get('refresh') == '1')
    data = result.get('value') or None
    return render(request, 'monitor_app/worker_admission.html', {
        'queue_label': ', '.join(short_name(name) for name in queues),
        'selected_queue': ','.join(queues),
        'queue_choices': list(PandaQueue.objects.exclude(queue_name__in=RETIRED_QUEUES)
                              .order_by('queue_name').values_list('queue_name', flat=True)),
        'hours': hours, 'hour_options': (1, 3, 6, 12), 'data': data,
        'evidence': startup_evidence(queues), 'failure': (data or {}).get('error'),
    })
