"""
PanDA Database browsing views.

This module provides views for browsing the external PanDA database
following the same patterns as the existing SWF database browser.
"""

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.db import connections
from datetime import datetime, date


@login_required
def panda_database_tables_list(request):
    """
    PanDA database tables list view using server-side DataTables.
    Shows all tables in the PanDA database with counts and last insert times.
    """
    from django.urls import reverse
    
    # Column definitions for DataTables
    columns = [
        {'name': 'name', 'title': 'Table Name', 'orderable': True},
        {'name': 'count', 'title': 'Row Count', 'orderable': True},
        {'name': 'last_insert', 'title': 'Last Insert', 'orderable': True},
    ]
    
    context = {
        'table_title': 'PanDA Database Overview',
        'table_description': 'Server-side processing view of all tables in the PanDA database with row counts and last insert times.',
        'ajax_url': reverse('monitor_app:panda_database_tables_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'monitor_app/database_tables_server.html', context)


@login_required
def panda_database_tables_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of PanDA database tables.
    """
    from ..utils import DataTablesProcessor, format_datetime
    
    # Initialize DataTables processor
    columns = ['name', 'count', 'last_insert']
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='asc')
    
    # Get PanDA database connection
    panda_connection = connections['panda']
    
    # Build table metadata
    table_records = []
    try:
        with panda_connection.cursor() as cursor:
            # Get list of all tables in the PanDA database
            # PanDA uses the doma_panda schema, not public
            cursor.execute("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'doma_panda' 
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """)
            tables = [row[0] for row in cursor.fetchall()]
            
            for table_name in tables:
                record = {
                    'name': table_name,
                    'count': 0,
                    'last_insert': None
                }
                
                try:
                    # Get row count - use schema-qualified table name
                    cursor.execute(f'SELECT COUNT(*) FROM "doma_panda"."{table_name}"')
                    record['count'] = cursor.fetchone()[0]
                    
                    # Try to find a timestamp column for last insert
                    cursor.execute("""
                        SELECT column_name 
                        FROM information_schema.columns 
                        WHERE table_schema = 'doma_panda'
                        AND table_name = %s 
                        AND data_type IN ('timestamp', 'timestamp with time zone', 'timestamp without time zone')
                        ORDER BY ordinal_position
                        LIMIT 1
                    """, [table_name])
                    
                    timestamp_col = cursor.fetchone()
                    if timestamp_col:
                        cursor.execute(f'SELECT MAX("{timestamp_col[0]}") FROM "doma_panda"."{table_name}"')
                        result = cursor.fetchone()
                        if result and result[0]:
                            record['last_insert'] = result[0]
                            
                except Exception:
                    pass  # Skip tables we can't access
                
                table_records.append(record)
                
    except Exception as e:
        # If we can't connect to PanDA database, return empty result
        table_records = []
    
    # Get total counts
    records_total = len(table_records)
    
    # Apply search filtering
    if dt.search_value:
        search_term = dt.search_value.lower()
        table_records = [r for r in table_records if search_term in r['name'].lower()]
    
    records_filtered = len(table_records)
    
    # Apply ordering
    table_records.sort(key=lambda r: (r[dt.order_column] is None, r[dt.order_column]), reverse=(dt.order_direction == 'desc'))
    
    # Apply pagination
    start = dt.start
    length = dt.length if dt.length > 0 else len(table_records)
    paginated_records = table_records[start:start + length]
    
    # Format data for DataTables
    data = []
    for record in paginated_records:
        from django.urls import reverse
        table_url = reverse('monitor_app:panda_database_table_list', args=[record['name']])
        table_link = f'<a href="{table_url}">{record["name"]}</a>'
        count_str = str(record['count'])
        last_insert_str = format_datetime(record['last_insert'])
        
        data.append([table_link, count_str, last_insert_str])
    
    return dt.create_response(data, records_total, records_filtered)


@login_required
def panda_database_table_list(request, table_name):
    """
    PanDA database individual table view (excluding problematic TEXT columns).
    """
    from django.urls import reverse
    
    # Get PanDA database connection
    panda_connection = connections['panda']
    
    # Get column information (exclude problematic TEXT columns)
    columns = []
    try:
        with panda_connection.cursor() as cursor:
            # Get first 20 columns only
            cursor.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_schema = 'doma_panda' 
                AND table_name = %s 
                ORDER BY ordinal_position
                LIMIT 20
            """, [table_name])
            columns = [row[0] for row in cursor.fetchall()]
            if not columns:
                raise Http404()
    except Exception:
        raise Http404()
    
    # Convert columns for DataTables format
    datatable_columns = [{'name': col, 'title': col.replace('_', ' ').title(), 'orderable': True} for col in columns]
    
    context = {
        'table_title': f'PanDA Table: {table_name}',
        'table_description': f'PanDA database table contents for {table_name} (first 20 columns only). Search, sorting, and pagination available.',
        'ajax_url': reverse('monitor_app:panda_database_table_datatable_ajax', kwargs={'table_name': table_name}),
        'columns': datatable_columns,
        'table_name': table_name,
    }
    return render(request, 'monitor_app/database_table_list.html', context)


@login_required
def panda_database_table_datatable_ajax(request, table_name):
    """
    AJAX endpoint for server-side DataTables processing of individual PanDA database table.
    """
    from ..utils import DataTablesProcessor, format_datetime
    
    # Get PanDA database connection
    panda_connection = connections['panda']
    
    # Get column information (exclude problematic TEXT columns)
    columns = []
    try:
        with panda_connection.cursor() as cursor:
            # Get first 20 columns only
            cursor.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_schema = 'doma_panda' 
                AND table_name = %s 
                ORDER BY ordinal_position
                LIMIT 20
            """, [table_name])
            columns = [row[0] for row in cursor.fetchall()]
            if not columns:
                raise Http404()
    except Exception:
        raise Http404()
    
    # Initialize DataTables processor
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='asc')
    
    # Build base query - select columns excluding large TEXT fields
    column_list = ', '.join([f'"{col}"' for col in columns])
    query = f'SELECT {column_list} FROM "doma_panda"."{table_name}"'
    count_query = f'SELECT COUNT(*) FROM "doma_panda"."{table_name}"'
    params = []
    
    try:
        with panda_connection.cursor() as cursor:
            # Get total count
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
            cursor.execute(filtered_count_query, params)
            records_filtered = cursor.fetchone()[0]
            
            # Apply ordering
            if dt.order_column:
                order_direction = 'ASC' if dt.order_direction == 'asc' else 'DESC'
                filtered_query += f' ORDER BY "{dt.order_column}" {order_direction}'
            
            # Apply pagination
            filtered_query += f' LIMIT {dt.length} OFFSET {dt.start}'
            
            # Execute main query
            cursor.execute(filtered_query, params)
            rows = cursor.fetchall()
            
            # Format data for DataTables
            data = []
            for row in rows:
                formatted_row = []
                for i, value in enumerate(row):
                    if i == 0:  # First column should be a link to detail page
                        from django.urls import reverse
                        detail_url = reverse('monitor_app:panda_database_table_row_detail', args=[table_name, str(value)])
                        formatted_row.append(f'<a href="{detail_url}">{str(value) if value is not None else ""}</a>')
                    elif value is None:
                        formatted_row.append('')
                    elif isinstance(value, (datetime, date)):
                        formatted_row.append(format_datetime(value))
                    else:
                        formatted_row.append(str(value))
                data.append(formatted_row)
            
    except Exception as e:
        # Return empty result on error
        data = []
        records_total = 0
        records_filtered = 0
    
    return dt.create_response(data, records_total, records_filtered)


