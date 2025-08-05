"""
Common utility functions for the monitor application.
"""
from datetime import timedelta
from django.utils import timezone
from django.http import JsonResponse
from django.db.models import Q


def format_duration(delta, is_ongoing=False):
    """
    Format a timedelta to a human-readable duration string.
    
    Args:
        delta: timedelta object representing the duration
        is_ongoing: bool, whether to append "(ongoing)" to the result
        
    Returns:
        str: Formatted duration string
        - For durations < 24 hours: "HH:MM:SS"
        - For durations >= 24 hours: "Nd HH:MM:SS"
        - Appends " (ongoing)" if is_ongoing=True
        
    Examples:
        format_duration(timedelta(hours=2, minutes=15, seconds=30))
        # Returns: "02:15:30"
        
        format_duration(timedelta(days=5, hours=8, minutes=42, seconds=15))
        # Returns: "5d 08:42:15"
        
        format_duration(timedelta(days=1, hours=6), is_ongoing=True)
        # Returns: "1d 06:00:00 (ongoing)"
    """
    if not isinstance(delta, timedelta):
        return 'N/A'
    
    total_seconds = delta.total_seconds()
    if total_seconds < 0:
        return 'N/A'
    
    days, remainder = divmod(total_seconds, 86400)  # 86400 seconds in a day
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    if days > 0:
        duration_str = f"{int(days)}d {int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"
    else:
        duration_str = f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"
    
    if is_ongoing:
        duration_str += " (ongoing)"
    
    return duration_str


def format_run_duration(start_time, end_time=None):
    """
    Format the duration of a run, handling both completed and ongoing runs.
    
    Args:
        start_time: datetime when the run started
        end_time: datetime when the run ended (None for ongoing runs)
        
    Returns:
        str: Formatted duration string
    """
    if not start_time:
        return 'N/A'
    
    if end_time:
        # Completed run
        duration = end_time - start_time
        return format_duration(duration)
    else:
        # Ongoing run
        now = timezone.now()
        elapsed = now - start_time
        return format_duration(elapsed, is_ongoing=True)


def format_elapsed_time(start_time, reference_time=None):
    """
    Format elapsed time from a start time to now (or reference time).
    
    Args:
        start_time: datetime to calculate elapsed time from
        reference_time: datetime to calculate to (defaults to now)
        
    Returns:
        str: Formatted elapsed time string
    """
    if not start_time:
        return 'N/A'
    
    if reference_time is None:
        reference_time = timezone.now()
    
    elapsed = reference_time - start_time
    return format_duration(elapsed)


def format_datetime(dt):
    """
    Standard datetime formatting for all views in the monitor application.
    
    Args:
        dt: datetime object to format
        
    Returns:
        str: Formatted datetime string in YYYYMMDD HH:MM:SS format, or 'N/A' if None
        
    Example:
        format_datetime(datetime(2024, 8, 5, 14, 30, 15))
        # Returns: "20240805 14:30:15"
    """
    if not dt:
        return 'N/A'
    
    return dt.strftime('%Y%m%d %H:%M:%S')


