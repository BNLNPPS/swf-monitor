/* Pure shared cylinder geometry. No imports, DOM access or executable startup. */
function createEpicPillarModel() {
  const siteScale = 2500;
  const execMaxHeight = 27;
  const fullHeight = value => Math.max(8, 6 + 110 * Math.max(0, value) / siteScale);
  const siteHeight = value => Math.min(116, fullHeight(value));
  const failureFraction = (finished, failed) => {
    if (!Number.isFinite(Number(finished)) || !Number.isFinite(Number(failed))) return 0;
    const good = Math.max(0, Number(finished)), bad = Math.max(0, Number(failed));
    return good + bad > 0 ? bad / (good + bad) : 0;
  };

  function segments({height, fullHeight: whole = height, endCaps = 0, failedFraction = 0, radius = 0}) {
    const body = Math.max(0, height - endCaps);
    const fraction = Math.max(0, Math.min(1, Number(failedFraction) || 0));
    const failedHeight = Math.min(body, Math.max(0, whole - endCaps) * fraction);
    // Keep the curved front entirely inside the measured red extent. Clipping
    // this ellipse at the split flattens it and can hide a small green remainder.
    const redRadius = Math.min(radius, failedHeight / 2);
    return {height, failedHeight, finishedHeight: body - failedHeight, redRadius,
      redRectHeight: Math.max(0, failedHeight - redRadius),
      redCenter: endCaps / 2 + failedHeight - redRadius};
  }

  function siteSVG({height, whole, color, defs, shade, failedFraction}) {
    const body = 'M0 3H18V' + (height - 3) + 'Q9 ' + (height + 3) + ' 0 ' + (height - 3) + 'Z';
    const split = segments({height, fullHeight: whole, endCaps: 6, failedFraction, radius: 3});
    let red = '', top = color;
    if (split.failedHeight > 0) {
      red = split.failedHeight >= height - 6 ? '<path d="' + body + '" fill="#f47c70"/>' :
        '<rect x="0" y="3" width="18" height="' + split.redRectHeight + '" fill="#f47c70"/>' +
        '<ellipse cx="9" cy="' + split.redCenter + '" rx="9" ry="' + split.redRadius + '" fill="#f47c70"/>';
      top = '#f47c70';
    }
    return defs + '<path d="' + body + '" fill="' + color + '"/>' + red +
      '<path d="' + body + '" fill="url(#' + shade + ')"/>' +
      '<ellipse cx="9" cy="3" rx="9" ry="3" fill="' + top + '"/>';
  }

  return Object.freeze({segments, failureFraction, fullHeight, siteHeight, siteSVG, siteScale, execMaxHeight});
}
