from django.shortcuts import render
from django.http import HttpResponse
from rest_framework import viewsets
from .models import MonitoredItem
from .serializers import MonitoredItemSerializer

# Create your views here.
def index(request):
    return render(request, 'monitor_app/index.html')

class MonitoredItemViewSet(viewsets.ModelViewSet):
    queryset = MonitoredItem.objects.all()
    serializer_class = MonitoredItemSerializer
