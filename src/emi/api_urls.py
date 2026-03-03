from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .api_views import (
    PhysicsCategoryViewSet, PhysicsTagViewSet,
    EvgenTagViewSet, SimuTagViewSet, RecoTagViewSet,
    DatasetViewSet, ProdConfigViewSet,
)

router = DefaultRouter()
router.register(r'physics-categories', PhysicsCategoryViewSet, basename='physics-category')
router.register(r'physics-tags', PhysicsTagViewSet, basename='physics-tag')
router.register(r'evgen-tags', EvgenTagViewSet, basename='evgen-tag')
router.register(r'simu-tags', SimuTagViewSet, basename='simu-tag')
router.register(r'reco-tags', RecoTagViewSet, basename='reco-tag')
router.register(r'datasets', DatasetViewSet, basename='dataset')
router.register(r'prod-configs', ProdConfigViewSet, basename='prod-config')

urlpatterns = [
    path('', include(router.urls)),
]
