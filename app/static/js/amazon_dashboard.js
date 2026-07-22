// ─────────────────────────────────────────────────────────────────────────────
// Dashboard Amazon — JS (Ventas + Productos + Salud)
// ─────────────────────────────────────────────────────────────────────────────

var amzChartInstance = null;
var amzDailyGoal = 50000;
var amzOpsCurrentFilter = 'all';   // filtro activo en Operaciones
// Seller ID de la cuenta activa — usado en TODOS los fetch para evitar mezclar cuentas

// ─── Banner de error Amazon con botón de reconexión automático ───────────────
function _amzIsAuthError(msg) {
    return msg && (
        msg.indexOf('403') !== -1 ||
        msg.indexOf('401') !== -1 ||
        msg.indexOf('Unauthorized') !== -1 ||
        msg.indexOf('LWA') !== -1 ||
        msg.indexOf('expired') !== -1 ||
        msg.indexOf('invalid_client') !== -1
    );
}
function _amzErrorBanner(msg) {
    msg = (msg || '').substring(0, 200);
    if (_amzIsAuthError(msg)) {
        return '<div class="col-span-4 flex flex-col items-center gap-3 py-6 px-4">' +
            '<div class="flex items-center gap-2 text-red-600 font-semibold text-sm">' +
            '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">' +
            '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" ' +
            'd="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>' +
            'Token Amazon expirado — reconexion requerida</div>' +
            '<p class="text-xs text-gray-500 text-center max-w-md">' + msg + '</p>' +
            '<button onclick="fixAmazonToken(this)" ' +
            'class="bg-orange-500 hover:bg-orange-600 text-white font-bold px-5 py-2.5 rounded-lg text-sm transition">' +
            'Reconectar Amazon ahora →</button>' +
            '<p class="text-[10px] text-gray-400" id="amazon-fix-msg">Presiona para reconectar usando credenciales guardadas</p>' +
            '<div class="w-full max-w-lg mt-3 border-t border-gray-100 pt-3">' +
            '<p class="text-[11px] text-gray-500 font-semibold mb-1">¿El botón no funciona? Pega el token manualmente:</p>' +
            '<p class="text-[10px] text-gray-400 mb-2">Seller Central MX → Apps y Servicios → Desarrollar apps → Autorizar app → copiar Refresh Token</p>' +
            '<textarea id="amz-manual-token" rows="3" placeholder="Atzr|IwEBIA..." ' +
            'class="w-full text-xs border border-gray-200 rounded-lg p-2 font-mono resize-none focus:outline-none focus:border-orange-400"></textarea>' +
            '<button onclick="saveManualAmazonToken()" ' +
            'class="mt-2 w-full bg-gray-700 hover:bg-gray-800 text-white font-bold px-4 py-2 rounded-lg text-xs transition">' +
            'Guardar token manualmente</button>' +
            '<p class="text-[10px] text-gray-400 mt-1" id="amz-manual-msg"></p>' +
            '</div>' +
            '</div>';
    }
    return '<div class="col-span-4 text-center py-4">' +
        '<p class="text-amber-600 text-sm">⚠️ API Amazon no disponible: ' + msg + '</p>' +
        '<button onclick="loadAmazonData()" class="mt-2 text-xs text-orange-500 underline">Reintentar</button>' +
        '</div>';
}
var amzTabLoaded = { dashboard: amzActiveTab === 'dashboard', ventas: amzActiveTab === 'ventas', salud: false, operaciones: amzActiveTab === 'operaciones', finanzas: amzActiveTab === 'finanzas', fba: false, listings: false, deals: false };

// Usa fecha LOCAL (no UTC de toISOString) para que "Hoy" coincida con
// la fecha del usuario aunque sea después de las 18h CST (medianoche UTC)
function toDateStr(d) {
    var mm = d.getMonth() + 1, dd = d.getDate();
    return d.getFullYear() + '-' + (mm < 10 ? '0' : '') + mm + '-' + (dd < 10 ? '0' : '') + dd;
}

function fmtMoney(n) {
    if (n == null || n === undefined) return '—';
    return n.toLocaleString('es-MX', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function fmtCompact(n) {
    if (n == null) return '—';
    if (n >= 1000000) return '$' + (n/1000000).toFixed(1) + 'M';
    if (n >= 1000)    return '$' + (n/1000).toFixed(1) + 'K';
    return '$' + fmtMoney(n);
}

// ─── Paginación genérica (compartida con tab Deals) ──────────────────────────
// _renderPaginated definida más abajo en el bloque TAB DEALS

// ─── Tab navigation ───────────────────────────────────────────────────────────
function switchAmzTab(tabName) {
    ['dashboard', 'ventas', 'salud', 'operaciones', 'finanzas', 'fba', 'listings', 'deals'].forEach(function(t) {
        var el = document.getElementById('amz-tab-' + t);
        if (el) el.classList.toggle('hidden', t !== tabName);
    });
    document.querySelectorAll('.amz-tab-btn').forEach(function(btn) {
        var active = btn.getAttribute('data-tab') === tabName;
        btn.classList.toggle('border-orange-500', active);
        btn.classList.toggle('text-orange-600',  active);
        btn.classList.toggle('font-semibold',    active);
        btn.classList.toggle('border-transparent', !active);
        btn.classList.toggle('text-gray-500',    !active);
        btn.classList.toggle('font-medium',      !active);
    });

    amzActiveTab = tabName;
    if (!amzTabLoaded[tabName]) {
        amzTabLoaded[tabName] = true;
        if (tabName === 'dashboard') loadAmazonDashboard();
        else if (tabName === 'salud') loadAmzSaludTab();
        else if (tabName === 'operaciones') loadAmzOperacionesTab();
        else if (tabName === 'finanzas') loadAmzFinanzasTab();
        else if (tabName === 'fba') loadFbaTab();
        else if (tabName === 'listings') loadListingsTab();
        else if (tabName === 'deals') loadDealsTab();
        else if (tabName === 'ventas') { loadAmzBriefing(); loadAmzRecentOrders(); loadTopProducts(); }
    } else if (tabName === 'ventas') {
        loadAmzBriefing();
    }
}

// ─── Range helpers ────────────────────────────────────────────────────────────
function setRange(days) {
    var end = new Date(), start = new Date();
    start.setDate(start.getDate() - (days - 1));
    document.getElementById('amz_date_from').value = toDateStr(start);
    document.getElementById('amz_date_to').value   = toDateStr(end);
    var labels = {7:'Últimos 7 días', 15:'Últimos 15 días', 30:'Últimos 30 días', 90:'Últimos 3 meses'};
    var periodEl = document.getElementById('amz-period-label');
    if (periodEl) periodEl.textContent = labels[days] || 'Periodo personalizado';
}

function highlightRangeBtn(btn) {
    document.querySelectorAll('.amz-range-btn').forEach(function(b) {
        b.classList.remove('bg-orange-400', 'text-white', 'font-bold', 'border-orange-400');
        b.classList.add('border-gray-300', 'text-gray-600');
        b.style.background = '';
    });
    if (btn) {
        btn.classList.add('bg-orange-400', 'text-white', 'font-bold', 'border-orange-400');
        btn.classList.remove('border-gray-300', 'text-gray-600');
    }
}

function getDateParams() {
    var df = document.getElementById('amz_date_from').value;
    var dt = document.getElementById('amz_date_to').value;
    var p = [];
    if (df) p.push('date_from=' + df);
    if (dt) p.push('date_to=' + dt);
    if (window.amzActiveSellerId) p.push('seller_id=' + window.amzActiveSellerId);
    return p.join('&');
}

// ─── TAB VENTAS ───────────────────────────────────────────────────────────────

function _trendArrow(pct) {
    if (pct == null || isNaN(pct)) return '';
    var abs = Math.abs(pct);
    if (abs <= 2) return '<span class="text-gray-400 text-xs ml-1">→ 0%</span>';
    var sign = pct > 0 ? '+' : '−';
    var color = pct > 0 ? 'text-green-500' : 'text-red-500';
    var arrow = pct > 0 ? '↑' : '↓';
    return '<span class="' + color + ' text-xs ml-1 font-semibold">' + arrow + ' ' + sign + abs.toFixed(1) + '%</span>';
}

function renderAmazonMetrics(m) {
    var udsXOrden = m.total_orders > 0 ? (m.total_units / m.total_orders).toFixed(1) : '—';
    var listingsVal = (m.active_listings != null) ? m.active_listings : '—';
    var trend = m.trend || {};
    var cards = [
        { label: 'Órdenes del Período', value: m.total_orders,
          icon: '📦', color: '#F97316', hint: udsXOrden + ' uds/orden en promedio', trendPct: trend.orders_pct },
        { label: 'Unidades Ordenadas',  value: m.total_units,
          icon: '📊', color: '#EA580C', hint: m.total_orders > 0 ? udsXOrden + ' uds por orden' : '—', trendPct: trend.units_pct },
        { label: 'Ventas del Período',  value: '$' + fmtMoney(m.total_revenue),
          icon: '💰', color: '#C2410C', hint: m.total_orders > 0 ? '$' + fmtMoney(m.avg_per_order) + ' por orden' : '—', trendPct: trend.revenue_pct },
        { label: 'Listings Activos',    value: listingsVal,
          icon: '🏷️', color: '#9A3412', hint: 'Productos activos en Amazon MX', trendPct: null },
        { label: 'Neto Est. (−15%)',    value: '$' + fmtMoney(m.net_revenue_est || 0),
          icon: '📉', color: '#7C3AED', hint: 'Revenue menos comisión Amazon ~15%', trendPct: trend.revenue_pct }
    ];
    var html = '<div class="grid grid-cols-2 md:grid-cols-5 gap-3 md:gap-4">';
    cards.forEach(function(c) {
        html += '<div class="bg-white rounded-xl shadow p-4 md:p-5 border-b-4" style="border-bottom-color:'+c.color+'">' +
            '<div class="flex items-center justify-between mb-2"><span class="text-xs text-gray-400 font-medium">'+c.label+'</span><span class="text-xl">'+c.icon+'</span></div>' +
            '<p class="text-2xl md:text-3xl font-extrabold" style="color:'+c.color+'">'+c.value+'</p>' +
            '<p class="text-xs mt-2 text-gray-400">'+c.hint+_trendArrow(c.trendPct)+'</p></div>';
    });
    html += '</div>';
    document.getElementById('amz-metric-cards').innerHTML = html;
}

function renderAmazonChart(chartObj) {
    var loading = document.getElementById('amz-chart-loading');
    var canvas  = document.getElementById('amzSalesChart');
    loading.classList.add('hidden');
    canvas.classList.remove('hidden');
    if (amzChartInstance) { amzChartInstance.destroy(); amzChartInstance = null; }

    var data = chartObj.data, group_by = chartObj.group_by;
    var months = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'];
    var labels = data.map(function(d) {
        var p = d.date.split('-');
        return group_by === 'month' ? months[parseInt(p[1])-1]+' '+p[0] : parseInt(p[2])+'/'+months[parseInt(p[1])-1];
    });
    var df = document.getElementById('amz_date_from').value;
    var dt = document.getElementById('amz_date_to').value;
    document.getElementById('amz-chart-title').textContent    = 'Ventas '+(group_by==='month'?'Mensuales':'Diarias')+' · '+df+' – '+dt;
    document.getElementById('amz-chart-subtitle').textContent = data.reduce(function(s,d){return s+d.orders;},0)+' órdenes · $'+fmtMoney(data.reduce(function(s,d){return s+d.revenue;},0))+' revenue';

    amzChartInstance = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                { label: 'Órdenes', data: data.map(function(d){return d.orders;}),
                  backgroundColor: 'rgba(249,115,22,0.75)', borderColor: '#F97316', borderWidth: 1, borderRadius: 4, yAxisID: 'y', order: 2 },
                { label: 'Revenue ($)', data: data.map(function(d){return d.revenue;}),
                  type: 'line', borderColor: '#3B82F6', backgroundColor: 'rgba(59,130,246,0.06)',
                  fill: true, tension: 0.35, pointRadius: 3, pointBackgroundColor: '#3B82F6', borderWidth: 2, yAxisID: 'y1', order: 1 }
            ]
        },
        options: {
            responsive: true, interaction: {mode:'index',intersect:false},
            plugins: { legend: {display:false}, tooltip: { callbacks: { label: function(ctx) {
                return ctx.dataset.yAxisID==='y1' ? 'Revenue: $'+ctx.parsed.y.toLocaleString('es-MX',{minimumFractionDigits:2}) : 'Órdenes: '+ctx.parsed.y;
            }}}},
            scales: {
                y:  { beginAtZero:true, position:'left',  title:{display:true,text:'Órdenes',color:'#C2410C',font:{weight:'bold'}}, ticks:{stepSize:1,color:'#C2410C'}, grid:{color:'rgba(0,0,0,0.04)'} },
                y1: { beginAtZero:true, position:'right', title:{display:true,text:'Revenue ($)',color:'#2563EB',font:{weight:'bold'}}, ticks:{color:'#2563EB', callback:function(v){return '$'+v.toLocaleString('es-MX');}}, grid:{drawOnChartArea:false} },
                x:  { ticks:{maxRotation:45,minRotation:0,font:{size:11}}, grid:{display:false} }
            }
        }
    });
}

function updateAmazonTodayBar(todayRevenue, todayOrders, todayUnits) {
    var pct = amzDailyGoal > 0 ? (todayRevenue / amzDailyGoal * 100) : 0;
    var cappedPct = Math.min(pct, 100);
    var barColor = pct >= 100 ? '#10B981' : (pct >= 80 ? '#F97316' : '#EF4444');

    var fill = document.getElementById('amz-today-goal-fill');
    fill.style.background = barColor;
    fill.style.width = Math.max(cappedPct, pct > 0 ? 2 : 0) + '%';
    var lbl = document.getElementById('amz-today-pct-label');
    if (cappedPct > 15) { lbl.classList.remove('hidden'); lbl.textContent = pct.toFixed(1) + '%'; }
    else lbl.classList.add('hidden');

    document.getElementById('amz-today-goal-text').textContent = '$'+fmtMoney(todayRevenue)+' / $'+fmtMoney(amzDailyGoal)+' ('+pct.toFixed(1)+'%)';
    document.getElementById('amz-today-sold-label').textContent = '$'+fmtMoney(todayRevenue)+' vendido';
    var rem = Math.max(amzDailyGoal - todayRevenue, 0);
    document.getElementById('amz-today-remaining-label').textContent = rem > 0 ? 'Faltan $'+fmtMoney(rem) : '🎯 ¡Meta alcanzada!';

    var qsOrders  = document.getElementById('amz-qs-orders');
    var qsUnits   = document.getElementById('amz-qs-units');
    var qsRevenue = document.getElementById('amz-qs-revenue');
    var qsPct     = document.getElementById('amz-qs-pct');
    var qsBar     = document.getElementById('amz-qs-bar');
    if (qsOrders)  qsOrders.textContent  = (todayOrders||0) + ' órd.';
    if (qsUnits)   qsUnits.textContent   = (todayUnits||0)  + ' uds';
    if (qsRevenue) qsRevenue.textContent = '$' + fmtMoney(todayRevenue);
    if (qsPct) { qsPct.textContent = pct.toFixed(1) + '%'; qsPct.style.color = pct >= 100 ? '#10B981' : (pct >= 80 ? '#D97706' : '#EF4444'); }
    if (qsBar)  { qsBar.style.width = cappedPct + '%'; qsBar.style.background = barColor; }
}

function renderAmazonDailyTable(data) {
    if (data.goal) { amzDailyGoal = data.goal; document.getElementById('amz-daily-goal-input').value = amzDailyGoal; }
    var daily = data.daily_data || [], totals = data.totals || {};
    if (!daily.length) {
        document.getElementById('amz-daily-sales-table').innerHTML = '<p class="text-center py-6 text-gray-400 text-sm">Sin datos para el periodo</p>';
        return;
    }
    var todayStr = toDateStr(new Date());
    var todayRevenue = 0, todayOrders = 0, todayUnits = 0;

    var _amzDailyRows = [];
    daily.forEach(function(day) {
        var pct = day.pct_of_goal, isToday = day.date === todayStr;
        if (isToday) { todayRevenue = day.revenue; todayOrders = day.orders; todayUnits = day.units; }
        var rowBg   = isToday ? 'bg-orange-50' : (pct>=100?'bg-green-50':(pct>=80?'bg-yellow-50':''));
        var barColor= pct>=100?'#10B981':(pct>=80?'#F97316':'#EF4444');
        var badge, bc;
        if (pct>=120){badge='Sobre';bc='bg-emerald-100 text-emerald-800';}
        else if(pct>=100){badge='Meta';bc='bg-green-100 text-green-800';}
        else if(pct>=80){badge='Cerca';bc='bg-yellow-100 text-yellow-800';}
        else if(pct>0){badge='Bajo';bc='bg-red-100 text-red-700';}
        else{badge='—';bc='bg-gray-100 text-gray-400';}
        var dateDisp = isToday ? '<span class="font-bold text-orange-500">● Hoy</span>' : day.date;
        var row = '<tr class="'+rowBg+' border-b border-gray-100 hover:bg-gray-50 transition">';
        row += '<td class="py-2.5 px-4 font-medium text-gray-700 whitespace-nowrap">'+dateDisp+'</td>';
        row += '<td class="py-2.5 px-3 text-right text-gray-600">'+day.orders+'</td>';
        row += '<td class="py-2.5 px-3 text-right text-gray-600">'+day.units+'</td>';
        row += '<td class="py-2.5 px-3 text-right font-semibold text-orange-700">$'+fmtMoney(day.revenue)+'</td>';
        row += '<td class="py-2.5 px-3 text-right text-violet-600 font-medium">$'+fmtMoney(day.net_est || 0)+'</td>';
        row += '<td class="py-2.5 px-3 text-right font-bold" style="color:'+barColor+'">'+pct.toFixed(1)+'%</td>';
        row += '<td class="py-2.5 px-4"><div class="w-full bg-gray-200 rounded-full h-2"><div class="h-2 rounded-full" style="width:'+Math.min(pct,100)+'%;background:'+barColor+'"></div></div></td>';
        row += '<td class="py-2.5 px-3 text-center"><span class="px-2 py-0.5 text-xs rounded-full font-semibold '+bc+'">'+badge+'</span></td>';
        row += '</tr>';
        _amzDailyRows.push(row);
    });

    var tfoot = '<tfoot class="bg-orange-50/50 text-sm font-semibold border-t-2 border-orange-200"><tr>';
    tfoot += '<td class="py-3 px-4 text-gray-600">Total <span class="font-normal text-gray-400">('+totals.total_days+' días)</span></td>';
    tfoot += '<td class="py-3 px-3 text-right text-gray-800">'+totals.orders+'</td>';
    tfoot += '<td class="py-3 px-3 text-right text-gray-800">'+totals.units+'</td>';
    tfoot += '<td class="py-3 px-3 text-right text-orange-700">$'+fmtMoney(totals.revenue)+'</td>';
    tfoot += '<td class="py-3 px-3 text-right text-violet-600">$'+fmtMoney(Math.round(totals.revenue * 0.85 * 100) / 100)+'</td>';
    tfoot += '<td class="py-3 px-3 text-right text-gray-500">Prom: '+totals.avg_pct.toFixed(1)+'%</td>';
    tfoot += '<td class="py-3 px-4"></td>';
    tfoot += '<td class="py-3 px-3 text-center text-xs text-gray-500">'+totals.days_met+'/'+totals.total_days+' días</td>';
    tfoot += '</tr></tfoot>';

    var thead = '<thead><tr class="text-left text-xs text-gray-400 bg-gray-50">' +
        '<th class="py-2.5 px-4 font-semibold">Fecha</th>' +
        '<th class="py-2.5 px-3 font-semibold text-right">Órd.</th>' +
        '<th class="py-2.5 px-3 font-semibold text-right">Uds</th>' +
        '<th class="py-2.5 px-3 font-semibold text-right">Revenue</th>' +
        '<th class="py-2.5 px-3 font-semibold text-right">Neto Est.</th>' +
        '<th class="py-2.5 px-3 font-semibold text-right">% Meta</th>' +
        '<th class="py-2.5 px-4 font-semibold w-36">Progreso</th>' +
        '<th class="py-2.5 px-3 font-semibold text-center">Estado</th>' +
        '</tr></thead>';
    document.getElementById('amz-daily-sales-table').innerHTML =
        '<table class="w-full text-sm">' + thead +
        '<tbody id="amz-daily-tbody"></tbody>' + tfoot + '</table>' +
        '<div id="amz-daily-pag"></div>';
    window['amz-daily-pag_go'] = function(p) { _renderPaginated(_amzDailyRows, p, 'amz-daily-tbody', 'amz-daily-pag'); };
    _renderPaginated(_amzDailyRows, 1, 'amz-daily-tbody', 'amz-daily-pag');
    updateAmazonTodayBar(todayRevenue, todayOrders, todayUnits);
}

function loadAmazonDashboard() {
    var p = getDateParams();
    var btn = document.getElementById('btn-amz-filtrar');
    btn.disabled = true; btn.classList.add('opacity-50');

    document.getElementById('amz-metric-cards').innerHTML =
        '<div class="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">'+
        Array(4).fill('<div class="bg-white p-4 md:p-5 rounded-xl shadow animate-pulse"><div class="h-3 bg-gray-200 rounded w-1/2 mb-3"></div><div class="h-7 bg-orange-100 rounded w-3/4 mb-2"></div><div class="h-3 bg-gray-100 rounded w-1/3"></div></div>').join('')+'</div>';

    document.getElementById('amz-chart-loading').classList.remove('hidden');
    document.getElementById('amzSalesChart').classList.add('hidden');

    document.getElementById('amz-daily-sales-table').innerHTML =
        '<div class="animate-pulse px-4 py-3 space-y-2">'+
        Array(6).fill('<div class="h-8 bg-orange-50 rounded"></div>').join('')+'</div>';

    fetch('/api/metrics/amazon-dashboard-data?' + p)
        .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
        .then(function(resp){
            if (resp.error && !resp.metrics.total_orders) {
                document.getElementById('amz-metric-cards').innerHTML = _amzErrorBanner(resp.error);
            } else {
                if (resp.metrics) renderAmazonMetrics(resp.metrics);
                if (resp.chart)   renderAmazonChart(resp.chart);
            }
        })
        .catch(function(e){
            document.getElementById('amz-metric-cards').innerHTML = _amzErrorBanner(e.message);
        });

    fetch('/api/metrics/amazon-daily-sales-data?' + p)
        .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
        .then(function(data){
            if (data.error && (!data.daily_data || !data.daily_data.length)) {
                document.getElementById('amz-daily-sales-table').innerHTML = _amzErrorBanner(data.error);
                updateAmazonTodayBar(0,0,0);
            } else {
                renderAmazonDailyTable(data);
            }
        })
        .catch(function(e){
            document.getElementById('amz-daily-sales-table').innerHTML = _amzErrorBanner(e.message);
            updateAmazonTodayBar(0,0,0);
        })
        .finally(function(){ btn.disabled = false; btn.classList.remove('opacity-50'); });
}

window.amzOrdFilter = window.amzOrdFilter || 'all';

window.setAmzOrdFilter = function(f) {
    window.amzOrdFilter = f;
    ['all', 'FBA', 'FBM'].forEach(function(k) {
        var btn = document.getElementById('amz-ord-f-' + k);
        if (!btn) return;
        if (k === f) {
            btn.className = btn.className.replace('text-gray-600 hover:bg-orange-50', '').trim();
            btn.classList.add('bg-orange-500', 'text-white');
        } else {
            btn.classList.remove('bg-orange-500', 'text-white');
            btn.classList.add('text-gray-600', 'hover:bg-orange-50');
        }
    });
    loadAmzRecentOrders(0);
};

