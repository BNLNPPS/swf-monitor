from django.urls import path, include
from . import views
from rest_framework.routers import DefaultRouter

app_name = 'monitor_app'

router = DefaultRouter()
router.register(r'monitoreditems', views.MonitoredItemViewSet)

urlpatterns = [
    path('', views.index, name='index'),
    path('create/', views.monitored_item_create, name='monitored_item_create'),
    path('<int:pk>/update/', views.monitored_item_update, name='monitored_item_update'),
    path('<int:pk>/delete/', views.monitored_item_delete, name='monitored_item_delete'),
    path('api/', include(router.urls)),
]
