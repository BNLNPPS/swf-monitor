from django.urls import path, include
from . import alarm_views
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
    live_policy,
    log_list,
    log_detail,
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
    namespace_detail,
    message_detail,
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
    mcp_health,
    panda_hub, prod_hub, testbed_hub,
    ai_content_list,
    ai_content_detail,
    ai_content_body,
    ai_content_legacy_detail,
    ai_content_legacy_body,
    ai_content_set_quality,
)

# Import PanDA database views from new dedicated module
from .viewdir.panda_database import (
    panda_database_tables_list,
    panda_database_tables_datatable_ajax,
    panda_database_table_list,
    panda_database_table_datatable_ajax,
    panda_database_table_row_detail,
)

# Import PanDA production monitor views
from .viewdir.pandamon import (
    panda_activity,
    compute_usage,
    compute_usage_data,
    panda_jobs_list,
    panda_jobs_datatable_ajax,
    panda_jobs_filter_counts,
    panda_tasks_list,
    panda_tasks_datatable_ajax,
    panda_tasks_filter_counts,
    panda_job_detail,
    epicprod_job_refresh,
    panda_task_detail,
    panda_errors_list,
    panda_errors_datatable_ajax,
    panda_diagnostics_list,
    panda_diagnostics_datatable_ajax,
    panda_view_text,
    panda_payload_log,
    epic_queues_list,
    epic_queue_detail,
)

from .viewdir.system_status import (
    sysconfig_save,
    system_status_json,
    system_status_page,
    system_status_refresh,
)
from .viewdir.snapper import (
    snapper_report,
    snapper_root,
    snapper_system,
)

# Import iDDS database views from new dedicated module
from .viewdir.analysis import analysis_view
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
from .tf_slices_views import (
    tf_slices_list,
    tf_slices_datatable_ajax,
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
    namespaces_list,
    namespaces_datatable_ajax,
)

app_name = 'monitor_app'

