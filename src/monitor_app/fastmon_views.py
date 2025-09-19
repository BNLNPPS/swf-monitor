"""
FastMon-specific views for Time Frame file monitoring.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import JsonResponse
from django.urls import reverse
from .models import FastMonFile
from .utils import DataTablesProcessor, get_filter_params, format_datetime


@login_required
def fastmon_files_list(request):
    """
    Professional FastMon files list view using server-side DataTables.
    Provides high-performance access to all Time Frame (TF) file records with filtering.
    """
    # Get filter parameters (for initial state)
    stf_filename = request.GET.get('stf_filename')
    status_filter = request.GET.get('status')
    run_number = request.GET.get('run_number')

    # Get filter options for dropdown links
    # Get unique STF filenames that have TF files
    stf_filenames = FastMonFile.objects.select_related('stf_file').values_list(
        'stf_file__stf_filename', flat=True
    ).distinct()

    # Get run numbers from related STF files
    run_numbers = FastMonFile.objects.select_related('stf_file__run').values_list(
        'stf_file__run__run_number', flat=True
    ).distinct()

    # Get status choices from the model
    statuses = [choice[0] for choice in FastMonFile._meta.get_field('status').choices]

    # Column definitions for DataTables
    columns = [
        {'name': 'tf_filename', 'title': 'TF Filename', 'orderable': True},
        {'name': 'stf_file__stf_filename', 'title': 'Parent STF', 'orderable': True},
        {'name': 'stf_file__run__run_number', 'title': 'Run', 'orderable': True},
        {'name': 'file_size_bytes', 'title': 'Size (bytes)', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
    ]

    context = {
        'table_title': 'FastMon Files (Time Frames)',
        'table_description': 'Track Time Frame (TF) files sampled from Super Time Frames for fast monitoring.',
        'ajax_url': reverse('monitor_app:fastmon_files_datatable_ajax'),
        'columns': columns,
        'stf_filenames': sorted([s for s in stf_filenames if s]),
        'run_numbers': sorted(run_numbers, reverse=True),
        'statuses': statuses,
        'selected_stf_filename': stf_filename,
        'selected_status': status_filter,
        'selected_run_number': run_number,
    }
    return render(request, 'monitor_app/fastmon_files_list.html', context)


def fastmon_files_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of FastMon files.
    """
    # Initialize DataTables processor
    columns = ['tf_filename', 'stf_file__stf_filename', 'stf_file__run__run_number',
               'file_size_bytes', 'status', 'created_at']
    dt = DataTablesProcessor(request, columns, default_order_column=5, default_order_direction='desc')

    # Build base queryset with related data
    queryset = FastMonFile.objects.select_related('stf_file', 'stf_file__run')

    # Apply filters using utility
    filter_mapping = {
        'stf_filename': 'stf_file__stf_filename',
        'status': 'status',
        'run_number': 'stf_file__run__run_number'
    }
    filters = get_filter_params(request, filter_mapping.keys())
    # Apply filters with correct field names
    for param_name, field_name in filter_mapping.items():
        if filters[param_name]:
            queryset = queryset.filter(**{field_name: filters[param_name]})

    # Get counts and apply search/pagination
    records_total = FastMonFile.objects.count()
    search_fields = ['tf_filename', 'stf_file__stf_filename', 'stf_file__run__run_number']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()

    # Apply ordering and pagination
    queryset = queryset.order_by(dt.get_order_by())
    fastmon_files = dt.apply_pagination(queryset)

    # Format data for DataTables
    data = []
    for file in fastmon_files:
        # Use plain text status (consistent with STF files view)
        status_text = file.get_status_display()

        # Format file size with commas for readability
        file_size = f"{file.file_size_bytes:,}" if file.file_size_bytes else "N/A"

        # Make STF filename clickable to go to STF detail page
        from django.urls import reverse
        stf_detail_url = reverse('monitor_app:stf_file_detail', args=[file.stf_file.file_id])
        stf_link = f'<a href="{stf_detail_url}">{file.stf_file.stf_filename}</a>'

        # Make run number clickable to go to run detail page
        run_detail_url = reverse('monitor_app:run_detail', args=[file.stf_file.run.run_number])
        run_link = f'<a href="{run_detail_url}">{file.stf_file.run.run_number}</a>'

        data.append([
            file.tf_filename,
            stf_link,
            run_link,
            file_size,
            status_text,
            format_datetime(file.created_at)
        ])

    # Return DataTables-formatted response
    return JsonResponse({
        'draw': dt.draw,
        'recordsTotal': records_total,
        'recordsFiltered': records_filtered,
        'data': data
    })