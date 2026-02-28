from django.urls import path, include
from . import views

app_name = 'emi'

urlpatterns = [
    # Hub
    path('', views.emi_hub, name='emi_hub'),

    # Physics Categories
    path('categories/', views.physics_categories_list, name='physics_categories_list'),
    path('categories/create/', views.physics_category_create, name='physics_category_create'),

    # Tags (parameterized by type)
    path('tags/<str:tag_type>/', views.tags_list, name='tags_list'),
    path('tags/<str:tag_type>/datatable/', views.tags_datatable_ajax, name='tags_datatable_ajax'),
    path('tags/p/create/', views.tag_compose, kwargs={'tag_type': 'p'}, name='tag_compose'),
    path('tags/<str:tag_type>/create/', views.tag_create, name='tag_create'),
    path('tags/<str:tag_type>/<int:tag_number>/', views.tag_detail, name='tag_detail'),
    path('tags/<str:tag_type>/<int:tag_number>/edit/', views.tag_edit, name='tag_edit'),
    path('tags/<str:tag_type>/<int:tag_number>/lock/', views.tag_lock, name='tag_lock'),

    # Datasets
    path('datasets/', views.datasets_list, name='datasets_list'),
    path('datasets/datatable/', views.datasets_datatable_ajax, name='datasets_datatable_ajax'),
    path('datasets/create/', views.dataset_create, name='dataset_create'),
    path('datasets/<int:pk>/', views.dataset_detail, name='dataset_detail'),
    path('datasets/<int:pk>/add-block/', views.dataset_add_block, name='dataset_add_block'),

    # Production Configs
    path('configs/', views.prod_configs_list, name='prod_configs_list'),
    path('configs/datatable/', views.prod_configs_datatable_ajax, name='prod_configs_datatable_ajax'),
    path('configs/create/', views.prod_config_create, name='prod_config_create'),
    path('configs/<int:pk>/', views.prod_config_detail, name='prod_config_detail'),
    path('configs/<int:pk>/edit/', views.prod_config_edit, name='prod_config_edit'),

    # REST API
    path('api/', include('emi.api_urls')),
]
