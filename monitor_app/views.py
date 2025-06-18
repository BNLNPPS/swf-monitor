from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticatedOrReadOnly
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from django.contrib.auth.decorators import login_required
from .models import MonitoredItem
from .serializers import MonitoredItemSerializer
from .forms import MonitoredItemForm

# Create your views here.
def index(request):
    items = MonitoredItem.objects.all()
    return render(request, 'monitor_app/index.html', {'items': items})

@login_required
def monitored_item_create(request):
    if request.method == 'POST':
        form = MonitoredItemForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('monitor_app:index')
    else:
        form = MonitoredItemForm()
    return render(request, 'monitor_app/monitored_item_form.html', {'form': form})

@login_required
def monitored_item_update(request, pk):
    item = get_object_or_404(MonitoredItem, pk=pk)
    if request.method == 'POST':
        form = MonitoredItemForm(request.POST, instance=item)
        if form.is_valid():
            form.save()
            return redirect('monitor_app:index')
    else:
        form = MonitoredItemForm(instance=item)
    return render(request, 'monitor_app/monitored_item_form.html', {'form': form})

@login_required
def monitored_item_delete(request, pk):
    item = get_object_or_404(MonitoredItem, pk=pk)
    if request.method == 'POST':
        item.delete()
        return redirect('monitor_app:index')
    return render(request, 'monitor_app/monitored_item_confirm_delete.html', {'item': item})


class MonitoredItemViewSet(viewsets.ModelViewSet):
    queryset = MonitoredItem.objects.all()
    serializer_class = MonitoredItemSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['post'], url_path='update_status', permission_classes=[AllowAny], authentication_classes=[])
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
