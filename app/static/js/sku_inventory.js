/**
 * SKU Inventory - Lanzar SKUs
 * Extracted from sku_inventory.html template
 */
(function() {
    var allResults = [];
    var currentFilter = 'all';
    var currentStep = 1;
    var selectedSku = null;
    var selectedStock = 0;
    var selectedCategory = null;
    var categoryAttributes = null;
    var researchData = null;
    var researchSkipped = false;
    var validationPassed = false;
    var aiAvailable = false;
    var activeAiController = null; // AbortController for cancelling AI requests

    // Check AI availability on load
    checkAiStatus();

    async function checkAiStatus() {
        try {
            var resp = await fetch('/api/sku-inventory/ai-status');
            var data = await resp.json();
            aiAvailable = data.available === true;
        } catch (e) {
            aiAvailable = false;
        }
        // Toggle AI button visibility
        document.querySelectorAll('.ai-btn').forEach(function(btn) {
            btn.classList.toggle('hidden', !aiAvailable);
        });
    }

    // File input handler
    document.getElementById('file-input').addEventListener('change', function(e) {
        var file = e.target.files[0];
        if (file) {
            document.getElementById('file-name').textContent = 'Archivo: ' + file.name;
        }
    });

    // Comparar button
    document.getElementById('btn-comparar').addEventListener('click', doCompare);
    document.getElementById('btn-limpiar').addEventListener('click', function() {
        document.getElementById('file-input').value = '';
        document.getElementById('file-name').textContent = '';
        document.getElementById('text-skus').value = '';
        document.getElementById('sku-count').textContent = '';
        document.getElementById('loading').classList.add('hidden');
        document.getElementById('summary-section').classList.add('hidden');
        document.getElementById('results-section').classList.add('hidden');
        document.getElementById('empty-state').classList.remove('hidden');
        allResults = [];
    });

    // Filter buttons
    document.querySelectorAll('.filter-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            currentFilter = this.getAttribute('data-filter');
            setActiveFilter(currentFilter);
            renderTable();
        });
    });

    // Search
    document.getElementById('search-sku').addEventListener('input', renderTable);

    // Title character counter
    var titleInput = document.getElementById('item-title');
    if (titleInput) {
        var counterEl = document.getElementById('title-char-count');
        function updateTitleCounter() {
            var len = titleInput.value.length;
            if (counterEl) {
                counterEl.textContent = len + '/60';
                counterEl.className = 'text-xs ml-2 ' + (len > 60 ? 'text-red-500 font-bold' : len >= 40 ? 'text-green-600' : 'text-gray-400');
            }
        }
        titleInput.addEventListener('input', updateTitleCounter);
        // Initial update
        updateTitleCounter();
    }

    async function doCompare() {
        var file = document.getElementById('file-input').files[0];
        var textSkus = document.getElementById('text-skus').value;

        if (!file && !textSkus.trim()) {
            alert('Sube un archivo o pega una lista de SKUs');
            return;
        }

        document.getElementById('loading').classList.remove('hidden');
        document.getElementById('summary-section').classList.add('hidden');
        document.getElementById('results-section').classList.add('hidden');
        document.getElementById('empty-state').classList.add('hidden');

        try {
            var formData = new FormData();
            if (file) formData.append('file', file);
            if (textSkus.trim()) formData.append('text_skus', textSkus);

            var parseResp = await fetch('/api/sku-inventory/parse-skus', {
                method: 'POST',
                body: formData
            });
            var parseData = await parseResp.json();

            if (parseData.error) {
                alert(parseData.error);
                document.getElementById('loading').classList.add('hidden');
                document.getElementById('empty-state').classList.remove('hidden');
                return;
            }

            var skus = parseData.skus;
            document.getElementById('sku-count').textContent = skus.length + ' SKUs encontrados';

            var compareResp = await fetch('/api/sku-inventory/compare', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(skus)
            });
            var compareData = await compareResp.json();

            if (compareData.error) {
                alert(compareData.error);
                document.getElementById('loading').classList.add('hidden');
                return;
            }

            allResults = compareData.results;
            renderSummary(compareData.summary);

            document.getElementById('loading').classList.add('hidden');
            document.getElementById('summary-section').classList.remove('hidden');
            document.getElementById('results-section').classList.remove('hidden');

            currentFilter = 'all';
            setActiveFilter('all');
            renderTable();

        } catch (err) {
            console.error(err);
            alert('Error al procesar: ' + err.message);
            document.getElementById('loading').classList.add('hidden');
        }
    }

    function renderSummary(s) {
        document.getElementById('sum-total').textContent = s.total;
        document.getElementById('sum-not-published').textContent = s.not_published;
        document.getElementById('sum-paused').textContent = s.paused;
        document.getElementById('sum-active').textContent = s.active;
        document.getElementById('sum-no-stock').textContent = s.no_stock;
    }

    function setActiveFilter(f) {
        document.querySelectorAll('.filter-btn').forEach(function(b) {
            b.classList.remove('bg-yellow-400', 'text-gray-800');
            b.classList.add('bg-gray-200', 'text-gray-600');
        });
        var active = document.querySelector('.filter-btn[data-filter="' + f + '"]');
        if (active) {
            active.classList.add('bg-yellow-400', 'text-gray-800');
            active.classList.remove('bg-gray-200', 'text-gray-600');
        }
    }

    function statusBadge(status) {
        var map = {
            not_published: '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-green-100 text-green-800">Candidato</span>',
            paused: '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-purple-100 text-purple-800">Pausado</span>',
            active: '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-blue-100 text-blue-800">Activo</span>',
            no_stock: '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-gray-200 text-gray-600">Sin stock</span>'
        };
        return map[status] || status;
    }

    function actionButton(r) {
        if (r.meli_status === 'not_published') {
            return '<button onclick="openCreateModal(\'' + r.sku + '\', ' + (r.total_stock || 0) + ')" class="px-3 py-1 bg-green-500 text-white text-xs font-medium rounded hover:bg-green-600 transition">Lanzar</button>';
        } else if (r.meli_status === 'paused') {
            var bestId = r.best_item_id || r.item_id;
            if (r.items && r.items.length > 1) {
                return _multiItemActions(r, 'paused');
            }
            return '<div class="flex gap-1 justify-center">' +
                '<button onclick="reactivateItem(\'' + bestId + '\')" class="px-2 py-1 bg-purple-500 text-white text-xs font-medium rounded hover:bg-purple-600 transition">Reactivar</button>' +
                '<button onclick="openOptimizeModal(\'' + bestId + '\', \'' + r.sku + '\')" class="px-2 py-1 bg-yellow-400 text-gray-800 text-xs font-medium rounded hover:bg-yellow-500 transition">Optimizar</button>' +
                '</div>';
        } else if (r.meli_status === 'active') {
            var bestId = r.best_item_id || r.item_id;
            if (r.items && r.items.length > 1) {
                return _multiItemActions(r, 'active');
            }
            var btns = '<div class="flex gap-1 justify-center">';
            if (r.permalink) {
                btns += '<a href="' + r.permalink + '" target="_blank" class="px-2 py-1 bg-blue-100 text-blue-700 text-xs font-medium rounded hover:bg-blue-200 transition">Ver</a>';
            }
            btns += '<button onclick="openOptimizeModal(\'' + bestId + '\', \'' + r.sku + '\')" class="px-2 py-1 bg-yellow-400 text-gray-800 text-xs font-medium rounded hover:bg-yellow-500 transition">Optimizar</button>';
            btns += '</div>';
            return btns;
        }
        return '-';
    }

    function _multiItemActions(r, mode) {
        var html = '<details class="text-xs">';
        html += '<summary class="cursor-pointer px-2 py-1 bg-yellow-400 text-gray-800 font-medium rounded hover:bg-yellow-500 transition inline-block text-center">Acciones (' + r.items.length + ')</summary>';
        html += '<div class="mt-1 flex flex-col gap-1">';
        r.items.forEach(function(it) {
            var shortId = it.id.replace('MLM', '');
            var statusIcon = it.status === 'active' ? '\u2705' : it.status === 'paused' ? '\u23f8\ufe0f' : '\u26aa';
            html += '<div class="flex items-center gap-1 flex-wrap">';
            html += '<span class="text-[9px]">' + statusIcon + shortId + '</span>';
            if (it.status === 'paused') {
                html += '<button onclick="reactivateItem(\'' + it.id + '\')" class="px-1 py-0.5 bg-purple-500 text-white text-[9px] rounded hover:bg-purple-600">React</button>';
            }
            html += '<button onclick="openOptimizeModal(\'' + it.id + '\', \'' + r.sku + '\')" class="px-1 py-0.5 bg-yellow-400 text-gray-800 text-[9px] rounded hover:bg-yellow-500">Opt</button>';
            html += '</div>';
        });
        html += '</div></details>';
        return html;
    }

    function renderMultiItemIds(r) {
        if (!r.items || r.items.length === 0) {
            if (r.item_id) {
                return '<a href="https://articulo.mercadolibre.com.mx/' + r.item_id + '" target="_blank" class="text-blue-600 hover:underline">' + r.item_id + '</a>';
            }
            return '<span class="text-gray-400">-</span>';
        }

        if (r.items.length === 1) {
            var it = r.items[0];
            var typeBadge = it.type === 'variacion'
                ? '<span class="px-1 py-0.5 bg-indigo-100 text-indigo-700 text-[9px] font-bold rounded">VAR\u00d7' + it.variation_count + '</span>'
                : '<span class="px-1 py-0.5 bg-gray-100 text-gray-600 text-[9px] font-bold rounded">UNICA</span>';
            var singleHtml = '<div class="flex flex-col gap-0.5">';
            singleHtml += '<div class="flex items-center gap-1">';
            singleHtml += '<a href="https://articulo.mercadolibre.com.mx/' + it.id + '" target="_blank" class="text-blue-600 hover:underline">' + it.id + '</a>';
            singleHtml += typeBadge;
            singleHtml += '</div>';
            if (it.matching_variations && it.matching_variations.length > 0) {
                it.matching_variations.forEach(function(mv) {
                    singleHtml += '<div class="text-[9px] text-gray-500 pl-3">\u21b3 ' + mv.attrs + ' (stock: ' + mv.stock + ')</div>';
                });
            }
            singleHtml += '</div>';
            return singleHtml;
        }

        var html = '<div class="flex flex-col divide-y divide-gray-200">';
        r.items.forEach(function(it, idx) {
            var statusDot = it.status === 'active' ? 'bg-green-400' : it.status === 'paused' ? 'bg-purple-400' : 'bg-gray-400';
            var typeBadge = it.type === 'variacion'
                ? '<span class="px-1 py-0.5 bg-indigo-100 text-indigo-700 text-[9px] font-bold rounded">VAR\u00d7' + it.variation_count + '</span>'
                : '<span class="px-1 py-0.5 bg-gray-100 text-gray-600 text-[9px] font-bold rounded">UNICA</span>';
            var stockTxt = '<span class="text-[9px] text-gray-400">stk:' + it.total_meli_stock + '</span>';

            html += '<div class="py-1">';
            html += '<div class="flex items-center gap-1">';
            html += '<span class="w-1.5 h-1.5 rounded-full ' + statusDot + ' flex-shrink-0"></span>';
            html += '<a href="https://articulo.mercadolibre.com.mx/' + it.id + '" target="_blank" class="text-blue-600 hover:underline text-[10px] font-mono">' + it.id + '</a>';
            html += typeBadge + ' ' + stockTxt;
            html += '</div>';

            if (it.matching_variations && it.matching_variations.length > 0) {
                it.matching_variations.forEach(function(mv) {
                    html += '<div class="text-[9px] text-gray-500 pl-3">\u21b3 ' + mv.attrs + ' (stock: ' + mv.stock + ')</div>';
                });
            }
            html += '</div>';
        });
        html += '<div class="pt-1 text-[9px] text-gray-400">' + r.items.length + ' listados</div>';
        html += '</div>';
        return html;
    }

    function renderTable() {
        var tbody = document.getElementById('results-tbody');
        var search = (document.getElementById('search-sku').value || '').toLowerCase();

        var filtered = allResults.filter(function(r) {
            if (currentFilter !== 'all' && r.meli_status !== currentFilter) return false;
            if (search && r.sku.toLowerCase().indexOf(search) === -1) {
                if (r.item_title && r.item_title.toLowerCase().indexOf(search) !== -1) return true;
                return false;
            }
            return true;
        });

        if (filtered.length === 0) {
            tbody.innerHTML = '<tr><td colspan="12" class="px-4 py-8 text-center text-gray-400">No hay SKUs con este filtro</td></tr>';
            return;
        }

        // Flatten: one row per MLM item
        var rows = [];
        filtered.forEach(function(r) {
            var stockGr = r.stock_gr ? r.stock_gr.total : 0;
            var stockIc = r.stock_ic ? r.stock_ic.total : 0;
            var stockOther = r.stock_other || 0;
            var grTooltip = r.stock_gr ? 'MTY: ' + (r.stock_gr.mty||0) + ' | CDMX: ' + (r.stock_gr.cdmx||0) + ' | TJ: ' + (r.stock_gr.tj||0) : '';
            var icTooltip = r.stock_ic ? 'MTY: ' + (r.stock_ic.mty||0) + ' | CDMX: ' + (r.stock_ic.cdmx||0) + ' | TJ: ' + (r.stock_ic.tj||0) : '';

            var shared = { sku: r.sku, meli_status: r.meli_status, stockGr: stockGr, stockIc: stockIc, stockOther: stockOther, grTooltip: grTooltip, icTooltip: icTooltip, total_stock: r.total_stock };

            if (!r.items || r.items.length === 0) {
                rows.push({ shared: shared, item: null, r: r });
            } else {
                r.items.forEach(function(it) {
                    rows.push({ shared: shared, item: it, r: r });
                });
            }
        });

        var html = '';
        rows.forEach(function(row) {
            var s = row.shared;
            var it = row.item;
            var r = row.r;

            var rowBg = '';
            if (s.meli_status === 'not_published') rowBg = 'bg-green-50';
            else if (s.meli_status === 'paused') rowBg = 'bg-purple-50';
            else if (s.meli_status === 'no_stock') rowBg = 'bg-gray-50';

            html += '<tr class="border-t ' + rowBg + ' hover:bg-gray-100">';

            // Status
            var itemStatus = it ? it.status : s.meli_status;
            html += '<td class="px-4 py-2">' + statusBadge(itemStatus) + '</td>';

            // SKU
            html += '<td class="px-4 py-2 font-mono text-xs font-semibold">' + s.sku + '</td>';

            // BinManager stock
            html += '<td class="px-4 py-2 text-right font-semibold cursor-help ' + (s.stockGr > 0 ? 'text-green-600' : 'text-gray-400') + '" title="' + s.grTooltip + '">' + s.stockGr + '</td>';
            html += '<td class="px-4 py-2 text-right font-semibold cursor-help ' + (s.stockIc > 0 ? 'text-blue-600' : 'text-gray-400') + '" title="' + s.icTooltip + '">' + s.stockIc + '</td>';
            html += '<td class="px-4 py-2 text-right ' + (s.stockOther > 0 ? 'text-orange-500' : 'text-gray-400') + '">' + (s.stockOther > 0 ? s.stockOther : '-') + '</td>';

            if (!it) {
                // No listing
                html += '<td class="px-3 py-2 text-gray-400">-</td>';
                html += '<td class="px-3 py-2 text-gray-400">-</td>';
                html += '<td class="px-3 py-2 text-center">-</td>';
                html += '<td class="px-3 py-2 text-right">-</td>';
                html += '<td class="px-3 py-2 text-right">-</td>';
                html += '<td class="px-3 py-2 text-center">' + actionButton(r) + '</td>';
            } else {
                html += _renderItemCells(it, r);
            }

            html += '</tr>';
        });
        tbody.innerHTML = html;
    }

    function _renderItemCells(it, r) {
        var statusDot = it.status === 'active' ? 'bg-green-400' : it.status === 'paused' ? 'bg-purple-400' : 'bg-gray-400';
        var typeBadge = it.type === 'variacion'
            ? '<span class="px-1 py-0.5 bg-indigo-100 text-indigo-700 text-[9px] font-bold rounded">VAR\u00d7' + it.variation_count + '</span>'
            : '<span class="px-1 py-0.5 bg-gray-100 text-gray-600 text-[9px] font-bold rounded">UNICA</span>';

        // Score
        var scoreHtml = '-';
        if (it.score !== null && it.score !== undefined) {
            var scoreColor = 'bg-red-100 text-red-700';
            if (it.score >= 75) scoreColor = 'bg-green-100 text-green-700';
            else if (it.score >= 50) scoreColor = 'bg-yellow-100 text-yellow-700';
            scoreHtml = '<span class="inline-block px-2 py-0.5 rounded-full text-xs font-bold ' + scoreColor + '">' + it.score + '%</span>';
        }

        // MeLi stock
        var itemStock = it.total_meli_stock !== undefined ? it.total_meli_stock : 0;
        var meliStockHtml = '<span class="inline-flex items-center gap-1"><span>' + itemStock + '</span>';
        meliStockHtml += ' <button onclick="editMeliStock(\'' + it.id + '\', ' + itemStock + ')" class="text-gray-400 hover:text-blue-600" title="Editar stock MeLi">';
        meliStockHtml += '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"/></svg>';
        meliStockHtml += '</button></span>';

        // Variation detail
        var varDetail = '';
        if (it.matching_variations && it.matching_variations.length > 0) {
            it.matching_variations.forEach(function(mv) {
                varDetail += '<div class="text-[9px] text-gray-500">\u21b3 ' + mv.attrs + ' (stk:' + mv.stock + ')</div>';
            });
        }

        // Title
        var title = it.title || '';
        var titleShort = title.length > 35 ? title.substring(0, 35) + '...' : title;

        // Actions
        var actHtml = '';
        if (it.status === 'paused') {
            actHtml = '<div class="flex gap-1 justify-center">' +
                '<button onclick="reactivateItem(\'' + it.id + '\')" class="px-2 py-0.5 bg-purple-500 text-white text-[10px] rounded hover:bg-purple-600">React</button>' +
                '<button onclick="openOptimizeModal(\'' + it.id + '\', \'' + r.sku + '\')" class="px-2 py-0.5 bg-yellow-400 text-gray-800 text-[10px] rounded hover:bg-yellow-500">Opt</button>' +
                '</div>';
        } else if (it.status === 'active') {
            actHtml = '<div class="flex gap-1 justify-center">';
            if (it.permalink) {
                actHtml += '<a href="' + it.permalink + '" target="_blank" class="px-2 py-0.5 bg-blue-100 text-blue-700 text-[10px] rounded hover:bg-blue-200">Ver</a>';
            }
            actHtml += '<button onclick="openOptimizeModal(\'' + it.id + '\', \'' + r.sku + '\')" class="px-2 py-0.5 bg-yellow-400 text-gray-800 text-[10px] rounded hover:bg-yellow-500">Opt</button>';
            actHtml += '</div>';
        }

        var cells = '';
        cells += '<td class="px-3 py-2"><div class="flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full ' + statusDot + '"></span><a href="https://articulo.mercadolibre.com.mx/' + it.id + '" target="_blank" class="text-blue-600 hover:underline text-[11px] font-mono">' + it.id + '</a>' + typeBadge + '</div>' + varDetail + '</td>';
        cells += '<td class="px-3 py-2 text-gray-700 text-xs max-w-[180px] truncate" title="' + title + '">' + (titleShort || '<span class="text-gray-400">-</span>') + '</td>';
        cells += '<td class="px-3 py-2 text-center">' + scoreHtml + '</td>';
        cells += '<td class="px-3 py-2 text-right">' + meliStockHtml + '</td>';
        cells += '<td class="px-3 py-1.5 text-right">' + (it.price ? '$' + it.price.toLocaleString('es-MX') : '-') + '</td>';
        cells += '<td class="px-3 py-1.5 text-center">' + actHtml + '</td>';
        return cells;
    }

    // =========================================================
    // Modal functions
    // =========================================================

    window.openCreateModal = function(sku, stock) {
        selectedSku = sku;
        selectedStock = stock;
        selectedCategory = null;
        categoryAttributes = null;
        researchData = null;
        researchSkipped = false;
        validationPassed = false;
        currentStep = 0;

        document.getElementById('modal-sku').textContent = 'SKU: ' + sku;
        document.getElementById('item-title').value = sku;
        document.getElementById('item-quantity').value = stock;
        document.getElementById('item-price').value = '';
        document.getElementById('item-description').value = '';
        document.getElementById('item-pictures').value = '';
        document.getElementById('categories-list').innerHTML = '';
        document.getElementById('research-warnings').classList.add('hidden');
        document.getElementById('competitors-panel').classList.add('hidden');
        document.getElementById('price-hint').classList.add('hidden');
        document.getElementById('reference-pictures').classList.add('hidden');
        document.getElementById('validation-result').innerHTML = '';

        // Reset compliance fields
        var warrantyType = document.getElementById('warranty-type');
        if (warrantyType) warrantyType.value = 'manufacturer';
        var warrantyDuration = document.getElementById('warranty-duration');
        if (warrantyDuration) warrantyDuration.value = '12';
        var shippingMode = document.getElementById('shipping-mode');
        if (shippingMode) shippingMode.value = 'me2';
        var freeShipping = document.getElementById('free-shipping');
        if (freeShipping) freeShipping.checked = true;
        var gtinInput = document.getElementById('gtin-value');
        if (gtinInput) gtinInput.value = '';

        // Reset title counter
        if (typeof updateTitleCounter === 'function') updateTitleCounter();

        // Toggle AI buttons
        document.querySelectorAll('.ai-btn').forEach(function(btn) {
            btn.classList.toggle('hidden', !aiAvailable);
        });

        showStep(0);
        document.getElementById('create-modal').classList.remove('hidden');

        // Start auto-research
        doResearch(sku, stock);
    };

    window.closeModal = function() {
        cancelAiRequest();
        document.getElementById('create-modal').classList.add('hidden');
    };

    window.skipResearch = function() {
        researchSkipped = true;
        currentStep = 1;
        showStep(1);
    };

    // =========================================================
    // Research
    // =========================================================

    async function doResearch(sku, stock) {
        var bar = document.getElementById('research-progress-bar');
        var status = document.getElementById('research-status-text');
        var skipBtn = document.getElementById('btn-skip-research');

        bar.style.width = '10%';
        status.textContent = 'Buscando en MeLi y la web...';

        // Show skip button after 5 seconds
        var skipTimer = setTimeout(function() {
            skipBtn.classList.remove('hidden');
        }, 5000);

        try {
            bar.style.width = '25%';
            status.textContent = 'Analizando competencia en MeLi...';

            var resp = await fetch('/api/sku-inventory/research', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({sku: sku, stock: stock})
            });

            bar.style.width = '70%';
            status.textContent = 'Procesando resultados...';

            var data = await resp.json();

            if (data.error) {
                console.warn('Research failed:', data.error);
                bar.style.width = '100%';
                status.textContent = 'Investigacion no disponible - continuando manualmente';
                clearTimeout(skipTimer);
                skipBtn.classList.add('hidden');
                setTimeout(function() {
                    if (!researchSkipped) {
                        currentStep = 1;
                        showStep(1);
                    }
                }, 1000);
                return;
            }

            bar.style.width = '90%';
            status.textContent = 'Generando listado optimizado...';

            researchData = data;

            bar.style.width = '100%';
            status.textContent = 'Listo!';
            clearTimeout(skipTimer);
            skipBtn.classList.add('hidden');

            // Pre-fill fields and move to step 1
            setTimeout(function() {
                if (!researchSkipped) {
                    prefillFromResearch(data);
                    currentStep = 1;
                    showStep(1);
                }
            }, 500);

        } catch (err) {
            console.warn('Research error:', err);
            bar.style.width = '100%';
            status.textContent = 'Error en investigacion - continuando manualmente';
            clearTimeout(skipTimer);
            skipBtn.classList.add('hidden');
            setTimeout(function() {
                if (!researchSkipped) {
                    currentStep = 1;
                    showStep(1);
                }
            }, 1000);
        }
    }

    function prefillFromResearch(data) {
        // Title
        if (data.title && data.title.length > 3) {
            document.getElementById('item-title').value = data.title;
            markAutoFilled('item-title');
            // Update counter
            var counterEl = document.getElementById('title-char-count');
            if (counterEl) {
                var len = data.title.length;
                counterEl.textContent = len + '/60';
                counterEl.className = 'text-xs ml-2 ' + (len > 60 ? 'text-red-500 font-bold' : len >= 40 ? 'text-green-600' : 'text-gray-400');
            }
        }

        // Category - auto-select if found
        if (data.category_id) {
            selectedCategory = {
                id: data.category_id,
                name: data.category_name || data.category_id
            };

            var catHtml = '<div class="border rounded-lg p-3 bg-yellow-50 border-yellow-400">';
            catHtml += '<div class="flex items-center gap-2">';
            catHtml += '<p class="font-medium text-gray-800">' + (data.category_name || data.category_id) + '</p>';
            catHtml += '<span class="text-xs bg-yellow-200 text-yellow-800 px-2 py-0.5 rounded-full">Auto</span>';
            catHtml += '</div>';
            if (data.category_path) {
                catHtml += '<p class="text-xs text-gray-500">' + data.category_path + '</p>';
            }
            catHtml += '<p class="text-xs text-yellow-600 mt-1">Puedes buscar otra categoria con el boton de arriba</p>';
            catHtml += '</div>';
            document.getElementById('categories-list').innerHTML = catHtml;
        }

        // Warnings
        if (data.warnings && data.warnings.length > 0) {
            var wHtml = '<div class="bg-yellow-50 border border-yellow-200 rounded-lg p-3">';
            wHtml += '<p class="text-xs font-semibold text-yellow-700 mb-1">Avisos de la investigacion:</p>';
            data.warnings.forEach(function(w) {
                wHtml += '<p class="text-xs text-yellow-600">- ' + w + '</p>';
            });
            wHtml += '</div>';
            document.getElementById('research-warnings').innerHTML = wHtml;
            document.getElementById('research-warnings').classList.remove('hidden');
        }

        // Competitors panel
        if (data.competitors && data.competitors.length > 0) {
            var priceHtml = '';
            if (data.suggested_price && data.suggested_price.min) {
                priceHtml = '<div class="flex gap-4 text-sm">';
                priceHtml += '<span class="text-gray-600">Min: <strong class="text-gray-800">$' + data.suggested_price.min.toLocaleString('es-MX') + '</strong></span>';
                priceHtml += '<span class="text-gray-600">Mediana: <strong class="text-green-600">$' + data.suggested_price.median.toLocaleString('es-MX') + '</strong></span>';
                priceHtml += '<span class="text-gray-600">Max: <strong class="text-gray-800">$' + data.suggested_price.max.toLocaleString('es-MX') + '</strong></span>';
                priceHtml += '</div>';
            }
            document.getElementById('competitors-price-range').innerHTML = priceHtml;

            var compHtml = '';
            data.competitors.forEach(function(c) {
                compHtml += '<div class="flex items-center gap-3 p-2 bg-gray-50 rounded text-xs">';
                if (c.thumbnail) {
                    compHtml += '<img src="' + c.thumbnail + '" class="w-10 h-10 object-cover rounded" onerror="this.style.display=\'none\'">';
                }
                compHtml += '<div class="flex-1 min-w-0">';
                compHtml += '<p class="truncate text-gray-700">' + c.title + '</p>';
                compHtml += '<p class="text-gray-500">$' + (c.price || 0).toLocaleString('es-MX') + ' | ' + (c.sold_quantity || 0) + ' vendidos</p>';
                compHtml += '</div></div>';
            });
            document.getElementById('competitors-list').innerHTML = compHtml;
            document.getElementById('competitors-panel').classList.remove('hidden');
        }

        // Price hint for step 3
        if (data.suggested_price) {
            var sp = data.suggested_price;
            var hintHtml = '';

            if (sp.suggested) {
                document.getElementById('item-price').value = sp.suggested;
                markAutoFilled('item-price');
            }

            if (sp.formula) {
                hintHtml += '<p class="text-green-700 font-medium">Precio calculado: $' + (sp.calculated || 0).toLocaleString('es-MX') + ' MXN (' + sp.formula + ')</p>';
            }
            if (sp.source === 'market') {
                hintHtml += '<p class="text-blue-600">Precio basado en mediana del mercado</p>';
            }
            if (sp.market_median) {
                hintHtml += '<p class="text-gray-500">Mercado: $' + (sp.market_min || 0).toLocaleString('es-MX') + ' - $' + (sp.market_max || 0).toLocaleString('es-MX') + ' (mediana: $' + sp.market_median.toLocaleString('es-MX') + ')</p>';
            }

            if (hintHtml) {
                document.getElementById('price-hint').innerHTML = hintHtml;
                document.getElementById('price-hint').classList.remove('hidden');
            }
        }

        // Description
        if (data.description) {
            document.getElementById('item-description').value = data.description;
            markAutoFilled('item-description');
        }

        // GTIN from research
        var gtinInput = document.getElementById('gtin-value');
        if (gtinInput && data.gtin) {
            gtinInput.value = data.gtin;
            markAutoFilled('gtin-value');
        }

        // Pictures (reference)
        if (data.pictures && data.pictures.length > 0) {
            var picGrid = '';
            data.pictures.forEach(function(pic) {
                var verifiedBadge = pic.verified ? '<span class="absolute top-0 left-0 text-[9px] bg-green-600 text-white px-1 rounded-br">OK</span>' : '';
                picGrid += '<div class="relative">';
                picGrid += '<img src="' + pic.url + '" class="w-full h-16 object-cover rounded border cursor-pointer hover:opacity-75" ';
                picGrid += 'onclick="useReferencePic(\'' + pic.url + '\')" onerror="this.parentElement.style.display=\'none\'" title="Click para usar">';
                picGrid += verifiedBadge;
                picGrid += '<span class="absolute top-0 right-0 text-[9px] bg-gray-800 text-white px-1 rounded-bl">' + pic.source + '</span>';
                picGrid += '</div>';
            });
            document.getElementById('reference-pictures-grid').innerHTML = picGrid;
            document.getElementById('reference-pictures').classList.remove('hidden');
        }
    }

    function markAutoFilled(elementId) {
        var el = document.getElementById(elementId);
        if (el) {
            el.classList.add('bg-yellow-50', 'border-yellow-300');
            el.addEventListener('input', function handler() {
                el.classList.remove('bg-yellow-50', 'border-yellow-300');
                el.removeEventListener('input', handler);
            }, {once: true});
        }
    }

    window.useReferencePic = function(url) {
        var textarea = document.getElementById('item-pictures');
        var current = textarea.value.trim();
        if (current) {
            textarea.value = current + '\n' + url;
        } else {
            textarea.value = url;
        }
    };

    // =========================================================
    // AI Improve buttons
    // =========================================================

    function cancelAiRequest() {
        if (activeAiController) {
            activeAiController.abort();
            activeAiController = null;
        }
    }

    window.aiAutocorrect = async function() {
        var titleEl = document.getElementById('opt-title');
        var descEl = document.getElementById('opt-description');
        var resultDiv = document.getElementById('opt-autocorrect-result');
        var btn = resultDiv.previousElementSibling;

        if (!titleEl || !descEl) return;

        resultDiv.classList.remove('hidden');
        resultDiv.innerHTML = '<div class="flex items-center gap-2 text-emerald-600"><svg class="animate-spin h-3 w-3" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg> Revisando ortografia y caracteres...</div>';
        btn.disabled = true;

        try {
            var resp = await fetch('/api/sku-inventory/ai-improve', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    field: 'autocorrect',
                    current_value: descEl.value,
                    context: { title: titleEl.value, sku: selectedSku || '' }
                })
            });

            var data = await resp.json();

            if (!resp.ok || data.error) {
                var errMsg = data.error || ('Error HTTP ' + resp.status);
                resultDiv.innerHTML = '<p class="text-red-500 text-xs">' + escapeHtml(errMsg) + '</p>';
                btn.disabled = false;
                return;
            }

            var raw = data.result || '';

            // Parse JSON from response (strip markdown backticks if any)
            var cleaned = raw.replace(/```json\s*/g, '').replace(/```\s*/g, '').trim();
            var parsed;
            try {
                parsed = JSON.parse(cleaned);
            } catch(e) {
                resultDiv.innerHTML = '<p class="text-red-500">No se pudo parsear respuesta de IA</p><pre class="text-[10px] mt-1 whitespace-pre-wrap">' + escapeHtml(raw) + '</pre>';
                btn.disabled = false;
                return;
            }

            var changes = parsed.changes || [];
            if (changes.length === 0) {
                resultDiv.innerHTML = '<p class="text-emerald-700 font-medium">Sin errores encontrados. Titulo y descripcion estan correctos.</p>';
                btn.disabled = false;
                return;
            }

            // Show changes and apply buttons
            var html = '<p class="font-medium text-emerald-700 mb-2">Encontrados ' + changes.length + ' cambio(s):</p>';
            html += '<ul class="list-disc pl-4 space-y-0.5 mb-3">';
            changes.forEach(function(c) { html += '<li>' + escapeHtml(c) + '</li>'; });
            html += '</ul>';
            html += '<div class="flex gap-2">';
            html += '<button type="button" id="btn-apply-autocorrect" class="px-3 py-1 bg-emerald-500 text-white text-xs font-medium rounded hover:bg-emerald-600 transition">Aplicar correcciones</button>';
            html += '<button type="button" onclick="document.getElementById(\'opt-autocorrect-result\').classList.add(\'hidden\')" class="px-3 py-1 border border-gray-300 text-gray-600 text-xs rounded hover:bg-gray-100">Ignorar</button>';
            html += '</div>';
            resultDiv.innerHTML = html;

            document.getElementById('btn-apply-autocorrect').onclick = function() {
                if (parsed.title) {
                    titleEl.value = parsed.title;
                    var lenSpan = document.getElementById('opt-title-len');
                    if (lenSpan) lenSpan.textContent = '(' + parsed.title.length + '/60)';
                }
                if (parsed.description) {
                    descEl.value = parsed.description;
                }
                resultDiv.innerHTML = '<p class="text-emerald-700 font-medium">Correcciones aplicadas.</p>';
                setTimeout(function() { resultDiv.classList.add('hidden'); }, 2000);
            };

        } catch(e) {
            resultDiv.innerHTML = '<p class="text-red-500">Error: ' + escapeHtml(e.message) + '</p>';
        }
        btn.disabled = false;
    };

    window.aiImproveTitle = async function(targetInputId, contextOverrides) {
        var input = document.getElementById(targetInputId);
        if (!input) return;
        var currentValue = input.value.trim();

        var context = Object.assign({
            sku: selectedSku || '',
            brand: '',
            model: '',
            category: selectedCategory ? selectedCategory.name : ''
        }, contextOverrides || {});

        // Gather brand/model from attributes if available
        var brandEl = document.querySelector('.attr-input[data-attr-id="BRAND"]');
        if (brandEl && brandEl.value) context.brand = brandEl.tagName === 'SELECT' ? brandEl.options[brandEl.selectedIndex].text : brandEl.value;
        var modelEl = document.querySelector('.attr-input[data-attr-id="MODEL"]');
        if (modelEl && modelEl.value) context.model = modelEl.tagName === 'SELECT' ? modelEl.options[modelEl.selectedIndex].text : modelEl.value;

        // Show loading state
        var container = input.parentElement;
        var existingSug = container.querySelector('.ai-suggestions');
        if (existingSug) existingSug.remove();

        var sugDiv = document.createElement('div');
        sugDiv.className = 'ai-suggestions mt-2 p-2 bg-purple-50 border border-purple-200 rounded-lg';
        sugDiv.innerHTML = '<div class="flex items-center gap-2 text-xs text-purple-600"><svg class="animate-spin h-3 w-3" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg> Generando sugerencias con IA...</div>';
        container.appendChild(sugDiv);

        cancelAiRequest();
        activeAiController = new AbortController();

        try {
            var resp = await fetch('/api/sku-inventory/ai-improve', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({field: 'title', current_value: currentValue, context: context}),
                signal: activeAiController.signal
            });

            if (!resp.ok) {
                try {
                    var errData = await resp.json();
                    sugDiv.innerHTML = '<p class="text-xs text-red-500">' + escapeHtml(errData.error || 'Error: ' + resp.status) + '</p>';
                } catch(e) {
                    sugDiv.innerHTML = '<p class="text-xs text-red-500">Error: ' + resp.status + '</p>';
                }
                return;
            }

            // Read SSE stream
            var reader = resp.body.getReader();
            var decoder = new TextDecoder();
            var fullText = '';

            while (true) {
                var result = await reader.read();
                if (result.done) break;
                var chunk = decoder.decode(result.value, {stream: true});
                var lines = chunk.split('\n');
                for (var i = 0; i < lines.length; i++) {
                    var line = lines[i];
                    if (line.startsWith('data: ')) {
                        var payload = line.substring(6);
                        if (payload === '[DONE]') break;
                        if (payload.startsWith('[ERROR]')) {
                            sugDiv.innerHTML = '<p class="text-xs text-red-500">' + escapeHtml(payload.substring(8)) + '</p>';
                            return;
                        }
                        try {
                            var parsed = JSON.parse(payload);
                            if (parsed.token) fullText += parsed.token;
                        } catch(e) {
                            fullText += payload;
                        }
                    }
                }
            }

            // Parse suggestions (expect 3 lines)
            var suggestions = fullText.split('\n').filter(function(l) {
                return l.trim().length > 5;
            }).map(function(l) {
                return l.replace(/^\d+[\.\)\-]\s*/, '').trim();
            }).slice(0, 3);

            if (suggestions.length === 0) {
                sugDiv.innerHTML = '<p class="text-xs text-gray-500">No se generaron sugerencias</p>';
                return;
            }

            var chipsHtml = '<p class="text-xs text-purple-700 font-medium mb-1">Sugerencias IA:</p><div class="space-y-1">';
            suggestions.forEach(function(sug) {
                var charCount = sug.length;
                var charColor = charCount > 60 ? 'text-red-500' : charCount >= 40 ? 'text-green-600' : 'text-gray-400';
                chipsHtml += '<div class="flex items-center gap-2">';
                chipsHtml += '<button type="button" onclick="this.closest(\'.ai-suggestions\').previousElementSibling.tagName===\'INPUT\'?this.closest(\'.ai-suggestions\').previousElementSibling.value=this.getAttribute(\'data-val\'):document.getElementById(\'' + targetInputId + '\').value=this.getAttribute(\'data-val\');this.closest(\'.ai-suggestions\').remove()" data-val="' + escapeHtml(sug) + '" class="flex-1 text-left text-xs bg-white border border-purple-200 rounded px-2 py-1 hover:bg-purple-100 transition truncate">' + sug + '</button>';
                chipsHtml += '<span class="text-[10px] ' + charColor + ' flex-shrink-0">' + charCount + '</span>';
                chipsHtml += '</div>';
            });
            chipsHtml += '</div><button type="button" onclick="this.parentElement.remove()" class="mt-1 text-[10px] text-gray-400 hover:text-gray-600">Cerrar</button>';
            sugDiv.innerHTML = chipsHtml;

        } catch (e) {
            if (e.name === 'AbortError') {
                sugDiv.remove();
            } else {
                sugDiv.innerHTML = '<p class="text-xs text-red-500">Error: ' + e.message + '</p>';
            }
        }
    };

    window.aiImproveDescription = async function(targetInputId, contextOverrides) {
        var textarea = document.getElementById(targetInputId);
        if (!textarea) return;
        var currentValue = textarea.value.trim();

        var context = Object.assign({
            sku: selectedSku || '',
            brand: '',
            model: '',
            category: selectedCategory ? selectedCategory.name : ''
        }, contextOverrides || {});

        var brandEl = document.querySelector('.attr-input[data-attr-id="BRAND"]');
        if (brandEl && brandEl.value) context.brand = brandEl.tagName === 'SELECT' ? brandEl.options[brandEl.selectedIndex].text : brandEl.value;

        var container = textarea.parentElement;
        var existingSug = container.querySelector('.ai-desc-preview');
        if (existingSug) existingSug.remove();

        var previewDiv = document.createElement('div');
        previewDiv.className = 'ai-desc-preview mt-2 p-3 bg-purple-50 border border-purple-200 rounded-lg';
        previewDiv.innerHTML = '<div class="flex items-center justify-between mb-2"><span class="flex items-center gap-2 text-xs text-purple-600"><svg class="animate-spin h-3 w-3" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg> Generando descripcion...</span><button type="button" onclick="cancelAiRequest();this.closest(\'.ai-desc-preview\').remove()" class="text-[10px] text-gray-400 hover:text-red-500">Cancelar</button></div><pre class="ai-stream-output text-xs text-gray-700 whitespace-pre-wrap max-h-48 overflow-y-auto"></pre>';
        container.appendChild(previewDiv);

        var outputEl = previewDiv.querySelector('.ai-stream-output');

        cancelAiRequest();
        activeAiController = new AbortController();

        try {
            var resp = await fetch('/api/sku-inventory/ai-improve', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({field: 'description', current_value: currentValue, context: context}),
                signal: activeAiController.signal
            });

            if (!resp.ok) {
                try {
                    var errData = await resp.json();
                    previewDiv.innerHTML = '<p class="text-xs text-red-500">' + escapeHtml(errData.error || 'Error: ' + resp.status) + '</p>';
                } catch(e) {
                    previewDiv.innerHTML = '<p class="text-xs text-red-500">Error: ' + resp.status + '</p>';
                }
                return;
            }

            var reader = resp.body.getReader();
            var decoder = new TextDecoder();
            var fullText = '';

            while (true) {
                var result = await reader.read();
                if (result.done) break;
                var chunk = decoder.decode(result.value, {stream: true});
                var lines = chunk.split('\n');
                for (var i = 0; i < lines.length; i++) {
                    var line = lines[i];
                    if (line.startsWith('data: ')) {
                        var payload = line.substring(6);
                        if (payload === '[DONE]') break;
                        if (payload.startsWith('[ERROR]')) {
                            outputEl.innerHTML = '<p class="text-red-500">' + escapeHtml(payload.substring(8)) + '</p>';
                            return;
                        }
                        try {
                            var parsed = JSON.parse(payload);
                            if (parsed.token) fullText += parsed.token;
                        } catch(e) {
                            fullText += payload;
                        }
                    }
                }
                outputEl.textContent = fullText;
                outputEl.scrollTop = outputEl.scrollHeight;
            }

            // Add "Use" button
            var actionsHtml = '<div class="flex gap-2 mt-2">';
            actionsHtml += '<button type="button" class="px-3 py-1 bg-green-500 text-white text-xs font-medium rounded hover:bg-green-600" onclick="document.getElementById(\'' + targetInputId + '\').value=this.closest(\'.ai-desc-preview\').querySelector(\'.ai-stream-output\').textContent;this.closest(\'.ai-desc-preview\').remove()">Usar esta descripcion</button>';
            actionsHtml += '<button type="button" class="px-3 py-1 bg-gray-200 text-gray-600 text-xs rounded hover:bg-gray-300" onclick="this.closest(\'.ai-desc-preview\').remove()">Descartar</button>';
            actionsHtml += '</div>';
            previewDiv.insertAdjacentHTML('beforeend', actionsHtml);
            // Remove spinner
            var spinner = previewDiv.querySelector('.animate-spin');
            if (spinner) spinner.closest('span').innerHTML = '<span class="text-xs text-green-600 font-medium">Descripcion generada</span>';

        } catch (e) {
            if (e.name === 'AbortError') {
                previewDiv.remove();
            } else {
                previewDiv.innerHTML = '<p class="text-xs text-red-500">Error: ' + e.message + '</p>';
            }
        }
    };

    window.aiAutoFillAttributes = async function() {
        var context = {
            sku: selectedSku || '',
            brand: '',
            model: '',
            category: selectedCategory ? selectedCategory.name : ''
        };
        var brandEl = document.querySelector('.attr-input[data-attr-id="BRAND"]');
        if (brandEl && brandEl.value) context.brand = brandEl.tagName === 'SELECT' ? brandEl.options[brandEl.selectedIndex].text : brandEl.value;
        var modelEl = document.querySelector('.attr-input[data-attr-id="MODEL"]');
        if (modelEl && modelEl.value) context.model = modelEl.tagName === 'SELECT' ? modelEl.options[modelEl.selectedIndex].text : modelEl.value;

        // Collect empty attributes
        var emptyAttrs = [];
        document.querySelectorAll('.attr-input').forEach(function(el) {
            if (!el.value || el.value === '') {
                var attrId = el.getAttribute('data-attr-id');
                var wrapper = el.closest('[data-attr-wrapper]');
                var name = wrapper ? wrapper.querySelector('label').textContent.trim().replace(' *', '') : attrId;
                emptyAttrs.push({id: attrId, name: name});
            }
        });

        if (emptyAttrs.length === 0) {
            alert('Todos los atributos ya estan llenos');
            return;
        }

        // Show loading on button
        var btn = document.getElementById('btn-ai-autofill');
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<svg class="animate-spin h-3 w-3 inline mr-1" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>Procesando...';
        }

        try {
            var resp = await fetch('/api/sku-inventory/ai-improve', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    field: 'attributes',
                    current_value: JSON.stringify(emptyAttrs),
                    context: context
                })
            });

            var data = await resp.json();
            if (!resp.ok || data.error) {
                throw new Error(data.error || 'HTTP ' + resp.status);
            }

            var fullText = data.result || '';

            // Try to parse JSON from AI response
            var jsonMatch = fullText.match(/\[[\s\S]*\]/);
            if (jsonMatch) {
                var aiAttrs = JSON.parse(jsonMatch[0]);
                var filled = 0;
                aiAttrs.forEach(function(attr) {
                    if (attr.id && attr.value_name) {
                        var el = document.querySelector('.attr-input[data-attr-id="' + attr.id + '"]');
                        if (el && (!el.value || el.value === '')) {
                            if (el.tagName === 'SELECT') {
                                // Try to find matching option
                                var found = false;
                                for (var j = 0; j < el.options.length; j++) {
                                    if (el.options[j].text.toLowerCase() === attr.value_name.toLowerCase()) {
                                        el.value = el.options[j].value;
                                        found = true;
                                        break;
                                    }
                                }
                                if (!found) {
                                    for (var j = 0; j < el.options.length; j++) {
                                        if (el.options[j].text.toLowerCase().indexOf(attr.value_name.toLowerCase()) !== -1) {
                                            el.value = el.options[j].value;
                                            found = true;
                                            break;
                                        }
                                    }
                                }
                                if (found) {
                                    el.classList.add('bg-purple-50', 'border-purple-300');
                                    filled++;
                                }
                            } else {
                                el.value = attr.value_name;
                                el.classList.add('bg-purple-50', 'border-purple-300');
                                filled++;
                            }
                        }
                    }
                });
                if (btn) btn.innerHTML = 'IA Auto-fill (' + filled + ' completados)';
            }
        } catch (e) {
            console.error('AI autofill error:', e);
            alert('Error al auto-llenar: ' + e.message);
        }

        if (btn) {
            btn.disabled = false;
            setTimeout(function() {
                btn.innerHTML = '<svg class="w-3.5 h-3.5 inline mr-1" fill="currentColor" viewBox="0 0 20 20"><path d="M5 2a1 1 0 011 1v1h1a1 1 0 010 2H6v1a1 1 0 01-2 0V6H3a1 1 0 010-2h1V3a1 1 0 011-1zm0 10a1 1 0 011 1v1h1a1 1 0 110 2H6v1a1 1 0 11-2 0v-1H3a1 1 0 110-2h1v-1a1 1 0 011-1zM12 2a1 1 0 01.967.744L14.146 7.2 17.5 9.134a1 1 0 010 1.732l-3.354 1.935-1.18 4.455a1 1 0 01-1.933 0L9.854 12.8 6.5 10.866a1 1 0 010-1.732l3.354-1.935 1.18-4.455A1 1 0 0112 2z"/></svg>IA Auto-fill';
            }, 2000);
        }
    };

    // =========================================================
    // Category search
    // =========================================================

    window.searchCategory = async function() {
        var title = document.getElementById('item-title').value.trim();
        if (!title) {
            alert('Escribe un titulo para buscar categoria');
            return;
        }

        document.getElementById('categories-loading').classList.remove('hidden');
        document.getElementById('categories-list').innerHTML = '';

        try {
            var resp = await fetch('/api/sku-inventory/suggest-category?title=' + encodeURIComponent(title));
            var data = await resp.json();

            document.getElementById('categories-loading').classList.add('hidden');

            if (data.error) {
                document.getElementById('categories-list').innerHTML = '<p class="text-red-500 text-sm">' + data.error + '</p>';
                return;
            }

            _renderCategoryResults(data.categories || []);

        } catch (err) {
            document.getElementById('categories-loading').classList.add('hidden');
            document.getElementById('categories-list').innerHTML = '<p class="text-red-500 text-sm">Error: ' + err.message + '</p>';
        }
    };

    window.searchCategoryByKeyword = async function() {
        var keyword = document.getElementById('category-keyword').value.trim();
        if (!keyword) {
            alert('Escribe una palabra clave para buscar');
            return;
        }

        document.getElementById('categories-loading').classList.remove('hidden');
        document.getElementById('categories-list').innerHTML = '';

        try {
            var resp = await fetch('/api/sku-inventory/search-categories?q=' + encodeURIComponent(keyword));
            var data = await resp.json();

            document.getElementById('categories-loading').classList.add('hidden');

            if (data.error) {
                document.getElementById('categories-list').innerHTML = '<p class="text-red-500 text-sm">' + data.error + '</p>';
                return;
            }

            _renderCategoryResults(data.categories || []);

        } catch (err) {
            document.getElementById('categories-loading').classList.add('hidden');
            document.getElementById('categories-list').innerHTML = '<p class="text-red-500 text-sm">Error: ' + err.message + '</p>';
        }
    };

    function _renderCategoryResults(categories) {
        var html = '';
        categories.forEach(function(cat) {
            var isSelected = selectedCategory && selectedCategory.id === cat.id;
            var resultsInfo = cat.results ? ' (' + cat.results.toLocaleString() + ' productos)' : '';
            html += '<div class="border rounded-lg p-3 hover:bg-yellow-50 cursor-pointer transition ' + (isSelected ? 'bg-yellow-100 border-yellow-400' : '') + '" onclick="selectCategory(\'' + cat.id + '\', \'' + escapeHtml(cat.name) + '\')">';
            html += '<div class="flex items-center justify-between"><p class="font-medium text-gray-800">' + cat.name + '<span class="text-xs text-gray-400">' + resultsInfo + '</span></p><span class="text-xs font-mono bg-gray-100 text-gray-600 px-2 py-0.5 rounded">' + cat.id + '</span></div>';
            if (cat.path) html += '<p class="text-xs text-gray-500 mt-1">' + cat.path + '</p>';
            html += '</div>';
        });

        if (!html) {
            html = '<p class="text-gray-500 text-sm">No se encontraron categorias. Intenta con otra palabra clave o ingresa el ID manualmente.</p>';
        }

        document.getElementById('categories-list').innerHTML = html;
    }

    window.selectCategory = function(catId, catName) {
        selectedCategory = {id: catId, name: catName};

        document.querySelectorAll('#categories-list > div').forEach(function(el) {
            el.classList.remove('bg-yellow-100', 'border-yellow-400');
        });
        if (event && event.currentTarget) {
            event.currentTarget.classList.add('bg-yellow-100', 'border-yellow-400');
        }
    };

    window.useManualCategory = function() {
        var catId = document.getElementById('manual-category-id').value.trim();
        if (!catId) {
            alert('Ingresa un ID de categoria');
            return;
        }
        var catName = document.getElementById('manual-category-name').value.trim() || catId;
        selectedCategory = {id: catId, name: catName};

        document.getElementById('categories-list').innerHTML =
            '<div class="border rounded-lg p-3 bg-yellow-100 border-yellow-400">' +
            '<div class="flex items-center justify-between"><p class="font-medium text-gray-800">' + catName + '</p><span class="text-xs font-mono bg-gray-100 text-gray-600 px-2 py-0.5 rounded">' + catId + '</span></div>' +
            '</div>';
    };

    // =========================================================
    // Step navigation
    // =========================================================

    window.nextStep = async function() {
        if (currentStep === 1) {
            if (!selectedCategory) {
                alert('Selecciona una categoria');
                return;
            }
            await loadAttributes();
            currentStep = 2;
        } else if (currentStep === 2) {
            currentStep = 3;
        } else if (currentStep === 3) {
            await buildReviewAndValidate();
            currentStep = 4;
        }
        showStep(currentStep);
    };

    window.prevStep = function() {
        if (currentStep > 1) {
            currentStep--;
            showStep(currentStep);
        }
    };

    function showStep(step) {
        document.querySelectorAll('.step-content').forEach(function(el) {
            el.classList.add('hidden');
        });
        document.getElementById('step-' + step).classList.remove('hidden');

        document.querySelectorAll('.step-indicator').forEach(function(el) {
            var s = parseInt(el.getAttribute('data-step'));
            var circle = el.querySelector('span:first-child');
            var label = el.querySelector('span:last-child');

            if (s <= step) {
                circle.classList.remove('bg-gray-300', 'text-gray-600');
                circle.classList.add('bg-yellow-400', 'text-gray-800');
                label.classList.remove('text-gray-500');
                label.classList.add('text-gray-800', 'font-medium');
            } else {
                circle.classList.add('bg-gray-300', 'text-gray-600');
                circle.classList.remove('bg-yellow-400', 'text-gray-800');
                label.classList.add('text-gray-500');
                label.classList.remove('text-gray-800', 'font-medium');
            }
        });

        var isResearchStep = step === 0;
        document.getElementById('btn-prev-step').classList.toggle('hidden', step <= 1);
        document.getElementById('btn-next-step').classList.toggle('hidden', step === 4 || isResearchStep);
        document.getElementById('btn-publish').classList.toggle('hidden', step !== 4);
    }

    // =========================================================
    // Attributes
    // =========================================================

    async function loadAttributes() {
        document.getElementById('attributes-loading').classList.remove('hidden');
        document.getElementById('required-attributes').innerHTML = '';
        document.getElementById('recommended-attributes').innerHTML = '';
        var optionalContainer = document.getElementById('optional-attributes');
        if (optionalContainer) optionalContainer.innerHTML = '';

        try {
            var resp = await fetch('/api/sku-inventory/category-attributes/' + selectedCategory.id);
            var data = await resp.json();

            document.getElementById('attributes-loading').classList.add('hidden');
            categoryAttributes = data;

            // Render required
            var reqHtml = '';
            data.required.forEach(function(attr) {
                reqHtml += buildAttributeInput(attr, true);
            });
            document.getElementById('required-attributes').innerHTML = reqHtml || '<p class="text-gray-500 text-sm col-span-2">Sin atributos obligatorios</p>';

            // Render recommended
            var recHtml = '';
            data.recommended.forEach(function(attr) {
                recHtml += buildAttributeInput(attr, false);
            });
            document.getElementById('recommended-attributes').innerHTML = recHtml || '<p class="text-gray-500 text-sm col-span-2">Sin atributos recomendados</p>';

            // Render optional (collapsible by group)
            if (optionalContainer && data.optional && data.optional.length > 0) {
                var groups = {};
                data.optional.forEach(function(attr) {
                    var group = attr.group_name || 'Otros';
                    if (!groups[group]) groups[group] = [];
                    groups[group].push(attr);
                });

                var optHtml = '';
                var totalOpt = data.optional.length;
                var allCountEl = document.getElementById('optional-count');
                if (allCountEl) allCountEl.textContent = totalOpt;

                Object.keys(groups).forEach(function(groupName) {
                    optHtml += '<details class="mb-3">';
                    optHtml += '<summary class="cursor-pointer text-xs font-medium text-gray-500 hover:text-gray-700">' + groupName + ' (' + groups[groupName].length + ')</summary>';
                    optHtml += '<div class="grid grid-cols-1 md:grid-cols-2 gap-3 mt-2 pl-2">';
                    groups[groupName].forEach(function(attr) {
                        optHtml += buildAttributeInput(attr, false);
                    });
                    optHtml += '</div></details>';
                });
                optionalContainer.innerHTML = optHtml;
            }

            // Auto-fill attributes from research
            if (researchData && researchData.attributes) {
                prefillAttributes(researchData.attributes);
            }

        } catch (err) {
            document.getElementById('attributes-loading').classList.add('hidden');
            alert('Error cargando atributos: ' + err.message);
        }
    }

    function buildAttributeInput(attr, required) {
        var html = '<div class="' + (required ? 'required-attr' : 'recommended-attr') + '" data-attr-wrapper="' + attr.id + '">';
        html += '<label class="block text-sm font-medium text-gray-700 mb-1">' + attr.name;
        if (required) html += ' <span class="text-red-500">*</span>';
        html += '</label>';

        if (attr.values && attr.values.length > 0) {
            html += '<select data-attr-id="' + attr.id + '" class="attr-input w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:border-yellow-400">';
            html += '<option value="">Seleccionar...</option>';
            attr.values.forEach(function(v) {
                html += '<option value="' + v.id + '">' + v.name + '</option>';
            });
            if (attr.allow_custom) {
                html += '<option value="__custom__">Otro (escribir)...</option>';
            }
            html += '</select>';
        } else {
            html += '<input type="text" data-attr-id="' + attr.id + '" class="attr-input w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:border-yellow-400" placeholder="Escribir valor...">';
        }

        html += '</div>';
        return html;
    }

    var pendingSuggestions = [];

    function prefillAttributes(researchAttrs) {
        pendingSuggestions = [];

        researchAttrs.forEach(function(ra) {
            var input = document.querySelector('.attr-input[data-attr-id="' + ra.id + '"]');
            if (!input) return;

            var matchedValue = null;
            var matchedText = ra.value_name || '';

            if (input.tagName === 'SELECT') {
                if (ra.value_id) {
                    for (var i = 0; i < input.options.length; i++) {
                        if (input.options[i].value === String(ra.value_id)) {
                            matchedValue = input.options[i].value;
                            matchedText = input.options[i].text;
                            break;
                        }
                    }
                }
                if (!matchedValue && ra.value_name) {
                    var lowerName = ra.value_name.toLowerCase();
                    for (var i = 0; i < input.options.length; i++) {
                        if (input.options[i].text.toLowerCase() === lowerName) {
                            matchedValue = input.options[i].value;
                            matchedText = input.options[i].text;
                            break;
                        }
                    }
                    if (!matchedValue) {
                        for (var i = 0; i < input.options.length; i++) {
                            if (input.options[i].text.toLowerCase().indexOf(lowerName) !== -1 ||
                                lowerName.indexOf(input.options[i].text.toLowerCase()) !== -1) {
                                matchedValue = input.options[i].value;
                                matchedText = input.options[i].text;
                                break;
                            }
                        }
                    }
                }
                if (!matchedValue && ra.value_name) {
                    matchedValue = '__text__';
                    matchedText = ra.value_name;
                }
            } else {
                if (ra.value_name) {
                    matchedValue = '__text__';
                    matchedText = ra.value_name;
                }
            }

            if (matchedValue) {
                pendingSuggestions.push({
                    attrId: ra.id,
                    matchedValue: matchedValue,
                    matchedText: matchedText,
                    inputEl: input
                });

                var wrapper = document.querySelector('[data-attr-wrapper="' + ra.id + '"]');
                if (wrapper) {
                    var old = wrapper.querySelector('.suggestion-chip');
                    if (old) old.remove();

                    var chipHtml = '<div class="suggestion-chip mt-1 flex items-center gap-1">' +
                        '<span class="text-xs text-blue-600 bg-blue-50 border border-blue-200 rounded px-2 py-0.5 truncate max-w-[200px]" title="' + matchedText + '">Sugerencia: ' + matchedText + '</span>' +
                        '<button type="button" onclick="acceptSuggestion(\'' + ra.id + '\')" class="text-xs bg-green-500 text-white rounded px-1.5 py-0.5 hover:bg-green-600" title="Aceptar">&#10003;</button>' +
                        '<button type="button" onclick="dismissSuggestion(\'' + ra.id + '\')" class="text-xs bg-gray-300 text-gray-600 rounded px-1.5 py-0.5 hover:bg-gray-400" title="Descartar">&#10005;</button>' +
                        '</div>';
                    wrapper.insertAdjacentHTML('beforeend', chipHtml);
                }
            }
        });

        if (pendingSuggestions.length > 0) {
            document.getElementById('suggestions-count').textContent = pendingSuggestions.length;
            document.getElementById('suggestions-banner').classList.remove('hidden');
        }
    }

    window.acceptSuggestion = function(attrId) {
        var idx = pendingSuggestions.findIndex(function(s) { return s.attrId === attrId; });
        if (idx === -1) return;

        var sug = pendingSuggestions[idx];
        if (sug.matchedValue === '__text__') {
            sug.inputEl.value = sug.matchedText;
        } else {
            sug.inputEl.value = sug.matchedValue;
        }
        sug.inputEl.classList.add('bg-green-50', 'border-green-300');

        var wrapper = document.querySelector('[data-attr-wrapper="' + attrId + '"]');
        if (wrapper) {
            var chip = wrapper.querySelector('.suggestion-chip');
            if (chip) chip.remove();
        }

        pendingSuggestions.splice(idx, 1);
        _updateSuggestionsBanner();
    };

    window.dismissSuggestion = function(attrId) {
        var idx = pendingSuggestions.findIndex(function(s) { return s.attrId === attrId; });
        if (idx === -1) return;

        var wrapper = document.querySelector('[data-attr-wrapper="' + attrId + '"]');
        if (wrapper) {
            var chip = wrapper.querySelector('.suggestion-chip');
            if (chip) chip.remove();
        }

        pendingSuggestions.splice(idx, 1);
        _updateSuggestionsBanner();
    };

    window.acceptAllSuggestions = function() {
        var copy = pendingSuggestions.slice();
        copy.forEach(function(s) {
            acceptSuggestion(s.attrId);
        });
    };

    function _updateSuggestionsBanner() {
        if (pendingSuggestions.length > 0) {
            document.getElementById('suggestions-count').textContent = pendingSuggestions.length;
            document.getElementById('suggestions-banner').classList.remove('hidden');
        } else {
            document.getElementById('suggestions-banner').classList.add('hidden');
        }
    }

    // =========================================================
    // Step 4: Review, quality score, validation
    // =========================================================

    async function buildReviewAndValidate() {
        var title = document.getElementById('item-title').value;
        var price = document.getElementById('item-price').value;
        var qty = document.getElementById('item-quantity').value;
        var listingType = document.getElementById('item-listing-type').value;
        var condition = document.getElementById('item-condition').value;
        var description = document.getElementById('item-description').value;
        var picturesText = document.getElementById('item-pictures').value.trim();

        // Compliance values
        var warrantyType = document.getElementById('warranty-type');
        var warrantyDuration = document.getElementById('warranty-duration');
        var freeShipping = document.getElementById('free-shipping');
        var gtinInput = document.getElementById('gtin-value');

        // Build summary
        var html = '<p><strong>SKU:</strong> ' + selectedSku + '</p>';
        html += '<p><strong>Titulo:</strong> ' + title + ' <span class="text-xs text-gray-400">(' + title.length + ' chars)</span></p>';
        html += '<p><strong>Categoria:</strong> ' + selectedCategory.name + ' (' + selectedCategory.id + ')</p>';
        html += '<p><strong>Precio:</strong> $' + parseFloat(price || 0).toLocaleString('es-MX') + ' MXN</p>';
        html += '<p><strong>Cantidad:</strong> ' + qty + ' unidades</p>';
        html += '<p><strong>Tipo:</strong> ' + listingType + '</p>';
        html += '<p><strong>Condicion:</strong> ' + (condition === 'new' ? 'Nuevo' : 'Usado') + '</p>';

        if (warrantyType && warrantyType.value !== 'none') {
            html += '<p><strong>Garantia:</strong> ' + warrantyType.options[warrantyType.selectedIndex].text + ' - ' + (warrantyDuration ? warrantyDuration.value : '12') + ' meses</p>';
        }
        if (freeShipping && freeShipping.checked) {
            html += '<p><strong>Envio:</strong> Gratis</p>';
        }
        if (gtinInput && gtinInput.value) {
            html += '<p><strong>GTIN/UPC:</strong> ' + gtinInput.value + '</p>';
        }

        var picCount = picturesText ? picturesText.split('\n').filter(function(l) { return l.trim(); }).length : 0;
        html += '<p><strong>Imagenes:</strong> ' + picCount + '</p>';

        var attrCount = 0;
        document.querySelectorAll('.attr-input').forEach(function(el) {
            if (el.value && el.value !== '' && el.value !== '__custom__') attrCount++;
        });
        html += '<p><strong>Atributos completados:</strong> ' + attrCount + '</p>';

        document.getElementById('review-summary').innerHTML = html;

        // Quality score with compliance checks
        var hasGtin = gtinInput && gtinInput.value.trim().length >= 8;
        var hasWarranty = warrantyType && warrantyType.value !== 'none';
        var hasFreeShipping = freeShipping && freeShipping.checked;

        calculateQualityScore({
            title: title,
            price: parseFloat(price) || 0,
            description: description,
            picturesCount: picCount,
            attributesCount: attrCount,
            requiredCount: categoryAttributes ? categoryAttributes.required.length : 0,
            hasGtin: hasGtin,
            hasWarranty: hasWarranty,
            hasFreeShipping: hasFreeShipping,
        });

        await validateWithMeli();
    }

    function calculateQualityScore(data) {
        var score = 0;
        var checks = [];

        // Title: 15 points
        if (data.title && data.title.length >= 40) {
            score += 15;
            checks.push({ok: true, text: 'Titulo descriptivo (' + data.title.length + ' chars)'});
        } else if (data.title && data.title.length >= 20) {
            score += 10;
            checks.push({ok: false, text: 'Titulo corto (' + data.title.length + ') - recomendado 40+'});
        } else {
            checks.push({ok: false, text: 'Titulo muy corto o vacio'});
        }

        // Price: 10 points
        if (data.price > 0) {
            score += 10;
            checks.push({ok: true, text: 'Precio definido: $' + data.price.toLocaleString('es-MX')});
        } else {
            checks.push({ok: false, text: 'Sin precio definido'});
        }

        // Pictures: 20 points
        if (data.picturesCount >= 6) {
            score += 20;
            checks.push({ok: true, text: data.picturesCount + ' imagenes (excelente)'});
        } else if (data.picturesCount >= 3) {
            score += 12;
            checks.push({ok: false, text: data.picturesCount + ' imagenes - recomendado 6+'});
        } else if (data.picturesCount >= 1) {
            score += 5;
            checks.push({ok: false, text: 'Solo ' + data.picturesCount + ' imagen(es) - sube mas'});
        } else {
            checks.push({ok: false, text: 'Sin imagenes'});
        }

        // Description: 10 points
        if (data.description && data.description.length >= 100) {
            score += 10;
            checks.push({ok: true, text: 'Descripcion completa'});
        } else if (data.description && data.description.length > 0) {
            score += 5;
            checks.push({ok: false, text: 'Descripcion corta - agrega mas detalle'});
        } else {
            checks.push({ok: false, text: 'Sin descripcion'});
        }

        // Attributes: 15 points
        var reqTotal = data.requiredCount || 1;
        var reqFilled = data.attributesCount;
        if (reqFilled >= reqTotal && reqTotal > 0) {
            score += 15;
            checks.push({ok: true, text: 'Atributos completados (' + reqFilled + ')'});
        } else if (reqFilled > 0) {
            score += Math.round(15 * reqFilled / Math.max(reqTotal, 1));
            checks.push({ok: false, text: 'Faltan atributos: ' + reqFilled + ' completados'});
        } else {
            checks.push({ok: false, text: 'Sin atributos completados'});
        }

        // GTIN: 10 points
        if (data.hasGtin) {
            score += 10;
            checks.push({ok: true, text: 'GTIN/UPC presente'});
        } else {
            checks.push({ok: false, text: 'Sin GTIN/UPC - mejora posicionamiento'});
        }

        // Warranty: 10 points
        if (data.hasWarranty) {
            score += 10;
            checks.push({ok: true, text: 'Garantia configurada'});
        } else {
            checks.push({ok: false, text: 'Sin garantia - agrega una'});
        }

        // Free shipping: 10 points
        if (data.hasFreeShipping) {
            score += 10;
            checks.push({ok: true, text: 'Envio gratis activado'});
        } else {
            checks.push({ok: false, text: 'Sin envio gratis - casi obligatorio en MeLi'});
        }

        // Render quality score
        score = Math.min(100, score);
        var bar = document.getElementById('quality-score-bar');
        var badge = document.getElementById('quality-score-badge');

        bar.style.width = score + '%';
        badge.textContent = score + '%';

        if (score >= 75) {
            bar.className = 'h-3 rounded-full transition-all duration-500 bg-green-500';
            badge.className = 'px-3 py-1 rounded-full text-sm font-bold bg-green-100 text-green-800';
        } else if (score >= 50) {
            bar.className = 'h-3 rounded-full transition-all duration-500 bg-yellow-400';
            badge.className = 'px-3 py-1 rounded-full text-sm font-bold bg-yellow-100 text-yellow-800';
        } else {
            bar.className = 'h-3 rounded-full transition-all duration-500 bg-red-400';
            badge.className = 'px-3 py-1 rounded-full text-sm font-bold bg-red-100 text-red-800';
        }

        var checkHtml = '';
        checks.forEach(function(c) {
            if (c.ok) {
                checkHtml += '<div class="flex items-center gap-2 text-green-700"><svg class="w-4 h-4 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/></svg><span>' + c.text + '</span></div>';
            } else {
                checkHtml += '<div class="flex items-center gap-2 text-yellow-700"><svg class="w-4 h-4 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/></svg><span>' + c.text + '</span></div>';
            }
        });
        document.getElementById('quality-checklist').innerHTML = checkHtml;
    }

    async function validateWithMeli() {
        var title = document.getElementById('item-title').value.trim();
        var price = parseFloat(document.getElementById('item-price').value);
        var qty = parseInt(document.getElementById('item-quantity').value);
        var listingType = document.getElementById('item-listing-type').value;
        var condition = document.getElementById('item-condition').value;
        var picturesText = document.getElementById('item-pictures').value.trim();

        if (!title || !price || !qty || !picturesText) {
            validationPassed = false;
            return;
        }

        var pictures = [];
        picturesText.split('\n').forEach(function(url) {
            url = url.trim();
            if (url) pictures.push({source: url});
        });

        var attributes = collectAttributes();

        var payload = {
            title: title,
            category_id: selectedCategory.id,
            price: price,
            available_quantity: qty,
            listing_type_id: listingType,
            condition: condition,
            pictures: pictures,
            attributes: attributes,
            site_id: 'MLM',
            currency_id: 'MXN',
            buying_mode: 'buy_it_now'
        };

        // Add compliance fields
        addComplianceToPayload(payload);

        if (researchData && researchData.family_name) {
            payload.family_name = researchData.family_name;
        }

        document.getElementById('validation-loading').classList.remove('hidden');
        document.getElementById('validation-result').innerHTML = '';

        try {
            var resp = await fetch('/api/sku-inventory/validate-item', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            var data = await resp.json();

            document.getElementById('validation-loading').classList.add('hidden');

            var causes = data.cause || data.causes || [];
            var errorMsg = data.error || data.message || '';

            if (causes.length > 0) {
                validationPassed = false;
                var errHtml = '<div class="bg-red-50 border border-red-200 rounded-lg p-4 text-sm">';
                errHtml += '<p class="font-semibold text-red-700 mb-2">Errores de validacion:</p>';
                causes.forEach(function(c) {
                    var msg = (typeof c === 'string') ? c : (c.message || c.code || JSON.stringify(c));
                    errHtml += '<p class="text-red-600">- ' + msg + '</p>';
                });
                if (errorMsg) errHtml += '<p class="text-red-500 text-xs mt-1">' + errorMsg + '</p>';
                errHtml += '</div>';
                document.getElementById('validation-result').innerHTML = errHtml;
            } else if (errorMsg) {
                validationPassed = false;
                document.getElementById('validation-result').innerHTML =
                    '<div class="bg-red-50 border border-red-200 rounded-lg p-4 text-sm">' +
                    '<p class="font-semibold text-red-700 mb-1">Validacion fallida</p>' +
                    '<p class="text-red-600">' + errorMsg + '</p></div>';
            } else if (data.status && data.status >= 400) {
                validationPassed = false;
                document.getElementById('validation-result').innerHTML =
                    '<div class="bg-red-50 border border-red-200 rounded-lg p-4 text-sm">' +
                    '<p class="font-semibold text-red-700">Validacion fallida (status ' + data.status + ')</p></div>';
            } else {
                validationPassed = true;
                document.getElementById('validation-result').innerHTML =
                    '<div class="bg-green-50 border border-green-200 rounded-lg p-4 text-sm">' +
                    '<p class="font-semibold text-green-700">Validacion exitosa - listo para publicar</p></div>';
            }
        } catch (err) {
            document.getElementById('validation-loading').classList.add('hidden');
            validationPassed = true;
            document.getElementById('validation-result').innerHTML =
                '<div class="bg-yellow-50 border border-yellow-200 rounded-lg p-4 text-sm">' +
                '<p class="text-yellow-700">No se pudo validar - puedes intentar publicar de todos modos</p></div>';
        }
    }

    function collectAttributes() {
        var attributes = [];
        document.querySelectorAll('.attr-input').forEach(function(el) {
            var attrId = el.getAttribute('data-attr-id');
            var value = el.value;
            if (value && value !== '' && value !== '__custom__') {
                if (el.tagName === 'SELECT') {
                    attributes.push({id: attrId, value_id: value});
                } else {
                    attributes.push({id: attrId, value_name: value});
                }
            }
        });
        attributes.push({id: 'SELLER_SKU', value_name: selectedSku});

        // Add GTIN from compliance section
        var gtinInput = document.getElementById('gtin-value');
        if (gtinInput && gtinInput.value.trim()) {
            var existingGtin = attributes.find(function(a) { return a.id === 'GTIN'; });
            if (!existingGtin) {
                attributes.push({id: 'GTIN', value_name: gtinInput.value.trim()});
            }
        }

        return attributes;
    }

    function addComplianceToPayload(payload) {
        // Warranty
        var warrantyType = document.getElementById('warranty-type');
        var warrantyDuration = document.getElementById('warranty-duration');
        if (warrantyType && warrantyType.value !== 'none') {
            var typeMap = {manufacturer: 'Garantia de fabrica', extended: 'Garantia extendida'};
            payload.warranty = typeMap[warrantyType.value] || 'Garantia del vendedor';
            payload.warranty += ': ' + (warrantyDuration ? warrantyDuration.value : '12') + ' meses';

            if (!payload.sale_terms) payload.sale_terms = [];
            payload.sale_terms.push({
                id: 'WARRANTY_TYPE',
                value_name: warrantyType.value === 'manufacturer' ? 'Garantia del vendedor' : 'Garantia de fabrica'
            });
            payload.sale_terms.push({
                id: 'WARRANTY_TIME',
                value_name: (warrantyDuration ? warrantyDuration.value : '12') + ' meses'
            });
        }

        // Shipping
        var shippingMode = document.getElementById('shipping-mode');
        var freeShipping = document.getElementById('free-shipping');
        if (shippingMode || freeShipping) {
            payload.shipping = {
                mode: shippingMode ? shippingMode.value : 'me2',
                free_shipping: freeShipping ? freeShipping.checked : true,
            };
        }
    }

    // =========================================================
    // Publish
    // =========================================================

    window.publishItem = async function() {
        var title = document.getElementById('item-title').value.trim();
        var price = parseFloat(document.getElementById('item-price').value);
        var qty = parseInt(document.getElementById('item-quantity').value);
        var listingType = document.getElementById('item-listing-type').value;
        var condition = document.getElementById('item-condition').value;
        var description = document.getElementById('item-description').value.trim();
        var picturesText = document.getElementById('item-pictures').value.trim();

        if (!title || !price || !qty) {
            alert('Completa titulo, precio y cantidad');
            return;
        }

        var pictures = [];
        if (picturesText) {
            picturesText.split('\n').forEach(function(url) {
                url = url.trim();
                if (url) pictures.push({source: url});
            });
        }

        if (pictures.length === 0) {
            alert('Agrega al menos una imagen');
            return;
        }

        var attributes = collectAttributes();

        var payload = {
            title: title,
            category_id: selectedCategory.id,
            price: price,
            available_quantity: qty,
            listing_type_id: listingType,
            condition: condition,
            pictures: pictures,
            attributes: attributes,
            seller_custom_field: selectedSku
        };

        addComplianceToPayload(payload);

        if (researchData && researchData.family_name) {
            payload.family_name = researchData.family_name;
        }

        if (description) {
            payload.description = {plain_text: description};
        }

        document.getElementById('btn-publish').disabled = true;
        document.getElementById('btn-publish').textContent = 'Publicando...';

        try {
            var resp = await fetch('/api/sku-inventory/create-item', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            var data = await resp.json();

            if (data.error) {
                document.getElementById('validation-result').innerHTML = '<div class="bg-red-50 border border-red-200 rounded-lg p-4 text-red-700 text-sm"><strong>Error:</strong> ' + data.error + '</div>';
                document.getElementById('btn-publish').disabled = false;
                document.getElementById('btn-publish').textContent = 'Publicar en MeLi';
                return;
            }

            if (data.id) {
                document.getElementById('validation-result').innerHTML = '<div class="bg-green-50 border border-green-200 rounded-lg p-4 text-green-700 text-sm"><strong>Publicado exitosamente!</strong><br>Item ID: <a href="' + data.permalink + '" target="_blank" class="underline">' + data.id + '</a></div>';
                document.getElementById('btn-publish').classList.add('hidden');

                var idx = allResults.findIndex(function(r) { return r.sku === selectedSku; });
                if (idx !== -1) {
                    allResults[idx].meli_status = 'active';
                    allResults[idx].item_id = data.id;
                    allResults[idx].item_title = data.title;
                    allResults[idx].permalink = data.permalink;
                    renderTable();
                }
            }

        } catch (err) {
            document.getElementById('validation-result').innerHTML = '<div class="bg-red-50 border border-red-200 rounded-lg p-4 text-red-700 text-sm"><strong>Error:</strong> ' + err.message + '</div>';
            document.getElementById('btn-publish').disabled = false;
            document.getElementById('btn-publish').textContent = 'Publicar en MeLi';
        }
    };

    // =========================================================
    // Optimize modal
    // =========================================================

    var optimizeItemId = null;
    var optimizeItemData = null;

    window.openOptimizeModal = async function(itemId, sku) {
        optimizeItemId = itemId;
        optimizeItemData = null;

        document.getElementById('opt-item-id').textContent = 'Item: ' + itemId + ' | SKU: ' + sku;
        document.getElementById('opt-loading').classList.remove('hidden');
        document.getElementById('opt-loading').innerHTML = '<div class="text-center"><svg class="animate-spin h-10 w-10 mx-auto mb-3 text-yellow-400" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg><p class="text-gray-500 text-sm">Cargando detalles e investigando producto...</p></div>';
        document.getElementById('opt-content').classList.add('hidden');
        document.getElementById('opt-footer').classList.add('hidden');
        document.getElementById('opt-result').innerHTML = '';
        document.getElementById('optimize-modal').classList.remove('hidden');

        // Toggle AI buttons in optimize modal
        document.querySelectorAll('.ai-btn-opt').forEach(function(btn) {
            btn.classList.toggle('hidden', !aiAvailable);
        });

        try {
            var detailsPromise = fetch('/api/sku-inventory/item-details/' + itemId).then(function(r) { return r.json(); });
            var researchPromise = fetch('/api/sku-inventory/research', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({sku: sku})
            }).then(function(r) { return r.json(); }).catch(function() { return {}; });

            var results = await Promise.all([detailsPromise, researchPromise]);
            var data = results[0];
            var research = results[1];

            if (data.error) {
                document.getElementById('opt-loading').innerHTML = '<p class="text-red-500 text-center py-8">' + data.error + '</p>';
                return;
            }

            optimizeItemData = data;
            var item = data.item;

            // Score
            var score = data.score || 0;
            var scoreBadge = document.getElementById('opt-score-badge');
            scoreBadge.textContent = score + '%';
            if (score >= 75) scoreBadge.className = 'text-2xl font-bold text-green-600';
            else if (score >= 50) scoreBadge.className = 'text-2xl font-bold text-yellow-600';
            else scoreBadge.className = 'text-2xl font-bold text-red-600';

            // Tips
            var tipsHtml = '';
            (data.tips || []).forEach(function(t) {
                tipsHtml += '<div class="flex items-center gap-2 text-xs">' +
                    '<svg class="w-3.5 h-3.5 text-yellow-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/></svg>' +
                    '<span class="text-gray-600">' + t.msg + '</span></div>';
            });
            if (!tipsHtml) tipsHtml = '<p class="text-green-600 text-xs font-semibold">Publicacion optimizada!</p>';
            document.getElementById('opt-tips').innerHTML = tipsHtml;

            // Detect catalog item (family_name blocks title edits)
            var isCatalog = !!(item.family_name || item.catalog_product_id);
            var titleInput = document.getElementById('opt-title');
            var currentTitle = item.title || '';
            titleInput.value = currentTitle;
            document.getElementById('opt-title-len').textContent = '(' + currentTitle.length + '/60)';
            if (isCatalog) {
                titleInput.disabled = true;
                titleInput.classList.add('bg-gray-100', 'text-gray-500');
                titleInput.title = 'Titulo bloqueado por MeLi (item de catalogo)';
                var lockNotice = document.getElementById('opt-catalog-notice');
                if (!lockNotice) {
                    titleInput.parentElement.insertAdjacentHTML('afterbegin',
                        '<div id="opt-catalog-notice" class="mb-2 px-3 py-2 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-700 flex items-center gap-2">' +
                        '<svg class="w-4 h-4 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd"/></svg>' +
                        '<span>Item de catalogo: el titulo no se puede modificar desde la API. Puedes editar descripcion, fotos y atributos.</span>' +
                        '</div>');
                }
            } else {
                titleInput.disabled = false;
                titleInput.classList.remove('bg-gray-100', 'text-gray-500');
                titleInput.title = '';
                var lockNotice = document.getElementById('opt-catalog-notice');
                if (lockNotice) lockNotice.remove();
            }

            var titleSugEl = document.getElementById('opt-title-suggestion');
            if (titleSugEl) titleSugEl.remove();
            if (research.title && research.title !== currentTitle && research.title.length >= 10) {
                var titleParent = document.getElementById('opt-title').parentElement;
                titleParent.insertAdjacentHTML('beforeend',
                    '<div id="opt-title-suggestion" class="mt-1 flex items-center gap-1">' +
                    '<span class="text-xs text-blue-600 bg-blue-50 border border-blue-200 rounded px-2 py-0.5 truncate max-w-[400px]" title="' + research.title + '">Sugerencia: ' + research.title + '</span>' +
                    '<button type="button" onclick="document.getElementById(\'opt-title\').value=\'' + research.title.replace(/'/g, "\\'") + '\';document.getElementById(\'opt-title-len\').textContent=\'(' + research.title.length + '/60)\';this.parentElement.remove()" class="text-xs bg-green-500 text-white rounded px-1.5 py-0.5 hover:bg-green-600">Usar</button>' +
                    '</div>');
            }

            // Description
            var currentDesc = data.description || '';
            document.getElementById('opt-description').value = currentDesc;

            var descSugEl = document.getElementById('opt-desc-suggestion');
            if (descSugEl) descSugEl.remove();
            if (research.description && research.description.length > 50) {
                var descParent = document.getElementById('opt-description').parentElement;
                descParent.insertAdjacentHTML('beforeend',
                    '<div id="opt-desc-suggestion" class="mt-1">' +
                    '<div class="text-xs text-blue-600 mb-1">Sugerencia de descripcion encontrada (' + research.description.length + ' chars)</div>' +
                    '<button type="button" onclick="document.getElementById(\'opt-description\').value=optimizeResearchData.description;this.parentElement.remove()" class="text-xs bg-green-500 text-white rounded px-2 py-0.5 hover:bg-green-600">Usar descripcion sugerida</button>' +
                    '</div>');
            }

            window.optimizeResearchData = research;

            // Pictures
            var pics = item.pictures || [];
            document.getElementById('opt-pics-count').textContent = '(' + pics.length + ' actuales)';
            var picsHtml = '';
            pics.forEach(function(p) {
                var url = p.secure_url || p.url || '';
                if (url) {
                    picsHtml += '<img src="' + url + '" class="w-16 h-16 object-cover rounded border" onerror="this.style.display=\'none\'">';
                }
            });
            document.getElementById('opt-current-pics').innerHTML = picsHtml || '<span class="text-gray-400 text-xs">Sin fotos</span>';
            document.getElementById('opt-new-pics').value = '';

            // Suggested pictures
            var sugPicsEl = document.getElementById('opt-suggested-pics');
            if (sugPicsEl) sugPicsEl.remove();
            if (research.pictures && research.pictures.length > 0) {
                var sugPicsHtml = '<div id="opt-suggested-pics" class="mt-2"><p class="text-xs text-blue-600 mb-1">Fotos encontradas (click para agregar):</p><div class="flex flex-wrap gap-2">';
                research.pictures.forEach(function(pic) {
                    sugPicsHtml += '<div class="relative cursor-pointer" onclick="addOptPic(\'' + pic.url + '\', this)">' +
                        '<img src="' + pic.url + '" class="w-16 h-16 object-cover rounded border border-blue-300 hover:opacity-75" onerror="this.parentElement.style.display=\'none\'">' +
                        '<span class="absolute bottom-0 left-0 right-0 text-[8px] bg-blue-600 text-white text-center">' + (pic.source || 'web') + '</span>' +
                        '</div>';
                });
                sugPicsHtml += '</div></div>';
                document.getElementById('opt-new-pics').parentElement.insertAdjacentHTML('beforeend', sugPicsHtml);
            }

            // Missing attributes
            var attrs = item.attributes || [];
            var filledIds = {};
            attrs.forEach(function(a) {
                if (a.value_name) filledIds[a.id] = true;
            });

            var researchAttrMap = {};
            if (research.attributes) {
                research.attributes.forEach(function(ra) {
                    researchAttrMap[ra.id] = ra;
                });
            }

            var currentAttrValues = {};
            attrs.forEach(function(a) {
                if (a.value_name) currentAttrValues[a.id] = a.value_name;
            });

            var catId = item.category_id;
            if (catId) {
                try {
                    var catResp = await fetch('/api/sku-inventory/category-attributes/' + catId);
                    var catData = await catResp.json();

                    var missing = [];
                    var improvable = [];
                    var allCatAttrs = (catData.required || []).concat(catData.recommended || []);
                    allCatAttrs.forEach(function(ca) {
                        var suggestion = researchAttrMap[ca.id];
                        if (!filledIds[ca.id]) {
                            missing.push(ca);
                        } else if (suggestion && suggestion.value_name) {
                            var current = (currentAttrValues[ca.id] || '').toLowerCase().trim();
                            var suggested = (suggestion.value_name || '').toLowerCase().trim();
                            if (current && suggested && current !== suggested) {
                                ca._current_value = currentAttrValues[ca.id];
                                improvable.push(ca);
                            }
                        }
                    });

                    var attrsHtml = '';

                    if (missing.length > 0) {
                        attrsHtml += '<p class="text-xs font-semibold text-red-600 mb-2">Atributos faltantes (' + missing.length + '):</p>';
                        missing.forEach(function(attr) {
                            var suggestion = researchAttrMap[attr.id];
                            attrsHtml += '<div data-opt-attr="' + attr.id + '" class="mb-2">';
                            attrsHtml += '<label class="block text-xs font-medium text-gray-600 mb-1">' + attr.name;
                            if (attr.required) attrsHtml += ' <span class="text-red-500">*</span>';
                            attrsHtml += '</label>';
                            if (attr.values && attr.values.length > 0) {
                                attrsHtml += '<select data-opt-attr-id="' + attr.id + '" class="opt-attr-input w-full border border-red-300 rounded px-2 py-1.5 text-xs focus:outline-none focus:border-yellow-400">';
                                attrsHtml += '<option value="">Seleccionar...</option>';
                                attr.values.forEach(function(v) {
                                    attrsHtml += '<option value="' + v.id + '">' + v.name + '</option>';
                                });
                                attrsHtml += '</select>';
                            } else {
                                attrsHtml += '<input type="text" data-opt-attr-id="' + attr.id + '" class="opt-attr-input w-full border border-red-300 rounded px-2 py-1.5 text-xs focus:outline-none focus:border-yellow-400" placeholder="Valor...">';
                            }
                            if (suggestion && (suggestion.value_name || suggestion.value_id)) {
                                var sugText = suggestion.value_name || suggestion.value_id;
                                attrsHtml += '<div class="opt-attr-sug mt-0.5 flex items-center gap-1">' +
                                    '<span class="text-[10px] text-blue-600 bg-blue-50 border border-blue-200 rounded px-1.5 py-0.5 truncate max-w-[200px]" title="Fuente: ' + (suggestion.source || 'research') + '">' + sugText + '</span>' +
                                    '<button type="button" onclick="acceptOptAttrSuggestion(\'' + attr.id + '\', \'' + (suggestion.value_id || '').toString().replace(/'/g, "\\'") + '\', \'' + (suggestion.value_name || '').replace(/'/g, "\\'") + '\')" class="text-[10px] bg-green-500 text-white rounded px-1 py-0.5 hover:bg-green-600">Usar</button>' +
                                    '</div>';
                            }
                            attrsHtml += '</div>';
                        });
                    }

                    if (improvable.length > 0) {
                        attrsHtml += '<p class="text-xs font-semibold text-yellow-600 mb-2 mt-3">Atributos con posible mejora (' + improvable.length + '):</p>';
                        improvable.forEach(function(attr) {
                            var suggestion = researchAttrMap[attr.id];
                            var sugText = suggestion.value_name || suggestion.value_id;
                            attrsHtml += '<div data-opt-attr="' + attr.id + '" class="mb-2">';
                            attrsHtml += '<label class="block text-xs font-medium text-gray-600 mb-1">' + attr.name + '</label>';
                            attrsHtml += '<div class="flex items-center gap-2 text-xs">';
                            attrsHtml += '<span class="text-gray-500">Actual: <strong>' + (attr._current_value || '') + '</strong></span>';
                            attrsHtml += '<span class="text-gray-300">&rarr;</span>';
                            attrsHtml += '<span class="text-blue-600 bg-blue-50 border border-blue-200 rounded px-1.5 py-0.5">' + sugText + '</span>';
                            attrsHtml += '<button type="button" onclick="acceptOptAttrChange(\'' + attr.id + '\', \'' + (suggestion.value_id || '').toString().replace(/'/g, "\\'") + '\', \'' + (suggestion.value_name || '').replace(/'/g, "\\'") + '\', this)" class="text-[10px] bg-yellow-500 text-white rounded px-1.5 py-0.5 hover:bg-yellow-600">Cambiar</button>';
                            attrsHtml += '</div>';
                            attrsHtml += '<input type="hidden" data-opt-attr-id="' + attr.id + '" class="opt-attr-input" value="">';
                            attrsHtml += '</div>';
                        });
                    }

                    if (missing.length > 0 || improvable.length > 0) {
                        document.getElementById('opt-attrs-list').innerHTML = attrsHtml;
                        document.getElementById('opt-attrs-section').classList.remove('hidden');
                    } else {
                        document.getElementById('opt-attrs-section').classList.add('hidden');
                    }
                } catch (e) {
                    document.getElementById('opt-attrs-section').classList.add('hidden');
                }
            }

            document.getElementById('opt-loading').classList.add('hidden');
            document.getElementById('opt-content').classList.remove('hidden');
            document.getElementById('opt-footer').classList.remove('hidden');

        } catch (err) {
            document.getElementById('opt-loading').innerHTML = '<p class="text-red-500 text-center py-8">Error: ' + err.message + '</p>';
        }
    };

    window.closeOptimizeModal = function() {
        cancelAiRequest();
        document.getElementById('optimize-modal').classList.add('hidden');
    };

    window.addOptPic = function(url, el) {
        var textarea = document.getElementById('opt-new-pics');
        var current = textarea.value.trim();
        textarea.value = current ? current + '\n' + url : url;
        if (el) {
            el.style.opacity = '0.4';
            el.style.pointerEvents = 'none';
        }
    };

    window.acceptOptAttrSuggestion = function(attrId, valueId, valueName) {
        var input = document.querySelector('.opt-attr-input[data-opt-attr-id="' + attrId + '"]');
        if (!input) return;

        if (input.tagName === 'SELECT' && valueId) {
            var found = false;
            for (var i = 0; i < input.options.length; i++) {
                if (input.options[i].value === valueId) {
                    input.value = valueId;
                    found = true;
                    break;
                }
            }
            if (!found && valueName) {
                var lower = valueName.toLowerCase();
                for (var i = 0; i < input.options.length; i++) {
                    if (input.options[i].text.toLowerCase() === lower ||
                        input.options[i].text.toLowerCase().indexOf(lower) !== -1) {
                        input.value = input.options[i].value;
                        found = true;
                        break;
                    }
                }
            }
        } else if (input.tagName === 'INPUT' || input.tagName === 'SELECT') {
            input.value = valueName || valueId;
        }

        input.classList.add('bg-green-50', 'border-green-300');

        var wrapper = document.querySelector('[data-opt-attr="' + attrId + '"]');
        if (wrapper) {
            var chip = wrapper.querySelector('.opt-attr-sug');
            if (chip) chip.remove();
        }
    };

    window.acceptOptAttrChange = function(attrId, valueId, valueName, btn) {
        var input = document.querySelector('.opt-attr-input[data-opt-attr-id="' + attrId + '"]');
        if (input) {
            input.value = valueId || valueName;
            input.setAttribute('data-value-name', valueName);
        }
        if (btn) {
            btn.textContent = 'Aceptado';
            btn.className = 'text-[10px] bg-green-500 text-white rounded px-1.5 py-0.5';
            btn.disabled = true;
        }
    };

    window.saveOptimization = async function() {
        if (!optimizeItemId || !optimizeItemData) return;

        var payload = {};
        var item = optimizeItemData.item;

        var newTitle = document.getElementById('opt-title').value.trim();
        var isCatalogItem = !!(item.family_name || item.catalog_product_id);
        if (newTitle && newTitle !== item.title && !isCatalogItem) {
            payload.title = newTitle;
        }

        var newDesc = document.getElementById('opt-description').value.trim();
        if (newDesc && newDesc !== (optimizeItemData.description || '')) {
            payload.description = newDesc;
        }

        var newPicsText = document.getElementById('opt-new-pics').value.trim();
        if (newPicsText) {
            var existingPics = (item.pictures || []).map(function(p) { return {id: p.id}; });
            newPicsText.split('\n').forEach(function(url) {
                url = url.trim();
                if (url) existingPics.push({source: url});
            });
            payload.pictures = existingPics;
        }

        var newAttrs = [];
        document.querySelectorAll('.opt-attr-input').forEach(function(el) {
            var attrId = el.getAttribute('data-opt-attr-id');
            var value = el.value;
            if (value && value !== '') {
                var valueName = el.getAttribute('data-value-name');
                if (el.tagName === 'SELECT') {
                    newAttrs.push({id: attrId, value_id: value});
                } else if (valueName) {
                    newAttrs.push({id: attrId, value_name: valueName});
                } else {
                    newAttrs.push({id: attrId, value_name: value});
                }
            }
        });
        if (newAttrs.length > 0) {
            payload.attributes = newAttrs;
        }

        if (Object.keys(payload).length === 0) {
            alert('No hay cambios para guardar');
            return;
        }

        document.getElementById('btn-save-optimize').disabled = true;
        document.getElementById('btn-save-optimize').textContent = 'Guardando...';
        document.getElementById('opt-result').innerHTML = '';

        try {
            var resp = await fetch('/api/sku-inventory/optimize/' + optimizeItemId, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            var data = await resp.json();

            if (data.error) {
                document.getElementById('opt-result').innerHTML = '<span class="text-red-600">' + escapeHtml(data.error) + '</span>';
            } else if (data.errors && data.errors.length > 0) {
                var errHtml = '<div class="text-yellow-700"><span class="font-semibold">Guardado parcial.</span> Errores:<ul class="list-disc pl-4 mt-1 text-xs">';
                data.errors.forEach(function(e) { errHtml += '<li>' + escapeHtml(e) + '</li>'; });
                errHtml += '</ul></div>';
                if (data.new_score) errHtml += '<span class="text-green-600 text-xs">Score: ' + data.new_score + '%</span>';
                document.getElementById('opt-result').innerHTML = errHtml;
            } else {
                var newScore = data.new_score || 0;
                document.getElementById('opt-result').innerHTML = '<span class="text-green-600 font-semibold">Guardado! Nuevo score: ' + newScore + '%</span>';

                var badge = document.getElementById('opt-score-badge');
                badge.textContent = newScore + '%';
                if (newScore >= 75) badge.className = 'text-2xl font-bold text-green-600';
                else if (newScore >= 50) badge.className = 'text-2xl font-bold text-yellow-600';
                else badge.className = 'text-2xl font-bold text-red-600';

                var idx = allResults.findIndex(function(r) { return r.item_id === optimizeItemId; });
                if (idx !== -1) {
                    allResults[idx].listing_score = newScore;
                    if (payload.title) allResults[idx].item_title = payload.title;
                    renderTable();
                }
            }
        } catch (err) {
            document.getElementById('opt-result').innerHTML = '<span class="text-red-600">Error: ' + err.message + '</span>';
        }

        document.getElementById('btn-save-optimize').disabled = false;
        document.getElementById('btn-save-optimize').textContent = 'Guardar cambios';
    };

    // =========================================================
    // Edit MeLi Stock
    // =========================================================

    window.editMeliStock = function(itemId, currentQty) {
        var newQty = prompt('Nuevo stock para ' + itemId + ':', currentQty);
        if (newQty === null) return;
        newQty = parseInt(newQty);
        if (isNaN(newQty) || newQty < 0) {
            alert('Cantidad invalida');
            return;
        }
        updateMeliStock(itemId, newQty);
    };

    async function updateMeliStock(itemId, quantity) {
        try {
            var resp = await fetch('/api/sku-inventory/update-stock/' + itemId, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({quantity: quantity})
            });
            var data = await resp.json();

            if (data.error) {
                alert('Error: ' + data.error);
                return;
            }

            var idx = allResults.findIndex(function(r) { return r.item_id === itemId; });
            if (idx !== -1) {
                allResults[idx].meli_stock = quantity;
                renderTable();
            }

            alert('Stock actualizado a ' + quantity);

        } catch (err) {
            alert('Error: ' + err.message);
        }
    }

    // =========================================================
    // Reactivate
    // =========================================================

    window.reactivateItem = async function(itemId) {
        if (!confirm('Reactivar este item?')) return;

        try {
            var resp = await fetch('/api/sku-inventory/reactivate/' + itemId, {method: 'PUT'});
            var data = await resp.json();

            if (data.error) {
                alert('Error: ' + data.error);
                return;
            }

            alert('Item reactivado!');

            var idx = allResults.findIndex(function(r) { return r.item_id === itemId; });
            if (idx !== -1) {
                allResults[idx].meli_status = 'active';
                renderTable();
            }

        } catch (err) {
            alert('Error: ' + err.message);
        }
    };

    function escapeHtml(str) {
        return str.replace(/'/g, "\\'").replace(/"/g, '&quot;');
    }

    // Initialize
    document.getElementById('empty-state').classList.remove('hidden');
})();
