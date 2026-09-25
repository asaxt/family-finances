(() => {
  const form = document.getElementById('chat-form');
  const question = document.getElementById('chat-question');
  const log = document.getElementById('chat-messages');
  const status = document.getElementById('chat-status');
  const error = document.getElementById('chat-error');
  const send = document.getElementById('chat-send');
  const clear = document.getElementById('chat-clear');
  let history = [];
  let busy = false;
  let activeRequest;
  window.addEventListener('pagehide', () => {
    history = []; log.replaceChildren(); question.value = ''; error.hidden = true;
    activeRequest?.abort();
  });
  const money = (value, currency) => `${currency} ${value}`;
  function message(who, text) {
    const article = document.createElement('article');
    article.className = 'assistant-message';
    const label = document.createElement('strong');
    label.textContent = who;
    const body = document.createElement('p');
    body.className = 'assistant-answer';
    body.textContent = text;
    article.append(label, body);
    log.append(article);
    return article;
  }
  function sources(article, evidence) {
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'View supporting data';
    details.append(summary);
    const text = document.createElement('p');
    const plan = evidence.filters;
    const lines = [`Transaction dates: ${plan.date_from || 'earliest available'} to ${plan.date_to || 'latest available'}.`,
      `Category: ${plan.category || 'all'}. Description search: ${plan.search || 'none'}.`];
    if (evidence.transactions) {
      const data = evidence.transactions;
      lines.push(`Transactions: ${data.matched_records} matching records. First: ${data.first_record || 'none'}; last: ${data.last_record || 'none'}.`, data.rules);
      for (const row of data.summaries) {
        lines.push(`${row.group}: ${row.label} (${row.count} records) — tracked spending ${money(row.tracked_spending, row.currency)}; bank income ${money(row.bank_income, row.currency)}; bank spending ${money(row.bank_spending, row.currency)}; bank net ${money(row.bank_net, row.currency)}; all money in ${money(row.money_in, row.currency)}; all money out ${money(row.money_out, row.currency)}.`);
      }
      lines.push('Most recent matching records (up to 20):');
      for (const row of data.recent_records) lines.push(`${row.date} · ${row.description} · ${row.category} · ${row.direction} ${money(row.amount, row.currency)}`);
      lines.push('Largest matching records (up to 10):');
      for (const row of data.largest_records) lines.push(`${row.date} · ${row.description} · ${row.direction} ${money(row.amount, row.currency)}`);
    }
    if (evidence.savings) {
      const data = evidence.savings;
      lines.push('Savings: latest recorded snapshots.', data.rules);
      for (const [key, value] of Object.entries(data.totals)) lines.push(`${key.replaceAll('_', ' ')}: ${money(value, data.currency)}`);
      for (const row of data.accounts) lines.push(`${row.name} · ${row.classification} · ${row.recorded_on || 'no balance recorded'} · ${row.balance === null ? 'unknown balance' : money(row.balance, data.currency)}`);
    }
    text.className = 'assistant-answer';
    text.textContent = lines.join('\n');
    details.append(text);
    for (const [label, url, available] of [['Open Transactions', '/transactions', evidence.transactions], ['Open Savings', '/savings', evidence.savings]]) {
      if (!available) continue;
      const link = document.createElement('a');
      link.textContent = label;
      link.href = url;
      link.className = 'button secondary';
      details.append(link);
    }
    article.append(details);
  }
  document.querySelectorAll('.assistant-examples button').forEach(button => {
    button.addEventListener('click', () => { if (!busy) { question.value = button.textContent; question.focus(); } });
  });
  clear.addEventListener('click', () => {
    if (busy) return;
    history = []; log.replaceChildren(); error.hidden = true; question.value = ''; question.focus();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !question.value.trim()) return;
    const value = question.value.trim();
    busy = true; send.disabled = true; clear.disabled = true; question.disabled = true;
    error.hidden = true; status.textContent = 'Reading your data and asking Ollama… This can take a few minutes.';
    const userMessage = message('You', value);
    activeRequest = new AbortController();
    try {
      const response = await fetch('/api/assistant', {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.elements.csrf_token.value},
        body: JSON.stringify({question: value, history: history.slice(-6)}),
        signal: AbortSignal.any([activeRequest.signal, AbortSignal.timeout(390000)])
      });
      if (response.status === 401) { window.location.assign('/login'); return; }
      const data = await response.json().catch(() => { throw new Error('Your session may have expired. Reload the page and try again.'); });
      if (!response.ok) throw new Error(data.error || 'The assistant could not answer. Please try again.');
      const article = message('Local assistant', data.answer);
      sources(article, data.evidence);
      history.push({role: 'user', content: value}, {role: 'assistant', content: data.answer.slice(0, 8000)});
      history = history.slice(-6);
      question.value = '';
    } catch (failure) {
      userMessage.remove();
      if (failure.name === 'AbortError') return;
      error.textContent = failure.name === 'TimeoutError' ? 'Ollama took too long. Try a narrower question.' : failure.message;
      error.hidden = false;
    } finally {
      busy = false; send.disabled = false; clear.disabled = false; question.disabled = false;
      activeRequest = null;
      status.textContent = ''; question.focus();
    }
  });
})();
