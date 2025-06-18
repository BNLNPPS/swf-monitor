from django.urls import path
from . import views

app_name = 'monitor_app'

urlpatterns = [
    path('', views.index, name='index'),
    path('create/', views.monitored_item_create, name='monitored_item_create'),
    path('<int:pk>/update/', views.monitored_item_update, name='monitored_item_update'),
    path('<int:pk>/delete/', views.monitored_item_delete, name='monitored_item_delete'),
]
