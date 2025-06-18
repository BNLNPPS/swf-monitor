from django.shortcuts import render
from django.http import HttpResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from .models import MonitoredItem
from .serializers import MonitoredItemSerializer

# Create your views here.
def index(request):
    return render(request, 'monitor_app/index.html')

class MonitoredItemViewSet(viewsets.ModelViewSet):
    queryset = MonitoredItem.objects.all()
    serializer_class = MonitoredItemSerializer

    @action(detail=False, methods=['post'], url_path='update_status', permission_classes=[AllowAny])
    def update_status(self, request):
        """
        Custom endpoint for agents to update their status and heartbeat.
        Expects: {"name": <agent_name>, "status": <status>, "last_heartbeat": <optional ISO datetime>}
        """
        name = request.data.get('name')
        status_value = request.data.get('status')
        last_heartbeat = request.data.get('last_heartbeat')
        if not name or not status_value:
            return Response({'error': 'Missing required fields: name and status'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            item = MonitoredItem.objects.get(name=name)
        except MonitoredItem.DoesNotExist:
            return Response({'error': f'MonitoredItem with name {name} not found'}, status=status.HTTP_404_NOT_FOUND)
        item.status = status_value
        if last_heartbeat:
            item.last_heartbeat = last_heartbeat
        item.save()
        serializer = self.get_serializer(item)
        return Response(serializer.data, status=status.HTTP_200_OK)
