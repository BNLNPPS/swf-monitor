"""
TF Slices views for fast processing workflow monitoring.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import JsonResponse
from django.urls import reverse
from .models import TFSlice
from .utils import DataTablesProcessor, get_filter_params, format_datetime


@login_required
def tf_slices_list(request):
    """
    TF slices list view using server-side DataTables.
    Provides high-performance access to all TF slice records with filtering.
    """
    # Get filter parameters
    tf_filename = request.GET.get('tf_filename')
    stf_filename = request.GET.get('stf_filename')
    status_filter = request.GET.get('status')
    run_number = request.GET.get('run_number')

    # Get filter options for dropdown links
    tf_filenames = TFSlice.objects.values_list(
        'tf_filename', flat=True
    ).distinct()

    stf_filenames = TFSlice.objects.values_list(
        'stf_filename', flat=True
    ).distinct()

    run_numbers = TFSlice.objects.values_list(
        'run_number', flat=True
    ).distinct()

    # Get unique status values (no choices defined, so get from DB)
    statuses = TFSlice.objects.values_list('status', flat=True).distinct()

    # Column definitions for DataTables
    columns = [
        {'name': 'slice_id', 'title': 'Slice ID', 'orderable': True},
        {'name': 'tf_filename', 'title': 'TF Sample', 'orderable': True},
        {'name': 'stf_filename', 'title': 'STF File', 'orderable': True},
        {'name': 'run_number', 'title': 'Run', 'orderable': True},
        {'name': 'tf_first', 'title': 'TF Range', 'orderable': True},
        {'name': 'tf_count', 'title': 'TF Count', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'assigned_worker', 'title': 'Worker', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
    ]

    context = {
        'table_title': 'TF Slices (Fast Processing)',
        'table_description': 'Track Time Frame slices for parallel worker processing in fast processing workflow.',
        'ajax_url': reverse('monitor_app:tf_slices_datatable_ajax'),
        'columns': columns,
        'tf_filenames': sorted([f for f in tf_filenames if f]),
        'stf_filenames': sorted([f for f in stf_filenames if f]),
        'run_numbers': sorted(run_numbers, reverse=True),
        'statuses': sorted([s for s in statuses if s]),
        'selected_tf_filename': tf_filename,
        'selected_stf_filename': stf_filename,
        'selected_status': status_filter,
        'selected_run_number': run_number,
    }
    return render(request, 'monitor_app/tf_slices_list.html', context)


def tf_slices_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of TF slices.
    """
    # Initialize DataTables processor
    columns = ['slice_id', 'tf_filename', 'stf_filename', 'run_number',
               'tf_first', 'tf_count', 'status', 'assigned_worker', 'created_at']
    dt = DataTablesProcessor(request, columns, default_order_column=8, default_order_direction='desc')

    # Build base queryset
    queryset = TFSlice.objects.all()

    # Apply filters
    filter_mapping = {
        'tf_filename': 'tf_filename',
        'stf_filename': 'stf_filename',
        'status': 'status',
        'run_number': 'run_number'
    }
    filters = get_filter_params(request, filter_mapping.keys())
    for param_name, field_name in filter_mapping.items():
        if filters[param_name]:
            queryset = queryset.filter(**{field_name: filters[param_name]})

    # Get counts and apply search/pagination
    records_total = TFSlice.objects.count()
    search_fields = ['slice_id', 'tf_filename', 'stf_filename', 'run_number', 'status', 'assigned_worker']
    queryset = dt.apply_search(queryset, search_fields)
    records_filtered = queryset.count()

    # Apply ordering and pagination
    queryset = queryset.order_by(dt.get_order_by())
    slices = dt.apply_pagination(queryset)

    # Format data for DataTables
    data = []
    for slice_obj in slices:
        # TF range display
        tf_range = f"{slice_obj.tf_first}-{slice_obj.tf_last}"

        # Worker display
        worker_display = slice_obj.assigned_worker if slice_obj.assigned_worker else "-"

        data.append([
            slice_obj.slice_id,
            slice_obj.tf_filename,
            slice_obj.stf_filename,
            slice_obj.run_number,
            tf_range,
            slice_obj.tf_count,
            slice_obj.status,
            worker_display,
            format_datetime(slice_obj.created_at)
        ])

    # Return DataTables-formatted response
    return JsonResponse({
        'draw': dt.draw,
        'recordsTotal': records_total,
        'recordsFiltered': records_filtered,
        'data': data
    })