urlpatterns = [
    path('', home, name='home'),
    path('api/mcp-health/', mcp_health, name='mcp_health'),
    path('dashboard/', index, name='index'),
    path('about/', about, name='about'),
    path('create/', system_agent_create, name='system_agent_create'),
    path('<int:pk>/update/', system_agent_update, name='system_agent_update'),
    path('system_agents/<int:pk>/delete/', system_agent_delete, name='system_agent_delete'),
    path('account/', account_view, name='account'),
    path('logs/summary/', log_summary, name='log_summary'),
    path('logs/summary/datatable/', log_summary_datatable_ajax, name='log_summary_datatable_ajax'),
    path('logs/', log_list, name='log_list'),
    path('logs/live-policy/', live_policy, name='live_policy'),
    path('logs/<int:log_id>/', log_detail, name='log_detail'),
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

    # TF Slices (Fast Processing)
    path('tf-slices/', tf_slices_list, name='tf_slices_list'),
    path('tf-slices/datatable/', tf_slices_datatable_ajax, name='tf_slices_datatable_ajax'),

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
    path('workflow/namespaces/<str:namespace>/', namespace_detail, name='namespace_detail'),
    path('workflow/messages/', workflow_messages, name='workflow_messages'),
    path('workflow/messages/<uuid:message_id>/', message_detail, name='message_detail'),
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

    # Namespaces
    path('namespaces/', namespaces_list, name='namespaces_list'),
    path('namespaces/datatable/', namespaces_datatable_ajax, name='namespaces_datatable_ajax'),

    # System State
    path('persistent-state/', persistent_state_view, name='persistent_state'),
    path('analysis/', analysis_view, name='analysis'),
    path('panda/system/', system_status_page, name='system_status'),
    path('panda/system/status.json', system_status_json, name='system_status_json'),
    path('panda/system/refresh/', system_status_refresh, name='system_status_refresh'),
    path('system/', system_status_page, name='system_status_root'),
    path('system/status.json', system_status_json, name='system_status_json_root'),
    path('system/refresh/', system_status_refresh, name='system_status_refresh_root'),
    path('system/sysconfig/', sysconfig_save, name='sysconfig_save'),

    # Snapper coherent operational state history
    path('snapper/', snapper_root, name='snapper_root'),
    path('snapper/<str:scope>/report/', snapper_report,
         name='snapper_report'),
    path('snapper/<str:scope>/report/<uuid:snap_id>/', snapper_report,
         name='snapper_report_snap'),
    path('snapper/<str:scope>/system/', snapper_system,
         name='snapper_system'),
    
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
    path('prod/', prod_hub, name='prod_hub'),
    path('compute-usage/', compute_usage, name='compute_usage'),
    path('compute-usage/data/', compute_usage_data,
         name='compute_usage_data'),
    path('ai/assessments/', ai_content_list, name='ai_content_list'),
    path('ai/assessments/legacy/<int:content_id>/', ai_content_legacy_detail,
         name='ai_content_legacy_detail'),
    path('ai/assessments/legacy/<int:content_id>/body/',
         ai_content_legacy_body, name='ai_content_legacy_body'),
    path('ai/assessments/<uuid:page_group_id>/', ai_content_detail,
         name='ai_content_detail'),
    path('ai/assessments/<uuid:page_group_id>/body/', ai_content_body,
         name='ai_content_body'),
    path('ai/assessments/<int:content_id>/quality/', ai_content_set_quality, name='ai_content_set_quality'),
    path('testbed/', testbed_hub, name='testbed_hub'),

    # PanDA Production Monitor
    path('panda/activity/', panda_activity, name='panda_activity'),
    path('panda/jobs/', panda_jobs_list, name='panda_jobs_list'),
    path('panda/jobs/datatable/', panda_jobs_datatable_ajax, name='panda_jobs_datatable_ajax'),
    path('panda/jobs/filter-counts/', panda_jobs_filter_counts, name='panda_jobs_filter_counts'),
    path('panda/jobs/<int:pandaid>/', panda_job_detail, name='panda_job_detail'),
    path('panda/jobs/<int:pandaid>/payload-log/', panda_payload_log, name='panda_payload_log'),
    path('epicprod/jobs/<int:pandaid>/', panda_job_detail, name='epicprod_job_detail'),
    path('epicprod/jobs/<int:pandaid>/refresh/', epicprod_job_refresh, name='epicprod_job_refresh'),
    path('panda/view-text/', panda_view_text, name='panda_view_text'),
    path('panda/tasks/', panda_tasks_list, name='panda_tasks_list'),
    path('panda/tasks/datatable/', panda_tasks_datatable_ajax, name='panda_tasks_datatable_ajax'),
    path('panda/tasks/filter-counts/', panda_tasks_filter_counts, name='panda_tasks_filter_counts'),
    path('panda/tasks/<int:jeditaskid>/', panda_task_detail, name='panda_task_detail'),
    path('panda/errors/', panda_errors_list, name='panda_errors_list'),
    path('panda/errors/datatable/', panda_errors_datatable_ajax, name='panda_errors_datatable_ajax'),
    path('panda/diagnostics/', panda_diagnostics_list, name='panda_diagnostics_list'),
    path('panda/diagnostics/datatable/', panda_diagnostics_datatable_ajax, name='panda_diagnostics_datatable_ajax'),
    path('panda/epic-queues/', epic_queues_list, name='epic_queues_list'),
    path('panda/epic-queues/<str:queue_name>/', epic_queue_detail, name='epic_queue_detail'),

    # Alarms
    path('alarms/', alarm_views.alarms_dashboard, name='alarms_dashboard'),
    path('alarms/events/<str:event_uuid>/', alarm_views.alarm_event_detail,
         name='alarm_event_detail'),
    path('alarms/runs/<str:run_uuid>/<str:entry_id>/',
         alarm_views.alarm_run_report, name='alarm_run_report'),
    path('alarms/<str:entry_id>/task/',
         alarm_views.alarm_task_history, name='alarm_task_history'),
    path('alarms/teams/new/', alarm_views.team_new, name='team_new'),
    path('alarms/teams/create/', alarm_views.team_create, name='team_create'),
    path('alarms/teams/<str:at_name>/edit/', alarm_views.team_edit, name='team_edit'),
    path('alarms/teams/<str:at_name>/save/', alarm_views.team_save, name='team_save'),
    path('alarms/teams/<str:at_name>/versions/<int:version_num>/',
         alarm_views.team_version, name='team_version'),
    path('alarms/<str:entry_id>/edit/', alarm_views.alarm_config_edit,
         name='alarm_config_edit'),
    path('alarms/<str:entry_id>/save/', alarm_views.alarm_config_save,
         name='alarm_config_save'),
    path('alarms/<str:entry_id>/versions/<int:version_num>/',
         alarm_views.alarm_config_version, name='alarm_config_version'),
    path('alarms/<str:entry_id>/test/', alarm_views.alarm_test, name='alarm_test'),

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
