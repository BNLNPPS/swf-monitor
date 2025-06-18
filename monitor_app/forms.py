from django import forms
from .models import MonitoredItem

class MonitoredItemForm(forms.ModelForm):
    class Meta:
        model = MonitoredItem
        fields = ['name', 'description', 'status', 'agent_url']
