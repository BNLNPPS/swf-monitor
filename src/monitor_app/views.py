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
from .models import SystemAgent, AppLog, Run, StfFile, Subscriber, MessageQueueDispatch, FastMonFile, PersistentState, PandaQueue, RucioEndpoint
from .workflow_models import STFWorkflow, AgentWorkflowStage, WorkflowMessage, WorkflowStatus, AgentType
from .serializers import (
    SystemAgentSerializer, AppLogSerializer, LogSummarySerializer, 
    STFWorkflowSerializer, AgentWorkflowStageSerializer, WorkflowMessageSerializer,
    RunSerializer, StfFileSerializer, SubscriberSerializer, MessageQueueDispatchSerializer, FastMonFileSerializer
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
        # This ensures all fields are updated on every heartbeat, not just on creation
        agent, created = SystemAgent.objects.update_or_create(
            instance_name=instance_name,
            defaults={
                'agent_type': request.data.get('agent_type', 'other'),
                'description': request.data.get('description', ''),
                'status': request.data.get('status', 'OK'),
                'agent_url': request.data.get('agent_url', None),
                'workflow_enabled': request.data.get('workflow_enabled', False),
                'last_heartbeat': timezone.now(),
            }
        )
        
        # Explicitly update workflow_enabled if it was provided (to handle existing records)
        if not created and 'workflow_enabled' in request.data:
            agent.workflow_enabled = request.data.get('workflow_enabled', False)
            agent.save(update_fields=['workflow_enabled'])
        
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

class FastMonFileViewSet(viewsets.ModelViewSet):
    """API endpoint for Fast Monitoring files."""
    queryset = FastMonFile.objects.all()
    serializer_class = FastMonFileSerializer
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
    from .utils import DataTablesProcessor, get_filter_params, apply_filters, format_datetime
    from django.db.models import Q, Count, Max
    from urllib.parse import urlencode
    
    # Initialize DataTables processor
    columns = ['app_name', 'instance_name', 'latest_timestamp', 'info_count', 'warning_count', 'error_count', 'critical_count', 'debug_count', 'total_count', 'actions']
    dt = DataTablesProcessor(request, columns, default_order_column=2, default_order_direction='desc')
    
    # Apply filters to base queryset
    base_queryset = AppLog.objects.all()
    filters = get_filter_params(request, ['app_name', 'instance_name', 'levelname'])
    base_queryset = apply_filters(base_queryset, filters)
    
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
    
    # Get counts and apply search
    records_total = AppLog.objects.values('app_name', 'instance_name').annotate(count=Count('id')).count()
    search_fields = ['app_name', 'instance_name']
    summary_queryset = dt.apply_search(summary_queryset, search_fields)
    records_filtered = summary_queryset.count()
    
    # Apply ordering (all columns can use default ordering)
    summary_queryset = summary_queryset.order_by(dt.get_order_by())
    summary_data = dt.apply_pagination(summary_queryset)
    
    # Helper function for drill-down links
    def create_level_link(count, level, app_name, instance_name):
        if count == 0:
            return str(count)
        params = {'app_name': app_name, 'instance_name': instance_name, 'levelname': level}
        url = f'/logs/?{urlencode(params)}'
        return f'<a href="{url}">{count}</a>'
    
    # Format data for DataTables
    data = []
    for item in summary_data:
        timestamp_str = format_datetime(item['latest_timestamp'])
        
        # Create filter-preserving links
        app_filter_url = f"?app_name={item['app_name']}"
        if filters['instance_name']:
            app_filter_url += f"&instance_name={filters['instance_name']}"
        logs_url = reverse('monitor_app:log_list')
        app_name_link = f'<a href="{logs_url}?{app_filter_url}">{item["app_name"]}</a>'
        
        instance_filter_url = f"?instance_name={item['instance_name']}"
        if filters['app_name']:
            instance_filter_url += f"&app_name={filters['app_name']}"
        logs_url = reverse('monitor_app:log_list')
        instance_name_link = f'<a href="{logs_url}?{instance_filter_url}">{item["instance_name"]}</a>'
        
        logs_url = reverse('monitor_app:log_list')
        view_logs_url = f'{logs_url}?app_name={item["app_name"]}&instance_name={item["instance_name"]}'
        view_logs_link = f'<a href="{view_logs_url}">View Logs</a>'
        
        data.append([
            app_name_link, instance_name_link, timestamp_str,
            create_level_link(item['info_count'], 'INFO', item['app_name'], item['instance_name']),
            create_level_link(item['warning_count'], 'WARNING', item['app_name'], item['instance_name']),
            create_level_link(item['error_count'], 'ERROR', item['app_name'], item['instance_name']),
            create_level_link(item['critical_count'], 'CRITICAL', item['app_name'], item['instance_name']),
            create_level_link(item['debug_count'], 'DEBUG', item['app_name'], item['instance_name']),
            item['total_count'], view_logs_link
        ])
    
    return dt.create_response(data, records_total, records_filtered)


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

    # Filter field definitions for dynamic filtering
    filter_fields = [
        {'name': 'app_name', 'label': 'Applications', 'type': 'select'},
        {'name': 'instance_name', 'label': 'Instances', 'type': 'select'},
        {'name': 'levelname', 'label': 'Levels', 'type': 'select'},
    ]

    context = {
        'table_title': 'Log List',
        'table_description': 'View and search application logs with dynamic filtering by source, instance, and level.',
        'ajax_url': reverse('monitor_app:logs_datatable_ajax'),
        'filter_counts_url': reverse('monitor_app:log_filter_counts'),
        'columns': columns,
        'filter_fields': filter_fields,
        'selected_app': app_name,
        'selected_instance': instance_name,
        'selected_levelname': levelname,
    }
    return render(request, 'monitor_app/log_list_dynamic.html', context)



def logs_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of logs.
    Handles pagination, searching, ordering, and filtering.
    """
    from .utils import DataTablesProcessor, get_filter_params, apply_filters, format_datetime
    from django.utils.dateparse import parse_datetime
    
    # Initialize DataTables processor
    columns = ['timestamp', 'app_name', 'instance_name', 'levelname', 'message', 'module', 'funcname']
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='desc')
    
    # Build base queryset and apply standard filters
    queryset = AppLog.objects.all()
    filters = get_filter_params(request, ['app_name', 'instance_name', 'levelname'])
    queryset = apply_filters(queryset, filters)
    
    # Handle time range filters
    start_time = request.GET.get('start_time')
    end_time = request.GET.get('end_time')
    if start_time:
        dt_parsed = parse_datetime(start_time)
        if dt_parsed:
            queryset = queryset.filter(timestamp__gte=dt_parsed)
    if end_time:
        dt_parsed = parse_datetime(end_time)
        if dt_parsed:
            queryset = queryset.filter(timestamp__lte=dt_parsed)
    
    # Get counts and apply search/pagination
    records_total = AppLog.objects.count()
    search_fields = ['app_name', 'instance_name', 'levelname', 'message', 'module', 'funcname']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()
    
    queryset = queryset.order_by(dt.get_order_by())
    logs = dt.apply_pagination(queryset)
    
    # Format data for DataTables
    data = []
    for log in logs:
        timestamp_str = format_datetime(log.timestamp)
        
        # Create filter-preserving links
        app_filter_url = f"?app_name={log.app_name}"
        if filters['instance_name']:
            app_filter_url += f"&instance_name={filters['instance_name']}"
        app_name_link = f'<a href="{app_filter_url}">{log.app_name}</a>'
        
        instance_filter_url = f"?instance_name={log.instance_name}"
        if filters['app_name']:
            instance_filter_url += f"&app_name={filters['app_name']}"
        instance_name_link = f'<a href="{instance_filter_url}">{log.instance_name}</a>'
        
        # Use plain text level (consistent with other views)
        level_text = log.levelname
        
        # Truncate message if too long
        message = log.message[:200] + '...' if len(log.message) > 200 else log.message
        func_display = f"{log.funcname}:{log.lineno}"
        
        data.append([
            timestamp_str, app_name_link, instance_name_link, 
            level_text, message, log.module, func_display
        ])
    
    return dt.create_response(data, records_total, records_filtered)


def get_log_filter_counts(request):
    """
    AJAX endpoint that returns dynamic filter options with counts.
    Only shows options that have >0 matches in the current filtered dataset.
    """
    from .utils import get_filter_counts, get_filter_params, apply_filters
    
    # Get current filters
    current_filters = get_filter_params(request, ['app_name', 'instance_name', 'levelname'])
    
    # Build base queryset
    base_queryset = AppLog.objects.all()
    
    # Get filter counts considering current filters
    filter_fields = ['app_name', 'instance_name', 'levelname']
    filter_counts = get_filter_counts(base_queryset, filter_fields, current_filters)
    
    return JsonResponse({
        'filter_counts': filter_counts,
        'current_filters': current_filters
    })




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
    """
    Modern database tables list view using server-side DataTables.
    Shows all swf_ tables with counts and last insert times.
    """
    from django.urls import reverse
    
    # Column definitions for DataTables
    columns = [
        {'name': 'name', 'title': 'Table Name', 'orderable': True},
        {'name': 'count', 'title': 'Row Count', 'orderable': True},
        {'name': 'last_insert', 'title': 'Last Insert', 'orderable': True},
    ]
    
    context = {
        'table_title': 'Database Overview',
        'table_description': 'Server-side processing view of all swf_ tables in the database with row counts and last insert times.',
        'ajax_url': reverse('monitor_app:database_tables_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'monitor_app/database_tables_server.html', context)


def database_tables_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of database tables.
    Uses proper DataTables pattern with simulated queryset for table metadata.
    """
    from .utils import DataTablesProcessor, format_datetime
    
    # Initialize DataTables processor
    columns = ['name', 'count', 'last_insert']
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='asc')
    
    # Build table metadata as a list of dict objects (simulating queryset records)
    table_records = []
    for model in apps.get_models():
        if not model._meta.db_table.startswith('swf_'):
            continue
            
        record = {
            'name': model._meta.db_table,
            'count': 0,
            'last_insert': None
        }
        
        try:
            record['count'] = model.objects.count()
            # Try to get last insertion time if a DateTimeField exists
            dt_fields = [f.name for f in model._meta.fields if f.get_internal_type() == 'DateTimeField']
            if dt_fields:
                last_obj = model.objects.order_by('-' + dt_fields[0]).first()
                if last_obj:
                    record['last_insert'] = getattr(last_obj, dt_fields[0])
        except Exception:
            pass  # Table may not exist or be accessible
        
        table_records.append(record)
    
    # Get total counts
    records_total = len(table_records)
    
    # Apply search filtering using DataTables pattern
    if dt.search_value:
        search_term = dt.search_value.lower()
        table_records = [r for r in table_records if search_term in r['name'].lower()]
    
    records_filtered = len(table_records)
    
    # Apply ordering using standard DataTables approach
    # Python's sort handles None values naturally - they sort before all other values
    table_records.sort(key=lambda r: (r[dt.order_column] is None, r[dt.order_column]), reverse=(dt.order_direction == 'desc'))
    
    # Apply pagination using DataTables pattern
    start = dt.start
    length = dt.length if dt.length > 0 else len(table_records)
    paginated_records = table_records[start:start + length]
    
    # Format data for DataTables
    data = []
    for record in paginated_records:
        table_url = reverse('monitor_app:database_table_list', args=[record['name']])
        table_link = f'<a href="{table_url}">{record["name"]}</a>'
        count_str = str(record['count'])
        last_insert_str = format_datetime(record['last_insert'])
        
        data.append([table_link, count_str, last_insert_str])
    
    return dt.create_response(data, records_total, records_filtered)


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
    from django.urls import reverse
    
    # Convert columns for DataTables format
    datatable_columns = [{'name': col, 'title': col.replace('_', ' ').title(), 'orderable': True} for col in columns]
    
    context = {
        'table_title': f'Table: {table_name}',
        'table_description': f'Database table contents for {table_name} with search, sorting, and pagination.',
        'ajax_url': reverse('monitor_app:database_table_datatable_ajax', kwargs={'table_name': table_name}),
        'columns': datatable_columns,
        'table_name': table_name,
    }
    return render(request, 'monitor_app/database_table_list.html', context)


