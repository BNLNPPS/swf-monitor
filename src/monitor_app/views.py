from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.db.models import Count, Max
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
from .serializers import SystemAgentSerializer, AppLogSerializer, LogSummarySerializer
from .forms import SystemAgentForm
from rest_framework.views import APIView
from django.apps import apps
from django.db import connection
from django.utils import timezone

# Create your views here.
def home(request):
    if request.user.is_authenticated:
        return redirect('monitor_app:authenticated_home')
    return render(request, 'monitor_app/welcome.html')

@login_required
def authenticated_home(request):
    return render(request, 'monitor_app/authenticated_home.html')

def about(request):
    return render(request, 'monitor_app/about.html')

@login_required
def index(request):
    agents = SystemAgent.objects.all()

    # Filtering
    agent_type = request.GET.get('agent_type')
    status = request.GET.get('status')
    if agent_type:
        agents = agents.filter(agent_type=agent_type)
    if status:
        agents = agents.filter(status=status)

    # Get unique agent types and statuses for filter links
    agent_types = SystemAgent.objects.values_list('agent_type', flat=True)
    agent_types = sorted(set([t for t in agent_types if t]), key=lambda x: x.lower())
    statuses = sorted([s[0] for s in SystemAgent.STATUS_CHOICES], key=lambda x: x.lower())

    columns = [
        {"name": "instance_name", "label": "Agent"},
        {"name": "agent_type", "label": "Type"},
        {"name": "status", "label": "Status"},
        {"name": "last_heartbeat", "label": "Last Heartbeat"},
        {"name": "agent_url", "label": "Agent URL"},
        {"name": "actions", "label": "Actions"},
    ]

    context = {
        'agents': agents,
        'agent_types': agent_types,
        'statuses': statuses,
        'selected_agent_type': agent_type,
        'selected_status': status,
        'columns': columns,
    }
    return render(request, 'monitor_app/index.html', context)

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

    # Get latest timestamp for each (app_name, instance_name)
    latest_timestamps = AppLog.objects.values("app_name", "instance_name").annotate(latest=Max("timestamp"))
    latest_map = {}
    for item in latest_timestamps:
        latest_map[(item["app_name"], item["instance_name"])] = item["latest"]

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
                "latest_timestamp": latest_map.get((app_key, instance_key)),
            }
        summary[app_key][instance_key]["levels"][level] = count
        summary[app_key][instance_key]["total"] += count

    # Only provide 'summary' in context, as requested
    context = {"summary": summary}
    return render(request, "monitor_app/log_summary.html", context)

@login_required
def log_list(request):
    """
    Displays a paginated list of all log entries, with filtering.
    """
    from django.utils.dateparse import parse_datetime
    log_list = AppLog.objects.all()

    # Filtering
    app_name = request.GET.get('app_name')
    instance_name = request.GET.get('instance_name')
    start_time = request.GET.get('start_time')
    end_time = request.GET.get('end_time')

    if app_name:
        log_list = log_list.filter(app_name=app_name)
    if instance_name:
        log_list = log_list.filter(instance_name=instance_name)
    if start_time:
        dt = parse_datetime(start_time)
        if dt:
            log_list = log_list.filter(timestamp__gte=dt)
    if end_time:
        dt = parse_datetime(end_time)
        if dt:
            log_list = log_list.filter(timestamp__lte=dt)

    # Get distinct app and instance names for filter links, sorted alphabetically, case-insensitive, unique
    app_names_qs = AppLog.objects.values_list('app_name', flat=True)
    instance_names_qs = AppLog.objects.values_list('instance_name', flat=True)
    app_names = sorted(set([name for name in app_names_qs if name]), key=lambda x: x.lower())
    instance_names = sorted(set([name for name in instance_names_qs if name]), key=lambda x: x.lower())

    # Pagination
    paginator = Paginator(log_list, 25) # Show 25 logs per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Get first and last log for timestamp range display
    logs_list = list(page_obj.object_list)
    first_log = logs_list[0] if logs_list else None
    last_log = logs_list[-1] if logs_list else None

    # Always provide 'page_obj' in context, even if empty
    context = {
        'page_obj': page_obj,
        'app_names': app_names,
        'instance_names': instance_names,
        'selected_app': app_name,
        'selected_instance': instance_name,
        'first_log': first_log,
        'last_log': last_log,
    }
    return render(request, 'monitor_app/log_list.html', context)

class LogSummaryView(generics.ListAPIView):
    """
    API endpoint that provides a summary of logs grouped by app and instance, with error rollups.
    """
    serializer_class = LogSummarySerializer
    permission_classes = [AllowAny]  # or your desired permission class
    queryset = AppLog.objects.all()  # Provide a queryset for DRF permissions

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

@login_required
def database_overview(request):
    tables = []
    for model in apps.get_models():
        table_info = {'name': model._meta.db_table, 'count': 0, 'last_insert': None}
        try:
            count = model.objects.count()
            table_info['count'] = count
            # Try to get last insertion time if a DateTimeField exists
            dt_fields = [f.name for f in model._meta.fields if f.get_internal_type() == 'DateTimeField']
            if dt_fields:
                last_obj = model.objects.order_by('-' + dt_fields[0]).first()
                if last_obj:
                    table_info['last_insert'] = getattr(last_obj, dt_fields[0])
        except Exception:
            pass  # Table may not exist or be accessible
        tables.append(table_info)
    tables = sorted(tables, key=lambda t: t['name'])
    tables = [t for t in tables if t['name'].startswith('swf_')]
    return render(request, 'monitor_app/database_overview.html', {'tables': tables})

from django.http import Http404

@login_required
def database_table_list(request, table_name):
    if not table_name.startswith('swf_'):
        raise Http404()
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT * FROM "{table_name}" LIMIT 100')
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    # Identify datetime columns using Django model if available
    dt_columns = []
    for model in apps.get_models():
        if model._meta.db_table == table_name:
            dt_columns = [f.name for f in model._meta.fields if f.get_internal_type() == 'DateTimeField']
            break
    def get_item(row, key):
        return row.get(key, '')
    from django.template.defaulttags import register
    register.filter('get_item', get_item)
    return render(request, 'monitor_app/database_table_list.html', {
        'table_name': table_name,
        'columns': columns,
        'rows': rows,
        'dt_columns': dt_columns,
    })
