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
    if(!planned && field('recorded_income').value===''){target.textContent='Income for this effective month is outside the current report. Percentage allowances will use each month’s recorded income.';target.classList.remove('budget-negative');return;}
    target.textContent = `Income base: ${money(income)} · Category allowances: ${money(total)} · Brokerage remainder: ${money(income-total)}${planned ? '' : ' (illustration using the selected month’s recorded income)'}`;
    target.classList.toggle('budget-negative', total > income);
  }
  form.addEventListener('input', preview);
  form.addEventListener('change', preview);
  const plans=JSON.parse(document.querySelector('#budget-plan-data').textContent);
  const incomes=JSON.parse(document.querySelector('#budget-income-data').textContent);
  const timelineStart=document.querySelector('#budget-timeline-start');
  const draft=document.querySelector('#budget-draft');
  const reviewPanel=document.querySelector('#budget-replacement-review');
  let pending=null;
  const monthNumber=value=>Number(value.slice(0,4))*12+Number(value.slice(5))-1;
  const monthAt=number=>`${Math.floor(number/12)}-${String(number%12+1).padStart(2,'0')}`;
  const setting=value=>!value ? 'No allowance' : value[0]==='recorded' ? 'Recorded income' : value[0]==='percent' ? `${value[1]/100}%` : money(value[1]);
  function savedPlan(target) {
    const key=Object.keys(plans).sort().filter(key=>key<=target).at(-1);
    return {key,plan:plans[key]};
  }
  function setIncome() {
    field('recorded_income').value=incomes[monthNumber(field('month').value)-monthNumber(field('since').value)] ?? '';
    preview();
  }
  function renderTimeline() {
    if(!timelineStart.validity.valid || !timelineStart.value) return;
    const months=Array.from({length:12},(_,index)=>monthAt(monthNumber(timelineStart.value)+index));
    const table=document.createElement('table'),head=table.createTHead().insertRow();
    const heading=document.createElement('th');heading.textContent='Saved settings';head.append(heading);
    for(const target of months) {
      const cell=document.createElement('th'),button=document.createElement('button');
      button.type='button';button.className='text-link';button.textContent=target;button.title=`Load saved plan for ${target}`;
      button.addEventListener('click',()=>{
        const {key,plan}=savedPlan(target);
        field('month').value=target;field('income_mode').value=plan?.income_mode ?? 'recorded';field('income').value=plan?.income==null?'':plan.income/100;
        for(const row of rows){const line=plan?.lines.find(item=>item.category===row.querySelector('[name=category]').value);row.querySelector('[name=kind]').value=line?.kind ?? 'percent';row.querySelector('[name=value]').value=line?line.value/100:'';}
        document.querySelector('#budget-draft-source').textContent=`Draft loaded for ${target}${key ? ` from plan saved ${key}` : ' with no saved plan'}.`;
        pending=null;reviewPanel.hidden=true;draft.hidden=false;setIncome();
      });
      cell.append(button);head.append(cell);
    }
    const body=table.createTBody();
    const names=rows.map(row=>row.querySelector('[name=category]').value);
    for(const name of [null,...names]) {
      const tr=body.insertRow(),label=document.createElement('th');label.scope='row';label.textContent=name ?? 'Income base';tr.append(label);
      for(const target of months) {
        const {plan}=savedPlan(target),line=plan?.lines.find(item=>item.category===name);
        tr.insertCell().textContent=name===null ? (!plan?'No plan':setting([plan.income_mode==='recorded'?'recorded':'amount',plan.income])) : line?setting([line.kind,line.value]):'—';
      }
    }
    document.querySelector('#budget-plan-timeline').replaceChildren(table);
  }
  timelineStart.addEventListener('change',renderTimeline);
  field('month').addEventListener('change',setIncome);
  document.querySelector('#budget-use-start').addEventListener('click',()=>{field('month').value=field('since').value;setIncome();});
  async function send(payload) {
    const response=await fetch(form.getAttribute('action'),{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':field('csrf_token').value},body:JSON.stringify(payload)});
    const result=await response.json();
    if(!response.ok) throw new Error(result.error || 'The budget could not be saved. Reload and try again.');
    return result;
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button=form.querySelector('button[type=submit]'),status=document.querySelector('#budget-save-status');
    const payload=Object.fromEntries(['month','income_mode','income','version','since','granularity'].map(name=>[name,field(name).value]));
    // Earlier effective dates also extend accumulation so the result includes the change.
    if(payload.month<payload.since) payload.since=payload.month;
    payload.lines=rows.map(row=>({category:row.querySelector('[name=category]').value,kind:row.querySelector('[name=kind]').value,value:row.querySelector('[name=value]').value})).filter(row=>row.kind!=='none' && row.value.trim()!=='');
    button.disabled=true;status.textContent='Checking saved settings…';
    try {
      const {review}=await send({...payload,preview:true});
      pending={...payload,confirmation:review.confirmation};
      document.querySelector('#budget-review-summary').textContent=`Apply this entire plan from ${payload.month} onward. Months before it stay unchanged. ${review.conflict?'Existing settings will be overwritten.':'Review the settings below.'} ${review.replaced_starts.length?`Saved plan dates replaced: ${review.replaced_starts.join(', ')}.`:''}`;
      const content=document.querySelector('#budget-review-changes');content.replaceChildren();
      for(const interval of review.intervals) {
        const title=document.createElement('h4');title.textContent=`${interval.first} ${interval.last?`through ${interval.last}`:'onward'}${interval.source?` · previously from ${interval.source}`:' · no saved plan'}`;content.append(title);
        if(!interval.changes.length){const text=document.createElement('p');text.textContent='Same settings; these continue from the new effective month.';content.append(text);continue;}
        const table=document.createElement('table'),head=table.createTHead().insertRow();
        for(const text of ['Setting','Saved','Proposed']){const th=document.createElement('th');th.textContent=text;head.append(th);}
        const body=table.createTBody();
        for(const change of interval.changes){const tr=body.insertRow();for(const text of [change.category,setting(change.before),setting(change.after)])tr.insertCell().textContent=text;}
        content.append(table);
      }
      document.querySelector('#budget-review-status').textContent='';status.textContent='';draft.hidden=true;reviewPanel.hidden=false;document.querySelector('#budget-review-title').focus();
    }catch(error){status.textContent=error.message;}finally{button.disabled=false;}
  });
  document.querySelector('#budget-revise').addEventListener('click',()=>{pending=null;reviewPanel.hidden=true;draft.hidden=false;field('month').focus();});
  document.querySelector('#budget-cancel').addEventListener('click',()=>{pending=null;form.reset();reviewPanel.hidden=true;draft.hidden=false;renderTimeline();setIncome();document.querySelector('#budget-draft-source').textContent='Draft reset to the settings loaded when this page opened.';dialog.close();});
  document.querySelector('#budget-apply').addEventListener('click',async event=>{
    if(!pending)return;
    event.target.disabled=true;
    try{const result=await send(pending);location.href=result.url;}
    catch(error){document.querySelector('#budget-review-status').textContent=error.message;}
    finally{event.target.disabled=false;}
  });
  renderTimeline();
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