@login_required
def database_table_datatable_ajax(request, table_name):
    """
    AJAX endpoint for server-side DataTables processing of individual database table.
    Provides pagination, search, and sorting for any swf_ table.
    """
    if not table_name.startswith('swf_'):
        raise Http404()
    
    from .utils import DataTablesProcessor, format_datetime
    
    # Get column information
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT * FROM "{table_name}" LIMIT 1')
        columns = [col[0] for col in cursor.description]
    
    # Initialize DataTables processor
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='asc')
    
    # Build base query
    query = f'SELECT * FROM "{table_name}"'
    count_query = f'SELECT COUNT(*) FROM "{table_name}"'
    params = []
    
    # Get total count
    with connection.cursor() as cursor:
        cursor.execute(count_query)
        records_total = cursor.fetchone()[0]
    
    # Apply search filtering
    where_conditions = []
    if dt.search_value:
        search_conditions = []
        for column in columns:
            search_conditions.append(f'CAST("{column}" AS TEXT) ILIKE %s')
            params.append(f'%{dt.search_value}%')
        where_conditions.append(f"({' OR '.join(search_conditions)})")
    
    # Build filtered query
    filtered_query = query
    filtered_count_query = count_query
    if where_conditions:
        where_clause = ' WHERE ' + ' AND '.join(where_conditions)
        filtered_query += where_clause
        filtered_count_query += where_clause
    
    # Get filtered count
    with connection.cursor() as cursor:
        cursor.execute(filtered_count_query, params)
        records_filtered = cursor.fetchone()[0]
    
    # Apply ordering
    if dt.order_column and dt.order_column in columns:
        order_clause = f' ORDER BY "{dt.order_column}" {dt.order_direction.upper()}'
        filtered_query += order_clause
    
    # Apply pagination
    filtered_query += f' LIMIT {dt.length} OFFSET {dt.start}'
    
    # Execute final query
    with connection.cursor() as cursor:
        cursor.execute(filtered_query, params)
        results = cursor.fetchall()
    
    # Identify datetime columns using Django model if available
    dt_columns = []
    for model in apps.get_models():
        if model._meta.db_table == table_name:
            dt_columns = [f.name for f in model._meta.fields if f.get_internal_type() == 'DateTimeField']
            break
    
    # Format results for DataTables
    data = []
    for row in results:
        row_data = []
        for i, value in enumerate(row):
            column_name = columns[i]
            if column_name in dt_columns and value:
                # Format datetime values
                row_data.append(format_datetime(value))
            else:
                row_data.append(str(value) if value is not None else '')
        data.append(row_data)
    
    return dt.create_response(data, records_total, records_filtered)


