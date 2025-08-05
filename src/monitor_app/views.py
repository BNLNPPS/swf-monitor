from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.db.models import Count, Max
from django.core.paginator import Paginator
from rest_framework import viewsets, generics
from rest_framework.decorators import action, api_view, authentication_classes, permission_classes
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
from .models import SystemAgent, AppLog, Run, StfFile, Subscriber, MessageQueueDispatch, PersistentState
from .workflow_models import STFWorkflow, AgentWorkflowStage, WorkflowMessage, WorkflowStatus, AgentType
from .serializers import (
    SystemAgentSerializer, AppLogSerializer, LogSummarySerializer, 
    STFWorkflowSerializer, AgentWorkflowStageSerializer, WorkflowMessageSerializer,
    RunSerializer, StfFileSerializer, SubscriberSerializer, MessageQueueDispatchSerializer
)
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
    """A simple landing page for authenticated users."""
    return render(request, 'monitor_app/index.html')

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
        'agents': [{'id': agent.id, 'name': agent.instance_name, 'status': agent.status} for agent in agents]
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

    @action(detail=False, methods=['post'], url_path='heartbeat')
    def heartbeat(self, request):
        """
        Custom action for agents to register themselves and send heartbeats.
        This will create or update an agent entry.
        """
        instance_name = request.data.get('instance_name')
        if not instance_name:
            return Response({"instance_name": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST)
        
        # Use update_or_create to handle both registration and heartbeats
        agent, created = SystemAgent.objects.update_or_create(
            instance_name=instance_name,
            defaults={
                'agent_type': request.data.get('agent_type', 'other'),
                'description': request.data.get('description', ''),
                'status': request.data.get('status', 'OK'),
                'agent_url': request.data.get('agent_url', None),
                'last_heartbeat': timezone.now(),
            }
        )
        
        # Return the full agent data
        return Response(self.get_serializer(agent).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class STFWorkflowViewSet(viewsets.ModelViewSet):
    """API endpoint for STF Workflows."""
    queryset = STFWorkflow.objects.all()
    serializer_class = STFWorkflowSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

class AgentWorkflowStageViewSet(viewsets.ModelViewSet):
    """API endpoint for Agent Workflow Stages."""
    queryset = AgentWorkflowStage.objects.all()
    serializer_class = AgentWorkflowStageSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

class WorkflowMessageViewSet(viewsets.ModelViewSet):
    """API endpoint for Workflow Messages."""
    queryset = WorkflowMessage.objects.all()
    serializer_class = WorkflowMessageSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]


class AppLogViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows logs to be viewed or created.
    """
    queryset = AppLog.objects.all()
    serializer_class = AppLogSerializer
    permission_classes = [AllowAny] # For now, allow any client to post logs


class RunViewSet(viewsets.ModelViewSet):
    """API endpoint for data-taking runs."""
    queryset = Run.objects.all()
    serializer_class = RunSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]


class StfFileViewSet(viewsets.ModelViewSet):
    """API endpoint for STF file tracking."""
    queryset = StfFile.objects.all()
    serializer_class = StfFileSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]


class SubscriberViewSet(viewsets.ModelViewSet):
    """API endpoint for message queue subscribers."""
    queryset = Subscriber.objects.all()
    serializer_class = SubscriberSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]


class MessageQueueDispatchViewSet(viewsets.ModelViewSet):
    """API endpoint for message queue dispatches."""
    queryset = MessageQueueDispatch.objects.all()
    serializer_class = MessageQueueDispatchSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

@login_required
def log_summary(request):
    """
    Professional log summary view using server-side DataTables for optimal performance.
    Replaced the old client-side implementation.
    """
    # Get filter parameters (for initial state)
    app_name = request.GET.get('app_name')
    instance_name = request.GET.get('instance_name')
    levelname = request.GET.get('levelname')
    
    # Get distinct app and instance names for filter links
    app_names_qs = AppLog.objects.values_list('app_name', flat=True)
    instance_names_qs = AppLog.objects.values_list('instance_name', flat=True)
    app_names = sorted(set([name for name in app_names_qs if name]), key=lambda x: x.lower())
    instance_names = sorted(set([name for name in instance_names_qs if name]), key=lambda x: x.lower())

    # Column definitions for DataTables
    columns = [
        {'name': 'app_name', 'title': 'Application Name', 'orderable': True},
        {'name': 'instance_name', 'title': 'Instance Name', 'orderable': True},
        {'name': 'latest_timestamp', 'title': 'Latest Timestamp', 'orderable': True},
        {'name': 'info_count', 'title': 'INFO', 'orderable': True},
        {'name': 'warning_count', 'title': 'WARNING', 'orderable': True},
        {'name': 'error_count', 'title': 'ERROR', 'orderable': True},
        {'name': 'critical_count', 'title': 'CRITICAL', 'orderable': True},
        {'name': 'debug_count', 'title': 'DEBUG', 'orderable': True},
        {'name': 'total_count', 'title': 'Total', 'orderable': True},
        {'name': 'actions', 'title': 'Actions', 'orderable': False},
    ]

    context = {
        'table_title': 'Log Summary',
        'table_description': 'Server-side aggregated log counts by application and instance, with level breakdowns and drill-down access.',
        'ajax_url': reverse('monitor_app:log_summary_datatable_ajax'),
        'columns': columns,
        'app_names': app_names,
        'instance_names': instance_names,
        'selected_app': app_name,
        'selected_instance': instance_name,
        'selected_levelname': levelname,
    }
    return render(request, 'monitor_app/log_summary_ajax.html', context)




def log_summary_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of log summary data.
    Handles pagination, searching, ordering, and filtering.
    """
    from django.http import JsonResponse
    from django.db.models import Q, Count, Max
    import json
    
    # DataTables parameters
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 100))
    search_value = request.GET.get('search[value]', '').strip()
    
    # Column definitions (must match template column order)
    columns = ['app_name', 'instance_name', 'latest_timestamp', 'info_count', 'warning_count', 'error_count', 'critical_count', 'debug_count', 'total_count', 'actions']
    
    # Order parameters
    order_column_idx = int(request.GET.get('order[0][column]', 0))
    order_direction = request.GET.get('order[0][dir]', 'desc')
    order_column = columns[order_column_idx] if 0 <= order_column_idx < len(columns) else 'app_name'
    
    # Apply existing filters (app_name, instance_name, levelname)
    app_name = request.GET.get('app_name')
    instance_name = request.GET.get('instance_name')
    levelname = request.GET.get('levelname')
    
    # Start with base queryset
    base_queryset = AppLog.objects.all()
    if app_name:
        base_queryset = base_queryset.filter(app_name=app_name)
    if instance_name:
        base_queryset = base_queryset.filter(instance_name=instance_name)
    if levelname:
        base_queryset = base_queryset.filter(levelname=levelname)
    
    # Create summary queryset - one row per app/instance pair
    summary_queryset = (
        base_queryset.values('app_name', 'instance_name')
        .annotate(
            latest_timestamp=Max('timestamp'),
            info_count=Count('id', filter=Q(levelname='INFO')),
            warning_count=Count('id', filter=Q(levelname='WARNING')),
            error_count=Count('id', filter=Q(levelname='ERROR')),
            critical_count=Count('id', filter=Q(levelname='CRITICAL')),
            debug_count=Count('id', filter=Q(levelname='DEBUG')),
            total_count=Count('id')
        )
    )
    
    # Total count before search filtering
    records_total_queryset = (
        AppLog.objects.values('app_name', 'instance_name')
        .annotate(count=Count('id'))
    )
    records_total = records_total_queryset.count()
    records_filtered = summary_queryset.count()
    
    # Apply search filter
    if search_value:
        search_q = Q(app_name__icontains=search_value) | Q(instance_name__icontains=search_value)
        summary_queryset = summary_queryset.filter(search_q)
        records_filtered = summary_queryset.count()
    
    # Apply ordering
    if order_column == 'latest_timestamp':
        order_by = f'{"-" if order_direction == "desc" else ""}latest_timestamp'
    elif order_column in ['info_count', 'warning_count', 'error_count', 'critical_count', 'debug_count', 'total_count']:
        order_by = f'{"-" if order_direction == "desc" else ""}{order_column}'
    else:
        # For app_name, instance_name, actions
        order_by = f'{"-" if order_direction == "desc" else ""}{order_column}'
    
    summary_queryset = summary_queryset.order_by(order_by)
    
    # Apply pagination
    summary_data = summary_queryset[start:start + length]
    
    # Format data for DataTables
    data = []
    for item in summary_data:
        # Format timestamp
        timestamp_str = item['latest_timestamp'].strftime('%Y%m%d %H:%M:%S') if item['latest_timestamp'] else 'N/A'
        
        # Create app_name link (preserve filters)
        app_filter_url = f"?app_name={item['app_name']}"
        if instance_name:
            app_filter_url += f"&instance_name={instance_name}"
        app_name_link = f'<a href="/logs/{app_filter_url}">{item["app_name"]}</a>'
        
        # Create instance_name link (preserve filters)
        instance_filter_url = f"?instance_name={item['instance_name']}"
        if app_name:
            instance_filter_url += f"&app_name={app_name}"
        instance_name_link = f'<a href="/logs/{instance_filter_url}">{item["instance_name"]}</a>'
        
        # View logs action link
        view_logs_url = f'/logs/?app_name={item["app_name"]}&instance_name={item["instance_name"]}'
        view_logs_link = f'<a href="{view_logs_url}">View Logs</a>'
        
        # Create clickable drill-down links for each log level count
        from urllib.parse import urlencode
        
        def create_level_link(count, level):
            if count == 0:
                return str(count)
            params = {
                'app_name': item['app_name'],
                'instance_name': item['instance_name'],
                'levelname': level
            }
            url = f'/logs/?{urlencode(params)}'
            return f'<a href="{url}">{count}</a>'
        
        data.append([
            app_name_link,
            instance_name_link,
            timestamp_str,
            create_level_link(item['info_count'], 'INFO'),
            create_level_link(item['warning_count'], 'WARNING'),
            create_level_link(item['error_count'], 'ERROR'),
            create_level_link(item['critical_count'], 'CRITICAL'),
            create_level_link(item['debug_count'], 'DEBUG'),
            item['total_count'],  # Total count stays as plain number
            view_logs_link
        ])
    
    response = {
        'draw': draw,
        'recordsTotal': records_total,
        'recordsFiltered': records_filtered,
        'data': data
    }
    
    return JsonResponse(response)


