/* Dashboard DOM adapters for the shared, pure epic-pillar-model.js geometry. */
((root) => {
  'use strict';
  const model = root.createEpicPillarModel();
  const {segments, failureFraction, fullHeight, siteHeight, siteSVG, siteScale, execMaxHeight} = model;

  function renderSitePillars(document = root.document) {
    document.querySelectorAll('.site').forEach(panel => {
      const name = panel.dataset.siteKey || panel.querySelector('.panelhead strong').textContent.trim();
      const configured = Number(root.epicDashboard?.pillarScales?.[name]);
      const scale = configured > 0 ? configured : siteScale;
      panel.dataset.pillarScale = String(scale);
      const pillars = panel.querySelector('.queuepillars');
      if (!pillars || !(configured > 0)) return;
      let label = pillars.querySelector('.pillar-scale');
      if (!label) {
        label = document.createElement('span'); label.className = 'pillar-scale';
        pillars.appendChild(label);
      }
      label.textContent = String(scale);
      label.title = 'Pillar scale: ' + scale + ' · running pillar counts payload threads';
    });
    document.querySelectorAll('.site .queuepillar.svgcolumn').forEach(pillar => {
      const numbers = Array.from(pillar.title.matchAll(/\d[\d,]*/g), match => Number(match[0].replaceAll(',', '')));
      const value = pillar.dataset.value === undefined ? (numbers[0] || 0) : Number(pillar.dataset.value);
      const svg = pillar.querySelector('svg');
      const color = svg.querySelector('path').getAttribute('fill');
      const defs = svg.querySelector('defs').outerHTML, shade = svg.querySelector('linearGradient').id;
      const scale = Number(pillar.closest('.site').dataset.pillarScale) || siteScale;
      const scaled = value * siteScale / scale;
      const height = siteHeight(scaled);
      const finished = pillar.dataset.finished === undefined ? (numbers[1] || 0) : Number(pillar.dataset.finished);
      const failed = pillar.dataset.failed === undefined ? (numbers[2] || 0) : Number(pillar.dataset.failed);
      const fraction = pillar.dataset.failed !== undefined || pillar.title.includes('finished') ? failureFraction(finished, failed) : 0;
      pillar.classList.toggle('pillar-break', value > scale);
      pillar.style.height = height + 'px';
      svg.style.overflow = 'visible'; svg.setAttribute('height', height); svg.setAttribute('viewBox', '0 0 18 ' + height);
      svg.innerHTML = siteSVG({height, whole: fullHeight(scaled), color, defs, shade, failedFraction: fraction});
    });
  }

  function renderExecPillars(document = root.document) {
    document.querySelectorAll('.exec').forEach(executor => {
      const pillar = executor.querySelector('.cylinder');
      if (!pillar) return;
      const count = Number(executor.querySelector('.count').textContent.replaceAll(',', ''));
      const scaled = siteHeight(count), height = Math.min(execMaxHeight, scaled);
      if (pillar.dataset.failureFraction === undefined) {
        const percent = parseFloat(pillar.style.getPropertyValue('--fail'));
        pillar.dataset.failureFraction = String(Number.isFinite(percent) ? percent / 100 : 0);
      }
      const fraction = Number(pillar.dataset.failureFraction) || 0;
      const split = segments({height, failedFraction: fraction, radius: 3});
      pillar.style.setProperty('--fail', split.redRectHeight + 'px');
      pillar.style.setProperty('--red-radius', split.redRadius + 'px');
      const red = pillar.querySelector('.failurecap');
      if (red) { red.style.clipPath = 'none'; red.style.borderRadius = '0'; red.style.display = fraction > 0 ? '' : 'none'; }
      pillar.style.height = height + 'px'; pillar.classList.remove('capped');
      pillar.classList.toggle('pillar-break', scaled > execMaxHeight);
    });
    document.querySelector('.executeplot').style.height = '58px';
  }

  const api = Object.freeze({...model, renderSitePillars, renderExecPillars});
  root.EpicPillars = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