function loadAmzRecentOrders(_retryCount) {
    var el = document.getElementById('amz-recent-orders');
    if (!el) return;
    var retryCount = _retryCount || 0;
    el.innerHTML = '<div class="animate-pulse space-y-3">'+Array(5).fill('<div class="h-12 bg-orange-50 rounded-lg"></div>').join('')+'</div>';
    var params = new URLSearchParams();
    if (window.amzActiveSellerId) params.set('seller_id', window.amzActiveSellerId);
    var daysEl = document.getElementById('amz-orders-days');
    params.set('days', daysEl ? daysEl.value : '1');
    params.set('fulfillment', window.amzOrdFilter || 'all');
    fetch('/api/metrics/amazon-recent-orders?' + params.toString())
        .then(function(r){
            if (r.status === 429) { _amzOrdersRateLimit(el, retryCount); return null; }
            return r.text();
        })
        .then(function(html){
            if (html === null) return;
            el.innerHTML = html;
        })
        .catch(function(){ el.innerHTML = '<p class="text-red-400 text-center py-4 text-sm">Error cargando órdenes</p>'; });
}

function _amzOrdersRateLimit(el, retryCount) {
    // Después de 3 intentos fallidos, dejar de reintentar automáticamente
    if (retryCount >= 3) {
        el.innerHTML =
            '<div class="flex flex-col items-center justify-center py-8 gap-3">' +
            '<p class="text-sm text-gray-500">Órdenes no disponibles temporalmente (rate limit SP-API)</p>' +
            '<button onclick="loadAmzRecentOrders(0)" class="text-xs text-orange-600 border border-orange-200 px-3 py-1.5 rounded-lg hover:border-orange-400 transition">Reintentar manualmente</button>' +
            '</div>';
        return;
    }
    var delay = Math.min(120, 20 * Math.pow(2, retryCount));  // 20s, 40s, 80s, tope 120s
    var deadline = Date.now() + delay * 1000;
    if (window._amzOrdersTimer) clearInterval(window._amzOrdersTimer);
    var timer = setInterval(function() {
        var secs = Math.ceil((deadline - Date.now()) / 1000);
        if (secs <= 0) {
            clearInterval(timer);
            loadAmzRecentOrders(retryCount + 1);
            return;
        }
        el.innerHTML =
            '<div class="flex flex-col items-center justify-center py-8 gap-3">' +
            '<svg class="w-8 h-8 text-orange-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>' +
            '<p class="text-sm font-semibold text-orange-700">Rate limit Amazon SP-API</p>' +
            '<p class="text-xs text-gray-500">Reintentando en <span class="font-bold text-orange-600">' + secs + 's</span>… (' + (retryCount+1) + '/3)</p>' +
            '<button onclick="if(window._amzOrdersTimer)clearInterval(window._amzOrdersTimer);loadAmzRecentOrders(0)" class="text-xs text-orange-600 hover:underline mt-1">Reintentar ahora</button>' +
            '</div>';
    }, 500);
    window._amzOrdersTimer = timer;
}

// ── Multi-cuenta comparativa ──────────────────────────────────────────────
async function loadAmzCompare() {
  var el = document.getElementById('amz-compare-content');
  if (!el) return;
  try {
    var r = await fetch('/api/dashboard/multi-account-amazon');
    var d = await r.json();
    var accounts = (d.amazon_accounts || d.accounts || []);
    if (!accounts.length) { el.innerHTML = '<div class="text-center py-4 text-gray-400 text-xs">Sin cuentas disponibles</div>'; return; }
    var fmt = function(v) { return new Intl.NumberFormat('es-MX',{style:'currency',currency:'MXN',minimumFractionDigits:0,maximumFractionDigits:0}).format(v||0); };
    var cards = accounts.map(function(acc) {
      var m = acc.month || {};
      var t = acc.today || {};
      return '<div class="flex-1 min-w-0 bg-orange-50 rounded-xl p-4 border border-orange-100">' +
        '<div class="flex items-center gap-2 mb-2">' +
          '<span class="w-2 h-2 rounded-full bg-orange-500 shrink-0"></span>' +
          '<span class="font-semibold text-gray-800 text-sm truncate">' + (acc.nickname||acc.seller_id) + '</span>' +
          '<span class="text-xs text-gray-400 shrink-0">' + (acc.marketplace||'MX') + '</span>' +
        '</div>' +
        '<div class="grid grid-cols-2 gap-2 text-xs">' +
          '<div class="bg-white rounded-lg p-2">' +
            '<p class="text-gray-400 mb-0.5">Hoy</p>' +
            '<p class="font-bold text-orange-700">' + fmt(t.revenue) + '</p>' +
            '<p class="text-gray-400">' + (t.orders||0) + ' órd</p>' +
          '</div>' +
          '<div class="bg-white rounded-lg p-2">' +
            '<p class="text-gray-400 mb-0.5">Mes</p>' +
            '<p class="font-bold text-gray-800">' + fmt(m.revenue) + '</p>' +
            '<p class="text-gray-400">' + (m.orders||0) + ' órd</p>' +
          '</div>' +
        '</div>' +
        (acc.error ? '<p class="text-xs text-red-400 mt-1">⚠ ' + acc.error.slice(0,50) + '</p>' : '') +
      '</div>';
    }).join('');
    el.innerHTML = '<div class="flex flex-wrap gap-3">' + cards + '</div>';
  } catch(e) {
    el.innerHTML = '<div class="text-center py-4 text-red-400 text-xs">Error: ' + e.message + '</div>';
  }
}

// ── Morning Briefing ───────────────────────────────────────────────────────
var _amzBriefingLoaded = false;
function refreshAmzBriefing() {
    _amzBriefingLoaded = false;
    loadAmzBriefing();
}
async function loadAmzBriefing() {
    if (_amzBriefingLoaded) return;
    _amzBriefingLoaded = true;
    var cont = document.getElementById('amz-briefing-content');
    var dateEl = document.getElementById('amz-briefing-date');
    if (!cont) return;
    var now = new Date();
    var todayStr = toDateStr(now);
    if (dateEl) {
        var months = ['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
        dateEl.textContent = now.getDate() + ' de ' + months[now.getMonth()] + ' ' + now.getFullYear();
    }
    try {
        var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
        var r = await fetch('/api/metrics/amazon-daily-sales-data?date_from=' + todayStr + '&date_to=' + todayStr + (window.amzActiveSellerId ? '&seller_id=' + window.amzActiveSellerId : ''));
        var d = await r.json();
        var daily = (d.daily_data || [])[0] || {};
        var todayRev = daily.revenue || 0;
        var todayOrd = daily.orders || 0;
        var todayUds = daily.units || 0;
        var goal = d.goal || 0;
        var pct = goal > 0 ? (todayRev / goal * 100) : 0;
        var pctColor = pct >= 100 ? '#10B981' : (pct >= 80 ? '#F59E0B' : '#EF4444');
        cont.innerHTML = [
            { label: 'Revenue hoy', val: '$' + fmtMoney(todayRev), color: '#FF9900' },
            { label: 'Órdenes hoy', val: todayOrd + ' órd.', color: '#FBD38D' },
            { label: 'Unidades', val: todayUds + ' uds', color: '#FBD38D' },
            { label: '% de Meta', val: pct.toFixed(1) + '%', color: pctColor },
        ].map(function(k) {
            return '<div class="bg-white/5 rounded-lg px-3 py-2.5 text-center">' +
                '<p class="text-xs text-gray-400 mb-1">' + k.label + '</p>' +
                '<p class="font-extrabold text-sm" style="color:' + k.color + '">' + k.val + '</p>' +
            '</div>';
        }).join('');
    } catch(e) {
        cont.innerHTML = '<div class="col-span-4 text-xs text-gray-500 text-center py-2">Sin datos disponibles</div>';
    }
}

// ── Alertas ────────────────────────────────────────────────────────────────
async function loadAmzAlerts() {
  try {
    var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
    var r = await fetch('/api/amazon/alerts' + sellerParam);
    var d = await r.json();
    if (!d || d.error) { document.getElementById('amz-alerts-content').innerHTML = ''; return; }

    if (d.total_alerts === 0) {
      document.getElementById('amz-alerts-content').innerHTML =
        '<div class="flex items-center gap-2 text-sm text-green-600 bg-green-50 border border-green-200 rounded-xl px-4 py-2.5 mb-1"><span>✓</span><span>Sin alertas críticas</span></div>';
      return;
    }

    var html = '<div class="flex flex-wrap gap-2 mb-1">';
    if (d.suppressed && d.suppressed.length)
      html += '<button onclick="switchAmzTab(\'operaciones\')" class="flex items-center gap-1.5 text-sm bg-red-50 border border-red-200 text-red-700 hover:bg-red-100 rounded-xl px-3 py-1.5 transition">🚫 ' + d.suppressed.length + ' suprimido' + (d.suppressed.length > 1 ? 's' : '') + '</button>';
    if (d.no_stock_active && d.no_stock_active.length)
      html += '<button onclick="switchAmzTab(\'operaciones\')" class="flex items-center gap-1.5 text-sm bg-orange-50 border border-orange-200 text-orange-700 hover:bg-orange-100 rounded-xl px-3 py-1.5 transition">📦 ' + d.no_stock_active.length + ' sin stock</button>';
    if (d.low_stock && d.low_stock.length)
      html += '<button onclick="switchAmzTab(\'operaciones\')" class="flex items-center gap-1.5 text-sm bg-yellow-50 border border-yellow-200 text-yellow-700 hover:bg-yellow-100 rounded-xl px-3 py-1.5 transition">⚠️ ' + d.low_stock.length + ' stock bajo (&lt;' + d.threshold + 'u)</button>';
    html += '</div>';
    document.getElementById('amz-alerts-content').innerHTML = html;
  } catch(e) { document.getElementById('amz-alerts-content').innerHTML = ''; }
}

// ── Tab Operaciones ────────────────────────────────────────────────────────
var _opsLoaded = false;
async function loadAmzOperacionesTab() {
  if (_opsLoaded) return;
  _opsLoaded = true;
  loadOpsAlerts();
  loadOpsStockBar();
  loadOpsListing();
  loadStockThreshold();
}

async function loadOpsStockBar() {
  var el = document.getElementById('amz-ops-stock-bar');
  if (!el) return;
  var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/restock-report' + sellerParam);
    var d = await r.json();
    if (d.error || !d.summary) return;
    var s = d.summary;
    var critical = (s.critical||0) + (s.out_of_stock||0);
    var warning  = s.warning || 0;
    var ok       = s.ok || 0;
    if (critical === 0 && warning === 0) {
      el.innerHTML = '<div class="flex items-center gap-2 px-4 py-2 bg-green-50 border border-green-200 rounded-xl text-sm text-green-700 mb-1">' +
        '<span class="font-semibold">✅ Stock FBA OK</span>' +
        '<span class="text-green-600">' + ok + ' SKUs con cobertura suficiente</span>' +
        '<button onclick="switchAmzTab(\'fba\')" class="ml-auto text-xs text-green-600 hover:text-green-800 underline">Ver FBA →</button>' +
      '</div>';
    } else {
      el.innerHTML = '<div class="flex flex-wrap items-center gap-3 px-4 py-3 bg-red-50 border border-red-200 rounded-xl mb-1">' +
        '<span class="text-sm font-bold text-red-700">⚠️ Stock Issues FBA</span>' +
        (critical > 0 ? '<span class="bg-red-100 text-red-700 text-xs font-semibold px-2 py-0.5 rounded-full">🚨 ' + critical + ' crítico/sin stock</span>' : '') +
        (warning > 0  ? '<span class="bg-yellow-100 text-yellow-700 text-xs font-semibold px-2 py-0.5 rounded-full">⚠️ ' + warning + ' advertencia</span>' : '') +
        (ok > 0       ? '<span class="bg-green-100 text-green-700 text-xs font-semibold px-2 py-0.5 rounded-full">✅ ' + ok + ' OK</span>' : '') +
        '<button onclick="switchAmzTab(\'fba\')" class="ml-auto text-xs text-orange-600 hover:text-orange-800 font-medium underline">Ver calculadora FBA →</button>' +
      '</div>';
    }
  } catch(e) { /* silent */ }
}

async function loadOpsAlerts() {
  var el = document.getElementById('amz-ops-alerts');
  if (!el) return;
  try {
    var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
    var r = await fetch('/api/amazon/alerts' + sellerParam);
    var d = await r.json();
    if (!d || d.total_alerts === 0) { el.innerHTML = ''; return; }
    var html = '<div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-2">';
    if (d.suppressed && d.suppressed.length) {
      html += '<div class="bg-red-50 border border-red-200 rounded-xl p-4"><p class="font-semibold text-red-700 mb-2">🚫 Listings suprimidos (' + d.suppressed.length + ')</p><ul class="text-sm text-red-600 space-y-1">';
      d.suppressed.slice(0,5).forEach(function(s) { html += '<li class="truncate" title="' + s.title + '">• ' + s.sku + ' — ' + (s.first_issue || 'Ver issues') + '</li>'; });
      if (d.suppressed.length > 5) html += '<li class="text-red-400">+' + (d.suppressed.length - 5) + ' más...</li>';
      html += '</ul></div>';
    }
    if (d.no_stock_active && d.no_stock_active.length) {
      html += '<div class="bg-orange-50 border border-orange-200 rounded-xl p-4"><p class="font-semibold text-orange-700 mb-2">📦 Sin stock activos (' + d.no_stock_active.length + ')</p><ul class="text-sm text-orange-600 space-y-1">';
      d.no_stock_active.slice(0,5).forEach(function(s) { html += '<li class="truncate" title="' + s.title + '">• ' + s.sku + '</li>'; });
      if (d.no_stock_active.length > 5) html += '<li class="text-orange-400">+' + (d.no_stock_active.length - 5) + ' más...</li>';
      html += '</ul></div>';
    }
    if (d.low_stock && d.low_stock.length) {
      html += '<div class="bg-yellow-50 border border-yellow-200 rounded-xl p-4"><p class="font-semibold text-yellow-700 mb-2">⚠️ Stock bajo (' + d.low_stock.length + ')</p><ul class="text-sm text-yellow-700 space-y-1">';
      d.low_stock.slice(0,5).forEach(function(s) { html += '<li class="truncate">• ' + s.sku + ': ' + s.fba_stock + '/' + s.threshold + 'u</li>'; });
      if (d.low_stock.length > 5) html += '<li class="text-yellow-500">+' + (d.low_stock.length - 5) + ' más...</li>';
      html += '</ul></div>';
    }
    html += '</div>';
    el.innerHTML = html;
  } catch(e) { el.innerHTML = ''; }
}

async function loadOpsListing() {
  var wrap = document.getElementById('amz-ops-catalog-wrap');
  if (!wrap) return;
  var filter = amzOpsCurrentFilter || 'all';
  var sellerParam = window.amzActiveSellerId ? '&seller_id=' + window.amzActiveSellerId : '';
  wrap.innerHTML = '<div class="text-center py-8 text-gray-400 text-sm">Cargando...</div>';
  try {
    var r = await fetch('/api/amazon/products/catalog?status_filter=' + filter + sellerParam);
    var html = await r.text();
    wrap.innerHTML = html;
    initBulkSelection();
  } catch(e) { wrap.innerHTML = '<div class="text-center py-8 text-red-400 text-sm">Error al cargar catálogo</div>'; }
}

// ── Bulk selection ─────────────────────────────────────────────────────────
var amzSelectedSkus = new Set();

function initBulkSelection() {
  var all = document.getElementById('amz-select-all');
  if (all) {
    all.addEventListener('change', function() {
      document.querySelectorAll('.amz-sku-check').forEach(function(cb) {
        cb.checked = all.checked;
        if (all.checked) amzSelectedSkus.add(cb.dataset.sku);
        else amzSelectedSkus.delete(cb.dataset.sku);
      });
      updateBulkBar();
    });
  }
  document.querySelectorAll('.amz-sku-check').forEach(function(cb) {
    cb.addEventListener('change', function() {
      if (this.checked) amzSelectedSkus.add(this.dataset.sku);
      else amzSelectedSkus.delete(this.dataset.sku);
      updateBulkBar();
    });
  });
}

function updateBulkBar() {
  var bar = document.getElementById('amz-bulk-bar');
  var cnt = document.getElementById('amz-bulk-count');
  if (!bar || !cnt) return;
  cnt.textContent = amzSelectedSkus.size;
  bar.classList.toggle('hidden', amzSelectedSkus.size === 0);
}

function clearBulkSelection() {
  amzSelectedSkus.clear();
  document.querySelectorAll('.amz-sku-check, #amz-select-all').forEach(function(cb) { cb.checked = false; });
  updateBulkBar();
}

async function executeBulkAction(action) {
  if (amzSelectedSkus.size === 0) return;
  var skuList = Array.from(amzSelectedSkus);
  var label = action === 'set_qty_zero' ? 'Qty 0' : 'Activar FBA';
  if (!confirm('¿' + label + ' ' + skuList.length + ' listing(s) seleccionados?')) return;

  var bar = document.getElementById('amz-bulk-bar');
  bar.innerHTML = '<span class="text-sm">Procesando...</span>';

  var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/products/bulk-action' + sellerParam, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({skus: skuList, action: action})
    });
    var d = await r.json();
    clearBulkSelection();
    _opsLoaded = false;
    loadOpsListing();
    loadAmzAlerts();
    alert('✓ ' + d.succeeded + ' OK' + (d.failed > 0 ? ', ' + d.failed + ' con error' : ''));
  } catch(e) {
    alert('Error al ejecutar la acción');
    clearBulkSelection();
  }
}

// ── Stock threshold ─────────────────────────────────────────────────────────
async function loadStockThreshold() {
  try {
    var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
    var r = await fetch('/api/amazon/settings/stock-threshold' + sellerParam);
    var d = await r.json();
    var inp = document.getElementById('amz-threshold-input');
    if (inp && d.threshold !== undefined) inp.value = d.threshold;
  } catch(e) {}
}

async function saveStockThreshold() {
  var inp = document.getElementById('amz-threshold-input');
  var saved = document.getElementById('amz-threshold-saved');
  if (!inp) return;
  var threshold = parseInt(inp.value, 10);
  var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/settings/stock-threshold' + sellerParam, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({threshold: threshold})
    });
    var d = await r.json();
    if (d.ok) {
      saved.classList.remove('hidden');
      setTimeout(function() { saved.classList.add('hidden'); }, 3000);
      loadAmzAlerts();
    }
  } catch(e) { alert('Error al guardar'); }
}

// ── Tab Finanzas ────────────────────────────────────────────────────────────
var _finanzasLoaded = false;
async function loadAmzFinanzasTab() {
  if (_finanzasLoaded) return;
  _finanzasLoaded = true;
  var el = document.getElementById('amz-finanzas-content');
  if (!el) return;
  el.innerHTML = '<div class="animate-pulse space-y-3">' + Array(3).fill('<div class="h-24 bg-orange-50 rounded-xl"></div>').join('') + '</div>';
  var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/finances/summary' + sellerParam);
    var d = await r.json();
    if (d.error) {
      el.innerHTML = '<div class="text-center py-12 text-red-400 text-sm">' + d.error + '</div>';
      return;
    }

    var fmt = function(v, cur) { return new Intl.NumberFormat('es-MX', {style:'currency', currency: cur||'MXN', minimumFractionDigits:0, maximumFractionDigits:0}).format(v||0); };
    var cm = d.current_month || {}, pm = d.prev_month || {};
    var cur = d.currency || 'MXN';

    // Month-over-month trend
    var salesDelta = pm.sales > 0 ? Math.round((cm.sales - pm.sales) / pm.sales * 100) : null;
    var trendHtml = salesDelta != null
      ? '<span class="text-xs font-semibold ml-1 ' + (salesDelta >= 0 ? 'text-green-600' : 'text-red-600') + '">' +
          (salesDelta >= 0 ? '▲' : '▼') + ' ' + Math.abs(salesDelta) + '% vs ' + (pm.label||'mes ant.') +
        '</span>'
      : '';

    // ── KPI Cards ─────────────────────────────────────────────────────────
    var ref = d.refunds_30d || {};
    var refColor = (ref.rate_pct||0) >= 5 ? '#EF4444' : ((ref.rate_pct||0) >= 2 ? '#F59E0B' : '#6B7280');
    var kpiCards = [
      { label: 'Ventas ' + (cm.label||'mes actual'), value: fmt(cm.sales, cur), icon: '💰', color: '#F97316',
        sub: (cm.orders||0) + ' órd · ' + (cm.units||0) + ' uds', extra: trendHtml },
      { label: 'Fees estimados (' + (cm.label||'mes') + ')', value: fmt(cm.fees_est, cur), icon: '📋', color: '#6366F1',
        sub: '~20% comisión + FBA est.', extra: '' },
      { label: 'Neto estimado (' + (cm.label||'mes') + ')', value: fmt(cm.net_est, cur), icon: '📈', color: '#10B981',
        sub: 'Ventas - fees estimados', extra: '' },
      { label: 'Pendiente liquidación', value: fmt(d.pending_payout, d.pending_currency||cur), icon: '🏦', color: '#0EA5E9',
        sub: 'Ciclo abierto en Amazon', extra: '' },
      { label: 'Reembolsos 30d', value: (ref.count||0) + ' dev.', icon: '↩️', color: refColor,
        sub: fmt(ref.total||0, ref.currency||cur) + ' · ' + (ref.rate_pct||0) + '% de ventas', extra: '' },
    ].map(function(c) {
      return '<div class="bg-white rounded-xl shadow p-4 border-b-4" style="border-bottom-color:' + c.color + '">' +
        '<div class="flex items-center justify-between mb-2">' +
          '<span class="text-xs text-gray-400 font-medium">' + c.label + '</span>' +
          '<span class="text-xl">' + c.icon + '</span>' +
        '</div>' +
        '<p class="text-xl font-extrabold" style="color:' + c.color + '">' + c.value + c.extra + '</p>' +
        '<p class="text-xs mt-1 text-gray-400">' + c.sub + '</p>' +
      '</div>';
    }).join('');

    // ── Comparativo mes anterior ───────────────────────────────────────────
    var prevRow = pm.sales > 0
      ? '<div class="bg-gray-50 rounded-xl p-4 mb-5 flex flex-wrap gap-6 text-sm">' +
          '<span class="text-gray-500 font-medium">' + (pm.label||'Mes anterior') + ':</span>' +
          '<span>Ventas <b class="text-gray-800">' + fmt(pm.sales,cur) + '</b></span>' +
          '<span>Fees <b class="text-gray-600">' + fmt(pm.fees_est,cur) + '</b></span>' +
          '<span>Neto <b class="text-green-700">' + fmt(pm.net_est,cur) + '</b></span>' +
          '<span>Órdenes <b class="text-gray-700">' + (pm.orders||0) + '</b></span>' +
        '</div>'
      : '';

    // ── Settlement groups table ────────────────────────────────────────────
    var groups = d.groups || [];
    var _finRows = groups.map(function(g, i) {
      var fecha = g.fund_transfer_date ? g.fund_transfer_date.split('T')[0] : '—';
      var monto = g.converted_total || g.original_total || 0;
      var montoFmt = fmt(monto, g.currency||cur);
      var statusColor = g.status === 'Closed' ? 'bg-green-100 text-green-700' :
                        g.status === 'Open'   ? 'bg-blue-100 text-blue-700' :
                        'bg-gray-100 text-gray-500';
      var rowBg = i === 0 ? 'bg-orange-50/40' : '';
      return '<tr class="' + rowBg + ' hover:bg-gray-50 transition">' +
        '<td class="px-5 py-3 font-mono text-xs text-gray-400">' + (g.group_id ? g.group_id.slice(-10) : '—') + '</td>' +
        '<td class="px-5 py-3 text-gray-600 text-sm">' + fecha + '</td>' +
        '<td class="px-5 py-3 text-right font-bold text-sm ' + (monto < 0 ? 'text-red-600' : 'text-gray-800') + '">' + montoFmt + '</td>' +
        '<td class="px-5 py-3 text-center"><span class="inline-block px-2 py-0.5 rounded-full text-xs font-medium ' + statusColor + '">' + (g.status||'—') + '</span></td>' +
        '</tr>';
    });

    var tableHtml = groups.length
      ? '<div class="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden">' +
          '<div class="px-6 py-4 border-b border-gray-100 flex items-center justify-between">' +
            '<div><h3 class="font-semibold text-gray-800">Liquidaciones Amazon</h3>' +
            '<p class="text-xs text-gray-400 mt-0.5">Períodos de pago (últimos 6 meses)</p></div>' +
          '</div>' +
          '<div class="overflow-x-auto">' +
            '<table class="w-full text-sm"><thead class="bg-gray-50"><tr class="text-left text-xs text-gray-400 uppercase tracking-wide">' +
              '<th class="px-5 py-3">ID Período</th>' +
              '<th class="px-5 py-3">Fecha transferencia</th>' +
              '<th class="px-5 py-3 text-right">Monto neto</th>' +
              '<th class="px-5 py-3 text-center">Estado</th>' +
            '</tr></thead><tbody id="amz-fin-tbody" class="divide-y divide-gray-50"></tbody></table></div></div>' +
          '<div id="amz-fin-pag"></div>'
      : '<div class="text-center py-8 text-gray-400 text-sm">Sin liquidaciones disponibles aún</div>';

    el.innerHTML =
      '<div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3 mb-5">' + kpiCards + '</div>' +
      prevRow +
      '<div class="bg-orange-50 border border-orange-100 rounded-xl p-3 mb-5 text-xs text-orange-700">' +
        '<b>Nota:</b> Fees y neto son estimados (~20% de ventas). Para datos exactos consultar <a href="https://sellercentral.amazon.com.mx/payments/dashboard" target="_blank" class="underline">Seller Central → Pagos</a>.' +
      '</div>' +
      tableHtml;

    if (groups.length) {
      window['amz-fin-pag_go'] = function(p) { _renderPaginated(_finRows, p, 'amz-fin-tbody', 'amz-fin-pag'); };
      _renderPaginated(_finRows, 1, 'amz-fin-tbody', 'amz-fin-pag');
    }
  } catch(e) {
    el.innerHTML = '<div class="text-center py-12 text-red-400 text-sm">Error al cargar finanzas: ' + e.message + '</div>';
  }
}