# Views for SWF Data Models

@login_required
def runs_list(request):
    """
    Professional runs list view using server-side DataTables.
    Provides high-performance access to all run records with filtering.
    """
    from django.urls import reverse
    
    # Column definitions for DataTables
    columns = [
        {'name': 'run_number', 'title': 'Run Number', 'orderable': True},
        {'name': 'start_time', 'title': 'Start Time', 'orderable': True},
        {'name': 'end_time', 'title': 'End Time', 'orderable': True},
        {'name': 'duration', 'title': 'Duration', 'orderable': True},
        {'name': 'stf_files_count', 'title': 'STF Files', 'orderable': True},
        {'name': 'actions', 'title': 'Actions', 'orderable': False},
    ]
    
    context = {
        'table_title': 'Testbed Runs',
        'table_description': 'Monitor testbed runs with start/end times, duration, and associated STF files.',
        'ajax_url': reverse('monitor_app:runs_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'monitor_app/runs_list.html', context)


def runs_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of runs.
    Handles pagination, searching, ordering, and filtering.
    """
    from .utils import DataTablesProcessor, format_run_duration, format_datetime
    from django.db.models import Count, Case, When, F, DurationField
    from django.utils import timezone
    
    # Initialize DataTables processor
    columns = ['run_number', 'start_time', 'end_time', 'duration', 'stf_files_count', 'actions']
    special_order_cases = {
        'stf_files_count': 'stf_files_count',
        'duration': 'calculated_duration'  # Sort by the calculated duration field
    }
    dt = DataTablesProcessor(request, columns, default_order_column=1, default_order_direction='desc')
    
    # Build base queryset with STF file count and calculated duration
    queryset = Run.objects.annotate(
        stf_files_count=Count('stf_files'),
        calculated_duration=Case(
            # If end_time exists, calculate duration: end_time - start_time
            When(end_time__isnull=False, then=F('end_time') - F('start_time')),
            # If still in progress (end_time is NULL), duration = now - start_time
            default=timezone.now() - F('start_time'),
            output_field=DurationField()
        )
    ).all()
    
    # Get counts and apply search/pagination
    records_total = Run.objects.count()
    search_fields = ['run_number', 'start_time', 'end_time']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()
    
    queryset = queryset.order_by(dt.get_order_by(special_order_cases))
    runs = dt.apply_pagination(queryset)
    
    # Format data for DataTables
    data = []
    for run in runs:
        start_time_str = format_datetime(run.start_time)
        end_time_str = format_datetime(run.end_time) if run.end_time else '—'
        duration_str = format_run_duration(run.start_time, run.end_time)
        run_detail_url = reverse('monitor_app:run_detail', args=[run.run_id])
        run_number_link = f'<a href="{run_detail_url}">{run.run_number}</a>'
        
        # Make STF files count clickable to filter STF files by this run
        if run.stf_files_count > 0:
            stf_files_url = reverse('monitor_app:stf_files_list')
            stf_files_link = f'<a href="{stf_files_url}?run_number={run.run_number}">{run.stf_files_count}</a>'
        else:
            stf_files_link = str(run.stf_files_count)
        
        run_detail_url = reverse('monitor_app:run_detail', args=[run.run_id])
        view_link = f'<a href="{run_detail_url}">View</a>'
        
        data.append([
            run_number_link, start_time_str, end_time_str,
            duration_str, stf_files_link, view_link
        ])
    
    return dt.create_response(data, records_total, records_filtered)

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
        'table_description': 'Track STF files by run, machine state, and processing status.',
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
    from .utils import DataTablesProcessor, get_filter_params, format_datetime
    
    # Initialize DataTables processor
    columns = ['stf_filename', 'run__run_number', 'machine_state', 'status', 'created_at', 'actions']
    dt = DataTablesProcessor(request, columns, default_order_column=4, default_order_direction='desc')
    
    # Build base queryset
    queryset = StfFile.objects.select_related('run').all()
    
    # Apply filters using utility
    filter_mapping = {
        'run_number': 'run__run_number',  # Map filter param to actual field
        'status': 'status',
        'machine_state': 'machine_state'
    }
    filters = get_filter_params(request, filter_mapping.keys())
    # Apply filters with correct field names
    for param_name, field_name in filter_mapping.items():
        if filters[param_name]:
            queryset = queryset.filter(**{field_name: filters[param_name]})
    
    # Get counts and apply search/pagination
    records_total = StfFile.objects.count()
    search_fields = ['stf_filename', 'run__run_number', 'machine_state', 'status']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()
    
    queryset = queryset.order_by(dt.get_order_by())
    stf_files = dt.apply_pagination(queryset)
    
    # Format data for DataTables
    data = []
    for file in stf_files:
        # Use plain text status (consistent with runs view)
        status_text = file.get_status_display()
        timestamp_str = format_datetime(file.created_at)
        run_link = f'<a href="{reverse("monitor_app:run_detail", args=[file.run.run_id])}">{file.run.run_number}</a>' if file.run else 'N/A'
        stf_file_detail_url = reverse('monitor_app:stf_file_detail', args=[file.file_id])
        view_link = f'<a href="{stf_file_detail_url}">View</a>'
        
        data.append([
            file.stf_filename, run_link, file.machine_state or '',
            status_text, timestamp_str, view_link
        ])
    
    return dt.create_response(data, records_total, records_filtered)


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
    """Professional subscribers list view using server-side DataTables."""
    from django.urls import reverse
    
    # Column definitions for DataTables
    columns = [
        {'name': 'subscriber_name', 'title': 'Subscriber Name', 'orderable': True},
        {'name': 'description', 'title': 'Description', 'orderable': True},
        {'name': 'fraction', 'title': 'Fraction', 'orderable': True},
        {'name': 'is_active', 'title': 'is_active', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
        {'name': 'actions', 'title': 'Actions', 'orderable': False},
    ]
    
    # Filter field definitions for dynamic filtering using generic auto-discovery
    filter_fields = [
        {'name': 'is_active', 'label': 'is_active', 'type': 'select'},
    ]
    
    context = {
        'table_title': 'Message Queue Subscribers',
        'table_description': 'Monitor message queue subscribers and their activity status.',
        'ajax_url': reverse('monitor_app:subscribers_datatable_ajax'),
        'filter_counts_url': reverse('monitor_app:subscribers_filter_counts'),
        'columns': columns,
        'filter_fields': filter_fields,
        'selected_is_active': request.GET.get('is_active'),
    }
    return render(request, 'monitor_app/subscribers_list_dynamic.html', context)

def subscribers_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of subscribers.
    Handles pagination, searching, ordering, and filtering using utils.py functions.
    """
    from .utils import DataTablesProcessor, get_filter_params, apply_filters, format_datetime
    
    # Initialize DataTables processor
    columns = ['subscriber_name', 'description', 'fraction', 'is_active', 'created_at', 'updated_at', 'actions']
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='asc')
    
    # Build base queryset and apply filters using utils.py
    queryset = Subscriber.objects.all()
    filters = get_filter_params(request, ['is_active'])
    queryset = apply_filters(queryset, filters)
    
    # Get counts and apply search/pagination
    records_total = Subscriber.objects.count()
    search_fields = ['subscriber_name', 'description']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()
    
    queryset = queryset.order_by(dt.get_order_by())
    subscribers = dt.apply_pagination(queryset)
    
    # Format data for DataTables
    data = []
    for subscriber in subscribers:
        subscriber_detail_url = reverse('monitor_app:subscriber_detail', args=[subscriber.subscriber_id])
        subscriber_name_link = f'<a href="{subscriber_detail_url}">{subscriber.subscriber_name}</a>'
        description = subscriber.description[:100] + '...' if subscriber.description and len(subscriber.description) > 100 else (subscriber.description or '')
        fraction_str = f"{subscriber.fraction:.3f}" if subscriber.fraction is not None else 'N/A'
        # Show raw DB value, not massaged badges
        is_active_value = str(subscriber.is_active).lower()  # True -> 'true', False -> 'false'
        created_str = format_datetime(subscriber.created_at)
        updated_str = format_datetime(subscriber.updated_at)
        subscriber_detail_url = reverse('monitor_app:subscriber_detail', args=[subscriber.subscriber_id])
        view_link = f'<a href="{subscriber_detail_url}">View</a>'
        
        data.append([
            subscriber_name_link, description, fraction_str, is_active_value,
            created_str, updated_str, view_link
        ])
    
    return dt.create_response(data, records_total, records_filtered)


def get_subscribers_filter_counts(request):
    """
    AJAX endpoint that returns dynamic filter options with counts for subscribers.
    Uses utils.py get_filter_counts() for generic auto-discovery of field values.
    """
    from .utils import get_filter_counts, get_filter_params, apply_filters
    
    # Get current filters
    current_filters = get_filter_params(request, ['is_active'])
    
    # Build base queryset
    base_queryset = Subscriber.objects.all()
    
    # Use generic get_filter_counts to auto-discover field values
    filter_fields = ['is_active']
    filter_counts = get_filter_counts(base_queryset, filter_fields, current_filters)
    
    return JsonResponse({
        'filter_counts': filter_counts,
        'current_filters': current_filters
    })


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
    """Professional workflow list view using server-side DataTables with dynamic filtering."""
    from django.urls import reverse
    from .utils import get_filter_counts
    from .workflow_models import STFWorkflow
    
    # Column definitions for DataTables
    columns = [
        {'name': 'filename', 'title': 'Filename', 'orderable': True},
        {'name': 'msg_type', 'title': 'Type', 'orderable': True},
        {'name': 'current_status', 'title': 'Status', 'orderable': True},
        {'name': 'current_agent', 'title': 'Current Agent', 'orderable': True},
        {'name': 'daq_state', 'title': 'DAQ State', 'orderable': True},
        {'name': 'generated_time', 'title': 'Generated', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
    ]
    
    # Get filter counts for dynamic filtering
    filter_fields = ['current_status', 'current_agent', 'daq_state']
    filter_counts = get_filter_counts(STFWorkflow.objects.all(), filter_fields)
    
    context = {
        'table_title': 'Workflow List',
        'table_description': 'Monitor workflow progress through the processing pipeline from generation to completion.',
        'ajax_url': reverse('monitor_app:workflow_datatable_ajax'),
        'columns': columns,
        'filter_counts': filter_counts,
    }
    return render(request, 'monitor_app/workflow_list.html', context)


def workflow_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of workflows.
    Handles pagination, searching, ordering, and filtering.
    """
    from .utils import DataTablesProcessor, format_datetime, apply_filters, get_filter_params
    from .workflow_models import STFWorkflow
    
    # Initialize DataTables processor
    columns = ['filename', 'msg_type', 'current_status', 'current_agent', 'daq_state', 'generated_time', 'updated_at']
    dt = DataTablesProcessor(request, columns, default_order_column=5, default_order_direction='desc')
    
    # Build base queryset
    queryset = STFWorkflow.objects.all()
    
    # Apply dynamic filters
    filter_fields = ['current_status', 'current_agent', 'daq_state']
    filters = get_filter_params(request, filter_fields)
    queryset = apply_filters(queryset, filters)
    
    # Get counts and apply search/pagination
    records_total = STFWorkflow.objects.count()
    search_fields = ['filename', 'current_status', 'current_agent', 'daq_state']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()
    
    queryset = queryset.order_by(dt.get_order_by())
    workflows = dt.apply_pagination(queryset)
    
    # Format data for DataTables
    data = []
    for workflow in workflows:
        workflow_detail_url = reverse('monitor_app:workflow_detail', args=[workflow.workflow_id])
        filename_link = f'<a href="{workflow_detail_url}">{workflow.filename}</a>'
        
        # Extract msg_type from JSON metadata safely
        msg_type = 'N/A'
        if workflow.stf_metadata and isinstance(workflow.stf_metadata, dict):
            msg_type = workflow.stf_metadata.get('msg_type', 'N/A')
        
        status_display = workflow.get_current_status_display()
        agent_display = workflow.get_current_agent_display()
        daq_state_str = f"{workflow.daq_state} / {workflow.daq_substate}"
        generated_time_str = format_datetime(workflow.generated_time)
        updated_time_str = format_datetime(workflow.updated_at)
        
        data.append([
            filename_link, msg_type, status_display, agent_display,
            daq_state_str, generated_time_str, updated_time_str
        ])
    
    return dt.create_response(data, records_total, records_filtered)


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
    """View showing the status of all workflow agents using server-side DataTables."""
    from django.urls import reverse
    
    context = {
        'table_title': 'Agent Status',
        'table_description': 'Status and statistics for all agents.',
        'ajax_url': reverse('monitor_app:workflow_agents_datatable_ajax'),
        'columns': [
            {'title': 'Agent Name', 'orderable': True},
            {'title': 'Type', 'orderable': True},
            {'title': 'Status', 'orderable': True},
            {'title': 'Workflow Enabled', 'orderable': True},
            {'title': 'Last Heartbeat', 'orderable': True},
            {'title': 'Currently Processing', 'orderable': True},
            {'title': 'Recently Completed (1hr)', 'orderable': True},
            {'title': 'Total Processed', 'orderable': True},
        ],
        'filter_fields': [],  # No filters for this view
        'default_order': [[4, 'desc']],  # Default sort by Last Heartbeat descending
    }
    
    return render(request, 'monitor_app/workflow_agents_list_dynamic.html', context)


@login_required
def workflow_agents_datatable_ajax(request):
    """AJAX endpoint for workflow agents DataTable server-side processing."""
    from datetime import timedelta
    from .utils import DataTablesProcessor, format_datetime
    
    # Column definitions matching the template order
    columns = ['instance_name', 'agent_type', 'status', 'workflow_enabled', 'last_heartbeat', 'current_processing', 'recent_completed', 'total_stf_processed']
    
    dt = DataTablesProcessor(request, columns, default_order_column=4, default_order_direction='desc')  # Sort by last_heartbeat descending
    
    # Base queryset - show all agents, not just workflow-enabled ones
    queryset = SystemAgent.objects.all()
    
    # For sorting current_processing and recent_completed, we need to annotate
    # But for now, let's keep it simple and sort by the basic fields
    special_cases = {
        'current_processing': 'instance_name',  # Fallback to instance name
        'recent_completed': 'instance_name',     # Fallback to instance name
    }
    
    order_by = dt.get_order_by(special_cases)
    queryset = queryset.order_by(order_by)
    
    # Apply search if provided
    search_fields = ['instance_name', 'agent_type', 'status']
    queryset = dt.apply_search(queryset, search_fields)
    
    # Get counts
    records_total = SystemAgent.objects.count()
    records_filtered = queryset.count()
    
    # Apply pagination
    agents = dt.apply_pagination(queryset)
    
    # Build data rows
    data = []
    for agent in agents:
        # Calculate current processing stages
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
        
        # Calculate recent completion rate (last hour)
        recent_completed = AgentWorkflowStage.objects.filter(
            agent_name=agent.instance_name,
            completed_at__gte=timezone.now() - timedelta(hours=1)
        ).count()
        
        # Format status badge
        status_class = {
            'OK': 'success',
            'WARNING': 'warning', 
            'ERROR': 'danger'
        }.get(agent.status, 'secondary')
        
        status_badge = f'<span class="badge bg-{status_class}">{agent.status}</span>'
        
        # Create agent name link
        agent_detail_url = reverse('monitor_app:agent_detail', args=[agent.instance_name])
        agent_link = f'<a href="{agent_detail_url}">{agent.instance_name}</a>'
        
        # Format workflow enabled badge
        workflow_enabled_class = 'success' if agent.workflow_enabled else 'secondary'
        workflow_enabled_text = 'Enabled' if agent.workflow_enabled else 'Disabled'
        workflow_enabled_badge = f'<span class="badge bg-{workflow_enabled_class}">{workflow_enabled_text}</span>'
        
        # Format heartbeat - sorting is now handled at database level
        heartbeat_cell = format_datetime(agent.last_heartbeat) if agent.last_heartbeat else 'Never'
        
        row = [
            agent_link,
            agent.get_agent_type_display(),
            status_badge,
            workflow_enabled_badge,
            heartbeat_cell,
            str(current_stages),
            str(recent_completed),
            str(agent.total_stf_processed or 0),
        ]
        data.append(row)
    
    return dt.create_response(data, records_total, records_filtered)


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
    """View showing all workflow messages with dynamic filtering."""
    from django.urls import reverse
    
    context = {
        'table_title': 'Workflow Messages',
        'table_description': 'All messages exchanged in the workflow system with filtering capabilities.',
        'ajax_url': reverse('monitor_app:workflow_messages_datatable_ajax'),
        'filter_counts_url': reverse('monitor_app:workflow_messages_filter_counts'),
        'columns': [
            {'title': 'Timestamp', 'orderable': True},
            {'title': 'message_type', 'orderable': True},
            {'title': 'sender_agent', 'orderable': True},
            {'title': 'recipient_agent', 'orderable': True},
            {'title': 'workflow', 'orderable': True},
            {'title': 'is_successful', 'orderable': True},
        ],
        'filter_fields': [
            {'name': 'message_type', 'label': 'message_type', 'type': 'select'},
            {'name': 'sender_agent', 'label': 'sender_agent', 'type': 'select'},
            {'name': 'recipient_agent', 'label': 'recipient_agent', 'type': 'select'},
            {'name': 'workflow', 'label': 'workflow', 'type': 'select'},
            {'name': 'is_successful', 'label': 'is_successful', 'type': 'select'},
        ],
        # Add current filter values for initial state
        'selected_message_type': request.GET.get('message_type'),
        'selected_sender_agent': request.GET.get('sender_agent'),
        'selected_recipient_agent': request.GET.get('recipient_agent'),
        'selected_workflow': request.GET.get('workflow'),
        'selected_is_successful': request.GET.get('is_successful'),
    }
    
    return render(request, 'monitor_app/workflow_messages_dynamic.html', context)


@login_required
def workflow_messages_datatable_ajax(request):
    """AJAX endpoint for workflow messages DataTable server-side processing."""
    from .utils import DataTablesProcessor, get_filter_params, apply_filters, format_datetime
    
    # Column definitions matching the template order  
    columns = ['sent_at', 'message_type', 'sender_agent', 'recipient_agent', 'workflow', 'is_successful']
    
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='desc')
    
    # Base queryset
    queryset = WorkflowMessage.objects.select_related('workflow')
    
    # Apply filters
    filter_params = get_filter_params(request, ['message_type', 'sender_agent', 'recipient_agent', 'workflow', 'is_successful'])
    
    # Handle workflow filter - need to map workflow display names to IDs
    if filter_params.get('workflow'):
        workflow_value = filter_params['workflow']
        # Try to find workflow by filename
        try:
            if workflow_value != 'N/A':
                workflow_obj = STFWorkflow.objects.filter(filename=workflow_value).first()
                if workflow_obj:
                    filter_params['workflow'] = workflow_obj.workflow_id
                else:
                    # Filter out all results if workflow not found
                    queryset = queryset.none()
            else:
                # Filter for null workflow
                filter_params['workflow__isnull'] = True
                del filter_params['workflow']
        except:
            pass
    
    queryset = apply_filters(queryset, filter_params)
    
    # Apply search if provided
    search_fields = ['message_type', 'sender_agent', 'recipient_agent']
    queryset = dt.apply_search(queryset, search_fields)
    
    # Apply ordering
    order_by = dt.get_order_by()
    queryset = queryset.order_by(order_by)
    
    # Get counts
    records_total = WorkflowMessage.objects.count()
    records_filtered = queryset.count()
    
    # Apply pagination
    messages = dt.apply_pagination(queryset)
    
    # Build data rows
    data = []
    for message in messages:
        # Format status
        if message.is_successful is True:
            status = '<span class="badge bg-success">Success</span>'
        elif message.is_successful is False:
            status = '<span class="badge bg-danger">Failed</span>'
        else:
            status = '<span class="badge bg-secondary">Unknown</span>'
        
        # Format workflow link
        if message.workflow:
            workflow_detail_url = reverse('monitor_app:workflow_detail', args=[message.workflow.workflow_id])
            workflow_link = f'<a href="{workflow_detail_url}" style="font-size: 0.8rem;">{message.workflow.filename}</a>'
        else:
            workflow_link = 'N/A'
        
        # Format agent links
        sender_link = f'<a href="{reverse("monitor_app:agent_detail", args=[message.sender_agent])}">{message.sender_agent}</a>' if message.sender_agent else 'N/A'
        
        # Handle special case for "all-agents" recipient
        if message.recipient_agent == 'all-agents':
            workflow_agents_url = reverse('monitor_app:workflow_agents_list')
            recipient_link = f'<a href="{workflow_agents_url}">{message.recipient_agent}</a>'
        elif message.recipient_agent:
            agent_detail_url = reverse('monitor_app:agent_detail', args=[message.recipient_agent])
            recipient_link = f'<a href="{agent_detail_url}">{message.recipient_agent}</a>'
        else:
            recipient_link = 'N/A'
        
        row = [
            format_datetime(message.sent_at),
            message.message_type,
            sender_link,
            recipient_link,
            workflow_link,
            status,
        ]
        data.append(row)
    
    return dt.create_response(data, records_total, records_filtered)


@login_required
def get_workflow_messages_filter_counts(request):
    """Get filter counts for workflow messages filters."""
    from .utils import get_filter_params, apply_filters, get_filter_counts
    from django.http import JsonResponse
    
    # Get current filters
    current_filters = get_filter_params(request, ['message_type', 'sender_agent', 'recipient_agent', 'workflow', 'is_successful'])
    
    # Base queryset
    queryset = WorkflowMessage.objects.select_related('workflow')
    
    # Calculate counts for each filter
    filter_fields = ['message_type', 'sender_agent', 'recipient_agent', 'is_successful']
    filter_counts = get_filter_counts(queryset, filter_fields, current_filters)
    
    # Handle workflow filter specially - show filenames instead of IDs
    workflow_queryset = queryset
    temp_filters = {k: v for k, v in current_filters.items() if k != 'workflow' and v}
    workflow_queryset = apply_filters(workflow_queryset, temp_filters)
    
    # Get workflow counts with filenames
    workflow_counts = []
    
    # Count messages with workflows
    workflow_msgs = workflow_queryset.filter(workflow__isnull=False).values('workflow__filename').annotate(count=Count('message_id')).filter(count__gt=0).order_by('-count', 'workflow__filename')
    for item in workflow_msgs:
        workflow_counts.append((item['workflow__filename'], item['count']))
    
    # Count messages without workflows
    null_count = workflow_queryset.filter(workflow__isnull=True).count()
    if null_count > 0:
        workflow_counts.append(('N/A', null_count))
    
    filter_counts['workflow'] = workflow_counts
    
    return JsonResponse({'filter_counts': filter_counts})


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
    from .utils import format_datetime
    
    state_data = PersistentState.get_state()
    
    # Get the actual database record for metadata
    try:
        state_obj = PersistentState.objects.get(id=1)
        updated_at = format_datetime(state_obj.updated_at)
    except PersistentState.DoesNotExist:
        updated_at = None
    
    # Format any timestamp values in the state data for display
    from monitor_app.utils import format_timestamp_fields
    formatted_state_data = format_timestamp_fields(state_data)
    
    # Use the same formatted data for JSON display
    formatted_json_data = formatted_state_data
    
    context = {
        'state_data': formatted_state_data,
        'updated_at': updated_at,
        'state_json': json.dumps(formatted_json_data, indent=2),  # Use formatted data for JSON view too
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


# ==================== PANDA QUEUES AND RUCIO ENDPOINTS VIEWS ====================

@login_required
def panda_queues_list(request):
    """
    Professional PanDA queues list view using server-side DataTables.
    Displays computing queue configurations with key fields and JSON links.
    """
    from django.urls import reverse
    
    # Column definitions for DataTables
    columns = [
        {'name': 'queue_name', 'title': 'Name', 'orderable': True},
        {'name': 'site', 'title': 'Site', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'queue_type', 'title': 'Type', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
        {'name': 'json', 'title': 'JSON', 'orderable': False},
    ]
    
    context = {
        'table_title': 'PanDA Queues',
        'table_description': 'Computing queue configurations for the PanDA workload management system.',
        'ajax_url': reverse('monitor_app:panda_queues_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'monitor_app/panda_queues_list.html', context)


def panda_queues_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of PanDA queues.
    Handles pagination, searching, and ordering.
    """
    from .utils import DataTablesProcessor, format_datetime
    
    # Initialize DataTables processor
    columns = ['queue_name', 'site', 'status', 'queue_type', 'updated_at', 'json']
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='asc')
    
    # Build base queryset
    queryset = PandaQueue.objects.all()
    
    # Get counts and apply search/pagination
    records_total = PandaQueue.objects.count()
    search_fields = ['queue_name', 'site', 'status', 'queue_type']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()
    
    queryset = queryset.order_by(dt.get_order_by())
    queues = dt.apply_pagination(queryset)
    
    # Format data for DataTables
    data = []
    for queue in queues:
        queue_name_link = f'<a href="{reverse("monitor_app:panda_queue_detail", args=[queue.queue_name])}">{queue.queue_name}</a>'
        
        # Extract key fields from config_data if not set
        if not queue.site and queue.config_data:
            queue.site = queue.config_data.get('site', '')
        if not queue.queue_type and queue.config_data:
            queue.queue_type = queue.config_data.get('type', '')
        
        updated_str = format_datetime(queue.updated_at)
        json_link = f'<a href="{reverse("monitor_app:panda_queue_json", args=[queue.queue_name])}">View JSON</a>'
        
        data.append([
            queue_name_link, queue.site or '', queue.status,
            queue.queue_type or '', updated_str, json_link
        ])
    
    return dt.create_response(data, records_total, records_filtered)


@login_required
def panda_queue_detail(request, queue_name):
    """Display detailed view of a specific PanDA queue configuration."""
    queue = get_object_or_404(PandaQueue, queue_name=queue_name)
    
    # Extract some key fields for summary display
    summary_fields = {}
    if queue.config_data:
        # Extract commonly useful fields
        for field in ['resource_type', 'cloud', 'country', 'site']:
            if field in queue.config_data:
                summary_fields[field] = queue.config_data[field]
    
    context = {
        'queue': queue,
        'summary_fields': summary_fields,
    }
    return render(request, 'monitor_app/panda_queue_detail.html', context)


@login_required
def panda_queue_json(request, queue_name):
    """Display JSON view of a PanDA queue configuration using renderjson."""
    queue = get_object_or_404(PandaQueue, queue_name=queue_name)
    
    import json
    context = {
        'queue': queue,
        'config_json': json.dumps(queue.config_data, indent=2),
        'title': f'PanDA Queue: {queue.queue_name}',
    }
    return render(request, 'monitor_app/json_viewer.html', context)


@login_required
def rucio_endpoints_list(request):
    """
    Professional Rucio endpoints list view using server-side DataTables.
    Displays DDM endpoint configurations with key fields and JSON links.
    """
    from django.urls import reverse
    
    # Column definitions for DataTables
    columns = [
        {'name': 'endpoint_name', 'title': 'Endpoint Name', 'orderable': True},
        {'name': 'site', 'title': 'Site', 'orderable': True},
        {'name': 'endpoint_type', 'title': 'Type', 'orderable': True},
        {'name': 'is_tape', 'title': 'Tape', 'orderable': True},
        {'name': 'is_active', 'title': 'Active', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
        {'name': 'json', 'title': 'JSON', 'orderable': False},
    ]
    
    context = {
        'table_title': 'Rucio Endpoints',
        'table_description': 'Distributed data management endpoints for the Rucio system.',
        'ajax_url': reverse('monitor_app:rucio_endpoints_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'monitor_app/rucio_endpoints_list.html', context)


def rucio_endpoints_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of Rucio endpoints.
    Handles pagination, searching, and ordering.
    """
    from .utils import DataTablesProcessor, format_datetime
    
    # Initialize DataTables processor
    columns = ['endpoint_name', 'site', 'endpoint_type', 'is_tape', 'is_active', 'updated_at', 'json']
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='asc')
    
    # Build base queryset
    queryset = RucioEndpoint.objects.all()
    
    # Get counts and apply search/pagination
    records_total = RucioEndpoint.objects.count()
    search_fields = ['endpoint_name', 'site', 'endpoint_type']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()
    
    queryset = queryset.order_by(dt.get_order_by())
    endpoints = dt.apply_pagination(queryset)
    
    # Format data for DataTables
    data = []
    for endpoint in endpoints:
        endpoint_name_link = f'<a href="{reverse("monitor_app:rucio_endpoint_detail", args=[endpoint.endpoint_name])}">{endpoint.endpoint_name}</a>'
        
        # Extract key fields from config_data if not set
        if not endpoint.site and endpoint.config_data:
            endpoint.site = endpoint.config_data.get('rcsite', endpoint.config_data.get('site', ''))
        if not endpoint.endpoint_type and endpoint.config_data:
            endpoint.endpoint_type = 'tape' if endpoint.config_data.get('is_tape') else 'disk'
        
        # Format boolean fields
        is_tape_badge = '<span class="badge bg-warning">Tape</span>' if endpoint.is_tape else '<span class="badge bg-info">Disk</span>'
        is_active_badge = '<span class="badge bg-success">Active</span>' if endpoint.is_active else '<span class="badge bg-secondary">Inactive</span>'
        
        updated_str = format_datetime(endpoint.updated_at)
        json_link = f'<a href="{reverse("monitor_app:rucio_endpoint_json", args=[endpoint.endpoint_name])}">View JSON</a>'
        
        data.append([
            endpoint_name_link, endpoint.site or '', endpoint.endpoint_type or '',
            is_tape_badge, is_active_badge, updated_str, json_link
        ])
    
    return dt.create_response(data, records_total, records_filtered)


@login_required
def rucio_endpoint_detail(request, endpoint_name):
    """Display detailed view of a specific Rucio endpoint configuration."""
    endpoint = get_object_or_404(RucioEndpoint, endpoint_name=endpoint_name)
    
    # Extract some key fields for summary display
    summary_fields = {}
    if endpoint.config_data:
        # Extract commonly useful fields
        for field in ['cloud', 'rc_site']:
            if field in endpoint.config_data:
                summary_fields[field] = endpoint.config_data[field]
        
        # Extract resource info if available
        if 'resource' in endpoint.config_data and isinstance(endpoint.config_data['resource'], dict):
            resource = endpoint.config_data['resource']
            summary_fields['resource_endpoint'] = resource.get('endpoint', '')
    
    context = {
        'endpoint': endpoint,
        'summary_fields': summary_fields,
    }
    return render(request, 'monitor_app/rucio_endpoint_detail.html', context)


@login_required
def rucio_endpoint_json(request, endpoint_name):
    """Display JSON view of a Rucio endpoint configuration using renderjson."""
    endpoint = get_object_or_404(RucioEndpoint, endpoint_name=endpoint_name)
    
    import json
    context = {
        'endpoint': endpoint,
        'config_json': json.dumps(endpoint.config_data, indent=2),
        'title': f'Rucio Endpoint: {endpoint.endpoint_name}',
    }
    return render(request, 'monitor_app/json_viewer.html', context)


@login_required
def panda_queues_all_json(request):
    """Display JSON view of all PanDA queue configurations."""
    queues_data = {}
    for queue in PandaQueue.objects.all().order_by('queue_name'):
        queues_data[queue.queue_name] = queue.config_data
    
    import json
    context = {
        'config_json': json.dumps(queues_data, indent=2),
        'title': 'All PanDA Queues Configuration',
    }
    return render(request, 'monitor_app/json_viewer.html', context)


@login_required
def rucio_endpoints_all_json(request):
    """Display JSON view of all Rucio endpoint configurations."""
    endpoints_data = {}
    for endpoint in RucioEndpoint.objects.all().order_by('endpoint_name'):
        endpoints_data[endpoint.endpoint_name] = endpoint.config_data
    
    import json
    context = {
        'config_json': json.dumps(endpoints_data, indent=2),
        'title': 'All Rucio Endpoints Configuration',
    }
    return render(request, 'monitor_app/json_viewer.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser)
def update_panda_queues_from_github(request):
    """Update PanDA queues from GitHub main branch. Requires superuser."""
    import json
    import urllib.request
    from django.contrib import messages
    from django.shortcuts import redirect
    from datetime import datetime
    import email.utils
    
    github_url = "https://raw.githubusercontent.com/BNLNPPS/swf-testbed/main/config/panda_queues.json"
    repo_location = "BNLNPPS/swf-testbed"
    file_path = "config/panda_queues.json"
    github_file_url = "https://github.com/BNLNPPS/swf-testbed/blob/main/config/panda_queues.json"
    
    try:
        # Fetch JSON from GitHub
        with urllib.request.urlopen(github_url) as response:
            data = json.loads(response.read().decode())
        
        # Clear existing data and reload
        PandaQueue.objects.all().delete()
        
        created_count = 0
        for queue_name, config in data.items():
            # Extract key fields from config
            site = config.get('site', '')
            queue_type = config.get('type', '')
            
            # Determine status based on config
            status = 'active'  # Default to active
            if config.get('status') == 'offline':
                status = 'offline'
            
            # Create queue
            PandaQueue.objects.create(
                queue_name=queue_name,
                site=site,
                queue_type=queue_type,
                status=status,
                config_data=config,
            )
            created_count += 1
        
        messages.success(request, 
            f'Successfully updated {created_count} PanDA queues from GitHub<br>'
            f'<strong>Repository:</strong> {repo_location}<br>'
            f'<strong>File:</strong> {file_path}<br>'
            f'<strong>View on GitHub:</strong> <a href="{github_file_url}" target="_blank">Click here to see what was loaded</a>',
            extra_tags='safe'
        )
        
    except Exception as e:
        messages.error(request, f'Failed to update from GitHub: {str(e)}')
    
    return redirect('monitor_app:panda_queues_list')


@login_required
@user_passes_test(lambda u: u.is_superuser)
def update_rucio_endpoints_from_github(request):
    """Update Rucio endpoints from GitHub main branch. Requires superuser."""
    import json
    import urllib.request
    from django.contrib import messages
    from django.shortcuts import redirect
    from datetime import datetime
    import email.utils
    
    github_url = "https://raw.githubusercontent.com/BNLNPPS/swf-testbed/main/config/ddm_endpoints.json"
    repo_location = "BNLNPPS/swf-testbed"
    file_path = "config/ddm_endpoints.json"
    github_file_url = "https://github.com/BNLNPPS/swf-testbed/blob/main/config/ddm_endpoints.json"
    
    try:
        # Fetch JSON from GitHub
        with urllib.request.urlopen(github_url) as response:
            data = json.loads(response.read().decode())
        
        # Clear existing data and reload
        RucioEndpoint.objects.all().delete()
        
        created_count = 0
        for endpoint_name, config in data.items():
            # Extract key fields from config
            site = config.get('rcsite', config.get('site', ''))
            is_tape = config.get('is_tape', False)
            
            # Determine endpoint type
            if is_tape:
                endpoint_type = 'tape'
            elif config.get('is_cache'):
                endpoint_type = 'cache'
            else:
                endpoint_type = 'disk'
            
            # Check if active based on rc_site_state
            is_active = config.get('rc_site_state') == 'ACTIVE'
            
            # Create endpoint
            RucioEndpoint.objects.create(
                endpoint_name=endpoint_name,
                site=site,
                endpoint_type=endpoint_type,
                is_tape=is_tape,
                is_active=is_active,
                config_data=config,
            )
            created_count += 1
        
        messages.success(request, 
            f'Successfully updated {created_count} Rucio endpoints from GitHub<br>'
            f'<strong>Repository:</strong> {repo_location}<br>'
            f'<strong>File:</strong> {file_path}<br>'
            f'<strong>View on GitHub:</strong> <a href="{github_file_url}" target="_blank">Click here to see what was loaded</a>',
            extra_tags='safe'
        )
        
    except Exception as e:
        messages.error(request, f'Failed to update from GitHub: {str(e)}')
    
    return redirect('monitor_app:rucio_endpoints_list')