@login_required
def log_list(request):
    """
    Professional log list view using server-side DataTables.
    Replaced the old pagination-based view for better performance and UX.
    """
    from django.utils.dateparse import parse_datetime
    
    # Get filter parameters (for initial state)
    app_name = request.GET.get('app_name')
    instance_name = request.GET.get('instance_name')
    levelname = request.GET.get('levelname')
    
    # Get distinct app and instance names for filter links
    app_names_qs = AppLog.objects.values_list('app_name', flat=True)
    instance_names_qs = AppLog.objects.values_list('instance_name', flat=True)
    app_names = sorted(set([name for name in app_names_qs if name]), key=lambda x: x.lower())
    instance_names = sorted(set([name for name in instance_names_qs if name]), key=lambda x: x.lower())

    # Column definitions for DataTables
    columns = [
        {'name': 'timestamp', 'title': 'Timestamp', 'orderable': True},
        {'name': 'app_name', 'title': 'App Name', 'orderable': True},
        {'name': 'instance_name', 'title': 'Instance Name', 'orderable': True},
        {'name': 'levelname', 'title': 'Level', 'orderable': True},
        {'name': 'message', 'title': 'Message', 'orderable': False},
        {'name': 'module', 'title': 'Module', 'orderable': True},
        {'name': 'funcname', 'title': 'Function', 'orderable': True},
    ]

    context = {
        'table_title': 'Log List',
        'table_description': 'Server-side processing with live search, sorting, and filtering across all log records.',
        'ajax_url': reverse('monitor_app:logs_datatable_ajax'),
        'columns': columns,
        'app_names': app_names,
        'instance_names': instance_names,
        'selected_app': app_name,
        'selected_instance': instance_name,
        'selected_levelname': levelname,
    }
    return render(request, 'monitor_app/log_list_clean.html', context)



