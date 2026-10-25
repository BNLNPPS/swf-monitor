(() => {
  'use strict';
  const page = document.getElementById('efficiency-page');
  if (!page) return;
  const node = name => document.getElementById('efficiency-' + name);
  const number = n => Number(n || 0).toLocaleString('en-US', {maximumFractionDigits: 1});
  const hours = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : number(n);
  const iso = d => d.toISOString().slice(0, 10);
  let loading = false, timer, snapshot, selected = null;

  function periods(start, end, bucket) {
    const at = new Date(start + 'T00:00:00Z'), last = new Date(end + 'T00:00:00Z'), out = [];
    if (bucket === 'month') at.setUTCDate(1);
    else at.setUTCDate(at.getUTCDate() - (at.getUTCDay() + 6) % 7);
    while (at <= last) {
      out.push(iso(at));
      if (bucket === 'month') at.setUTCMonth(at.getUTCMonth() + 1);
      else at.setUTCDate(at.getUTCDate() + 7);
    }
    return out;
  }

  function render(data) {
    const stamps = periods(data.start, data.end, data.bucket);
    const names = new Map(data.sites.map(s => [s.id, s.label]));
    const byCell = new Map(data.cells.map(c => [c.site + '/' + c.period, c]));
    const volume = new Map();
    data.cells.forEach(c => volume.set(c.site, (volume.get(c.site) || 0) + c.ended));
    let sites = Array.from(names.keys()).filter(s => volume.get(s) > 0).sort((a, b) => volume.get(b) - volume.get(a));
    const limit = Number(node('sites').value);
    const totalSites = sites.length;
    if (limit) sites = sites.slice(0, limit);
    const totals = data.cells.reduce((sum, c) => [sum[0] + c.finished, sum[1] + c.ended, sum[2] + c.missing], [0, 0, 0]);
    const pct = totals[1] ? (100 * totals[0] / totals[1]).toFixed(1) + '%' : '—';
    node('status').textContent = 'Overall ' + pct + ' successful core-hours · ' + hours(totals[0]) + ' / ' + hours(totals[1]) +
      ' core-h · showing ' + sites.length + ' of ' + totalSites + ' sites' +
      (totals[2] ? ' · ' + number(totals[2]) + ' ended jobs lack usable timings' : '') +
      (data.exclude_task ? ' · task 40856 excluded' : '');
    const through = (data.through || '').slice(0, 10);
    const x = stamps.map(d => {
      const next = new Date(d + 'T00:00:00Z');
      if (data.bucket === 'month') next.setUTCMonth(next.getUTCMonth() + 1);
      else next.setUTCDate(next.getUTCDate() + 7);
      next.setUTCDate(next.getUTCDate() - 1);
      const last = iso(next);
      const partial = d < data.start || last > data.end || (d <= through && through <= last);
      return (data.bucket === 'month' ? d.slice(0, 7) : d) + (partial ? '<br>partial' : '');
    });
    const z = [], customdata = [], annotations = [];
    sites.forEach((site, i) => {
      z[i] = []; customdata[i] = [];
      stamps.forEach((stamp, j) => {
        const c = byCell.get(site + '/' + stamp);
        z[i][j] = c?.efficiency ?? null;
        customdata[i][j] = c ? [c.finished, c.ended, c.jobs, c.missing] : [0, 0, 0, 0];
        if (c?.efficiency != null) annotations.push({x: x[j], y: names.get(site), xref: 'x', yref: 'y',
          text: Math.round(c.efficiency) + '%<br><span style="font-size:10px">' + hours(c.ended) + ' core-h</span>',
          showarrow: false, font: {size: 12, color: '#17232c'}});
      });
    });
    const layout = {paper_bgcolor: 'transparent', plot_bgcolor: '#ededed', font: {family: 'Arial, sans-serif'},
      margin: {l: 160, r: 70, t: 60, b: 65}, autosize: true};
    Plotly.react(node('heatmap'), [{type: 'heatmap', x, y: sites.map(s => names.get(s)), z, customdata,
      zmin: 0, zmax: 100, colorscale: [[0, '#e59ca0'], [0.5, '#f4e8ad'], [1, '#79bf91']],
      xgap: 1, ygap: 1, hoverongaps: false,
      colorbar: {title: '%'}, hovertemplate: '%{y}<br>%{x}<br>%{z:.1f}% successful core-hours' +
        '<br>%{customdata[0]:,.1f} successful / %{customdata[1]:,.1f} ended core-h' +
        '<br>%{customdata[2]:,} ended jobs · %{customdata[3]:,} missing timings<extra></extra>'}],
    {...layout, title: {text: 'Efficiency by site and ' + data.bucket, x: 0.02}, annotations,
      height: Math.max(340, 110 + sites.length * 48), yaxis: {autorange: 'reversed', automargin: true},
      xaxis: {side: 'top', type: 'category'}}, {responsive: true, displaylogo: false}).then(() => {
      node('heatmap').removeAllListeners('plotly_click');
      node('heatmap').on('plotly_click', event => {
        const site = sites.find(s => names.get(s) === event.points[0].y);
        selected = selected === site ? null : site;
        render(snapshot);
      });
    });
    const lines = sites.map(site => ({type: 'scatter', mode: 'lines+markers', name: names.get(site),
      x: stamps, y: stamps.map(stamp => byCell.get(site + '/' + stamp)?.efficiency ?? null),
      connectgaps: false, visible: selected && selected !== site ? 'legendonly' : true,
      customdata: stamps.map(stamp => byCell.get(site + '/' + stamp)?.ended || 0),
      line: {width: 2}, marker: {size: 6},
      hovertemplate: names.get(site) + '<br>%{x}<br>%{y:.1f}%<br>%{customdata:,.1f} ended core-h<extra></extra>'}));
    Plotly.react(node('trends'), lines, {...layout,
      title: {text: 'Efficiency trends' + (selected ? ' · ' + names.get(selected) : ''), x: 0.02},
      margin: {l: 65, r: 20, t: 55, b: 110}, height: 490,
      xaxis: {title: 'Job end period (UTC)'}, yaxis: {title: 'Successful core-hours (%)', range: [0, 100]},
      legend: {orientation: 'h', y: -0.22}}, {responsive: true, displaylogo: false});
  }

  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!window.swfPollGate || window.swfPollGate.attended()) update();
      else schedule(300000);
    }, delay);
  }

  async function update(force = false) {
    if (loading) return;
    if (!node('controls').reportValidity()) return;
    if (node('start').value > node('end').value) {
      node('error').hidden = false; node('error').textContent = 'From must be on or before Through.'; return;
    }
    loading = true; clearTimeout(timer); node('update').disabled = true;
    const url = new URL(page.dataset.url, location.href);
    for (const key of ['start', 'end', 'bucket']) url.searchParams.set(key, node(key).value);
    if (node('exclude').checked) url.searchParams.set('exclude_task', '40856');
    if (force) url.searchParams.set('refresh', '1');
    let pending = false;
    try {
      const response = await fetch(url, {cache: 'no-store'});
      const data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || 'History request: HTTP ' + response.status);
      pending = data.refreshing || response.status === 202;
      node('error').hidden = true;
      node('freshness').textContent = data.built_at ? 'Updated ' + new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York', year: 'numeric', month: 'short', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(new Date(data.built_at)) + ' ET' +
        (pending ? ' · updating…' : '') + (data.age_seconds > 7200 ? ' · update delayed' : '') : 'Building history…';
      if (data.cells) {snapshot = data; render(data);}
    } catch (error) {
      node('error').textContent = error.message; node('error').hidden = false;
    } finally {
      loading = false; node('update').disabled = false;
      schedule(pending ? 5000 : 300000);
    }
  }
  node('controls').addEventListener('submit', event => {event.preventDefault(); update();});
  node('controls').addEventListener('change', () => {selected = null; update();});
  node('sites').addEventListener('change', () => {if (snapshot) render(snapshot);});
  node('update').addEventListener('click', () => update(true));
  if (window.swfPollGate) window.swfPollGate.onReturn(() => update());
  update();
})();
