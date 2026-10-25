(() => {
  const config = window.epicDashboard;
  const column = document.getElementById('right-column');
  if (!config?.efficiencyUrl || !column) return;
  const panel = document.createElement('section');
  panel.id = 'compute-efficiency'; panel.className = 'panel';
  panel.innerHTML = '<div class="panelhead"><strong><a class="efficiency-link">Compute efficiency</a></strong></div>' +
    '<div class="efficiency-current"><a class="efficiency-link"><b>—</b></a><span>last 24 hours</span></div>' +
    '<a class="efficiency-chart efficiency-link" title="Monthly core-hour-weighted efficiency · fixed 0–100% scale"><svg viewBox="0 0 240 60" preserveAspectRatio="none" role="img" aria-label="Monthly compute efficiency since May"></svg></a>' +
    '<div class="efficiency-months"></div><span class="efficiency-updated" role="status">Loading…</span>';
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
    const value = panel.querySelector('.efficiency-current b');
    value.textContent = percent(current?.efficiency);
    value.title = current ? detail(current) + '\nWindow: ' + et(current.start) + ' – ' + et(current.end) +
      '\nSuccessful-job core-hours / all ended-job core-hours; running jobs and idle pilots excluded.' : 'Building last-24-hour reading';
    const cells = panel.querySelector('.efficiency-months');
    const chart = panel.querySelector('svg');
    const months = data.months || [];
    cells.style.setProperty('--months', Math.max(1, months.length));
    cells.replaceChildren(); chart.replaceChildren();
    [5, 30, 55].forEach(y => chart.append(svg('line', {x1: 0, x2: 240, y1: y, y2: y,
      stroke: '#334652', 'stroke-width': 1})));
    let segment = [];
    function line() {
      if (segment.length > 1) chart.append(svg('polyline', {points: segment.join(' '),
        fill: 'none', stroke: '#6fcad2', 'stroke-width': 1.8, 'vector-effect': 'non-scaling-stroke'}));
      segment = [];
    }
    months.forEach((row, i) => {
      const cell = document.createElement('a');
      cell.href = config.efficiencyHistoryUrl; cell.target = '_blank'; cell.rel = 'noopener';
      cell.title = row.period.slice(0, 7) + (row.partial ? ' · month to date' : '') + '\n' + detail(row);
      const label = document.createElement('span'); label.textContent = row.label + (row.partial ? '*' : '');
      const reading = document.createElement('b'); reading.textContent = percent(row.efficiency);
      cell.append(label, reading); cells.append(cell);
      if (row.efficiency == null) { line(); return; }
      const x = (i + .5) * 240 / months.length, y = 55 - row.efficiency / 2;
      segment.push(x + ',' + y);
      const dot = svg('circle', {cx: x, cy: y, r: 2.4, fill: row.partial ? '#e3b954' : '#6fcad2'});
      dot.setAttribute('title', cell.title);
      const title = svg('title', {}); title.textContent = cell.title;
      dot.append(title); chart.append(dot);
    });
    line();
    chart.setAttribute('aria-label', 'Monthly compute efficiency, 0–100%: ' + months.map(row =>
      row.label + (row.partial ? ' month to date ' : ' ') + percent(row.efficiency)).join('; '));
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
