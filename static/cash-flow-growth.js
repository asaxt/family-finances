(() => {
  const canvas=document.querySelector('#cash-flow-growth-chart');
  if(!canvas || typeof Chart==='undefined')return;
  const growth=JSON.parse(document.querySelector('#cash-flow-growth-data').textContent);
  const money=value=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(value/100);
  const percent=value=>`${value>0?'+':''}${value.toFixed(1)}%`;
  new Chart(canvas,{
    type:'line',
    data:{labels:growth.points.map(point=>point.month),datasets:[
      {label:'Income change',data:growth.points.map(point=>point.income),borderColor:'#24634e',backgroundColor:'#24634e',metric:'income'},
      {label:'Expense change',data:growth.points.map(point=>point.spending),borderColor:'#c25760',backgroundColor:'#c25760',borderDash:[6,3],metric:'spending'}
    ]},
    options:{responsive:true,maintainAspectRatio:false,spanGaps:false,
      interaction:{mode:'index',intersect:false},
      elements:{line:{borderWidth:2,tension:0},point:{radius:3,hitRadius:8}},
      scales:{x:{grid:{display:false},ticks:{maxTicksLimit:12}},y:{beginAtZero:true,title:{display:true,text:'Change (%)'},ticks:{callback:value=>`${value}%`},grid:{color:context=>context.tick.value===0?'#82978c':'#edf0ed'}}},
      plugins:{legend:{labels:{boxWidth:14}},tooltip:{callbacks:{
        title:items=>{const point=growth.points[items[0].dataIndex];return [`${point.current.first} through ${point.current.last}`,`vs. ${point.prior.first} through ${point.prior.last}`];},
        label:item=>`${item.dataset.label}: ${percent(item.parsed.y)}`,
        afterLabel:item=>{const point=growth.points[item.dataIndex],key=item.dataset.metric;return `Current: ${money(point.current[key])} · Earlier: ${money(point.prior[key])}`;}
      }}}
    }
  });
})();
