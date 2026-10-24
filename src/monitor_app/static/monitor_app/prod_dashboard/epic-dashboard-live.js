(() => {
  const config = window.epicDashboard;
  const number = value => value == null ? '—' : Math.round(value).toLocaleString('en-US');
  const color = level => ({green: 'green', amber: 'gold', red: 'red', grey: 'muted', warning: 'gold', error: 'red', critical: 'red'}[String(level).toLowerCase()] || 'muted');
  const list = value => Array.isArray(value) ? value : value && value.items || [];
  const sumKnown = (rows, key) => !rows.length || rows.some(row => row[key] == null)
    ? null : rows.reduce((total, row) => total + row[key], 0);
  const et = (value, date = false, year = false) => {
    if (!value) return '';
    return new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York',
      ...(date ? {month: 'short', day: '2-digit', ...(year ? {year: 'numeric'} : {})} : {}),
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(new Date(value)) + ' ET';
  };
  function text(node, value) {
    if (!node) return;
    const anchor = node.querySelector(':scope > a.dashboard-drilldown');
    (anchor || node).textContent = value;
  }
  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }
  function anchor(href, className, value, title) {
    const node = element('a', className, value);
    node.href = href;
    node.target = '_blank'; node.rel = 'noopener';
    if (title) node.title = title;
    return node;
  }
  const panels = new Map(Array.from(document.querySelectorAll('.site')).map(panel =>
    [panel.dataset.siteKey || panel.querySelector('strong').textContent.trim(), panel]));
  const aliases = {osg: 'OSG', grex: 'GREX', nersc: 'Perlmutter', google: 'Google',
    'compute-canada': 'Compute Canada', jlab: 'JLab'};
  const executorModel = document.querySelector('.exec').cloneNode(true);

  function plot(container, columns, times) {
    if (!container) return;
    const svg = container.querySelector('svg');
    if (!svg) return;
    if (!times || !times.length) {
      svg.querySelectorAll('polyline,polygon').forEach(node => node.remove());
      text(container.querySelector('.chart-y-label'), '—');
      text(container.querySelector('.axis span:last-child'), '—');
      return;
    }
    const palette = container.closest('.site')
      ? ['#edf2f1', '#c4b6a2', '#7fce95', '#f47c70']
      : ['#7fce95'];
    const view = svg.viewBox.baseVal;
    const width = view.width, bottom = view.height - 4, height = view.height - 10;
    const peak = Math.max(1, ...columns.flat().filter(value => value != null));
    const order = 10 ** Math.floor(Math.log10(peak));
    const scale = [1, 2, 5, 10].map(n => n * order).find(n => n >= peak);
    svg.querySelectorAll('polyline,polygon').forEach(node => node.remove());
    columns.forEach((values, index) => {
      let segment = [];
      function flush() {
        if (!segment.length) return;
        if (index === 0) {
          const fill = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
          fill.setAttribute('points', segment[0][0] + ',' + bottom + ' ' +
            segment.map(p => p.join(',')).join(' ') + ' ' + segment[segment.length - 1][0] + ',' + bottom);
          fill.setAttribute('fill', palette[0] || '#7fce95');
          fill.setAttribute('opacity', '.23'); svg.appendChild(fill);
        }
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        line.setAttribute('points', segment.map(p => p.join(',')).join(' '));
        line.setAttribute('fill', 'none');
        line.setAttribute('stroke', palette[index]);
        line.setAttribute('stroke-width', '1.7');
        line.setAttribute('vector-effect', 'non-scaling-stroke');
        svg.appendChild(line); segment = [];
      }
      values.forEach((value, i) => {
        if (value == null) { flush(); return; }
        segment.push([width * i / Math.max(1, times.length - 1), bottom - height * value / scale]);
      });
      flush();
    });
    text(container.querySelector('.chart-y-label'), number(scale));
    text(container.querySelector('.axis span:last-child'), et(times[times.length - 1]));
  }

  function renderSite(site, trend) {
    const panel = panels.get(aliases[site.id] || site.name);
    if (!panel) return;
    const queues = site.queues || [];
    const sum = key => sumKnown(queues, key);
    const pilots = queues.map(queue => queue.pilots && queue.pilots.waiting);
    const waiting = pilots.some(value => value != null) ? pilots.reduce((n, value) => n + (value || 0), 0) : null;
    const values = [sum('running'), sum('finished_1h'), sum('failed_1h'), sum('queued'),
      waiting, sum('harvester_slots_opened_1h')];
    panel.querySelectorAll('.metric b').forEach((node, i) => text(node, number(values[i])));
    const dot = panel.querySelector('.panelhead > .dot');
    if (dot) dot.style.background = 'var(--' + color(site.health && site.health.level) + ')';
    const pillars = panel.querySelectorAll('.queuepillar.svgcolumn');
    const titles = [number(values[0]) + ' running; ' + number(values[1]) + ' finished, ' +
      number(values[2]) + ' failed in last 1h', number(values[3]) + ' queued',
      number(waiting) + ' pilots waiting', number(values[5]) + ' slots/1h'];
    pillars.forEach((node, i) => {
      node.title = titles[i];
      node.dataset.value = String([values[0], values[3], waiting, values[5]][i] || 0);
      node.style.visibility = [values[0], values[3], waiting, values[5]][i] == null ? 'hidden' : '';
      if (i === 0) {
        node.dataset.finished = String(values[1]);
        node.dataset.failed = String(values[2]);
      }
      if (node.firstElementChild && node.firstElementChild.tagName === 'A') node.firstElementChild.title = titles[i];
    });
    if (trend) plot(panel.querySelector('.plot'), [trend.running, trend.queued,
      trend.finished.map(n => n == null ? null : n * 4),
      trend.failed.map(n => n == null ? null : n * 4)], current.trends.t);
    else plot(panel.querySelector('.plot'), [], []);
  }

  function renderExecutors(sites) {
    const osg = sites.find(site => site.id === 'osg');
    const executors = (osg && osg.execute_sites || []).filter(item => item.running > 0);
    const strip = document.querySelector('.executeplot');
    const sample = strip.querySelector('.exec');
    const labelFont = sample ? getComputedStyle(sample.querySelector('.ename')).font : '8px Arial';
    const countFont = sample ? getComputedStyle(sample.querySelector('.count')).font : 'bold 9px Arial';
    const measure = document.createElement('canvas').getContext('2d');
    function width(name, running) {
      measure.font = labelFont;
      const label = measure.measureText(name).width;
      measure.font = countFont;
      return Math.max(16, label, measure.measureText(number(running)).width) + 4;
    }
    const prefixes = [0];
    executors.forEach(item => prefixes.push(Math.max(prefixes[prefixes.length - 1],
      width(item.short || item.name, item.running))));
    let remaining = executors.reduce((sum, item) => sum + item.running, 0);
    let visible = 0;
    executors.forEach((item, i) => {
      remaining -= item.running;
      const overflow = i + 1 < executors.length;
      const columns = i + 1 + (overflow ? 1 : 0);
      const columnWidth = Math.max(prefixes[i + 1], overflow ? width('Other', remaining) : 0);
      if (columns <= 15 && columns * columnWidth <= strip.clientWidth) visible = i + 1;
    });
    const shown = executors.slice(0, visible);
    const other = executors.slice(visible);
    if (other.length) shown.push({name: 'Other', short: 'Other',
      running: other.reduce((n, item) => n + item.running, 0),
      finished_1h: other.reduce((n, item) => n + item.finished_1h, 0),
      failed_1h: other.reduce((n, item) => n + item.failed_1h, 0),
      detail: other.map(item => item.name + ': ' + number(item.running)).join(', ')});
    const nodes = shown.map(item => {
      const node = executorModel.cloneNode(true);
      node.title = item.name + ': ' + number(item.running) + ' running, ' +
        number(item.finished_1h) + ' finished/1h, ' + number(item.failed_1h) + ' failed/1h' +
        (item.detail ? '\n' + item.detail : '');
      node.href = item.links && item.links.jobs || 'https://epic-devcloud.org/prod/snapper/epicprod/site/?site=BNL_OSG_PanDA_1&window=12h';
      text(node.querySelector('.count'), number(item.running));
      text(node.querySelector('.ename'), item.short || item.name);
      const pillar = node.querySelector('.cylinder');
      pillar.dataset.failureFraction = String(EpicPillars.failureFraction(item.finished_1h, item.failed_1h));
      pillar.style.setProperty('--top', item.failed_1h > 0 ? '#f47c70' : '#45b975');
      pillar.querySelector('.failurecap').style.display = item.failed_1h > 0 ? '' : 'none';
      return node;
    });
    strip.style.gridTemplateColumns = 'repeat(' + Math.max(1, nodes.length) + ',minmax(0,1fr))';
    strip.replaceChildren(...nodes);
    text(document.querySelector('.executeintro .large'), number(executors.length));
    renderExecPillars();
  }

  function renderTasks(record) {
    const rows = list(record.tasks).map(task => {
      const row = element('tr'); row.title = task.detail || '';
      const id = element('td'); id.appendChild(anchor(task.link, '', task.title || task.id));
      row.appendChild(id);
      const hours = task.at ? Math.floor((Date.now() - Date.parse(task.at)) / 3600000) : null;
      row.appendChild(element('td', 'muted', number(hours)));
      const counts = task.counts || {};
      ['running', 'queued', 'ungenerated', 'finished', 'failed'].forEach((key, i) => {
        const cell = element('td', ['', 'muted', 'muted', 'green', 'red'][i], counts[key] ? number(counts[key]) : '');
        if (key === 'queued') cell.style.color = 'var(--queued)';
        row.appendChild(cell);
      });
      return row;
    });
    document.querySelector('#tasks tbody').replaceChildren(...rows);
    const paused = list(record.paused).map(task => anchor(task.link, 'pausechip gold',
      task.title || task.id, task.detail));
    document.querySelector('#paused-top .paused-list').replaceChildren(...paused);
    fitPausedTasks();
    const completed = record.completed;
    const count = document.querySelector('#completed-tasks .completed-count');
    count.replaceChildren(completed && completed.link
      ? anchor(completed.link, '', number(completed.tasks), completed.detail)
      : document.createTextNode(number(completed && completed.tasks)));
    const finished = [...list(completed)].sort((a, b) => Number(b.id) - Number(a.id)).map(task => anchor(task.link,
      'completed-task ' + (task.status === 'done' ? 'green' : 'gold'), task.id, task.detail));
    document.querySelector('#completed-tasks .completed-list').replaceChildren(...(finished.length
      ? finished : [element('span', 'small muted', completed ? 'None' : 'Unavailable')]));
  }

  function renderStorage(record) {
    const science = (record.storage || []).find(row => row.id === 'jlab-rucio') || {};
    const logs = (record.storage || []).find(row => row.id === 'bnl-rucio') || {};
    const left = document.querySelector('#storage .storagemain > div:first-child');
    if (science) {
      const count = left.querySelector(':scope > b');
      text(count, number(science.arrived_1h));
      count.className = color(science.health && science.health.level);
      const volume = left.querySelector('.science-volume') || left.querySelector(':scope > div.small:not(.green):not(.muted)');
      volume.classList.add('science-volume');
      const amount = element('span', 'cyan', number(science.bytes_1h == null ? null : science.bytes_1h / 1e9) + ' GB/1h');
      volume.replaceChildren(amount, document.createTextNode(' · ' + number(science.events_1h) + ' events'));
      const incomplete = left.querySelector('.registration-incomplete');
      incomplete.className = 'small registration-incomplete ' +
        (science.failed_1h == null ? 'muted' : science.failed_1h > 0 ? 'gold' : 'green');
      incomplete.dataset.drilldown = science.links && science.links.failed || '';
      text(incomplete, number(science.failed_1h) + ' incomplete');
      if (science.flow) plot(document.querySelector('#storage .plot'),
        [science.flow.files.map(n => n == null ? null : n * 4)], record.trends && record.trends.t);
      else plot(document.querySelector('#storage .plot'), [], []);
      const capacity = science.capacity;
      if (capacity) {
        const detail = document.querySelector('.science-storage .small');
        const used = element('b', '', number(capacity.used_bytes == null ? null : capacity.used_bytes / 1e12));
        detail.replaceChildren(used, document.createTextNode(' / ' + number(capacity.limit_bytes == null ? null : capacity.limit_bytes / 1e12) +
          ' TB · ' + number(capacity.fraction == null ? null : capacity.fraction * 100) + '%'));
        const fill = document.querySelector('.science-storage .storefill i');
        fill.style.visibility = capacity.fraction == null ? 'hidden' : '';
        document.querySelector('.science-storage .storefill i').style.width =
          Math.min(100, Math.max(0, capacity.fraction * 100)) + '%';
      } else {
        text(document.querySelector('.science-storage .small'), '—');
        document.querySelector('.science-storage .storefill i').style.visibility = 'hidden';
      }
    }
    if (logs) {
      const count = left.querySelector('.logs b');
      text(count, number(logs.arrived_1h));
      count.className = color(logs.health && logs.health.level);
      const failures = left.querySelector('.log-stageout-failures');
      failures.className = 'small log-stageout-failures ' +
        (logs.failed_1h == null ? 'muted' : logs.failed_1h > 0 ? 'gold' : 'green');
      text(failures, number(logs.failed_1h) + ' stage-out failures');
    }
  }

  function renderReports(record) {
    const rows = list(record.live).slice(0, 5).map(item => {
      const row = element('div', 'boardrow');
      row.append(element('time', '', et(item.at).replace(' ET', '')),
        anchor(item.link, color(item.level), item.title, item.detail));
      return row;
    });
    document.querySelector('#events .board').replaceChildren(...rows);
    const issues = list(record.report).slice(0, 5).map(item => {
      const row = anchor(item.link || config.snapshotUrl, 'issue', undefined, item.text);
      const dot = element('span', 'dot'); dot.style.background = 'var(--' + color(item.level) + ')';
      row.append(dot, element('span', '', item.text)); return row;
    });
    const idle = document.querySelector('#report .idle');
    document.querySelector('#report .issues').replaceChildren(...issues, ...(idle ? [idle] : []));
    const assessment = record.report && record.report.assessment;
    if (assessment) {
      const fields = ['assessment-created', 'assessment-verdict', 'assessment-finished']
        .map(name => document.querySelector('#daily-assessment .' + name));
      const verdict = assessment.verdict || 'unknown';
      text(fields[1], verdict.charAt(0).toUpperCase() + verdict.slice(1));
      fields[1].className = 'assessment-verdict ' + color(assessment.level);
      const count = (assessment.narration || '').match(/finished\s+([\d,]+)\s+jobs/i);
      text(fields[2], count ? count[1] + ' jobs finished' : '');
      text(fields[0], et(assessment.at, true));
      const link = anchor(assessment.link, '', assessment.narration || '', assessment.narration);
      document.querySelector('#daily-assessment .assessment-text').replaceChildren(link);
    } else {
      const fields = ['assessment-created', 'assessment-verdict', 'assessment-finished']
        .map(name => document.querySelector('#daily-assessment .' + name));
      text(fields[1], 'Unavailable'); fields[1].className = 'assessment-verdict muted';
      [fields[0], fields[2], document.querySelector('#daily-assessment .assessment-text')]
        .forEach(node => text(node, ''));
    }
  }

  function renderCampaign(campaign) {
    campaign = campaign || {};
    const strip = document.getElementById('campaign-summary');
    text(strip.querySelector('.campaign-title'), 'Campaign ' + (campaign.id || '—'));
    let since = '—';
    if (campaign.delivered_since) {
      since = new Intl.DateTimeFormat('en-US', {month: 'short', day: 'numeric',
        timeZone: 'UTC'}).format(new Date(campaign.delivered_since + 'T00:00:00Z'));
    }
    text(strip.querySelector('.campaign-since'), 'delivered since ' + since);
    text(strip.querySelector('.campaign-events'), campaign.events == null ? '—' : number(campaign.events / 1e6) + 'M');
    text(strip.querySelector('.campaign-bytes'), campaign.bytes == null ? '—' : number(campaign.bytes / 1e12));
    text(strip.querySelector('.campaign-configs'), number(campaign.configurations) + ' physics configs');
    strip.dataset.campaign = campaign.id || '';
  }

  let current = null;
  function render(record) {
    if (!record || record.error) throw new Error(record && record.error || 'Production record missing');
    current = record;
    const sites = record.sites || [];
    const sum = key => sumKnown(sites.flatMap(site => site.queues || []), key);
    const storage = (record.storage || []).find(row => row.id === 'jlab-rucio') || {};
    const front = record.pressure_front || {};
    const values = [[front.ready_tasks, front.released_1h], [sum('running'), sum('finished_1h')],
      [sum('queued'), sum('ungenerated')], [storage.arrived_1h, storage.bytes_1h == null ? null : storage.bytes_1h / 1e9]];
    document.querySelectorAll('#flow > .flowcell').forEach((cell, i) => {
      if (values[i]) cell.querySelectorAll('.flowrow b').forEach((node, j) => text(node, number(values[i][j])));
    });
    sites.forEach(site => renderSite(site, record.trends && record.trends.sites.find(row => row.id === site.id)));
    panels.forEach((panel, name) => {
      if (panel.querySelector('.metric') && !sites.some(site => (aliases[site.id] || site.name) === name))
        renderSite({name, queues: [], health: {level: 'grey'}}, null);
    });
    renderCampaign(record.campaign);
    renderSitePillars();
    renderExecutors(sites); renderTasks(record); renderStorage(record); renderReports(record);
    text(document.getElementById('stamp'), et(record.observed_at, true, true));
    document.getElementById('stamp').classList.remove('red');
    document.body.classList.remove('record-error');
    linkDashboard();
  }
  function failed(error) {
    console.error('Production dashboard update failed:', error);
    const stamp = document.getElementById('stamp');
    stamp.classList.add('red'); stamp.title = error.message;
    if (!current) { document.body.classList.add('record-error'); stamp.textContent = 'Production record unavailable'; }
  }
  let loading = false;
  async function update() {
    if (loading) return;
    loading = true;
    try {
      const response = await fetch(config.snapshotUrl, {cache: 'no-store'});
      if (!response.ok) throw new Error('Production record: HTTP ' + response.status);
      render(await response.json());
    } catch (error) { failed(error); }
    finally { loading = false; }
  }
  // An operations display stays live when visible, including on an
  // unfocused screen. Poll the shared cache, never force a rebuild.
  function visible() { return !document.hidden; }
  const initial = document.getElementById('dashboard-record');
  if (initial) {
    try { render(JSON.parse(initial.textContent)); }
    catch (error) { failed(error); update(); }
  } else {
    document.body.classList.add('record-error');
    text(document.getElementById('stamp'), 'Loading production record…');
    update();
  }
  setInterval(() => { if (visible()) update(); }, 60000);
  addEventListener('focus', () => { if (visible()) update(); });
  addEventListener('pageshow', () => { if (visible()) update(); });
  document.addEventListener('visibilitychange', () => { if (visible()) update(); });
})();