def logs_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of logs.
    Handles pagination, searching, ordering, and filtering.
    """
    from django.http import JsonResponse
    from django.utils.dateparse import parse_datetime
    from django.db.models import Q
    import json
    
    # DataTables parameters
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 25))
    search_value = request.GET.get('search[value]', '').strip()
    
    # Column definitions (must match template column order)
    columns = ['timestamp', 'app_name', 'instance_name', 'levelname', 'message', 'module', 'funcname']
    
    # Order parameters
    order_column_idx = int(request.GET.get('order[0][column]', 0))
    order_direction = request.GET.get('order[0][dir]', 'desc')
    order_column = columns[order_column_idx] if 0 <= order_column_idx < len(columns) else 'timestamp'
    if order_direction == 'asc':
        order_by = order_column
    else:
        order_by = f'-{order_column}'
    
    # Build base queryset
    queryset = AppLog.objects.all()
    
    # Apply existing filters (app_name, instance_name, levelname, time_range)
    app_name = request.GET.get('app_name')
    instance_name = request.GET.get('instance_name')
    levelname = request.GET.get('levelname')
    start_time = request.GET.get('start_time')
    end_time = request.GET.get('end_time')
    
    if app_name:
        queryset = queryset.filter(app_name=app_name)
    if instance_name:
        queryset = queryset.filter(instance_name=instance_name)
    if levelname:
        queryset = queryset.filter(levelname=levelname)
    if start_time:
        dt = parse_datetime(start_time)
        if dt:
            queryset = queryset.filter(timestamp__gte=dt)
    if end_time:
        dt = parse_datetime(end_time)
        if dt:
            queryset = queryset.filter(timestamp__lte=dt)
    
    # Total count before search filtering
    records_total = AppLog.objects.count()
    records_filtered = queryset.count()
    
    # Apply search filter
    if search_value:
        search_q = Q(app_name__icontains=search_value) | \
                   Q(instance_name__icontains=search_value) | \
                   Q(levelname__icontains=search_value) | \
                   Q(message__icontains=search_value) | \
                   Q(module__icontains=search_value) | \
                   Q(funcname__icontains=search_value)
        queryset = queryset.filter(search_q)
        records_filtered = queryset.count()
    
    # Apply ordering and pagination
    queryset = queryset.order_by(order_by)[start:start + length]
    
    # Format data for DataTables
    data = []
    for log in queryset:
        # Format timestamp for display
        timestamp_str = log.timestamp.strftime('%Y%m%d %H:%M:%S')
        
        # Create app_name link (preserve filters)
        app_filter_url = f"?app_name={log.app_name}"
        if instance_name:
            app_filter_url += f"&instance_name={instance_name}"
        app_name_link = f'<a href="{app_filter_url}">{log.app_name}</a>'
        
        # Create instance_name link (preserve filters) 
        instance_filter_url = f"?instance_name={log.instance_name}"
        if app_name:
            instance_filter_url += f"&app_name={app_name}"
        instance_name_link = f'<a href="{instance_filter_url}">{log.instance_name}</a>'
        
        # Format level with badge
        level_badge = f'<span class="badge badge-{log.levelname.lower()}">{log.levelname}</span>'
        
        # Truncate message if too long
        message = log.message
        if len(message) > 200:
            message = message[:200] + '...'
        
        # Function with line number
        func_display = f"{log.funcname}:{log.lineno}"
        
        data.append([
            timestamp_str,
            app_name_link,
            instance_name_link,
            level_badge,
            message,
            log.module,
            func_display
        ])
    
    response = {
        'draw': draw,
        'recordsTotal': records_total,
        'recordsFiltered': records_filtered,
        'data': data
    }
    
    return JsonResponse(response)


class LogSummaryView(generics.ListAPIView):
    """
    API endpoint that provides a summary of logs grouped by app and instance, with error rollups.
    """
    serializer_class = LogSummarySerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]
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
                .values('levelname')
                .annotate(count=Count('id'))
            )
            # Get recent errors (last 5)
            recent_errors = list(
                AppLog.objects.filter(app_name=app, instance_name=instance, levelname__in=['ERROR', 'CRITICAL'])
                .order_by('-timestamp')[:5]
                .values('timestamp', 'levelname', 'message', 'module', 'funcname', 'lineno')
            )
            summary[app][instance] = {
                'error_counts': {e['levelname']: e['count'] for e in error_counts},
                'recent_errors': recent_errors,
            }
        return Response(summary, status=status.HTTP_200_OK)

@login_required
def database_tables_list(request):
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
    return render(request, 'monitor_app/database_tables_list.html', {'tables': tables})

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

# Views for SWF Data Models

@login_required
def runs_list(request):
    """
    Professional runs list view using server-side DataTables.
    Provides high-performance access to all run records with filtering.
    """
    from django.urls import reverse
    
    # Get filter parameters (for initial state)
    status_filter = request.GET.get('status')
    
    # Column definitions for DataTables
    columns = [
        {'name': 'run_number', 'title': 'Run Number', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'start_time', 'title': 'Start Time', 'orderable': True},
        {'name': 'end_time', 'title': 'End Time', 'orderable': True},
        {'name': 'duration', 'title': 'Duration', 'orderable': False},
        {'name': 'stf_files_count', 'title': 'STF Files', 'orderable': True},
        {'name': 'actions', 'title': 'Actions', 'orderable': False},
    ]
    
    context = {
        'table_title': 'Data-Taking Runs',
        'table_description': 'Server-side processing with live search and filtering across all run records.',
        'ajax_url': reverse('monitor_app:runs_datatable_ajax'),
        'columns': columns,
        'selected_status': status_filter,
    }
    return render(request, 'monitor_app/runs_list.html', context)


def runs_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of runs.
    Handles pagination, searching, ordering, and filtering.
    """
    from django.http import JsonResponse
    from django.db.models import Q, Count
    from django.utils import timezone
    import json
    
    # DataTables parameters
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 50))
    search_value = request.GET.get('search[value]', '').strip()
    
    # Column definitions (must match template column order)
    columns = ['run_number', 'status', 'start_time', 'end_time', 'duration', 'stf_files_count', 'actions']
    
    # Order parameters
    order_column_idx = int(request.GET.get('order[0][column]', 2))  # Default to start_time desc
    order_direction = request.GET.get('order[0][dir]', 'desc')
    order_column = columns[order_column_idx] if 0 <= order_column_idx < len(columns) else 'start_time'
    
    # Handle special ordering cases
    if order_column == 'status':
        # Order by end_time null status (active vs completed)
        order_by = 'end_time' if order_direction == 'asc' else '-start_time'
    elif order_column == 'stf_files_count':
        # This will be handled after annotation
        order_by = 'stf_files_count' if order_direction == 'asc' else '-stf_files_count'
    else:
        order_by = order_column if order_direction == 'asc' else f'-{order_column}'
    
    # Build base queryset with STF file count
    queryset = Run.objects.annotate(stf_files_count=Count('stf_files')).all()
    
    # Apply existing filters (status)
    status_filter = request.GET.get('status')
    if status_filter == 'active':
        queryset = queryset.filter(end_time__isnull=True)
    elif status_filter == 'completed':
        queryset = queryset.filter(end_time__isnull=False)
    
    # Total count before search filtering
    records_total = Run.objects.count()
    records_filtered = queryset.count()
    
    # Apply search filter
    if search_value:
        search_q = (Q(run_number__icontains=search_value) | 
                   Q(start_time__icontains=search_value) |
                   Q(end_time__icontains=search_value))
        queryset = queryset.filter(search_q)
        records_filtered = queryset.count()
    
    # Apply ordering
    queryset = queryset.order_by(order_by)
    
    # Apply pagination
    runs = queryset[start:start + length]
    
    # Format data for DataTables
    data = []
    for run in runs:
        # Format status as plain text
        status_text = 'Active' if not run.end_time else 'Completed'
        
        # Format timestamps
        start_time_str = run.start_time.strftime('%Y%m%d %H:%M:%S') if run.start_time else 'N/A'
        end_time_str = run.end_time.strftime('%Y%m%d %H:%M:%S') if run.end_time else '—'
        
        # Calculate duration using common utility
        from .utils import format_run_duration
        duration_str = format_run_duration(run.start_time, run.end_time)
        
        # Create run number link
        run_number_link = f'<a href="/runs/{run.run_id}/">{run.run_number}</a>'
        
        # View details action link
        view_link = f'<a href="/runs/{run.run_id}/">View</a>'
        
        data.append([
            run_number_link,
            status_text,
            start_time_str,
            end_time_str,
            duration_str,
            run.stf_files_count,
            view_link
        ])
    
    response = {
        'draw': draw,
        'recordsTotal': records_total,
        'recordsFiltered': records_filtered,
        'data': data
    }
    
    return JsonResponse(response)