@login_required
def panda_database_table_row_detail(request, table_name, row_id):
    """
    PanDA database individual row detail view.
    Shows all column values for a specific row.
    """
    # Get PanDA database connection
    panda_connection = connections['panda']
    
    # Get all columns for this table (all columns for detail view)
    columns = []
    try:
        with panda_connection.cursor() as cursor:
            cursor.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_schema = 'doma_panda' 
                AND table_name = %s 
                ORDER BY ordinal_position
            """, [table_name])
            columns = [row[0] for row in cursor.fetchall()]
            if not columns:
                raise Http404("Table not found")
    except Exception:
        raise Http404("Table not found")
    
    # Get the row data using the first column as identifier
    row_data = {}
    try:
        with panda_connection.cursor() as cursor:
            # Build query to get the specific row
            column_list = ', '.join([f'"{col}"' for col in columns])
            query = f'SELECT {column_list} FROM "doma_panda"."{table_name}" WHERE "{columns[0]}" = %s LIMIT 1'
            
            cursor.execute(query, [row_id])
            row = cursor.fetchone()
            
            if not row:
                raise Http404("Row not found")
                
            # Create list of (column, value) pairs
            row_data = []
            for i, column in enumerate(columns):
                value = row[i]
                if value is None:
                    formatted_value = None
                elif isinstance(value, (datetime, date)):
                    from ..utils import format_datetime
                    formatted_value = format_datetime(value)
                else:
                    formatted_value = str(value)
                row_data.append((column, formatted_value))
                    
    except Exception:
        raise Http404("Row not found")
    
    context = {
        'table_name': table_name,
        'row_id': row_id,
        'row_data': row_data,
    }
    return render(request, 'monitor_app/panda_table_row_detail.html', context)