class DataTablesProcessor:
    """
    Common processor for server-side DataTables AJAX requests.
    Handles pagination, searching, ordering, and filtering consistently.
    """
    
    def __init__(self, request, columns, default_order_column=0, default_order_direction='desc'):
        """
        Initialize DataTables processor with request parameters.
        
        Args:
            request: Django request object
            columns: List of column names that match template column order
            default_order_column: Default column index for ordering (0-based)
            default_order_direction: 'asc' or 'desc'
        """
        self.request = request
        self.columns = columns
        
        # Extract DataTables parameters
        self.draw = int(request.GET.get('draw', 1))
        self.start = int(request.GET.get('start', 0))
        self.length = int(request.GET.get('length', 100))
        self.search_value = request.GET.get('search[value]', '').strip()
        
        # Order parameters
        self.order_column_idx = int(request.GET.get('order[0][column]', default_order_column))
        self.order_direction = request.GET.get('order[0][dir]', default_order_direction)
        self.order_column = self.columns[self.order_column_idx] if 0 <= self.order_column_idx < len(self.columns) else self.columns[default_order_column]
    
    def get_order_by(self, special_cases=None):
        """
        Get the order_by string for queryset ordering.
        
        Args:
            special_cases: Dict mapping column names to custom order_by strings
            
        Returns:
            str: Order by string for queryset
        """
        if special_cases and self.order_column in special_cases:
            order_by = special_cases[self.order_column]
        else:
            order_by = self.order_column
        
        if self.order_direction == 'desc' and not order_by.startswith('-'):
            order_by = f'-{order_by}'
        elif self.order_direction == 'asc' and order_by.startswith('-'):
            order_by = order_by[1:]
            
        return order_by
    
    def apply_search(self, queryset, search_fields):
        """
        Apply search filtering to queryset.
        
        Args:
            queryset: Django queryset to filter
            search_fields: List of field names to search in
            
        Returns:
            Filtered queryset
        """
        if not self.search_value:
            return queryset
            
        search_q = Q()
        for field in search_fields:
            search_q |= Q(**{f'{field}__icontains': self.search_value})
        
        return queryset.filter(search_q)
    
    def apply_pagination(self, queryset):
        """
        Apply pagination to queryset.
        
        Args:
            queryset: Django queryset to paginate
            
        Returns:
            Paginated queryset slice
        """
        return queryset[self.start:self.start + self.length]
    
    def create_response(self, data, records_total, records_filtered):
        """
        Create standardized DataTables JSON response.
        
        Args:
            data: List of data rows for the table
            records_total: Total number of records before filtering
            records_filtered: Number of records after filtering
            
        Returns:
            JsonResponse object
        """
        return JsonResponse({
            'draw': self.draw,
            'recordsTotal': records_total,
            'recordsFiltered': records_filtered,
            'data': data
        })


def get_filter_params(request, param_names):
    """
    Extract filter parameters from request GET params.
    
    Args:
        request: Django request object
        param_names: List of parameter names to extract
        
    Returns:
        Dict of parameter_name: value pairs
    """
    return {param: request.GET.get(param) for param in param_names}


def apply_filters(queryset, filters):
    """
    Apply multiple filters to a queryset.
    Handles boolean field conversion from string values.
    
    Args:
        queryset: Django queryset to filter
        filters: Dict of field_name: value pairs
        
    Returns:
        Filtered queryset
    """
    for field, value in filters.items():
        # Apply filter for any non-None, non-empty value (including 'false' string)
        if value is not None and value != '':
            # Convert string boolean values to actual booleans for database filtering
            if value == 'true':
                filter_value = True
            elif value == 'false':
                filter_value = False
            else:
                filter_value = value
            
            queryset = queryset.filter(**{field: filter_value})
    return queryset


def get_filter_counts(queryset, filter_fields, current_filters=None):
    """
    Calculate counts for each possible filter value, considering current filters.
    Only returns options that have >0 matches in the current filtered dataset.
    
    Args:
        queryset: Base queryset to calculate counts from
        filter_fields: List of field names to calculate counts for
        current_filters: Dict of currently active filters to consider
        
    Returns:
        Dict of filter_field: [(value, count), ...] pairs, sorted by count desc
    """
    from django.db.models import Count
    
    if current_filters is None:
        current_filters = {}
    
    filter_counts = {}
    
    for field in filter_fields:
        # Start with base queryset
        field_queryset = queryset
        
        # Apply all current filters EXCEPT the one we're calculating counts for
        temp_filters = {k: v for k, v in current_filters.items() if k != field and v}
        field_queryset = apply_filters(field_queryset, temp_filters)
        
        # Get distinct values and their counts
        # Use the model's primary key field name instead of assuming 'id'
        pk_field = field_queryset.model._meta.pk.name
        counts = (field_queryset
                 .values(field)
                 .annotate(count=Count(pk_field))
                 .filter(count__gt=0)  # Only include options with >0 matches
                 .order_by('-count', field))
        
        # Convert to list of tuples: (value, count)
        # Include all values, including False for boolean fields (don't filter out falsy values)
        filter_counts[field] = [(item[field], item['count']) for item in counts if item[field] is not None]
    
    return filter_counts