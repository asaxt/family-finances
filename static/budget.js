(() => {
  const form = document.querySelector('#budget-plan-form');
  const rows = [...form.querySelectorAll('[data-budget-line]')];
  const field = name => form.elements.namedItem(name);
  const money = cents => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(cents / 100);
  function preview() {
    const planned = field('income_mode').value === 'planned';
    document.querySelector('#budget-income-label').hidden = !planned;
    field('income').disabled = !planned;
    field('income').required = planned;
    const income = planned ? Math.round(Number(field('income').value) * 100) : Number(field('recorded_income').value);
    let total = 0;
    for (const row of rows) {
      const kind = row.querySelector('[name=kind]').value;
      const value = row.querySelector('[name=value]');
      value.disabled = kind === 'none';
      value.required = kind !== 'none';
      value.max = kind === 'percent' ? '100' : '1000000000';
      if (kind === 'amount') total += Math.round(Number(value.value) * 100);
      if (kind === 'percent') total += Math.round(Math.max(0,income) * Math.round(Number(value.value) * 100) / 10000);
    }
    const target = document.querySelector('#budget-preview');
    target.textContent = `Income base: ${money(income)} · Category allowances: ${money(total)} · Brokerage remainder: ${money(income-total)}${planned ? '' : ' (illustration using the selected month’s recorded income)'}`;
    target.classList.toggle('budget-negative', total > income);
  }
  form.addEventListener('input', preview);
  form.addEventListener('change', preview);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button[type=submit]');
    const status = document.querySelector('#budget-save-status');
    const payload = Object.fromEntries(['month','income_mode','income','version','since'].map(name => [name,field(name).value]));
    payload.lines = rows.map(row => ({category:row.querySelector('[name=category]').value,kind:row.querySelector('[name=kind]').value,value:row.querySelector('[name=value]').value})).filter(row => row.kind !== 'none');
    button.disabled = true;
    status.textContent = 'Saving…';
    try {
      const response = await fetch(form.getAttribute('action'), {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':field('csrf_token').value},body:JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'The budget could not be saved. Reload and try again.');
      location.href = result.url;
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
  preview();
  const history = JSON.parse(document.querySelector('#budget-history-data').textContent);
  const chart = typeof Chart !== 'undefined' ? new Chart(document.querySelector('#budget-history-chart'), {
    type:'bar',data:{labels:history.map(row=>row.month),datasets:[
      {label:'Category allowance',data:history.map(row=>row.plan ? row.allowance/100 : null),backgroundColor:'#c6e681'},
      {label:'Recorded spending',data:history.map(row=>row.future || !row.records ? null : row.actual/100),backgroundColor:'#245346'}
    ]},options:{responsive:true,maintainAspectRatio:false,scales:{y:{beginAtZero:true,ticks:{callback:value=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:0}).format(value)}}},plugins:{tooltip:{callbacks:{label:context=>`${context.dataset.label}: ${money(context.parsed.y*100)}`}}}}}
  ) : null;
  document.querySelector('#budget-chart-category').addEventListener('change', event => {
    if (!chart) return;
    const category = event.target.value;
    const selected = row => category ? row.rows.find(line => line.category === category) : row;
    chart.data.datasets[0].data = history.map(row => row.plan ? (selected(row)?.allowance || 0) / 100 : null);
    chart.data.datasets[1].data = history.map(row => row.future || !row.records ? null : (selected(row)?.actual || 0) / 100);
    chart.update();
  });
})();
