(() => {
  const form = document.getElementById('plan-form');
  const results = document.getElementById('plan-results');
  const error = document.getElementById('plan-error');
  const button = document.getElementById('plan-calculate');
  const mode = document.getElementById('plan-dollar-mode');
  const status = document.getElementById('plan-result-status');
  const fields = ['current_age', 'end_age', 'retirement_age', 'starting_assets', 'annual_savings',
    'annual_spending', 'annual_income', 'income_age', 'growth_rate', 'inflation_rate', 'expense_amount', 'expense_age'];
  const format = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 0});
  let calculated;
  let plans;
  let chart;
  let revision = 0;
  let controller;
  const dollars = (row, key) => row[key] / (mode.value === 'real' ? row.factor : 1);
  function render() {
    if (!calculated) return;
    const summary = document.getElementById('plan-summary');
    const table = document.getElementById('plan-table');
    summary.replaceChildren(); table.replaceChildren();
    const datasets = [];
    for (const [key, name, color] of [['baseline', 'Baseline', '#24634e'], ['comparison', 'What if', '#6686c4']]) {
      const result = calculated[key];
      const plan = plans[key];
      const card = document.createElement('article');
      const heading = document.createElement('h3'); heading.textContent = name;
      const assumptions = document.createElement('p');
      assumptions.className = 'plan-note';
      assumptions.textContent = `Retire at ${plan.retirement_age} · ${plan.growth_rate}% growth · ${format.format(plan.annual_savings)}/year saved · ${format.format(plan.annual_spending)}/year spent in retirement (today's dollars).`;
      const retirement = document.createElement('p');
      retirement.textContent = `${format.format(dollars(result.retirement, 'assets'))} at retirement · ${format.format(dollars(result.final, 'assets'))} at age ${result.final.age}`;
      const outcome = document.createElement('p');
      outcome.className = result.first_shortfall_age === null ? 'plan-funded' : 'plan-shortfall';
      outcome.textContent = result.first_shortfall_age === null
        ? `No unfunded spending before age ${result.final.age} under these assumptions.`
        : `First unfunded spending during age ${result.first_shortfall_age}–${result.first_shortfall_age + 1}. Total unfunded: ${format.format(result.total_real_shortfall)} in today's dollars.`;
      card.append(heading, assumptions, retirement, outcome); summary.append(card);
      datasets.push({label: name, data: result.rows.map(row => dollars(row, 'assets')), borderColor: color,
        backgroundColor: color, pointRadius: 0, borderWidth: 3, tension: 0});
    }
    for (let i = 0; i < calculated.baseline.rows.length; i++) {
      for (const [key, name] of [['baseline', 'Baseline'], ['comparison', 'What if']]) {
        const row = calculated[key].rows[i];
        const tr = document.createElement('tr');
        const values = [row.age, name, format.format(dollars(row, 'assets')), format.format(dollars(row, 'savings')),
          format.format(dollars(row, 'income')), format.format(dollars(row, 'spending') + dollars(row, 'expense')),
          format.format(dollars(row, 'shortfall'))];
        for (const value of values) { const td = document.createElement('td'); td.textContent = value; tr.append(td); }
        table.append(tr);
      }
    }
    chart?.destroy();
    chart = new Chart(document.getElementById('plan-chart'), {
      type: 'line', data: {labels: calculated.baseline.rows.map(row => row.age), datasets},
      options: {responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: 'index', intersect: false},
        scales: {x: {title: {display: true, text: 'Age'}}, y: {beginAtZero: true,
          title: {display: true, text: mode.value === 'real' ? "Today's dollars" : 'Future dollars'},
          ticks: {callback: value => new Intl.NumberFormat('en-US', {notation: 'compact', style: 'currency', currency: 'USD'}).format(value)}}},
        plugins: {tooltip: {callbacks: {label: item => `${item.dataset.label}: ${format.format(item.parsed.y)}`}}}}
    });
  }
  form.addEventListener('input', () => {
    revision++;
    if (calculated) status.textContent = 'Inputs changed. Compare again to update these results.';
  });
  mode.addEventListener('change', render);
  window.addEventListener('pagehide', () => {
    controller?.abort(); calculated = null; plans = null; chart?.destroy();
    document.getElementById('plan-summary').replaceChildren();
    document.getElementById('plan-table').replaceChildren();
    form.reset(); results.hidden = true;
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    const baseline = Object.fromEntries(fields.map(key => [key, Number(form.elements[key].value)]));
    const comparison = {...baseline,
      retirement_age: baseline.retirement_age + Number(form.elements.retirement_change.value),
      annual_savings: baseline.annual_savings + Number(form.elements.savings_change.value),
      annual_spending: baseline.annual_spending + Number(form.elements.spending_change.value),
      growth_rate: baseline.growth_rate + Number(form.elements.growth_change.value)};
    const submittedRevision = revision;
    button.disabled = true; error.hidden = true;
    controller = new AbortController();
    try {
      const response = await fetch('/api/projections', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.elements.csrf_token.value},
        body: JSON.stringify({baseline, comparison}), signal: controller.signal});
      if (response.status === 401) { window.location.assign('/login'); return; }
      const data = await response.json().catch(() => { throw new Error('Your session may have expired. Reload and try again.'); });
      if (!response.ok) throw new Error(data.error || 'The projection could not be calculated.');
      calculated = data; plans = {baseline, comparison}; results.hidden = false;
      status.textContent = revision === submittedRevision
        ? 'Calculated from the assumptions above. Both paths start with the same investments.'
        : 'Inputs changed. Compare again to update these results.';
      render(); results.scrollIntoView({behavior: 'smooth', block: 'start'});
    } catch (failure) {
      if (failure.name !== 'AbortError') { error.textContent = failure.message; error.hidden = false; }
    } finally { button.disabled = false; }
  });
})();