// ─── TAB SALUD ────────────────────────────────────────────────────────────────
function loadAmzSaludTab() {
    var cont = document.getElementById('amz-health-content');
    cont.innerHTML = '<div class="animate-pulse space-y-3">'+Array(4).fill('<div class="h-20 bg-orange-50 rounded-xl"></div>').join('')+'</div>';
    var _hSellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
    fetch('/api/metrics/amazon-health-data' + _hSellerParam)
        .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
        .then(function(data){ renderAmzHealth(data, cont); amzTabLoaded.salud = true; })
        .catch(function(e){ cont.innerHTML = '<p class="text-red-400 text-center py-6 bg-white rounded-xl shadow p-6">Error cargando salud: '+e.message+'</p>'; });
    loadAmzBuyerMessages();
}

function renderAmzHealth(d, cont) {
    var score = d.health_score || 0;
    var scoreColor = score >= 80 ? '#10B981' : (score >= 60 ? '#F59E0B' : '#EF4444');
    var scoreLabel = score >= 80 ? 'Excelente' : (score >= 60 ? 'Regular' : 'Crítico');
    var o = d.orders || {}, fba = d.fba || {};
    var pct = score; // 0-100

    // Circular progress ring via conic-gradient
    var ringStyle = 'background: conic-gradient(' + scoreColor + ' ' + pct + '%, #e5e7eb ' + pct + '%); width:96px; height:96px; border-radius:50%; display:flex; align-items:center; justify-content:center;';
    var innerStyle = 'width:72px; height:72px; border-radius:50%; background:#fff; display:flex; flex-direction:column; align-items:center; justify-content:center;';
    var circularScore =
        '<div style="' + ringStyle + '">' +
          '<div style="' + innerStyle + '">' +
            '<span class="text-2xl font-extrabold" style="color:' + scoreColor + '; line-height:1">' + score + '</span>' +
            '<span class="text-xs font-semibold" style="color:' + scoreColor + '">' + scoreLabel + '</span>' +
          '</div>' +
        '</div>';

    // Alerts
    var alertsHtml = (d.alerts || []).map(function(a) {
        var bg = a.level==='error'?'bg-red-50 border-red-200 text-red-700': a.level==='warning'?'bg-yellow-50 border-yellow-200 text-yellow-700':'bg-green-50 border-green-200 text-green-700';
        var icon = a.level==='error'?'⛔':a.level==='warning'?'⚠️':'✅';
        return '<div class="flex items-center gap-3 p-3 rounded-lg border '+bg+'"><span class="text-lg shrink-0">'+icon+'</span><p class="text-sm font-medium">'+a.msg+'</p></div>';
    }).join('');

    var html = '<!-- Score de salud -->' +
    '<div class="bg-white rounded-xl shadow p-5 mb-4 border-l-4 border-orange-400">' +
        '<div class="flex items-center justify-between mb-4">' +
            '<div><h2 class="text-base font-bold text-gray-800">Salud de la Cuenta</h2>' +
            '<p class="text-xs text-gray-400 mt-0.5">' + (d.nickname||'Amazon') + ' · ' + (d.marketplace||'MX') + ' · Últimos 30 días</p></div>' +
            '<div class="flex flex-col items-center gap-1">' +
                circularScore +
                '<div class="text-xs text-gray-400 mt-1">/ 100</div>' +
            '</div>' +
        '</div>' +
        '<div class="flex gap-6 text-xs text-gray-500 mt-2">' +
            '<span>Cancelaciones: <b style="color:'+scoreColor+'">'+o.cancel_rate+'%</b></span>' +
            '<span>Sin enviar: <b>'+o.unshipped+'</b></span>' +
            '<span>Envíos tardíos: <b>'+(o.late_ship_rate||0)+'%</b></span>' +
            '<span>FBA no vendible: <b>'+fba.unfulfillable+'</b></span>' +
        '</div>' +
    '</div>' +

    '<!-- Alertas -->' +
    '<div class="space-y-2 mb-4">' + alertsHtml + '</div>' +

    '<!-- ODR + Late Shipment KPIs -->' +
    (function() {
        var lsr = o.late_ship_rate || 0;
        var lsrColor = lsr >= 4 ? '#EF4444' : (lsr >= 2 ? '#F59E0B' : '#10B981');
        var crColor  = o.cancel_rate >= 2.5 ? '#EF4444' : (o.cancel_rate >= 1 ? '#F59E0B' : '#10B981');
        return '<div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">' + [
            { label: 'Tasa cancelación', value: o.cancel_rate+'%', threshold: '<2.5%', color: crColor,
              icon: '❌', ok: o.cancel_rate < 2.5 },
            { label: 'Env. tardíos (estimado)', value: lsr+'%', threshold: '<4%', color: lsrColor,
              icon: '📅', ok: lsr < 4 },
            { label: 'Órdenes sin enviar', value: o.unshipped, threshold: 'meta: 0', color: o.unshipped > 5 ? '#F59E0B' : '#10B981',
              icon: '📦', ok: o.unshipped === 0 },
            { label: 'FBA no vendible', value: fba.unfulfillable+' u', threshold: 'meta: 0', color: fba.unfulfillable > 0 ? '#EF4444' : '#10B981',
              icon: '🏚️', ok: fba.unfulfillable === 0 },
        ].map(function(c) {
            return '<div class="bg-white rounded-xl shadow p-4 border-l-4" style="border-left-color:' + c.color + '">' +
                '<div class="flex items-center justify-between mb-1">' +
                    '<span class="text-xs text-gray-400">' + c.label + '</span>' +
                    '<span>' + (c.ok ? '✅' : '⚠️') + '</span>' +
                '</div>' +
                '<p class="text-xl font-extrabold" style="color:'+c.color+'">' + c.value + '</p>' +
                '<p class="text-xs text-gray-400">Amazon: ' + c.threshold + '</p>' +
            '</div>';
        }).join('') + '</div>';
    })() +

    '<!-- Cards Órdenes y FBA -->' +
    '<div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">' +
        '<!-- Órdenes -->' +
        '<div class="bg-white rounded-xl shadow p-5">' +
            '<h3 class="font-bold text-gray-700 mb-3 text-sm">📦 Órdenes (30 días)</h3>' +
            '<div class="space-y-2">' +
                '<div class="flex justify-between text-sm"><span class="text-gray-500">Total órdenes</span><span class="font-bold text-gray-800">'+o.total_30d+'</span></div>' +
                '<div class="flex justify-between text-sm"><span class="text-gray-500">Enviadas / Entregadas</span><span class="font-bold text-green-600">'+o.shipped+'</span></div>' +
                '<div class="flex justify-between text-sm"><span class="text-gray-500">Pendientes de envío</span><span class="font-bold text-yellow-600">'+o.unshipped+'</span></div>' +
                '<div class="flex justify-between text-sm"><span class="text-gray-500">Envíos tardíos (est.)</span><span class="font-bold '+(o.late_ship_count>0?'text-red-600':'text-green-600')+'">'+o.late_ship_count+' ('+(o.late_ship_rate)+'%)</span></div>' +
                '<div class="flex justify-between text-sm"><span class="text-gray-500">Pendientes pago</span><span class="font-semibold text-gray-500">'+o.pending+'</span></div>' +
                '<div class="flex justify-between text-sm border-t pt-2 mt-2"><span class="text-gray-500">Canceladas</span><span class="font-bold '+(o.canceled>0?'text-red-600':'text-gray-500')+'">'+o.canceled+' ('+o.cancel_rate+'%)</span></div>' +
            '</div>' +
        '</div>' +
        '<!-- FBA -->' +
        '<div class="bg-white rounded-xl shadow p-5">' +
            '<h3 class="font-bold text-gray-700 mb-3 text-sm">🏭 Inventario FBA</h3>' +
            '<div class="space-y-2">' +
                '<div class="flex justify-between text-sm"><span class="text-gray-500">SKUs en FBA</span><span class="font-bold text-gray-800">'+fba.sku_count+'</span></div>' +
                '<div class="flex justify-between text-sm"><span class="text-gray-500">Disponible (vendible)</span><span class="font-bold text-green-600">'+fba.fulfillable+'</span></div>' +
                '<div class="flex justify-between text-sm"><span class="text-gray-500">Reservado (en órdenes)</span><span class="font-semibold text-blue-600">'+fba.reserved+'</span></div>' +
                '<div class="flex justify-between text-sm"><span class="text-gray-500">En camino</span><span class="font-semibold text-indigo-600">'+fba.inbound+'</span></div>' +
                '<div class="flex justify-between text-sm border-t pt-2 mt-2"><span class="text-gray-500">No vendible (dañado)</span><span class="font-bold '+(fba.unfulfillable>0?'text-red-600':'text-gray-500')+'">'+fba.unfulfillable+'</span></div>' +
            '</div>' +
        '</div>' +
    '</div>' +

    '<!-- Guía de salud -->' +
    '<div class="bg-orange-50 rounded-xl p-4 text-xs text-orange-700 border border-orange-100">' +
        '<p class="font-semibold mb-1">📋 Criterios de salud Amazon (umbrales de suspensión)</p>' +
        '<ul class="space-y-1 list-disc ml-4">' +
            '<li><b>ODR (tasa de defectos)</b> &lt; 1% — incluye reseñas negativas, reclamaciones A-Z y contracargos</li>' +
            '<li><b>Tasa de cancelación</b> &lt; 2.5% — cancelaciones previas a envío</li>' +
            '<li><b>Envíos tardíos</b> &lt; 4% — pedidos enviados después de la fecha límite de envío</li>' +
            '<li><b>Inventario FBA no vendible</b> — generar removal order para recuperar unidades dañadas</li>' +
        '</ul>' +
    '</div>';

    cont.innerHTML = html;
}

// ─── Meta diaria ──────────────────────────────────────────────────────────────
document.getElementById('btn-amz-update-goal').addEventListener('click', function() {
    var newGoal = parseFloat(document.getElementById('amz-daily-goal-input').value) || 50000;
    amzDailyGoal = newGoal;
    fetch('/api/metrics/amazon-goal', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({daily_goal: newGoal, seller_id: window.amzActiveSellerId || ''})
    }).then(function(){
        var p = getDateParams();
        fetch('/api/metrics/amazon-daily-sales-data?' + p)
            .then(function(r){return r.json();})
            .then(function(data){ renderAmazonDailyTable(data); });
    });
});

// ─── Range buttons ────────────────────────────────────────────────────────────
document.querySelectorAll('.amz-range-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
        setRange(parseInt(this.getAttribute('data-days')));
        highlightRangeBtn(this);
        loadAmazonDashboard();
    });
});

document.getElementById('btn-amz-filtrar').addEventListener('click', function() {
    var df = document.getElementById('amz_date_from').value;
    var dt = document.getElementById('amz_date_to').value;
    var periodEl2 = document.getElementById('amz-period-label');
    if (df && dt && periodEl2) periodEl2.textContent = df + ' – ' + dt;
    highlightRangeBtn(null);
    loadAmazonDashboard();
});

document.getElementById('btn-amz-limpiar').addEventListener('click', function() {
    setRange(30);
    highlightRangeBtn(document.querySelector('.amz-range-btn[data-days="30"]'));
    loadAmazonDashboard();
});

// ─── Balance Amazon ────────────────────────────────────────────────────────────
function loadAmzBalance() {
    var el = document.getElementById('amz-balance-val');
    if (!el) return;
    el.textContent = 'Cargando…';
    fetch('/api/metrics/account-balance')
        .then(function(r) { return r.json(); })
        .then(function(d) {
            var amz = d.amazon || {};
            if (amz.error) {
                el.textContent = 'No disponible';
                el.title = amz.error;
                return;
            }
            var pending = amz.pending_amount;
            var currency = amz.currency || 'MXN';
            el.textContent = pending != null
                ? '$' + pending.toLocaleString('es-MX', {minimumFractionDigits:2,maximumFractionDigits:2}) + ' ' + currency
                : '—';
        })
        .catch(function() { el.textContent = 'Error al cargar'; });
}

// ─── Carga inicial ─────────────────────────────────────────────────────────────
setRange(30);
highlightRangeBtn(document.querySelector('.amz-range-btn[data-days="30"]'));
if (amzActiveTab === 'salud') {
    loadAmzSaludTab();
    amzTabLoaded.salud = true;
} else if (amzActiveTab === 'operaciones') {
    loadAmzOperacionesTab();
} else if (amzActiveTab === 'finanzas') {
    loadAmzFinanzasTab();
} else if (amzActiveTab === 'ventas') {
    loadAmzBriefing();
    loadAmzRecentOrders();
    setTimeout(loadTopProducts, 1500);
} else {
    // dashboard (default)
    loadAmazonDashboard();
}
loadAmzBalance();
loadAmzAlerts();
loadAmzStatsRow();

function saveManualAmazonToken() {
    var token = (document.getElementById('amz-manual-token') || {}).value || '';
    token = token.trim();
    var msgEl = document.getElementById('amz-manual-msg');
    if (!token || !token.startsWith('Atzr|')) {
        if (msgEl) msgEl.textContent = 'El token debe comenzar con "Atzr|"';
        return;
    }
    if (token.length < 100) {
        if (msgEl) msgEl.textContent = 'Token demasiado corto — copia el token completo';
        return;
    }
    if (msgEl) msgEl.textContent = 'Guardando...';
    var fd = new FormData();
    fd.append('refresh_token', token);
    fetch('/auth/amazon/manual-token', {method: 'POST', body: fd})
        .then(function(r){ return r.json(); })
        .then(function(d){
            if (d.status === 'ok') {
                if (msgEl) msgEl.textContent = '✓ Token guardado. Recargando...';
                setTimeout(function(){ window.location.reload(); }, 1500);
            } else {
                if (msgEl) msgEl.textContent = 'Error: ' + (d.message || 'desconocido');
            }
        }).catch(function(e){
            if (msgEl) msgEl.textContent = 'Error de red: ' + e;
        });
}
function fixAmazonToken(btn) {
    if (btn) { btn.textContent = 'Verificando...'; btn.disabled = true; }
    var msgEl = document.getElementById('amazon-fix-msg');
    if (msgEl) msgEl.textContent = 'Verificando autorización SP-API...';
    fetch('/api/system-health/fix-amazon', {method:'POST'})
        .then(function(r){ return r.json(); })
        .then(function(d){
            if (d.status === 'ok') {
                if (msgEl) msgEl.textContent = d.message || 'OK — recargando...';
                setTimeout(function(){ window.location.reload(); }, 1500);
            } else if (d.status === 'reauth') {
                if (msgEl) msgEl.textContent = 'Abriendo autorización Amazon...';
                setTimeout(function(){ window.location.href = '/auth/amazon/connect'; }, 800);
            } else {
                if (msgEl) msgEl.textContent = 'Error: ' + (d.message || 'desconocido');
                if (btn) { btn.textContent = 'Reintentar'; btn.disabled = false; }
            }
        }).catch(function(e){
            if (msgEl) msgEl.textContent = 'Error de red: ' + e;
            if (btn) { btn.textContent = 'Reintentar'; btn.disabled = false; }
        });
}

// ─── Top Products ──────────────────────────────────────────────────────────
async function loadTopProducts() {
  var el = document.getElementById('amz-top-products-content');
  if (!el) return;
  el.innerHTML = '<div class="animate-pulse space-y-2">' + Array(5).fill('<div class="h-8 bg-orange-50 rounded"></div>').join('') + '</div>';
  var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/top-products' + sellerParam);
    var d = await r.json();
    if (!d.has_data || !d.items || !d.items.length) {
      el.innerHTML = '<div class="text-center py-6 text-gray-400 text-sm">Sin datos de ventas por SKU disponibles aún.<br><span class="text-xs">Los datos se procesan en segundo plano (puede tardar 1-2 minutos)</span></div>';
      return;
    }
    var _topRows = d.items.map(function(item) {
      var barColor = item.rank <= 3 ? '#F97316' : (item.rank <= 7 ? '#FB923C' : '#FED7AA');
      var row = '<tr class="border-b border-gray-50 hover:bg-gray-50">';
      row += '<td class="py-2.5 px-4"><span class="inline-flex w-6 h-6 rounded-full items-center justify-center text-xs font-bold text-white" style="background:' + barColor + '">' + item.rank + '</span></td>';
      row += '<td class="py-2.5 px-4"><div class="font-medium text-gray-700 text-xs">' + item.sku + '</div><div class="text-xs text-gray-400 truncate max-w-[200px]">' + (item.title || '—') + '</div></td>';
      row += '<td class="py-2.5 px-4 text-right font-semibold text-gray-700">' + item.units_30d + '</td>';
      row += '<td class="py-2.5 px-4 text-right font-bold text-orange-700">$' + (item.revenue_30d || 0).toLocaleString('es-MX', {minimumFractionDigits:2}) + '</td>';
      row += '<td class="py-2.5 px-4 text-right"><div class="flex items-center justify-end gap-1"><div class="w-16 bg-gray-200 rounded-full h-1.5"><div class="h-1.5 rounded-full bg-orange-400" style="width:' + Math.min(item.share_pct || 0, 100) + '%"></div></div><span class="text-xs text-gray-500">' + (item.share_pct || 0).toFixed(1) + '%</span></div></td>';
      row += '<td class="py-2.5 px-4 text-right ' + (item.fba_stock === 0 ? 'text-red-600 font-bold' : 'text-gray-600') + '">' + item.fba_stock + '</td>';
      row += '</tr>';
      return row;
    });
    el.innerHTML = '<div class="overflow-x-auto"><table class="w-full text-sm">' +
      '<thead><tr class="text-left text-xs text-gray-400 bg-gray-50"><th class="py-2.5 px-4">#</th><th class="py-2.5 px-4">SKU / Título</th><th class="py-2.5 px-4 text-right">Uds 30d</th><th class="py-2.5 px-4 text-right">Revenue</th><th class="py-2.5 px-4 text-right">% Total</th><th class="py-2.5 px-4 text-right">FBA Stock</th></tr></thead>' +
      '<tbody id="amz-top-tbody"></tbody></table></div><div id="amz-top-pag"></div>';
    window['amz-top-pag_go'] = function(p) { _renderPaginated(_topRows, p, 'amz-top-tbody', 'amz-top-pag'); };
    _renderPaginated(_topRows, 1, 'amz-top-tbody', 'amz-top-pag');
  } catch(e) {
    el.innerHTML = '<div class="text-center py-6 text-red-400 text-sm">Error al cargar top productos</div>';
  }
}

// ─── Ventas — sub-vista Resumen / Por SKU ───────────────────────────────────
var _amzVentasSkuLoaded = false;
var _amzSkuDays = 30;

window.setAmzVentasView = function(view) {
    ['resumen', 'sku'].forEach(function(v) {
        var el = document.getElementById('amz-ventas-' + v);
        if (el) el.classList.toggle('hidden', v !== view);
        var btn = document.getElementById('amz-ventas-view-' + v);
        if (btn) {
            btn.classList.toggle('bg-orange-500', v === view);
            btn.classList.toggle('text-white', v === view);
            btn.classList.toggle('text-gray-600', v !== view);
            btn.classList.toggle('hover:bg-orange-50', v !== view);
        }
    });
    if (view === 'sku' && !_amzVentasSkuLoaded) {
        _amzVentasSkuLoaded = true;
        loadAmzSkuSales();
    }
};

window.setAmzSkuPeriod = function(days) {
    _amzSkuDays = days;
    document.querySelectorAll('.amz-sku-preset').forEach(function(btn) {
        var isActive = parseInt(btn.dataset.days, 10) === days;
        btn.className = 'amz-sku-preset px-3 py-1 text-xs rounded-full transition font-medium ' +
            (isActive ? 'bg-orange-400 text-white font-semibold' : 'bg-gray-100 text-gray-600 hover:bg-orange-100 hover:text-orange-800');
    });
    loadAmzSkuSales();
};

