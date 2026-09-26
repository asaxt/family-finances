// Mirror controls are disabled for clarity; the server and database enforce it.
(() => {
  const allowed = new Set(['/logout', '/api/local-ai', '/api/overview-lookback', '/api/projections']);
  for (const form of document.forms) {
    if (form.method.toLowerCase() !== 'post' || allowed.has(new URL(form.action).pathname)) continue;
    for (const control of form.elements) control.disabled = true;
    const group = document.createElement('fieldset');
    group.disabled = true;
    group.className = 'mirror-disabled-fields';
    group.title = 'Make data changes in the production app.';
    while (form.firstChild) group.append(form.firstChild);
    form.append(group);
  }
  const timestamp = document.getElementById('mirror-copy-time');
  if (timestamp) timestamp.textContent = new Date(timestamp.dateTime).toLocaleString();
})();
