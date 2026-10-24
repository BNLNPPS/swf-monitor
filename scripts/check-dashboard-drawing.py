#!/usr/bin/env python3
"""Architectural boundary: one dashboard document and one pillar implementation."""
from pathlib import Path
import hashlib
import re

root = Path(__file__).resolve().parents[1]
assets = root / 'src/monitor_app/static/monitor_app/prod_dashboard'
model = assets / 'epic-pillar-model.js'
assert model.is_file(), 'canonical pillar module missing'
for name in ('epic-dashboard-panels.js', 'epic-dashboard-links.js', 'epic-dashboard-weekly.js'):
    assert not (root / 'src/static' / name).exists(), f'duplicate dashboard source: {name}'
entry = (root / 'src/static/epic-dashboard-design.html').read_text()
assert '<iframe src="../panda/dashboard/"' in entry, 'development URL must use the canonical dashboard'
assert '<svg' not in entry and '<script' not in entry, 'development URL contains a second dashboard implementation'
template = (root / 'src/monitor_app/templates/monitor_app/prod_dashboard.html').read_text()
assert template.count("static 'monitor_app/prod_dashboard/epic-pillars.js'") == 1
assert template.count("static 'monitor_app/prod_dashboard/epic-pillar-model.js'") == 1
assert template.index('epic-pillar-model.js') < template.index('epic-pillars.js') < template.index('epic-dashboard-panels.js')
assert not re.search(r'\b(?:document|window|require|import)\b', model.read_text()), 'shared model must be independent of DOM and module loaders'
room_hash = root.parent / 'tjrepo/etaverse/scripts/audition/comp-room-pillar-model.sha256'
if room_hash.exists():
    assert room_hash.read_text().strip() == hashlib.sha256(model.read_bytes()).hexdigest(), 'room pillar-model pin must follow the canonical model'
for source in assets.glob('*.js'):
    if source == model:
        continue
    forbidden = r'function\s+(?:segments|createEpicPillarModel)\s*\('
    assert not re.search(forbidden, source.read_text()), f'pillar model copy in {source}'
    if source.name != 'epic-pillars.js':
        assert not re.search(r'function\s+(?:renderSitePillars|renderExecPillars)\s*\(', source.read_text()), f'pillar adapter copy in {source}'
print('dashboard drawing boundary: one document, one pillar implementation')