function loadAmzSkuSales() {
    var wrap = document.getElementById('amz-sku-table-wrap');
    if (!wrap) return;
    if (!window.amzActiveSellerId) {
        wrap.innerHTML = '<div class="text-center py-10 text-red-400 text-sm">No hay cuenta Amazon activa — selecciona una en el menú de arriba.</div>';
        return;
    }
    wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">Cargando ventas por SKU...</div>';

    fetch('/partials/amazon-sku-sales-table?seller_id=' + encodeURIComponent(window.amzActiveSellerId) + '&days=' + _amzSkuDays)
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.error) { wrap.innerHTML = '<div class="text-center py-10 text-red-400 text-sm">' + d.error + '</div>'; return; }
            if (d.computing) {
                wrap.innerHTML = '<div class="flex items-center justify-center gap-3 py-10 text-sm text-gray-500">' +
                    '<svg class="animate-spin h-5 w-5 text-orange-500" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"></circle>' +
                    '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg>' +
                    'Calculando ventas por SKU (primera carga puede tardar varios minutos) — se cachea 3h…</div>';
                setTimeout(loadAmzSkuSales, 8000);
                return;
            }

            var rows = d.rows || [];
            var totalEl = document.getElementById('amz-sku-total');
            if (totalEl) totalEl.textContent = rows.length ? rows.length + ' SKUs con ventas' : '';
            if (!rows.length) { wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">Sin ventas en este periodo.</div>'; return; }

            var trs = rows.map(function(r) {
                var pctColor = r.pct_recuperado == null ? 'text-gray-400' : (r.pct_recuperado >= 100 ? 'text-green-600' : (r.pct_recuperado >= 70 ? 'text-yellow-600' : 'text-red-600'));
                return '<tr class="hover:bg-gray-50 border-b border-gray-200">' +
                    '<td class="px-4 py-3 text-sm font-mono font-medium text-gray-800">' + r.sku + '</td>' +
                    '<td class="px-4 py-3 text-sm text-gray-800">' + (r.title || r.sku).slice(0, 50) + '</td>' +
                    '<td class="px-4 py-3 text-sm text-right font-medium text-gray-800">' + r.units + '</td>' +
                    '<td class="px-4 py-3 text-sm text-right"><div class="font-medium text-gray-800">$' + r.revenue_mxn.toLocaleString('es-MX', {maximumFractionDigits: 0}) + ' <span class="text-xs text-gray-400">MXN</span></div></td>' +
                    '<td class="px-4 py-3 text-sm text-center">' + (r.bm_stock != null ? r.bm_stock : '<span class="text-gray-300">—</span>') + '</td>' +
                    '<td class="px-4 py-3 text-sm text-right">' + (r.retail_ph_usd > 0 ? '$' + r.retail_ph_usd.toFixed(2) + ' USD' : '<span class="text-gray-300">—</span>') + '</td>' +
                    '<td class="px-4 py-3 text-sm text-center font-semibold ' + pctColor + '">' + (r.pct_recuperado != null ? r.pct_recuperado + '%' : '—') + '</td>' +
                    '</tr>';
            });

            var cards = rows.map(function(r) {
                var pctColor = r.pct_recuperado == null ? 'text-gray-400' : (r.pct_recuperado >= 100 ? 'text-green-600' : (r.pct_recuperado >= 70 ? 'text-yellow-600' : 'text-red-600'));
                return '<div class="border border-gray-100 rounded-xl p-3">' +
                    '<div class="flex items-center justify-between gap-2">' +
                        '<span class="font-mono text-sm font-medium text-gray-800">' + r.sku + '</span>' +
                        '<span class="text-xs font-semibold ' + pctColor + '">' + (r.pct_recuperado != null ? r.pct_recuperado + '%' : '—') + '</span>' +
                    '</div>' +
                    '<p class="text-sm text-gray-700 mt-0.5">' + (r.title || r.sku).slice(0, 60) + '</p>' +
                    '<div class="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5 text-xs text-gray-500">' +
                        '<span>Cant. <b class="text-gray-700">' + r.units + '</b></span>' +
                        '<span>Ingreso <b class="text-gray-700">$' + r.revenue_mxn.toLocaleString('es-MX', {maximumFractionDigits: 0}) + ' MXN</b></span>' +
                        '<span>Stock BM ' + (r.bm_stock != null ? '<b class="text-gray-700">' + r.bm_stock + '</b>' : '—') + '</span>' +
                    '</div>' +
                '</div>';
            });
            wrap.innerHTML =
                '<div id="amz-sku-cards" class="md:hidden space-y-2"></div>' +
                '<div class="hidden md:block overflow-x-auto">' +
                '<table class="w-full"><thead class="bg-gray-50"><tr>' +
                '<th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase border-b-2 border-gray-300">SKU</th>' +
                '<th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase border-b-2 border-gray-300">Producto</th>' +
                '<th class="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase border-b-2 border-gray-300">Cantidad</th>' +
                '<th class="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase border-b-2 border-gray-300">Ingreso</th>' +
                '<th class="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase border-b-2 border-gray-300">Stock BM</th>' +
                '<th class="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase border-b-2 border-gray-300">Retail PH</th>' +
                '<th class="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase border-b-2 border-gray-300" title="Ingreso vs Retail PH de BM">% Recuperado</th>' +
                '</tr></thead><tbody id="amz-sku-tbody" class="divide-y divide-gray-200"></tbody></table>' +
                '</div>' +
                '<div id="amz-sku-pag"></div>';
            window['amz-sku-pag_go'] = function(p) {
                _renderPaginated(trs, p, 'amz-sku-tbody', 'amz-sku-pag');
                _renderPaginated(cards, p, 'amz-sku-cards', null);
            };
            _renderPaginated(trs, 1, 'amz-sku-tbody', 'amz-sku-pag');
            _renderPaginated(cards, 1, 'amz-sku-cards', null);
        })
        .catch(function(e) {
            wrap.innerHTML = '<div class="text-center py-10 text-red-400 text-sm">Error: ' + e.message + '</div>';
        });
}

// ─── FBA & Stock Tab ────────────────────────────────────────────────────────
var _fbaTabLoaded = false;
async function loadFbaTab() {
  _fbaTabLoaded = true;
  var wrap = document.getElementById('amz-fba-table-wrap');
  var kpis = document.getElementById('amz-fba-kpis');
  if (!wrap) return;
  var filterEl = document.getElementById('amz-fba-filter');
  var filter = filterEl ? filterEl.value : 'all';
  wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">Calculando cobertura de stock...</div>';
  var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/restock-report' + sellerParam);
    var d = await r.json();
    if (d.error) { wrap.innerHTML = '<div class="text-center py-10 text-red-400 text-sm">' + d.error + '</div>'; return; }

    // Render KPIs
    var s = d.summary || {};

    // ── IPI Estimate ─────────────────────────────────────────────────────────
    // Formula proxy: IPI ∝ sell_through × 1000
    // sell_through = ok_count / total (penaliza excess + sin stock)
    var totalActive  = s.total_active || 0;
    var okCount      = s.ok || 0;
    var criticalOut  = (s.critical || 0) + (s.out_of_stock || 0);
    var sellThrough  = totalActive > 0 ? (okCount / totalActive) : 0;
    var excessPenalty = totalActive > 0 ? Math.min(0.3, criticalOut / totalActive * 0.5) : 0;
    var ipiEst = Math.round(Math.max(0, Math.min(1000, (sellThrough - excessPenalty) * 800 + 200)));
    var ipiColor = ipiEst >= 450 ? '#10B981' : (ipiEst >= 350 ? '#F59E0B' : '#EF4444');
    var ipiLabel = ipiEst >= 450 ? 'Bueno' : (ipiEst >= 350 ? 'Regular' : 'Mejorar');

    if (kpis) {
      kpis.className = 'grid grid-cols-2 md:grid-cols-5 gap-3 mb-5';
      kpis.innerHTML = [
        {label:'Total activos', value: totalActive, icon:'📦', color:'#F97316', sub:'listings activos'},
        {label:'OK (>30 días)', value: okCount, icon:'✅', color:'#10B981', sub:'cobertura suficiente'},
        {label:'Advertencia', value: (s.warning || 0), icon:'⚠️', color:'#D97706', sub:'10-30 días stock'},
        {label:'Crítico / Sin stock', value: criticalOut, icon:'🚨', color:'#EF4444', sub:'necesitan reposición'},
      ].map(function(c) {
        return '<div class="bg-white rounded-xl shadow p-4 border-b-4" style="border-bottom-color:' + c.color + '">' +
          '<div class="flex items-center justify-between mb-2"><span class="text-xs text-gray-400 font-medium">' + c.label + '</span><span class="text-xl">' + c.icon + '</span></div>' +
          '<p class="text-2xl font-extrabold" style="color:' + c.color + '">' + c.value + '</p>' +
          '<p class="text-xs mt-1 text-gray-400">' + c.sub + '</p></div>';
      }).join('') +
      // IPI card
      '<div class="bg-white rounded-xl shadow p-4 border-b-4" style="border-bottom-color:' + ipiColor + '">' +
        '<div class="flex items-center justify-between mb-2"><span class="text-xs text-gray-400 font-medium">IPI Estimado</span><span class="text-xl">📊</span></div>' +
        '<p class="text-2xl font-extrabold" style="color:' + ipiColor + '">' + ipiEst + '</p>' +
        '<p class="text-xs mt-1 text-gray-400">' + ipiLabel + ' · Amazon meta: ≥450</p></div>';
    }

    // Filter items
    var items = d.items || [];
    if (filter === 'critical') items = items.filter(function(i){ return i.status === 'critical'; });
    else if (filter === 'warning') items = items.filter(function(i){ return i.status === 'warning'; });
    else if (filter === 'out') items = items.filter(function(i){ return i.status === 'out'; });

    if (!items.length) { wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">No hay items para el filtro seleccionado</div>'; return; }

    var _fbaRows = items.map(function(item) {
      var statusColor = {ok:'bg-green-100 text-green-700', warning:'bg-yellow-100 text-yellow-700', critical:'bg-red-100 text-red-700', out:'bg-gray-100 text-gray-600'};
      var statusLabel = {ok:'OK', warning:'⚠️ Advertencia', critical:'🚨 Crítico', out:'📦 Sin stock'};
      var sc = statusColor[item.status] || 'bg-gray-100 text-gray-500';
      var sl = statusLabel[item.status] || item.status;
      var daysText = item.days_coverage == null ? '∞' : (item.days_coverage > 999 ? '>999' : Math.round(item.days_coverage));
      var rowBg = item.status === 'critical' ? 'bg-red-50/30' : (item.status === 'out' ? 'bg-gray-50' : '');
      var row = '<tr class="' + rowBg + ' hover:bg-orange-50/20">';
      row += '<td class="py-3 px-4"><div class="font-medium text-gray-700 text-xs">' + item.sku + '</div><div class="text-xs text-gray-400 truncate max-w-[220px]">' + (item.title || '—') + '</div></td>';
      row += '<td class="py-3 px-3 text-right font-bold ' + (item.fba_stock === 0 ? 'text-red-600' : 'text-gray-800') + '">' + item.fba_stock + '</td>';
      row += '<td class="py-3 px-3 text-right text-indigo-600">' + (item.inbound || 0) + '</td>';
      row += '<td class="py-3 px-3 text-right text-gray-600">' + (item.velocity_daily > 0 ? item.velocity_daily : '—') + '</td>';
      row += '<td class="py-3 px-3 text-right font-bold ' + (item.status === 'critical' ? 'text-red-600' : item.status === 'warning' ? 'text-yellow-600' : 'text-gray-700') + '">' + daysText + 'd</td>';
      row += '<td class="py-3 px-3 text-right"><span class="' + (item.restock_qty > 0 ? 'font-bold text-orange-700' : 'text-gray-400') + '">' + (item.restock_qty > 0 ? item.restock_qty + ' u' : '—') + '</span></td>';
      row += '<td class="py-3 px-3 text-center"><span class="inline-block px-2 py-0.5 rounded-full text-xs font-medium ' + sc + '">' + sl + '</span></td>';
      // Action: for critical/out_of_stock items show link to create FBA shipment
      var needsAction = item.status === 'critical' || item.status === 'out';
      row += '<td class="py-3 px-3 text-center">' + (needsAction
        ? '<a href="https://sellercentral.amazon.com.mx/fba/sendtoamazon" target="_blank" class="text-xs bg-red-50 border border-red-200 text-red-600 hover:bg-red-100 rounded px-2 py-0.5 font-medium transition whitespace-nowrap">📦 Crear envío</a>'
        : '') + '</td>';
      row += '</tr>';
      return row;
    });
    var _fbaCards = items.map(function(item) {
      var statusColor = {ok:'bg-green-100 text-green-700', warning:'bg-yellow-100 text-yellow-700', critical:'bg-red-100 text-red-700', out:'bg-gray-100 text-gray-600'};
      var statusLabel = {ok:'OK', warning:'⚠️ Advertencia', critical:'🚨 Crítico', out:'📦 Sin stock'};
      var sc = statusColor[item.status] || 'bg-gray-100 text-gray-500';
      var sl = statusLabel[item.status] || item.status;
      var daysText = item.days_coverage == null ? '∞' : (item.days_coverage > 999 ? '>999' : Math.round(item.days_coverage));
      var needsAction = item.status === 'critical' || item.status === 'out';
      return '<div class="' + (item.status === 'critical' ? 'bg-red-50/30 ' : '') + 'border border-gray-100 rounded-xl p-3">' +
        '<div class="flex items-center justify-between gap-2">' +
          '<span class="text-xs font-medium text-gray-700">' + item.sku + '</span>' +
          '<span class="inline-block px-2 py-0.5 rounded-full text-xs font-medium ' + sc + '">' + sl + '</span>' +
        '</div>' +
        '<p class="text-xs text-gray-400 truncate mt-0.5">' + (item.title || '—') + '</p>' +
        '<div class="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5 text-xs text-gray-500">' +
          '<span>FBA <b class="' + (item.fba_stock === 0 ? 'text-red-600' : 'text-gray-800') + '">' + item.fba_stock + '</b></span>' +
          '<span>En camino <b class="text-indigo-600">' + (item.inbound || 0) + '</b></span>' +
          '<span>Uds/día ' + (item.velocity_daily > 0 ? item.velocity_daily : '—') + '</span>' +
          '<span>Días cobertura <b class="' + (item.status === 'critical' ? 'text-red-600' : item.status === 'warning' ? 'text-yellow-600' : 'text-gray-700') + '">' + daysText + 'd</b></span>' +
          (item.restock_qty > 0 ? '<span>Pedir <b class="text-orange-700">' + item.restock_qty + ' u</b></span>' : '') +
        '</div>' +
        (needsAction ? '<a href="https://sellercentral.amazon.com.mx/fba/sendtoamazon" target="_blank" class="mt-2 inline-block text-xs bg-red-50 border border-red-200 text-red-600 hover:bg-red-100 rounded px-2 py-1 font-medium transition">📦 Crear envío</a>' : '') +
      '</div>';
    });
    wrap.innerHTML =
      '<div id="amz-fba-cards" class="md:hidden space-y-2"></div>' +
      '<div class="hidden md:block overflow-x-auto"><table class="w-full text-sm">' +
      '<thead><tr class="text-left text-xs text-gray-400 bg-gray-50">' +
      '<th class="py-3 px-4 font-semibold">SKU / Producto</th>' +
      '<th class="py-3 px-3 text-right font-semibold">FBA Stock</th>' +
      '<th class="py-3 px-3 text-right font-semibold">En camino</th>' +
      '<th class="py-3 px-3 text-right font-semibold">Uds/día 30d</th>' +
      '<th class="py-3 px-3 text-right font-semibold">Días cobertura</th>' +
      '<th class="py-3 px-3 text-right font-semibold">Qty a pedir</th>' +
      '<th class="py-3 px-3 text-center font-semibold">Estado</th>' +
      '<th class="py-3 px-3 text-center font-semibold">Acción</th>' +
      '</tr></thead><tbody id="amz-fba-tbody" class="divide-y divide-gray-50"></tbody></table></div>' +
      '<div id="amz-fba-pag"></div>';
    window['amz-fba-pag_go'] = function(p) {
      _renderPaginated(_fbaRows, p, 'amz-fba-tbody', 'amz-fba-pag');
      _renderPaginated(_fbaCards, p, 'amz-fba-cards', null);
    };
    _renderPaginated(_fbaRows, 1, 'amz-fba-tbody', 'amz-fba-pag');
    _renderPaginated(_fbaCards, 1, 'amz-fba-cards', null);
  } catch(e) {
    wrap.innerHTML = '<div class="text-center py-10 text-red-400 text-sm">Error al cargar restock: ' + e.message + '</div>';
  }
}

// ─── Listings Quality Tab ───────────────────────────────────────────────────
var _listingsTabLoaded = false;
async function loadListingsTab() {
  _listingsTabLoaded = true;
  var wrap = document.getElementById('amz-lq-table-wrap');
  var summaryEl = document.getElementById('amz-lq-summary');
  if (!wrap) return;
  wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">Analizando calidad de listings...</div>';
  var filterEl = document.getElementById('amz-lq-filter');
  var filter = filterEl ? filterEl.value : 'all';
  var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/listing-quality' + sellerParam);
    var d = await r.json();
    if (d.error) { wrap.innerHTML = '<div class="text-center py-10 text-red-400 text-sm">' + d.error + '</div>'; return; }

    var s = d.summary || {};
    if (summaryEl) {
      summaryEl.innerHTML = [
        {label:'Score Promedio', value: (s.avg_score||0).toFixed(1), icon:'📊', color:'#F97316', sub:'de 100 puntos'},
        {label:'Grado A (≥85)', value: s.grade_A||0, icon:'🟢', color:'#10B981', sub:'excelente calidad'},
        {label:'Grado B/C', value: (s.grade_B||0)+(s.grade_C||0), icon:'🟡', color:'#D97706', sub:'mejorar'},
        {label:'Grado D (<55)', value: s.grade_D||0, icon:'🔴', color:'#EF4444', sub:'crítico'},
      ].map(function(c) {
        return '<div class="bg-white rounded-xl shadow p-4 border-b-4" style="border-bottom-color:' + c.color + '">' +
          '<div class="flex items-center justify-between mb-2"><span class="text-xs text-gray-400 font-medium">' + c.label + '</span><span class="text-xl">' + c.icon + '</span></div>' +
          '<p class="text-2xl font-extrabold" style="color:' + c.color + '">' + c.value + '</p>' +
          '<p class="text-xs mt-1 text-gray-400">' + c.sub + '</p></div>';
      }).join('');
    }

    var items = d.items || [];
    if (filter === 'D') items = items.filter(function(i){ return i.grade === 'D'; });
    else if (filter === 'C') items = items.filter(function(i){ return i.grade === 'C' || i.grade === 'D'; });
    else if (filter === 'issues') items = items.filter(function(i){ return i.issues_count > 0; });

    if (!items.length) { wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">No hay listings para el filtro seleccionado</div>'; return; }

    var _lqRows = items.map(function(item) {
      var gradeColor = {A:'bg-green-100 text-green-700', B:'bg-blue-100 text-blue-700', C:'bg-yellow-100 text-yellow-700', D:'bg-red-100 text-red-700'};
      var scoreColor = item.score >= 85 ? '#10B981' : (item.score >= 70 ? '#3B82F6' : (item.score >= 55 ? '#D97706' : '#EF4444'));
      var statusColors = {ACTIVE:'text-green-600', INACTIVE:'text-gray-400', SUPPRESSED:'text-red-600', DISCOVERABLE:'text-yellow-600'};
      var gc = gradeColor[item.grade] || 'bg-gray-100 text-gray-500';
      var issuesHtml = (item.issues || []).map(function(i){ return '<div class="text-xs text-red-600 truncate max-w-[220px]">• ' + i + '</div>'; }).join('');
      var bsrHtml = (item.bsr_rank != null)
        ? '<div class="text-xs font-bold text-indigo-600">#' + item.bsr_rank.toLocaleString('es-MX') + '</div>' +
          (item.bsr_category ? '<div class="text-xs text-gray-400 truncate max-w-[120px]">' + item.bsr_category + '</div>' : '')
        : '<span class="text-xs text-gray-300">—</span>';
      var row = '<tr class="hover:bg-gray-50">';
      row += '<td class="py-3 px-4"><div class="font-medium text-gray-700 text-xs">' + item.sku + '</div><div class="text-xs text-gray-400 truncate max-w-[250px]">' + (item.title || '—') + '</div></td>';
      row += '<td class="py-3 px-3 text-center"><span class="inline-block w-8 py-0.5 rounded-full text-xs font-bold ' + gc + '">' + item.grade + '</span></td>';
      row += '<td class="py-3 px-3 text-center"><div class="flex items-center justify-center gap-1"><div class="w-14 bg-gray-200 rounded-full h-2"><div class="h-2 rounded-full" style="width:' + item.score + '%;background:' + scoreColor + '"></div></div><span class="text-xs font-bold" style="color:' + scoreColor + '">' + item.score + '</span></div></td>';
      row += '<td class="py-3 px-3 text-center text-xs font-medium ' + (statusColors[item.status] || 'text-gray-500') + '">' + item.status + '</td>';
      row += '<td class="py-3 px-3">' + bsrHtml + '</td>';
      row += '<td class="py-3 px-4">' + (issuesHtml || '<span class="text-xs text-green-600">Sin issues</span>') + '</td>';
      // Action: link to edit the listing in Seller Central (by SKU)
      var encodedSku = encodeURIComponent(item.sku);
      var editUrl = 'https://sellercentral.amazon.com.mx/inventory?search=' + encodedSku;
      row += '<td class="py-3 px-3 text-center">' +
        '<a href="' + editUrl + '" target="_blank" class="text-xs bg-blue-50 border border-blue-200 text-blue-600 hover:bg-blue-100 rounded px-2 py-0.5 font-medium transition whitespace-nowrap">✏️ Editar</a>' +
        '</td>';
      row += '</tr>';
      return row;
    });
    var _lqCards = items.map(function(item) {
      var gradeColor = {A:'bg-green-100 text-green-700', B:'bg-blue-100 text-blue-700', C:'bg-yellow-100 text-yellow-700', D:'bg-red-100 text-red-700'};
      var scoreColor = item.score >= 85 ? '#10B981' : (item.score >= 70 ? '#3B82F6' : (item.score >= 55 ? '#D97706' : '#EF4444'));
      var statusColors = {ACTIVE:'text-green-600', INACTIVE:'text-gray-400', SUPPRESSED:'text-red-600', DISCOVERABLE:'text-yellow-600'};
      var gc = gradeColor[item.grade] || 'bg-gray-100 text-gray-500';
      var issuesHtml = (item.issues || []).map(function(i){ return '<div class="text-xs text-red-600 truncate">• ' + i + '</div>'; }).join('');
      var encodedSku = encodeURIComponent(item.sku);
      var editUrl = 'https://sellercentral.amazon.com.mx/inventory?search=' + encodedSku;
      return '<div class="border border-gray-100 rounded-xl p-3">' +
        '<div class="flex items-center justify-between gap-2">' +
          '<span class="text-xs font-medium text-gray-700">' + item.sku + '</span>' +
          '<span class="inline-block w-8 py-0.5 rounded-full text-xs font-bold text-center ' + gc + '">' + item.grade + '</span>' +
        '</div>' +
        '<p class="text-xs text-gray-400 truncate mt-0.5">' + (item.title || '—') + '</p>' +
        '<div class="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5 text-xs text-gray-500">' +
          '<span>Score <b style="color:' + scoreColor + '">' + item.score + '</b></span>' +
          '<span class="font-medium ' + (statusColors[item.status] || 'text-gray-500') + '">' + item.status + '</span>' +
          (item.bsr_rank != null ? '<span>BSR <b class="text-indigo-600">#' + item.bsr_rank.toLocaleString('es-MX') + '</b></span>' : '') +
        '</div>' +
        '<div class="mt-1.5">' + (issuesHtml || '<span class="text-xs text-green-600">Sin issues</span>') + '</div>' +
        '<a href="' + editUrl + '" target="_blank" class="mt-2 inline-block text-xs bg-blue-50 border border-blue-200 text-blue-600 hover:bg-blue-100 rounded px-2 py-1 font-medium transition">✏️ Editar</a>' +
      '</div>';
    });
    wrap.innerHTML =
      '<div id="amz-lq-cards" class="md:hidden space-y-2"></div>' +
      '<div class="hidden md:block overflow-x-auto"><table class="w-full text-sm">' +
      '<thead><tr class="text-left text-xs text-gray-400 bg-gray-50">' +
      '<th class="py-3 px-4 font-semibold">SKU / Título</th>' +
      '<th class="py-3 px-3 text-center font-semibold">Grado</th>' +
      '<th class="py-3 px-3 text-center font-semibold">Score</th>' +
      '<th class="py-3 px-3 text-center font-semibold">Estado</th>' +
      '<th class="py-3 px-3 font-semibold">BSR</th>' +
      '<th class="py-3 px-3 font-semibold">Issues</th>' +
      '<th class="py-3 px-3 text-center font-semibold">Acción</th>' +
      '</tr></thead><tbody id="amz-lq-tbody" class="divide-y divide-gray-50"></tbody></table></div>' +
      '<div id="amz-lq-pag"></div>';
    window['amz-lq-pag_go'] = function(p) {
      _renderPaginated(_lqRows, p, 'amz-lq-tbody', 'amz-lq-pag');
      _renderPaginated(_lqCards, p, 'amz-lq-cards', null);
    };
    _renderPaginated(_lqRows, 1, 'amz-lq-tbody', 'amz-lq-pag');
    _renderPaginated(_lqCards, 1, 'amz-lq-cards', null);
  } catch(e) {
    wrap.innerHTML = '<div class="text-center py-10 text-red-400 text-sm">Error al cargar listings: ' + e.message + '</div>';
  }
}

// ─── AI Advisor ─────────────────────────────────────────────────────────────
var amzAIDrawerOpen = false;
var amzAIMode = 'general';
var amzAIStreaming = false;

function toggleAmzAIDrawer() {
  amzAIDrawerOpen = !amzAIDrawerOpen;
  var drawer = document.getElementById('amz-ai-drawer');
  var overlay = document.getElementById('amz-ai-overlay');
  if (amzAIDrawerOpen) {
    drawer.classList.remove('translate-x-full');
    overlay.classList.remove('hidden');
  } else {
    drawer.classList.add('translate-x-full');
    overlay.classList.add('hidden');
  }
}

