(() => {
  const launcher = document.getElementById('feedback-launcher');
  const panel = document.getElementById('feedback-panel');
  const form = document.getElementById('feedback-form');
  const textarea = document.getElementById('feedback-text');
  const toggle = open => {
    panel.hidden = !open;
    launcher.setAttribute('aria-expanded', String(open));
    (open ? textarea : launcher).focus();
  };
  launcher?.addEventListener('click', () => toggle(panel.hidden));
  document.getElementById('feedback-close')?.addEventListener('click', () => toggle(false));
  panel?.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); toggle(false); }
  });

  async function submit(form) {
    const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
    if (response.status === 401) throw new Error('App locked. Unlock it in another tab, then retry.');
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(data?.error || (response.status >= 500
      ? 'Could not complete this action. Try again.' : 'The form expired. Reload the page and try again.'));
    return data;
  }

  let sending = false;
  form?.addEventListener('submit', async event => {
    event.preventDefault();
    if (sending || !form.reportValidity()) return;
    const button = form.querySelector('button[type="submit"]');
    const status = form.querySelector('.feedback-status');
    sending = true;
    button.disabled = true;
    button.textContent = 'Sending…';
    status.textContent = '';
    // Capture the message before disabling its field; leave it intact if sending fails.
    const pending = submit(form);
    textarea.disabled = true;
    try {
      const result = await pending;
      textarea.value = '';
      status.textContent = result.message;
      button.textContent = 'Send';
    } catch (error) {
      status.textContent = error instanceof TypeError ? 'Could not send. Your message is still here; retry when connected.' : error.message;
      button.textContent = 'Retry';
    } finally {
      sending = false;
      button.disabled = false;
      textarea.disabled = false;
    }
  });

  document.querySelectorAll('[data-feedback-time]').forEach(time => {
    const date = new Date(time.dateTime);
    if (!Number.isNaN(date.getTime())) time.textContent = date.toLocaleString();
  });
  document.querySelectorAll('[data-feedback-delete]').forEach(deleteForm => {
    deleteForm.addEventListener('submit', async event => {
      event.preventDefault();
      const button = deleteForm.querySelector('button');
      const status = document.getElementById('feedback-inbox-status');
      button.disabled = true;
      try {
        await submit(deleteForm);
        deleteForm.closest('.feedback-entry').remove();
        document.getElementById('feedback-empty').hidden = Boolean(document.querySelector('.feedback-entry'));
        status.textContent = 'Message deleted.';
      } catch (error) {
        status.textContent = error instanceof TypeError ? 'Could not delete. Try again when connected.' : error.message;
        button.disabled = false;
      }
    });
  });
})();
