"""
Tag parameter schemas — required and optional fields per tag type.

Extensible without migration: add fields here and they appear in forms and validation.
The 'required' list is enforced on tag creation. The 'optional' list populates form fields.
All values are stored as JSON in the tag's parameters field.

Processes and choices assembled from 26.02.0 campaign production pages:
  https://eic.github.io/epic-prod/FULL/26.02.0/
  https://eic.github.io/epic-prod/RECO/26.02.0/
"""

TAG_SCHEMAS = {
    'p': {
        'required': ['process', 'beam_energy_electron', 'beam_energy_hadron'],
        'optional': [
            'beam_species', 'q2_range',
            'decay_mode', 'hadron_charge', 'coherence', 'model', 'polarization',
            'notes',
        ],
        'label': 'Physics',
        'prefix': 'p',
        'model': 'PhysicsTag',
        'choices': {
            'process': [
                'DIS_NC', 'DIS_CC', 'DDIS',
                'DVCS', 'DDVCS',
                'SIDIS_D0',
                'DEMP', 'DVMP',
                'DIFFRACTIVE_JPSI', 'DIFFRACTIVE_PHI', 'DIFFRACTIVE_RHO',
                'PHOTOPRODUCTION_JPSI', 'UPSILON',
            ],
            'beam_energy_electron': ['5', '10', '18', 'N/A'],
            'beam_energy_hadron': ['41', '100', '110', '130', '250', '275', 'N/A'],
            'beam_species': ['ep', 'eHe3', 'eAu'],
            'q2_range': [
                'minQ2=1', 'minQ2=10', 'minQ2=100', 'minQ2=1000',
                'q2_0_10', 'q2_1_100', 'q2_1_1000',
                'q2_1to10', 'q2_1to50', 'q2_1to10000',
                'q2_2to10', 'q2_10to100', 'q2_100to10000',
                'q2_nocut',
            ],
            'decay_mode': ['edecay', 'mudecay'],
            'hadron_charge': ['hplus', 'hminus'],
            'coherence': ['coherent'],
            'model': ['bsat', 'hiAcc', 'hiDiv'],
            'polarization': ['unpolarised'],
        },
    },
    'e': {
        'required': ['generator', 'generator_version'],
        'optional': [
            'signal_freq', 'signal_status',
            'bg_tag_prefix', 'bg_files',
            'notes',
        ],
        'label': 'EvGen',
        'prefix': 'e',
        'model': 'EvgenTag',
        'choices': {
            'generator': [
                'pythia8', 'EpIC', 'BeAGLE', 'eSTARlight', 'sartre',
                'DEMPgen', 'lAger', 'rapgap', 'particle_gun',
            ],
            'generator_version': [
                '8.310', '8.306-1.0', '8.306-1.1',
                'v1.1.6-1.2', '1.1.6-1.0',
                '1.03.02-1.2', '1.03.02-2.0', '1.03.02-1.1',
                '1.3.0-1.0', '1.39-1.1', '1.2.4',
                '3.6.1-1.0', '3.310-1.0',
            ],
            'bg_tag_prefix': [
                'Bkg_Exact1S_2us/GoldCt/5um',
                'Bkg_Exact1S_2us/GoldCt/10um',
            ],
        },
    },
    's': {
        'required': ['detector_sim', 'sim_version'],
        'optional': ['background_config', 'digitization', 'notes'],
        'label': 'Simulation',
        'prefix': 's',
        'model': 'SimuTag',
        'choices': {
            'detector_sim': ['npsim'],
            'sim_version': ['26.02.0'],
            'background_config': [
                'none',
                'Bkg_Exact1S_2us/GoldCt/5um',
                'Bkg_Exact1S_2us/GoldCt/10um',
            ],
        },
    },
    'r': {
        'required': ['reco_version', 'reco_config'],
        'optional': ['calibration_tag', 'alignment_tag', 'notes'],
        'label': 'Reconstruction',
        'prefix': 'r',
        'model': 'RecoTag',
        'choices': {
            'reco_version': ['26.02.0'],
            'reco_config': ['standard'],
        },
    },
}


def get_tag_model(tag_type):
    from . import models
    return getattr(models, TAG_SCHEMAS[tag_type]['model'])


def _schema_to_param_defs(tag_type):
    schema = TAG_SCHEMAS[tag_type]
    choices = schema.get('choices', {})
    defs = []
    for i, name in enumerate(schema['required']):
        defs.append({
            'name': name,
            'type': 'string',
            'required': True,
            'choices': choices.get(name, []),
            'allow_other': True,
            'sort_order': i,
        })
    offset = len(schema['required'])
    for i, name in enumerate(schema['optional']):
        defs.append({
            'name': name,
            'type': 'string',
            'required': False,
            'choices': choices.get(name, []),
            'allow_other': True,
            'sort_order': offset + i,
        })
    return defs


def _state_key(tag_type):
    return f'emi_param_defs_{tag_type}'


def get_param_defs(tag_type):
    from monitor_app.models import PersistentState
    key = _state_key(tag_type)
    try:
        ps = PersistentState.objects.get(id=1)
        defs = ps.state_data.get(key)
        if defs is not None:
            return defs
    except PersistentState.DoesNotExist:
        pass
    return seed_param_defs(tag_type)


def seed_param_defs(tag_type):
    from monitor_app.models import PersistentState
    defs = _schema_to_param_defs(tag_type)
    ps, _ = PersistentState.objects.get_or_create(id=1, defaults={'state_data': {}})
    ps.state_data[_state_key(tag_type)] = defs
    ps.save(update_fields=['state_data', 'updated_at'])
    return defs


def save_param_defs(tag_type, defs):
    from monitor_app.models import PersistentState
    ps, _ = PersistentState.objects.get_or_create(id=1, defaults={'state_data': {}})
    ps.state_data[_state_key(tag_type)] = defs
    ps.save(update_fields=['state_data', 'updated_at'])


def validate_parameters(tag_type, parameters):
    defs = get_param_defs(tag_type)
    required = [d['name'] for d in defs if d.get('required')]
    missing = [f for f in required if f not in parameters or not parameters[f]]
    if missing:
        return False, f"Missing required parameters: {', '.join(missing)}"
    return True, None