function setAmzAIMode(mode) {
  amzAIMode = mode;
  document.querySelectorAll('.amz-ai-mode-btn').forEach(function(btn) {
    var active = btn.getAttribute('data-mode') === mode;
    btn.classList.toggle('border-orange-400', active);
    btn.classList.toggle('bg-orange-50', active);
    btn.classList.toggle('text-orange-700', active);
    btn.classList.toggle('border-gray-200', !active);
    btn.classList.toggle('text-gray-600', !active);
    btn.classList.toggle('bg-white', !active);
  });
}

function askAmzAI(question) {
  var input = document.getElementById('amz-ai-input');
  if (input) input.value = question;
  sendAmzAI();
}

async function sendAmzAI() {
  if (amzAIStreaming) return;
  var input = document.getElementById('amz-ai-input');
  var sendBtn = document.getElementById('amz-ai-send');
  var messages = document.getElementById('amz-ai-messages');
  var question = (input ? input.value.trim() : '');
  if (!question) return;

  // Add user bubble
  var userBubble = '<div class="flex justify-end"><div class="bg-orange-500 text-white rounded-2xl rounded-tr-sm px-4 py-3 text-sm max-w-[85%]">' + question.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div></div>';
  messages.insertAdjacentHTML('beforeend', userBubble);
  if (input) input.value = '';

  // Add AI bubble placeholder
  var aiBubbleId = 'amz-ai-bubble-' + Date.now();
  messages.insertAdjacentHTML('beforeend',
    '<div class="flex gap-3"><div class="w-8 h-8 rounded-full bg-gray-800 flex items-center justify-center text-white text-sm shrink-0">🤖</div>' +
    '<div class="bg-gray-100 rounded-2xl rounded-tl-sm px-4 py-3 text-sm text-gray-800 max-w-[85%] flex-1" id="' + aiBubbleId + '">' +
    '<span class="inline-block w-2 h-2 rounded-full bg-gray-400 animate-bounce"></span>' +
    '<span class="inline-block w-2 h-2 rounded-full bg-gray-400 animate-bounce mx-1" style="animation-delay:0.15s"></span>' +
    '<span class="inline-block w-2 h-2 rounded-full bg-gray-400 animate-bounce" style="animation-delay:0.3s"></span>' +
    '</div></div>'
  );
  messages.scrollTop = messages.scrollHeight;

  amzAIStreaming = true;
  if (sendBtn) { sendBtn.disabled = true; sendBtn.textContent = '...'; }

  var sellerParam = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var resp = await fetch('/api/amazon/ai-advisor' + sellerParam, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: question, mode: amzAIMode})
    });

    var bubble = document.getElementById(aiBubbleId);
    var text = '';
    bubble.innerHTML = '';

    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';

    while (true) {
      var result = await reader.read();
      if (result.done) break;
      buffer += decoder.decode(result.value, {stream: true});
      var lines = buffer.split('\n');
      buffer = lines.pop();
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (line.startsWith('data: ')) {
          var chunk = line.slice(6);
          if (chunk === '[DONE]') break;
          try {
            var obj = JSON.parse(chunk);
            if (obj.text) {
              text += obj.text;
              bubble.innerHTML = text.replace(/\n/g, '<br>').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>').replace(/\*(.*?)\*/g, '<em>$1</em>');
              messages.scrollTop = messages.scrollHeight;
            } else if (obj.error) {
              bubble.innerHTML = '<span class="text-red-500">Error: ' + obj.error + '</span>';
            }
          } catch(e2) {}
        }
      }
    }
  } catch(e) {
    var bubble2 = document.getElementById(aiBubbleId);
    if (bubble2) bubble2.innerHTML = '<span class="text-red-500">Error de conexión: ' + e.message + '</span>';
  } finally {
    amzAIStreaming = false;
    if (sendBtn) { sendBtn.disabled = false; sendBtn.textContent = 'Enviar'; }
    messages.scrollTop = messages.scrollHeight;
  }
}

// ─── TAB DEALS ───────────────────────────────────────────────────────────────

// Estado de paginación por tabla
var _dealsPage = 1, _pricingPage = 1, _candidatesPage = 1;
var _dealsRows = [], _pricingRows = [], _candidatesRows = [];
var _PAGE_SIZE = 10;

function _renderPaginated(rows, page, tbodyId, paginatorId) {
  var total = rows.length;
  var totalPages = Math.max(1, Math.ceil(total / _PAGE_SIZE));
  var p = Math.min(Math.max(1, page), totalPages);
  var start = (p - 1) * _PAGE_SIZE, end = Math.min(start + _PAGE_SIZE, total);
  var tbody = document.getElementById(tbodyId);
  if (tbody) tbody.innerHTML = rows.slice(start, end).join('');
  var pag = document.getElementById(paginatorId);
  if (!pag) return p;
  if (total <= _PAGE_SIZE) { pag.innerHTML = ''; return p; }
  pag.innerHTML =
    '<div class="flex items-center justify-between px-4 py-3 border-t border-gray-100 bg-gray-50">' +
    '<span class="text-xs text-gray-400">' + (start+1) + '–' + end + ' de ' + total + '</span>' +
    '<div class="flex items-center gap-1">' +
      '<button onclick="window[\'' + paginatorId + '_go\'](' + (p-1) + ')" ' + (p<=1?'disabled':'') +
        ' class="px-3 py-1.5 text-xs rounded-lg border border-gray-200 text-gray-600 hover:bg-white disabled:opacity-30 disabled:cursor-not-allowed transition">‹ Ant</button>' +
      '<span class="px-3 text-xs text-gray-600 font-medium">' + p + ' / ' + totalPages + '</span>' +
      '<button onclick="window[\'' + paginatorId + '_go\'](' + (p+1) + ')" ' + (p>=totalPages?'disabled':'') +
        ' class="px-3 py-1.5 text-xs rounded-lg border border-gray-200 text-gray-600 hover:bg-white disabled:opacity-30 disabled:cursor-not-allowed transition">Sig ›</button>' +
    '</div></div>';
  return p;
}

function loadDealsTab() {
  loadDealsSection();
  loadCompPricingSection(false);
  loadDealCandidates();
}

async function loadDealsSection() {
  var el = document.getElementById('amz-deals-list');
  if (!el) return;
  el.innerHTML = '<div class="text-center py-8 text-gray-400 text-sm">Cargando deals...</div>';
  var status = (document.getElementById('amz-deals-filter') || {}).value || '';
  var sp = window.amzActiveSellerId ? '&seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/deals?status=' + status + sp);
    var d = await r.json();
    var kpis = document.getElementById('amz-deals-kpis');
    if (kpis) {
      kpis.innerHTML = [
        { label: 'Activos ahora', val: d.running_count || 0,  color: '#10B981', icon: '⚡' },
        { label: 'Próximos',      val: d.upcoming_count || 0, color: '#F97316', icon: '📅' },
        { label: 'Total deals',   val: d.total || 0,          color: '#6366F1', icon: '🏷️' },
        { label: 'Estado',        val: d.error ? '⚠️' : '✓', color: '#64748B', icon: '🔔' },
      ].map(function(k) {
        return '<div class="bg-white p-4 rounded-xl shadow">' +
          '<p class="text-xs text-gray-400 mb-1">' + k.icon + ' ' + k.label + '</p>' +
          '<p class="text-2xl font-bold" style="color:' + k.color + '">' + k.val + '</p>' +
        '</div>';
      }).join('');
    }
    if (!d.deals || d.deals.length === 0) {
      var msg = d.no_deals_msg || 'Sin deals para mostrar.';
      el.innerHTML = '<div class="flex flex-col items-center py-12 px-6 text-center">' +
        '<div class="text-5xl mb-3">🏷️</div>' +
        '<p class="text-gray-500 text-sm max-w-md mb-4">' + msg + '</p>' +
        '<a href="https://sellercentral.amazon.com.mx/gp/lightning-deals/home" target="_blank" ' +
           'class="text-xs bg-orange-500 text-white px-4 py-2 rounded-lg hover:bg-orange-600 transition">' +
           'Crear Deal en Seller Central →</a>' +
      '</div>';
      return;
    }
    var scBadge = { RUNNING: 'bg-green-100 text-green-700', UPCOMING: 'bg-blue-100 text-blue-700', ENDED: 'bg-gray-100 text-gray-500' };
    var tLabel  = { LIGHTNING_DEAL: '⚡ Lightning', BEST_DEAL: '⭐ Best Deal' };
    _dealsRows = d.deals.map(function(deal) {
      var prog = deal.progress_pct != null
        ? '<div class="w-full bg-gray-200 rounded-full h-1 mt-1"><div class="bg-green-500 h-1 rounded-full" style="width:' + Math.min(deal.progress_pct,100) + '%"></div></div>'
        : '';
      return '<tr class="border-b border-gray-50 hover:bg-orange-50 transition text-sm">' +
        '<td class="px-4 py-3 font-medium text-gray-800 max-w-xs truncate">' + (deal.title || '—') + '</td>' +
        '<td class="px-4 py-3 text-xs text-gray-500">' + (tLabel[deal.deal_type] || deal.deal_type) + '</td>' +
        '<td class="px-4 py-3"><span class="text-xs font-medium px-2 py-0.5 rounded-full ' + (scBadge[deal.status] || 'bg-gray-100 text-gray-500') + '">' + deal.status + '</span></td>' +
        '<td class="px-4 py-3">' + (deal.deal_price ? '$' + fmtMoney(deal.deal_price) : '—') + (deal.discount_pct > 0 ? ' <span class="text-xs font-bold text-red-500">-' + deal.discount_pct + '%</span>' : '') + '</td>' +
        '<td class="px-4 py-3 text-xs text-gray-400">' + (deal.start_time || '—') + '</td>' +
        '<td class="px-4 py-3 text-xs text-gray-400">' + (deal.end_time || '—') + '</td>' +
        '<td class="px-4 py-3 text-gray-600">' + (deal.units_sold != null ? deal.units_sold + ' uds' : '—') + prog + '</td>' +
      '</tr>';
    });
    _dealsPage = 1;
    el.innerHTML = '<div class="overflow-x-auto"><table class="w-full text-left">' +
      '<thead><tr class="bg-gray-50 text-xs text-gray-400 uppercase tracking-wide">' +
      '<th class="px-4 py-3">Título</th><th class="px-4 py-3">Tipo</th><th class="px-4 py-3">Estado</th>' +
      '<th class="px-4 py-3">Precio deal</th><th class="px-4 py-3">Inicio</th><th class="px-4 py-3">Fin</th>' +
      '<th class="px-4 py-3">Progreso</th></tr></thead>' +
      '<tbody id="amz-deals-tbody"></tbody></table></div>' +
      '<div id="amz-deals-pag"></div>';
    window['amz-deals-pag_go'] = function(p) { _dealsPage = _renderPaginated(_dealsRows, p, 'amz-deals-tbody', 'amz-deals-pag'); };
    _dealsPage = _renderPaginated(_dealsRows, 1, 'amz-deals-tbody', 'amz-deals-pag');
  } catch(e) {
    el.innerHTML = '<div class="text-center py-8 text-red-400 text-sm">Error: ' + e.message + '</div>';
  }
}

async function loadCompPricingSection(forceRefresh) {
  var wrap = document.getElementById('amz-pricing-table');
  if (!wrap) return;
  wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">Consultando precios competitivos… puede tardar ~20s la primera vez</div>';
  var sp = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/competitive-pricing?limit=20' + (sp ? '&' + sp.slice(1) : ''));
    var d = await r.json();
    // Buy Box Win % KPI card
    var bbWinPct = d.buybox_win_pct != null ? d.buybox_win_pct : null;
    var bbWinEl = document.getElementById('amz-bb-win-kpi');
    if (bbWinEl && bbWinPct != null) {
      var bbColor = bbWinPct >= 70 ? '#10B981' : (bbWinPct >= 40 ? '#F59E0B' : '#EF4444');
      bbWinEl.innerHTML =
        '<p class="text-xs text-gray-400 mb-1">🏆 Buy Box Win %</p>' +
        '<p class="text-2xl font-extrabold" style="color:' + bbColor + '">' + bbWinPct + '%</p>' +
        '<p class="text-xs text-gray-400 mt-1">' + (d.at_bb_count || 0) + ' de ' + (d.total || (d.at_bb_count||0)+(d.above_bb_count||0)+(d.below_bb_count||0)+(d.no_bb_count||0)) + ' analizados</p>';
      bbWinEl.style.borderBottom = '4px solid ' + bbColor;
    }
    // Mini KPIs
    var mkEl = document.getElementById('amz-pricing-mini-kpis');
    if (mkEl) {
      mkEl.innerHTML = [
        { label: 'Por encima BB', val: d.above_bb_count, bg: 'bg-red-50',    color: '#EF4444' },
        { label: 'En el BB',      val: d.at_bb_count,    bg: 'bg-green-50',  color: '#10B981' },
        { label: 'Por debajo BB', val: d.below_bb_count, bg: 'bg-indigo-50', color: '#6366F1' },
        { label: 'Sin Buy Box',   val: d.no_bb_count,    bg: 'bg-gray-50',   color: '#9CA3AF' },
      ].map(function(c) {
        return '<div class="' + c.bg + ' px-4 py-3 text-center">' +
          '<p class="text-xs text-gray-400 mb-1">' + c.label + '</p>' +
          '<p class="text-xl font-bold" style="color:' + c.color + '">' + (c.val || 0) + '</p>' +
        '</div>';
      }).join('');
    }
    if (!d.items || d.items.length === 0) {
      wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">Sin datos de pricing disponibles.</div>';
      return;
    }
    var sIcon = { above_bb: '🔴', at_bb: '🟢', below_bb: '🔵', no_bb: '⚪' };
    _pricingRows = d.items.map(function(item) {
      var gapCls = item.status === 'above_bb' ? 'text-red-600 font-semibold' :
                   item.status === 'at_bb'    ? 'text-green-600' :
                   item.status === 'below_bb' ? 'text-indigo-600' : 'text-gray-400';
      var gapStr = item.buybox_price
        ? (item.gap > 0 ? '+$' : '-$') + fmtMoney(Math.abs(item.gap)) + ' (' + item.gap_pct + '%)'
        : '—';
      // Action column: for above_bb → copy BB price; for others → text hint
      var actionCell;
      if (item.status === 'above_bb' && item.buybox_price) {
        var bbPrice = item.buybox_price.toFixed(2);
        actionCell = '<div class="flex flex-col gap-1">' +
          '<span class="text-xs text-red-600">' + item.action + '</span>' +
          '<button onclick="(function(b){navigator.clipboard.writeText(\'' + bbPrice + '\').then(function(){b.textContent=\'✓ Copiado\';setTimeout(function(){b.textContent=\'📋 Copiar $' + fmtMoney(item.buybox_price) + '\';},1500);});})(this)" ' +
          'class="text-xs bg-orange-50 border border-orange-200 text-orange-600 hover:bg-orange-100 rounded px-2 py-0.5 font-medium transition whitespace-nowrap">📋 Copiar $' + fmtMoney(item.buybox_price) + '</button>' +
        '</div>';
      } else {
        actionCell = '<span class="text-xs text-gray-500">' + item.action + '</span>';
      }
      return '<tr class="border-b border-gray-50 hover:bg-orange-50 transition text-sm">' +
        '<td class="px-4 py-3"><p class="font-medium text-gray-700 text-xs">' + item.sku + '</p>' +
          '<p class="text-gray-400 text-xs truncate max-w-xs">' + item.title + '</p></td>' +
        '<td class="px-4 py-3 text-xs">' + (item.asin ? '<a href="' + item.amazon_url + '" target="_blank" class="text-orange-500 hover:underline">' + item.asin + '</a>' : '—') + '</td>' +
        '<td class="px-4 py-3 font-medium text-gray-800">$' + fmtMoney(item.our_price) + '</td>' +
        '<td class="px-4 py-3 text-gray-600">' + (item.buybox_price ? '$' + fmtMoney(item.buybox_price) : '—') + '</td>' +
        '<td class="px-4 py-3 ' + gapCls + '">' + sIcon[item.status] + ' ' + gapStr + '</td>' +
        '<td class="px-4 py-3 max-w-xs">' + actionCell + '</td>' +
        '<td class="px-4 py-3 text-center ' + (item.fba_stock === 0 ? 'text-red-600 font-bold' : 'text-gray-600') + '">' + item.fba_stock + '</td>' +
      '</tr>';
    });
    _pricingPage = 1;
    wrap.innerHTML = '<div class="overflow-x-auto"><table class="w-full text-left">' +
      '<thead><tr class="bg-gray-50 text-xs text-gray-400 uppercase tracking-wide">' +
      '<th class="px-4 py-3">Producto</th><th class="px-4 py-3">ASIN</th>' +
      '<th class="px-4 py-3">Nuestro precio</th><th class="px-4 py-3">Buy Box</th>' +
      '<th class="px-4 py-3">Gap</th><th class="px-4 py-3">Recomendación</th>' +
      '<th class="px-4 py-3 text-center">Stock FBA</th></tr></thead>' +
      '<tbody id="amz-pricing-tbody"></tbody></table></div>' +
      '<div id="amz-pricing-pag"></div>';
    window['amz-pricing-pag_go'] = function(p) { _pricingPage = _renderPaginated(_pricingRows, p, 'amz-pricing-tbody', 'amz-pricing-pag'); };
    _pricingPage = _renderPaginated(_pricingRows, 1, 'amz-pricing-tbody', 'amz-pricing-pag');
  } catch(e) {
    wrap.innerHTML = '<div class="text-center py-8 text-red-400 text-sm">Error: ' + e.message + '</div>';
  }
}

var _candidatesData = [];
var _candidatesSortKey = 'rev_forecast';
var _candidatesSortDir = -1; // -1 desc, 1 asc
var _candidatesNoSalesData = false;

function _buildCandidatesRow(c) {
  var badge = c.is_on_deal
    ? '<span class="ml-1 text-xs bg-orange-100 text-orange-700 px-1.5 py-0.5 rounded font-medium">En deal -' + c.current_deal_pct + '%</span>'
    : '';
  var daysStr = c.days_inventory != null ? Math.round(c.days_inventory) + 'd' : '∞';
  var salesStr = _candidatesNoSalesData ? '?' : c.units_sold_30d;
  var dealPrice = c.deal_price_suggestion || 0;
  var revForecast = c.fba_stock > 0 ? '$' + fmtMoney(c.rev_forecast) : '—';
  var scBase = 'https://sellercentral.amazon.com.mx';
  var copyBtn = '<button onclick="navigator.clipboard.writeText(\'' + dealPrice.toFixed(2) + '\').then(function(){var b=this;b.textContent=\'✓\';setTimeout(function(){b.textContent=\'📋\';},1500);}).catch(function(){})" ' +
    'title="Copiar precio al portapapeles" class="ml-1 text-gray-400 hover:text-orange-500 transition text-xs">📋</button>';
  var scDealLink = c.asin
    ? '<a href="' + scBase + '/merchandising/deals" target="_blank" class="block text-xs text-orange-600 font-medium hover:underline whitespace-nowrap">Crear Deal →</a>'
    : '';
  var scProdLink = c.asin
    ? '<a href="' + c.amazon_url + '" target="_blank" class="block text-xs text-gray-400 hover:text-orange-500 hover:underline whitespace-nowrap">Ver ASIN</a>'
    : '—';
  return '<tr class="border-b border-gray-50 hover:bg-orange-50 transition text-sm">' +
    '<td class="px-4 py-3"><p class="font-medium text-gray-700 text-xs">' + c.sku + badge + '</p>' +
      '<p class="text-gray-400 text-xs truncate max-w-xs">' + c.title + '</p></td>' +
    '<td class="px-4 py-3 font-medium text-gray-800">$' + fmtMoney(c.our_price) + '</td>' +
    '<td class="px-4 py-3"><p class="font-bold text-orange-600">$' + fmtMoney(dealPrice) + copyBtn + '</p>' +
      '<p class="text-xs text-gray-400">-' + c.discount_pct + '%</p></td>' +
    '<td class="px-4 py-3 text-center font-bold text-indigo-700">' + c.fba_stock + '</td>' +
    '<td class="px-4 py-3 text-center text-gray-600">' + salesStr + '</td>' +
    '<td class="px-4 py-3 text-center text-gray-500 text-xs">' + daysStr + '</td>' +
    '<td class="px-4 py-3 text-right text-xs font-medium text-green-700">' + revForecast + '</td>' +
    '<td class="px-4 py-3 text-xs text-gray-500">' + c.reason + '</td>' +
    '<td class="px-4 py-3">' + scDealLink + scProdLink + '</td>' +
  '</tr>';
}

function _sortCandidates(key) {
  if (_candidatesSortKey === key) _candidatesSortDir *= -1;
  else { _candidatesSortKey = key; _candidatesSortDir = -1; }
  _candidatesData.sort(function(a, b) {
    var va = a[key] != null ? a[key] : -Infinity;
    var vb = b[key] != null ? b[key] : -Infinity;
    return (va < vb ? -1 : va > vb ? 1 : 0) * _candidatesSortDir;
  });
  _candidatesRows = _candidatesData.map(_buildCandidatesRow);
  _candidatesPage = 1;
  _renderPaginatedCandidates();
  // Update sort indicators
  document.querySelectorAll('.cand-sort-btn').forEach(function(btn) {
    var bk = btn.getAttribute('data-sort');
    btn.querySelector('span').textContent = bk === _candidatesSortKey ? (_candidatesSortDir === -1 ? ' ▼' : ' ▲') : ' ↕';
  });
}

function _renderPaginatedCandidates() {
  _candidatesPage = _renderPaginated(_candidatesRows, _candidatesPage, 'amz-candidates-tbody', 'amz-candidates-pag');
}

function _renderCandidatesTable() {
  var totalRevForecast = _candidatesData.reduce(function(s, c) { return s + (c.rev_forecast || 0); }, 0);
  var sortArrow = function(k) { return k === _candidatesSortKey ? (_candidatesSortDir === -1 ? ' ▼' : ' ▲') : ' ↕'; };
  var thSort = function(k, label, cls) {
    return '<th class="px-4 py-3' + (cls ? ' ' + cls : '') + '">' +
      '<button class="cand-sort-btn flex items-center gap-0.5 hover:text-orange-500 transition" data-sort="' + k + '" onclick="_sortCandidates(\'' + k + '\')">' +
      label + '<span class="text-gray-300 text-xs">' + sortArrow(k) + '</span></button></th>';
  };
  var wrap = document.getElementById('amz-candidates-table');
  if (!wrap) return;
  wrap.innerHTML = '<div class="overflow-x-auto"><table class="w-full text-left">' +
    '<thead><tr class="bg-gray-50 text-xs text-gray-400 uppercase tracking-wide">' +
    '<th class="px-4 py-3">Producto</th>' +
    thSort('our_price', 'Precio actual') +
    thSort('deal_price_suggestion', 'Deal sugerido') +
    thSort('fba_stock', 'Stock FBA', 'text-center') +
    thSort('units_sold_30d', 'Ventas 30d', 'text-center') +
    thSort('days_inventory', 'Días inv.', 'text-center') +
    thSort('rev_forecast', 'Rev. si deal', 'text-right') +
    '<th class="px-4 py-3">Razón</th><th class="px-4 py-3">Acciones</th></tr></thead>' +
    '<tbody id="amz-candidates-tbody"></tbody></table></div>' +
    '<div id="amz-candidates-pag"></div>' +
    (totalRevForecast > 0 ? '<div class="px-4 py-3 bg-green-50 border-t border-green-100 flex items-center justify-between">' +
      '<span class="text-xs text-green-700 font-medium">Revenue potencial total si ejecutas todos los deals:</span>' +
      '<span class="text-sm font-bold text-green-700">$' + fmtMoney(totalRevForecast) + '</span></div>' : '');
  window['amz-candidates-pag_go'] = function(p) { _candidatesPage = _renderPaginated(_candidatesRows, p, 'amz-candidates-tbody', 'amz-candidates-pag'); };
  _renderPaginatedCandidates();
}

