(() => {
  const form = document.querySelector('#budget-plan-form');
  const dialog = document.querySelector('#budget-editor');
  document.querySelector('#open-budget-editor')?.addEventListener('click', () => dialog.showModal());
  document.querySelector('#close-budget-editor').addEventListener('click', () => dialog.close());
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
      value.required = false;
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
    const payload = Object.fromEntries(['month','income_mode','income','version','since','granularity'].map(name => [name,field(name).value]));
    payload.lines = rows.map(row => ({category:row.querySelector('[name=category]').value,kind:row.querySelector('[name=kind]').value,value:row.querySelector('[name=value]').value})).filter(row => row.kind !== 'none' && row.value.trim() !== '');
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
  const granularity = document.querySelector('#spending-granularity');
  granularity.addEventListener('change',()=>{
    const quarterly=granularity.value==='quarter';
    const quarterField=document.querySelector('#spending-quarter-field');
    const monthField=document.querySelector('#spending-month-field');
    quarterField.hidden=!quarterly; quarterField.querySelector('select').disabled=!quarterly;
    monthField.hidden=quarterly; monthField.querySelector('input').disabled=quarterly;
  });
  const spending = JSON.parse(document.querySelector('#budget-history-data').textContent);
  const history = spending.points;
  const scope = JSON.parse(document.querySelector('#budget-scope-data').textContent);
  const colors = ['#24634e','#e7a944','#6686c4','#b76a79','#70a58b','#c19462','#8b6fac','#d68555'];
  const select = document.querySelector('#category-trend-select');
  const allValues=history.map(row=>row.records ? row.actual/100 : null);
  function openTransactions(index, category='') {
    const params = new URLSearchParams({purpose:'spending',date_from:history[index].date_from,date_to:history[index].date_to});
    for (const key of ['person','account']) if (scope[key]) params.set(key,scope[key]);
    if (category) params.set('category',category);
    location.href = `/transactions?${params}`;
  }
  const chartOptions = {
    responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
    elements:{line:{borderWidth:2,tension:0.15},point:{radius:3,hitRadius:8}},
    scales:{x:{grid:{display:false},ticks:{maxTicksLimit:10}},y:{beginAtZero:true,ticks:{callback:value=>money(value*100)}}},
    plugins:{legend:{labels:{boxWidth:12,font:{size:10}}},tooltip:{callbacks:{
      title:items=>{const row=history[items[0].dataIndex];return `${row.label}${row.partial?' · In progress':row.incomplete?' · Partial history':''}`;},
      label:context=>`${context.dataset.label}: ${money(context.parsed.y*100)}`
    }}}
  };
  let categoryChart;
  if (typeof Chart !== 'undefined') {
    const chart = new Chart(document.querySelector('#budget-history-chart'), {
      type:'line',data:{labels:history.map(row=>row.label),datasets:[
        {label:'Actual spending',data:allValues,borderColor:'#24634e',backgroundColor:'#24634e14',fill:true},
        ...spending.windows.map((window,index)=>({label:`${window} ${scope.granularity==='quarter'?'quarter':'month'} average`,data:history.map(row=>row[`ma_${window}`]===null ? null : row[`ma_${window}`]/100),borderColor:colors[index+1],borderDash:[2,3],hidden:index!==0,pointRadius:0}))
      ]},options:{...chartOptions,onClick:(_,elements)=>{if(elements.length) openTransactions(elements[0].index);}}
    });
    for (const toggle of document.querySelectorAll('[data-average]')) toggle.addEventListener('change',()=>{
      chart.setDatasetVisibility(1+spending.windows.indexOf(Number(toggle.dataset.average)),toggle.checked); chart.update();
    });
    categoryChart = new Chart(document.querySelector('#category-trend-chart'), {
      type:'line',data:{labels:history.map(row=>row.label),datasets:[{label:'All spending categories',data:allValues,borderColor:'#24634e',backgroundColor:'#24634e14',fill:true}]},
      options:{...chartOptions,onClick:(_,elements)=>{if(elements.length) openTransactions(elements[0].index,select.value);}}
    });
    const positive = spending.categories.filter(row=>row.total>0);
    new Chart(document.querySelector('#budget-mix-chart'), {
      type:'doughnut',data:{labels:positive.map(row=>row.name),datasets:[{data:positive.map(row=>row.total/100),backgroundColor:positive.map((_,index)=>colors[index%colors.length]),borderWidth:2}]},
      options:{responsive:true,maintainAspectRatio:false,cutout:'72%',plugins:{legend:{display:false},tooltip:{callbacks:{label:context=>`${context.label}: ${money(context.parsed*100)}`}}},
        onClick:(_,elements)=>{if(elements.length){select.value=positive[elements[0].index].name;showCategory();document.querySelector('.spending-category-trend').scrollIntoView({behavior:'smooth'});}}}
    });
    const legend=document.querySelector('#budget-mix-legend');
    positive.slice(0,6).forEach((row,index)=>{
      const item=document.createElement('span'),dot=document.createElement('i');
      dot.style.backgroundColor=colors[index%colors.length];item.append(dot,document.createTextNode(row.name));legend.append(item);
    });
    if(!positive.length) legend.textContent='No positive net spending in the recorded history.';
  }
  function showCategory() {
    const selected=spending.categories.find(row=>row.name===select.value);
    const values=selected ? selected.values.map(value=>value===null ? null : value/100) : allValues;
    const label=selected ? selected.name : 'All spending categories';
    if(categoryChart){categoryChart.data.datasets[0].label=label;categoryChart.data.datasets[0].data=values;categoryChart.update();}
    document.querySelector('#category-values-heading').textContent=label;
    for(const cell of document.querySelectorAll('[data-category-value]')) {
      const value=values[Number(cell.dataset.categoryValue)]; cell.textContent=value===null ? '—' : money(value*100);
    }
  }
  select.addEventListener('change',showCategory);
  showCategory();
  function filterCategories() {
    const query=document.querySelector('#budget-category-search').value.trim().toLocaleLowerCase();
    const items=[...document.querySelectorAll('[data-category-row]')];
    for (const item of [...items,...document.querySelectorAll('[data-category-detail]')]) item.hidden=!item.dataset.category.toLocaleLowerCase().includes(query);
    document.querySelector('#budget-no-match').hidden=!items.length || items.some(item=>!item.hidden);
  }
  document.querySelector('#budget-category-search').addEventListener('input',filterCategories);
})();
