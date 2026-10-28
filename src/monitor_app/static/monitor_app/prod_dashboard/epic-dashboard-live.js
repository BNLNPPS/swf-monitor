(() => {
  const config = window.epicDashboard;
  const number = value => value == null ? '—' : Math.round(value).toLocaleString('en-US');
  const color = level => ({green: 'green', blue: 'blue', amber: 'gold', red: 'red', grey: 'muted', warning: 'gold', error: 'red', critical: 'red'}[String(level).toLowerCase()] || 'muted');
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
  const aliases = config.sitePanels;
  const executorModel = document.querySelector('.exec').cloneNode(true);
  const trendModel = createEpicTrendModel();
  const productionModel = panels.get(aliases.grex);

  function productionPanel(panel, site) {
    if (panel.querySelector('.queuebody') || !site.production_panel) return;
    const body = panel.querySelector('.site-body');
    if (!body || !productionModel) return;
    const content = productionModel.querySelector('.queuebody').cloneNode(true);
    content.querySelectorAll('linearGradient[id]').forEach(gradient => {
      const previous = gradient.id;
      gradient.id = site.id + '-' + previous;
      content.querySelectorAll('[fill]').forEach(node => {
        if (node.getAttribute('fill') === 'url(#' + previous + ')')
          node.setAttribute('fill', 'url(#' + gradient.id + ')');
      });
    });
    body.replaceWith(content);
    const heading = panel.querySelector('.panelhead');
    if (!heading.querySelector(':scope > .dot'))
      heading.insertBefore(productionModel.querySelector('.panelhead > .dot').cloneNode(true),
        heading.querySelector(':scope > .site-title') || heading.querySelector('strong'));
    heading.querySelector('strong').style.fontSize = getComputedStyle(productionModel.querySelector('.panelhead strong')).fontSize;
  }

  function plot(container, model, times, options = {}) {
    if (!container) return;
    const legendKeys = {running: 'running', cores: 'running', 'finish/1h': 'finished',
      'fail/1h': 'failed', queued: 'queued'};
    container.querySelectorAll('.plotkey span').forEach(label => {
      const marker = label.querySelector('i');
      if (!marker) return;
      const key = legendKeys[label.textContent.trim()];
      if (!key) throw new Error('Unknown trend legend: ' + label.textContent);
      if (key === 'running' && label.textContent.trim() !== 'cores')
        label.replaceChildren(marker, document.createTextNode('cores'));
      marker.style.background = trendModel.palette[key];
    });
    const svg = container.querySelector('svg');
    if (!svg) return;
    svg.querySelectorAll('.trend-layer,polyline,polygon').forEach(node => node.remove());
    if (!times || !times.length || !model || !model.hasData) {
      text(container.querySelector('.chart-y-label'), '—');
      text(container.querySelector('.axis span:last-child'), '—');
      return;
    }
    const view = svg.viewBox.baseVal;
    const width = view.width, bottom = view.height - 4, top = 18, height = bottom - top;
    // The shared model spaces samples evenly; ``positions`` places them by
    // time (a live last point closer than a whole bin), outside the model.
    const at = options.positions;
    const place = at && at.length > 1 ? x => {
      const n = at.length - 1, f = x * n, i = Math.min(n - 1, Math.floor(f));
      return at[i] + (at[i + 1] - at[i]) * (f - i);
    } : x => x;
    const ceiling = options.ceiling || model.ceiling;
    const grid = Array.from(svg.querySelectorAll(':scope > line'));
    grid.forEach((line, i) => {
      const y = top + height * (i + 1) / (grid.length + 1);
      line.setAttribute('y1', y); line.setAttribute('y2', y);
    });
    const layer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    layer.classList.add('trend-layer'); svg.appendChild(layer);
    let clip;
    if (options.ceiling) {
      clip = svg.querySelector('clipPath');
      if (!clip) {
        const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
        clip = document.createElementNS('http://www.w3.org/2000/svg', 'clipPath');
        clip.id = 'resource-' + container.dataset.metric + '-clip';
        clip.setAttribute('clipPathUnits', 'userSpaceOnUse');
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', '0'); rect.setAttribute('y', top);
        rect.setAttribute('width', width); rect.setAttribute('height', height);
        clip.appendChild(rect); defs.appendChild(clip); svg.prepend(defs);
      }
    }
    model.curves.forEach((curve, index) => {
      const detail = options.curves?.[curve.key];
      const group = document.createElementNS('http://www.w3.org/2000/svg', detail ? 'a' : 'g');
      if (detail) {
        group.setAttribute('href', detail.href); group.setAttribute('target', '_blank');
        group.setAttribute('rel', 'noopener');
        group.setAttribute('aria-label', detail.title);
      }
      layer.appendChild(group);
      const points = segment => segment.map(([x, y]) => [width * place(x), bottom - height * y * model.ceiling / ceiling]);
      curve.solid.forEach(path => {
        const segment = points(path);
        if (index === 0 && options.fill !== false) {
          const fill = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
          fill.setAttribute('points', segment[0][0] + ',' + bottom + ' ' +
            segment.map(p => p.join(',')).join(' ') + ' ' + segment[segment.length - 1][0] + ',' + bottom);
          fill.setAttribute('fill', curve.color);
          fill.setAttribute('opacity', '.23'); group.appendChild(fill);
        }
      });
      function draw(path, overflow) {
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        line.setAttribute('points', points(path).map(p => p.join(',')).join(' '));
        line.setAttribute('fill', 'none');
        line.setAttribute('stroke', curve.color);
        line.setAttribute('stroke-width', '1.7');
        line.setAttribute('vector-effect', 'non-scaling-stroke');
        if (detail) line.setAttribute('title', detail.title);
        if (overflow) line.setAttribute('stroke-dasharray', '4 3');
        if (clip) line.setAttribute('clip-path', 'url(#' + clip.id + ')');
        group.appendChild(line);
      }
      curve.solid.forEach(path => draw(path, false));
      curve.overflow.forEach(path => draw(path, true));
      if (detail) detail.values.forEach((value, i) => {
        if (value == null) return;
        const point = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        point.setAttribute('cx', width * i / Math.max(1, times.length - 1));
        point.setAttribute('cy', bottom - height * Math.min(value, ceiling) / ceiling);
        point.setAttribute('r', value > ceiling ? '3' : '2.1'); point.setAttribute('fill', curve.color);
        if (value > ceiling) { point.setAttribute('stroke', '#edf2f1'); point.setAttribute('stroke-width', '.8'); }
        point.setAttribute('title', detail.pointTitle(i)); group.appendChild(point);
      });
    });
    text(container.querySelector('.chart-y-label'), (options.format || number)(ceiling));
    text(container.querySelector('.axis span:last-child'), et(times[times.length - 1]));
    // The last whole quarter-hour, at the scale number's level and font on
    // the right; the live end is not counted (Torre, 2026-10-28).
    if (options.updated) {
      let stamp = container.querySelector('.chart-updated');
      if (!stamp) {
        stamp = element('span', 'chart-y-label chart-updated');
        container.querySelector('.chart-y-label')?.after(stamp);
      }
      text(stamp, et(options.updated));
      stamp.title = 'History last updated at ' + et(options.updated) + '; the right end is live';
    }
  }

  const resourcePanel = element('section', 'panel');
  resourcePanel.id = 'task-resources';
  const resourceHead = element('div', 'panelhead');
  resourceHead.append(element('strong', '', 'Production resource peaks · 12h'));
  const resourceStamp = element('span', 'right small', 'Loading…');
  resourceHead.append(resourceStamp); resourcePanel.append(resourceHead);
  const resourceCharts = element('div', 'resource-charts');
  const resourceMetrics = [
    {key: 'memory', label: 'Peak memory', unit: 'GiB'},
    {key: 'disk', label: 'Peak scratch disk', unit: 'GiB'},
    {key: 'files', label: 'Peak scratch files', unit: 'files'},
  ];
  resourceMetrics.forEach(metric => {
    const chart = element('div', 'plot resource-plot'); chart.dataset.metric = metric.key;
    chart.append(element('div', 'resource-title', metric.label + ' · ' + metric.unit));
    const outliers = element('span', 'resource-outliers'); outliers.hidden = true; chart.append(outliers);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 200 82'); svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', metric.label + ' by task over twelve hours');
    for (let i = 0; i < 3; i += 1) {
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', '0'); line.setAttribute('x2', '200');
      line.setAttribute('stroke', '#34434e'); svg.appendChild(line);
    }
    chart.append(svg, element('span', 'chart-y-label', '—'));
    const axis = element('div', 'axis'); axis.append(element('span', '', '12h ago'), element('span', '', '—'));
    chart.append(axis, element('div', 'resource-coverage', 'Waiting for reported peaks'));
    resourceCharts.append(chart);
  });
  resourcePanel.append(resourceCharts);
  const resourceNote = element('div', 'resource-note', 'One colour per task · hover a curve or point for details · click for task');
  resourcePanel.append(resourceNote);
  document.querySelector('.bottomleft').append(resourcePanel);
  let resourceLoading = false;
  const resourceNumber = (value, metric) => value == null ? '—' : Number(value).toLocaleString('en-US', {
    maximumFractionDigits: metric === 'files' ? 0 : 2,
  });
  function renderResources(record, builtAt) {
    const jobs = record.tasks.reduce((total, task) => total + task.jobs.reduce((a, b) => a + b, 0), 0);
    resourceStamp.textContent = et(builtAt); resourceStamp.classList.remove('red');
    resourceStamp.title = record.basis;
    resourceMetrics.forEach(metric => {
      const chart = resourceCharts.querySelector('[data-metric="' + metric.key + '"]');
      const curves = {}, columns = [];
      let coverage = 0;
      record.tasks.forEach(task => {
        coverage += task.coverage[metric.key];
        if (!task.coverage[metric.key]) return;
        const key = String(task.id), values = task[metric.key];
        const peak = Math.max(...values.filter(value => value != null));
        columns.push({key, values, color: 'hsl(' + (task.id * 137.508 % 360) + ' 75% 70%)'});
        curves[key] = {
          href: config.taskUrl.replace(/\/0\/$/, '/' + task.id + '/'), values,
          title: task.name + '\nTask ' + task.id + ' · ' + metric.label + ': ' + resourceNumber(peak, metric.key) + ' ' + metric.unit + '\n' + number(task.coverage[metric.key]) + ' reported jobs in 12h',
          pointTitle: i => {
            const bin = new Date(record.times[i]).getTime();
            const start = new Date(Math.max(bin, new Date(record.from).getTime()));
            const end = new Date(Math.min(bin + record.bin_minutes * 60000, new Date(record.through).getTime()));
            return task.name + '\nTask ' + task.id + ' · ' + et(start, true) + '–' + et(end) +
              '\n' + metric.label + ': ' + resourceNumber(values[i], metric.key) + ' ' + metric.unit +
              '\n' + number(task.samples[metric.key][i]) + ' of ' + number(task.jobs[i]) + ' jobs reported this metric';
          },
        };
      });
      const ceiling = trendModel.ceilingForPeak(record.scale_peaks[metric.key]);
      const extremes = [];
      record.tasks.forEach(task => task[metric.key].forEach((value, i) => {
        if (value != null && value > ceiling) extremes.push({task, value, time: record.times[i]});
      }));
      extremes.sort((a, b) => b.value - a.value);
      const outlierLabel = chart.querySelector('.resource-outliers');
      outlierLabel.hidden = !extremes.length;
      if (extremes.length) {
        outlierLabel.textContent = '↑ ' + resourceNumber(extremes[0].value, metric.key) + ' ' + metric.unit + ' · ' + number(extremes.length);
        outlierLabel.title = 'Above the plotted ' + resourceNumber(ceiling, metric.key) + ' ' + metric.unit + ' range: ' + number(extremes.length) + ' points.\n' + extremes.map(peak =>
          'Task ' + peak.task.id + ' · ' + resourceNumber(peak.value, metric.key) + ' ' + metric.unit + ' · ' + et(peak.time, true) + '\n' + peak.task.name).join('\n');
      }
      plot(chart, trendModel.series(columns, {count: record.times.length}), record.times,
        {fill: false, ceiling, curves, format: value => resourceNumber(value, metric.key)});
      let empty = chart.querySelector('.resource-empty');
      if (!coverage && !empty) { empty = element('div', 'resource-empty'); chart.append(empty); }
      if (coverage && empty) empty.remove();
      else if (empty) empty.textContent = jobs ? 'No reported peaks' : 'No jobs observed';
      chart.querySelector('.resource-coverage').textContent = number(coverage) + ' / ' + number(jobs) + ' jobs';
      chart.querySelector('.resource-coverage').title = record.basis + '\n' + metric.label + ' coverage: ' + number(coverage) + ' of ' + number(jobs) + ' jobs. Missing measurements are gaps.';
      chart.querySelector('svg').setAttribute('aria-label', metric.label + ' · ' + number(coverage) + ' of ' + number(jobs) + ' jobs reported · ' + (coverage ? columns.length + ' task curves' : 'no reported peaks'));
    });
  }
  async function updateResources() {
    if (!config.resourcesUrl || resourceLoading) return;
    resourceLoading = true;
    try {
      const response = await fetch(config.resourcesUrl, {cache: 'no-store'});
      if (!response.ok) throw new Error('Task resources: HTTP ' + response.status);
      const data = await response.json();
      if (data.error) throw new Error(data.error);
      if (!data.record) { resourceStamp.textContent = 'Building…'; return; }
      renderResources(data.record, data.built_at);
    } catch (error) {
      console.error('Task resource history update failed:', error);
      resourceStamp.textContent = 'Unavailable'; resourceStamp.classList.add('red'); resourceStamp.title = error.message;
    } finally { resourceLoading = false; }
  }

  // TASKS, centred under a site's name: Prod tasks in flight for the
  // allocation, what has been sent to it and not ended (Torre, 2026-10-27).
  function siteTasks(panel, site, queues) {
    const name = panel.querySelector('.panelhead strong');
    if (!name || !queues.length) return;
    let title = name.parentElement.classList.contains('site-title') ? name.parentElement : null;
    if (!title) {
      title = element('span', 'site-title');
      name.replaceWith(title);
      title.append(name, element('a', 'site-tasks', 'TASKS'));
    }
    const ids = queues.map(queue => queue.id);
    const link = title.querySelector('.site-tasks');
    link.href = new URL('pcs/in-flight/?' + new URLSearchParams({site: site.name}),
      'https://epic-devcloud.org/prod/').href;
    link.target = '_blank'; link.rel = 'noopener';
    link.title = 'Tasks sent to ' + site.name + ' (' + ids.join(', ') + ')';
  }

  // A site's Production now lines by number at its title row's upper
  // right, each in its line's colour, its text on hover and its link: a
  // grid of small numbers filling columns, its type shrinking until every
  // number fits beside the title (Torre, 2026-10-28).
  function renderNotices(panel, notices) {
    const heading = panel.querySelector('.panelhead');
    if (!heading) return;
    let box = heading.querySelector(':scope > .site-notices');
    if (!notices.length) { if (box) box.remove(); return; }
    if (!box) {
      box = element('span', 'site-notices');
      const next = heading.querySelector(':scope > .weekly-cores, :scope > .site-node-cap, :scope > .site-status');
      if (next) next.before(box); else heading.append(box);
    }
    box.replaceChildren(...notices.map(item => {
      const node = anchor(item.link || config.snapshotUrl, 'site-notice', String(item.n), item.n + '. ' + item.text);
      node.style.color = 'var(--' + color(item.level) + ')';
      return node;
    }));
    fitNotices(box, heading, notices.length);
  }
  function fitNotices(box, heading, count) {
    const others = [...heading.children].filter(node => node !== box && node.offsetWidth > 0);
    const gap = parseFloat(getComputedStyle(heading).columnGap) || 6;
    const room = Math.max(12, heading.clientWidth - others.reduce((w, node) => w + node.offsetWidth, 0)
      - gap * (others.length + 1) - 20);
    const height = Math.max(8, heading.clientHeight - 4);
    for (let size = 12; size >= 5; size -= 0.5) {
      const line = Math.ceil(size * 1.05);
      const rows = Math.max(1, Math.min(count, Math.floor(height / line)));
      box.style.fontSize = size + 'px';
      box.style.gridTemplateRows = 'repeat(' + rows + ', ' + line + 'px)';
      if (box.offsetWidth <= room) break;
    }
  }

  function renderSite(site, trend) {
    const panel = panels.get(aliases[site.id] || site.name);
    if (!panel) return;
    panel.dataset.siteKey = aliases[site.id] || site.name;
    text(panel.querySelector('.panelhead strong'), site.name);
    const nodeCap = site.node_cap ?? config.siteNodeCaps?.[panel.dataset.siteKey];
    if (nodeCap != null) {
      const heading = panel.querySelector('.panelhead');
      let label = heading.querySelector('.site-node-cap');
      if (!label) {
        label = element('span', 'site-node-cap right small'); heading.append(label);
      }
      label.textContent = nodeCap + (Number(nodeCap) === 1 ? ' node' : ' nodes') +
        (site.cores_per_node ? ' × ' + site.cores_per_node + ' cores' : '');
    }
    productionPanel(panel, site);
    const active = (site.queues || []).filter(q => q.production_display || q.status === 'live');
    const queues = active.length ? active : site.queues || [];
    siteTasks(panel, site, queues);
    const sum = key => sumKnown(queues, key);
    const pilots = queues.map(queue => queue.pilots && queue.pilots.waiting_current);
    const waiting = pilots.some(value => value != null) ? pilots.reduce((n, value) => n + (value || 0), 0) : null;
    const values = [sum('processing_threads'), sum('finished_cores_1h'), sum('failed_cores_1h'), sum('queued_cores'),
      waiting, sum('harvester_slots_opened_1h')];
    const showPilots = site.id !== 'google';
    const pilotMetric = panel.querySelector('.pilotmetric');
    if (pilotMetric) pilotMetric.style.display = showPilots ? '' : 'none';
    const partialThreads = values[0] == null && sum('processing_known_threads') > 0;
    if (partialThreads) values[0] = sum('processing_known_threads');
    // An Event Service site's work arrives close by close and its jobs finish
    // once in hours, so its second number is the last hour's events, in
    // millions to one decimal, from the closes that stood (PROD_DASHBOARD.md).
    const eventService = queues.length > 0 && queues.every(q => q.event_service);
    const millions = value => value == null ? '—' : (value / 1e6).toLocaleString('en-US',
      {minimumFractionDigits: 1, maximumFractionDigits: 1});
    panel.querySelectorAll('.metric b').forEach((node, i) =>
      text(node, eventService && i === 1 ? millions(sum('events_1h')) : number(values[i])));
    const metrics = panel.querySelectorAll('.metric');
    if (metrics[1]) {
      const unit = metrics[1].querySelector('span');
      if (unit) unit.textContent = eventService ? 'Mevt/1h' : 'finish/1h';
      const unknown = sum('events_unknown_jobs');
      metrics[1].title = eventService
        ? number(sum('events_1h')) + ' events in Event Service closes that stood in the last hour' +
          (unknown ? '; ' + number(unknown) + ' jobs without a record yet' : '')
        : number(values[1]) + ' core completions from ' + number(sum('finished_jobs_1h')) + ' jobs in the last hour';
    }
    if (metrics[2]) metrics[2].title = number(values[2]) + ' failed cores from ' +
      number(sum('failed_jobs_1h')) + ' jobs in the last hour';
    if (metrics[3]) metrics[3].title = number(values[3]) + ' queued cores · ' +
      number(sum('queued_jobs')) + ' jobs waiting' +
      (site.cores_per_node ? '\nReady-to-fill core capacity at the current node cap; ' +
        site.cores_per_node + ' cores per node' : '');
    const runningMetric = panel.querySelector('.metric');
    if (runningMetric) {
      runningMetric.querySelector('span').textContent = 'cores';
      runningMetric.title = 'Cores carrying payload work: native chosen thread width; Event Service lanes with assigned work.\n' +
        'Idle Event Service lanes are excluded. Unit startup/finalization is included; this is not CPU utilization.\n' +
        'Unreported multicore native widths or stale Event Service reports remain unknown.' +
        (partialThreads ? '\nLower bound: some jobs have no fresh thread measurement.' : '');
    }
    // The bullet is the worst of the site's health, status and lines.
    const dot = panel.querySelector('.panelhead > .dot');
    if (dot) dot.style.background = 'var(--' + color(site.level || site.status_level || (site.health && site.health.level)) + ')';
    const status = panel.querySelector('.site-status');
    if (status) {
      text(status, site.status_label || 'unknown');
      status.title = site.status_reason || '';
      status.style.color = 'var(--' + color(site.status_level) + ')';
      status.hidden = !site.status_label;
    }
    const body = panel.querySelector('.site-body');
    if (body) {
      text(body, site.status_reason || site.status_label || 'Status unavailable');
    }
    // The upper right cites the site's numbered Production now lines (the
    // core limit, fair share and feed switch among them), then the weekly
    // average in grey (PROD_DASHBOARD.md).
    const weekly = panel.querySelector('.weekly-cores');
    if (weekly) config.setWeeklyOverride(weekly, null);
    panel.querySelectorAll('.site-fair-share').forEach(node => node.remove());
    renderNotices(panel, site.notices || []);
    const pillars = panel.querySelectorAll('.queuepillar.svgcolumn');
    const titles = [(partialThreads ? 'At least ' : '') + number(values[0]) + ' payload cores; ' + number(values[1]) + ' cores finished, ' +
      number(values[2]) + ' cores failed in last 1h', metrics[3]?.title || number(values[3]) + ' queued cores',
      number(waiting) + ' pilots waiting', number(values[5]) + ' slots/1h'];
    pillars.forEach((node, i) => {
      node.style.display = i === 2 && !showPilots ? 'none' : '';
      node.title = titles[i];
      node.dataset.value = String([values[0], values[3], waiting, values[5]][i] || 0);
      node.style.visibility = [values[0], values[3], waiting, values[5]][i] == null ? 'hidden' : '';
      if (i === 0) {
        node.dataset.finished = String(values[1]);
        node.dataset.failed = String(values[2]);
      }
      if (node.firstElementChild && node.firstElementChild.tagName === 'A') node.firstElementChild.title = titles[i];
    });
    if (trend) {
      // The right end is live: the panel's own cores and queued now, after
      // the last whole quarter-hour, placed by time; the finish and fail
      // rates end at the last whole quarter-hour (PROD_DASHBOARD.md).
      const whole = current.trends.t, minutes = current.trends.bin_minutes;
      const liveAt = current.trends.live_t;
      const live = trend.live && liveAt && whole.length
        && Date.parse(liveAt) > Date.parse(whole[whole.length - 1]) ? trend.live : null;
      const times = live ? whole.concat([liveAt]) : whole;
      const extend = (values, value) => live ? (values || []).concat([value]) : values;
      const coreTrend = {...trend, running: extend(trend.cores, live && live.cores),
        queued: extend(trend.queued_cores, live && live.queued_cores),
        finished: extend(trend.finished_cores, null), failed: extend(trend.failed_cores, null)};
      const start = Date.parse(times[0]), span = Date.parse(times[times.length - 1]) - start;
      const positions = live && span > 0 ? times.map(t => (Date.parse(t) - start) / span) : null;
      let model = trendModel.site(coreTrend, times, minutes);
      const minimum = config.siteTrendMinimums?.[site.id];
      if (model.hasData && minimum > model.ceiling) {
        // A reference column sets the floor through the shared geometry.
        model = trendModel.series([
          {key: 'running', values: coreTrend.running},
          {key: 'queued', values: coreTrend.queued, overflow: true},
          {key: 'finished', values: trendModel.hourly(coreTrend.finished, minutes)},
          {key: 'failed', values: trendModel.hourly(coreTrend.failed, minutes)},
          {key: 'scale-reference', values: [minimum]},
        ], {count: times.length});
        model.curves = model.curves.filter(curve => curve.key !== 'scale-reference');
      }
      plot(panel.querySelector('.plot'), model, times,
        {positions, updated: whole[whole.length - 1]});
    }
    else plot(panel.querySelector('.plot'), null, []);
  }

  function renderExecutors(sites) {
    const osg = sites.find(site => site.id === 'osg');
    const executors = (osg && osg.execute_sites || []).map(item => ({...item,
      running: item.processing_threads})).filter(item => item.running > 0);
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
    renderArrivals(record.arrivals);
  }

  // Science reaching JLab over 24 hours, one row per physics configuration,
  // newest arrival first: its physics, files, last arrival and a bar of the
  // plan's delivered against target (bright green finished or target met,
  // blue incomplete); the detail is the row's title.
  function renderArrivals(arrivals) {
    const count = document.querySelector('#pc-arrivals .arrivals-count');
    if (!count) return;
    const files = arrivals ? `${number(arrivals.files)} files` : '—';
    count.replaceChildren(arrivals && arrivals.link
      ? anchor(arrivals.link, '', files, arrivals.detail) : document.createTextNode(files));
    const rows = [...list(arrivals)].sort((a, b) => String(b.last_at || '').localeCompare(String(a.last_at || '')))
      .map(pc => {
        const row = anchor(pc.link, 'arrival-row', undefined, pc.detail);
        row.append(element('span', 'arrival-label', pc.label || pc.pc),
          element('span', 'arrival-files green', number(pc.files)),
          element('span', 'arrival-time muted', et(pc.last_at).replace(' ET', '')));
        if (pc.target_events) {
          const share = (pc.delivered_events || 0) / pc.target_events;
          const bar = element('span', 'arrival-bar');
          // Bright green finished (target met counts), blue incomplete.
          const fill = element('span', pc.state === 'finished' ? 'done' : 'running');
          fill.style.width = `${Math.min(100, Math.round(share * 100))}%`;
          bar.appendChild(fill);
          row.appendChild(bar);
        }
        return row;
      });
    document.querySelector('#pc-arrivals .arrivals-list').replaceChildren(...(rows.length
      ? rows : [element('span', 'small muted', arrivals ? 'None' : 'Unavailable')]));
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
      if (science.flow) {
        const times = record.trends && record.trends.t || [];
        plot(document.querySelector('#storage .plot'), trendModel.series([
          {key: 'files', values: trendModel.hourly(science.flow.files, record.trends?.bin_minutes)},
        ], {count: times.length}), times);
      } else plot(document.querySelector('#storage .plot'), null, []);
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
    // Each line's number, in its colour, where the bullet was: the site
    // panels cite these numbers (PROD_DASHBOARD.md).
    const issues = list(record.report).map(item => {
      const row = anchor(item.link || config.snapshotUrl, 'issue', undefined, item.text);
      const number = element('span', 'issue-n', item.n != null ? String(item.n) : '');
      number.style.color = 'var(--' + color(item.level) + ')';
      row.append(number, element('span', '', item.text)); return row;
    });
    document.querySelector('#report .issues').replaceChildren(...issues);
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

  function renderCampaign(group) {
    group = group || {};
    const strip = document.getElementById('campaign-summary');
    const metrics = group.metrics || [];
    text(strip.querySelector('.campaign-title'), group.title || 'Campaign —');
    text(strip.querySelector('.campaign-since'), group.subtitle || '');
    text(strip.querySelector('.campaign-events'), metrics[0]?.value || '—');
    text(strip.querySelector('.campaign-bytes'), metrics[1]?.value || '—');
    const totals = strip.querySelector('.campaign-totals');
    totals.replaceChildren(...metrics.flatMap((item, i) => {
      const value = element('b', 'green ' + (i ? 'campaign-bytes' : 'campaign-events'), item.value);
      value.style.color = item.colour;
      return [value, document.createTextNode(' ' + item.label + ' ')];
    }));
    text(strip.querySelector('.campaign-configs'), group.footer?.text || '');
    strip.dataset.campaign = current.campaign?.id || '';
  }

  function renderHeader(groups, linksOnly = false) {
    const cells = document.querySelectorAll('#flow > .flowcell');
    const targets = {computing: cells[1], work: cells[2], campaign: document.getElementById('campaign-summary')};
    function bind(node, item) {
      if (!node || !item) return;
      let link = node.querySelector(':scope > a.dashboard-drilldown');
      if (!link) {
        link = anchor(item.link, 'dashboard-drilldown');
        link.append(...node.childNodes); node.append(link);
      }
      link.href = item.link; link.title = item.detail || '';
      link.dashboardDestination = item.link;
      node.title = item.detail || '';
    }
    for (const group of groups || []) {
      const cell = targets[group.id];
      if (!cell) continue;
      if (group.id === 'campaign') {
        if (!linksOnly) renderCampaign(group);
        bind(cell.querySelector('.campaign-title'), group);
        bind(cell.querySelector('.campaign-configs'), group.footer);
        ['.campaign-events', '.campaign-bytes'].forEach((selector, i) => bind(cell.querySelector(selector), group.metrics[i]));
      } else {
        if (!linksOnly) text(cell.querySelector('.label'), group.title);
        bind(cell.querySelector('.label'), group);
        cell.querySelectorAll('.flowrow > div').forEach((node, i) => {
          const item = group.metrics[i];
          if (!item) return;
          if (!linksOnly) {
            const value = node.querySelector('b');
            text(value, item.value); value.style.color = item.colour;
            text(node.querySelector('span'), item.label);
          }
          bind(node, item);
        });
      }
    }
  }

  let current = null;
  function render(record) {
    if (!record || record.error) throw new Error(record && record.error || 'Production record missing');
    current = record;
    const sites = record.sites || [];
    const sum = key => sumKnown(sites.flatMap(site => site.queues || []), key);
    const storage = (record.storage || []).find(row => row.id === 'jlab-rucio') || {};
    const front = record.pressure_front || {};
    const values = [[front.ready_tasks, front.released_1h], null,
      null, [storage.arrived_1h, storage.bytes_1h == null ? null : storage.bytes_1h / 1e9]];
    document.querySelectorAll('#flow > .flowcell').forEach((cell, i) => {
      if (values[i]) cell.querySelectorAll('.flowrow b').forEach((node, j) => text(node, number(values[i][j])));
    });
    sites.forEach(site => renderSite(site, record.trends && record.trends.sites.find(row => row.id === site.id)));
    panels.forEach((panel, name) => {
      if (panel.querySelector('.metric') && !sites.some(site => (aliases[site.id] || site.name) === name))
        renderSite({name, queues: [], health: {level: 'grey'}}, null);
    });
    renderHeader(record.report?.header);
    renderSitePillars();
    renderExecutors(sites); renderTasks(record); renderStorage(record); renderReports(record);
    text(document.getElementById('stamp'), et(record.observed_at, true, true));
    document.getElementById('stamp').classList.remove('red');
    document.body.classList.remove('record-error');
    linkDashboard(record);
    renderHeader(record.report?.header, true);
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
    window.epicDashboardEfficiency?.update();
    updateResources();
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
  updateResources();
  setInterval(() => { if (visible()) update(); }, 60000);
  addEventListener('focus', () => { if (visible()) update(); });
  addEventListener('pageshow', () => { if (visible()) update(); });
  document.addEventListener('visibilitychange', () => { if (visible()) update(); });
})();
