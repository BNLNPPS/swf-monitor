"""
EMI web UI views and DataTable AJAX endpoints.

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
    PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, Dataset, ProdConfig,
)
from .schemas import TAG_SCHEMAS, get_tag_model
from .forms import PhysicsTagForm, SimpleTagForm, DatasetForm, PhysicsCategoryForm, ProdConfigForm


def emi_hub(request):
    context = {
        'categories_count': PhysicsCategory.objects.count(),
        'physics_tags_count': PhysicsTag.objects.count(),
        'evgen_tags_count': EvgenTag.objects.count(),
        'simu_tags_count': SimuTag.objects.count(),
        'reco_tags_count': RecoTag.objects.count(),
        'datasets_count': Dataset.objects.values('dataset_name').distinct().count(),
        'prod_configs_count': ProdConfig.objects.count(),
    }
    return render(request, 'emi/emi_hub.html', context)


# ── Physics Categories ────────────────────────────────────────────

def physics_categories_list(request):
    categories = PhysicsCategory.objects.annotate(tag_count=Count('tags'))
    return render(request, 'emi/physics_categories_list.html', {'categories': categories})


@login_required
def physics_category_create(request):
    if request.method == 'POST':
        form = PhysicsCategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f"Category {form.instance.digit}: {form.instance.name} created.")
            return redirect('emi:physics_categories_list')
    else:
        form = PhysicsCategoryForm()
    return render(request, 'emi/physics_category_create.html', {'form': form})


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
        'table_description': f'All {schema["label"].lower()} tags registered in EMI.',
        'ajax_url': reverse('emi:tags_datatable_ajax', args=[tag_type]),
        'columns': columns,
        'tag_type': tag_type,
        'schema': schema,
        'statuses': statuses,
        'categories': categories,
        'selected_status': status_filter,
        'selected_category': category_filter,
    }
    return render(request, 'emi/tag_list.html', context)


def tags_datatable_ajax(request, tag_type):
    model = TAG_MODELS[tag_type]

    if tag_type == 'p':
        col_names = ['tag_label', 'category__name', 'description', 'status', 'created_by', 'created_at', 'actions']
    else:
        col_names = ['tag_label', 'description', 'status', 'created_by', 'created_at', 'actions']

    dt = DataTablesProcessor(request, col_names, default_order_column=0, default_order_direction='asc')

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
        detail_url = reverse('emi:tag_detail', args=[tag_type, tag.tag_number])
        tag_link = f'<a href="{detail_url}">{tag.tag_label}</a>'
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
            f'<a href="{detail_url}">View</a>',
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

    context = {
        'tag': tag,
        'tag_type': tag_type,
        'schema': schema,
        'datasets': datasets,
        'required_fields': schema['required'],
        'optional_fields': schema['optional'],
    }
    return render(request, 'emi/tag_detail.html', context)


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
            return redirect('emi:tag_detail', tag_type=tag_type, tag_number=tag.tag_number)
    else:
        form = FormClass(**form_kwargs)

    context = {
        'form': form,
        'tag_type': tag_type,
        'schema': schema,
    }
    template = 'emi/tag_create_physics.html' if tag_type == 'p' else 'emi/tag_create.html'
    return render(request, template, context)


@login_required
def tag_compose(request, tag_type):
    """Split-panel composition UI for creating tags from existing ones."""
    schema = TAG_SCHEMAS[tag_type]
    model = TAG_MODELS[tag_type]

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    if request.method == 'POST':
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
            return redirect('emi:tag_detail', tag_type=tag_type, tag_number=tag.tag_number)
    else:
        form = FormClass(**form_kwargs)

    qs = model.objects.all()
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
        }
        if tag_type == 'p':
            entry['category_digit'] = t.category.digit
            entry['category_name'] = t.category.name
        tags_data.append(entry)

    context = {
        'form': form,
        'tag_type': tag_type,
        'schema': schema,
        'tags_json': json.dumps(tags_data, default=str),
    }
    return render(request, 'emi/tag_compose_physics.html', context)


@login_required
def tag_lock(request, tag_type, tag_number):
    if request.method != 'POST':
        return redirect('emi:tag_detail', tag_type=tag_type, tag_number=tag_number)
    model = TAG_MODELS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)
    if tag.status == 'locked':
        messages.warning(request, f"Tag {tag.tag_label} is already locked.")
    else:
        tag.status = 'locked'
        tag.save(update_fields=['status', 'updated_at'])
        messages.success(request, f"Tag {tag.tag_label} locked. It can now be used in datasets.")
    return redirect('emi:tag_detail', tag_type=tag_type, tag_number=tag_number)


@login_required
def tag_edit(request, tag_type, tag_number):
    model = TAG_MODELS[tag_type]
    schema = TAG_SCHEMAS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)

    if tag.status == 'locked':
        messages.error(request, f"Tag {tag.tag_label} is locked and cannot be edited.")
        return redirect('emi:tag_detail', tag_type=tag_type, tag_number=tag_number)

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
            return redirect('emi:tag_detail', tag_type=tag_type, tag_number=tag_number)
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
    template = 'emi/tag_create_physics.html' if tag_type == 'p' else 'emi/tag_create.html'
    return render(request, template, context)


# ── Datasets ──────────────────────────────────────────────────────

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
        'table_description': 'All datasets registered in EMI.',
        'ajax_url': reverse('emi:datasets_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'emi/datasets_list.html', context)


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
        detail_url = reverse('emi:dataset_detail', args=[ds.id])
        p_url = reverse('emi:tag_detail', args=['p', ds.physics_tag.tag_number])
        e_url = reverse('emi:tag_detail', args=['e', ds.evgen_tag.tag_number])
        s_url = reverse('emi:tag_detail', args=['s', ds.simu_tag.tag_number])
        r_url = reverse('emi:tag_detail', args=['r', ds.reco_tag.tag_number])
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
    return render(request, 'emi/dataset_detail.html', context)


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
            return redirect('emi:dataset_detail', pk=ds.pk)
    else:
        form = DatasetForm()
    return render(request, 'emi/dataset_create.html', {'form': form})


@login_required
def dataset_add_block(request, pk):
    if request.method != 'POST':
        return redirect('emi:dataset_detail', pk=pk)
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
    return redirect('emi:dataset_detail', pk=dataset.pk)


# ── Production Configs ────────────────────────────────────────────

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
        'ajax_url': reverse('emi:prod_configs_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'emi/prod_configs_list.html', context)


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
        detail_url = reverse('emi:prod_config_detail', args=[pc.pk])
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
    return render(request, 'emi/prod_config_detail.html', {'config': config})


@login_required
def prod_config_create(request):
    if request.method == 'POST':
        form = ProdConfigForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f"Production config '{form.instance.name}' created.")
            return redirect('emi:prod_config_detail', pk=form.instance.pk)
    else:
        form = ProdConfigForm()
    return render(request, 'emi/prod_config_form.html', {'form': form})


@login_required
def prod_config_edit(request, pk):
    config = get_object_or_404(ProdConfig, pk=pk)
    if request.method == 'POST':
        form = ProdConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, f"Production config '{config.name}' updated.")
            return redirect('emi:prod_config_detail', pk=config.pk)
    else:
        form = ProdConfigForm(instance=config)
    return render(request, 'emi/prod_config_form.html', {'form': form, 'editing': True, 'config': config})
