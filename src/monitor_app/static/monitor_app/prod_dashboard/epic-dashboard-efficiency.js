(() => {
  const config = window.epicDashboard;
  const column = document.getElementById('right-column');
  if (!config?.efficiencyUrl || !column) return;
  const trendModel = createEpicTrendModel();
  const compact = value => value >= 1000 ? (value / 1000).toFixed(1) + 'k' : Math.round(value).toLocaleString('en-US');
  const metrics = [
    {key: 'efficiency', label: 'Efficiency', unit: '%', format: value => value.toFixed(1) + '%', ceiling: 100},
    {key: 'core_hours_per_day', label: 'Core-hours / day', unit: 'core-h/day', format: compact},
    {key: 'productive_cores', label: 'Productive avg cores', unit: 'cores', format: value => Math.round(value).toLocaleString('en-US')},
  ];
  const reading = (metric, row) => row?.[metric.key] == null ? '—' : metric.format(row[metric.key]);
  const panel = document.createElement('section');
  panel.id = 'compute-efficiency'; panel.className = 'panel';
  panel.innerHTML = '<div class="panelhead"><strong><a class="efficiency-link">Production compute</a></strong></div>' +
    '<div class="efficiency-legend"></div>' + metrics.map(metric =>
      '<section class="efficiency-metric" data-key="' + metric.key + '">' +
      '<div class="efficiency-title">' + metric.label + '</div>' +
      '<div class="efficiency-current"><a class="efficiency-link"><b>—</b></a><span>all sites · last 24h</span></div>' +
      '<a class="efficiency-chart efficiency-link"><svg viewBox="0 0 240 70" preserveAspectRatio="none" role="img"></svg></a>' +
      '<div class="efficiency-caption">All-site averages below · coloured curves by site</div><div class="efficiency-months"></div></section>'
    ).join('') + '<span class="efficiency-updated" role="status">Loading…</span>';
  column.appendChild(panel);
  panel.querySelectorAll('.efficiency-link').forEach(link => {
    link.href = config.efficiencyHistoryUrl; link.target = '_blank'; link.rel = 'noopener';
  });
  const stamp = panel.querySelector('.efficiency-updated');
  const percent = value => value == null ? '—' : value.toFixed(1) + '%';
  const hours = value => Math.round(value).toLocaleString('en-US');
  const et = value => new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York',
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'})
    .format(new Date(value)) + ' ET';
  const detail = row => percent(row.efficiency) + ' · ' + hours(row.finished) +
    ' successful / ' + hours(row.ended) + ' ended core-hours · ' + hours(row.jobs) +
    ' ended jobs' + (row.missing ? ' · ' + hours(row.missing) + ' jobs without valid timing' : '');
  const NS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }
  function render(data) {
    const current = data.current;
    const empty = {finished: 0, ended: 0, jobs: 0, missing: 0, days: 1,
      efficiency: null, core_hours_per_day: null, productive_cores: null};
    const today = row => ({...empty, ...row, period: 'last24h', label: '24h', partial: false, current: true});
    const months = [...(data.months || []), today(current)];
    const sites = (data.sites || []).map(site => ({...site,
      months: [...site.months, today(current?.sites?.[site.id])]}));
    panel.querySelector('.efficiency-legend').replaceChildren(...sites.map(site => {
      const label = document.createElement('span'); label.textContent = '━ ' + site.label;
      label.style.color = site.color;
      label.title = site.label + ' · last 24h\n' + metrics.map(metric =>
        metric.label + ': ' + reading(metric, current?.sites?.[site.id])).join('\n');
      return label;
    }));
    metrics.forEach(metric => {
      const block = panel.querySelector('[data-key="' + metric.key + '"]');
      const value = block.querySelector('.efficiency-current b');
      value.textContent = reading(metric, current);
      value.title = current ? detail(current) + '\nWindow: ' + et(current.start) + ' – ' + et(current.end) +
        '\nEnded-job walltime × cores; no processor-strength weights.' : 'Building last-24-hour reading';
      const cells = block.querySelector('.efficiency-months');
      const chart = block.querySelector('svg');
      cells.style.setProperty('--months', Math.max(1, months.length));
      cells.replaceChildren(); chart.replaceChildren();
      const model = trendModel.series(sites.map(site => ({key: site.id, color: site.color,
        values: site.months.map(row => row[metric.key])})), {count: months.length});
      const ceiling = metric.ceiling || model.ceiling;
      [12, 37, 62].forEach((position, index) => {
        chart.append(svg('line', {x1: 30, x2: 240, y1: position, y2: position,
          stroke: '#445864', 'stroke-width': 1}));
        const label = svg('text', {x: 27, y: position + 3.5, fill: '#c4d0d8',
          'font-size': 11, 'text-anchor': 'end'});
        label.textContent = metric.format(ceiling * (1 - index / 2)); chart.append(label);
      });
      const step = 210 / Math.max(1, months.length);
      const x = position => 30 + step / 2 + position * (210 - step);
      const y = fraction => 62 - fraction * 50;
      chart.append(svg('line', {x1: 240-step, x2: 240-step, y1: 7, y2: 67,
        stroke: '#6a7b86', 'stroke-width': 1, 'stroke-dasharray': '2 3'}));
      model.curves.forEach((curve, index) => {
        const site = sites[index];
        const group = svg('g', {class: 'efficiency-series'});
        curve.solid.forEach(path => {
          if (path.length < 2) return;
          group.append(svg('polyline', {points: path.map(point =>
            x(point[0]) + ',' + y(point[1] * model.ceiling / ceiling)).join(' '),
            fill: 'none', stroke: curve.color, 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke',
            title: site.label + '\n' + site.months.map(row => row.label + ': ' + reading(metric, row)).join('\n')}));
        });
        site.months.forEach((row, i) => {
          if (row[metric.key] == null) return;
          const title = site.label + ' · ' + (row.current ? 'last 24 hours' : row.period.slice(0, 7)) + (row.partial ? ' · month to date' : '') +
            '\n' + metric.label + ': ' + reading(metric, row) + '\n' + detail(row);
          group.append(svg('circle', {cx: 30 + (i + .5) * step, cy: y(row[metric.key] / ceiling),
            r: row.current ? 5 : 2.6, fill: curve.color,
            stroke: row.current ? '#edf2f1' : 'none', 'stroke-width': row.current ? 1 : 0,
            class: row.current ? 'efficiency-today' : 'efficiency-month', title}));
        });
        chart.append(group);
      });
      months.forEach(row => {
        const cell = document.createElement('a');
        cell.href = config.efficiencyHistoryUrl; cell.target = '_blank'; cell.rel = 'noopener';
        cell.title = 'All sites · ' + (row.current ? 'last 24 hours' : row.period.slice(0, 7)) + (row.partial ? ' · month to date' : '') +
          '\n' + detail(row) + '\nAveraged over ' + row.days.toFixed(2) + ' calendar days';
        const label = document.createElement('span'); label.textContent = row.label + (row.partial ? '*' : '');
        const amount = document.createElement('b'); amount.textContent = reading(metric, row);
        cell.append(label, amount); cells.append(cell);
      });
      chart.setAttribute('aria-label', metric.label + ' monthly since May: ' + sites.map(site =>
        site.label + ': ' + site.months.map(row => row.label + ' ' + reading(metric, row)).join(', ')).join('; '));
    });
    stamp.classList.remove('red');
    stamp.textContent = current ? 'As of ' + et(current.end) + ' · *month to date' : 'Building last-24-hour reading…';
    stamp.title = data.history_built_at ? 'Monthly history updated ' + et(data.history_built_at) : 'Monthly history is building';
  }
  let loading = false;
  async function update() {
    if (loading) return;
    loading = true;
    try {
      const response = await fetch(config.efficiencyUrl, {cache: 'no-store'});
      if (!response.ok) throw new Error('Efficiency: HTTP ' + response.status);
      const data = await response.json();
      if (data.error) throw new Error(data.error);
      render(data);
    } catch (error) {
      console.error('Dashboard efficiency update failed:', error);
      stamp.textContent = 'Update unavailable'; stamp.classList.add('red'); stamp.title = error.message;
    } finally { loading = false; }
  }
  window.epicDashboardEfficiency = {update};
  update();
})();
