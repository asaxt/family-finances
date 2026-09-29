(() => {
  const money = cents => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(cents / 100);
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
      type:'line',data:{labels:history.map(row=>row.label),datasets:[{label:'Monthly spending',data:allValues,borderColor:'#24634e',backgroundColor:'#24634e14',fill:false},...[3,12].map((window,index)=>({label:`${window}-month average`,data:[],borderColor:colors[index+1],borderDash:[5,3],pointRadius:0}))]},
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
    const averages=Object.fromEntries([3,12].map(window=>[window,selected?selected[`ma_${window}`]:history.map(row=>row[`ma_${window}`])]));
    if(categoryChart){
      categoryChart.data.datasets[0].label=`${label} · monthly`;categoryChart.data.datasets[0].data=values;
      [3,12].forEach((window,index)=>{categoryChart.data.datasets[index+1].data=averages[window].map(value=>value===null?null:value/100);});categoryChart.update();
    }
    for(const cell of document.querySelectorAll('[data-category-ma]')) {const value=averages[cell.dataset.categoryMa][Number(cell.dataset.index)];cell.textContent=value===null?'—':money(value);}
    document.querySelector('#category-values-heading').textContent=label;
    for(const cell of document.querySelectorAll('[data-category-value]')) {
      const value=values[Number(cell.dataset.categoryValue)]; cell.textContent=value===null ? '—' : money(value*100);
    }
  }
  for(const toggle of document.querySelectorAll('[data-category-average]')) toggle.addEventListener('change',()=>{
    if(categoryChart){categoryChart.setDatasetVisibility(1+[3,12].indexOf(Number(toggle.dataset.categoryAverage)),toggle.checked);categoryChart.update();}
  });
  select.addEventListener('change',showCategory);
  showCategory();
})();
