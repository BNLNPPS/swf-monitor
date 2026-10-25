(() => {
  const config = window.epicDashboard;
  const resources = document.getElementById('task-resources');
  if (!config?.productiveCoresUrl || !resources) return;
  const trendModel = createEpicTrendModel();
  const panel = document.createElement('section');
  panel.id = 'productive-cores'; panel.className = 'panel';
  panel.innerHTML = '<div class="panelhead"><strong>Productive average cores · daily</strong>' +
    '<span class="right small productive-range" role="status">Loading…</span></div>' +
    '<div class="productive-legend"></div>' +
    '<svg class="productive-main-chart" viewBox="0 0 650 230" role="img" aria-label="Daily productive average cores by production site"></svg>' +
    '<div class="ready-queue"><div class="ready-queue-heading"><strong>Pressure front ready tasks</strong>' +
    '<span class="right small ready-queue-status"></span></div>' +
    '<svg viewBox="0 0 650 88" role="img" aria-label="Daily average production ready queue"></svg></div>';
  resources.before(panel);
  const chart = panel.querySelector('.productive-main-chart');
  const readyChart = panel.querySelector('.ready-queue svg');
  const stamp = panel.querySelector('.productive-range');
  const NS = 'http://www.w3.org/2000/svg';
  const number = n => Math.round(n).toLocaleString('en-US');
  function svg(tag, attrs, content) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    if (content != null) node.textContent = content;
    return node;
  }
  const dateLabel = value => new Intl.DateTimeFormat('en-US', {
    timeZone: 'UTC', month: 'short', day: 'numeric',
  }).format(new Date(value + 'T00:00:00Z'));
  const x = fraction => 49 + fraction * 589;
  function timeAxis(target, days, baseline, labelY) {
    const tickCount = 6;
    for (let i = 0; i <= tickCount; i += 1) {
      const index = Math.round(i * (days.length - 1) / tickCount);
      const fraction = index / Math.max(1, days.length - 1);
      target.append(svg('line', {x1: x(fraction), x2: x(fraction), y1: baseline, y2: baseline + 5,
        stroke: '#9cabb5', 'stroke-width': 1}));
      target.append(svg('text', {x: x(fraction), y: labelY, fill: '#c4d0d8', 'font-size': 12,
        'text-anchor': i === 0 ? 'start' : i === tickCount ? 'end' : 'middle'}, dateLabel(days[index].day)));
    }
  }
  function renderReady(days) {
    const first = days.findIndex(day => day.ready_tasks > 0);
    const values = days.map((day, index) => first < 0 || index < first ? null : day.ready_tasks);
    const model = trendModel.series([{key: 'ready', color: '#6fcad2', values}], {count: days.length});
    const ceiling = Math.max(2, 2 * Math.ceil(model.ceiling / 2));
    const y = fraction => 61 - fraction * 53;
    readyChart.replaceChildren();
    const amount = n => n.toLocaleString('en-US', {maximumFractionDigits: 1});
    [0, .5, 1].forEach(fraction => {
      readyChart.append(svg('line', {x1: x(0), x2: x(1), y1: y(fraction), y2: y(fraction),
        stroke: '#344651', 'stroke-width': 1}));
      readyChart.append(svg('text', {x: 42, y: y(fraction) + 4, fill: '#c4d0d8',
        'font-size': 12, 'text-anchor': 'end'}, number(ceiling * fraction)));
    });
    model.curves[0].solid.forEach(path => {
      const points = path.map(point => x(point[0]) + ',' + y(point[1] * model.ceiling / ceiling));
      readyChart.append(svg('polygon', {points: x(path[0][0]) + ',61 ' + points.join(' ') + ' ' +
        x(path[path.length - 1][0]) + ',61', fill: '#6fcad2', 'fill-opacity': .15}));
      readyChart.append(svg('polyline', {points: points.join(' '), fill: 'none', stroke: '#6fcad2',
        'stroke-width': 1.7, 'vector-effect': 'non-scaling-stroke'}));
    });
    const width = 589 / Math.max(1, days.length - 1);
    days.forEach((day, index) => {
      if (values[index] == null) return;
      const position = index / Math.max(1, days.length - 1);
      readyChart.append(svg('circle', {cx: x(position), cy: y(values[index] / ceiling),
        r: 1.8, fill: '#6fcad2'}));
      readyChart.append(svg('rect', {x: Math.max(49, x(position) - width / 2), y: 5,
        width: index === 0 || index === days.length - 1 ? width / 2 : width, height: 57,
        fill: 'transparent', class: 'productive-day',
        title: day.day + ' UTC\nDaily average ready tasks: ' + amount(values[index]) +
          '\nTime-weighted recorded front depth\nCoverage: ' +
          amount(day.ready_coverage_hours || 0) + ' of 24 hours'}));
    });
    timeAxis(readyChart, days, 61, 82);
    panel.querySelector('.ready-queue-status').textContent = first < 0
      ? 'No nonzero recorded days' : 'Since ' + dateLabel(days[first].day);
  }
  function render(data) {
    const days = data.daily;
    if (!days?.length) throw new Error('Daily production history has no completed days');
    const sites = data.sites;
    const sums = sites.map((site, index) => days.map(day => day.total == null ? null :
      sites.slice(0, index + 1).reduce((sum, member) => sum + day.sites[member.id].cores, 0)));
    const model = trendModel.series(sites.map((site, i) => ({key: site.id,
      color: site.color, values: sums[i]})), {count: days.length});
    const y = fraction => 200 - fraction * 185;
    chart.replaceChildren();
    for (let i = 0; i <= 4; i += 1) {
      const fraction = i / 4;
      chart.append(svg('line', {x1: x(0), x2: x(1), y1: y(fraction), y2: y(fraction),
        stroke: '#344651', 'stroke-width': 1}));
      chart.append(svg('text', {x: 42, y: y(fraction) + 4, fill: '#c4d0d8',
        'font-size': 12, 'text-anchor': 'end'}, number(model.ceiling * fraction)));
    }
    model.curves.forEach((curve, index) => {
      curve.solid.forEach(path => {
        if (path.length < 2) return;
        const lower = path.map(point => {
          const dayIndex = Math.round(point[0] * (days.length - 1));
          return [point[0], index ? sums[index - 1][dayIndex] / model.ceiling : 0];
        }).reverse();
        chart.append(svg('polygon', {points: [...path, ...lower].map(point =>
          x(point[0]) + ',' + y(point[1])).join(' '), fill: curve.color, 'fill-opacity': .8}));
      });
    });
    const total = model.curves[model.curves.length - 1];
    total.solid.forEach(path => chart.append(svg('polyline', {points: path.map(point =>
      x(point[0]) + ',' + y(point[1])).join(' '), fill: 'none', stroke: '#edf2f1',
      'stroke-width': 2, 'vector-effect': 'non-scaling-stroke'})));
    timeAxis(chart, days, 200, 221);
    renderReady(days);
    const width = 589 / Math.max(1, days.length - 1);
    days.forEach((day, index) => {
      const title = day.day + ' UTC · production only\nTotal: ' +
        (day.total == null ? 'incomplete timing coverage' : number(day.total) + ' average cores') + '\n' +
        sites.map(site => {
          const row = day.sites[site.id];
          return site.label + ': ' + (row.cores == null ? 'incomplete' : number(row.cores) + ' cores') +
            ' · ' + number(row.core_hours) + ' successful core-hours' +
            (row.missing ? ' · ' + row.missing + ' jobs without valid timing' : '');
        }).join('\n') + '\nSuccessful core-hours / 24 elapsed hours; running jobs excluded.';
      chart.append(svg('rect', {x: Math.max(49, x(index / Math.max(1, days.length - 1)) - width / 2),
        y: 10, width: index === 0 || index === days.length - 1 ? width / 2 : width,
        height: 191, fill: 'transparent', class: 'productive-day', title}));
    });
    panel.querySelector('.productive-legend').replaceChildren(...[...sites,
      {label: 'Total', color: '#edf2f1'}].map(site => {
      const label = document.createElement('span');
      label.textContent = (site.label === 'Total' ? '━ ' : '■ ') + site.label;
      label.style.color = site.color; return label;
    }));
    stamp.textContent = dateLabel(data.start) + ' – ' + dateLabel(data.end);
    stamp.classList.remove('red');
    stamp.title = days.length + ' completed UTC days · production only\n' +
      'Snapper daily production record · updated ' + data.through;
    chart.setAttribute('aria-label', days.length + ' daily production measurements: stacked OSG, GREX and Perlmutter average cores; white total.');
  }
  let loading = false, retry;
  async function update() {
    if (loading) return;
    loading = true;
    clearTimeout(retry);
    try {
      const response = await fetch(config.productiveCoresUrl, {cache: 'no-store'});
      if (!response.ok) throw new Error('Daily cores: HTTP ' + response.status);
      const data = await response.json();
      if (data.error) throw new Error(data.error);
      if (data.record) render(data.record);
      if (data.refreshing || !data.record) retry = setTimeout(update, 5000);
    } catch (error) {
      console.error('Daily productive cores update failed:', error);
      stamp.textContent = 'Update unavailable'; stamp.classList.add('red'); stamp.title = error.message;
    } finally { loading = false; }
  }
  window.epicDashboardProductiveCores = {update};
  update();
  setInterval(update, 300000);
})();
