from django import forms
from .models import SystemAgent

class SystemAgentForm(forms.ModelForm):
    class Meta:
        model = SystemAgent
        fields = ['instance_name', 'agent_type', 'description', 'status', 'agent_url']
