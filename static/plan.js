(() => {
  const form = document.getElementById('plan-form');
  const results = document.getElementById('plan-results');
  const error = document.getElementById('plan-error');
  const button = document.getElementById('plan-calculate');
  const pin = document.getElementById('plan-pin');
  const unpin = document.getElementById('plan-unpin');
  const mode = document.getElementById('plan-dollar-mode');
  const status = document.getElementById('plan-result-status');
  const monthlyYear = document.getElementById('plan-monthly-year');
  const source = JSON.parse(document.getElementById('plan-data').textContent);
  const fields = ['starting_taxable', 'inflation_rate', 'growth_rate', 'tax_payments_in_spending'];
  const personNumbers = ['annual_income', 'tax_advantaged_rate', 'current_age', 'retirement_age',
    'starting_pretax', 'starting_roth', 'withdrawal_rate', 'work_state_percent'];
  const personText = ['name', 'contribution_type', 'residence_state', 'employment_state'];
  function readPlan() {
    const plan = Object.fromEntries(fields.map(key => [key, Number(form.elements[key].value)]));
    plan.filing_status = form.elements.filing_status.value;
    plan.mfs_allocation = form.elements.mfs_allocation.value;
    plan.people = [0, 1].map(index => Object.fromEntries([
      ...personNumbers.map(key => [key, Number(form.elements[`person_${index}_${key}`].value)]),
      ...personText.map(key => [key, form.elements[`person_${index}_${key}`].value])
    ]));
    return plan;
  }
  const format = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 0});
  let calculated, plans, baseline, chart, controller;
  let revision = 0;
  const dollars = (row, key) => row[key] / (mode.value === 'real' ? row.factor : 1);
  function updateDerived() {
    const separate = form.elements.filing_status.value === 'separate';
    document.getElementById('plan-separate-fields').hidden = !separate;
    form.elements.mfs_allocation.required = separate;
    const current = readPlan();
    const fresh = calculated && JSON.stringify(current) === JSON.stringify(plans.comparison);
    const result = fresh ? calculated.comparison : null;
    document.getElementById('plan-taxable-savings').textContent = result ? `${format.format(result.annual_taxable_savings)} / year` : 'Calculate to estimate';
    document.getElementById('plan-tax-advantaged').textContent = result
      ? `Retirement savings: ${format.format(result.annual_tax_advantaged_savings)} / year. Adjusted spending: ${format.format(result.adjusted_annual_spending)} / year.` : '';
    document.getElementById('plan-cash-flow-note').textContent = result && result.annual_taxable_savings < 0
      ? 'The negative remainder draws from brokerage; any uncovered amount becomes a funding gap.'
      : 'This is a cash-flow estimate, not a measurement of actual brokerage transfers.';
    const first = result?.rows[1];
    document.getElementById('plan-tax-summary').textContent = first
      ? `First-year modeled taxes: ${format.format(first.taxes)} — federal income ${format.format(first.federal_income_tax)}, federal payroll ${format.format(first.federal_payroll_tax)}, Oregon ${format.format(first.oregon_tax)}, Washington ${format.format(first.washington_tax)}. Interstate credits included: ${format.format(first.interstate_credit)}.`
      : 'Calculate to see federal income, federal payroll, Oregon, and Washington tax estimates.';
    const retirement = current.people.reduce((total, person) => total + person.starting_pretax + person.starting_roth, 0);
    document.getElementById('plan-starting-split').textContent = `Entered starting investments: ${format.format(retirement)} retirement + ${format.format(current.starting_taxable)} brokerage. Verify the ownership allocation above.`;
  }
  function render() {
    if (!calculated) return;
    const summary = document.getElementById('plan-summary');
    const table = document.getElementById('plan-table');
    summary.replaceChildren(); table.replaceChildren();
    const comparing = baseline && JSON.stringify(plans.baseline) !== JSON.stringify(plans.comparison);
    const scenarios = comparing ? [['baseline', 'Baseline'], ['comparison', 'Current plan']] : [['comparison', 'Current plan']];
    const selectedYear = monthlyYear.value || String(calculated.comparison.first_retirement_monthly.year);
    monthlyYear.replaceChildren();
    for (const month of calculated.comparison.monthly) {
      const option = document.createElement('option');
      option.value = String(month.year);
      option.textContent = `${month.year} · ages ${month.ages.join(' & ')} · ${['Neither retired', 'One retired', 'Both retired'][month.retired_count]}`;
      monthlyYear.append(option);
    }
    monthlyYear.value = selectedYear;
    renderMonthly(scenarios);
    const datasets = [];
    for (const [key, name] of scenarios) {
      const result = calculated[key], plan = plans[key];
      const card = document.createElement('article');
      const heading = document.createElement('h3'); heading.textContent = name;
      const assumptions = document.createElement('p'); assumptions.className = 'plan-note';
      assumptions.textContent = `${plan.filing_status === 'joint' ? 'Joint return' : 'Separate returns'} · ${plan.people.map(person => `${person.name}: retire at ${person.retirement_age}, save ${person.tax_advantaged_rate}%`).join(' · ')} · ${plan.growth_rate}% growth · ${plan.inflation_rate}% inflation.`;
      const retirement = document.createElement('p');
      retirement.textContent = `When both have retired: ${format.format(dollars(result.retirement, 'retirement_assets'))} retirement + ${format.format(dollars(result.retirement, 'taxable_assets'))} taxable. Total in ${result.final.year}: ${format.format(dollars(result.final, 'assets'))}.`;
      const outcome = document.createElement('p');
      outcome.className = result.first_shortfall_year === null ? 'plan-funded' : 'plan-shortfall';
      outcome.textContent = result.first_shortfall_year === null
        ? `No funding gap through ${result.final.year - 1} under these assumptions.`
        : `First funding gap during ${result.first_shortfall_year}. Total unfunded: ${format.format(result.total_real_shortfall)} in today's dollars.`;
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
        const values = [`${row.year} / ${row.ages.join(' & ')}`, name, ...['retirement_assets', 'taxable_assets', 'income', 'tax_advantaged_savings',
          'withdrawal', 'federal_income_tax', 'federal_payroll_tax', 'oregon_tax', 'washington_tax', 'interstate_credit', 'spending', 'taxable_cash_flow', 'shortfall'].map(field => format.format(dollars(row, field)))];
        for (const value of values) { const td = document.createElement('td'); td.textContent = value; tr.append(td); }
        table.append(tr);
      }
    }
    chart?.destroy();
    chart = new Chart(document.getElementById('plan-chart'), {
      type: 'line', data: {labels: calculated.comparison.rows.map(row => row.year), datasets},
      options: {responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: 'index', intersect: false},
        scales: {x: {title: {display: true, text: 'Calendar year'}}, y: {beginAtZero: true,
          title: {display: true, text: mode.value === 'real' ? "Today's dollars" : 'Future dollars'},
          ticks: {callback: value => new Intl.NumberFormat('en-US', {notation: 'compact', style: 'currency', currency: 'USD'}).format(value)}}},
        plugins: {tooltip: {callbacks: {label: item => `${item.dataset.label}: ${format.format(item.parsed.y)}`}}}}
    });
  }
  function renderMonthly(scenarios) {
    if (!calculated) return;
    if (!Array.isArray(scenarios)) {
      const comparing = baseline && JSON.stringify(plans.baseline) !== JSON.stringify(plans.comparison);
      scenarios = comparing ? [['baseline', 'Baseline'], ['comparison', 'Current plan']] : [['comparison', 'Current plan']];
    }
    const summary = document.getElementById('plan-monthly-summary');
    summary.replaceChildren();
    for (const [key, name] of scenarios) {
      const result = calculated[key];
      const month = result.monthly.find(row => row.year === Number(monthlyYear.value));
      if (!month) continue;
      const factor = mode.value === 'real' ? month.factor : 1;
      const money = value => `${format.format(value)} / month`;
      const card = document.createElement('article');
      const heading = document.createElement('h3'); heading.textContent = `${name} · ${month.year}`;
      const note = document.createElement('p'); note.className = 'plan-note';
      note.textContent = `${['Neither spouse retired', 'One spouse retired', 'Both spouses retired'][month.retired_count]} · ages ${month.ages.join(' & ')}. Projection amounts are ${mode.value === 'real' ? "in today's purchasing power" : 'in future dollars'}.`;
      const list = document.createElement('dl');
      const items = [
        ['Current comparable spending (today’s dollars)', result.adjusted_annual_spending / 12],
        ['Needed to maintain today’s lifestyle', month.spending / factor],
        ['Available after tax from income & retirement withdrawals', month.available / factor],
        ['Difference before brokerage top-up', month.difference / factor],
        ['Planned brokerage top-up', month.brokerage_draw / factor],
        ['Unfunded spending gap', month.shortfall / factor]
      ];
      for (const [label, amount] of items) {
        const term = document.createElement('dt'); term.textContent = label;
        const value = document.createElement('dd'); value.textContent = money(amount);
        list.append(term, value);
      }
      const adjustment = document.createElement('p'); adjustment.className = 'plan-note';
      adjustment.textContent = `Current recorded average: ${money(result.annual_spending / 12)}. Comparable spending removes income-tax payments already counted in that history. Modeled taxes in the selected year: ${money(month.taxes / factor)}, already deducted from available spending.`;
      card.append(heading, note, list, adjustment); summary.append(card);
    }
  }
  monthlyYear.addEventListener('change', renderMonthly);
  form.addEventListener('input', () => {
    revision++; updateDerived(); pin.disabled = true;
    if (calculated) status.textContent = 'Inputs changed. Calculate again to update these results.';
  });
  pin.addEventListener('click', () => {
    if (!calculated || pin.disabled) return;
    baseline = structuredClone(plans.comparison); plans.baseline = structuredClone(baseline); calculated.baseline = calculated.comparison;
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
    document.getElementById('plan-monthly-summary').replaceChildren(); monthlyYear.replaceChildren(); monthlyYear.value = '';
    form.reset(); results.hidden = true; unpin.hidden = true; error.hidden = true; updateDerived();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    if (!form.reportValidity()) return;
    const current = readPlan();
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
      if (source.annual_spending !== data.comparison.annual_spending
          || source.date_from !== data.spending_date_from || source.date_to !== data.spending_date_to) {
        throw new Error('Your source data changed. Reload Plan to review the updated spending estimate.');
      }
      calculated = data; plans = submitted; results.hidden = false;
      monthlyYear.value = '';
      updateDerived();
      pin.disabled = revision !== submittedRevision;
      status.textContent = pin.disabled ? 'Inputs changed. Calculate again to update these results.'
        : `Calculated using spending from ${data.spending_date_from} through ${data.spending_date_to}.`;
      render(); results.scrollIntoView({behavior: 'smooth', block: 'start'});
    } catch (failure) {
      if (failure.name !== 'AbortError') { error.textContent = failure.message; error.hidden = false; }
    } finally { button.disabled = source.annual_spending === null; }
  });
  const save = document.getElementById('plan-save');
  save?.addEventListener('click', async () => {
    if (!form.reportValidity()) return;
    const saveStatus = document.getElementById('plan-save-status');
    const savedRevision = revision;
    save.disabled = true;
    try {
      const response = await fetch('/api/plan-settings', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.elements.csrf_token.value},
        body: JSON.stringify(readPlan())});
      if (response.status === 401) { window.location.assign('/login'); return; }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not save. Reload and try again.');
      saveStatus.textContent = savedRevision === revision ? 'Household and assumptions saved in your encrypted vault.' : 'Earlier inputs saved. Your latest edits have not been saved.';
    } catch (failure) { saveStatus.textContent = failure.message; }
    finally { save.disabled = false; }
  });
  updateDerived();
})();
