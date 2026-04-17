"""
PCS web UI views and DataTable AJAX endpoints.

Views are generic across tag types (p/e/s/r) where possible, parameterized by tag_type.
Tag list views use server-side DataTables via monitor_app._datatable_base.html.
Read operations are public; create/edit/lock require login.
"""
import json
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.http import JsonResponse
from django.contrib import messages
from django.db.models import Count

from monitor_app.utils import DataTablesProcessor, get_filter_params, format_datetime

from .models import (
    PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag,
    Dataset, ProdConfig, ProdTask,
)
from .schemas import TAG_SCHEMAS, get_tag_model, get_param_defs, save_param_defs
from .forms import PhysicsTagForm, SimpleTagForm, DatasetForm, PhysicsCategoryForm, ProdConfigForm


def pcs_hub_counts():
    """PCS entity counts — shared by PCS hub and production hub."""
    return {
        'categories_count': PhysicsCategory.objects.count(),
        'physics_tags_count': PhysicsTag.objects.count(),
        'evgen_tags_count': EvgenTag.objects.count(),
        'simu_tags_count': SimuTag.objects.count(),
        'reco_tags_count': RecoTag.objects.count(),
        'datasets_count': Dataset.objects.values('dataset_name').distinct().count(),
        'prod_configs_count': ProdConfig.objects.count(),
        'prod_tasks_count': ProdTask.objects.count(),
    }


def pcs_hub(request):
    return render(request, 'pcs/pcs_hub.html', pcs_hub_counts())


# ── Physics Categories ────────────────────────────────────────────

def physics_categories_list(request):
    categories = PhysicsCategory.objects.annotate(tag_count=Count('tags'))
    return render(request, 'pcs/physics_categories_list.html', {'categories': categories})


@login_required
def physics_category_create(request):
    if request.method == 'POST':
        form = PhysicsCategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f"Category {form.instance.digit}: {form.instance.name} created.")
            return redirect('pcs:physics_categories_list')
    else:
        form = PhysicsCategoryForm()
    return render(request, 'pcs/physics_category_create.html', {'form': form})


# ── Tag list/detail/create (generic across p/e/s/r) ──────────────

TAG_MODELS = {
    'p': PhysicsTag,
    'e': EvgenTag,
    's': SimuTag,
    'r': RecoTag,
}


