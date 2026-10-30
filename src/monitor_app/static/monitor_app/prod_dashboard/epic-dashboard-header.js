(() => {
  const stage = document.getElementById('stage');
  const flow = document.getElementById('flow');
  const header = document.querySelector('header');
  if (!stage || !flow || !header) return;
  const space = document.createElement('div');
  space.setAttribute('aria-hidden', 'true');
  flow.replaceWith(space);
  const pinned = document.createElement('div');
  pinned.id = 'dashboard-header';
  pinned.style.cssText = 'position:fixed;left:0;top:0;width:1200px;height:71px;'
    + 'padding:8px 10px;overflow:hidden;transform-origin:top left;background:var(--bg);z-index:10;';
  pinned.append(header, flow);
  document.body.appendChild(pinned);
  function fit() { pinned.style.transform = stage.style.transform; }
  addEventListener('resize', fit);
  fit();
})();
