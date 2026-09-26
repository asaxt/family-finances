(() => {
  const form = document.getElementById('plan-form');
  const results = document.getElementById('plan-results');
  const error = document.getElementById('plan-error');
  const button = document.getElementById('plan-calculate');
  const pin = document.getElementById('plan-pin');
  const unpin = document.getElementById('plan-unpin');
  const mode = document.getElementById('plan-dollar-mode');
  const status = document.getElementById('plan-result-status');
  const source = JSON.parse(document.getElementById('plan-data').textContent);
  const fields = ['annual_income', 'tax_advantaged_rate', 'withdrawal_rate', 'retirement_age',
    'current_age', 'starting_assets', 'inflation_rate', 'growth_rate'];
  const format = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 0});
  let calculated, plans, baseline, chart, controller;
  let revision = 0;
  const dollars = (row, key) => row[key] / (mode.value === 'real' ? row.factor : 1);
  function updateDerived() {
    const incomeField = form.elements.annual_income;
    const rateField = form.elements.tax_advantaged_rate;
    const income = Number(incomeField.value), rate = Number(rateField.value);
    const known = incomeField.value !== '' && rateField.value !== '' && incomeField.validity.valid && rateField.validity.valid;
    const taxSaving = income * rate / 100;
    const surplus = known && source.annual_spending !== null ? income - taxSaving - source.annual_spending : null;
    document.getElementById('plan-taxable-savings').textContent = surplus === null ? '—' : `${format.format(surplus)} / year`;
    document.getElementById('plan-tax-advantaged').textContent = known ? `Tax-advantaged savings: ${format.format(taxSaving)} / year (${rate}% of income).` : 'Enter income and a saving percentage to calculate.';
    document.getElementById('plan-cash-flow-note').textContent = surplus !== null && surplus < 0
      ? 'Your contributions and spending exceed income. The negative remainder draws from taxable investments; any uncovered amount becomes a funding gap.'
      : 'The remainder is a calculated cash-flow estimate, not a measurement of actual brokerage transfers. Income and contributions stop at retirement.';
    const assets = form.elements.starting_assets;
    document.getElementById('plan-starting-split').textContent = assets.value !== '' && assets.validity.valid
      ? `Starting split: ${format.format(Number(assets.value) * source.retirement_share)} retirement · ${format.format(Number(assets.value) * (1 - source.retirement_share))} taxable.`
      : 'Enter starting investments to see the split.';
  }
  function render() {
    if (!calculated) return;
    const summary = document.getElementById('plan-summary');
    const table = document.getElementById('plan-table');
    summary.replaceChildren(); table.replaceChildren();
    const comparing = baseline && JSON.stringify(plans.baseline) !== JSON.stringify(plans.comparison);
    const scenarios = comparing ? [['baseline', 'Baseline'], ['comparison', 'Current plan']] : [['comparison', 'Current plan']];
    const datasets = [];
    for (const [key, name] of scenarios) {
      const result = calculated[key], plan = plans[key];
      const card = document.createElement('article');
      const heading = document.createElement('h3'); heading.textContent = name;
      const assumptions = document.createElement('p'); assumptions.className = 'plan-note';
      assumptions.textContent = `Retire at ${plan.retirement_age} · ${plan.tax_advantaged_rate}% saved · ${plan.withdrawal_rate}% withdrawn each retirement year · ${plan.growth_rate}% growth · ${plan.inflation_rate}% inflation.`;
      const retirement = document.createElement('p');
      retirement.textContent = `At retirement: ${format.format(dollars(result.retirement, 'retirement_assets'))} retirement + ${format.format(dollars(result.retirement, 'taxable_assets'))} taxable. Total at age ${result.final.age}: ${format.format(dollars(result.final, 'assets'))}.`;
      const outcome = document.createElement('p');
      outcome.className = result.first_shortfall_age === null ? 'plan-funded' : 'plan-shortfall';
      outcome.textContent = result.first_shortfall_age === null
        ? `No funding gap before age ${result.final.age} under these assumptions.`
        : `First funding gap during age ${result.first_shortfall_age}–${result.first_shortfall_age + 1}. Total unfunded: ${format.format(result.total_real_shortfall)} in today's dollars.`;
      card.append(heading, assumptions, retirement, outcome); summary.append(card);
      for (const [field, label, color] of [['retirement_assets', 'Retirement', '#24634e'], ['taxable_assets', 'Taxable', '#6686c4']]) {
        datasets.push({label: `${name} · ${label}`, data: result.rows.map(row => dollars(row, field)),
          borderColor: color, backgroundColor: color, borderDash: key === 'baseline' ? [6, 4] : [],
          pointRadius: 0, borderWidth: 3, tension: 0});
      }
    }
    for (let i = 0; i < calculated.comparison.rows.length; i++) {
      for (const [key, name] of scenarios) {
        const row = calculated[key].rows[i], tr = document.createElement('tr');
        const values = [row.age, name, ...['retirement_assets', 'taxable_assets', 'income', 'tax_advantaged_savings',
          'withdrawal', 'spending', 'taxable_cash_flow', 'shortfall'].map(field => format.format(dollars(row, field)))];
        for (const value of values) { const td = document.createElement('td'); td.textContent = value; tr.append(td); }
        table.append(tr);
      }
    }
    chart?.destroy();
    chart = new Chart(document.getElementById('plan-chart'), {
      type: 'line', data: {labels: calculated.comparison.rows.map(row => row.age), datasets},
      options: {responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: 'index', intersect: false},
        scales: {x: {title: {display: true, text: 'Age'}}, y: {beginAtZero: true,
          title: {display: true, text: mode.value === 'real' ? "Today's dollars" : 'Future dollars'},
          ticks: {callback: value => new Intl.NumberFormat('en-US', {notation: 'compact', style: 'currency', currency: 'USD'}).format(value)}}},
        plugins: {tooltip: {callbacks: {label: item => `${item.dataset.label}: ${format.format(item.parsed.y)}`}}}}
    });
  }
  form.addEventListener('input', () => {
    revision++; updateDerived(); pin.disabled = true;
    if (calculated) status.textContent = 'Inputs changed. Calculate again to update these results.';
  });
  pin.addEventListener('click', () => {
    if (!calculated || pin.disabled) return;
    baseline = {...plans.comparison}; plans.baseline = {...baseline}; calculated.baseline = calculated.comparison;
    unpin.hidden = false; status.textContent = 'Baseline kept in this page. Edit inputs above and calculate to compare.'; render();
  });
  unpin.addEventListener('click', () => {
    baseline = null; unpin.hidden = true; render();
    status.textContent = pin.disabled ? 'Inputs changed. Calculate again to update these results.' : 'Comparison cleared. Showing the last calculated plan.';
  });
  mode.addEventListener('change', render);
  window.addEventListener('pagehide', () => {
    controller?.abort(); calculated = null; plans = null; baseline = null; chart?.destroy();
    document.getElementById('plan-summary').replaceChildren(); document.getElementById('plan-table').replaceChildren();
    form.reset(); results.hidden = true; unpin.hidden = true; error.hidden = true; updateDerived();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    const current = Object.fromEntries(fields.map(key => [key, Number(form.elements[key].value)]));
    const submitted = {baseline: baseline || current, comparison: current};
    const submittedRevision = revision;
    button.disabled = true; pin.disabled = true; error.hidden = true;
    controller = new AbortController();
    try {
      const response = await fetch('/api/projections', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.elements.csrf_token.value},
        body: JSON.stringify(submitted), signal: controller.signal});
      if (response.status === 401) { window.location.assign('/login'); return; }
      const data = await response.json().catch(() => { throw new Error('Your session may have expired. Reload and try again.'); });
      if (!response.ok) throw new Error(data.error || 'The projection could not be calculated.');
      if (source.annual_spending !== data.comparison.annual_spending || source.retirement_share !== data.retirement_share
          || source.date_from !== data.spending_date_from || source.date_to !== data.spending_date_to) {
        throw new Error('Your source data changed. Reload Plan to review the updated spending estimate and investment split.');
      }
      calculated = data; plans = submitted; results.hidden = false;
      updateDerived();
      pin.disabled = revision !== submittedRevision;
      status.textContent = pin.disabled ? 'Inputs changed. Calculate again to update these results.'
        : `Calculated using spending from ${data.spending_date_from} through ${data.spending_date_to}.`;
      render(); results.scrollIntoView({behavior: 'smooth', block: 'start'});
    } catch (failure) {
      if (failure.name !== 'AbortError') { error.textContent = failure.message; error.hidden = false; }
    } finally { button.disabled = source.annual_spending === null; }
  });
  updateDerived();
})();