def tags_list(request, tag_type):
    schema = TAG_SCHEMAS[tag_type]
    model = TAG_MODELS[tag_type]

    status_filter = request.GET.get('status', '')
    category_filter = request.GET.get('category', '')

    columns = [
        {'name': 'tag_label', 'title': 'Tag', 'orderable': True},
        {'name': 'description', 'title': 'Description', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
        {'name': 'actions', 'title': '', 'orderable': False},
    ]
    if tag_type == 'p':
        columns.insert(1, {'name': 'category__name', 'title': 'Category', 'orderable': True})

    statuses = ['draft', 'locked']
    categories = list(PhysicsCategory.objects.values_list('name', flat=True)) if tag_type == 'p' else []

    context = {
        'table_title': f'{schema["label"]} Tags',
        'table_description': f'All {schema["label"].lower()} tags registered in PCS.',
        'ajax_url': reverse('pcs:tags_datatable_ajax', args=[tag_type]),
        'columns': columns,
        'tag_type': tag_type,
        'schema': schema,
        'statuses': statuses,
        'categories': categories,
        'selected_status': status_filter,
        'selected_category': category_filter,
    }
    return render(request, 'pcs/tag_list.html', context)


def tags_datatable_ajax(request, tag_type):
    model = TAG_MODELS[tag_type]

    if tag_type == 'p':
        col_names = ['tag_label', 'category__name', 'description', 'status', 'created_by', 'created_at', 'actions']
    else:
        col_names = ['tag_label', 'description', 'status', 'created_by', 'created_at', 'actions']

    dt = DataTablesProcessor(request, col_names, default_order_column=0, default_order_direction='desc')

    qs = model.objects.all()
    if tag_type == 'p':
        qs = qs.select_related('category')

    filters = get_filter_params(request, ['status', 'category'])
    if filters['status']:
        qs = qs.filter(status=filters['status'])
    if tag_type == 'p' and filters.get('category'):
        qs = qs.filter(category__name=filters['category'])

    records_total = model.objects.count()
    search_fields = ['tag_label', 'description', 'created_by']
    if tag_type == 'p':
        search_fields.append('category__name')
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()

    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    data = []
    for tag in page:
        compose_url = reverse('pcs:tag_compose', args=[tag_type])
        tag_url = f'{compose_url}?selected={tag.tag_number}'
        tag_link = f'<a href="{tag_url}">{tag.tag_label}</a>'
        status_badge = (
            f'<span class="badge bg-secondary">{tag.status}</span>'
            if tag.status == 'draft'
            else f'<span class="badge bg-success">{tag.status}</span>'
        )
        row = [tag_link]
        if tag_type == 'p':
            row.append(tag.category.name)
        row += [
            tag.description[:80] + ('...' if len(tag.description) > 80 else ''),
            status_badge,
            tag.created_by,
            format_datetime(tag.created_at),
            f'<a href="{tag_url}">View</a>',
        ]
        data.append(row)

    return dt.create_response(data, records_total, records_filtered)


def tag_detail(request, tag_type, tag_number):
    model = TAG_MODELS[tag_type]
    schema = TAG_SCHEMAS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)

    datasets = []
    if tag.status == 'locked':
        filter_kwarg = {f'{schema["prefix"]}__tag_number' if schema["prefix"] == 'p' else f'{"physics" if schema["prefix"] == "p" else {"e": "evgen", "s": "simu", "r": "reco"}[schema["prefix"]]}_tag': tag}
        # Build the correct filter field name
        field_map = {'p': 'physics_tag', 'e': 'evgen_tag', 's': 'simu_tag', 'r': 'reco_tag'}
        datasets = Dataset.objects.filter(**{field_map[tag_type]: tag}).order_by('-created_at')

    defs = get_param_defs(tag_type)
    context = {
        'tag': tag,
        'tag_type': tag_type,
        'schema': schema,
        'datasets': datasets,
        'required_fields': [d['name'] for d in defs if d.get('required')],
        'optional_fields': [d['name'] for d in defs if not d.get('required')],
    }
    return render(request, 'pcs/tag_detail.html', context)


