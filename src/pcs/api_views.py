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
from rest_framework.permissions import IsAuthenticatedOrReadOnly, SAFE_METHODS, BasePermission


class IsOwnerOrReadOnly(BasePermission):
    """Read open to anyone; write requires authenticated owner (by created_by username)."""
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        return getattr(obj, 'created_by', None) == request.user.username
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

    @action(detail=False, methods=['post'], url_path='intake')
    def intake(self, request):
        """
        Idempotent intake of an external (e.g. EVGEN CSV manifest) Dataset.

        Idempotency key: (source.kind, source.location). Repeated calls
        with the same key return the same Dataset row.

        Body:
            source_location  (required) — e.g. CSV manifest path
            source_kind      (default 'csv_manifest')
            stage            (default 'evgen')
            scope            (default 'group.EIC.evgen' when creating)
            detector_version (required when creating)
            detector_config  (required when creating)
            physics_tag      (label, required when creating)
            evgen_tag        (label, required when creating)
            simu_tag         (label, required when creating)
            reco_tag         (label, required when creating)
            description      (optional)
        """
        location = request.data.get('source_location')
        kind = request.data.get('source_kind', 'csv_manifest')
        if not location:
            return Response(
                {'detail': 'source_location is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = self.get_queryset().filter(
            metadata__source__location=location,
            metadata__source__kind=kind,
        ).first()
        if existing:
            return Response(self.get_serializer(existing).data,
                            status=status.HTTP_200_OK)

        # Need to create — collect required fields
        required = ['detector_version', 'detector_config',
                    'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag']
        missing = [k for k in required if not request.data.get(k)]
        if missing:
            return Response(
                {'detail': f'No existing Dataset for {kind}:{location}; '
                          f'creation requires: {", ".join(missing)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve tag labels
        tag_models = {
            'physics_tag': PhysicsTag, 'evgen_tag': EvgenTag,
            'simu_tag': SimuTag,       'reco_tag': RecoTag,
        }
        tags = {}
        for field, model in tag_models.items():
            label = request.data[field]
            tag = model.objects.filter(tag_label=label).first()
            if not tag:
                return Response(
                    {'detail': f'{field} not found: {label}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if tag.status != 'locked':
                return Response(
                    {'detail': f'{field} {label} must be locked before use'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            tags[field] = tag

        ds = Dataset(
            scope=request.data.get('scope', 'group.EIC.evgen'),
            detector_version=request.data['detector_version'],
            detector_config=request.data['detector_config'],
            description=request.data.get('description', ''),
            metadata={
                'stage': request.data.get('stage', 'evgen'),
                'source': {'kind': kind, 'location': location},
            },
            created_by=request.user.username,
            **tags,
        )
        try:
            ds.save()
        except Exception as e:
            return Response({'detail': str(e)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(ds).data,
                        status=status.HTTP_201_CREATED)

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
    """Production configuration templates. Owner-only edit; anyone can create."""
    queryset = ProdConfig.objects.all()
    serializer_class = ProdConfigSerializer
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsOwnerOrReadOnly]

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
    permission_classes = [IsOwnerOrReadOnly]

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

    # Allowed lifecycle transitions for set-status. Submission and
    # post-submission state changes are recorded via record-submission and
    # automation, not direct human transitions.
    PRODTASK_TRANSITIONS = {
        'draft':     {'ready'},
        'ready':     {'draft', 'submitted'},
        'submitted': {'completed', 'failed'},
        'completed': set(),
        'failed':    set(),
    }

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
        allowed = self.PRODTASK_TRANSITIONS.get(task.status, set())
        if new_status != task.status and new_status not in allowed:
            return Response(
                {'detail': f'Cannot transition from {task.status!r} to '
                          f'{new_status!r}. Allowed from {task.status!r}: '
                          f'{sorted(allowed) or "(terminal)"}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        task.status = new_status
        task.save(update_fields=['status', 'updated_at'])
        return Response(self.get_serializer(task).data)

    @action(detail=True, methods=['post'], url_path='link-input')
    def link_input(self, request, pk=None):
        """Link input Dataset(s) to a ProdTask via overrides JSON.

        Body:
            did   — single DID, OR
            dids  — list of DIDs (sets overrides.input_dataset_dids)

        Provide one or the other, not both. The linked Dataset(s) must
        already exist; this endpoint never creates Datasets.
        """
        task = self.get_object()
        did = request.data.get('did')
        dids = request.data.get('dids')
        if did and dids:
            return Response({'detail': 'Provide one of did or dids, not both'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not did and not dids:
            return Response({'detail': 'did or dids is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        targets = [did] if did else list(dids)
        found = set(Dataset.objects.filter(did__in=targets)
                    .values_list('did', flat=True))
        missing = [d for d in targets if d not in found]
        if missing:
            return Response({'detail': f'Dataset(s) not found: {missing}'},
                            status=status.HTTP_400_BAD_REQUEST)
        ov = dict(task.overrides or {})
        if did:
            ov['input_dataset_did'] = did
            ov.pop('input_dataset_dids', None)
        else:
            ov['input_dataset_dids'] = list(dids)
            ov.pop('input_dataset_did', None)
        task.overrides = ov
        task.save(update_fields=['overrides', 'updated_at'])
        return Response(self.get_serializer(task).data)

    PUBLIC_CATALOG_KEYS = (
        'public_catalog_repo', 'public_catalog_issue', 'public_catalog_pr',
        'public_catalog_row_index', 'public_catalog_csv_path',
        'public_catalog_row_key', 'public_catalog_page_url',
        'public_catalog_commit_sha',
    )

    @action(detail=False, methods=['post'], url_path='intake')
    def intake(self, request):
        """Idempotent intake of a draft ProdTask from a request payload.

        Idempotency key (required, one of):
            public_catalog_issue (preferred), or
            (public_catalog_csv_path, public_catalog_row_key)

        On match, the existing ProdTask is updated (catalogue mapping
        merged into overrides; description optionally refreshed;
        input_dataset_did optionally set/updated). On no match, a new
        draft is created and requires:

            name           — ProdTask.name (unique)
            dataset        — output Dataset, by DID or dataset_name
            prod_config    — ProdConfig, by name
            description    — optional
            input_dataset_did — optional, set on overrides
            public_catalog_* — any subset, persisted to overrides

        New tasks are created with status='draft' and created_by from
        the authenticated user.
        """
        data = request.data
        key_issue = data.get('public_catalog_issue')
        key_csv_path = data.get('public_catalog_csv_path')
        key_row = data.get('public_catalog_row_key')
        if not key_issue and not (key_csv_path and key_row):
            return Response(
                {'detail': 'Idempotency key required: public_catalog_issue '
                          'or (public_catalog_csv_path, public_catalog_row_key)'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = self.get_queryset()
        if key_issue:
            existing = qs.filter(overrides__public_catalog_issue=key_issue).first()
        else:
            existing = qs.filter(
                overrides__public_catalog_csv_path=key_csv_path,
                overrides__public_catalog_row_key=key_row,
            ).first()

        new_catalog = {k: data[k] for k in self.PUBLIC_CATALOG_KEYS if k in data}
        new_input_did = data.get('input_dataset_did')

        if existing:
            self.check_object_permissions(request, existing)
            ov = dict(existing.overrides or {})
            ov.update(new_catalog)
            if new_input_did:
                ov['input_dataset_did'] = new_input_did
            existing.overrides = ov
            update_fields = ['overrides', 'updated_at']
            if data.get('description') is not None:
                existing.description = data['description']
                update_fields.append('description')
            existing.save(update_fields=update_fields)
            return Response(self.get_serializer(existing).data,
                            status=status.HTTP_200_OK)

        # Create new draft
        name = data.get('name')
        dataset_handle = data.get('dataset')
        config_handle = data.get('prod_config')
        missing = [k for k, v in [('name', name),
                                  ('dataset', dataset_handle),
                                  ('prod_config', config_handle)]
                   if not v]
        if missing:
            return Response(
                {'detail': f'Creating a new task requires: {", ".join(missing)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        output_ds = (Dataset.objects.filter(did=dataset_handle).first()
                     or Dataset.objects.filter(dataset_name=dataset_handle).first())
        if not output_ds:
            return Response(
                {'detail': f'Output dataset not found: {dataset_handle}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        config = ProdConfig.objects.filter(name=config_handle).first()
        if not config:
            return Response(
                {'detail': f'ProdConfig not found: {config_handle}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ov = dict(new_catalog)
        if new_input_did:
            ov['input_dataset_did'] = new_input_did
        task = ProdTask.objects.create(
            name=name,
            description=data.get('description', ''),
            status='draft',
            dataset=output_ds,
            prod_config=config,
            overrides=ov or None,
            created_by=request.user.username,
        )
        return Response(self.get_serializer(task).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='record-submission')
    def record_submission(self, request):
        """
        Record outcome of a JEDI submission. Sets panda_task_id and status.
        Called by `pcs-task-cmd --submit` after Client.insertTaskParams()
        returns the JEDI task ID.

        Gates:
            - Task must be in status='ready'. No submit from draft;
              no re-submit from submitted/completed/failed.
            - Task must not already record a panda_task_id; refuses to
              overwrite (returns 409). Treats panda_task_id as one-shot.

        Query params:
            name — ProdTask.name (required)

        Body (JSON):
            jedi_task_id — int, required
            status       — str, optional (default 'submitted'); must be a
                           valid PRODTASK_STATUS_CHOICES value
        """
        name = request.query_params.get('name') or request.data.get('name')
        if not name:
            return Response({'detail': 'Missing ?name='},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            task = self.get_queryset().get(name=name)
        except ProdTask.DoesNotExist:
            return Response({'detail': f"No task named '{name}'"},
                            status=status.HTTP_404_NOT_FOUND)
        self.check_object_permissions(request, task)

        # Gate 1: must be in 'ready' state.
        if task.status != 'ready':
            return Response(
                {'detail': f'Task must be in status=ready before submission '
                          f'(current: {task.status!r}). Mark it ready via '
                          f'set-status first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Gate 2: refuse if a submission was already recorded.
        if task.panda_task_id is not None:
            return Response(
                {'detail': f'Task already records '
                          f'panda_task_id={task.panda_task_id}. '
                          f'Refusing to overwrite.'},
                status=status.HTTP_409_CONFLICT,
            )

        jedi_task_id = request.data.get('jedi_task_id')
        try:
            task.panda_task_id = int(jedi_task_id)
        except (TypeError, ValueError):
            return Response({'detail': 'jedi_task_id must be an integer'},
                            status=status.HTTP_400_BAD_REQUEST)

        new_status = request.data.get('status', 'submitted')
        valid = [c[0] for c in task._meta.get_field('status').choices]
        if new_status not in valid:
            return Response(
                {'detail': f'Invalid status. Choose from: {", ".join(valid)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        task.status = new_status
        task.save(update_fields=['panda_task_id', 'status', 'updated_at'])
        return Response(self.get_serializer(task).data)