async function loadDealCandidates() {
  var wrap = document.getElementById('amz-candidates-table');
  if (!wrap) return;
  wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">Analizando candidatos a deal...</div>';
  var sp = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/deal-candidates' + sp);
    var d = await r.json();
    var warn = document.getElementById('amz-no-sales-warn');
    if (warn) warn.classList.toggle('hidden', !d.no_sales_data);
    _candidatesNoSalesData = !!d.no_sales_data;
    if (!d.candidates || d.candidates.length === 0) {
      wrap.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">No hay candidatos con stock suficiente.</div>';
      return;
    }
    // Enrich candidates with rev_forecast
    _candidatesData = d.candidates.map(function(c) {
      c.rev_forecast = (c.fba_stock || 0) * (c.deal_price_suggestion || 0);
      return c;
    });
    // Default sort: highest revenue potential first
    _candidatesData.sort(function(a, b) { return b.rev_forecast - a.rev_forecast; });
    _candidatesRows = _candidatesData.map(_buildCandidatesRow);
    _candidatesPage = 1;
    _renderCandidatesTable();
  } catch(e) {
    wrap.innerHTML = '<div class="text-center py-8 text-red-400 text-sm">Error: ' + e.message + '</div>';
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// STATS ROW — carga conteos de listings para las tarjetas superiores
// ══════════════════════════════════════════════════════════════════════════════
var _amzStatsCounts = { active: null, inactive: null, suppressed: null };

window.amzUpdateStatsFromCatalog = function(filter, total) {
  if (filter === 'all') return; // 'all' no actualiza conteos individuales
  _amzStatsCounts[filter] = total;
  _renderAmzStatsRow();
};

async function loadAmzStatsRow() {
  // Carga conteos de todos los estados en paralelo
  var sp = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId : '';
  try {
    var r = await fetch('/api/amazon/alerts' + sp);
    var d = await r.json();
    if (d && !d.error) {
      _amzStatsCounts.suppressed = (d.suppressed || []).length;
      _amzStatsCounts.noStock    = (d.no_stock_active || []).length;
      _amzStatsCounts.lowStock   = (d.low_stock || []).length;
    }
  } catch(e) { /* silent */ }

  // Cargar conteo total de activos desde catálogo
  var spAmp = window.amzActiveSellerId ? '&seller_id=' + window.amzActiveSellerId : '';
  ['active','inactive'].forEach(async function(status) {
    try {
      var r = await fetch('/api/amazon/products/catalog?status_filter=' + status + spAmp);
      var html = await r.text();
      var match = html.match(/data-total="(\d+)"|(\d+)\s+listings/);
      // Usar el contador que inyecta el template en el script inline
    } catch(e) {}
  });

  _renderAmzStatsRow();
}

function _renderAmzStatsRow() {
  var row = document.getElementById('amz-stats-row');
  if (!row) return;

  var cards = [
    {
      label: 'Activos',
      value: _amzStatsCounts.active != null ? _amzStatsCounts.active : '—',
      icon: '<svg class="w-5 h-5 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>',
      bg: 'bg-green-100', filter: 'active',
      hover: 'hover:border-green-300'
    },
    {
      label: 'Inactivos',
      value: _amzStatsCounts.inactive != null ? _amzStatsCounts.inactive : '—',
      icon: '<svg class="w-5 h-5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
      bg: 'bg-gray-100', filter: 'inactive',
      hover: 'hover:border-orange-300'
    },
    {
      label: 'Suprimidos',
      value: _amzStatsCounts.suppressed != null ? _amzStatsCounts.suppressed : '—',
      icon: '<svg class="w-5 h-5 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>',
      bg: 'bg-red-100', filter: 'suppressed',
      hover: 'hover:border-red-300'
    },
    {
      label: 'Sin Stock / Low',
      value: (_amzStatsCounts.noStock != null && _amzStatsCounts.lowStock != null)
             ? (_amzStatsCounts.noStock + _amzStatsCounts.lowStock) : '—',
      icon: '<svg class="w-5 h-5 text-yellow-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>',
      bg: 'bg-yellow-100', filter: 'operaciones',
      hover: 'hover:border-yellow-300'
    }
  ];

  row.innerHTML = cards.map(function(c) {
    var action = c.filter === 'operaciones'
      ? 'switchAmzTab(\'operaciones\')'
      : 'switchAmzTab(\'operaciones\'); setAmzOpsFilter(\'' + c.filter + '\')';
    return '<div onclick="' + action + '" class="kpi-card bg-white rounded-xl border p-4 flex items-center gap-3 cursor-pointer ' + c.hover + ' transition">' +
      '<div class="w-10 h-10 rounded-xl ' + c.bg + ' flex items-center justify-center shrink-0">' + c.icon + '</div>' +
      '<div><p class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-0.5">' + c.label + '</p>' +
      '<p class="font-black text-2xl text-gray-800 tabular-nums leading-none">' + c.value + '</p></div>' +
      '</div>';
  }).join('');
}

// ══════════════════════════════════════════════════════════════════════════════
// OPERACIONES — tab filter (reemplaza el <select>)
// ══════════════════════════════════════════════════════════════════════════════
function setAmzOpsFilter(filter) {
  amzOpsCurrentFilter = filter;
  // Actualizar estado visual de los tabs
  document.querySelectorAll('.amz-ops-tab-btn').forEach(function(btn) {
    var active = btn.getAttribute('data-filter') === filter;
    btn.classList.toggle('border-orange-500', active);
    btn.classList.toggle('text-orange-700', active);
    btn.classList.toggle('bg-orange-50', active);
    btn.classList.toggle('border-transparent', !active);
    btn.classList.toggle('text-gray-500', !active);
    btn.classList.toggle('bg-transparent', !active);
  });
  _opsLoaded = false;
  loadOpsListing();
}

function onAmzOpsSearch() {
  var inp = document.getElementById('amz-ops-search');
  var clearBtn = document.getElementById('amz-ops-search-clear');
  if (clearBtn) clearBtn.classList.toggle('hidden', !(inp && inp.value));
  // Filtro en cliente sobre las filas ya cargadas
  var query = inp ? inp.value.toLowerCase() : '';
  document.querySelectorAll('#amz-ops-catalog-wrap tbody tr').forEach(function(row) {
    var text = (row.dataset.sku + ' ' + row.dataset.title + ' ' + row.dataset.asin).toLowerCase();
    row.style.display = (!query || text.indexOf(query) !== -1) ? '' : 'none';
  });
}

function clearAmzOpsSearch() {
  var inp = document.getElementById('amz-ops-search');
  if (inp) { inp.value = ''; onAmzOpsSearch(); }
}

// ══════════════════════════════════════════════════════════════════════════════
// PANEL LATERAL — abre/cierra y gestiona subtabs
// ══════════════════════════════════════════════════════════════════════════════
var _amzPanelCurrentTab = 'info';
var _amzPanelData = null;

function openAmzPanel(dataset) {
  _amzPanelData = {
    sku:           dataset.sku   || '',
    title:         dataset.title || '',
    asin:          dataset.asin  || '',
    status:        dataset.status || '',
    url:           dataset.url   || '',
    price:         parseFloat(dataset.price) || 0,
    fba:           parseInt(dataset.fba, 10) || 0,
    reserved:      parseInt(dataset.reserved, 10) || 0,
    inbound:       parseInt(dataset.inbound, 10) || 0,
    unfulfillable: parseInt(dataset.unfulfillable, 10) || 0,
    image:         dataset.image || '',
    suggestion:    dataset.suggestion || ''
  };

  // Actualizar header del panel
  var asinEl   = document.getElementById('amz-panel-asin');
  var titleEl  = document.getElementById('amz-panel-title');
  var badgeEl  = document.getElementById('amz-panel-status-badge');
  var linkEl   = document.getElementById('amz-panel-permalink');
  if (asinEl)  asinEl.textContent  = _amzPanelData.asin ? 'ASIN: ' + _amzPanelData.asin : 'SKU: ' + _amzPanelData.sku;
  if (titleEl) titleEl.textContent = _amzPanelData.title;
  if (badgeEl) {
    var s = _amzPanelData.status;
    badgeEl.textContent = s === 'ACTIVE' ? 'Activo' : (s === 'DISCOVERABLE' ? 'Visible' : 'Inactivo');
    badgeEl.className = 'text-xs px-2 py-0.5 rounded-full font-medium ' +
      (s === 'ACTIVE' ? 'bg-green-100 text-green-800' : (s === 'DISCOVERABLE' ? 'bg-yellow-100 text-yellow-800' : 'bg-red-100 text-red-700'));
  }
  if (linkEl) {
    linkEl.href = _amzPanelData.url || '#';
    linkEl.style.display = _amzPanelData.url ? '' : 'none';
  }

  // Mostrar tab info por defecto
  setAmzPanelTab('info');

  // Abrir panel
  var overlay = document.getElementById('amz-panel-overlay');
  var panel   = document.getElementById('amz-side-panel');
  if (overlay) { overlay.classList.remove('hidden'); }
  if (panel) {
    panel.classList.remove('hidden');
    setTimeout(function() { panel.style.transform = 'translateX(0)'; }, 10);
  }
}

function closeAmzPanel() {
  var overlay = document.getElementById('amz-panel-overlay');
  var panel   = document.getElementById('amz-side-panel');
  if (panel) {
    panel.style.transform = 'translateX(100%)';
    setTimeout(function() {
      panel.classList.add('hidden');
      if (overlay) overlay.classList.add('hidden');
    }, 300);
  }
}

function setAmzPanelTab(tab) {
  _amzPanelCurrentTab = tab;
  document.querySelectorAll('.amz-panel-tab').forEach(function(btn) {
    var active = btn.getAttribute('data-ptab') === tab;
    btn.classList.toggle('border-orange-500', active);
    btn.classList.toggle('text-orange-700', active);
    btn.classList.toggle('active-amz-panel-tab', active);
    btn.classList.toggle('border-transparent', !active);
    btn.classList.toggle('text-gray-500', !active);
  });
  _renderAmzPanelContent(tab);
}

function _renderAmzPanelContent(tab) {
  var cont = document.getElementById('amz-panel-content');
  if (!cont || !_amzPanelData) return;
  var d = _amzPanelData;

  if (tab === 'info') {
    cont.innerHTML =
      '<div class="p-5 space-y-4">' +
        (d.image ? '<img src="' + d.image + '" alt="" class="w-32 h-32 object-contain mx-auto rounded-xl border border-gray-100 bg-gray-50">' : '') +
        '<div class="space-y-2 text-sm">' +
          '<div class="flex justify-between py-2 border-b border-gray-50"><span class="text-gray-500">SKU</span><span class="font-mono font-medium text-gray-800">' + d.sku + '</span></div>' +
          (d.asin ? '<div class="flex justify-between py-2 border-b border-gray-50"><span class="text-gray-500">ASIN</span><span class="font-mono font-medium text-orange-600">' + d.asin + '</span></div>' : '') +
          '<div class="flex justify-between py-2 border-b border-gray-50"><span class="text-gray-500">Precio</span><span class="font-bold text-gray-800">' + (d.price > 0 ? '$' + d.price.toLocaleString('es-MX', {minimumFractionDigits:2}) : '—') + '</span></div>' +
          '<div class="flex justify-between py-2 border-b border-gray-50"><span class="text-gray-500">Estado</span><span>' + (d.status === 'ACTIVE' ? '<span class="bg-green-100 text-green-800 text-xs px-2 py-0.5 rounded-full font-medium">Activo</span>' : '<span class="bg-red-100 text-red-700 text-xs px-2 py-0.5 rounded-full font-medium">Inactivo</span>') + '</span></div>' +
          (d.suggestion ? '<div class="py-2 border-b border-gray-50"><span class="text-gray-500 block mb-1">Sugerencia</span><p class="text-xs text-yellow-700 bg-yellow-50 rounded-lg px-3 py-2">' + d.suggestion + '</p></div>' : '') +
        '</div>' +
        (d.url ? '<a href="' + d.url + '" target="_blank" class="flex items-center justify-center gap-2 w-full border border-orange-200 text-orange-600 hover:bg-orange-50 rounded-lg py-2 text-sm font-medium transition mt-2">Ver en Amazon →</a>' : '') +
      '</div>';

  } else if (tab === 'stock') {
    var fbaColor = d.fba >= 10 ? 'text-green-600' : (d.fba > 0 ? 'text-yellow-600' : 'text-red-600');
    cont.innerHTML =
      '<div class="p-5 space-y-3">' +
        '<h3 class="font-semibold text-gray-700 text-sm">📦 Inventario FBA</h3>' +
        '<div class="bg-white rounded-xl border divide-y divide-gray-50 text-sm">' +
          '<div class="flex justify-between px-4 py-3"><span class="text-gray-500">Disponible (vendible)</span><span class="font-bold text-lg ' + fbaColor + '">' + d.fba + '</span></div>' +
          '<div class="flex justify-between px-4 py-3"><span class="text-gray-500">Reservado (en órdenes)</span><span class="font-semibold text-blue-600">' + d.reserved + '</span></div>' +
          '<div class="flex justify-between px-4 py-3"><span class="text-gray-500">En camino (inbound)</span><span class="font-semibold text-indigo-600">' + d.inbound + '</span></div>' +
          '<div class="flex justify-between px-4 py-3"><span class="text-gray-500">No vendible (dañado)</span><span class="font-semibold ' + (d.unfulfillable > 0 ? 'text-red-600' : 'text-gray-400') + '">' + d.unfulfillable + '</span></div>' +
        '</div>' +
        '<div class="bg-orange-50 rounded-xl p-3 text-xs text-orange-700 border border-orange-100">' +
          '<p>Total en Amazon: <b>' + (d.fba + d.reserved) + ' u</b> (vendible + reservado)</p>' +
        '</div>' +
        '<a href="https://sellercentral.amazon.com.mx/inventory?search=' + encodeURIComponent(d.sku) + '" target="_blank" class="flex items-center justify-center gap-2 w-full border border-blue-200 text-blue-600 hover:bg-blue-50 rounded-lg py-2 text-sm font-medium transition">Editar en Seller Central →</a>' +
      '</div>';

  } else if (tab === 'buybox') {
    cont.innerHTML =
      '<div class="p-5">' +
        '<div class="flex items-center justify-center h-24 text-gray-400 text-sm">' +
          '<div class="text-center"><p class="text-2xl mb-2">🏆</p><p>Cargando datos de Buy Box...</p></div>' +
        '</div>' +
      '</div>';
    // Lazy load Buy Box data
    (async function() {
      var sp = window.amzActiveSellerId ? '?seller_id=' + window.amzActiveSellerId + '&asin=' + d.asin : '?asin=' + d.asin;
      try {
        var r = await fetch('/api/amazon/products/buybox' + sp);
        var bbd = await r.json();
        if (_amzPanelCurrentTab !== 'buybox') return;
        if (bbd.error) {
          cont.innerHTML = '<div class="p-5 text-center text-red-400 text-sm">' + bbd.error + '</div>';
          return;
        }
        var html = '<div class="p-5 space-y-3">' +
          '<h3 class="font-semibold text-gray-700 text-sm">🏆 Buy Box</h3>' +
          '<div class="bg-white rounded-xl border divide-y divide-gray-50 text-sm">';
        (bbd.competitors || []).forEach(function(c) {
          html += '<div class="flex justify-between px-4 py-3">' +
            '<div><p class="font-medium text-gray-800">' + (c.seller_name || c.seller_id || 'Vendedor') + '</p>' +
            '<p class="text-xs text-gray-400">' + (c.fulfillment || '') + (c.is_buybox_winner ? ' · <span class="text-green-600 font-semibold">Buy Box ✓</span>' : '') + '</p></div>' +
            '<span class="font-bold text-gray-800">$' + (c.price || 0).toLocaleString('es-MX', {minimumFractionDigits:2}) + '</span></div>';
        });
        if (!(bbd.competitors || []).length) html += '<div class="px-4 py-6 text-center text-gray-400 text-xs">Sin datos de competidores disponibles</div>';
        html += '</div></div>';
        cont.innerHTML = html;
      } catch(e) {
        if (_amzPanelCurrentTab === 'buybox')
          cont.innerHTML = '<div class="p-5 text-center text-gray-400 text-sm">Datos no disponibles para este ASIN</div>';
      }
    })();

  } else if (tab === 'atributos') {
    cont.innerHTML =
      '<div class="p-5 space-y-3">' +
        '<h3 class="font-semibold text-gray-700 text-sm">🏷️ Atributos</h3>' +
        '<div class="bg-white rounded-xl border divide-y divide-gray-50 text-sm">' +
          '<div class="flex justify-between px-4 py-3"><span class="text-gray-500">SKU</span><span class="font-mono text-gray-800">' + d.sku + '</span></div>' +
          (d.asin ? '<div class="flex justify-between px-4 py-3"><span class="text-gray-500">ASIN</span><span class="font-mono text-orange-600">' + d.asin + '</span></div>' : '') +
          '<div class="flex justify-between px-4 py-3"><span class="text-gray-500">Estado</span><span>' + d.status + '</span></div>' +
          '<div class="flex justify-between px-4 py-3"><span class="text-gray-500">Marketplace</span><span>Amazon MX</span></div>' +
        '</div>' +
        '<p class="text-xs text-gray-400 text-center mt-4">Datos completos en Seller Central</p>' +
        '<a href="https://sellercentral.amazon.com.mx/inventory?search=' + encodeURIComponent(d.sku) + '" target="_blank" class="flex items-center justify-center gap-2 w-full border border-blue-200 text-blue-600 hover:bg-blue-50 rounded-lg py-2 text-sm font-medium transition">Ver en Seller Central →</a>' +
      '</div>';

  } else if (tab === 'imagenes') {
    cont.innerHTML =
      '<div class="p-5 space-y-4">' +
        '<h3 class="font-semibold text-gray-700 text-sm">🖼 Imágenes</h3>' +
        (d.image
          ? '<div class="grid grid-cols-2 gap-3">' +
              '<img src="' + d.image + '" alt="" class="w-full aspect-square object-contain rounded-xl border border-gray-100 bg-gray-50">' +
            '</div>'
          : '<div class="text-center py-8 text-gray-400 text-sm"><p class="text-3xl mb-2">🖼</p><p>Sin imagen disponible</p></div>') +
        '<a href="https://sellercentral.amazon.com.mx/inventory?search=' + encodeURIComponent(d.sku) + '" target="_blank" class="flex items-center justify-center gap-2 w-full border border-orange-200 text-orange-600 hover:bg-orange-50 rounded-lg py-2 text-sm font-medium transition mt-2">Gestionar imágenes en SC →</a>' +
      '</div>';
  }
}

// ─── ASIN Search ──────────────────────────────────────────────────────────────
window.searchAsin = async function() {
    var asin = (document.getElementById('amz-asin-input').value || '').trim().toUpperCase();
    var days = document.getElementById('amz-asin-days').value || 30;
    var resultEl = document.getElementById('amz-asin-result');
    if (!asin) { resultEl.innerHTML = '<p class="text-xs text-red-500">Ingresa un ASIN.</p>'; return; }
    if (asin.length !== 10) { resultEl.innerHTML = '<p class="text-xs text-red-500">El ASIN debe tener exactamente 10 caracteres (tienes ' + asin.length + ').</p>'; return; }

    resultEl.innerHTML = '<div class="flex items-center gap-2 py-4 text-sm text-gray-400">' +
        '<svg class="animate-spin h-4 w-4 text-orange-400" viewBox="0 0 24 24" fill="none"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"/></svg>' +
        'Consultando ASIN ' + asin + '...</div>';

    var sellerParam = window.amzActiveSellerId ? '&seller_id=' + encodeURIComponent(window.amzActiveSellerId) : '';
    try {
        var r = await fetch('/api/amazon/asin-search?asin=' + asin + '&days=' + days + sellerParam);
        var data = await r.json();
        if (data.error) {
            resultEl.innerHTML = '<div class="p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">' + data.error + '</div>';
            return;
        }
        resultEl.innerHTML = _renderAsinResult(data);
    } catch(e) {
        resultEl.innerHTML = '<p class="text-xs text-red-500">Error: ' + e.message + '</p>';
    }
};

function _bsrTier(rank) {
    if (!rank) return { label: 'Sin rank', color: 'gray', est: '' };
    if (rank <= 100)   return { label: 'Muy alta',  color: 'green',  est: 'est. 500+ uds/mes' };
    if (rank <= 500)   return { label: 'Alta',       color: 'green',  est: 'est. 100–500 uds/mes' };
    if (rank <= 2000)  return { label: 'Media',      color: 'yellow', est: 'est. 20–100 uds/mes' };
    if (rank <= 10000) return { label: 'Moderada',   color: 'yellow', est: 'est. 5–20 uds/mes' };
    return                    { label: 'Baja',       color: 'red',    est: 'est. <5 uds/mes' };
}

function _tierCls(color) {
    if (color === 'green')  return 'bg-green-100 text-green-700 border-green-200';
    if (color === 'yellow') return 'bg-yellow-100 text-yellow-700 border-yellow-200';
    if (color === 'red')    return 'bg-red-100 text-red-700 border-red-200';
    return 'bg-gray-100 text-gray-500 border-gray-200';
}

function _fbaReferralFee(category, itemClass) {
    var s = ((category || '') + ' ' + (itemClass || '')).toLowerCase();
    if (/personal.?computer|laptop|desktop/.test(s) && !/accessor/.test(s)) return 8;
    if (/camera|photo/.test(s)) return 8;
    if (/cell.?phone/.test(s) && !/accessor/.test(s)) return 8;
    if (/consumer.?electronics/.test(s)) return 8;
    if (/automotive/.test(s)) return 12;
    if (/jewelry|jewellery/.test(s)) return 20;
    if (/watch/.test(s)) return 16;
    if (/amazon.?device/.test(s)) return 45;
    return 15;
}

function _renderAsinResult(d) {
    var p       = d.product || {};
    var t       = d.totals  || {};
    var daily   = d.daily   || [];
    var listing = d.listing || {};
    var offers  = d.offers  || {};
    var mkt     = d.marketplace || '';
    var hasSales    = t.units > 0 || t.orders > 0;
    var currency    = t.currency || 'USD';
    var isMX        = mkt === 'MX';
    var scBase      = mkt === 'US' ? 'https://sellercentral.amazon.com' : 'https://sellercentral.amazon.com.mx';
    var amzDomain   = mkt === 'US' ? 'amazon.com' : 'amazon.com.mx';

    // BSR — tomar el mejor rank de classificationRanks (no display group)
    var bsrList   = (p.bsr || []).filter(function(b) { return !b.is_display; });
    var bestBsr   = bsrList.length ? bsrList.reduce(function(a, b) { return a.rank < b.rank ? a : b; }) : null;
    var tier      = _bsrTier(bestBsr ? bestBsr.rank : null);

    // Offers
    var bbPrice     = offers.buy_box_price || null;
    var bbCur       = offers.buy_box_currency || currency;
    var numSellers  = offers.total_offers || 0;
    var sellers     = offers.sellers || [];
    var bbSeller    = sellers.find(function(s) { return s.is_buy_box; }) || null;
    var listPriceO  = offers.list_price || p.list_price || null;
    var listPriceCur = offers.list_price_currency || p.list_price_currency || 'USD';
    var discount    = (bbPrice && listPriceO && listPriceO > bbPrice)
        ? Math.round((1 - bbPrice / listPriceO) * 100) : 0;

    // Tu posición
    var inCatalog   = !!listing.sku;
    var myPrice     = listing.price ? Number(listing.price) : null;
    var myStatus    = listing.status || '';
    var myVsBb      = (myPrice && bbPrice) ? (myPrice <= bbPrice ? 'competitivo' : 'alto') : null;

    var html = '<div class="border border-gray-200 rounded-xl overflow-hidden text-sm">';

    // ── 1. Header ────────────────────────────────────────────────────────
    html += '<div class="flex gap-4 p-4 bg-orange-50 border-b border-orange-100">';
    if (p.image_url)
        html += '<img src="' + p.image_url + '" alt="" class="w-20 h-20 object-contain rounded-lg bg-white border border-gray-200 flex-shrink-0">';
    else
        html += '<div class="w-20 h-20 bg-gray-100 rounded-lg flex items-center justify-center text-gray-300 flex-shrink-0 text-3xl">📦</div>';

    html += '<div class="flex-1 min-w-0">';
    html += '<div class="flex flex-wrap gap-1.5 items-center mb-1">';
    html += '<span class="font-mono text-xs font-bold bg-orange-600 text-white px-2 py-0.5 rounded">' + d.asin + '</span>';
    html += '<span class="text-[10px] px-2 py-0.5 rounded-full border font-bold ' + (mkt === 'US' ? 'bg-blue-50 border-blue-200 text-blue-700' : 'bg-green-50 border-green-200 text-green-700') + '">' + (mkt || '?') + '</span>';
    if (inCatalog) html += '<span class="text-[10px] px-2 py-0.5 rounded-full bg-green-100 text-green-700 border border-green-200 font-semibold">✓ En tu catálogo</span>';
    html += '</div>';
    if (p.found) {
        html += '<p class="font-bold text-gray-800 leading-snug mb-1 text-xs">' + (p.title || '—') + '</p>';
        html += '<div class="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-gray-500">';
        if (p.brand)        html += '<span><span class="text-gray-400">Marca</span> ' + p.brand + '</span>';
        if (p.model_number) html += '<span><span class="text-gray-400">Modelo</span> ' + p.model_number + '</span>';
        if (listPriceO)     html += '<span><span class="text-gray-400">P. lista</span> $' + Number(listPriceO).toFixed(2) + ' ' + listPriceCur + '</span>';
        html += '</div>';
    } else {
        html += '<p class="text-xs text-gray-400">ASIN no encontrado en el catálogo de este marketplace.</p>';
    }
    html += '</div></div>';

    // ── 2. BSR strip ─────────────────────────────────────────────────────
    if (bsrList.length) {
        html += '<div class="flex flex-wrap items-center gap-2 px-4 py-2.5 bg-white border-b border-gray-100 text-xs">';
        html += '<span class="text-gray-400 font-semibold uppercase tracking-wide text-[10px]">BSR</span>';
        bsrList.slice(0, 3).forEach(function(b) {
            html += '<span class="px-2 py-0.5 rounded-full border ' + _tierCls(tier.color) + '">#' + b.rank.toLocaleString() + ' ' + b.category + '</span>';
        });
        html += '<span class="ml-auto px-2 py-0.5 rounded-full border font-semibold ' + _tierCls(tier.color) + '">' + tier.label + '</span>';
        if (tier.est) html += '<span class="text-gray-400 text-[10px]">' + tier.est + '</span>';
        html += '</div>';
    }

    // ── 3. KPI strip ─────────────────────────────────────────────────────
    html += '<div class="grid grid-cols-2 sm:grid-cols-4 border-b border-gray-100">';
    var kc = 'px-4 py-3 text-center border-r border-gray-100 last:border-r-0';

    // Buy Box
    html += '<div class="' + kc + '">';
    html += '<p class="text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">Buy Box</p>';
    if (bbPrice) {
        html += '<p class="text-lg font-black text-orange-600">$' + bbPrice.toFixed(2) + '</p>';
        html += '<p class="text-[10px] text-gray-400">' + bbCur + (discount ? ' · −' + discount + '%' : '') + '</p>';
    } else {
        html += '<p class="text-lg font-black text-gray-300">—</p>';
        html += '<p class="text-[10px] text-gray-400">sin buy box</p>';
    }
    html += '</div>';

    // Vendedores
    html += '<div class="' + kc + '">';
    html += '<p class="text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">Vendedores</p>';
    var compColor = numSellers === 0 ? 'text-gray-300' : numSellers <= 2 ? 'text-green-600' : numSellers <= 5 ? 'text-yellow-600' : 'text-red-500';
    html += '<p class="text-lg font-black ' + compColor + '">' + numSellers + '</p>';
    var compLabel = numSellers === 0 ? 'sin oferta' : numSellers === 1 ? 'poco disputado' : numSellers <= 3 ? 'comp. baja' : numSellers <= 7 ? 'comp. media' : 'muy competido';
    html += '<p class="text-[10px] text-gray-400">' + compLabel + '</p>';
    html += '</div>';

    // Tus unidades (propias)
    html += '<div class="' + kc + '">';
    html += '<p class="text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">Tus uds.</p>';
    html += '<p class="text-lg font-black ' + (t.units > 0 ? 'text-green-600' : 'text-gray-300') + '">' + (t.units || 0) + '</p>';
    html += '<p class="text-[10px] text-gray-400">últimos ' + d.days + ' días</p>';
    html += '</div>';

    // Tu revenue
    html += '<div class="' + kc + '">';
    html += '<p class="text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">Tu revenue</p>';
    html += '<p class="text-lg font-black ' + (t.revenue > 0 ? 'text-orange-600' : 'text-gray-300') + '">$' + Number(t.revenue || 0).toLocaleString('en-US', {maximumFractionDigits: 0}) + '</p>';
    html += '<p class="text-[10px] text-gray-400">' + currency + '</p>';
    html += '</div>';

    html += '</div>'; // end KPI grid

    // ── 4. Decision cards ─────────────────────────────────────────────────
    html += '<div class="grid grid-cols-1 sm:grid-cols-3 gap-0 border-b border-gray-100">';

    // Demanda
    var demandBorderCls = tier.color === 'green' ? 'border-green-300' : tier.color === 'yellow' ? 'border-yellow-300' : 'border-gray-200';
    html += '<div class="p-4 border-r border-gray-100">';
    html += '<p class="text-[10px] text-gray-400 uppercase tracking-wide font-semibold mb-2">📊 Demanda del mercado</p>';
    if (bestBsr) {
        html += '<p class="text-base font-bold ' + (tier.color === 'green' ? 'text-green-700' : tier.color === 'yellow' ? 'text-yellow-700' : 'text-red-600') + '">' + tier.label + '</p>';
        html += '<p class="text-xs text-gray-500 mt-0.5">BSR #' + bestBsr.rank.toLocaleString() + ' en ' + bestBsr.category + '</p>';
        if (tier.est) html += '<p class="text-[11px] text-gray-400 mt-1">' + tier.est + ' <span class="text-gray-300">(aprox.)</span></p>';
    } else {
        html += '<p class="text-sm text-gray-400">Sin datos BSR</p>';
    }
    html += '</div>';

    // Competencia
    html += '<div class="p-4 border-r border-gray-100">';
    html += '<p class="text-[10px] text-gray-400 uppercase tracking-wide font-semibold mb-2">🏆 Competencia activa</p>';
    if (numSellers > 0 && bbSeller) {
        var compSignalCls = numSellers <= 2 ? 'text-green-700' : numSellers <= 5 ? 'text-yellow-700' : 'text-red-600';
        html += '<p class="text-base font-bold ' + compSignalCls + '">' + numSellers + ' ' + (numSellers === 1 ? 'vendedor' : 'vendedores') + '</p>';
        html += '<p class="text-xs text-gray-600 mt-0.5 font-semibold">Buy Box: $' + (bbPrice ? bbPrice.toFixed(2) : '—') + ' ' + bbCur + '</p>';
        html += '<div class="flex gap-1.5 flex-wrap mt-1">';
        if (bbSeller.is_fba)   html += '<span class="text-[10px] px-1.5 py-0.5 bg-blue-50 border border-blue-200 text-blue-600 rounded">FBA</span>';
        if (bbSeller.is_prime) html += '<span class="text-[10px] px-1.5 py-0.5 bg-indigo-50 border border-indigo-200 text-indigo-600 rounded">Prime</span>';
        if (bbSeller.feedback_count) html += '<span class="text-[10px] text-gray-400">' + bbSeller.feedback_count.toLocaleString() + ' reviews · ' + bbSeller.feedback_pct.toFixed(0) + '%</span>';
        html += '</div>';
    } else if (numSellers === 0) {
        html += '<p class="text-base font-bold text-gray-400">Sin oferta activa</p>';
        html += '<p class="text-xs text-gray-400 mt-0.5">Oportunidad de entrada sin competencia</p>';
    } else {
        html += '<p class="text-base font-bold text-gray-600">' + numSellers + ' vendedores</p>';
    }
    html += '</div>';

    // Tu posición
    html += '<div class="p-4">';
    html += '<p class="text-[10px] text-gray-400 uppercase tracking-wide font-semibold mb-2">🏬 Tu posición</p>';
    if (inCatalog) {
        var posCls = myVsBb === 'competitivo' ? 'text-green-700' : myVsBb === 'alto' ? 'text-yellow-700' : 'text-blue-700';
        html += '<p class="text-base font-bold ' + posCls + '">Publicado' + (myStatus ? ' · ' + myStatus : '') + '</p>';
        if (myPrice) {
            html += '<p class="text-xs text-gray-600 mt-0.5">Tu precio: $' + myPrice.toFixed(2) + ' ' + (isMX ? 'MXN' : 'USD') + '</p>';
            if (bbPrice && myVsBb) {
                var diffAmt = Math.abs(myPrice - bbPrice).toFixed(2);
                html += '<p class="text-[11px] mt-1 ' + (myVsBb === 'competitivo' ? 'text-green-600' : 'text-yellow-600') + '">';
                html += myVsBb === 'competitivo' ? '✓ Competitivo vs buy box' : '↑ $' + diffAmt + ' sobre buy box';
                html += '</p>';
            }
        }
        if (listing.sku) html += '<p class="text-[10px] text-gray-400 mt-1">SKU: ' + listing.sku + '</p>';
    } else {
        html += '<p class="text-base font-bold text-gray-400">Sin listing activo</p>';
        html += '<p class="text-xs text-gray-400 mt-0.5">No está en tu catálogo</p>';
        if (bbPrice) {
            html += '<p class="text-[11px] text-blue-600 mt-1">Buy box disponible a $' + bbPrice.toFixed(2) + ' ' + bbCur + '</p>';
        }
    }
    html += '</div>';

    html += '</div>'; // end decision cards

    // ── 5. Sellers table ─────────────────────────────────────────────────
    if (sellers.length) {
        html += '<div class="border-b border-gray-100">';
        html += '<p class="px-4 pt-3 pb-1 text-[10px] text-gray-400 uppercase tracking-wide font-semibold">Vendedores activos</p>';
        html += '<div class="overflow-x-auto"><table class="w-full text-xs">';
        html += '<thead><tr class="bg-gray-50 text-gray-400 text-[10px] uppercase">';
        html += '<th class="px-4 py-2 text-left">Precio</th><th class="px-4 py-2 text-left">Envío</th><th class="px-4 py-2 text-left">Tipo</th><th class="px-4 py-2 text-left">Buy Box</th><th class="px-4 py-2 text-left">Prime</th><th class="px-4 py-2 text-left">Reviews</th>';
        html += '</tr></thead><tbody>';
        sellers.forEach(function(s, i) {
            var bg = i % 2 === 0 ? 'bg-white' : 'bg-gray-50';
            html += '<tr class="' + bg + '">';
            html += '<td class="px-4 py-2 font-bold text-gray-800">$' + s.price.toFixed(2) + ' <span class="font-normal text-gray-400">' + s.currency + '</span></td>';
            html += '<td class="px-4 py-2 text-gray-500">' + (s.shipping > 0 ? '$' + s.shipping.toFixed(2) : 'Gratis') + '</td>';
            html += '<td class="px-4 py-2"><span class="px-1.5 py-0.5 rounded text-[10px] ' + (s.is_fba ? 'bg-blue-50 text-blue-700' : 'bg-gray-100 text-gray-500') + '">' + (s.is_fba ? 'FBA' : 'FBM') + '</span></td>';
            html += '<td class="px-4 py-2">' + (s.is_buy_box ? '<span class="text-orange-600 font-bold">✓</span>' : '<span class="text-gray-300">—</span>') + '</td>';
            html += '<td class="px-4 py-2">' + (s.is_prime ? '<span class="text-indigo-600">✓</span>' : '<span class="text-gray-300">—</span>') + '</td>';
            html += '<td class="px-4 py-2 text-gray-500">' + (s.feedback_count ? s.feedback_count.toLocaleString() + ' (' + s.feedback_pct.toFixed(0) + '%)' : '—') + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table></div></div>';
    }

    // ── 6. Tus ventas (propias) ───────────────────────────────────────────
    html += '<div class="border-b border-gray-100">';
    html += '<p class="px-4 pt-3 pb-1 text-[10px] text-gray-400 uppercase tracking-wide font-semibold">Tus ventas — últimos ' + d.days + ' días</p>';
    if (!hasSales) {
        html += '<p class="px-4 pb-4 text-xs text-gray-400">Sin ventas propias registradas en este período.</p>';
    } else {
        html += '<div class="overflow-x-auto"><table class="w-full text-xs">';
        html += '<thead><tr class="bg-gray-50 text-gray-400 text-[10px] uppercase">';
        html += '<th class="px-4 py-2 text-left">Fecha</th><th class="px-4 py-2 text-right">Uds.</th><th class="px-4 py-2 text-right">Órdenes</th><th class="px-4 py-2 text-right">Revenue (' + currency + ')</th><th class="px-4 py-2 text-right">Prom.</th>';
        html += '</tr></thead><tbody>';
        var maxU = Math.max.apply(null, daily.map(function(r) { return r.units; }));
        daily.slice().reverse().forEach(function(row, i) {
            var bw = maxU > 0 ? Math.round(row.units / maxU * 60) : 0;
            var bg2 = i % 2 === 0 ? 'bg-white' : 'bg-gray-50';
            html += '<tr class="' + bg2 + ' hover:bg-orange-50/40 transition">';
            html += '<td class="px-4 py-1.5 font-mono text-gray-500">' + row.date + '</td>';
            html += '<td class="px-4 py-1.5 text-right"><div class="flex items-center justify-end gap-1.5"><div class="h-1.5 bg-orange-200 rounded-full" style="width:' + bw + 'px"></div><span class="font-bold text-gray-800">' + row.units + '</span></div></td>';
            html += '<td class="px-4 py-1.5 text-right text-gray-500">' + row.orders + '</td>';
            html += '<td class="px-4 py-1.5 text-right font-semibold text-orange-700">$' + Number(row.revenue).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) + '</td>';
            html += '<td class="px-4 py-1.5 text-right text-gray-400">$' + Number(row.avg_price).toFixed(2) + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
    }
    html += '</div>';

    // ── 6.5. Calculadora de Precio de Compra + Veredicto ─────────────────────
    var simId = 'fba_' + d.asin;
    var refRate = _fbaReferralFee(bestBsr ? bestBsr.category : '', p.item_class || '');
    var bbPriceForSim = bbPrice || 0;
    var bbReviews = bbSeller ? (bbSeller.feedback_count || 0) : 0;
    var brandOwnerLikely = !!(bbSeller && bbReviews > 5000);

    // ─── Veredicto: 3 dimensiones calculadas una vez con datos del ASIN ──────
    // 1. Buy Box
    var bbSignal, bbMsg;
    if (numSellers === 0) {
        bbSignal = 'green';  bbMsg = 'Sin competencia activa — Buy Box garantizado al entrar';
    } else if (!brandOwnerLikely && numSellers <= 3) {
        bbSignal = 'green';  bbMsg = numSellers + ' vendedores independientes — rotación de Buy Box probable';
    } else if (!brandOwnerLikely && numSellers <= 6) {
        bbSignal = 'yellow'; bbMsg = numSellers + ' vendedores sin brand owner detectado — competencia media';
    } else if (brandOwnerLikely && numSellers <= 3) {
        bbSignal = 'yellow'; bbMsg = 'Posible brand owner (' + bbReviews.toLocaleString() + ' reviews) con pocos sellers';
    } else {
        bbSignal = 'red';    bbMsg = 'Brand owner directo + ' + numSellers + ' competidores — Buy Box muy difícil';
    }

    // 2. Demanda
    var demSignal = tier.color === 'green' ? 'green' : tier.color === 'yellow' ? 'yellow' : 'red';
    var demMsg    = bestBsr ? (tier.label + ' · BSR #' + bestBsr.rank.toLocaleString() + ' · ' + tier.est) : 'Sin datos de BSR disponibles';

    // 3. Margen disponible (fees ratio at buy box price con defaults)
    var mrgSignal, mrgMsg;
    if (bbPriceForSim > 0) {
        var _defFees  = bbPriceForSim * refRate / 100 + 5.68 + 0.02;
        var _feeRatio = _defFees / bbPriceForSim;
        if (_feeRatio < 0.30) {
            mrgSignal = 'green';  mrgMsg = 'Fees ~' + Math.round(_feeRatio * 100) + '% del precio — buen margen disponible para COGS';
        } else if (_feeRatio < 0.45) {
            mrgSignal = 'yellow'; mrgMsg = 'Fees ~' + Math.round(_feeRatio * 100) + '% del precio — margen ajustado, costo de compra crítico';
        } else {
            mrgSignal = 'red';    mrgMsg = 'Fees ~' + Math.round(_feeRatio * 100) + '% del precio — poco espacio para COGS + ganancia';
        }
    } else {
        mrgSignal = 'yellow'; mrgMsg = 'Sin precio de Buy Box disponible para calcular';
    }

    // 4. Veredicto general
    var vLabel, vDesc, vBg, vTxtCls;
    if (bbSignal === 'red') {
        vLabel = '❌ No recomendado'; vBg = 'bg-red-50 border-red-300'; vTxtCls = 'text-red-800';
        vDesc = 'Buy Box prácticamente inalcanzable como reseller. Considera marca propia o busca un ASIN sin brand owner directo.';
    } else if (mrgSignal === 'red') {
        vLabel = '❌ No recomendado'; vBg = 'bg-red-50 border-red-300'; vTxtCls = 'text-red-800';
        vDesc = 'Los fees consumen demasiado del precio de venta. No queda espacio suficiente para COGS + ganancia.';
    } else if (bbSignal === 'green' && demSignal === 'green' && mrgSignal === 'green') {
        vLabel = '✅ Vale la pena analizar'; vBg = 'bg-green-50 border-green-300'; vTxtCls = 'text-green-800';
        vDesc = 'Demanda alta, Buy Box alcanzable y margen disponible suficiente. Verifica el precio tope de compra con tus costos reales.';
    } else {
        vLabel = '⚠️ Condicional'; vBg = 'bg-yellow-50 border-yellow-300'; vTxtCls = 'text-yellow-800';
        vDesc = 'Factores mixtos. El precio tope de compra abajo te dirá si tus costos reales permiten un margen viable.';
    }

    // ─── HTML del bloque ─────────────────────────────────────────────────────
    html += '<div class="border-b border-gray-100">';
    html += '<button onclick="window.toggleFbaSim(\'' + simId + '\')" class="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50 transition text-left">';
    html += '<div class="flex items-center gap-2">';
    html += '<span class="text-[10px] text-gray-400 uppercase tracking-wide font-semibold">¿Vale la pena? — Calculadora de Precio de Compra</span>';
    html += '</div>';
    html += '<svg id="' + simId + '_chev" class="w-4 h-4 text-gray-400 transition-transform duration-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>';
    html += '</button>';
    html += '<div id="' + simId + '" class="hidden px-4 pb-5 space-y-3">';

    // Veredicto card (siempre visible al abrir)
    html += '<div class="rounded-xl border-2 p-4 ' + vBg + '">';
    html += '<p class="text-sm font-bold ' + vTxtCls + ' mb-1">' + vLabel + '</p>';
    html += '<p class="text-[11px] ' + vTxtCls + ' opacity-80 mb-3">' + vDesc + '</p>';
    var dimDefs = [
        { icon: '🏆', label: 'Buy Box',           sig: bbSignal,  msg: bbMsg },
        { icon: '📊', label: 'Demanda',            sig: demSignal, msg: demMsg },
        { icon: '💰', label: 'Espacio de margen',  sig: mrgSignal, msg: mrgMsg },
    ];
    dimDefs.forEach(function(dim) {
        var cls = dim.sig === 'green' ? 'text-green-700' : dim.sig === 'yellow' ? 'text-yellow-700' : 'text-red-600';
        var dot = dim.sig === 'green' ? '●' : dim.sig === 'yellow' ? '●' : '●';
        html += '<div class="flex items-start gap-1.5 mb-1">';
        html += '<span class="' + cls + ' text-[10px] mt-0.5 shrink-0">' + dot + '</span>';
        html += '<p class="text-[11px] ' + cls + '"><span class="font-semibold">' + dim.icon + ' ' + dim.label + ':</span> ' + dim.msg + '</p>';
        html += '</div>';
    });
    html += '</div>';

    // Orange: precio venta + fees
    html += '<div class="bg-orange-50 border border-orange-100 rounded-lg p-3">';
    html += '<p class="text-[10px] font-semibold text-gray-500 uppercase tracking-wide mb-2">Precio de venta y fees Amazon</p>';
    html += '<div class="grid grid-cols-2 sm:grid-cols-4 gap-2">';

    html += '<div><label class="text-[10px] text-gray-400 block mb-1">Precio de venta</label>';
    html += '<div class="flex"><span class="bg-gray-100 border border-r-0 border-gray-200 rounded-l-md px-1.5 text-[10px] text-gray-400 flex items-center">$</span>';
    html += '<input type="number" id="' + simId + '_sell" value="' + (bbPriceForSim || '') + '" placeholder="0.00" min="0" step="0.01" oninput="window.calcFbaSim(\'' + simId + '\')" class="w-full border border-gray-200 rounded-r-md px-2 py-1.5 text-xs bg-white focus:outline-none focus:ring-1 focus:ring-orange-300"></div></div>';

    html += '<div><label class="text-[10px] text-gray-400 block mb-1">Referral fee</label>';
    html += '<div class="flex"><input type="number" id="' + simId + '_ref" value="' + refRate + '" min="0" max="50" step="0.5" oninput="window.calcFbaSim(\'' + simId + '\')" class="w-full border border-gray-200 rounded-l-md px-2 py-1.5 text-xs text-right bg-white focus:outline-none focus:ring-1 focus:ring-orange-300">';
    html += '<span class="bg-gray-100 border border-l-0 border-gray-200 rounded-r-md px-1.5 text-[10px] text-gray-400 flex items-center">%</span></div></div>';

    html += '<div><label class="text-[10px] text-gray-400 block mb-1">FBA fulfillment</label>';
    html += '<div class="flex"><span class="bg-gray-100 border border-r-0 border-gray-200 rounded-l-md px-1.5 text-[10px] text-gray-400 flex items-center">$</span>';
    html += '<input type="number" id="' + simId + '_fba" value="5.68" min="0" step="0.01" oninput="window.calcFbaSim(\'' + simId + '\')" class="w-full border border-gray-200 rounded-r-md px-2 py-1.5 text-xs bg-white focus:outline-none focus:ring-1 focus:ring-orange-300"></div></div>';

    html += '<div><label class="text-[10px] text-gray-400 block mb-1">Storage / ud / mes</label>';
    html += '<div class="flex"><span class="bg-gray-100 border border-r-0 border-gray-200 rounded-l-md px-1.5 text-[10px] text-gray-400 flex items-center">$</span>';
    html += '<input type="number" id="' + simId + '_stor" value="0.02" min="0" step="0.01" oninput="window.calcFbaSim(\'' + simId + '\')" class="w-full border border-gray-200 rounded-r-md px-2 py-1.5 text-xs bg-white focus:outline-none focus:ring-1 focus:ring-orange-300"></div></div>';

    html += '</div></div>';

    // Blue: objetivo de compra
    html += '<div class="bg-blue-50 border border-blue-100 rounded-lg p-3">';
    html += '<p class="text-[10px] font-semibold text-gray-500 uppercase tracking-wide mb-2">Tu objetivo de compra</p>';
    html += '<div class="grid grid-cols-3 gap-2">';

    html += '<div><label class="text-[10px] text-gray-400 block mb-1">Margen objetivo</label>';
    html += '<div class="flex"><input type="number" id="' + simId + '_margin" value="25" min="1" max="90" step="1" oninput="window.calcFbaSim(\'' + simId + '\')" class="w-full border border-gray-200 rounded-l-md px-2 py-1.5 text-xs text-right bg-white focus:outline-none focus:ring-1 focus:ring-blue-300">';
    html += '<span class="bg-gray-100 border border-l-0 border-gray-200 rounded-r-md px-1.5 text-[10px] text-gray-400 flex items-center">%</span></div></div>';

    html += '<div><label class="text-[10px] text-gray-400 block mb-1">Aranceles de importación</label>';
    html += '<div class="flex"><input type="number" id="' + simId + '_tar" value="0" min="0" max="200" step="1" oninput="window.calcFbaSim(\'' + simId + '\')" class="w-full border border-gray-200 rounded-l-md px-2 py-1.5 text-xs text-right bg-white focus:outline-none focus:ring-1 focus:ring-blue-300">';
    html += '<span class="bg-gray-100 border border-l-0 border-gray-200 rounded-r-md px-1.5 text-[10px] text-gray-400 flex items-center">%</span></div></div>';

    html += '<div><label class="text-[10px] text-gray-400 block mb-1">Flete / ud</label>';
    html += '<div class="flex"><span class="bg-gray-100 border border-r-0 border-gray-200 rounded-l-md px-1.5 text-[10px] text-gray-400 flex items-center">$</span>';
    html += '<input type="number" id="' + simId + '_frt" value="0" min="0" step="0.01" oninput="window.calcFbaSim(\'' + simId + '\')" class="w-full border border-gray-200 rounded-r-md px-2 py-1.5 text-xs bg-white focus:outline-none focus:ring-1 focus:ring-blue-300"></div></div>';

    html += '</div></div>';

    // Resultados (actualizados por calcFbaSim)
    html += '<div id="' + simId + '_res"></div>';

    html += '</div></div>'; // end sim body + wrapper

    // ── 7. Links ─────────────────────────────────────────────────────────
    html += '<div class="px-4 py-3 bg-gray-50 flex gap-2 flex-wrap">';
    html += '<a href="https://www.' + amzDomain + '/dp/' + d.asin + '" target="_blank" class="text-xs px-3 py-1.5 bg-orange-50 border border-orange-200 text-orange-700 rounded-lg hover:bg-orange-100 transition font-medium">Ver en Amazon →</a>';
    if (inCatalog && listing.sku)
        html += '<a href="' + scBase + '/skucentral?mSku=' + listing.sku + '" target="_blank" class="text-xs px-3 py-1.5 bg-blue-50 border border-blue-200 text-blue-700 rounded-lg hover:bg-blue-100 transition font-medium">Ver en Seller Central →</a>';
    html += '</div>';

    html += '</div>';
    return html;
}

window.toggleFbaSim = function(simId) {
    var el   = document.getElementById(simId);
    var chev = document.getElementById(simId + '_chev');
    if (!el) return;
    var opening = el.classList.contains('hidden');
    el.classList.toggle('hidden', !opening);
    if (chev) chev.style.transform = opening ? 'rotate(180deg)' : '';
    if (opening) window.calcFbaSim(simId);
};

window.calcFbaSim = function(simId) {
    var g     = function(id) { var el = document.getElementById(id); return el ? (parseFloat(el.value) || 0) : 0; };
    var sell  = g(simId + '_sell');
    var ref   = g(simId + '_ref');
    var fba   = g(simId + '_fba');
    var stor  = g(simId + '_stor');
    var mgn   = g(simId + '_margin');
    var tar   = g(simId + '_tar');
    var frt   = g(simId + '_frt');
    var resEl = document.getElementById(simId + '_res');
    if (!resEl) return;

    if (!sell) {
        resEl.innerHTML = '<p class="text-[10px] text-gray-400 text-center py-3">Ingresa el precio de venta para calcular.</p>';
        return;
    }

    // Reverse calculation: given sell price + target margin → max COGS
    // margin% = (sell - fees - landedCost) / sell
    // landedMax = sell × (1 - margin%) - fees
    // cogsMax   = (landedMax - frt) / (1 + tar%)
    function calcFor(sellP, marginPct) {
        var refAmt    = sellP * (ref / 100);
        var totalFees = refAmt + fba + stor;
        var netProc   = sellP - totalFees;
        var landedMax = netProc - (sellP * marginPct / 100);
        var cogsMax   = tar > 0 ? (landedMax - frt) / (1 + tar / 100) : (landedMax - frt);
        return { refAmt: refAmt, totalFees: totalFees, netProc: netProc, landedMax: landedMax, cogsMax: cogsMax };
    }

    var main = calcFor(sell, mgn);
    var h = '';

    // ─── Resultado principal ────────────────────────────────────────────────
    var ok = main.cogsMax > 0;
    h += '<div class="rounded-xl border-2 text-center p-4 mb-3 ' + (ok ? 'border-green-300 bg-green-50' : 'border-red-300 bg-red-50') + '">';
    h += '<p class="text-[10px] uppercase tracking-wide font-semibold ' + (ok ? 'text-green-600' : 'text-red-500') + ' mb-1">';
    h += 'Precio tope de compra (ex-fábrica) para ' + mgn + '% de margen</p>';
    if (ok) {
        h += '<p class="text-4xl font-black text-green-700 my-1">$' + main.cogsMax.toFixed(2) + '</p>';
        h += '<p class="text-[11px] text-green-600 mt-1">';
        h += 'Net proceeds $' + main.netProc.toFixed(2) + ' &nbsp;·&nbsp; Fees Amazon $' + main.totalFees.toFixed(2) + ' &nbsp;·&nbsp; Landed máx $' + main.landedMax.toFixed(2);
        h += '</p>';
    } else {
        h += '<p class="text-2xl font-bold text-red-600 my-1">No viable</p>';
        h += '<p class="text-[11px] text-red-500 mt-1">Los fees ($' + main.totalFees.toFixed(2) + ') ya consumen demasiado. El margen del ' + mgn + '% no es alcanzable en este precio.</p>';
    }
    h += '</div>';

    // ─── Tabla de escenarios ────────────────────────────────────────────────
    h += '<div class="rounded-lg border border-gray-200 overflow-hidden">';
    h += '<table class="w-full text-xs">';
    h += '<thead><tr class="bg-gray-50 text-[10px] text-gray-400 uppercase tracking-wide">';
    h += '<th class="px-3 py-2 text-center">Margen objetivo</th>';
    h += '<th class="px-3 py-2 text-right">Net Proceeds</th>';
    h += '<th class="px-3 py-2 text-right">Landed máx</th>';
    h += '<th class="px-3 py-2 text-right text-gray-600 font-bold">COGS máx (ex-fábrica)</th>';
    h += '</tr></thead><tbody>';

    [15, 20, 25, 30].forEach(function(pct, i) {
        var sc      = calcFor(sell, pct);
        var isSel   = Math.round(mgn) === pct;
        var rowBg   = isSel ? 'bg-orange-50' : i % 2 === 0 ? 'bg-white' : 'bg-gray-50';
        var cogsCls = sc.cogsMax <= 0 ? 'text-red-500' : sc.cogsMax >= 15 ? 'text-green-700 font-bold' : sc.cogsMax >= 5 ? 'text-yellow-700' : 'text-orange-600';
        h += '<tr class="' + rowBg + '">';
        h += '<td class="px-3 py-2 text-center font-semibold ' + (isSel ? 'text-orange-700' : 'text-gray-600') + '">' + pct + '%' + (isSel ? ' ◀' : '') + '</td>';
        h += '<td class="px-3 py-2 text-right text-gray-500">$' + sc.netProc.toFixed(2) + '</td>';
        h += '<td class="px-3 py-2 text-right text-blue-600">' + (sc.landedMax > 0 ? '$' + sc.landedMax.toFixed(2) : '<span class="text-red-400">—</span>') + '</td>';
        h += '<td class="px-3 py-2 text-right ' + cogsCls + '">' + (sc.cogsMax > 0 ? '$' + sc.cogsMax.toFixed(2) : 'No viable') + '</td>';
        h += '</tr>';
    });

    h += '</tbody></table>';
    h += '<div class="px-3 py-2 bg-gray-50 border-t border-gray-100 flex gap-3 flex-wrap text-[10px] text-gray-400">';
    h += '<span>Venta $' + sell.toFixed(2) + '</span>';
    h += '<span>Ref. ' + ref + '% ($' + main.refAmt.toFixed(2) + ')</span>';
    h += '<span>FBA $' + fba.toFixed(2) + '</span>';
    if (tar) h += '<span>Aranceles ' + tar + '%</span>';
    if (frt) h += '<span>Flete $' + frt.toFixed(2) + '/ud</span>';
    h += '</div></div>';

    resEl.innerHTML = h;
};

// ─────────────────────────────────────────────────────────────────────────────
// Mensajes de Compradores (Buyer-Seller Messaging) — vive en Salud, igual que
// Mensajes en ML (health_messages.html) en vez de en Retornos. Movido aquí
// desde amazon_returns.html a petición de Jovan.
// ─────────────────────────────────────────────────────────────────────────────

var amzMsgsOnlyPending = true;
var amzMsgsOrderSearch = '';

function _amzMsgsEscHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _amzMsgsEscAttr(s) {
    return _amzMsgsEscHtml(s).replace(/"/g, '&quot;');
}

function _amzMsgsSanitizeId(s) {
    return (s || '').replace(/[^a-zA-Z0-9]/g, '_');
}

function _amzMsgsFmtDate(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    return d.toLocaleDateString('es-MX', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function loadAmzBuyerMessages() {
    var el = document.getElementById('amz-msgs-content');
    if (!el) return;
    var qs = 'seller_id=' + encodeURIComponent(window.amzActiveSellerId || '') + '&days=365' +
        '&only_pending=' + (amzMsgsOnlyPending ? 'true' : 'false');
    if (amzMsgsOrderSearch) qs += '&order_id=' + encodeURIComponent(amzMsgsOrderSearch);
    fetch('/api/amazon/buyer-messages?' + qs)
        .then(function(r) { return r.json(); })
        .then(function(data) { _renderAmzBuyerMessages(data); })
        .catch(function(e) { el.innerHTML = '<p class="text-center text-red-500 py-6 text-sm">Error: ' + e.message + '</p>'; });
}

window.toggleAmzMsgsPending = function() {
    amzMsgsOnlyPending = !amzMsgsOnlyPending;
    document.getElementById('amz-msgs-toggle').textContent = amzMsgsOnlyPending ? 'Mostrando: solo pendientes' : 'Mostrando: todos';
    loadAmzBuyerMessages();
};

window.searchAmzMsgsByOrder = function() {
    var val = (document.getElementById('amz-msgs-order-search').value || '').trim();
    if (!val) return;
    amzMsgsOrderSearch = val;
    document.getElementById('amz-msgs-clear-search').classList.remove('hidden');
    loadAmzBuyerMessages();
};

window.clearAmzMsgsSearch = function() {
    amzMsgsOrderSearch = '';
    document.getElementById('amz-msgs-order-search').value = '';
    document.getElementById('amz-msgs-clear-search').classList.add('hidden');
    loadAmzBuyerMessages();
};

function _renderAmzBuyerMessages(data) {
    var el = document.getElementById('amz-msgs-content');
    var badge = document.getElementById('amz-msgs-unread');
    var threads = data.threads || [];
    if (data.error) { el.innerHTML = '<p class="text-center text-red-500 py-6 text-sm">' + data.error + '</p>'; if (badge) badge.classList.add('hidden'); return; }
    if (badge) {
        if (data.unread > 0) { badge.textContent = data.unread + ' sin leer'; badge.classList.remove('hidden'); } else { badge.classList.add('hidden'); }
    }
    if (!threads.length) {
        var emptyMsg = amzMsgsOrderSearch
            ? 'Sin mensajes para la orden ' + _amzMsgsEscHtml(amzMsgsOrderSearch) + '.'
            : (amzMsgsOnlyPending ? 'No hay mensajes pendientes de responder — todo al día. (Dale a "Mostrando: solo pendientes" para ver el historial completo)' : 'Sin mensajes de compradores en este periodo.');
        el.innerHTML = '<p class="text-center text-gray-400 py-8 text-sm">' + emptyMsg + '</p>';
        return;
    }

    function statusBadge(th, domId) {
        var vi = th.view_info;
        if (!vi) {
            return '<span id="amz-msg-badge-' + domId + '" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-gray-100 text-gray-400">Sin abrir</span>';
        }
        if (vi.status === 'resolved') {
            return '<span id="amz-msg-badge-' + domId + '" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-green-100 text-green-700">&#10003; Resuelto</span>';
        }
        if (vi.status === 'in_progress') {
            return '<span id="amz-msg-badge-' + domId + '" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-violet-100 text-violet-700">&#9679; Atendiendo: ' + _amzMsgsEscHtml(vi.viewed_by) + '</span>';
        }
        return '<span id="amz-msg-badge-' + domId + '" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-blue-100 text-blue-600">Abrió: ' + _amzMsgsEscHtml(vi.viewed_by) + '</span>';
    }

    function actionButtons(th, domId) {
        var vi = th.view_info;
        var status = vi ? vi.status : 'none';
        var takeBtn = (!vi || status === 'pending')
            ? '<button onclick="takeAmzThread(\'' + domId + '\', this)" class="text-xs px-3 py-1 rounded-full bg-violet-100 text-violet-700 hover:bg-violet-200 font-semibold transition">Tomar</button>'
            : '';
        var resolveBtn = (vi && status !== 'resolved')
            ? '<button onclick="setAmzThreadStatus(\'' + domId + '\', \'resolved\', this)" class="text-xs px-3 py-1 rounded-full bg-green-100 text-green-700 hover:bg-green-200 font-semibold transition">Marcar resuelto</button>'
            : (vi && status === 'resolved')
                ? '<button onclick="setAmzThreadStatus(\'' + domId + '\', \'pending\', this)" class="text-xs px-3 py-1 rounded-full bg-gray-100 text-gray-500 hover:bg-gray-200 font-semibold transition">Reabrir</button>'
                : '';
        return '<div class="flex items-center gap-2 mb-2 flex-wrap">' + takeBtn + resolveBtn + '</div>';
    }

    function threadHtml(th, variant) {
        var domId = variant + '-' + _amzMsgsSanitizeId(th.reply_to_addr);
        var lastInbound = null;
        for (var i = th.messages.length - 1; i >= 0; i--) {
            if (th.messages[i].direction === 'inbound') { lastInbound = th.messages[i]; break; }
        }
        var priorHandler = (th.view_info && th.view_info.viewed_by) ? th.view_info.viewed_by : '';

        var head = '<div class="flex items-center justify-between gap-2 flex-wrap">' +
                '<span class="font-semibold text-sm text-gray-800">' + _amzMsgsEscHtml(th.buyer_name || 'Comprador') + '</span>' +
                statusBadge(th, domId) +
                (th.unread > 0 ? '<span class="text-[10px] font-bold bg-red-100 text-red-700 px-1.5 py-0.5 rounded-full">' + th.unread + ' nuevo(s)</span>' : '') +
                '<span class="text-xs text-gray-400 ml-auto">' + _amzMsgsFmtDate(th.last_ts) + '</span>' +
            '</div>';
        var meta = '<div class="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-gray-400 mt-0.5">' +
                (th.order_id ? '<span>Orden <span class="font-mono">' + _amzMsgsEscHtml(th.order_id) + '</span></span>' : '') +
                (th.asin ? '<span>ASIN <span class="font-mono">' + _amzMsgsEscHtml(th.asin) + '</span></span>' : '') +
            '</div>' +
            (th.product_title ? '<p class="text-xs text-gray-500 truncate mt-0.5">' + _amzMsgsEscHtml(th.product_title) + '</p>' : '');

        var messages = '<div id="amz-thread-messages-' + domId + '" class="mt-2 space-y-1.5 max-h-48 overflow-y-auto">' +
            th.messages.map(function(m) {
                var isOut = m.direction === 'outbound';
                return '<div class="flex ' + (isOut ? 'justify-end' : 'justify-start') + '">' +
                    '<div class="max-w-[85%] rounded-lg px-2.5 py-2 text-sm ' + (isOut ? 'bg-blue-500 text-white' : 'bg-teal-50 text-gray-800 border border-teal-100') + ' whitespace-pre-line"' +
                        ' data-msg-role="' + (isOut ? 'seller' : 'buyer') + '" data-msg-text="' + _amzMsgsEscAttr(m.body_text) + '">' +
                        '<p class="text-[10px] font-medium ' + (isOut ? 'text-blue-100' : 'text-gray-400') + ' mb-0.5">' + (isOut ? 'Tú' : 'Comprador') + ' &middot; ' + _amzMsgsFmtDate(m.ts) + '</p>' +
                        _amzMsgsEscHtml(m.body_text) +
                    '</div>' +
                '</div>';
            }).join('') +
        '</div>';

        var aiButton = lastInbound ? (
            '<div class="mt-2 flex items-center gap-2 flex-wrap">' +
                '<button onclick="suggestBuyerMessageReply(this)" data-thread-id="' + domId + '"' +
                    ' data-product-title="' + _amzMsgsEscAttr(th.product_title) + '" data-order-id="' + _amzMsgsEscAttr(th.order_id) + '"' +
                    ' data-marketplace="' + _amzMsgsEscAttr(window.amzMarketplaceName || 'MX') + '" data-prior-handler="' + _amzMsgsEscAttr(priorHandler) + '"' +
                    ' class="text-xs bg-purple-100 text-purple-700 px-3 py-1.5 rounded-full hover:bg-purple-200 font-semibold transition-colors flex-shrink-0">' +
                    '<svg class="w-4 h-4 inline mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>Sugerir con IA' +
                '</button>' +
                '<input type="text" id="ai-context-bmsg-' + domId + '" placeholder="Instrucciones para la IA (ej: ofrece reembolso, menciona garantía...)"' +
                    ' class="flex-1 min-w-[160px] border border-purple-200 rounded-full px-3 py-1 text-xs text-gray-700 focus:ring-1 focus:ring-purple-400 focus:outline-none placeholder-gray-400">' +
            '</div>' +
            '<div id="ai-panel-bmsg-' + domId + '" class="hidden mt-2">' +
                '<div class="bg-purple-50 border border-purple-200 rounded-lg p-3">' +
                    '<div class="flex items-center gap-2 mb-2">' +
                        '<span class="text-purple-600 text-xs font-semibold">Sugerencia IA</span>' +
                        '<div id="ai-spin-bmsg-' + domId + '" class="hidden"><svg class="animate-spin h-4 w-4 text-purple-500" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg></div>' +
                    '</div>' +
                    '<div id="ai-text-bmsg-' + domId + '" class="text-sm text-gray-700 whitespace-pre-wrap"></div>' +
                    '<div id="ai-act-bmsg-' + domId + '" class="hidden flex gap-2 mt-2">' +
                        '<button onclick="useAiSuggestion(\'buyer_message\', \'' + domId + '\')" class="text-xs bg-purple-600 text-white px-3 py-1 rounded hover:bg-purple-700">Usar respuesta</button>' +
                        '<button onclick="suggestBuyerMessageReply(document.querySelector(\'[data-thread-id=&quot;' + domId + '&quot;]\'))" class="text-xs bg-gray-200 text-gray-700 px-3 py-1 rounded hover:bg-gray-300">Regenerar</button>' +
                    '</div>' +
                '</div>' +
            '</div>'
        ) : '';

        var replyBox = lastInbound ? (
            '<div class="mt-2">' +
                '<textarea id="reply-text-' + domId + '" rows="2" placeholder="Escribe tu respuesta..." class="w-full text-xs border border-gray-200 rounded-lg p-2 focus:outline-none focus:ring-1 focus:ring-teal-400"></textarea>' +
                '<div class="flex items-center justify-between mt-1 gap-2 flex-wrap">' +
                    '<input type="file" id="reply-file-' + domId + '" class="text-[11px] text-gray-500 max-w-[180px]">' +
                    '<button onclick="replyToBuyerMessage(\'' + domId + '\', ' + lastInbound.id + ')" id="reply-btn-' + domId + '" class="text-xs font-semibold bg-teal-500 hover:bg-teal-600 text-white px-3 py-1 rounded-lg transition">Responder</button>' +
                '</div>' +
            '</div>'
        ) : '';

        return head + meta + actionButtons(th, domId) + messages + aiButton + replyBox;
    }

    var cards = '<div class="md:hidden space-y-3">' + threads.map(function(th) {
        var domId = 'm-' + _amzMsgsSanitizeId(th.reply_to_addr);
        return '<div id="thread-card-' + domId + '" data-reply-to-addr="' + _amzMsgsEscAttr(th.reply_to_addr) + '" class="border border-gray-100 rounded-xl p-3">' + threadHtml(th, 'm') + '</div>';
    }).join('') + '</div>';

    var table = '<div class="hidden md:block space-y-3">' + threads.map(function(th) {
        var domId = 'd-' + _amzMsgsSanitizeId(th.reply_to_addr);
        return '<div id="thread-card-' + domId + '" data-reply-to-addr="' + _amzMsgsEscAttr(th.reply_to_addr) + '" class="border border-gray-100 rounded-xl p-3 max-w-3xl">' + threadHtml(th, 'd') + '</div>';
    }).join('') + '</div>';

    el.innerHTML = cards + table;

    // Marcar como leídos los inbound sin leer que se acaban de mostrar —
    // UNA sola llamada con todos los IDs, no una por mensaje.
    var unreadIds = [];
    threads.forEach(function(th) {
        th.messages.forEach(function(m) {
            if (m.direction === 'inbound' && !m.read_at) unreadIds.push(m.id);
        });
    });
    if (unreadIds.length) {
        fetch('/api/amazon/buyer-messages/mark-read', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: unreadIds }),
        }).catch(function() {});
    }
}

window.takeAmzThread = function(domId, btn) {
    var card = btn.closest('[id^="thread-card-"]');
    var addr = card ? card.getAttribute('data-reply-to-addr') : '';
    btn.disabled = true; btn.textContent = '...';
    fetch('/api/amazon/buyer-messages/take', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seller_id: window.amzActiveSellerId, reply_to_addr: addr }),
    })
        .then(function(r) { return r.json(); })
        .then(function() { loadAmzBuyerMessages(); })
        .catch(function() { btn.disabled = false; btn.textContent = 'Tomar'; });
};

window.setAmzThreadStatus = function(domId, status, btn) {
    var card = btn.closest('[id^="thread-card-"]');
    var addr = card ? card.getAttribute('data-reply-to-addr') : '';
    btn.disabled = true;
    fetch('/api/amazon/buyer-messages/status', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seller_id: window.amzActiveSellerId, reply_to_addr: addr, status: status }),
    })
        .then(function(r) { return r.json(); })
        .then(function() { loadAmzBuyerMessages(); })
        .catch(function() { btn.disabled = false; });
};