@login_required
def tag_create(request, tag_type):
    schema = TAG_SCHEMAS[tag_type]

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    if request.method == 'POST':
        form = FormClass(request.POST, **form_kwargs)
        if form.is_valid():
            model = TAG_MODELS[tag_type]
            params = form.get_parameters()

            if tag_type == 'p':
                category = form.cleaned_data['category']
                tag_number = PhysicsTag.allocate_next(category)
                tag = PhysicsTag(
                    tag_number=tag_number,
                    category=category,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            else:
                tag_number = model.allocate_next()
                tag = model(
                    tag_number=tag_number,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            tag.save()
            messages.success(request, f"Tag {tag.tag_label} created.")
            compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
            return redirect(f'{compose_url}?selected={tag.tag_number}')
    else:
        form = FormClass(**form_kwargs)

    context = {
        'form': form,
        'tag_type': tag_type,
        'schema': schema,
    }
    template = 'pcs/tag_create_physics.html' if tag_type == 'p' else 'pcs/tag_create.html'
    return render(request, template, context)


def tag_compose(request, tag_type):
    """Split-panel browse + compose UI for physics tags."""
    schema = TAG_SCHEMAS[tag_type]
    model = TAG_MODELS[tag_type]

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    selected_tag = None
    if request.method == 'POST':
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        form = FormClass(request.POST, **form_kwargs)
        if form.is_valid():
            params = form.get_parameters()
            if tag_type == 'p':
                category = form.cleaned_data['category']
                tag_number = PhysicsTag.allocate_next(category)
                tag = PhysicsTag(
                    tag_number=tag_number,
                    category=category,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            else:
                tag_number = model.allocate_next()
                tag = model(
                    tag_number=tag_number,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            tag.save()
            messages.success(request, f"Tag {tag.tag_label} created.")
            compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
            return redirect(f'{compose_url}?selected={tag.tag_number}')
    else:
        form = FormClass(**form_kwargs)
        selected_tag = request.GET.get('selected')

    qs = model.objects.order_by('-tag_number')
    if tag_type == 'p':
        qs = qs.select_related('category')
    tags_data = []
    for t in qs:
        entry = {
            'tag_number': t.tag_number,
            'tag_label': t.tag_label,
            'status': t.status,
            'description': t.description,
            'parameters': t.parameters,
            'created_by': t.created_by,
            'created_at': t.created_at.strftime('%Y-%m-%d %H:%M'),
        }
        if tag_type == 'p':
            entry['category_digit'] = t.category.digit
            entry['category_name'] = t.category.name
        tags_data.append(entry)

    param_defs = get_param_defs(tag_type)
    choices_from_defs = {d['name']: d['choices'] for d in param_defs if d.get('choices')}
    filter_fields = [d['name'] for d in param_defs
                     if d['name'] not in ('notes', 'description')]

    # Peek at next tag suffix from PersistentState (read-only, no increment)
    from monitor_app.models import PersistentState
    state_keys = {'p': 'pcs_next_physics', 'e': 'pcs_next_evgen',
                  's': 'pcs_next_simu', 'r': 'pcs_next_reco'}
    try:
        ps = PersistentState.objects.get(id=1)
        next_suffix = ps.state_data.get(state_keys[tag_type], 1)
    except PersistentState.DoesNotExist:
        next_suffix = 1

    context = {
        'form': form,
        'tag_type': tag_type,
        'schema': schema,
        'tags_json': json.dumps(tags_data, default=str),
        'choices_json': json.dumps(choices_from_defs),
        'filter_fields_json': json.dumps(filter_fields),
        'param_defs_json': json.dumps(param_defs),
        'next_suffix': next_suffix,
        'username': request.user.username if request.user.is_authenticated else '',
        'selected_tag': selected_tag,
    }
    return render(request, 'pcs/tag_compose.html', context)


def param_defs_api(request, tag_type):
    if tag_type not in TAG_SCHEMAS:
        return JsonResponse({'error': 'Invalid tag type'}, status=400)
    if request.method == 'GET':
        return JsonResponse({'defs': get_param_defs(tag_type)})
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Login required'}, status=403)
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        defs = data.get('defs')
        if not isinstance(defs, list):
            return JsonResponse({'error': 'defs must be a list'}, status=400)
        names_seen = set()
        for i, d in enumerate(defs):
            if not isinstance(d, dict) or not d.get('name'):
                return JsonResponse({'error': f'Invalid param def at index {i}'}, status=400)
            name = d['name'].strip()
            if name in names_seen:
                return JsonResponse({'error': f'Duplicate param name: {name}'}, status=400)
            names_seen.add(name)
            d['name'] = name
            d.setdefault('type', 'string')
            d.setdefault('required', False)
            d.setdefault('choices', [])
            d.setdefault('allow_other', True)
            d['sort_order'] = i
        save_param_defs(tag_type, defs)
        return JsonResponse({'ok': True, 'defs': defs})
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@login_required
def tag_delete(request, tag_type, tag_number):
    if request.method != 'POST':
        return redirect('pcs:tag_compose', tag_type=tag_type)
    model = TAG_MODELS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)
    if tag.status == 'locked':
        messages.error(request, f"Tag {tag.tag_label} is locked and cannot be deleted.")
        return redirect('pcs:tag_compose', tag_type=tag_type)
    if tag.created_by != request.user.username:
        messages.error(request, f"Only the creator ({tag.created_by}) can delete {tag.tag_label}.")
        return redirect('pcs:tag_compose', tag_type=tag_type)
    label = tag.tag_label
    tag.delete()
    messages.success(request, f"Tag {label} deleted.")
    return redirect('pcs:tag_compose', tag_type=tag_type)


@login_required
def tag_lock(request, tag_type, tag_number):
    compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
    selected_url = f'{compose_url}?selected={tag_number}'
    if request.method != 'POST':
        return redirect(selected_url)
    model = TAG_MODELS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)
    if tag.created_by != request.user.username:
        messages.error(request, f"Only the creator ({tag.created_by}) can lock this tag.")
    elif tag.status == 'locked':
        messages.warning(request, f"Tag {tag.tag_label} is already locked.")
    else:
        tag.status = 'locked'
        tag.save(update_fields=['status', 'updated_at'])
        messages.success(request, f"Tag {tag.tag_label} locked. It can now be used in datasets.")
    return redirect(selected_url)


@login_required
def tag_edit(request, tag_type, tag_number):
    model = TAG_MODELS[tag_type]
    schema = TAG_SCHEMAS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)

    compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
    selected_url = f'{compose_url}?selected={tag_number}'
    if tag.status == 'locked':
        messages.error(request, f"Tag {tag.tag_label} is locked and cannot be edited.")
        return redirect(selected_url)

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    if request.method == 'POST':
        form = FormClass(request.POST, **form_kwargs)
        if form.is_valid():
            tag.description = form.cleaned_data['description']
            tag.parameters = form.get_parameters()
            if tag_type == 'p':
                tag.category = form.cleaned_data['category']
            tag.save()
            messages.success(request, f"Tag {tag.tag_label} updated.")
            return redirect(selected_url)
    else:
        initial = {
            'description': tag.description,
            'created_by': tag.created_by,
        }
        if tag_type == 'p':
            initial['category'] = tag.category
        for k, v in tag.parameters.items():
            initial[f'param_{k}'] = v
        form = FormClass(initial=initial, **form_kwargs)

    context = {
        'form': form,
        'tag': tag,
        'tag_type': tag_type,
        'schema': schema,
        'editing': True,
    }
    template = 'pcs/tag_create_physics.html' if tag_type == 'p' else 'pcs/tag_create.html'
    return render(request, template, context)


# ── Datasets ──────────────────────────────────────────────────────

def datasets_compose(request):
    """Two-pane browse/create UI for datasets."""
    if request.method == 'POST' and request.user.is_authenticated:
        form = DatasetForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            ds = Dataset(
                scope=cd['scope'],
                detector_version=cd['detector_version'],
                detector_config=cd['detector_config'],
                physics_tag=cd['physics_tag'],
                evgen_tag=cd['evgen_tag'],
                simu_tag=cd['simu_tag'],
                reco_tag=cd['reco_tag'],
                description=cd.get('description', ''),
                created_by=cd['created_by'],
            )
            ds.save()
            messages.success(request, f"Dataset created: {ds.did}")
            return redirect(f"{reverse('pcs:datasets_compose')}?selected={ds.pk}")

    qs = Dataset.objects.filter(block_num=1).select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag',
    ).order_by('-created_at')
    datasets_data = []
    for ds in qs:
        datasets_data.append({
            'id': ds.id,
            'dataset_name': ds.dataset_name,
            'did': ds.did,
            'scope': ds.scope,
            'detector_version': ds.detector_version,
            'detector_config': ds.detector_config,
            'description': ds.description,
            'blocks': ds.blocks,
            'created_by': ds.created_by,
            'created_at': ds.created_at.strftime('%Y-%m-%d %H:%M'),
            'physics_tag': {'id': ds.physics_tag_id, 'label': ds.physics_tag.tag_label,
                            'description': ds.physics_tag.description, 'parameters': ds.physics_tag.parameters},
            'evgen_tag': {'id': ds.evgen_tag_id, 'label': ds.evgen_tag.tag_label,
                          'description': ds.evgen_tag.description, 'parameters': ds.evgen_tag.parameters},
            'simu_tag': {'id': ds.simu_tag_id, 'label': ds.simu_tag.tag_label,
                         'description': ds.simu_tag.description, 'parameters': ds.simu_tag.parameters},
            'reco_tag': {'id': ds.reco_tag_id, 'label': ds.reco_tag.tag_label,
                         'description': ds.reco_tag.description, 'parameters': ds.reco_tag.parameters},
        })

    # Full tag data for browsing and diffs
    tags_data = {}
    for ttype, model in TAG_MODELS_MAP.items():
        tag_list = []
        qs_tags = model.objects.order_by('tag_number')
        if ttype == 'p':
            qs_tags = qs_tags.select_related('category')
        for t in qs_tags:
            entry = {'id': t.id, 'tag_number': t.tag_number, 'label': t.tag_label,
                     'description': t.description, 'status': t.status,
                     'parameters': t.parameters, 'created_by': t.created_by}
            if ttype == 'p':
                entry['category_name'] = t.category.name
            tag_list.append(entry)
        tags_data[ttype] = tag_list

    context = {
        'datasets_json': json.dumps(datasets_data),
        'tags_json': json.dumps(tags_data),
        'selected': request.GET.get('selected'),
        'username': request.user.username if request.user.is_authenticated else '',
    }
    return render(request, 'pcs/dataset_compose.html', context)


