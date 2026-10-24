#!/usr/bin/env python3
"""Architectural boundary: one dashboard document and shared drawing models."""
from pathlib import Path
import hashlib
import re

root = Path(__file__).resolve().parents[1]
assets = root / 'src/monitor_app/static/monitor_app/prod_dashboard'
model = assets / 'epic-pillar-model.js'
trend_model = assets / 'epic-trend-model.js'
for shared in (model, trend_model):
    assert shared.is_file(), f'canonical drawing module missing: {shared.name}'
    assert not re.search(r'\b(?:document|window|require|import)\b', shared.read_text()), 'shared model must be independent of DOM and module loaders'
for name in ('epic-dashboard-panels.js', 'epic-dashboard-links.js', 'epic-dashboard-weekly.js'):
    assert not (root / 'src/static' / name).exists(), f'duplicate dashboard source: {name}'
entry = (root / 'src/static/epic-dashboard-design.html').read_text()
assert "fetch(new URL('../panda/dashboard/'" in entry and 'document.write(html)' in entry, 'development URL must load the canonical dashboard'
assert '<svg' not in entry and entry.count('<script>') == 1 and len(entry) < 1500, 'development URL contains a second dashboard implementation'
template = (root / 'src/monitor_app/templates/monitor_app/prod_dashboard.html').read_text()
assert template.count("static 'monitor_app/prod_dashboard/epic-pillars.js'") == 1
assert template.count("static 'monitor_app/prod_dashboard/epic-pillar-model.js'") == 1
assert template.index('epic-pillar-model.js') < template.index('epic-pillars.js') < template.index('epic-dashboard-panels.js')
assert template.count("static 'monitor_app/prod_dashboard/epic-trend-model.js'") == 1
assert template.index('epic-trend-model.js') < template.index('epic-dashboard-live.js')
live = (assets / 'epic-dashboard-live.js').read_text()
assert 'createEpicTrendModel()' in live and 'trendModel.site(' in live, 'dashboard trends must consume the shared model'
assert 'share.notice' in live and 'notice.text' in live and 'notice.detail' in live, 'fair-share notice must come from the feed'
assert all('notice.' + field in live for field in ('text', 'level', 'detail', 'link')), 'fair-share adapter must use the complete notice'
assert 'queue.pilots.waiting_current' in live, 'waiting pilots must use the feed-resolved count'
assert not re.search(r"['\"](?:at fair share limit|fair share limited)['\"]", live), 'dashboard must not author a second fair-share notice'
room_hash = root.parent / 'tjrepo/etaverse/scripts/audition/comp-room-pillar-model.sha256'
if room_hash.exists():
    assert room_hash.read_text().strip() == hashlib.sha256(model.read_bytes()).hexdigest(), 'room pillar-model pin must follow the canonical model'
for source in assets.glob('*.js'):
    if source in (model, trend_model):
        continue
    forbidden = r'function\s+(?:segments|createEpicPillarModel|createEpicTrendModel)\s*\('
    assert not re.search(forbidden, source.read_text()), f'drawing model copy in {source}'
    if source.name != 'epic-pillars.js':
        assert not re.search(r'function\s+(?:renderSitePillars|renderExecPillars)\s*\(', source.read_text()), f'pillar adapter copy in {source}'
print('dashboard drawing boundary: one document, shared pillar and trend models')