window.replyToBuyerMessage = function(domId, messageId) {
    var ta = document.getElementById('reply-text-' + domId);
    var btn = document.getElementById('reply-btn-' + domId);
    var fileInput = document.getElementById('reply-file-' + domId);
    var text = (ta.value || '').trim();
    if (!text) { ta.focus(); return; }
    btn.disabled = true;
    btn.textContent = 'Enviando...';

    var fd = new FormData();
    fd.append('text', text);
    if (fileInput && fileInput.files && fileInput.files[0]) {
        fd.append('attachment', fileInput.files[0]);
    }

    var controller = new AbortController();
    var timeoutId = setTimeout(function() { controller.abort(); }, 30000);

    fetch('/api/amazon/buyer-messages/' + messageId + '/reply', {
        method: 'POST',
        body: fd,
        signal: controller.signal,
    })
        .then(function(r) { clearTimeout(timeoutId); return r.json(); })
        .then(function(data) {
            if (data.ok) {
                loadAmzBuyerMessages();
            } else {
                alert(data.detail || 'No se pudo enviar la respuesta');
                btn.disabled = false;
                btn.textContent = 'Responder';
            }
        })
        .catch(function(e) {
            clearTimeout(timeoutId);
            var msg = (e.name === 'AbortError') ? 'Tardó demasiado en responder (30s) — puede que sí se haya enviado, revisa el hilo en unos segundos.' : ('Error: ' + e.message);
            alert(msg);
            btn.disabled = false;
            btn.textContent = 'Responder';
        });
};
