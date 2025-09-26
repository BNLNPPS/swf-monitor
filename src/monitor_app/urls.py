from django.urls import path, include
from .views import (
    home,
    authenticated_home,
    about,
    index,
    system_agent_create,
    system_agent_update,
    system_agent_delete,
    account_view,
    log_summary,
    log_summary_datatable_ajax,
    log_list,
    logs_datatable_ajax,
    get_log_filter_counts,
    runs_datatable_ajax,
    stf_files_datatable_ajax,
    database_tables_list,
    database_tables_datatable_ajax,
    database_table_list,
    database_table_datatable_ajax,
    runs_list,
    run_detail,
    stf_files_list,
    stf_file_detail,
    subscribers_list,
    subscriber_detail,
    # Workflow views
    workflow_detail,
    workflow_agents_list,
    agent_detail,
    workflow_messages,
    workflow_realtime_data_api,
    workflow_datatable_ajax,
    workflow_agents_datatable_ajax,
    workflow_messages_datatable_ajax,
    get_workflow_messages_filter_counts,
    subscribers_datatable_ajax,
    get_subscribers_filter_counts,
    persistent_state_view,
    # PanDA and Rucio views
    panda_queues_list,
    panda_queues_datatable_ajax,
    panda_queue_detail,
    panda_queue_json,
    rucio_endpoints_list,
    rucio_endpoints_datatable_ajax,
    rucio_endpoint_detail,
    rucio_endpoint_json,
    panda_queues_all_json,
    rucio_endpoints_all_json,
    update_panda_queues_from_github,
    update_rucio_endpoints_from_github,
    panda_hub,
)

# Import PanDA database views from new dedicated module
from .viewdir.panda_database import (
    panda_database_tables_list,
    panda_database_tables_datatable_ajax,
    panda_database_table_list,
    panda_database_table_datatable_ajax,
    panda_database_table_row_detail,
)

# Import iDDS database views from new dedicated module
from .viewdir.idds_database import (
    idds_database_tables_list,
    idds_database_tables_datatable_ajax,
    idds_database_table_list,
    idds_database_table_datatable_ajax,
)
from .fastmon_views import (
    fastmon_files_list,
    fastmon_files_datatable_ajax,
)
from .workflow_views import (
    workflows_home,
    workflow_definitions_list,
    workflow_definitions_datatable_ajax,
    workflow_definitions_filter_counts,
    workflow_executions_list,
    workflow_executions_datatable_ajax,
    workflow_executions_filter_counts,
    workflow_definition_detail,
    workflow_execution_detail,
)

app_name = 'monitor_app'

