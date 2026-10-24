/* Pure shared trend geometry. No imports, DOM access or executable startup. */
function createEpicTrendModel() {
  const palette = Object.freeze({running: '#edf2f1', queued: '#c4b6a2',
    finished: '#7fce95', failed: '#f47c70', files: '#7fce95',
    incomplete: '#f47c70', bytes: '#6fcad2'});
  const rateMultiplier = 4;
  const valueOf = value => value == null || !Number.isFinite(Number(value))
    ? null : Math.max(0, Number(value));
  const samePoint = (a, b) => a[0] === b[0] && a[1] === b[1];

  function append(paths, start, end = start) {
    let path = paths[paths.length - 1];
    if (!path || !samePoint(path[path.length - 1], start)) {
      path = [start]; paths.push(path);
    }
    if (!samePoint(path[path.length - 1], end)) path.push(end);
  }

  function trace(values, ceiling, count) {
    const solid = [], overflow = [];
    let previous = null;
    for (let i = 0; i < count; i += 1) {
      const value = valueOf(values[i]);
      if (value == null) { previous = null; continue; }
      const x = i / Math.max(1, count - 1);
      const above = value > ceiling;
      const point = [x, Math.min(value, ceiling) / ceiling];
      if (!previous) append(above ? overflow : solid, point);
      else if (above === previous.above) {
        append(above ? overflow : solid, previous.point, point);
      } else {
        const fraction = (ceiling - previous.value) / (value - previous.value);
        const crossing = [previous.x + (x - previous.x) * fraction, 1];
        append(previous.above ? overflow : solid, previous.point, crossing);
        append(above ? overflow : solid, crossing, point);
      }
      previous = {x, value, above, point};
    }
    return {solid, overflow};
  }

  function series(columns, options = {}) {
    const count = options.count == null
      ? Math.max(0, ...columns.map(column => (column.values || []).length)) : options.count;
    if (!Number.isInteger(count) || count < 0) throw new RangeError('Invalid trend sample count');
    let peak = 0, hasData = false;
    for (const column of columns) {
      if (column.overflow) continue;
      for (const raw of (column.values || []).slice(0, count)) {
        const value = valueOf(raw);
        if (value != null) { hasData = true; peak = Math.max(peak, value); }
      }
    }
    // A wholly zero or unavailable series needs a nonzero denominator.
    const ceiling = peak || 1;
    const curves = columns.map(column => ({key: column.key,
      color: palette[column.key] || column.color || palette.finished,
      ...trace(column.values || [], ceiling, count)}));
    return {ceiling, hasData, curves};
  }

  function site(trend, times) {
    const hourly = values => (values || []).map(value => {
      const n = valueOf(value); return n == null ? null : n * rateMultiplier;
    });
    return series([
      {key: 'running', values: trend.running},
      {key: 'queued', values: trend.queued, overflow: true},
      {key: 'finished', values: hourly(trend.finished)},
      {key: 'failed', values: hourly(trend.failed)},
    ], {count: times.length});
  }

  return Object.freeze({palette, rateMultiplier, series, site});
}
