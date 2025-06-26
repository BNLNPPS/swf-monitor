from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.db.models import Count
from django.core.paginator import Paginator
from rest_framework import viewsets, generics
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import update_session_auth_hash
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied
from .models import SystemAgent, AppLog
from .serializers import SystemAgentSerializer, AppLogSerializer
from .forms import SystemAgentForm
from rest_framework.views import APIView

# Create your views here.
def home(request):
    if request.user.is_authenticated:
        return redirect('monitor_app:index')
    return render(request, 'monitor_app/welcome.html')

def about(request):
    return render(request, 'monitor_app/about.html')

@login_required
def index(request):
    agents = SystemAgent.objects.all()
    return render(request, 'monitor_app/index.html', {'agents': agents})

def staff_member_required(view_func):
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped_view

@login_required
@staff_member_required
def system_agent_create(request):
    if request.method == 'POST':
        form = SystemAgentForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('monitor_app:index')
    else:
        form = SystemAgentForm()
    return render(request, 'monitor_app/system_agent_form.html', {'form': form})

@login_required
@staff_member_required
def system_agent_update(request, pk):
    agent = get_object_or_404(SystemAgent, pk=pk)
    if request.method == 'POST':
        form = SystemAgentForm(request.POST, instance=agent)
        if form.is_valid():
            form.save()
            return redirect('monitor_app:index')
    else:
        form = SystemAgentForm(instance=agent)
    return render(request, 'monitor_app/system_agent_form.html', {'form': form})

@login_required
@staff_member_required
def system_agent_delete(request, pk):
    agent = get_object_or_404(SystemAgent, pk=pk)
    if request.method == 'POST':
        agent.delete()
        return redirect('monitor_app:index')
    return render(request, 'monitor_app/system_agent_confirm_delete.html', {'agent': agent})

@login_required
def get_system_agents_data(request):
    agents = SystemAgent.objects.all()
    data = {
        'agents': [{'id': agent.id, 'name': agent.name, 'status': agent.status} for agent in agents]
    }
    return JsonResponse(data)

@login_required
def account_view(request):
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # Important!
            messages.success(request, 'Your password was successfully updated!')
            return redirect('monitor_app:account')
        else:
            messages.error(request, 'Please correct the error below.')
    else:
        form = PasswordChangeForm(request.user)
    return render(request, 'monitor_app/account.html', {
        'form': form,
        'user': request.user
    })


class SystemAgentViewSet(viewsets.ModelViewSet):
    queryset = SystemAgent.objects.all()
    serializer_class = SystemAgentSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

class AppLogViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows logs to be viewed or created.
    """
    queryset = AppLog.objects.all()
    serializer_class = AppLogSerializer
    permission_classes = [AllowAny] # For now, allow any client to post logs

@login_required
def log_summary(request):
    """
    Displays a summary of log entries, grouped by application, instance, and level.
    """
    log_summary_data = (
        AppLog.objects.values("app_name", "instance_name", "level_name")
        .annotate(count=Count("id"))
        .order_by("app_name", "instance_name", "level_name")
    )

    # Restructure the data for the template
    summary = {}
    for item in log_summary_data:
        app_key = item["app_name"]
        instance_key = item["instance_name"]
        level = item["level_name"]
        count = item["count"]

        if app_key not in summary:
            summary[app_key] = {}
        if instance_key not in summary[app_key]:
            summary[app_key][instance_key] = {
                "levels": {},
                "total": 0,
            }
        
        summary[app_key][instance_key]["levels"][level] = count
        summary[app_key][instance_key]["total"] += count

    context = {"summary": summary}
    return render(request, "monitor_app/log_summary.html", context)

@login_required
def log_list(request):
    """
    Displays a paginated list of all log entries, with filtering.
    """
    log_list = AppLog.objects.all()

    # Filtering
    app_name = request.GET.get('app_name')
    instance_name = request.GET.get('instance_name')

    if app_name:
        log_list = log_list.filter(app_name=app_name)
    if instance_name:
        log_list = log_list.filter(instance_name=instance_name)

    # Get distinct app and instance names for filter dropdowns
    app_names = AppLog.objects.values_list('app_name', flat=True).distinct()
    instance_names = AppLog.objects.values_list('instance_name', flat=True).distinct()

    # Pagination
    paginator = Paginator(log_list, 25) # Show 25 logs per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'app_names': app_names,
        'instance_names': instance_names,
        'selected_app': app_name,
        'selected_instance': instance_name,
    }
    return render(request, 'monitor_app/log_list.html', context)

class LogSummaryView(APIView):
    """
    API endpoint that provides a summary of logs grouped by app and instance, with error rollups.
    """
    def get(self, request, format=None):
        # Get all unique app/instance pairs
        logs = AppLog.objects.all()
        summary = {}
        for log in logs.values('app_name', 'instance_name').distinct():
            app = log['app_name']
            instance = log['instance_name']
            if app not in summary:
                summary[app] = {}
            # Aggregate error counts by level for this app/instance
            error_counts = (
                AppLog.objects.filter(app_name=app, instance_name=instance)
                .values('level_name')
                .annotate(count=Count('id'))
            )
            # Get recent errors (last 5)
            recent_errors = list(
                AppLog.objects.filter(app_name=app, instance_name=instance, level_name__in=['ERROR', 'CRITICAL'])
                .order_by('-timestamp')[:5]
                .values('timestamp', 'level_name', 'message', 'module', 'func_name', 'line_no')
            )
            summary[app][instance] = {
                'error_counts': {e['level_name']: e['count'] for e in error_counts},
                'recent_errors': recent_errors,
            }
        return Response(summary, status=status.HTTP_200_OK)