urlpatterns = [
    path('', home, name='home'),
    path('dashboard/', index, name='index'),
    path('about/', about, name='about'),
    path('create/', system_agent_create, name='system_agent_create'),
    path('<int:pk>/update/', system_agent_update, name='system_agent_update'),
    path('system_agents/<int:pk>/delete/', system_agent_delete, name='system_agent_delete'),
    path('account/', account_view, name='account'),
    path('logs/summary/', log_summary, name='log_summary'),
    path('logs/summary/datatable/', log_summary_datatable_ajax, name='log_summary_datatable_ajax'),
    path('logs/', log_list, name='log_list'),
    path('logs/datatable/', logs_datatable_ajax, name='logs_datatable_ajax'),
    path('logs/filter-counts/', get_log_filter_counts, name='log_filter_counts'),
    path('home/', authenticated_home, name='authenticated_home'),
    path('database/', database_tables_list, name='database_tables_list'),
    path('database/datatable/', database_tables_datatable_ajax, name='database_tables_datatable_ajax'),
    path('database/<str:table_name>/', database_table_list, name='database_table_list'),
    path('database/<str:table_name>/datatable/', database_table_datatable_ajax, name='database_table_datatable_ajax'),
    
    # SWF Data Model URLs
    path('runs/', runs_list, name='runs_list'),
    path('runs/datatable/', runs_datatable_ajax, name='runs_datatable_ajax'),
    path('runs/<int:run_number>/', run_detail, name='run_detail'),
    path('stf-files/', stf_files_list, name='stf_files_list'),
    path('stf-files/datatable/', stf_files_datatable_ajax, name='stf_files_datatable_ajax'),
    path('stf-files/<uuid:file_id>/', stf_file_detail, name='stf_file_detail'),

    # FastMon Files (Time Frames)
    path('fastmon-files/', fastmon_files_list, name='fastmon_files_list'),
    path('fastmon-files/datatable/', fastmon_files_datatable_ajax, name='fastmon_files_datatable_ajax'),

    path('subscribers/', subscribers_list, name='subscribers_list'),
    path('subscribers/datatable/', subscribers_datatable_ajax, name='subscribers_datatable_ajax'),
    path('subscribers/filter-counts/', get_subscribers_filter_counts, name='subscribers_filter_counts'),
    path('subscribers/<int:subscriber_id>/', subscriber_detail, name='subscriber_detail'),

    # Workflow URLs
    path('workflow/list/datatable/', workflow_datatable_ajax, name='workflow_datatable_ajax'),
    path('workflow/<uuid:workflow_id>/', workflow_detail, name='workflow_detail'),
    path('workflow/agents/', workflow_agents_list, name='workflow_agents_list'),
    path('workflow/agents/datatable/', workflow_agents_datatable_ajax, name='workflow_agents_datatable_ajax'),
    path('workflow/agents/<str:instance_name>/', agent_detail, name='agent_detail'),
    path('workflow/messages/', workflow_messages, name='workflow_messages'),
    path('workflow/messages/datatable/', workflow_messages_datatable_ajax, name='workflow_messages_datatable_ajax'),
    path('workflow/messages/filter-counts/', get_workflow_messages_filter_counts, name='workflow_messages_filter_counts'),
    path('workflow/api/realtime-data/', workflow_realtime_data_api, name='workflow_realtime_data_api'),

    # Workflow Management
    path('workflows/', workflows_home, name='workflows_home'),

    # Workflow Definitions and Executions
    path('workflow-definitions/', workflow_definitions_list, name='workflow_definitions_list'),
    path('workflow-definitions/datatable/', workflow_definitions_datatable_ajax, name='workflow_definitions_datatable_ajax'),
    path('workflow-definitions/filter-counts/', workflow_definitions_filter_counts, name='workflow_definitions_filter_counts'),
    path('workflow-definitions/<str:workflow_name>/<str:version>/', workflow_definition_detail, name='workflow_definition_detail'),
    path('workflow-executions/', workflow_executions_list, name='workflow_executions_list'),
    path('workflow-executions/datatable/', workflow_executions_datatable_ajax, name='workflow_executions_datatable_ajax'),
    path('workflow-executions/filter-counts/', workflow_executions_filter_counts, name='workflow_executions_filter_counts'),
    path('workflow-executions/<str:execution_id>/', workflow_execution_detail, name='workflow_execution_detail'),

    # System State
    path('persistent-state/', persistent_state_view, name='persistent_state'),
    
    # PanDA Queues
    path('panda-queues/', panda_queues_list, name='panda_queues_list'),
    path('panda-queues/datatable/', panda_queues_datatable_ajax, name='panda_queues_datatable_ajax'),
    path('panda-queues/json/', panda_queues_all_json, name='panda_queues_all_json'),
    path('panda-queues/update-from-github/', update_panda_queues_from_github, name='update_panda_queues_from_github'),
    path('panda-queues/<str:queue_name>/', panda_queue_detail, name='panda_queue_detail'),
    path('panda-queues/<str:queue_name>/json/', panda_queue_json, name='panda_queue_json'),
    
    # Rucio Endpoints
    path('rucio-endpoints/', rucio_endpoints_list, name='rucio_endpoints_list'),
    path('rucio-endpoints/datatable/', rucio_endpoints_datatable_ajax, name='rucio_endpoints_datatable_ajax'),
    path('rucio-endpoints/json/', rucio_endpoints_all_json, name='rucio_endpoints_all_json'),
    path('rucio-endpoints/update-from-github/', update_rucio_endpoints_from_github, name='update_rucio_endpoints_from_github'),
    path('rucio-endpoints/<str:endpoint_name>/', rucio_endpoint_detail, name='rucio_endpoint_detail'),
    path('rucio-endpoints/<str:endpoint_name>/json/', rucio_endpoint_json, name='rucio_endpoint_json'),
    
    # PanDA Hub
    path('panda/', panda_hub, name='panda_hub'),
    
    # PanDA Database
    path('panda-database/', panda_database_tables_list, name='panda_database_tables_list'),
    path('panda-database/datatable/', panda_database_tables_datatable_ajax, name='panda_database_tables_datatable_ajax'),
    path('panda-database/<str:table_name>/', panda_database_table_list, name='panda_database_table_list'),
    path('panda-database/<str:table_name>/datatable/', panda_database_table_datatable_ajax, name='panda_database_table_datatable_ajax'),
    path('panda-database/<str:table_name>/<str:row_id>/', panda_database_table_row_detail, name='panda_database_table_row_detail'),
    
    # iDDS Database
    path('idds-database/', idds_database_tables_list, name='idds_database_tables_list'),
    path('idds-database/datatable/', idds_database_tables_datatable_ajax, name='idds_database_tables_datatable_ajax'),
    path('idds-database/<str:table_name>/', idds_database_table_list, name='idds_database_table_list'),
    path('idds-database/<str:table_name>/datatable/', idds_database_table_datatable_ajax, name='idds_database_table_datatable_ajax'),
    
    # API
    path('api/', include('monitor_app.api_urls')),
]
