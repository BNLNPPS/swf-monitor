"""
Tag parameter schemas — required and optional fields per tag type.

Extensible without migration: add fields here and they appear in forms and validation.
The 'required' list is enforced on tag creation. The 'optional' list populates form fields.
All values are stored as JSON in the tag's parameters field.
"""

TAG_SCHEMAS = {
    'p': {
        'required': ['process', 'beam_energy_electron', 'beam_energy_hadron'],
        'optional': ['crosssection', 'generator', 'luminosity', 'notes'],
        'label': 'Physics',
        'prefix': 'p',
        'model': 'PhysicsTag',
    },
    'e': {
        'required': ['signal_freq', 'signal_status'],
        'optional': ['generator_version', 'decay_mode', 'notes'],
        'label': 'EvGen',
        'prefix': 'e',
        'model': 'EvgenTag',
    },
    's': {
        'required': ['detector_sim', 'sim_version'],
        'optional': ['background_config', 'digitization', 'notes'],
        'label': 'Simulation',
        'prefix': 's',
        'model': 'SimuTag',
    },
    'r': {
        'required': ['reco_version', 'reco_config'],
        'optional': ['calibration_tag', 'alignment_tag', 'notes'],
        'label': 'Reconstruction',
        'prefix': 'r',
        'model': 'RecoTag',
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
