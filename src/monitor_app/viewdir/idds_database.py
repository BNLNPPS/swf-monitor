"""
iDDS Database browsing views.

This module provides views for browsing the external iDDS database
following the same patterns as the PanDA database browser.
"""

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.db import connections
from datetime import datetime, date


@login_required
def idds_database_tables_list(request):
    """
    iDDS database tables list view using server-side DataTables.
    Shows all tables in the iDDS database with counts and last insert times.
    """
    from django.urls import reverse
    
    # Column definitions for DataTables
    columns = [
        {'name': 'name', 'title': 'Table Name', 'orderable': True},
        {'name': 'count', 'title': 'Row Count', 'orderable': True},
        {'name': 'last_insert', 'title': 'Last Insert', 'orderable': True},
    ]
    
    context = {
        'table_title': 'iDDS Database Overview',
        'table_description': 'Server-side processing view of all tables in the iDDS database with row counts and last insert times.',
        'ajax_url': reverse('monitor_app:idds_database_tables_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'monitor_app/database_tables_server.html', context)


@login_required
def idds_database_tables_datatable_ajax(request):
    """
    AJAX endpoint for server-side DataTables processing of iDDS database tables.
    """
    from ..utils import DataTablesProcessor, format_datetime
    
    # Initialize DataTables processor
    columns = ['name', 'count', 'last_insert']
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='asc')
    
    # Get iDDS database connection
    idds_connection = connections['idds']
    
    # Build table metadata
    table_records = []
    try:
        with idds_connection.cursor() as cursor:
            # Get list of all tables in the iDDS database schema
            # Note: iDDS uses a specific schema, so we need to query that schema
            from django.conf import settings
            
            # Extract schema name from database configuration
            schema_name = 'doma_idds'  # Default from environment
            try:
                db_config = settings.DATABASES.get('idds', {})
                if 'OPTIONS' in db_config and 'options' in db_config['OPTIONS']:
                    options_str = db_config['OPTIONS']['options']
                    if 'search_path=' in options_str:
                        schema_name = options_str.split('search_path=')[1]
            except:
                pass
            
            cursor.execute("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = %s 
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """, [schema_name])
            
            tables = [row[0] for row in cursor.fetchall()]
            
            for table_name in tables:
                record = {
                    'name': table_name,
                    'count': 0,
                    'last_insert': None
                }
                
                try:
                    # Get row count - use schema-qualified table name
                    cursor.execute(f'SELECT COUNT(*) FROM "{schema_name}"."{table_name}"')
                    record['count'] = cursor.fetchone()[0]
                    
                    # Try to find a timestamp column for last insert
                    cursor.execute("""
                        SELECT column_name 
                        FROM information_schema.columns 
                        WHERE table_schema = %s
                        AND table_name = %s 
                        AND data_type IN ('timestamp', 'timestamp with time zone', 'timestamp without time zone')
                        ORDER BY ordinal_position
                        LIMIT 1
                    """, [schema_name, table_name])
                    
                    timestamp_col = cursor.fetchone()
                    if timestamp_col:
                        cursor.execute(f'SELECT MAX("{timestamp_col[0]}") FROM "{schema_name}"."{table_name}"')
                        result = cursor.fetchone()
                        if result and result[0]:
                            record['last_insert'] = result[0]
                            
                except Exception:
                    pass  # Skip tables we can't access
                
                table_records.append(record)
                
    except Exception as e:
        # If we can't connect to iDDS database, return empty result
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
        table_url = reverse('monitor_app:idds_database_table_list', args=[record['name']])
        table_link = f'<a href="{table_url}">{record["name"]}</a>'
        count_str = str(record['count'])
        last_insert_str = format_datetime(record['last_insert'])
        
        data.append([table_link, count_str, last_insert_str])
    
    return dt.create_response(data, records_total, records_filtered)


@login_required
def idds_database_table_list(request, table_name):
    """
    iDDS database individual table view.
    """
    from django.urls import reverse
    
    # Get iDDS database connection
    idds_connection = connections['idds']
    
    # Get column information
    columns = []
    try:
        with idds_connection.cursor() as cursor:
            # Get schema name from settings
            from django.conf import settings
            schema_name = 'doma_idds'  # Default from environment
            try:
                db_config = settings.DATABASES.get('idds', {})
                if 'OPTIONS' in db_config and 'options' in db_config['OPTIONS']:
                    options_str = db_config['OPTIONS']['options']
                    if 'search_path=' in options_str:
                        schema_name = options_str.split('search_path=')[1]
            except:
                pass
            
            cursor.execute(f'SELECT * FROM "{schema_name}"."{table_name}" LIMIT 1')
            columns = [col[0] for col in cursor.description]
    except Exception:
        raise Http404()
    
    # Convert columns for DataTables format
    datatable_columns = [{'name': col, 'title': col.replace('_', ' ').title(), 'orderable': True} for col in columns]
    
    context = {
        'table_title': f'iDDS Table: {table_name}',
        'table_description': f'iDDS database table contents for {table_name} with search, sorting, and pagination.',
        'ajax_url': reverse('monitor_app:idds_database_table_datatable_ajax', kwargs={'table_name': table_name}),
        'columns': datatable_columns,
        'table_name': table_name,
    }
    return render(request, 'monitor_app/database_table_list.html', context)


@login_required
def idds_database_table_datatable_ajax(request, table_name):
    """
    AJAX endpoint for server-side DataTables processing of individual iDDS database table.
    """
    from ..utils import DataTablesProcessor, format_datetime
    
    # Get iDDS database connection
    idds_connection = connections['idds']
    
    # Get column information
    columns = []
    try:
        with idds_connection.cursor() as cursor:
            # Get schema name from settings
            from django.conf import settings
            schema_name = 'doma_idds'  # Default from environment
            try:
                db_config = settings.DATABASES.get('idds', {})
                if 'OPTIONS' in db_config and 'options' in db_config['OPTIONS']:
                    options_str = db_config['OPTIONS']['options']
                    if 'search_path=' in options_str:
                        schema_name = options_str.split('search_path=')[1]
            except:
                pass
            
            cursor.execute(f'SELECT * FROM "{schema_name}"."{table_name}" LIMIT 1')
            columns = [col[0] for col in cursor.description]
    except Exception:
        raise Http404()
    
    # Initialize DataTables processor
    dt = DataTablesProcessor(request, columns, default_order_column=0, default_order_direction='asc')
    
    # Get schema name from settings
    from django.conf import settings
    schema_name = 'doma_idds'  # Default from environment
    try:
        db_config = settings.DATABASES.get('idds', {})
        if 'OPTIONS' in db_config and 'options' in db_config['OPTIONS']:
            options_str = db_config['OPTIONS']['options']
            if 'search_path=' in options_str:
                schema_name = options_str.split('search_path=')[1]
    except:
        pass
    
    # Build base query - use schema-qualified table name
    query = f'SELECT * FROM "{schema_name}"."{table_name}"'
    count_query = f'SELECT COUNT(*) FROM "{schema_name}"."{table_name}"'
    params = []
    
    try:
        with idds_connection.cursor() as cursor:
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
                    if value is None:
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