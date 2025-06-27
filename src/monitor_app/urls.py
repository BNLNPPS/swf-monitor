from django.urls import path
from .views import (
    home,
    about,
    index,
    system_agent_create,
    system_agent_update,
    system_agent_delete,
    get_system_agents_data,
    account_view,
    log_summary,
    log_list,
    authenticated_home,
    database_overview,
    database_table_list,
)

app_name = 'monitor_app'

urlpatterns = [
    path('', home, name='home'),
    path('dashboard/', index, name='index'),
    path('about/', about, name='about'),
    path('create/', system_agent_create, name='system_agent_create'),
    path('<int:pk>/update/', system_agent_update, name='system_agent_update'),
    path('system_agents/<int:pk>/delete/', system_agent_delete, name='system_agent_delete'),
    path('api/system_agents/', get_system_agents_data, name='system_agents_data'),
    path('account/', account_view, name='account'),
    path('logs/summary/', log_summary, name='log_summary'),
    path('logs/', log_list, name='log_list'),
    path('home/', authenticated_home, name='authenticated_home'),
    path('database/', database_overview, name='database_overview'),
    path('database/<str:table_name>/', database_table_list, name='database_table_list'),
]