def datasets_list(request):
    columns = [
        {'name': 'dataset_name', 'title': 'Dataset Name', 'orderable': True},
        {'name': 'physics_tag__tag_label', 'title': 'Physics', 'orderable': True},
        {'name': 'evgen_tag__tag_label', 'title': 'EvGen', 'orderable': True},
        {'name': 'simu_tag__tag_label', 'title': 'Simu', 'orderable': True},
        {'name': 'reco_tag__tag_label', 'title': 'Reco', 'orderable': True},
        {'name': 'blocks', 'title': 'Blocks', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
    ]
    context = {
        'table_title': 'Datasets',
        'table_description': 'All datasets registered in PCS.',
        'ajax_url': reverse('pcs:datasets_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'pcs/datasets_list.html', context)


def datasets_datatable_ajax(request):
    col_names = [
        'dataset_name', 'physics_tag__tag_label', 'evgen_tag__tag_label',
        'simu_tag__tag_label', 'reco_tag__tag_label', 'blocks', 'created_at',
    ]
    dt = DataTablesProcessor(request, col_names, default_order_column=6, default_order_direction='desc')

    # Only show block 1 rows (one row per logical dataset)
    qs = Dataset.objects.filter(block_num=1).select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag'
    )

    records_total = Dataset.objects.filter(block_num=1).count()
    search_fields = ['dataset_name', 'physics_tag__tag_label', 'evgen_tag__tag_label',
                     'simu_tag__tag_label', 'reco_tag__tag_label']
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()
    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    data = []
    for ds in page:
        detail_url = reverse('pcs:dataset_detail', args=[ds.id])
        p_url = f"{reverse('pcs:tag_compose', args=['p'])}?selected={ds.physics_tag.tag_number}"
        e_url = f"{reverse('pcs:tag_compose', args=['e'])}?selected={ds.evgen_tag.tag_number}"
        s_url = f"{reverse('pcs:tag_compose', args=['s'])}?selected={ds.simu_tag.tag_number}"
        r_url = f"{reverse('pcs:tag_compose', args=['r'])}?selected={ds.reco_tag.tag_number}"
        data.append([
            f'<a href="{detail_url}">{ds.dataset_name}</a>',
            f'<a href="{p_url}" title="{ds.physics_tag.description}">{ds.physics_tag.tag_label}</a>',
            f'<a href="{e_url}" title="{ds.evgen_tag.description}">{ds.evgen_tag.tag_label}</a>',
            f'<a href="{s_url}" title="{ds.simu_tag.description}">{ds.simu_tag.tag_label}</a>',
            f'<a href="{r_url}" title="{ds.reco_tag.description}">{ds.reco_tag.tag_label}</a>',
            str(ds.blocks),
            format_datetime(ds.created_at),
        ])

    return dt.create_response(data, records_total, records_filtered)


def dataset_detail(request, pk):
    dataset = get_object_or_404(
        Dataset.objects.select_related('physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag'),
        pk=pk,
    )
    blocks = Dataset.objects.filter(dataset_name=dataset.dataset_name).order_by('block_num')
    context = {
        'dataset': dataset,
        'blocks': blocks,
    }
    return render(request, 'pcs/dataset_detail.html', context)


@login_required
def dataset_create(request):
    if request.method == 'POST':
        form = DatasetForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            ds = Dataset(
                scope=cd['scope'],
                detector_version=cd['detector_version'],
                detector_config=cd['detector_config'],
                physics_tag=cd['physics_tag'],
                evgen_tag=cd['evgen_tag'],
                simu_tag=cd['simu_tag'],
                reco_tag=cd['reco_tag'],
                description=cd.get('description', ''),
                created_by=cd['created_by'],
            )
            ds.save()
            messages.success(request, f"Dataset created: {ds.did}")
            return redirect('pcs:dataset_detail', pk=ds.pk)
    else:
        form = DatasetForm()
    return render(request, 'pcs/dataset_create.html', {'form': form})


@login_required
def dataset_add_block(request, pk):
    if request.method != 'POST':
        return redirect('pcs:dataset_detail', pk=pk)
    dataset = get_object_or_404(Dataset, pk=pk)
    new_block_num = dataset.blocks + 1
    Dataset.objects.filter(dataset_name=dataset.dataset_name).update(blocks=new_block_num)
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
        created_by=request.user.username if request.user.is_authenticated else 'unknown',
    )
    messages.success(request, f"Block {new_block_num} added: {new_block.did}")
    return redirect('pcs:dataset_detail', pk=dataset.pk)


# ── Production Configs ────────────────────────────────────────────

def prod_configs_compose(request):
    """Two-pane browse/create/edit UI for production configs."""
    if request.method == 'POST' and request.user.is_authenticated:
        editing_pk = request.POST.get('editing_pk')
        if editing_pk:
            instance = get_object_or_404(ProdConfig, pk=editing_pk)
            form = ProdConfigForm(request.POST, instance=instance)
        else:
            form = ProdConfigForm(request.POST)
        if form.is_valid():
            pc = form.save()
            messages.success(request, f"Config '{pc.name}' {'updated' if editing_pk else 'created'}.")
            return redirect(f"{reverse('pcs:prod_configs_compose')}?selected={pc.pk}")

    qs = ProdConfig.objects.order_by('-updated_at')
    configs_data = []
    for pc in qs:
        configs_data.append({
            'id': pc.id,
            'name': pc.name,
            'description': pc.description,
            'bg_mixing': pc.bg_mixing,
            'bg_cross_section': pc.bg_cross_section,
            'bg_evtgen_file': pc.bg_evtgen_file,
            'copy_reco': pc.copy_reco,
            'copy_full': pc.copy_full,
            'copy_log': pc.copy_log,
            'use_rucio': pc.use_rucio,
            'jug_xl_tag': pc.jug_xl_tag,
            'container_image': pc.container_image,
            'target_hours_per_job': str(pc.target_hours_per_job) if pc.target_hours_per_job else '',
            'events_per_task': pc.events_per_task,
            'panda_site': pc.panda_site,
            'panda_queue': pc.panda_queue,
            'panda_working_group': pc.panda_working_group,
            'panda_resource_type': pc.panda_resource_type,
            'rucio_rse': pc.rucio_rse,
            'rucio_replication_rules': pc.rucio_replication_rules,
            'condor_template': pc.condor_template,
            'data': pc.data or {},
            'created_by': pc.created_by,
            'created_at': pc.created_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at': pc.updated_at.strftime('%Y-%m-%d %H:%M'),
        })

    context = {
        'configs_json': json.dumps(configs_data),
        'selected': request.GET.get('selected'),
        'username': request.user.username if request.user.is_authenticated else '',
    }
    return render(request, 'pcs/prod_config_compose.html', context)


def prod_configs_list(request):
    columns = [
        {'name': 'name', 'title': 'Name', 'orderable': True},
        {'name': 'description', 'title': 'Description', 'orderable': True},
        {'name': 'jug_xl_tag', 'title': 'JUG_XL', 'orderable': True},
        {'name': 'target_hours_per_job', 'title': 'Hours/Job', 'orderable': True},
        {'name': 'events_per_task', 'title': 'Events/Task', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
    ]
    context = {
        'table_title': 'Production Configs',
        'table_description': 'Reusable production configuration templates for job submission.',
        'ajax_url': reverse('pcs:prod_configs_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'pcs/prod_configs_list.html', context)


def prod_configs_datatable_ajax(request):
    col_names = ['name', 'description', 'jug_xl_tag', 'target_hours_per_job',
                 'events_per_task', 'created_by', 'updated_at']
    dt = DataTablesProcessor(request, col_names, default_order_column=6, default_order_direction='desc')

    qs = ProdConfig.objects.all()
    records_total = qs.count()
    search_fields = ['name', 'description', 'created_by', 'jug_xl_tag']
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()
    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    data = []
    for pc in page:
        detail_url = reverse('pcs:prod_config_detail', args=[pc.pk])
        data.append([
            f'<a href="{detail_url}">{pc.name}</a>',
            pc.description[:80] + ('...' if len(pc.description) > 80 else ''),
            pc.jug_xl_tag or '-',
            str(pc.target_hours_per_job) if pc.target_hours_per_job else '-',
            str(pc.events_per_task) if pc.events_per_task else '-',
            pc.created_by,
            format_datetime(pc.updated_at),
        ])

    return dt.create_response(data, records_total, records_filtered)


def prod_config_detail(request, pk):
    config = get_object_or_404(ProdConfig, pk=pk)
    return render(request, 'pcs/prod_config_detail.html', {'config': config})


@login_required
def prod_config_create(request):
    if request.method == 'POST':
        form = ProdConfigForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f"Production config '{form.instance.name}' created.")
            return redirect('pcs:prod_config_detail', pk=form.instance.pk)
    else:
        form = ProdConfigForm()
    return render(request, 'pcs/prod_config_form.html', {'form': form})


@login_required
def prod_config_edit(request, pk):
    config = get_object_or_404(ProdConfig, pk=pk)
    if request.method == 'POST':
        form = ProdConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, f"Production config '{config.name}' updated.")
            return redirect('pcs:prod_config_detail', pk=config.pk)
    else:
        form = ProdConfigForm(instance=config)
    return render(request, 'pcs/prod_config_form.html', {'form': form, 'editing': True, 'config': config})


# ── Production Tasks ─────────────────────────────────────────────

TAG_MODELS_MAP = {'p': PhysicsTag, 'e': EvgenTag, 's': SimuTag, 'r': RecoTag}


def prod_tasks_list(request):
    columns = [
        {'name': 'name', 'title': 'Name', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'dataset__dataset_name', 'title': 'Dataset', 'orderable': True},
        {'name': 'prod_config__name', 'title': 'Config', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
    ]
    context = {
        'table_title': 'Production Tasks',
        'table_description': 'Production task compositions (Dataset + Config).',
        'ajax_url': reverse('pcs:prod_tasks_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'pcs/prod_tasks_list.html', context)


def prod_tasks_datatable_ajax(request):
    col_names = ['name', 'status', 'dataset__dataset_name', 'prod_config__name',
                 'created_by', 'updated_at']
    dt = DataTablesProcessor(request, col_names, default_order_column=5, default_order_direction='desc')

    qs = ProdTask.objects.select_related('dataset', 'prod_config')
    records_total = qs.count()
    search_fields = ['name', 'description', 'dataset__dataset_name', 'prod_config__name', 'created_by']
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()
    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    status_colors = {'draft': 'secondary', 'ready': 'primary', 'submitted': 'info',
                     'completed': 'success', 'failed': 'danger'}
    data = []
    for t in page:
        detail_url = reverse('pcs:prod_task_detail', args=[t.pk])
        color = status_colors.get(t.status, 'secondary')
        data.append([
            f'<a href="{detail_url}">{t.name}</a>',
            f'<span class="badge bg-{color}">{t.status}</span>',
            t.dataset.dataset_name,
            t.prod_config.name,
            t.created_by,
            format_datetime(t.updated_at),
        ])

    return dt.create_response(data, records_total, records_filtered)


def prod_task_detail(request, pk):
    from .commands import build_task_params
    task = get_object_or_404(
        ProdTask.objects.select_related(
            'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
            'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
        ),
        pk=pk,
    )
    try:
        task_params = build_task_params(task)
        task_params_json = json.dumps(task_params, indent=2, sort_keys=False, default=str)
        task_params_error = None
    except Exception as e:
        task_params_json = None
        task_params_error = str(e)
    return render(request, 'pcs/prod_task_detail.html', {
        'task': task,
        'task_params_json': task_params_json,
        'task_params_error': task_params_error,
    })


def prod_task_compose(request):
    """Two-pane compose UI for building production tasks."""
    # Preload all component data as JSON for client-side browsing
    datasets_qs = Dataset.objects.filter(block_num=1).select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag',
    ).order_by('-created_at')
    datasets_data = []
    for ds in datasets_qs:
        datasets_data.append({
            'id': ds.id,
            'dataset_name': ds.dataset_name,
            'did': ds.did,
            'scope': ds.scope,
            'detector_version': ds.detector_version,
            'detector_config': ds.detector_config,
            'physics_tag': {'label': ds.physics_tag.tag_label, 'description': ds.physics_tag.description,
                            'parameters': ds.physics_tag.parameters},
            'evgen_tag': {'label': ds.evgen_tag.tag_label, 'description': ds.evgen_tag.description,
                          'parameters': ds.evgen_tag.parameters},
            'simu_tag': {'label': ds.simu_tag.tag_label, 'description': ds.simu_tag.description,
                         'parameters': ds.simu_tag.parameters},
            'reco_tag': {'label': ds.reco_tag.tag_label, 'description': ds.reco_tag.description,
                         'parameters': ds.reco_tag.parameters},
            'created_by': ds.created_by,
            'created_at': ds.created_at.strftime('%Y-%m-%d %H:%M'),
        })

    configs_qs = ProdConfig.objects.order_by('-updated_at')
    configs_data = []
    for pc in configs_qs:
        configs_data.append({
            'id': pc.id,
            'name': pc.name,
            'description': pc.description,
            'jug_xl_tag': pc.jug_xl_tag,
            'container_image': pc.container_image,
            'bg_mixing': pc.bg_mixing,
            'bg_cross_section': pc.bg_cross_section,
            'bg_evtgen_file': pc.bg_evtgen_file,
            'copy_reco': pc.copy_reco,
            'copy_full': pc.copy_full,
            'copy_log': pc.copy_log,
            'use_rucio': pc.use_rucio,
            'target_hours_per_job': str(pc.target_hours_per_job) if pc.target_hours_per_job else '',
            'events_per_task': pc.events_per_task,
            'panda_site': pc.panda_site,
            'panda_queue': pc.panda_queue,
            'panda_working_group': pc.panda_working_group,
            'panda_resource_type': pc.panda_resource_type,
            'rucio_rse': pc.rucio_rse,
            'data': pc.data or {},
            'created_by': pc.created_by,
            'updated_at': pc.updated_at.strftime('%Y-%m-%d %H:%M'),
        })

    tasks_qs = ProdTask.objects.select_related('dataset', 'prod_config').order_by('-updated_at')
    tasks_data = []
    for t in tasks_qs:
        tasks_data.append({
            'id': t.id,
            'name': t.name,
            'status': t.status,
            'dataset_id': t.dataset_id,
            'dataset_name': t.dataset.dataset_name,
            'prod_config_id': t.prod_config_id,
            'prod_config_name': t.prod_config.name,
            'csv_file': t.csv_file,
            'overrides': t.overrides or {},
            'description': t.description,
            'condor_command': t.condor_command,
            'panda_command': t.panda_command,
            'created_by': t.created_by,
            'updated_at': t.updated_at.strftime('%Y-%m-%d %H:%M'),
        })

    context = {
        'datasets_json': json.dumps(datasets_data),
        'configs_json': json.dumps(configs_data),
        'tasks_json': json.dumps(tasks_data),
        'selected_task': request.GET.get('selected'),
        'username': request.user.username if request.user.is_authenticated else '',
    }
    return render(request, 'pcs/prod_task_compose.html', context)


@login_required
def prod_task_delete(request, pk):
    if request.method != 'POST':
        return redirect('pcs:prod_task_detail', pk=pk)
    task = get_object_or_404(ProdTask, pk=pk)
    if task.status != 'draft':
        messages.error(request, "Only draft tasks can be deleted.")
        return redirect('pcs:prod_task_detail', pk=pk)
    task.delete()
    messages.success(request, f"Task '{task.name}' deleted.")
    return redirect('pcs:prod_tasks_list')


def prod_task_generate_commands(request, pk):
    """JSON endpoint: regenerate and return commands for a ProdTask."""
    task = get_object_or_404(
        ProdTask.objects.select_related(
            'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
            'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
        ),
        pk=pk,
    )
    task.generate_commands()
    task.save(update_fields=['condor_command', 'panda_command', 'updated_at'])
    return JsonResponse({
        'condor_command': task.condor_command,
        'panda_command': task.panda_command,
    })