@login_required
def run_detail(request, run_id):
    """Display detailed view of a specific run"""
    run = get_object_or_404(Run, run_id=run_id)
    stf_files = run.stf_files.all().order_by('-created_at')
    
    # Count files by status
    file_stats = {}
    for status_choice in StfFile._meta.get_field('status').choices:
        status_value = status_choice[0]
        file_stats[status_value] = stf_files.filter(status=status_value).count()
    
    context = {
        'run': run,
        'stf_files': stf_files,
        'file_stats': file_stats,
    }
    return render(request, 'monitor_app/run_detail.html', context)

@login_required
def stf_files_list(request):
    """
    Professional STF files list view using server-side DataTables.
    Provides high-performance access to all STF file records with filtering.
    """
    from django.urls import reverse
    
    # Get filter parameters (for initial state)
    run_number = request.GET.get('run_number')
    status_filter = request.GET.get('status')
    machine_state = request.GET.get('machine_state')
    
    # Get filter options for dropdown links
    run_numbers = Run.objects.values_list('run_number', flat=True).distinct()
    statuses = [choice[0] for choice in StfFile._meta.get_field('status').choices]
    machine_states = StfFile.objects.values_list('machine_state', flat=True).distinct()
    
    # Column definitions for DataTables
    columns = [
        {'name': 'stf_filename', 'title': 'STF Filename', 'orderable': True},
        {'name': 'run__run_number', 'title': 'Run', 'orderable': True},
        {'name': 'machine_state', 'title': 'Machine State', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
        {'name': 'actions', 'title': 'Actions', 'orderable': False},
    ]
    
    context = {
        'table_title': 'STF Files',
        'table_description': 'Server-side processing with live search and filtering across all STF file records.',
        'ajax_url': reverse('monitor_app:stf_files_datatable_ajax'),
        'columns': columns,
        'run_numbers': sorted(run_numbers, reverse=True),
        'statuses': statuses,
        'machine_states': sorted([s for s in machine_states if s]),
        'selected_run_number': run_number,
        'selected_status': status_filter,
        'selected_machine_state': machine_state,
    }
    return render(request, 'monitor_app/stf_files_list.html', context)


def stf_files_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of STF files.
    Handles pagination, searching, ordering, and filtering.
    """
    from django.http import JsonResponse
    from django.db.models import Q
    from urllib.parse import urlencode
    import json
    
    # DataTables parameters
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 50))
    search_value = request.GET.get('search[value]', '').strip()
    
    # Column definitions (must match template column order)
    columns = ['stf_filename', 'run__run_number', 'machine_state', 'status', 'created_at', 'actions']
    
    # Order parameters
    order_column_idx = int(request.GET.get('order[0][column]', 4))  # Default to created_at desc
    order_direction = request.GET.get('order[0][dir]', 'desc')
    order_column = columns[order_column_idx] if 0 <= order_column_idx < len(columns) else 'created_at'
    if order_direction == 'asc':
        order_by = order_column
    else:
        order_by = f'-{order_column}'
    
    # Build base queryset
    queryset = StfFile.objects.select_related('run').all()
    
    # Apply existing filters (run_number, status, machine_state)
    run_number = request.GET.get('run_number')
    status_filter = request.GET.get('status')
    machine_state = request.GET.get('machine_state')
    
    if run_number:
        queryset = queryset.filter(run__run_number=run_number)
    if status_filter:
        queryset = queryset.filter(status=status_filter)
    if machine_state:
        queryset = queryset.filter(machine_state=machine_state)
    
    # Total count before search filtering
    records_total = StfFile.objects.count()
    records_filtered = queryset.count()
    
    # Apply search filter
    if search_value:
        search_q = (Q(stf_filename__icontains=search_value) | 
                   Q(run__run_number__icontains=search_value) |
                   Q(machine_state__icontains=search_value) |
                   Q(status__icontains=search_value))
        queryset = queryset.filter(search_q)
        records_filtered = queryset.count()
    
    # Apply ordering
    queryset = queryset.order_by(order_by)
    
    # Apply pagination
    stf_files = queryset[start:start + length]
    
    # Format data for DataTables
    data = []
    for file in stf_files:
        # Format status badge
        status_class = {
            'REGISTERED': 'primary',
            'PROCESSING': 'warning', 
            'PROCESSED': 'success',
            'FAILED': 'danger'
        }.get(file.status, 'secondary')
        status_badge = f'<span class="badge bg-{status_class}">{file.get_status_display()}</span>'
        
        # Format timestamp
        timestamp_str = file.created_at.strftime('%Y%m%d %H:%M:%S') if file.created_at else 'N/A'
        
        # Create run link
        run_link = f'<a href="/runs/{file.run.run_id}/">{file.run.run_number}</a>' if file.run else 'N/A'
        
        # View details action link
        view_link = f'<a href="/stf-files/{file.file_id}/">View</a>'
        
        data.append([
            file.stf_filename,
            run_link,
            file.machine_state or '',
            status_badge,
            timestamp_str,
            view_link
        ])
    
    response = {
        'draw': draw,
        'recordsTotal': records_total,
        'recordsFiltered': records_filtered,
        'data': data
    }
    
    return JsonResponse(response)


@login_required
def stf_file_detail(request, file_id):
    """Display detailed view of a specific STF file"""
    stf_file = get_object_or_404(StfFile, file_id=file_id)
    dispatches = stf_file.dispatches.all().order_by('-dispatch_time')
    
    context = {
        'stf_file': stf_file,
        'dispatches': dispatches,
    }
    return render(request, 'monitor_app/stf_file_detail.html', context)

@login_required
def subscribers_list(request):
    """Display list of message queue subscribers"""
    subscribers = Subscriber.objects.all().order_by('subscriber_name')
    
    # Filter by active status
    status_filter = request.GET.get('status')
    if status_filter == 'active':
        subscribers = subscribers.filter(is_active=True)
    elif status_filter == 'inactive':
        subscribers = subscribers.filter(is_active=False)
    
    context = {
        'subscribers': subscribers,
        'status_filter': status_filter,
    }
    return render(request, 'monitor_app/subscribers_list.html', context)

@login_required
def subscriber_detail(request, subscriber_id):
    """Display details for a specific subscriber."""
    subscriber = get_object_or_404(Subscriber, subscriber_id=subscriber_id)
    
    context = {
        'subscriber': subscriber,
    }
    
    return render(request, 'monitor_app/subscriber_detail.html', context)

@login_required
def message_dispatch_detail(request, dispatch_id):
    """Display details for a specific message dispatch."""
    dispatch = get_object_or_404(MessageQueueDispatch, dispatch_id=dispatch_id)
    
    context = {
        'dispatch': dispatch,
    }
    
    return render(request, 'monitor_app/message_dispatch_detail.html', context)

@login_required
def message_dispatches_list(request):
    """Display list of message queue dispatches"""
    dispatches = MessageQueueDispatch.objects.all().order_by('-dispatch_time')
    
    # Filtering
    status_filter = request.GET.get('status')
    
    if status_filter == 'success':
        dispatches = dispatches.filter(is_successful=True)
    elif status_filter == 'failed':
        dispatches = dispatches.filter(is_successful=False)
    
    context = {
        'dispatches': dispatches,
        'status_filter': status_filter,
    }
    return render(request, 'monitor_app/message_dispatches_list.html', context)


# ==================== WORKFLOW VIEWS ====================

@login_required
def workflow_dashboard(request):
    """Main workflow dashboard showing pipeline status and statistics."""
    
    # Get workflow statistics
    total_workflows = STFWorkflow.objects.count()
    active_workflows = STFWorkflow.objects.exclude(
        current_status__in=[WorkflowStatus.WORKFLOW_COMPLETE, WorkflowStatus.FAILED]
    ).count()
    completed_workflows = STFWorkflow.objects.filter(
        current_status=WorkflowStatus.WORKFLOW_COMPLETE
    ).count()
    failed_workflows = STFWorkflow.objects.filter(
        current_status=WorkflowStatus.FAILED
    ).count()
    
    # Get recent workflows
    recent_workflows = STFWorkflow.objects.all().order_by('-created_at')[:20]
    
    # Get workflow status distribution
    status_counts = STFWorkflow.objects.values('current_status').annotate(
        count=Count('current_status')
    ).order_by('current_status')
    
    # Get agent statistics
    workflow_agents = SystemAgent.objects.filter(workflow_enabled=True)
    
    # Get DAQ state distribution
    daq_state_counts = STFWorkflow.objects.values('daq_state').annotate(
        count=Count('daq_state')
    ).order_by('daq_state')
    
    context = {
        'total_workflows': total_workflows,
        'active_workflows': active_workflows,
        'completed_workflows': completed_workflows,
        'failed_workflows': failed_workflows,
        'recent_workflows': recent_workflows,
        'status_counts': status_counts,
        'workflow_agents': workflow_agents,
        'daq_state_counts': daq_state_counts,
    }
    
    return render(request, 'monitor_app/workflow_dashboard.html', context)


@login_required
def workflow_list(request):
    """List view of all STF workflows."""
    
    workflows = STFWorkflow.objects.all().order_by('-created_at')
    
    context = {
        'workflows': workflows,
    }
    
    return render(request, 'monitor_app/workflow_list.html', context)


@login_required
def workflow_detail(request, workflow_id):
    """Detailed view of a specific workflow including all stages and messages."""
    
    workflow = get_object_or_404(STFWorkflow, workflow_id=workflow_id)
    
    # Get all stages for this workflow
    stages = AgentWorkflowStage.objects.filter(
        workflow=workflow
    ).order_by('created_at')
    
    # Get all messages for this workflow
    messages = WorkflowMessage.objects.filter(
        workflow=workflow
    ).order_by('sent_at')
    
    # Calculate workflow timing
    workflow_duration = None
    if workflow.completed_at:
        workflow_duration = (workflow.completed_at - workflow.created_at).total_seconds()
    elif workflow.failed_at:
        workflow_duration = (workflow.failed_at - workflow.created_at).total_seconds()
    
    context = {
        'workflow': workflow,
        'stages': stages,
        'messages': messages,
        'workflow_duration': workflow_duration,
    }
    
    return render(request, 'monitor_app/workflow_detail.html', context)


@login_required
def workflow_agents_list(request):
    """View showing the status of all workflow agents."""
    
    agents = SystemAgent.objects.filter(workflow_enabled=True).order_by('agent_type', 'instance_name')
    
    # Get current processing counts per agent
    agent_stats = []
    for agent in agents:
        # Get current processing stages
        current_stages = AgentWorkflowStage.objects.filter(
            agent_name=agent.instance_name,
            status__in=[
                WorkflowStatus.DATA_RECEIVED,
                WorkflowStatus.DATA_PROCESSING,
                WorkflowStatus.PROCESSING_RECEIVED,
                WorkflowStatus.PROCESSING_PROCESSING,
                WorkflowStatus.FASTMON_RECEIVED,
            ]
        ).count()
        
        # Get recent completion rate (last hour)
        from datetime import timedelta
        recent_completed = AgentWorkflowStage.objects.filter(
            agent_name=agent.instance_name,
            completed_at__gte=timezone.now() - timedelta(hours=1)
        ).count()
        
        agent_stats.append({
            'agent': agent,
            'current_processing': current_stages,
            'recent_completed': recent_completed,
        })
    
    context = {
        'agent_stats': agent_stats,
    }
    
    return render(request, 'monitor_app/workflow_agents_list.html', context)


@login_required
def agent_detail(request, instance_name):
    """Display details for a specific agent and its associated workflows."""
    agent = get_object_or_404(SystemAgent, instance_name=instance_name)
    workflows = STFWorkflow.objects.filter(current_agent=agent.agent_type).order_by('-generated_time')

    context = {
        'agent': agent,
        'workflows': workflows,
    }
    return render(request, 'monitor_app/agent_detail.html', context)



@login_required
def workflow_messages(request):
    """View showing all workflow messages for debugging."""
    
    messages = WorkflowMessage.objects.all().order_by('-sent_at')
    
    context = {
        'messages': messages,
    }
    
    return render(request, 'monitor_app/workflow_messages.html', context)


@login_required
def workflow_performance(request):
    """View showing workflow performance metrics and analytics."""
    
    # Get processing time statistics
    from django.db.models import Avg, Min, Max, Count
    
    # Overall workflow completion times
    completed_workflows = STFWorkflow.objects.filter(
        current_status=WorkflowStatus.WORKFLOW_COMPLETE,
        completed_at__isnull=False
    )
    
    # Agent performance statistics
    agent_performance = []
    for agent_type in AgentType.choices:
        agent_code = agent_type[0]
        agent_name = agent_type[1]
        
        stages = AgentWorkflowStage.objects.filter(
            agent_type=agent_code,
            processing_time_seconds__isnull=False
        )
        
        if stages.exists():
            stats = stages.aggregate(
                avg_time=Avg('processing_time_seconds'),
                min_time=Min('processing_time_seconds'),
                max_time=Max('processing_time_seconds'),
                count=Count('stage_id')
            )
            
            agent_performance.append({
                'agent_type': agent_name,
                'agent_code': agent_code,
                'avg_time': stats['avg_time'],
                'min_time': stats['min_time'],
                'max_time': stats['max_time'],
                'count': stats['count']
            })
    
    # Recent throughput (last 24 hours)
    from datetime import timedelta
    recent_time = timezone.now() - timedelta(hours=24)
    
    recent_workflows = STFWorkflow.objects.filter(
        created_at__gte=recent_time
    ).count()
    
    recent_completed = STFWorkflow.objects.filter(
        completed_at__gte=recent_time
    ).count()
    
    context = {
        'completed_workflows': completed_workflows,
        'agent_performance': agent_performance,
        'recent_workflows': recent_workflows,
        'recent_completed': recent_completed,
    }
    
    return render(request, 'monitor_app/workflow_performance.html', context)


@login_required
def workflow_realtime_dashboard(request):
    """Real-time workflow dashboard with live updates."""
    
    # Get initial data (same as regular dashboard)
    total_workflows = STFWorkflow.objects.count()
    active_workflows = STFWorkflow.objects.exclude(
        current_status__in=[WorkflowStatus.WORKFLOW_COMPLETE, WorkflowStatus.FAILED]
    ).count()
    completed_workflows = STFWorkflow.objects.filter(
        current_status=WorkflowStatus.WORKFLOW_COMPLETE
    ).count()
    failed_workflows = STFWorkflow.objects.filter(
        current_status=WorkflowStatus.FAILED
    ).count()
    
    workflow_agents = SystemAgent.objects.filter(workflow_enabled=True)
    
    context = {
        'total_workflows': total_workflows,
        'active_workflows': active_workflows,
        'completed_workflows': completed_workflows,
        'failed_workflows': failed_workflows,
        'workflow_agents': workflow_agents,
    }
    
    return render(request, 'monitor_app/workflow_realtime_dashboard.html', context)


@login_required
def workflow_realtime_data_api(request):
    """API endpoint providing real-time data for dashboard updates."""
    
    from datetime import timedelta
    
    # Basic metrics
    total_workflows = STFWorkflow.objects.count()
    active_workflows = STFWorkflow.objects.exclude(
        current_status__in=[WorkflowStatus.WORKFLOW_COMPLETE, WorkflowStatus.FAILED]
    ).count()
    completed_workflows = STFWorkflow.objects.filter(
        current_status=WorkflowStatus.WORKFLOW_COMPLETE
    ).count()
    failed_workflows = STFWorkflow.objects.filter(
        current_status=WorkflowStatus.FAILED
    ).count()
    
    # Pipeline stage counts
    pipeline_counts = {
        'daqsim': STFWorkflow.objects.filter(current_status=WorkflowStatus.GENERATED).count(),
        'data': STFWorkflow.objects.filter(
            current_status__in=[
                WorkflowStatus.DATA_RECEIVED, 
                WorkflowStatus.DATA_PROCESSING, 
                WorkflowStatus.DATA_COMPLETE
            ]
        ).count(),
        'processing': STFWorkflow.objects.filter(
            current_status__in=[
                WorkflowStatus.PROCESSING_RECEIVED, 
                WorkflowStatus.PROCESSING_PROCESSING, 
                WorkflowStatus.PROCESSING_COMPLETE
            ]
        ).count(),
        'fastmon': STFWorkflow.objects.filter(
            current_status__in=[
                WorkflowStatus.FASTMON_RECEIVED, 
                WorkflowStatus.FASTMON_COMPLETE
            ]
        ).count(),
    }
    
    # Agent status
    agents_data = []
    for agent in SystemAgent.objects.filter(workflow_enabled=True):
        agents_data.append({
            'instance_name': agent.instance_name,
            'agent_type': agent.agent_type,
            'status': agent.status,
            'current_stf_count': agent.current_stf_count,
            'total_stf_processed': agent.total_stf_processed,
            'last_heartbeat': agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        })
    
    # Recent messages (last 10)
    recent_messages = []
    for message in WorkflowMessage.objects.all().order_by('-sent_at')[:10]:
        recent_messages.append({
            'message_type': message.message_type,
            'sender_agent': message.sender_agent,
            'recipient_agent': message.recipient_agent,
            'timestamp': message.sent_at.strftime('%H:%M:%S'),
            'filename': message.workflow.filename if message.workflow else None,
            'is_successful': message.is_successful,
        })
    
    # Chart data
    # Throughput over last 10 minutes (data points every minute)
    now = timezone.now()
    throughput_labels = []
    throughput_data = []
    
    for i in range(10, 0, -1):
        time_point = now - timedelta(minutes=i)
        label = time_point.strftime('%H:%M')
        throughput_labels.append(label)
        
        # Count workflows created in this minute
        count = STFWorkflow.objects.filter(
            created_at__gte=time_point,
            created_at__lt=time_point + timedelta(minutes=1)
        ).count()
        throughput_data.append(count)
    
    # Processing times by agent type
    from django.db.models import Avg
    processing_times = []
    for agent_type in [AgentType.DATA, AgentType.PROCESSING, AgentType.FASTMON]:
        avg_time = AgentWorkflowStage.objects.filter(
            agent_type=agent_type,
            processing_time_seconds__isnull=False
        ).aggregate(avg=Avg('processing_time_seconds'))['avg']
        processing_times.append(round(avg_time, 2) if avg_time else 0)
    
    data = {
        'metrics': {
            'total_workflows': total_workflows,
            'active_workflows': active_workflows,
            'completed_workflows': completed_workflows,
            'failed_workflows': failed_workflows,
        },
        'pipeline': pipeline_counts,
        'agents': agents_data,
        'recent_messages': recent_messages,
        'charts': {
            'throughput': {
                'labels': throughput_labels,
                'data': throughput_data,
            },
            'processing_times': processing_times,
        }
    }
    
    return JsonResponse(data)


@login_required
def persistent_state_view(request):
    """View current persistent state data."""
    import json
    
    state_data = PersistentState.get_state()
    
    # Get the actual database record for metadata
    try:
        state_obj = PersistentState.objects.get(id=1)
        updated_at = state_obj.updated_at
    except PersistentState.DoesNotExist:
        updated_at = None
    
    context = {
        'state_data': state_data,
        'updated_at': updated_at,
        'state_json': json.dumps(state_data, indent=2),
    }
    
    return render(request, 'monitor_app/persistent_state.html', context)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def get_next_run_number(request):
    """API endpoint to get the next run number atomically."""
    try:
        run_number = PersistentState.get_next_run_number()
        return Response({
            'run_number': run_number,
            'status': 'success'
        })
    except Exception as e:
        return Response({
            'error': str(e),
            'status': 'error'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
