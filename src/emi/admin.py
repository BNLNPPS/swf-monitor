from django.contrib import admin
from .models import PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, Dataset, ProdConfig


@admin.register(PhysicsCategory)
class PhysicsCategoryAdmin(admin.ModelAdmin):
    list_display = ('digit', 'name', 'created_by', 'created_at')
    search_fields = ('name',)


@admin.register(PhysicsTag)
class PhysicsTagAdmin(admin.ModelAdmin):
    list_display = ('tag_label', 'category', 'status', 'description', 'created_by', 'created_at')
    list_filter = ('status', 'category')
    search_fields = ('tag_label', 'description')
    readonly_fields = ('tag_number', 'tag_label', 'created_at', 'updated_at')


@admin.register(EvgenTag)
class EvgenTagAdmin(admin.ModelAdmin):
    list_display = ('tag_label', 'status', 'description', 'created_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('tag_label', 'description')
    readonly_fields = ('tag_number', 'tag_label', 'created_at', 'updated_at')


@admin.register(SimuTag)
class SimuTagAdmin(admin.ModelAdmin):
    list_display = ('tag_label', 'status', 'description', 'created_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('tag_label', 'description')
    readonly_fields = ('tag_number', 'tag_label', 'created_at', 'updated_at')


@admin.register(RecoTag)
class RecoTagAdmin(admin.ModelAdmin):
    list_display = ('tag_label', 'status', 'description', 'created_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('tag_label', 'description')
    readonly_fields = ('tag_number', 'tag_label', 'created_at', 'updated_at')


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ('did', 'dataset_name', 'block_num', 'blocks', 'file_count', 'created_by', 'created_at')
    list_filter = ('detector_version', 'detector_config')
    search_fields = ('dataset_name', 'did')
    readonly_fields = ('created_at',)


@admin.register(ProdConfig)
class ProdConfigAdmin(admin.ModelAdmin):
    list_display = ('name', 'jug_xl_tag', 'target_hours_per_job', 'events_per_task',
                    'bg_mixing', 'use_rucio', 'created_by', 'updated_at')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at')
