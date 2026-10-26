(function () {
  const sites = {
    OSG: ['BNL_OSG_PanDA_1', 'BNL_OSG_EPIC_PROD_1'],
    GREX: ['UM_GREX_PanDA_1'],
    Perlmutter: ['NERSC_Perlmutter_epic', 'NERSC_Perlmutter_epic_es'],
    Google: ['BNL_ePIC_GOOGLE', 'BNL_ePIC_GOOGLE_es'],
    'Compute Canada': ['ComputeCanada_PanDA_1'],
    JLab: ['JLAB_PanDA_1']
  };
  const hour = 3600000;
  const key = 'epic-dashboard-weekly-cores-v1';

  function calculate(series) {
    if (series.error) throw new Error(series.error);
    const start = Date.parse(series.requested_start);
    const end = Date.parse(series.requested_end);
    if (!(end > start)) throw new Error('Weekly history has no valid interval');
    const gaps = (series.gaps || []).map(g => [Date.parse(g[0]), Date.parse(g[1])]);
    function mean(queue) {
      const curve = (series.curves || {})['sjc_' + queue];
      if (!curve) return null;
      const points = curve.points.map(p => [Date.parse(p[0]), p[1]]);
      let area = 0, duration = 0;
      points.forEach((point, i) => {
        if (point[1] === null || !Number.isFinite(point[1])) return;
        const a = Math.max(start, point[0]);
        const b = Math.min(end, i + 1 < points.length ? points[i + 1][0] : end,
          point[0] + 15 * 60000);
        if (b <= a) return;
        let covered = b - a;
        gaps.forEach(g => { covered -= Math.max(0, Math.min(b, g[1]) - Math.max(a, g[0])); });
        covered = Math.max(0, covered);
        area += point[1] * covered;
        duration += covered;
      });
      return duration ? {value: area / duration, coverage: duration / (end - start)} : null;
    }
    const values = {};
    Object.entries(sites).forEach(([name, queues]) => {
      const means = queues.map(mean);
      values[name] = means.every(Boolean) ? {
        value: means.reduce((sum, m) => sum + m.value, 0),
        coverage: Math.min(...means.map(m => m.coverage))
      } : null;
    });
    return {at: Date.now(), start: series.requested_start, end: series.requested_end, values};
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {calculate};
    return;
  }
  const source = document.currentScript.src;
  function render(label) {
    const content = label.querySelector('a') || label;
    content.textContent = label.dataset.overrideText || label.dataset.weeklyText || '… avg cores/1wk';
    label.title = label.dataset.overrideDetail || label.dataset.weeklyTitle || '';
    label.style.color = label.dataset.overrideText ? 'var(--red)' : '';
  }
  window.epicDashboard.renderWeeklyLabel = render;
  function override(label, notice) {
    label.dataset.overrideText = notice ? notice.text : '';
    label.dataset.overrideDetail = notice ? notice.detail : '';
    render(label);
  }
  window.epicDashboard.setWeeklyOverride = override;
  let initial = {};
  try {
    initial = JSON.parse(document.getElementById('dashboard-record')?.textContent || '{}');
  } catch (error) { console.error('Initial dashboard record unavailable:', error); }
  const headers = new Map();
  document.querySelectorAll('.site .panelhead').forEach(header => {
    const name = header.closest('.site').dataset.siteKey || header.querySelector('strong').textContent.trim();
    if (!(name in sites) || header.querySelector('.site-status')) return;
    const label = document.createElement('span');
    label.className = 'weekly-cores right small';
    label.textContent = '… avg cores/1wk';
    header.appendChild(label);
    headers.set(name, label);
    const site = (initial.sites || []).find(site =>
      window.epicDashboard.sitePanels[site.id] === name || site.name === name);
    override(label, site && site.worker_limit && site.worker_limit.notice);
  });

  let current = null, pending = false;
  function display(record) {
    current = record;
    headers.forEach((label, name) => {
      const result = record.values[name];
      label.dataset.weeklyText = (result ? Math.round(result.value).toLocaleString('en-US') : '—') +
        ' avg cores/1wk';
      label.dataset.weeklyTitle = result ? 'Time-weighted average running cores over the last week; ' +
        Math.round(result.coverage * 100) + '% recorded coverage.' +
        (name === 'OSG' ? ' Includes production on the retired OSG queue.' : '') :
        'No recorded running-core history for this queue.';
      render(label);
    });
  }
  async function load() {
    if (pending || (current && Date.now() - current.at < hour)) return;
    pending = true;
    try {
      if (!current) {
        try {
          const stored = localStorage.getItem(key);
          if (stored) display(JSON.parse(stored));
        } catch (error) { console.warn('Weekly core cache unavailable:', error); }
        if (!current) {
          const seed = await fetch(window.epicDashboard.weeklyUrl);
          if (!seed.ok) throw new Error('Weekly core record: HTTP ' + seed.status);
          display(await seed.json());
        }
      }
      if (Date.now() - current.at < hour) return;
      const url = new URL(window.epicDashboard.historyUrl, document.baseURI);
      url.searchParams.set('focus', 'site');
      url.searchParams.set('window', '7d');
      url.searchParams.set('selection', Object.values(sites).flat()
        .filter(queue => queue !== 'ComputeCanada_PanDA_1').join(','));
      const response = await fetch(url);
      if (!response.ok) throw new Error('Weekly core history: HTTP ' + response.status);
      const record = calculate(await response.json());
      display(record);
      try { localStorage.setItem(key, JSON.stringify(record)); }
      catch (error) { console.warn('Weekly core cache write failed:', error); }
    } catch (error) {
      console.error('Weekly average running cores unavailable:', error);
      headers.forEach(label => {
        label.dataset.weeklyTitle = error.message;
        render(label);
      });
    } finally { pending = false; }
  }
  function attend() {
    if (!document.hidden && document.hasFocus()) load();
  }
  if ('requestIdleCallback' in window) requestIdleCallback(load, {timeout: 3000});
  else setTimeout(load, 250);
  setInterval(attend, hour);
  addEventListener('focus', attend);
  document.addEventListener('visibilitychange', attend);
})();
