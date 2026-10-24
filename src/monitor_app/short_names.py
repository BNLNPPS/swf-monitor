"""Short names for concise settings: the computing centre floor and its
board lines, compact tables, titles. One filter, so a name reads the same
everywhere it is shortened (Torre Wenaus, 2026-10-22); templates use it as
``{{ name|short }}`` (swf_fmt). A name with no short form passes through
unchanged, and the full name stays in the record and on detail pages.
"""
import re

# A name whose short form no rule below gives.
SHORT_NAMES = {
    'NERSC': 'Perlmutter',
    'Compute Canada': 'Canada',
    'BNL_OSG_PanDA_1': 'OSG',
    'IRISHEP-SSL-UCHICAGO': 'IRISHEP',
    'UNL-PATH': 'UNL',
    'UWM-Mortimer': 'UWM',
    'Rhodes-HPC': 'Rhodes',
    'OSG_US_FSU_HNPGRID': 'FSU',
    'UW-La Crosse CSCPE': 'La Crosse',
    'UA Little Rock ITS': 'UALR',
    'Center for Advanced Research Computi': 'CARC',
    'Center for Advanced Research Computing': 'CARC',
    'WISC-PATH': 'WISC',
    'FANDM-ITS': 'F&M',
    'Colgate ITS': 'Colgate',
    'Syracuse-OSPool': 'Syracuse',
    'SYRA-PATH': 'SYRA',
    'OU-Sooner': 'OU',
    'FIU-IRCC': 'FIU',
    'ND_CAMLGPU': 'ND-CAML',
    # the OSG pools our submit host negotiates in, by central manager
    'cm-1.ospool.osg-htc.org': 'OSPool',
    'cm-2.ospool.osg-htc.org': 'OSPool',
    'jlab-cm.osg.chtc.io': 'JLab pool',
    'scicollector901.jlab.org': 'JLab pool',
}

# Organisation prefixes a site name carries before the cluster's own name:
# ComputeCanada-Fir reads Fir.
PREFIXES = ('ComputeCanada-',)


def short_name(name):
    """The concise form of a site, queue or resource name."""
    if not name:
        return name
    name = str(name)
    if name in SHORT_NAMES:
        return SHORT_NAMES[name]
    for prefix in PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def short_text(value):
    """Use display site names in prose, preserving internal identifiers."""
    return re.sub(r'(?<!\w)(NERSC|Compute Canada)(?!\w)',
                  lambda match: short_name(match.group()), str(value or ''))
