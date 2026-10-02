(() => {
  document.querySelectorAll('.person-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const status = form.querySelector('[data-save-status]');
    button.disabled = true;
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      if (response.status === 401) { window.location.assign('/login'); return; }
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not save. Reload and try again.');
      window.location.assign('/people?saved=1');
    } catch (error) { status.textContent = error.message; button.disabled = false; }
  }));
})();
