"""
PCS REST API ViewSets.

Endpoints under /pcs/api/. All endpoints require authentication.
Tag immutability enforced: PATCH returns 400 on locked tags. Lock is one-way via POST /lock/.
Tag delete via POST /delete/ — creator-only, draft-only (locked tags protected by PROTECT FK).
Tag numbers auto-assigned on POST: physics from category range, e/s/r from PersistentState.
Dataset creation requires all four tags to be locked. created_by set from authenticated user.
"""
from rest_framework import viewsets, status
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from monitor_app.middleware import TunnelAuthentication
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from rest_framework.response import Response
from django.db.models import Count

from .models import (
    PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag,
    Dataset, ProdConfig, ProdTask,
)
from .serializers import (
    PhysicsCategorySerializer, PhysicsTagSerializer,
    EvgenTagSerializer, SimuTagSerializer, RecoTagSerializer,
    DatasetSerializer, ProdConfigSerializer, ProdTaskSerializer,
)
from .schemas import validate_parameters, get_tag_model


class PhysicsCategoryViewSet(viewsets.ModelViewSet):
    """CRUD for physics categories. Categories are mutable (no lock lifecycle)."""
    queryset = PhysicsCategory.objects.annotate(tag_count=Count('tags'))
    serializer_class = PhysicsCategorySerializer
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user.username)


class _TagViewSetMixin:
    """Shared behavior for all tag ViewSets: draft/locked lifecycle, PATCH guard, lock/delete actions."""
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]
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

    @action(detail=True, methods=['post'], url_path='delete')
    def soft_delete(self, request, **kwargs):
        instance = self.get_object()
        if instance.status == 'locked':
            return Response(
                {'detail': f'Tag {instance.tag_label} is locked and cannot be deleted.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if instance.created_by != request.user.username:
            return Response(
                {'detail': f'Only the creator ({instance.created_by}) can delete this tag.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        label = instance.tag_label
        instance.delete()
        return Response({'detail': f'Tag {label} deleted.'})


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
        serializer.save(tag_number=tag_number, tag_label=f"p{tag_number}",
                        created_by=request.user.username)
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
        serializer.save(tag_number=tag_number, tag_label=f"{prefix}{tag_number}",
                        created_by=request.user.username)
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
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]
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
        instance = serializer.save(created_by=request.user.username)
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
            created_by=request.user.username,
        )
        return Response(self.get_serializer(new_block).data, status=status.HTTP_201_CREATED)


class ProdConfigViewSet(viewsets.ModelViewSet):
    """Production configuration templates. Always mutable — full CRUD."""
    queryset = ProdConfig.objects.all()
    serializer_class = ProdConfigSerializer
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user.username)


class ProdTaskViewSet(viewsets.ModelViewSet):
    """Production tasks: Dataset + ProdConfig compositions with command generation."""
    queryset = ProdTask.objects.select_related(
        'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
        'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
    )
    serializer_class = ProdTaskSerializer
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user.username)

    @action(detail=True, methods=['post'], url_path='generate-commands')
    def generate_commands(self, request, pk=None):
        task = self.get_object()
        task.generate_commands()
        task.save(update_fields=['condor_command', 'panda_command', 'updated_at'])
        return Response({
            'condor_command': task.condor_command,
            'panda_command': task.panda_command,
        })

    @action(detail=False, methods=['get'], url_path='command')
    def command(self, request):
        """
        Regenerate and return a task's submission artifact in one of three
        formats. Lookup by task name. No DB writes.

        Query params:
            name — ProdTask.name (required)
            fmt  — condor | panda | jedi | dump (required). Named 'fmt'
                   (not 'format') because DRF reserves 'format' for
                   content negotiation.

        Returns:
            text/plain for condor/panda, application/json for jedi/dump.
        """
        from django.http import HttpResponse, JsonResponse
        from .commands import (
            build_condor_command, build_panda_command,
            build_task_params, build_task_dump,
        )

        name = request.query_params.get('name')
        fmt = request.query_params.get('fmt', '').lower()
        if not name:
            return Response({'detail': 'Missing ?name='}, status=status.HTTP_400_BAD_REQUEST)
        if fmt not in ('condor', 'panda', 'jedi', 'dump'):
            return Response(
                {'detail': "fmt must be one of: condor, panda, jedi, dump"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            task = self.get_queryset().get(name=name)
        except ProdTask.DoesNotExist:
            return Response({'detail': f"No task named '{name}'"}, status=status.HTTP_404_NOT_FOUND)

        if fmt == 'condor':
            return HttpResponse(build_condor_command(task), content_type='text/plain')
        if fmt == 'panda':
            return HttpResponse(build_panda_command(task), content_type='text/plain')
        if fmt == 'jedi':
            return JsonResponse(build_task_params(task), json_dumps_params={'indent': 2})
        return JsonResponse(build_task_dump(task), json_dumps_params={'indent': 2})

    @action(detail=True, methods=['post'], url_path='set-status')
    def set_status(self, request, pk=None):
        task = self.get_object()
        new_status = request.data.get('status')
        valid = [c[0] for c in task._meta.get_field('status').choices]
        if new_status not in valid:
            return Response(
                {'detail': f'Invalid status. Choose from: {", ".join(valid)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        task.status = new_status
        task.save(update_fields=['status', 'updated_at'])
        return Response(self.get_serializer(task).data)
