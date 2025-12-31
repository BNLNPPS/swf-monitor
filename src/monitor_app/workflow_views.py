"""
Workflow-specific views for the SWF monitor application.
"""

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.db.models import Count
from django.utils import timezone

from .workflow_models import WorkflowDefinition, WorkflowExecution


@login_required
def workflows_home(request):
    """Workflows landing page with links to different workflow views."""
    return render(request, 'monitor_app/workflows_home.html')


@login_required
def workflow_definitions_list(request):
    """
    Professional workflow definitions list view using server-side DataTables.
    """
    # Get filter parameters (for initial state)
    workflow_name = request.GET.get('workflow_name')
    workflow_type = request.GET.get('workflow_type')
    created_by = request.GET.get('created_by')

    # Get unique values for filter links
    workflow_names = WorkflowDefinition.objects.values_list('workflow_name', flat=True).distinct().order_by('workflow_name')
    workflow_types = WorkflowDefinition.objects.values_list('workflow_type', flat=True).distinct().order_by('workflow_type')
    created_bys = WorkflowDefinition.objects.values_list('created_by', flat=True).distinct().order_by('created_by')

    columns = [
        {'name': 'workflow_name', 'title': 'Workflow Name', 'orderable': True},
        {'name': 'version', 'title': 'Version', 'orderable': True},
        {'name': 'workflow_type', 'title': 'Type', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
        {'name': 'execution_count', 'title': 'Executions', 'orderable': True},
        {'name': 'actions', 'title': 'Actions', 'orderable': False},
    ]

    filter_fields = [
        {'name': 'workflow_type', 'label': 'Type', 'type': 'select'},
        {'name': 'created_by', 'label': 'Created By', 'type': 'select'},
    ]

    context = {
        'table_title': 'Workflow Definitions',
        'table_description': 'View and manage workflow templates and configurations.',
        'ajax_url': reverse('monitor_app:workflow_definitions_datatable_ajax'),
        'filter_counts_url': reverse('monitor_app:workflow_definitions_filter_counts'),
        'columns': columns,
        'filter_fields': filter_fields,
        'workflow_names': list(workflow_names),
        'workflow_types': list(workflow_types),
        'created_bys': list(created_bys),
        'selected_workflow_name': workflow_name,
        'selected_workflow_type': workflow_type,
        'selected_created_by': created_by,
    }
    return render(request, 'monitor_app/workflow_definitions_list.html', context)


def workflow_definitions_datatable_ajax(request):
    """AJAX endpoint for server-side DataTables processing of workflow definitions."""
    from .utils import DataTablesProcessor, format_datetime

    columns = ['workflow_name', 'version', 'workflow_type', 'created_by', 'created_at', 'execution_count', 'actions']
    dt = DataTablesProcessor(request, columns, default_order_column=4, default_order_direction='desc')

    # Build queryset with execution count
    queryset = WorkflowDefinition.objects.annotate(
        execution_count=Count('executions')
    )

    # Apply filters
    workflow_name = request.GET.get('workflow_name')
    if workflow_name:
        queryset = queryset.filter(workflow_name=workflow_name)

    workflow_type = request.GET.get('workflow_type')
    if workflow_type:
        queryset = queryset.filter(workflow_type=workflow_type)

    created_by = request.GET.get('created_by')
    if created_by:
        queryset = queryset.filter(created_by=created_by)

    # Get counts and apply search/pagination
    records_total = WorkflowDefinition.objects.count()
    search_fields = ['workflow_name', 'version', 'workflow_type', 'created_by']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()

    queryset = queryset.order_by(dt.get_order_by())
    definitions = dt.apply_pagination(queryset)

    # Format data for DataTables
    data = []
    for definition in definitions:
        data.append([
            f'<a href="{reverse("monitor_app:workflow_definition_detail", args=[definition.workflow_name, definition.version])}" class="text-decoration-none">{definition.workflow_name}</a>',
            definition.version,
            definition.workflow_type,
            definition.created_by,
            format_datetime(definition.created_at),
            definition.execution_count,
            f'<a href="{reverse("monitor_app:workflow_definition_detail", args=[definition.workflow_name, definition.version])}" class="btn btn-sm btn-outline-primary">View</a>'
        ])

    return dt.create_response(data, records_total, records_filtered)


def workflow_definitions_filter_counts(request):
    """AJAX endpoint for dynamic filter counts."""
    workflow_type_counts = WorkflowDefinition.objects.values('workflow_type').annotate(count=Count('id')).order_by('workflow_type')
    created_by_counts = WorkflowDefinition.objects.values('created_by').annotate(count=Count('id')).order_by('created_by')

    return JsonResponse({
        'workflow_type': list(workflow_type_counts),
        'created_by': list(created_by_counts),
    })


@login_required
def workflow_executions_list(request):
    """
    Professional workflow executions list view using server-side DataTables.
    """
    # Get filter parameters (for initial state)
    workflow = request.GET.get('workflow')
    status = request.GET.get('status')
    executed_by = request.GET.get('executed_by')
    namespace = request.GET.get('namespace')

    # Get unique values for filter links
    workflows = WorkflowExecution.objects.select_related('workflow_definition').values_list(
        'workflow_definition__workflow_name', flat=True
    ).distinct().order_by('workflow_definition__workflow_name')

    statuses = WorkflowExecution.objects.values_list('status', flat=True).distinct().order_by('status')
    executed_bys = WorkflowExecution.objects.values_list('executed_by', flat=True).distinct().order_by('executed_by')
    namespaces = WorkflowExecution.objects.exclude(namespace__isnull=True).exclude(namespace='').values_list(
        'namespace', flat=True
    ).distinct().order_by('namespace')

    columns = [
        {'name': 'execution_id', 'title': 'Execution ID', 'orderable': True},
        {'name': 'workflow', 'title': 'Workflow', 'orderable': True},
        {'name': 'namespace', 'title': 'Namespace', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'stf_count', 'title': 'STFs', 'orderable': False},
        {'name': 'executed_by', 'title': 'Executed By', 'orderable': True},
        {'name': 'start_time', 'title': 'Started', 'orderable': True},
        {'name': 'duration', 'title': 'Duration', 'orderable': True},
        {'name': 'actions', 'title': 'Actions', 'orderable': False},
    ]

    filter_fields = [
        {'name': 'status', 'label': 'Status', 'type': 'select'},
        {'name': 'executed_by', 'label': 'Executed By', 'type': 'select'},
    ]

    context = {
        'table_title': 'Workflow Executions',
        'table_description': 'Monitor workflow execution instances and their status.',
        'ajax_url': reverse('monitor_app:workflow_executions_datatable_ajax'),
        'filter_counts_url': reverse('monitor_app:workflow_executions_filter_counts'),
        'columns': columns,
        'filter_fields': filter_fields,
        'workflows': list(workflows),
        'statuses': list(statuses),
        'executed_bys': list(executed_bys),
        'namespaces': list(namespaces),
        'selected_workflow': workflow,
        'selected_status': status,
        'selected_executed_by': executed_by,
        'selected_namespace': namespace,
    }
    return render(request, 'monitor_app/workflow_executions_list.html', context)


def workflow_executions_datatable_ajax(request):
    """AJAX endpoint for server-side DataTables processing of workflow executions."""
    from .utils import DataTablesProcessor, format_datetime, format_duration
    from .workflow_models import WorkflowMessage

    columns = ['execution_id', 'workflow', 'namespace', 'status', 'stf_count', 'executed_by', 'start_time', 'duration', 'actions']
    dt = DataTablesProcessor(request, columns, default_order_column=6, default_order_direction='desc')

    # Build queryset
    queryset = WorkflowExecution.objects.select_related('workflow_definition')

    # Apply filters
    workflow = request.GET.get('workflow')
    if workflow:
        queryset = queryset.filter(workflow_definition__workflow_name=workflow)

    status = request.GET.get('status')
    if status:
        queryset = queryset.filter(status=status)

    executed_by = request.GET.get('executed_by')
    if executed_by:
        queryset = queryset.filter(executed_by=executed_by)

    namespace = request.GET.get('namespace')
    if namespace:
        queryset = queryset.filter(namespace=namespace)

    # Get counts and apply search/pagination
    records_total = WorkflowExecution.objects.count()
    search_fields = ['execution_id', 'workflow_definition__workflow_name', 'status', 'executed_by']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()

    queryset = queryset.order_by(dt.get_order_by())
    executions = dt.apply_pagination(queryset)

    # Format data for DataTables
    data = []
    for execution in executions:
        # Calculate duration
        if execution.end_time:
            duration = execution.end_time - execution.start_time
            duration_str = format_duration(duration)
        elif execution.status == 'running':
            duration = timezone.now() - execution.start_time
            duration_str = format_duration(duration, is_ongoing=True)
        else:
            duration_str = '-'

        # Count STF messages for this execution
        stf_count = WorkflowMessage.objects.filter(
            execution_id=execution.execution_id,
            message_type='stf_gen'
        ).count()

        # Format namespace as link
        if execution.namespace:
            namespace_link = f'<a href="{reverse("monitor_app:namespace_detail", args=[execution.namespace])}">{execution.namespace}</a>'
        else:
            namespace_link = ''

        data.append([
            f'<a href="{reverse("monitor_app:workflow_execution_detail", args=[execution.execution_id])}" class="text-decoration-none">{execution.execution_id}</a>',
            f"{execution.workflow_definition.workflow_name} v{execution.workflow_definition.version}",
            namespace_link,
            execution.status,
            str(stf_count),
            execution.executed_by,
            format_datetime(execution.start_time),
            duration_str,
            f'<a href="{reverse("monitor_app:workflow_execution_detail", args=[execution.execution_id])}" class="btn btn-sm btn-outline-primary">View</a>'
        ])

    return dt.create_response(data, records_total, records_filtered)


def workflow_executions_filter_counts(request):
    """AJAX endpoint for dynamic filter counts."""
    status_counts = WorkflowExecution.objects.values('status').annotate(count=Count('id')).order_by('status')
    executed_by_counts = WorkflowExecution.objects.values('executed_by').annotate(count=Count('id')).order_by('executed_by')

    return JsonResponse({
        'status': list(status_counts),
        'executed_by': list(executed_by_counts),
    })


@login_required
def workflow_definition_detail(request, workflow_name, version):
    """Detail view for a specific workflow definition."""
    definition = get_object_or_404(WorkflowDefinition, workflow_name=workflow_name, version=version)

    # Get execution count for summary
    total_executions = definition.executions.count()

    context = {
        'definition': definition,
        'total_executions': total_executions,
    }
    return render(request, 'monitor_app/workflow_definition_detail.html', context)


@login_required
def workflow_execution_detail(request, execution_id):
    """Detail view for a specific workflow execution."""
    execution = get_object_or_404(WorkflowExecution, execution_id=execution_id)

    # Calculate duration if completed
    duration_text = None
    if execution.end_time and execution.start_time:
        delta = execution.end_time - execution.start_time
        total_seconds = delta.total_seconds()

        minutes = int(total_seconds // 60)
        seconds = total_seconds % 60

        if minutes > 0:
            duration_text = f"{minutes} minutes, {seconds:.2f} seconds"
        else:
            duration_text = f"{seconds:.2f} seconds"

    context = {
        'execution': execution,
        'duration_text': duration_text,
    }
    return render(request, 'monitor_app/workflow_execution_detail.html', context)