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
        },
    },
    'r': {
        'required': ['reco_version', 'reco_config'],
        'optional': ['calibration_tag', 'alignment_tag', 'notes'],
        'label': 'Reconstruction',
        'prefix': 'r',
        'model': 'RecoTag',
        'choices': {
            'reco_config': ['standard'],
        },
    },
}


def get_tag_model(tag_type):
    from . import models
    return getattr(models, TAG_SCHEMAS[tag_type]['model'])


def validate_parameters(tag_type, parameters):
    schema = TAG_SCHEMAS[tag_type]
    missing = [f for f in schema['required'] if f not in parameters or not parameters[f]]
    if missing:
        return False, f"Missing required parameters: {', '.join(missing)}"
    return True, None
