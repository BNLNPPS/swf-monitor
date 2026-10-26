function linkDashboard(record) {
  const base = 'https://epic-devcloud.org/prod/';
  const sites = Object.fromEntries((record.sites || []).map(site =>
    [window.epicDashboard.sitePanels[site.id] || site.name, site]));
  const queues = Object.fromEntries(Object.entries(sites).map(([name, site]) =>
    [name, (site.queues || []).map(queue => queue.id)]));
  function url(path, params = {}, hash = '') {
    const result = new URL(path, base);
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== '') result.searchParams.set(key, value);
    });
    result.hash = hash;
    return result.href;
  }
  function history(site, window = '12h') {
    if (site === 'Compute Canada') return queuePage(site);
    return url('snapper/epicprod/site/', {window, site: (queues[site] || []).join(',')});
  }
  function jobs(site, status, hours = 12, extra = {}) {
    return url('panda/jobs/', {site: extra.taskid ? undefined :
      (site ? queues[site] || [] : Object.values(queues).flat()).join(','), status, hours, ...extra});
  }
  function ended(site, status, extra = {}) {
    const end = new Date();
    return jobs(site, status, 1, {
      ended_after: new Date(end.getTime() - 3600000).toISOString(),
      ended_before: end.toISOString(), ...extra
    });
  }
  function errors(site, extra = {}) {
    const end = new Date();
    return url('panda/errors/', {site: (site ? queues[site] || [] : []).join(','),
      status: 'failed', hours: 1,
      ended_after: new Date(end.getTime() - 3600000).toISOString(),
      ended_before: end.toISOString(), ...extra});
  }
  function queuePage(site, workers = false) {
    const destination = new URL(sites[site]?.links?.panda_queue || 'panda/epic-queues/', base);
    if (workers) destination.hash = 'workers';
    return destination.href;
  }
  function storage(params = {}) {
    return url('snapper/epicprod/storage/', {window: '12h', ...params});
  }
  function link(element, destination, description) {
    if (!element) return;
    for (const [key, site] of Object.entries(sites)) {
      if (typeof description === 'string' && (description === key || description.startsWith(key + ':')))
        description = site.name + description.slice(key.length);
    }
    let anchor = element.tagName === 'A' ? element : element.querySelector(':scope > a.dashboard-drilldown');
    if (!anchor) {
      anchor = document.createElement('a');
      anchor.className = 'dashboard-drilldown';
      while (element.firstChild) anchor.appendChild(element.firstChild);
      element.appendChild(anchor);
    }
    const refresh = () => { anchor.href = typeof destination === 'function' ? destination() : destination; };
    refresh();
    anchor.target = '_blank';
    anchor.rel = 'noopener';
    anchor.title = description;
    anchor.dashboardDestination = destination;
    if (!anchor.dataset.dashboardBound) {
      anchor.dataset.dashboardBound = '1';
      const update = () => { anchor.href = typeof anchor.dashboardDestination === 'function' ? anchor.dashboardDestination() : anchor.dashboardDestination; };
      anchor.addEventListener('pointerdown', update);
      anchor.addEventListener('click', update);
    }
  }

  link(document.querySelector('header .brand'), url('snapper/epicprod/report/', {window: '12h'}),
    'Production · last 12h');
  document.querySelectorAll('#flow > .flowcell').forEach(cell => {
    const title = cell.querySelector('.label');
    if (!title) return;
    const metrics = cell.querySelectorAll('.flowrow > div');
    switch (title.textContent.trim()) {
      case 'Pressure front':
        link(title, url('panda/front/'), 'Pressure front');
        link(metrics[0], url('panda/front/', {}, 'ready'), 'Ready production tasks');
        link(metrics[1], url('panda/front/'), 'Production releases and queue decisions');
        break;
      case 'Computing now':
        link(title, url('snapper/epicprod/report/', {window: '12h'}), 'Production history');
        link(metrics[0], jobs('', 'running', 48), 'Running jobs');
        link(metrics[1], () => ended('', 'finished'), 'Jobs finished in the last hour');
        break;
      case 'Work available':
        link(title, url('panda/front/'), 'Production supply and ready work');
        link(metrics[0], jobs('', 'queued', 720), 'Queued jobs: defined, waiting, assigned, activated and sent');
        link(metrics[1], url('panda/tasks/', {days: 90}), 'Task input files');
        break;
      case 'Science data':
        link(title, storage(), 'Data arrivals and storage');
        link(metrics[0], storage({quantity: 'files'}), 'Data file arrivals');
        link(metrics[1], storage({quantity: 'bytes'}), 'Data volume');
        break;
    }
  });

  document.querySelectorAll('.site').forEach(panel => {
    const title = panel.querySelector('.panelhead strong');
    const name = panel.dataset.siteKey || title.textContent.trim();
    if (!(name in queues)) return;
    link(title, history(name), name + ': last 12h');
    const weekly = panel.querySelector('.weekly-cores');
    const workerLimit = sites[name].worker_limit && sites[name].worker_limit.notice;
    link(weekly, workerLimit ? workerLimit.link : history(name, '7d'),
      workerLimit ? workerLimit.detail : weekly && weekly.dataset.weeklyTitle ||
      name + ': running cores · last 7d');
    const status = panel.querySelector('.site-status');
    link(status, name === 'Perlmutter' ? url('panda/front/', {}, 'nersc-allocation') : queuePage(name),
      name + ': queue status');
    const body = panel.querySelector('.site-body');
    if (!body?.dataset.siteBoard) link(body, queuePage(name), name + ': queue details');
    const metrics = panel.querySelectorAll('.metric');
    const destinations = [
      jobs(name, 'running', 48), () => ended(name, 'finished'), () => errors(name),
      jobs(name, 'queued', 720), queuePage(name, true), url('panda/worker-starts/', {site: queues[name].join(','), hours: 1})
    ];
    const descriptions = ['Payload cores; open the contributing jobs', 'Core completions in the last hour', 'Failed cores in the last hour',
      'Queued core capacity', 'Harvester workers and pilot supply', 'Worker starts, admission declines and repeated wasted starts'];
    metrics.forEach((metric, i) => link(metric, destinations[i], name + ': ' +
      (i < 4 && metric.title ? metric.title : descriptions[i])));
    const plot = panel.querySelector('.plot');
    if (plot) link(plot.tagName === 'A' ? plot : plot.closest('a') || plot, history(name),
      name + ': last 12h');
    panel.querySelectorAll('.queuepillar.svgcolumn').forEach((pillar, i) => {
      const targets = [history(name), jobs(name, 'queued', 720), queuePage(name, true), url('panda/worker-starts/', {site: queues[name].join(','), hours: 1})];
      link(pillar, targets[i], pillar.title);
    });
  });

  link(document.querySelector('#executors h3'), history('OSG'), 'OSG production history and execution sites');
  link(document.querySelector('#executors .large'), jobs('OSG', 'running', 48), 'Running OSG jobs by execution site');
  document.querySelectorAll('.exec').forEach(anchor => {
    const destination = new URL(anchor.href);
    if (destination.pathname.endsWith('/panda/jobs/')) {
      destination.searchParams.delete('days');
      destination.searchParams.set('hours', '12');
      destination.searchParams.set('site', (queues.OSG || []).join(','));
      anchor.href = destination.href;
    } else anchor.href = history('OSG');
  });

  link(document.querySelector('#tasks .panelhead strong'), url('panda/tasks/', {days: 90}),
    'Tasks and input files');
  document.querySelectorAll('#tasks tbody tr').forEach(row => {
    const taskLink = row.querySelector('td:first-child a');
    if (!taskLink) return;
    const task = taskLink.textContent.trim();
    const detail = url('panda/tasks/' + task + '/');
    link(taskLink, url('panda/jobs/', {taskid: task, days: 'all'}), 'Jobs for task ' + task);
    const cells = Array.from(row.cells);
    cells.slice(1).forEach((cell, index) => {
      if (!cell.textContent.trim()) return;
      const column = index + 1;
      const destination = column === 2 ? jobs('', 'running', 2160, {taskid: task}) :
        column === 3 ? jobs('', 'queued', 2160, {taskid: task}) :
        column === 6 ? url('panda/errors/', {taskid: task, days: 'all', status: 'failed'}) : detail;
      const description = column === 2 ? 'Running jobs for task ' + task :
        column === 3 ? 'Queued jobs for task ' + task :
        column === 6 ? 'Error breakdown for task ' + task : 'Task ' + task + ': input files';
      link(cell, destination, description);
    });
  });
  const campaign = document.getElementById('campaign-summary');
  if (campaign) {
    const plan = url('pcs/plan/', {campaign: campaign.dataset.campaign || ''});
    link(campaign.querySelector('.campaign-title'), plan, 'Campaign production plan');
    link(campaign.querySelector('.campaign-configs'), plan, 'Physics configurations in this campaign');
  }
  link(document.querySelector('#paused-top .panelhead strong'), url('panda/tasks/', {status: 'paused', days: 90}),
    'Paused production tasks');
  document.querySelectorAll('#paused-top .pausechip').forEach(chip => {
    const task = chip.textContent.trim();
    if (/^\d+$/.test(task)) link(chip, url('panda/jobs/', {taskid: task, days: 'all'}),
      'Jobs for paused task ' + task);
  });

  const data = document.querySelector('#storage .storagemain > div:first-child');
  if (data) {
    const labels = data.querySelectorAll(':scope > .label');
    link(labels[0], storage(), 'JLab Rucio science data history');
    link(data.querySelector(':scope > b'), storage({quantity: 'files'}), 'Registered science data file history');
    link(data.querySelector(':scope > div.small:not(.green):not(.muted)'), storage({quantity: 'bytes'}), 'Registered science data volume history');
    const incomplete = data.querySelector('.registration-incomplete') ||
      Array.from(data.querySelectorAll(':scope > .small')).find(node => /incomplete/.test(node.textContent));
    link(incomplete, () => incomplete.dataset.drilldown || ended('', 'finished', {registration: 'incomplete'}),
      'Jobs finished in the last hour with data registration pending or unfinished');
    link(labels[1], url('panda/storage-doors/'), 'BNL Rucio storage services');
    link(data.querySelector('.logs b'), () => ended('', ''), 'Jobs ending in the last hour');
    link(data.querySelector('.log-stageout-failures'), url('snapper/epicprod/errors/', {window: '12h', lens: 'component'}),
      'Data management and log stage-out errors');
    link(data.querySelector(':scope > .small.muted'), url('pcs/stash/'), 'Science data failover stash and drain status');
  }
  link(document.querySelector('#storage .panelhead strong'), storage(), 'Science data and storage history');
  link(document.querySelector('#storage .plot'), storage({quantity: 'files'}), 'Registered files');
  link(document.querySelector('#storage .science-storage'),
    storage({rse: 'BNL-XRD', panels: 'capacity', quantity: 'bytes'}), 'BNL-XRD usage and quota');
  link(document.querySelector('#events .panelhead strong'), url('logs/', {live: 1}), 'Live production action stream');
  document.querySelectorAll('#events .boardrow').forEach(row => {
    const anchor = row.querySelector('a');
    if (anchor) link(row.querySelector('time'), anchor.href, anchor.title);
  });
  const assessment = document.querySelector('#daily-assessment .assessment-text a');
  if (assessment) document.querySelectorAll('#daily-assessment .assessment-title > span').forEach(element =>
    link(element, assessment.href, 'Daily assessment'));
  if (typeof fitPausedTasks === 'function') fitPausedTasks();
}
