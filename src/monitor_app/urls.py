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
    log_list,
    database_tables_list,
    database_table_list,
    runs_list,
    run_detail,
    stf_files_list,
    stf_file_detail,
    subscribers_list,
    subscriber_detail,
    message_dispatches_list,
    message_dispatch_detail,
    # Workflow views
    workflow_dashboard,
    workflow_list,
    workflow_detail,
    workflow_agents_list,
    agent_detail,
    workflow_messages,
    workflow_performance,
    workflow_realtime_dashboard,
    workflow_realtime_data_api,
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
    path('logs/', log_list, name='log_list'),
    path('home/', authenticated_home, name='authenticated_home'),
    path('database/', database_tables_list, name='database_tables_list'),
    path('database/<str:table_name>/', database_table_list, name='database_table_list'),
    
    # SWF Data Model URLs
    path('runs/', runs_list, name='runs_list'),
    path('runs/<uuid:run_id>/', run_detail, name='run_detail'),
    path('stf-files/', stf_files_list, name='stf_files_list'),
    path('stf-files/<uuid:file_id>/', stf_file_detail, name='stf_file_detail'),
    path('subscribers/', subscribers_list, name='subscribers_list'),
    path('subscribers/<uuid:subscriber_id>/', subscriber_detail, name='subscriber_detail'),
    path('message-dispatches/', message_dispatches_list, name='message_dispatches_list'),
    path('message-dispatches/<uuid:dispatch_id>/', message_dispatch_detail, name='message_dispatch_detail'),
    
    # Workflow URLs
    path('workflow/', workflow_dashboard, name='workflow_dashboard'),
    path('workflow/list/', workflow_list, name='workflow_list'),
    path('workflow/<uuid:workflow_id>/', workflow_detail, name='workflow_detail'),
    path('workflow/agents/', workflow_agents_list, name='workflow_agents_list'),
    path('workflow/agents/<str:instance_name>/', agent_detail, name='agent_detail'),
    path('workflow/messages/', workflow_messages, name='workflow_messages'),
    path('workflow/performance/', workflow_performance, name='workflow_performance'),
    path('workflow/realtime/', workflow_realtime_dashboard, name='workflow_realtime_dashboard'),
    path('workflow/api/realtime-data/', workflow_realtime_data_api, name='workflow_realtime_data_api'),
    
    # API v1
    path('api/v1/', include('monitor_app.api_urls')),
]
