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
    const payload = Object.fromEntries(['month','income_mode','income','version','since'].map(name => [name,field(name).value]));
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
  const history = JSON.parse(document.querySelector('#budget-history-data').textContent);
  const scope = JSON.parse(document.querySelector('#budget-scope-data').textContent);
  const colors = ['#24634e','#e7a944','#6686c4','#b76a79','#70a58b','#c19462','#8b6fac','#d68555'];
  const observed = row => row.records && !row.future && !row.foreign && !row.unknown;
  function openMonth(index) {
    const params = new URLSearchParams();
    for (const [key,value] of Object.entries(scope)) if (value) params.set(key,value);
    params.set('month',history[index].month);
    location.href = `/budget?${params}#category-patterns`;
  }
  if (typeof Chart !== 'undefined') {
    const chart = new Chart(document.querySelector('#budget-history-chart'), {
      type:'line', data:{labels:history.map(row=>row.month),datasets:[
        {label:'Spending',data:history.map(row=>observed(row) ? row.actual/100 : null),borderColor:'#24634e',backgroundColor:'#24634e14',fill:true},
        {label:'Income',data:history.map(row=>observed(row) ? row.deposited/100 : null),borderColor:'#6686c4'},
        {label:'Allowance',data:history.map(row=>row.plan ? row.allowance/100 : null),borderColor:'#b29449',borderDash:[5,4]},
        ...[3,6,12,24].map((window,index)=>({label:`${window} mo average`,data:history.map(row=>row[`ma_${window}`] === null ? null : row[`ma_${window}`]/100),borderColor:colors[index+1],borderDash:[2,3],hidden:window!==3,pointRadius:0}))
      ]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
        elements:{line:{borderWidth:2,tension:0.15},point:{radius:2,hitRadius:8}},
        onClick:(_,elements)=>{if(elements.length) openMonth(elements[0].index);},
        scales:{x:{grid:{display:false},ticks:{maxTicksLimit:8}},y:{beginAtZero:true,ticks:{callback:value=>money(value*100)}}},
        plugins:{legend:{labels:{boxWidth:12,font:{size:10},filter:item=>item.datasetIndex<3 && (item.datasetIndex!==2 || history.some(row=>row.plan))}},tooltip:{callbacks:{label:context=>`${context.dataset.label}: ${money(context.parsed.y*100)}`}}}}
    });
    for (const toggle of document.querySelectorAll('[data-average]')) toggle.addEventListener('change',()=>{
      chart.setDatasetVisibility(3+[3,6,12,24].indexOf(Number(toggle.dataset.average)),toggle.checked);
      chart.update();
    });
    const categories = history.at(-1).rows.filter(row=>row.actual>0).sort((a,b)=>b.actual-a.actual);
    new Chart(document.querySelector('#budget-mix-chart'), {
      type:'doughnut',data:{labels:categories.map(row=>row.category),datasets:[{data:categories.map(row=>row.actual/100),backgroundColor:categories.map((_,index)=>colors[index%colors.length]),borderWidth:2}]},
      options:{responsive:true,maintainAspectRatio:false,cutout:'72%',plugins:{legend:{display:false},tooltip:{callbacks:{label:context=>`${context.label}: ${money(context.parsed*100)}`}}},
        onClick:(_,elements)=>{if(elements.length){document.querySelector('#budget-category-search').value=categories[elements[0].index].category;filterCategories();document.querySelector('#category-patterns').scrollIntoView({behavior:'smooth'});}}}
    });
    const legend = document.querySelector('#budget-mix-legend');
    categories.slice(0,6).forEach((row,index)=>{
      const item=document.createElement('span'), dot=document.createElement('i');
      dot.style.backgroundColor=colors[index%colors.length]; item.append(dot,document.createTextNode(row.category)); legend.append(item);
    });
    if (!categories.length) legend.textContent='No positive net spending this month.';
  }
  function filterCategories() {
    const query=document.querySelector('#budget-category-search').value.trim().toLocaleLowerCase();
    const items=[...document.querySelectorAll('[data-category-row]')];
    for (const item of [...items,...document.querySelectorAll('[data-category-detail]')]) item.hidden=!item.dataset.category.toLocaleLowerCase().includes(query);
    document.querySelector('#budget-no-match').hidden=!items.length || items.some(item=>!item.hidden);
  }
  document.querySelector('#budget-category-search').addEventListener('input',filterCategories);
})();
