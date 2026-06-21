"""Global template context for lightweight monitor state."""

from .system_status import status_summary


def _active_nav(request):
    match = getattr(request, 'resolver_match', None)
    namespace = getattr(match, 'namespace', '') or ''
    url_name = getattr(match, 'url_name', '') or ''
    kwargs = getattr(match, 'kwargs', {}) or {}
    tag_type = kwargs.get('tag_type')

    pcs_questionnaire_names = {
        'questionnaires_list',
        'questionnaire_import',
        'questionnaire_detail',
        'questionnaire_match_add',
        'questionnaire_match_remove',
    }
    pcs_catalog_names = {
        'pcs_catalog',
        'pcs_catalog_csv_update',
        'pcs_catalog_past_update',
        'pcs_catalog_rucio_update',
        'pcs_catalog_evgen_update',
        'pcs_catalog_set_current',
        'pcs_catalog_set_last',
    }
    pcs_tag_names = {
        'physics_categories_list',
        'physics_category_create',
        'tag_compose',
        'param_defs_api',
        'tags_list',
        'tags_datatable_ajax',
        'tag_detail',
        'tag_edit',
        'tag_delete',
        'tag_lock',
        'tag_datasets',
    }
    pcs_dataset_names = {
        'datasets_compose',
        'datasets_list',
        'datasets_datatable_ajax',
        'dataset_create',
        'dataset_detail',
        'dataset_add_block',
        'compose_dataset_detail',
        'rucio_did_detail',
        'rucio_did_files',
    }
    pcs_config_names = {
        'prod_configs_compose',
        'prod_configs_list',
        'prod_configs_datatable_ajax',
        'prod_config_create',
        'prod_config_detail',
        'prod_config_edit',
    }
    pcs_task_names = {
        'prod_task_compose',
        'prod_tasks_list',
        'prod_tasks_datatable_ajax',
        'prod_task_detail',
        'prod_task_delete',
        'prod_task_generate_commands',
        'compose_task_detail',
    }
    pcs_names = (
        {'pcs_hub'}
        | pcs_tag_names
        | pcs_dataset_names
        | pcs_config_names
        | pcs_task_names
    )

    workflow_names = {
        'workflows_home',
        'workflow_definitions_list',
        'workflow_definitions_datatable_ajax',
        'workflow_definitions_filter_counts',
        'workflow_definition_detail',
        'workflow_executions_list',
        'workflow_executions_datatable_ajax',
        'workflow_executions_filter_counts',
        'workflow_execution_detail',
        'namespaces_list',
        'namespaces_datatable_ajax',
        'namespace_detail',
    }
    file_names = {
        'stf_files_list',
        'stf_files_datatable_ajax',
        'stf_file_detail',
        'fastmon_files_list',
        'fastmon_files_datatable_ajax',
        'tf_slices_list',
        'tf_slices_datatable_ajax',
    }
    log_names = {
        'log_summary',
        'log_summary_datatable_ajax',
        'log_list',
        'logs_datatable_ajax',
        'log_filter_counts',
        'log_detail',
    }
    database_names = {
        'database_tables_list',
        'database_tables_datatable_ajax',
        'database_table_list',
        'database_table_datatable_ajax',
    }
    panda_rucio_names = {
        'panda_hub',
        'panda_activity',
        'panda_jobs_list',
        'panda_jobs_datatable_ajax',
        'panda_jobs_filter_counts',
        'panda_job_detail',
        'panda_payload_log',
        'epicprod_job_detail',
        'epicprod_job_refresh',
        'panda_view_text',
        'panda_tasks_list',
        'panda_tasks_datatable_ajax',
        'panda_tasks_filter_counts',
        'panda_task_detail',
        'panda_errors_list',
        'panda_errors_datatable_ajax',
        'panda_diagnostics_list',
        'panda_diagnostics_datatable_ajax',
        'epic_queues_list',
        'epic_queue_detail',
        'panda_queues_list',
        'panda_queue_detail',
        'panda_database_tables_list',
        'panda_database_table_list',
        'idds_database_tables_list',
        'idds_database_table_list',
        'rucio_endpoints_list',
        'rucio_endpoint_detail',
    }
    panda_database_names = {
        'panda_database_tables_list',
        'panda_database_tables_datatable_ajax',
        'panda_database_table_list',
        'panda_database_table_datatable_ajax',
    }
    idds_database_names = {
        'idds_database_tables_list',
        'idds_database_tables_datatable_ajax',
        'idds_database_table_list',
        'idds_database_table_datatable_ajax',
    }
    rucio_endpoint_names = {
        'rucio_endpoints_list',
        'rucio_endpoints_datatable_ajax',
        'rucio_endpoint_detail',
    }
    alarm_names = {
        'alarms_dashboard',
        'alarm_event_detail',
        'alarm_config_edit',
        'alarm_config_save',
        'alarm_config_version',
        'alarm_test',
        'alarm_run_report',
        'alarm_task_history',
        'team_create',
        'team_new',
        'team_edit',
        'team_save',
        'team_version',
    }

    return {
        'requests': namespace == 'pcs' and url_name in pcs_questionnaire_names,
        'pcs': namespace == 'pcs' and url_name in pcs_names,
        'pcs_hub': namespace == 'pcs' and url_name == 'pcs_hub',
        'pcs_categories': namespace == 'pcs' and url_name in {
            'physics_categories_list',
            'physics_category_create',
        },
        'pcs_tags': namespace == 'pcs' and url_name in pcs_tag_names,
        'pcs_physics_tags': namespace == 'pcs' and url_name in pcs_tag_names and tag_type == 'p',
        'pcs_evgen_tags': namespace == 'pcs' and url_name in pcs_tag_names and tag_type == 'e',
        'pcs_simu_tags': namespace == 'pcs' and url_name in pcs_tag_names and tag_type == 's',
        'pcs_reco_tags': namespace == 'pcs' and url_name in pcs_tag_names and tag_type == 'r',
        'pcs_background_tags': namespace == 'pcs' and url_name in pcs_tag_names and tag_type == 'k',
        'pcs_datasets': namespace == 'pcs' and url_name in pcs_dataset_names,
        'pcs_configs': namespace == 'pcs' and url_name in pcs_config_names,
        'pcs_tasks': namespace == 'pcs' and url_name in pcs_task_names,
        'campaigns': namespace == 'pcs' and url_name in pcs_catalog_names,
        'workflows': namespace == 'monitor_app' and url_name in workflow_names,
        'files': namespace == 'monitor_app' and url_name in file_names,
        'agents': namespace == 'monitor_app' and url_name in {
            'workflow_agents_list',
            'workflow_agents_datatable_ajax',
            'agent_detail',
        },
        'subscribers': namespace == 'monitor_app' and url_name in {
            'subscribers_list',
            'subscribers_datatable_ajax',
            'subscribers_filter_counts',
            'subscriber_detail',
        },
        'messages': namespace == 'monitor_app' and url_name in {
            'workflow_messages',
            'workflow_messages_datatable_ajax',
            'workflow_messages_filter_counts',
            'message_detail',
        },
        'logs': namespace == 'monitor_app' and url_name in log_names,
        'database': namespace == 'monitor_app' and url_name in database_names,
        'state': namespace == 'monitor_app' and url_name == 'persistent_state',
        'panda_rucio': namespace == 'monitor_app' and url_name in panda_rucio_names,
        'panda_hub': namespace == 'monitor_app' and url_name == 'panda_hub',
        'panda_activity': namespace == 'monitor_app' and url_name == 'panda_activity',
        'panda_tasks': namespace == 'monitor_app' and url_name in {
            'panda_tasks_list',
            'panda_tasks_datatable_ajax',
            'panda_tasks_filter_counts',
            'panda_task_detail',
        },
        'panda_jobs': namespace == 'monitor_app' and url_name in {
            'panda_jobs_list',
            'panda_jobs_datatable_ajax',
            'panda_jobs_filter_counts',
            'panda_job_detail',
            'panda_payload_log',
            'epicprod_job_detail',
            'epicprod_job_refresh',
        },
        'panda_errors': namespace == 'monitor_app' and url_name in {
            'panda_errors_list',
            'panda_errors_datatable_ajax',
        },
        'panda_diagnostics': namespace == 'monitor_app' and url_name in {
            'panda_diagnostics_list',
            'panda_diagnostics_datatable_ajax',
        },
        'panda_queues': namespace == 'monitor_app' and url_name in {
            'epic_queues_list',
            'epic_queue_detail',
        },
        'alarms': namespace == 'monitor_app' and url_name in alarm_names,
        'panda_database': namespace == 'monitor_app' and url_name in panda_database_names,
        'idds_database': namespace == 'monitor_app' and url_name in idds_database_names,
        'rucio_endpoints': namespace == 'monitor_app' and url_name in rucio_endpoint_names,
        'system': namespace == 'monitor_app' and url_name in {
            'system_status',
            'system_status_root',
        },
        'about': namespace == 'monitor_app' and url_name == 'about',
        'account': namespace == 'monitor_app' and url_name == 'account',
    }


def system_status_nav(request):
    summary = status_summary()
    return {
        'active_nav': _active_nav(request),
        'system_status_overall': summary.get('overall_status', 'unknown'),
        'system_status_reason': summary.get('overall_reason', ''),
        'system_status_latest_checked_at': summary.get('latest_checked_at'),
    }
