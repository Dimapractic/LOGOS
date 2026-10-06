let state = null, selected = null, submitting = false, orderId = null;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names = {reserve:'Резерв товара',book:'Назначение машины',plan:'Подтверждение ERP',ready:'Готовность склада',load:'Отгрузка со склада',depart:'Отправление машины',arrive:'Прибытие',complete:'Передача получателю',cancel_tms:'Отмена перевозки',cancel_wms:'Снятие резерва',cancel_erp:'Отмена в ERP'};
const states = {pending:'В работе',retry:'Повтор запроса',dead:'Требуется решение',done:'Доставлен',cancelled:'Отменён'};
const objectStates = {reserved:'Зарезервирован',ready:'Готов к отгрузке',consumed:'Отгружен',booked:'Машина назначена',departed:'В пути',arrived:'Прибыл',cancelled:'Отменён'};
const date = t => t ? new Date(t * 1000).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—';
const timeOnly = t => new Date(t * 1000).toLocaleTimeString('ru-RU');
function badge(text, type=''){return `<span class="badge ${type}">${esc(text)}</span>`;}
function statusBadge(j){return badge(states[j.state],j.state==='dead'?'bad':j.state==='retry'?'warn':j.state==='cancelled'?'neutral':'');}
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,5000);}
async function api(path, body){
  const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await r.json();
  if(!r.ok) throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));
  return data;
}
function metric(label,value,sub){return `<article class="metric"><span>${label}</span><strong>${value ?? '—'}</strong><small>${sub}</small></article>`;}
function table(headers,rows){return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.join(''):`<tr><td colspan="${headers.length}" class="empty">Пока нет данных</td></tr>`}</tbody></table></div>`;}
function forecast(j){
 const o=j.order,p=j.plan,a=j.actual;
 if(j.state==='done') return a.complete;
 if(a.depart) return Math.max(o.window_start,a.depart+o.travel_min*60);
 const ready=a.ready || Math.max(p.ready,(a.reserve||state.clock)+o.picking_min*60,state.clock);
 return Math.max(o.window_start,Math.max(p.depart,ready+o.loading_min*60,state.clock)+o.travel_min*60);
}
function render(){
 $('#clock').textContent=date(state.clock);$('#clock-toggle').textContent=state.running?'Ⅱ Пауза':'▶ Запустить время';
 if(document.activeElement!==$('#speed')) $('#speed').value=state.speed;
 $('#worker-error').hidden=!state.worker_error;$('#worker-error').textContent=state.worker_error||'';
 const active=state.jobs.filter(j=>!['done','cancelled'].includes(j.state)).length;
 const problems=state.jobs.filter(j=>j.state==='dead').length,m=state.metrics;
 $('#metrics').innerHTML=metric('Заказов в работе',active,'из '+state.jobs.length+' заказов')+metric('Соблюдение SLA',m.sla_pct===null?'—':m.sla_pct+'%','по HTTP-командам')+metric('Доставлено в окно',m.in_window+'/'+m.delivered,'по модельному времени')+metric('Требуют решения',problems,'после остановки операции');
 $('#monitor-metrics').innerHTML=metric('HTTP-попыток',m.requests,'весь журнал')+metric('Успешные попытки',m.success_pct===null?'—':m.success_pct+'%','включая повторы')+metric('Повторных попыток',m.retries,'автоматические в цикле')+metric('В пределах SLA',m.sla_pct===null?'—':m.sla_pct+'%','ошибки не считаются успехом');
 const desc={erp:'Заказы и статусы выполнения',wms:'Остатки, резервы и отгрузка',tms:'Машины и планирование перевозок'};
 $('#systems').innerHTML=Object.entries(state.systems).map(([key,s])=>`<article class="system"><b>${key.toUpperCase()}</b>${badge(s.online?'Доступен':'Нет связи',s.online?'':'bad')}<p>${desc[key]}</p><small>${s.online?`Задержка ${s.data.fault.delay_ms} мс · отказов ${s.data.fault.fail_next} · потерь ${s.data.fault.lost_next}`:'Проверьте процесс сервиса'}</small></article>`).join('');
 const filter=$('#filter').value;
 const jobs=[...state.jobs].reverse().filter(j=>filter==='all'||(filter==='active'&&!['done','cancelled'].includes(j.state))||j.state===filter);
 $('#order-count').textContent=state.jobs.length;
 $('#orders').innerHTML=jobs.length?jobs.map(j=>`<tr><td><b>${esc(j.order.customer)}</b><small>${esc(j.order.id.slice(0,8))}</small></td><td>${esc(j.order.sku)}<small>${j.order.quantity} шт.</small></td><td>${date(j.order.window_start)}<small>до ${date(j.order.window_end)}</small></td><td>${names[j.stage]}${!['done','cancelled'].includes(j.state)&&forecast(j)>j.order.window_end?'<small class="danger">Риск опоздания</small>':''}</td><td>${statusBadge(j)}</td><td><button data-detail="${esc(j.order.id)}">Открыть ↗</button></td></tr>`).join(''):'<tr><td colspan="6" class="empty"><strong>Начните с первого заказа</strong>Создайте доставку, чтобы увидеть обмен между системами.</td></tr>';
 const wms=state.systems.wms.data,tms=state.systems.tms.data;
 $('#stock').innerHTML=wms?table(['Код','Товар','Доступно, шт.'],wms.products.map(p=>`<tr><td>${esc(p.sku)}</td><td>${esc(p.name)}</td><td><b>${p.stock}</b></td></tr>`)):'Склад недоступен';
 $('#reservations').innerHTML=wms?table(['Заказ','Товар','Количество','Статус'],Object.entries(wms.reservations).map(([id,r])=>`<tr><td>${esc(id.slice(0,8))}</td><td>${esc(r.sku)}</td><td>${r.quantity}</td><td>${badge(objectStates[r.status])}</td></tr>`)):'Склад недоступен';
 $('#shipments').innerHTML=tms?table(['Заказ','Машина','Плановая подача','До окончания интервала','Статус'],Object.entries(tms.shipments).map(([id,r])=>`<tr><td>${esc(id.slice(0,8))}</td><td>${esc(r.vehicle)}</td><td>${date(r.begin)}</td><td>${date(r.end)}</td><td>${badge(objectStates[r.status])}</td></tr>`)):'Транспортная система недоступна';
 $('#logs').innerHTML=state.logs.length?state.logs.map(l=>`<tr title="${esc(l.detail)}"><td>${timeOnly(l.at)}</td><td>${esc(l.order_id.slice(0,8))}</td><td><b>${l.service.toUpperCase()}</b><small>${names[l.stage]}</small></td><td>${l.attempt}</td><td>${badge(l.code||'Тайм-аут / сеть',l.success?'':'bad')}</td><td>${l.elapsed_ms} мс</td><td>${badge(l.sla_ok?'В норме':'Нарушен',l.sla_ok?'':'warn')}</td></tr>`).join(''):'<tr><td colspan="7" class="empty">Команды ещё не отправлялись</td></tr>';
 if(selected&&$('#detail-dialog').open) renderDetail();
}
function renderDetail(){
 const j=state.jobs.find(j=>j.order.id===selected);if(!j)return;
 const pairs=[['Начало сборки',j.plan.start,null],['Готовность склада',j.plan.ready,j.actual.ready],['Отправление',j.plan.depart,j.actual.depart],['Прибытие',j.plan.arrival,j.actual.arrive],['Передача получателю',j.plan.delivery,j.actual.complete]];
 const logs=state.logs.filter(l=>l.order_id===selected).slice(0,8);
 $('#detail').innerHTML=`<div class="detail-top"><b>${esc(j.order.customer)}</b><p>${esc(j.order.sku)} · ${j.order.quantity} шт. · ${esc(j.order.id.slice(0,8))}</p>${statusBadge(j)} <span class="muted">${names[j.stage]}</span></div><p>Окно: <b>${date(j.order.window_start)} — ${date(j.order.window_end)}</b></p>${j.state!=='cancelled'?`<p>Ожидаемая передача: <b>${date(forecast(j))}</b> ${forecast(j)>j.order.window_end?badge('Опоздание','warn'):badge('В пределах окна')}</p>`:''}${j.error?`<div class="error">${esc(j.error)}</div>`:''}<table class="detail-table"><thead><tr><th>Этап</th><th>План</th><th>Факт</th></tr></thead><tbody>${pairs.map(([n,p,a])=>`<tr><td>${n}</td><td>${date(p)}</td><td>${date(a)}</td></tr>`).join('')}</tbody></table><h3>Последние попытки</h3><div class="timeline">${logs.map(l=>`<div class="${l.success?'done':''}"><b>${names[l.stage]}</b> · ${l.elapsed_ms} мс<small>Попытка ${l.attempt} · HTTP ${l.code||'нет ответа'} · ${esc(l.detail||'Успешно')}</small></div>`).join('')||'<p class="muted">Ожидание первой команды</p>'}</div><div class="detail-actions">${j.state==='dead'?`<button class="primary" data-action="retry" data-id="${esc(selected)}">Повторить операцию</button>`:''}${['reserve','book','plan','ready'].includes(j.stage)&&!['done','cancelled'].includes(j.state)?`<button class="danger" data-action="cancel" data-id="${esc(selected)}">Отменить заказ</button>`:''}</div>`;
}
async function refresh(){try{state=await api('/api/state');$('#connection').textContent='● Соединение установлено';render();}catch(e){$('#connection').textContent='● Нет связи с приложением';}finally{setTimeout(refresh,1200);}}
const titles={overview:['Центр управления','От заказа до доставки — одна прозрачная цепочка.'],warehouse:['Управление складом','Резервирование, готовность и отгрузка товаров.'],transport:['Транспорт и доставка','Плановые интервалы и статусы перевозок.'],monitor:['Мониторинг SLA','Время ответа, ошибки и повторные попытки.'],faults:['Сценарии сбоев','Проверь устойчивость интеграции к ошибкам.']};
document.addEventListener('click',async e=>{
 const nav=e.target.closest('[data-page]');if(nav){document.querySelectorAll('.page').forEach(p=>p.hidden=p.id!==nav.dataset.page);document.querySelectorAll('[data-page]').forEach(n=>n.classList.toggle('active',n===nav));$('#page-title').textContent=titles[nav.dataset.page][0];$('#subtitle').textContent=titles[nav.dataset.page][1];}
 const close=e.target.closest('[data-close]');if(close)$('#'+close.dataset.close).close();
 const detail=e.target.closest('[data-detail]');if(detail){selected=detail.dataset.detail;renderDetail();$('#detail-dialog').showModal();}
 const action=e.target.closest('[data-action]');if(action){action.disabled=true;try{await api(`/api/jobs/${encodeURIComponent(action.dataset.id)}/${action.dataset.action}`,{});toast('Операция поставлена в очередь');}catch(e){toast(e.message);}finally{action.disabled=false;}}
});
function localInput(ts){const d=new Date(ts*1000);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
$('#new-order').onclick=()=>{if(!state)return;$('#order-form').reset();orderId=crypto.randomUUID();$('#order-form').elements.window_start.value=localInput(state.clock+10800);$('#order-form').elements.window_end.value=localInput(state.clock+11700);$('#order-error').hidden=true;$('#order-dialog').showModal();};
$('#order-form').onsubmit=async e=>{e.preventDefault();if(submitting)return;submitting=true;const button=e.submitter;button.disabled=true;const form=new FormData(e.target),data=Object.fromEntries(form);data.id=orderId;['quantity','picking_min','loading_min','travel_min','buffer_min'].forEach(k=>data[k]=Number(data[k]));['window_start','window_end'].forEach(k=>data[k]=new Date(data[k]).getTime()/1000);try{await api('/api/orders',data);$('#order-dialog').close();toast('Заказ создан. Интеграция запущена.');}catch(e){$('#order-error').hidden=false;$('#order-error').textContent=e.message;}finally{submitting=false;button.disabled=false;}};
$('#clock-toggle').onclick=async()=>{if(!state)return;try{await api('/api/clock',{running:!state.running,speed:Number($('#speed').value)});}catch(e){toast(e.message);}};
$('#speed').onchange=async()=>{if(state)try{await api('/api/clock',{running:state.running,speed:Number($('#speed').value)});}catch(e){toast(e.message);}};
$('#filter').onchange=()=>state&&render();
$('#fault-form').onsubmit=async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.target));const service=data.service;delete data.service;Object.keys(data).forEach(k=>data[k]=Number(data[k]));try{await api('/api/fault/'+service,data);toast('Настройки сбоев применены к '+service.toUpperCase());}catch(e){toast(e.message);}};
$('#clear-faults').onclick=async()=>{try{for(const s of ['erp','wms','tms'])await api('/api/fault/'+s,{delay_ms:0,fail_next:0,lost_next:0});$('#fault-form').reset();toast('Все сбои отключены');}catch(e){toast(e.message);}};
refresh();
