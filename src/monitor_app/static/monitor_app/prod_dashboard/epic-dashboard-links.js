function linkDashboard() {
  const base = 'https://epic-devcloud.org/prod/';
  const queues = {
    OSG: ['BNL_OSG_PanDA_1'],
    GREX: ['UM_GREX_PanDA_1'],
    Perlmutter: ['NERSC_Perlmutter_epic', 'NERSC_Perlmutter_epic_es'],
    Google: ['BNL_ePIC_GOOGLE', 'BNL_ePIC_GOOGLE_es'],
    'Compute Canada': ['ComputeCanada_PanDA_1'],
    JLab: ['JLAB_PanDA_1']
  };
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
    return url('panda/jobs/', {site: (site ? queues[site] || [] : Object.values(queues).flat()).join(','), status, hours, ...extra});
  }
  function ended(site, status, extra = {}) {
    const end = new Date();
    return jobs(site, status, 1, {
      ended_after: new Date(end.getTime() - 3600000).toISOString(),
      ended_before: end.toISOString(), ...extra
    });
  }
  function queuePage(site, workers = false) {
    const queue = (queues[site] || [])[0];
    return url('panda/epic-queues/' + encodeURIComponent(queue) + '/', {}, workers ? 'workers' : '');
  }
  function storage(params = {}) {
    return url('snapper/epicprod/storage/', {window: '12h', ...params});
  }
  function link(element, destination, description) {
    if (!element) return;
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
    'Live production time history over the last 12 hours');
  document.querySelectorAll('#flow > .flowcell').forEach(cell => {
    const title = cell.querySelector('.label');
    if (!title) return;
    const metrics = cell.querySelectorAll('.flowrow > div');
    switch (title.textContent.trim()) {
      case 'Pressure front':
        link(title, url('panda/front/'), 'Current production pressure front');
        link(metrics[0], url('panda/front/', {}, 'ready'), 'Ready production tasks');
        link(metrics[1], url('panda/front/'), 'Production releases and queue decisions');
        break;
      case 'Computing now':
        link(title, url('snapper/epicprod/report/', {window: '12h'}), 'Live production time history');
        link(metrics[0], jobs('', 'running', 48), 'Currently running production jobs');
        link(metrics[1], () => ended('', 'finished'), 'Jobs finished in the last hour');
        break;
      case 'Work available':
        link(title, url('panda/front/'), 'Production supply and ready work');
        link(metrics[0], jobs('', 'queued', 720), 'Queued jobs: defined, waiting, assigned, activated and sent');
        link(metrics[1], url('panda/tasks/', {days: 90}), 'Task input-file progress, including work not yet generated');
        break;
      case 'Science data':
        link(title, storage(), 'Science data arrival and storage time history');
        link(metrics[0], storage({quantity: 'files'}), 'Science data file arrival history');
        link(metrics[1], storage({quantity: 'bytes'}), 'Science data volume history');
        break;
    }
  });

  document.querySelectorAll('.site').forEach(panel => {
    const title = panel.querySelector('.panelhead strong');
    const name = title.textContent.trim();
    if (!(name in queues)) return;
    link(title, history(name), name + ': live queue history over the last 12 hours');
    const weekly = panel.querySelector('.weekly-cores');
    link(weekly, history(name, '7d'), name + ': running-core history over the last week');
    const status = panel.querySelector('.site-status');
    link(status, name === 'Perlmutter' ? url('panda/front/', {}, 'nersc-allocation') : queuePage(name),
      name + ': current queue configuration and operating status');
    link(panel.querySelector('.site-body'), queuePage(name), name + ': queue details');
    const metrics = panel.querySelectorAll('.metric');
    const destinations = [
      jobs(name, 'running', 48), () => ended(name, 'finished'), () => ended(name, 'failed'),
      jobs(name, 'queued', 720), queuePage(name, true), queuePage(name, true)
    ];
    const descriptions = ['Running jobs', 'Jobs finished in the last hour', 'Jobs failed in the last hour',
      'Queued jobs', 'Harvester workers and pilot supply', 'Harvester workers and slots'];
    metrics.forEach((metric, i) => link(metric, destinations[i], name + ': ' + descriptions[i]));
    const plot = panel.querySelector('.plot');
    if (plot) link(plot.tagName === 'A' ? plot : plot.closest('a') || plot, history(name),
      name + ': live queue history over the last 12 hours');
    panel.querySelectorAll('.queuepillar.svgcolumn').forEach((pillar, i) => {
      const targets = [history(name), jobs(name, 'queued', 720), queuePage(name, true), queuePage(name, true)];
      link(pillar, targets[i], pillar.title + '; open focused live information');
    });
  });

  link(document.querySelector('#executors h3'), history('OSG'), 'OSG production history and execution sites');
  link(document.querySelector('#executors .large'), jobs('OSG', 'running', 48), 'Running OSG jobs by execution site');
  document.querySelectorAll('.exec').forEach(anchor => {
    const destination = new URL(anchor.href);
    if (destination.pathname.endsWith('/panda/jobs/')) {
      destination.searchParams.delete('days');
      destination.searchParams.set('hours', '12');
      destination.searchParams.set('site', queues.OSG.join(','));
      anchor.href = destination.href;
    } else anchor.href = history('OSG');
  });

  link(document.querySelector('#tasks .panelhead strong'), url('panda/tasks/', {days: 90}),
    'Live task list and input-file progress');
  document.querySelectorAll('#tasks tbody tr').forEach(row => {
    const taskLink = row.querySelector('td:first-child a');
    if (!taskLink) return;
    const task = taskLink.textContent.trim();
    const cells = Array.from(row.cells);
    cells.slice(1).forEach((cell, index) => {
      if (!cell.textContent.trim()) return;
      const column = index + 1;
      const destination = column === 2 ? jobs('', 'running', 2160, {taskid: task}) :
        column === 3 ? jobs('', 'queued', 2160, {taskid: task}) : taskLink.href;
      const description = column === 2 ? 'Running jobs for task ' + task :
        column === 3 ? 'Queued jobs for task ' + task : 'Task ' + task + ': cumulative input-file accounting';
      link(cell, destination, description);
    });
  });
  link(document.querySelector('#paused-top .panelhead strong'), url('panda/tasks/', {status: 'paused', days: 90}),
    'Paused production tasks');

  const data = document.querySelector('#storage .storagemain > div:first-child');
  if (data) {
    const labels = data.querySelectorAll(':scope > .label');
    link(labels[0], storage(), 'JLab Rucio science data history');
    link(data.querySelector(':scope > b'), storage({quantity: 'files'}), 'Registered science data file history');
    link(data.querySelector(':scope > div.small:not(.green):not(.muted)'), storage({quantity: 'bytes'}), 'Registered science data volume history');
    link(data.querySelector(':scope > .small.green'), url('pcs/stash/'), 'Pending science data registrations and failover stash');
    link(labels[1], url('panda/storage-doors/'), 'BNL Rucio storage services');
    link(data.querySelector('.logs b'), () => ended('', ''), 'Jobs ending in the last hour, including their log stage-out');
    link(data.querySelector('.logs .small.green'), url('snapper/epicprod/errors/', {window: '12h', lens: 'component'}),
      'Recent data-management and log stage-out error history');
    link(data.querySelector(':scope > .small.muted'), url('pcs/stash/'), 'Science data failover stash and drain status');
  }
  link(document.querySelector('#storage .panelhead strong'), storage(), 'Science data and storage history');
  link(document.querySelector('#storage .plot'), storage({quantity: 'files'}), 'Registered-file time history');
  link(document.querySelector('#storage .science-storage'),
    storage({rse: 'BNL-XRD', panels: 'capacity', quantity: 'bytes'}), 'BNL-XRD production storage usage and quota history');
  link(document.querySelector('#events .panelhead strong'), url('logs/', {live: 1}), 'Live production action stream');
  document.querySelectorAll('#events .boardrow').forEach(row => {
    const anchor = row.querySelector('a');
    if (anchor) link(row.querySelector('time'), anchor.href, anchor.title);
  });
  const assessment = document.querySelector('#daily-assessment .assessment-text a');
  if (assessment) document.querySelectorAll('#daily-assessment .assessment-title > span').forEach(element =>
    link(element, assessment.href, 'Daily production assessment and its reporting interval'));
  if (typeof fitPausedTasks === 'function') fitPausedTasks();
}
linkDashboard();
