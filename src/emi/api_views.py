"""
EMI REST API ViewSets.

Endpoints under /emi/api/. No DELETE on any endpoint — tags and datasets are permanent.
Tag immutability enforced: PATCH returns 400 on locked tags. Lock is one-way via POST /lock/.
Tag numbers auto-assigned on POST: physics from category range, e/s/r from PersistentState.
Dataset creation requires all four tags to be locked.
"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.db.models import Count

from .models import (
    PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, Dataset, ProdConfig,
)
from .serializers import (
    PhysicsCategorySerializer, PhysicsTagSerializer,
    EvgenTagSerializer, SimuTagSerializer, RecoTagSerializer,
    DatasetSerializer, ProdConfigSerializer,
)
from .schemas import validate_parameters, get_tag_model


class PhysicsCategoryViewSet(viewsets.ModelViewSet):
    """CRUD for physics categories. Categories are mutable (no lock lifecycle)."""
    queryset = PhysicsCategory.objects.annotate(tag_count=Count('tags'))
    serializer_class = PhysicsCategorySerializer
    permission_classes = [AllowAny]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']


class _TagViewSetMixin:
    """Shared behavior for all tag ViewSets: draft/locked lifecycle, PATCH guard, lock action."""
    permission_classes = [AllowAny]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    lookup_field = 'tag_number'

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status == 'locked':
            return Response(
                {'detail': f'Tag {instance.tag_label} is locked and cannot be modified.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if 'status' in request.data:
            return Response(
                {'detail': 'Use the /lock/ endpoint to change status.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().partial_update(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def lock(self, request, **kwargs):
        instance = self.get_object()
        if instance.status == 'locked':
            return Response(
                {'detail': f'Tag {instance.tag_label} is already locked.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        instance.status = 'locked'
        instance.save(update_fields=['status', 'updated_at'])
        return Response(self.get_serializer(instance).data)


class PhysicsTagViewSet(_TagViewSetMixin, viewsets.ModelViewSet):
    queryset = PhysicsTag.objects.select_related('category')
    serializer_class = PhysicsTagSerializer

    def create(self, request, *args, **kwargs):
        category_digit = request.data.get('category')
        if not category_digit:
            return Response(
                {'category': ['This field is required.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            category = PhysicsCategory.objects.get(digit=category_digit)
        except PhysicsCategory.DoesNotExist:
            return Response(
                {'category': [f'Category {category_digit} does not exist.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        tag_number = PhysicsTag.allocate_next(category)
        data = request.data.copy()
        data['tag_number'] = tag_number
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save(tag_number=tag_number, tag_label=f"p{tag_number}")
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class _SimpleTagViewSet(_TagViewSetMixin, viewsets.ModelViewSet):
    tag_type = None

    def create(self, request, *args, **kwargs):
        model = get_tag_model(self.tag_type)
        tag_number = model.allocate_next()
        prefix = self.tag_type
        data = request.data.copy()
        data['tag_number'] = tag_number
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save(tag_number=tag_number, tag_label=f"{prefix}{tag_number}")
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class EvgenTagViewSet(_SimpleTagViewSet):
    queryset = EvgenTag.objects.all()
    serializer_class = EvgenTagSerializer
    tag_type = 'e'


class SimuTagViewSet(_SimpleTagViewSet):
    queryset = SimuTag.objects.all()
    serializer_class = SimuTagSerializer
    tag_type = 's'


class RecoTagViewSet(_SimpleTagViewSet):
    queryset = RecoTag.objects.all()
    serializer_class = RecoTagSerializer
    tag_type = 'r'


class DatasetViewSet(viewsets.ModelViewSet):
    """Dataset CRUD. POST validates all tags are locked and creates block 1. No DELETE."""
    queryset = Dataset.objects.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag'
    )
    serializer_class = DatasetSerializer
    permission_classes = [AllowAny]
    http_method_names = ['get', 'post', 'head', 'options']

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Validate all tags are locked
        for field in ['physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag']:
            tag = serializer.validated_data[field]
            if tag.status != 'locked':
                return Response(
                    {field: [f'Tag {tag.tag_label} must be locked before use in a dataset.']},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        instance = serializer.save()
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='add-block')
    def add_block(self, request, pk=None):
        dataset = self.get_object()
        new_block_num = dataset.blocks + 1
        # Update blocks count on all rows with this dataset_name
        Dataset.objects.filter(dataset_name=dataset.dataset_name).update(blocks=new_block_num)
        # Create the new block
        new_block = Dataset.objects.create(
            dataset_name=dataset.dataset_name,
            scope=dataset.scope,
            detector_version=dataset.detector_version,
            detector_config=dataset.detector_config,
            physics_tag=dataset.physics_tag,
            evgen_tag=dataset.evgen_tag,
            simu_tag=dataset.simu_tag,
            reco_tag=dataset.reco_tag,
            block_num=new_block_num,
            blocks=new_block_num,
            did=f"{dataset.scope}:{dataset.dataset_name}.b{new_block_num}",
            description=dataset.description,
            metadata=dataset.metadata,
            created_by=request.data.get('created_by', dataset.created_by),
        )
        return Response(self.get_serializer(new_block).data, status=status.HTTP_201_CREATED)


class ProdConfigViewSet(viewsets.ModelViewSet):
    """Production configuration templates. Always mutable — full CRUD."""
    queryset = ProdConfig.objects.all()
    serializer_class = ProdConfigSerializer
    permission_classes = [AllowAny]
