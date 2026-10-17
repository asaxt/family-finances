(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const form = $('plan-form');
  const defaults = JSON.parse($('plan-defaults').textContent);
  const latestSource = JSON.parse($('plan-data').textContent);
  const numeric = ['withdrawal_rate', 'starting_taxable', 'inflation_rate', 'growth_rate', 'tax_payments_in_spending'];
  const personNumbers = ['annual_income', 'tax_advantaged_rate', 'current_age', 'retirement_age', 'starting_pretax', 'starting_roth', 'work_state_percent'];
  const personText = ['name', 'contribution_type', 'residence_state', 'employment_state'];
  const profileFields = ['name', 'current_age', 'retirement_age', 'residence_state', 'employment_state', 'work_state_percent'];
  const balances = ['starting_pretax', 'starting_roth'];
  const currency = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 0});
  const compact = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', notation: 'compact'});
  const money = value => value == null ? '—' : currency.format(value);
  let references = structuredClone(defaults.references), source = structuredClone(latestSource), sourceId = null;
  let library = {version: '', plans: []}, activeId = '', savedSnapshot, baseline = null, baselineName = 'Baseline';
  let calculated = null, calculatedPlan, calculatedBaselineName = null, chart, controller, timer, revision = 0, dirty = false, ready = false, saving = false;
  const initialPlan = structuredClone(defaults.plan);
  const selectedChoice = i => defaults.choices.find(item => item.id === references.people[i].choice);
  const field = name => form.elements.namedItem(name);
  const number = name => field(name).value === '' ? null : Number(field(name).value);
  const csrf = () => field('csrf_token').value;
  const valid = () => [...form.elements].every(input => !input.willValidate || input.validity.valid);
  const tell = message => { $('plan-save-status').textContent = message; };
  const fail = message => { $('plan-error').textContent = message; $('plan-error').hidden = false; };
  const hasSpending = () => source.annual_spending !== null || number('monthly_lifestyle_spending') !== null;
  function readPlan() {
    return {...Object.fromEntries(numeric.map(key => [key, number(key)])),
      ...Object.fromEntries(['withdrawal_start', 'filing_status', 'mfs_allocation'].map(key => [key, field(key).value])),
      people: [0, 1].map(i => ({...Object.fromEntries(personNumbers.map(key => [key, number(`person_${i}_${key}`)])),
        ...Object.fromEntries(personText.map(key => [key, field(`person_${i}_${key}`).value]))})),
      annual_lifestyle_spending: number('monthly_lifestyle_spending') === null ? null : number('monthly_lifestyle_spending') * 12,
      life_events: [...$('plan-events-list').children].map(row => Object.fromEntries([...row.querySelectorAll('input')].map(input =>
        [input.dataset.eventField, input.type === 'number' ? (input.value === '' ? null : Number(input.value)) : input.value]))) };
  }
  function snapshot() { return {plan: readPlan(), references: structuredClone(references), source: structuredClone(source), sourceId, name: $('plan-name').value}; }
  function addEvent(event = {}) {
    if ($('plan-events-list').children.length >= 20) return;
    const row = document.createElement('div'); row.className = 'plan-event-row';
    for (const [key, label, type, value, min, max] of [
      ['name', 'Expense', 'text', event.name ?? '', null, null],
      ['start_year', 'First year', 'number', event.start_year ?? source.start_year, 2026, 2200],
      ['years', 'Years', 'number', event.years ?? 1, 1, 50],
      ['annual_amount', 'Cost / year ($)', 'number', event.annual_amount ?? '', 0, 100000000]]) {
      const wrapper = document.createElement('label'); wrapper.className = 'plan-field';
      const span = document.createElement('span'); span.textContent = label;
      const input = document.createElement('input'); input.type = type; input.dataset.eventField = key;
      input.value = value; input.required = true;
      if (type === 'number') { input.min = min; input.max = max; input.step = key === 'annual_amount' ? '0.01' : '1'; }
      else { input.maxLength = 60; input.placeholder = 'Name this expense'; }
      wrapper.append(span, input); row.append(wrapper);
    }
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'icon-button';
    remove.textContent = '×'; remove.setAttribute('aria-label', 'Remove this expense'); remove.title = 'Remove expense';
    remove.addEventListener('click', () => { row.remove(); changed(); }); row.append(remove);
    $('plan-events-list').append(row);
    return row;
  }
  function setPlan(plan) {
    for (const key of [...numeric, 'withdrawal_start', 'filing_status', 'mfs_allocation']) field(key).value = plan[key] ?? '';
    plan.people.forEach((person, i) => [...personNumbers, ...personText].forEach(key => field(`person_${i}_${key}`).value = person[key] ?? ''));
    field('monthly_lifestyle_spending').value = plan.annual_lifestyle_spending == null ? '' : Math.round(plan.annual_lifestyle_spending / 12 * 100) / 100;
    $('plan-events-list').replaceChildren(); (plan.life_events || []).forEach(addEvent);
    references.people.forEach((ref, i) => form.querySelector(`[data-profile-index="${i}"]`).value = ref.choice);
    sources();
  }
  function sources() {
    const separate = field('filing_status').value === 'separate';
    $('plan-separate-fields').hidden = !separate; field('mfs_allocation').required = separate;
    $('taxable-source').textContent = sourceId ? 'Saved starting balance. Refresh explicitly to use current Savings.' : `${references.taxable_from_savings ? 'Savings source' : 'Manual assumption'} · ${defaults.taxable.note}`;
    $('use-taxable').disabled = defaults.taxable.value === '';
    references.people.forEach((ref, i) => {
      const choice = selectedChoice(i);
      form.querySelector(`[data-use-profile="${i}"]`).disabled = !choice?.saved;
      form.querySelector(`[data-use-savings="${i}"]`).disabled = !choice;
      form.querySelector(`[data-person-source="${i}"]`).textContent = choice
        ? (sourceId ? 'Saved values. Refresh explicitly to use current people and Savings.' : `${ref.fields.length ? 'Connected source.' : 'Manual assumptions.'} Pre-tax: ${choice.balances.starting_pretax.note} Roth: ${choice.balances.starting_roth.note}`)
        : 'Choose a person or enter details. Blank balances are unknown, not zero.';
      const retireLabel = field(`person_${i}_retirement_age`).closest('label').querySelector('span');
      retireLabel.textContent = `${field(`person_${i}_name`).value || `Person ${i + 1}`} · retire at`;
    });
    const retirement = [0, 1].reduce((sum, i) => sum + (number(`person_${i}_starting_pretax`) || 0) + (number(`person_${i}_starting_roth`) || 0), 0);
    $('plan-starting-split').textContent = `Entered: ${money(retirement)} retirement + ${money(number('starting_taxable'))} taxable. Review any blank or stale sources.`;
    $('plan-source-status').textContent = `${sourceId ? 'Saved snapshot' : 'Current snapshot'} · starts ${source.start_year} · recorded spending ${money(source.annual_spending === null ? null : source.annual_spending / 12)} / month`;
    $('plan-spending-source').textContent = source.annual_spending === null ? 'Recorded spending is unavailable. Enter a monthly lifestyle assumption to explore a plan.'
      : `${money(source.annual_spending)} recorded over ${source.date_from} – ${source.date_to}. Income-tax payments entered below are removed before projecting lifestyle spending.`;
    $('plan-source-coverage').textContent = source.observed_months == null ? '' : `${source.observed_months} of 12 months have records in this snapshot.`;
    $('plan-source-warning').hidden = !source.unclassified;
  }
  function useDefaults(i, keys) {
    const choice = selectedChoice(i); if (!choice) return;
    const values = {...choice.values, ...Object.fromEntries(balances.map(key => [key, choice.balances[key].value]))};
    keys.forEach(key => { field(`person_${i}_${key}`).value = values[key] ?? ''; if (!references.people[i].fields.includes(key)) references.people[i].fields.push(key); });
  }
  function changed() {
    revision++; dirty = true; controller?.abort(); sources();
    $('plan-pin').disabled = true; $('plan-results').setAttribute('data-stale', 'true');
    if (calculated) $('plan-result-status').textContent = 'Updating draft — the results below are from the previous calculation.';
    tell('Unsaved draft · Save to keep it, or discard to return.');
    clearTimeout(timer);
    if ($('plan-auto').checked && ready) timer = setTimeout(() => calculate(false), 450);
  }
  form.addEventListener('input', event => {
    const name = event.target.name || '';
    if (name === 'starting_taxable') references.taxable_from_savings = false;
    const match = name.match(/^person_([01])_(.+)$/);
    if (match) references.people[Number(match[1])].fields = references.people[Number(match[1])].fields.filter(key => key !== match[2]);
    changed();
  });
  form.querySelectorAll('[data-profile-index]').forEach(select => select.addEventListener('change', () => {
    const i = Number(select.dataset.profileIndex);
    if (select.value && references.people[1 - i].choice === select.value) { select.value = references.people[i].choice; fail('Choose a different person to avoid counting balances twice.'); return; }
    references.people[i] = {choice: select.value, fields: []};
    useDefaults(i, [...profileFields, ...balances]); changed();
  }));
  form.querySelectorAll('[data-use-profile]').forEach(button => button.addEventListener('click', () => { useDefaults(Number(button.dataset.useProfile), profileFields); changed(); }));
  form.querySelectorAll('[data-use-savings]').forEach(button => button.addEventListener('click', () => { useDefaults(Number(button.dataset.useSavings), balances); changed(); }));
  $('use-taxable').addEventListener('click', () => { field('starting_taxable').value = defaults.taxable.value; references.taxable_from_savings = true; changed(); });
  $('plan-add-event').addEventListener('click', () => { const row = addEvent(); changed(); row?.querySelector('input').focus(); });
  $('plan-refresh-source').addEventListener('click', async () => {
    const started = revision; $('plan-refresh-source').disabled = true;
    try {
    const fresh = await json('/api/planning-sources');
    if (started !== revision) { tell('Inputs changed while sources refreshed. Try refresh again.'); return; }
    defaults.choices = fresh.choices; defaults.taxable = fresh.taxable; Object.assign(latestSource, fresh.source);
    sourceId = null; source = structuredClone(latestSource); baseline = null;
    references.people.forEach((ref, i) => useDefaults(i, [...ref.fields]));
    if (references.taxable_from_savings) field('starting_taxable').value = defaults.taxable.value;
    $('plan-unpin').hidden = true; $('plan-compare-library').value = ''; changed();
    } catch (error) { fail(error.message); } finally { $('plan-refresh-source').disabled = false; }
  });
  $('plan-open-inputs').addEventListener('click', () => { $('plan-people-section').open = true; validateVisible(); });
  function validateVisible() {
    const invalid = [...form.elements].find(input => input.willValidate && !input.validity.valid);
    if (invalid) { let parent = invalid.closest('details'); while (parent) { parent.open = true; parent = parent.parentElement.closest('details'); } invalid.reportValidity(); }
    return !invalid;
  }
  async function json(url, payload, signal) {
    const response = await fetch(url, payload ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf()}, body: JSON.stringify(payload), signal} : {signal});
    if (response.status === 401) { window.location.assign('/login'); throw new Error('Please unlock the app.'); }
    const data = await response.json().catch(() => { throw new Error('The request could not be completed. Please retry.'); });
    if (!response.ok) throw new Error(data.error || 'The request could not be completed.');
    return data;
  }
  async function calculate(explicit) {
    clearTimeout(timer);
    if (!valid() || !hasSpending()) {
      $('plan-live-status').textContent = 'Complete the missing inputs to calculate.';
      if (explicit) { validateVisible(); if (!hasSpending()) fail('Enter monthly lifestyle spending or import a complete spending history.'); }
      return;
    }
    controller?.abort(); controller = new AbortController(); const requestController = controller;
    const submittedRevision = revision, current = readPlan(), submittedBaseline = baseline ? structuredClone(baseline) : null, submittedBaselineName = baselineName;
    $('plan-live-status').textContent = 'Calculating…'; $('plan-error').hidden = true;
    try {
      const result = await json('/api/projections', {baseline: submittedBaseline || current, comparison: current, source_id: sourceId}, requestController.signal);
      if (submittedRevision !== revision || requestController.signal.aborted) return;
      calculated = result; calculatedPlan = current; calculatedBaselineName = submittedBaseline ? submittedBaselineName : null; source = result.source; sources();
      $('plan-results').hidden = false; $('plan-empty-state').hidden = true; $('plan-pin').disabled = false;
      $('plan-results').removeAttribute('data-stale'); $('plan-live-status').textContent = 'Up to date';
      $('plan-result-status').textContent = `${sourceId ? 'Saved' : 'Current'} spending snapshot · ${source.start_year} start · fixed-rate illustration`;
      render();
    } catch (error) { if (error.name !== 'AbortError') { fail(error.message); $('plan-live-status').textContent = 'Needs attention'; } }
  }
  form.addEventListener('submit', event => { event.preventDefault(); calculate(true); });
  $('plan-auto').addEventListener('change', () => { if ($('plan-auto').checked) calculate(false); });
  $('plan-pin').addEventListener('click', () => {
    if (!calculated || $('plan-pin').disabled) return;
    baseline = structuredClone(calculatedPlan); baselineName = $('plan-name').value || 'Baseline'; calculatedBaselineName = baselineName;
    calculated.baseline = calculated.comparison; $('plan-unpin').hidden = false;
    $('plan-result-status').textContent = 'Comparison kept. Change an input to see the difference.'; render();
  });
  $('plan-unpin').addEventListener('click', () => { revision++; controller?.abort(); baseline = null; calculatedBaselineName = null; $('plan-unpin').hidden = true; $('plan-compare-library').value = ''; if (calculated) render(); calculate(false); });
  $('plan-compare-library').addEventListener('change', () => {
    const item = library.plans.find(item => item.id === $('plan-compare-library').value); if (!item) return;
    baseline = structuredClone(item.plan); baselineName = item.name; $('plan-unpin').hidden = false;
    revision++; controller?.abort(); $('plan-pin').disabled = true; $('plan-results').setAttribute('data-stale', 'true'); $('plan-result-status').textContent = 'Updating comparison — previous results remain below.'; calculate(true);
  });
  const scenarios = () => calculatedBaselineName ? [['baseline', `${calculatedBaselineName} · baseline`], ['comparison', `${$('plan-name').value || 'Current plan'} · draft`]] : [['comparison', $('plan-name').value || 'Current plan']];
  const deflate = (row, value, flow = false) => value / ($('plan-dollar-mode').value === 'real' ? (flow ? row.cash_flow_factor : row.factor) : 1);
  function text(tag, value, className) { const node = document.createElement(tag); node.textContent = value; if (className) node.className = className; return node; }
  function render() {
    if (!calculated) return;
    const list = scenarios(), real = $('plan-dollar-mode').value === 'real';
    const previous = $('plan-monthly-year').value || String(calculated.comparison.first_retirement_monthly.year);
    $('plan-monthly-year').replaceChildren(...calculated.comparison.monthly.map(row => new Option(`${row.year} · ${row.ages.join(' / ')} yrs`, row.year)));
    $('plan-monthly-year').value = previous;
    if (!$('plan-monthly-year').value) $('plan-monthly-year').value = String(calculated.comparison.first_retirement_monthly.year);
    const colors = ['#0b7f89', '#d26738'];
    const datasets = list.map(([key, name], index) => ({label: name, data: calculated[key].rows.map(row => deflate(row, row.assets)), borderColor: colors[index], backgroundColor: colors[index], borderDash: key === 'baseline' ? [5, 4] : [], pointRadius: 0, borderWidth: 3, tension: 0}));
    if (chart) { chart.data.labels = calculated.comparison.rows.map(row => row.year); chart.data.datasets = datasets; chart.options.scales.y.title.text = real ? 'Today’s dollars' : 'Future dollars'; chart.update('none'); }
    else chart = new Chart($('plan-chart'), {type: 'line', data: {labels: calculated.comparison.rows.map(row => row.year), datasets}, options: {responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: 'index', intersect: false}, scales: {y: {beginAtZero: true, title: {display: true, text: real ? 'Today’s dollars' : 'Future dollars'}, ticks: {callback: value => compact.format(value)}}}, plugins: {tooltip: {callbacks: {label: item => `${item.dataset.label}: ${money(item.parsed.y)}`}}}}});
    $('plan-summary').replaceChildren();
    for (const [key, name] of list) {
      const result = calculated[key], card = text('article', '');
      card.append(text('h3', name), text('p', money(deflate(result.retirement, result.retirement.assets)), 'metric-value'), text('p', 'Investments when both retire', 'plan-note'));
      card.append(text('p', `${money(deflate(result.final, result.final.assets))} at the end of ${result.final.year - 1}`, 'plan-note'));
      card.append(text('p', result.first_funding_gap_year == null ? 'No modeled funding gap' : `First funding gap: ${result.first_funding_gap_year}`, result.first_funding_gap_year == null ? 'plan-funded' : 'plan-shortfall'));
      $('plan-summary').append(card);
    }
    $('plan-delta').hidden = !calculatedBaselineName;
    if (calculatedBaselineName) {
      const a = calculated.baseline, b = calculated.comparison;
      $('plan-delta').replaceChildren(text('strong', 'What changes?'));
      $('plan-delta').append(text('p', `${money(deflate(b.final, b.final.assets) - deflate(a.final, a.final.assets))} in final investments · ${money(b.total_real_funding_gap - a.total_real_funding_gap)} in total unfunded spending (today’s dollars).`));
      $('plan-delta').append(text('p', 'Both scenarios use the active spending snapshot and starting year. No-gap results do not establish that a plan is sustainable.', 'plan-note'));
    }
    renderMonthly(); renderTable();
  }
  function renderMonthly() {
    if (!calculated) return;
    $('plan-monthly-summary').replaceChildren();
    for (const [key, name] of scenarios()) {
      const row = calculated[key].monthly.find(row => row.year === Number($('plan-monthly-year').value)); if (!row) continue;
      const factor = $('plan-dollar-mode').value === 'real' ? row.factor : 1;
      const card = text('article', ''); card.append(text('h3', name), text('p', `${['Working', 'One retired', 'Both retired'][row.retired_count]} · ${row.year}`, 'plan-note'));
      const list = document.createElement('dl');
      for (const [label, amount] of [['Lifestyle / month', row.spending], ['Available after tax / month', row.available], ['Surplus or gap / month', row.difference], ['Planned expenses this year', row.event_spending * 12], ['Unfunded expenses this year', row.event_shortfall * 12]]) list.append(text('dt', label), text('dd', money(amount / factor)));
      card.append(list); $('plan-monthly-summary').append(card);
    }
  }
  function renderTable() {
    if (!calculated || !$('plan-table-details').open) { $('plan-table').replaceChildren(); return; }
    const fragment = document.createDocumentFragment();
    for (let index = 1; index < calculated.comparison.rows.length; index++) for (const [key, name] of scenarios()) {
      const row = calculated[key].rows[index], tr = document.createElement('tr');
      const values = [row.cash_flow_year, name, money(deflate(row, row.retirement_assets)), money(deflate(row, row.taxable_assets)), ...['income', 'taxes', 'spending', 'event_spending', 'event_shortfall', 'shortfall'].map(field => money(deflate(row, row[field], true)))];
      values.forEach(value => tr.append(text('td', value))); fragment.append(tr);
    }
    $('plan-table').replaceChildren(fragment);
  }
  $('plan-table-details').addEventListener('toggle', renderTable);
  $('plan-monthly-year').addEventListener('change', renderMonthly);
  $('plan-dollar-mode').addEventListener('change', render);
  function renderLibrary() {
    $('plan-library').replaceChildren(new Option('New plan', ''), ...library.plans.map(item => new Option(item.name, item.id)));
    $('plan-library').value = activeId;
    $('plan-compare-library').replaceChildren(new Option('Choose a plan', ''), ...library.plans.map(item => new Option(item.name, item.id)));
    $('workspace-delete').disabled = !activeId;
  }
  function load(item) {
    clearTimeout(timer); controller?.abort(); revision++; baseline = null; calculatedBaselineName = null; calculated = null;
    $('plan-unpin').hidden = true; $('plan-results').hidden = true; $('plan-empty-state').hidden = false;
    activeId = item?.id || ''; sourceId = item?.id || null; source = structuredClone(item?.source || latestSource);
    references = structuredClone(item?.references || defaults.references);
    $('plan-name').value = item?.name || ''; setPlan(item?.plan || initialPlan);
    savedSnapshot = snapshot(); dirty = false; renderLibrary();
    tell(item ? `Saved ${new Date(item.updated_at).toLocaleString()} · Snapshot kept until refreshed.` : 'New plan · Save when you are ready.');
    $('plan-error').hidden = true; calculate(false);
  }
  $('plan-library').addEventListener('change', () => {
    if (dirty && !window.confirm('Discard the unsaved edits and open another plan?')) { $('plan-library').value = activeId; return; }
    load(library.plans.find(item => item.id === $('plan-library').value));
  });
  $('plan-name').addEventListener('input', () => { dirty = true; tell('Unsaved name · Save to keep it.'); });
  $('workspace-discard').addEventListener('click', () => {
    if (!savedSnapshot) return;
    references = structuredClone(savedSnapshot.references); source = structuredClone(savedSnapshot.source); sourceId = savedSnapshot.sourceId;
    $('plan-name').value = savedSnapshot.name; setPlan(savedSnapshot.plan); baseline = null; $('plan-unpin').hidden = true;
    revision++; dirty = false; tell('Edits discarded.'); calculate(false);
  });
  async function save(copy) {
    if (saving || !ready) return;
    if (!validateVisible()) return;
    let name = $('plan-name').value.trim();
    if (!name) { $('plan-name').focus(); tell('Give this plan a name first.'); return; }
    if (copy && library.plans.some(item => item.name.toLowerCase() === name.toLowerCase())) {
      let counter = 2; while (library.plans.some(item => item.name.toLowerCase() === `${name} ${counter}`.toLowerCase())) counter++;
      name = `${name.slice(0, 55)} ${counter}`;
    }
    const submitted = snapshot(), savedRevision = revision;
    saving = true; ['workspace-save', 'workspace-copy', 'workspace-delete', 'workspace-discard', 'plan-library'].forEach(id => $(id).disabled = true);
    try {
      library = await json('/api/plans', {action: 'save', id: copy ? null : activeId || null, version: library.version, name, plan: submitted.plan, references: submitted.references, source_id: submitted.sourceId});
      activeId = library.active_id; const item = library.plans.find(item => item.id === activeId);
      savedSnapshot = {plan: structuredClone(item.plan), references: structuredClone(item.references), source: structuredClone(item.source), sourceId: item.id, name: item.name};
      if (revision === savedRevision && $('plan-name').value === submitted.name) { sourceId = item.id; source = structuredClone(item.source); $('plan-name').value = item.name; dirty = false; sources(); }
      renderLibrary(); if (calculated) render(); tell(dirty ? 'Saved the submitted version. Your newer edits are still a draft.' : 'Saved securely. You can return to this plan anytime.');
    } catch (error) { tell(error.message); }
    finally { saving = false; ['workspace-save', 'workspace-copy', 'workspace-discard', 'plan-library'].forEach(id => $(id).disabled = false); $('workspace-delete').disabled = !activeId; }
  }
  $('workspace-save').addEventListener('click', () => save(false));
  $('workspace-copy').addEventListener('click', () => save(true));
  $('workspace-delete').addEventListener('click', async () => {
    if (!activeId || saving || !window.confirm('Delete this saved plan? Your financial records will remain unchanged.')) return;
    saving = true;
    ['workspace-save', 'workspace-copy', 'workspace-delete', 'workspace-discard', 'plan-library'].forEach(id => $(id).disabled = true);
    try { library = await json('/api/plans', {action: 'delete', id: activeId, version: library.version}); load(null); tell('Saved plan deleted.'); }
    catch (error) { tell(error.message); } finally { saving = false; ['workspace-save', 'workspace-copy', 'workspace-discard', 'plan-library'].forEach(id => $(id).disabled = false); $('workspace-delete').disabled = !activeId; }
  });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('pagehide', () => { clearTimeout(timer); controller?.abort(); chart?.destroy(); calculated = null; });
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
  setPlan(initialPlan); savedSnapshot = snapshot();
  json('/api/plans').then(data => { library = data; ready = true; if (dirty) { renderLibrary(); tell('Saved plans loaded. Your draft edits were kept.'); calculate(false); } else load(library.plans.find(item => item.id === library.active_id)); })
    .catch(error => { tell(error.message); ready = true; $('workspace-save').disabled = true; $('workspace-copy').disabled = true; calculate(false); });
})();
