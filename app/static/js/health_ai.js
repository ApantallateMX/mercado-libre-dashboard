/* ============================================================
   Health AI — client-side helpers for AI-powered suggestions
   ============================================================ */

/* ---------- Generic SSE streamer ---------- */

function streamAiResponse(url, body, textElId, onComplete) {
    var textEl = document.getElementById(textElId);
    if (!textEl) return;
    textEl.textContent = '';

    var completed = false;
    function finish(fullText) {
        if (completed) return;
        completed = true;
        clearTimeout(globalTimeout);
        clearTimeout(chunkTimeout);
        if (onComplete) onComplete(fullText);
    }

    var controller = new AbortController();
    // Global timeout: 45s max total
    var globalTimeout = setTimeout(function () {
        if (!completed) {
            controller.abort();
            textEl.innerHTML += '<br><span class="text-red-500">Timeout: la IA no respondio en 45s</span>';
            finish('');
        }
    }, 45000);

    // Chunk inactivity timeout: 20s without data
    var chunkTimeout;
    function resetChunkTimeout() {
        clearTimeout(chunkTimeout);
        chunkTimeout = setTimeout(function () {
            if (!completed) {
                controller.abort();
                textEl.innerHTML += '<br><span class="text-red-500">Timeout: sin datos por 20s</span>';
                finish('');
            }
        }, 20000);
    }
    resetChunkTimeout();

    fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal
    })
    .then(function (resp) {
        if (!resp.ok) {
            return resp.json().then(function (d) {
                textEl.innerHTML = '<span class="text-red-500">' + (d.error || 'Error del servidor') + '</span>';
                finish('');
                throw new Error(d.error);
            });
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var fullText = '';

        function pump() {
            reader.read().then(function (result) {
                if (result.done) { finish(fullText); return; }
                resetChunkTimeout();
                var chunk = decoder.decode(result.value, { stream: true });
                var lines = chunk.split('\n');
                for (var i = 0; i < lines.length; i++) {
                    var line = lines[i].trim();
                    if (!line.startsWith('data: ')) continue;
                    var data = line.substring(6);
                    if (data === '[DONE]') { finish(fullText); return; }
                    if (data.startsWith('[ERROR]')) {
                        textEl.innerHTML += '<br><span class="text-red-500">' + data + '</span>';
                        finish('');
                        return;
                    }
                    try {
                        var parsed = JSON.parse(data);
                        if (parsed.text) { fullText += parsed.text; textEl.textContent = fullText; }
                    } catch (e) { /* skip */ }
                }
                pump();
            }).catch(function (err) {
                if (!completed) {
                    if (err.name !== 'AbortError') {
                        textEl.innerHTML += '<br><span class="text-red-500">Error de conexion</span>';
                    }
                    finish('');
                }
            });
        }
        pump();
    })
    .catch(function (e) {
        if (!completed) {
            if (e.name !== 'AbortError' && !textEl.innerHTML) {
                textEl.innerHTML = '<span class="text-red-500">Error: ' + e.message + '</span>';
            }
            finish('');
        }
    });
}

/* ---------- Panel helpers ---------- */

function _showPanel(panelId, spinnerId) {
    var panel = document.getElementById(panelId);
    if (panel) panel.classList.remove('hidden');
    var spinner = document.getElementById(spinnerId);
    if (spinner) spinner.classList.remove('hidden');
}

function _hideSpinnerShowActions(spinnerId, actionsId) {
    var spinner = document.getElementById(spinnerId);
    if (spinner) spinner.classList.add('hidden');
    var actions = document.getElementById(actionsId);
    if (actions) actions.classList.remove('hidden');
}

/* ---------- Questions ---------- */

window.suggestQuestionAnswer = function (btn) {
    var id = btn.getAttribute('data-id');
    var panelId = 'ai-panel-q-' + id;
    var textId = 'ai-text-q-' + id;
    var spinnerId = 'ai-spin-q-' + id;
    var actionsId = 'ai-act-q-' + id;

    // Hide actions, show spinner
    _showPanel(panelId, spinnerId);
    var actEl = document.getElementById(actionsId);
    if (actEl) actEl.classList.add('hidden');
    document.getElementById(textId).textContent = '';

    btn.disabled = true;
    btn.innerHTML = '<svg class="animate-spin h-4 w-4 inline mr-1" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg>Generando...';

    var buyerHistory = [];
    try { buyerHistory = JSON.parse(btn.getAttribute('data-buyer-history') || '[]'); } catch (e) { }

    var contextInput = document.getElementById('ai-context-q-' + id);
    var userContext = contextInput ? contextInput.value.trim() : '';

    var payload = {
        question_text: btn.getAttribute('data-text'),
        product_title: btn.getAttribute('data-product-title') || '',
        product_price: parseFloat(btn.getAttribute('data-price')) || 0,
        product_stock: parseInt(btn.getAttribute('data-stock')) || 0,
        elapsed: btn.getAttribute('data-elapsed') || '',
        buyer_history: buyerHistory,
        user_context: userContext
    };

    streamAiResponse('/api/health-ai/suggest-answer', payload, textId, function (fullText) {
        _hideSpinnerShowActions(spinnerId, actionsId);
        btn.disabled = false;
        btn.innerHTML = '<svg class="w-4 h-4 inline mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>Sugerir con IA';
    });
};

/* ---------- Claims — Analysis ---------- */

window.analyzeClaimAi = function (btn) {
    var id = btn.getAttribute('data-id');
    var panelId = 'ai-analysis-' + id;
    var panel = document.getElementById(panelId);
    if (!panel) return;
    panel.classList.remove('hidden');
    panel.innerHTML = '<div class="flex items-center gap-2 p-4"><svg class="animate-spin h-5 w-5 text-blue-500" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg><span class="text-sm text-blue-600">Analizando reclamo...</span></div>';

    btn.disabled = true;

    var payload = {
        reason_desc: btn.getAttribute('data-reason-desc'),
        product_title: btn.getAttribute('data-product-title') || '',
        product_price: parseFloat(btn.getAttribute('data-price')) || 0,
        days_open: parseInt(btn.getAttribute('data-days-open')) || 0,
        claims_rate: parseFloat(btn.getAttribute('data-claims-rate')) || 0,
        claims_status: btn.getAttribute('data-claims-status') || '',
        sale_fee: parseFloat(btn.getAttribute('data-sale-fee')) || 0,
        shipping_cost: parseFloat(btn.getAttribute('data-shipping-cost')) || 0
    };

    fetch('/api/health-ai/claim-analysis', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
        if (data.error) {
            panel.innerHTML = '<p class="p-4 text-red-500 text-sm">' + data.error + '</p>';
        } else {
            panel.innerHTML = buildAnalysisHtml(data);
        }
        btn.disabled = false;
    })
    .catch(function (e) {
        panel.innerHTML = '<p class="p-4 text-red-500 text-sm">Error: ' + e.message + '</p>';
        btn.disabled = false;
    });
};

function buildAnalysisHtml(d) {
    var rec = d.recommendation || 'mediar';
    var conf = d.confidence || 'baja';
    var fi = d.financial_impact || {};

    // Recommendation badge colors
    var recColors = {
        devolver_total: 'bg-red-100 text-red-800',
        devolver_parcial: 'bg-yellow-100 text-yellow-800',
        reemplazar: 'bg-blue-100 text-blue-800',
        mediar: 'bg-gray-100 text-gray-800',
        rechazar: 'bg-green-100 text-green-800'
    };
    var recLabels = {
        devolver_total: 'Devolucion Total',
        devolver_parcial: 'Devolucion Parcial',
        reemplazar: 'Reemplazo',
        mediar: 'Mediacion',
        rechazar: 'Rechazar Reclamo'
    };
    var confColors = { alta: 'text-green-600', media: 'text-yellow-600', baja: 'text-red-600' };

    var html = '<div class="bg-blue-50 border border-blue-200 rounded-lg p-4">';
    html += '<div class="flex items-center justify-between mb-3">';
    html += '<span class="text-xs font-bold text-blue-700 uppercase">Analisis IA</span>';
    html += '<span class="text-xs ' + (confColors[conf] || 'text-gray-500') + ' font-semibold">Confianza: ' + conf + '</span>';
    html += '</div>';

    // Recommendation badge
    html += '<div class="mb-3"><span class="inline-block px-3 py-1 rounded-full text-sm font-semibold ' + (recColors[rec] || 'bg-gray-100 text-gray-800') + '">' + (recLabels[rec] || rec) + '</span></div>';

    // Financial impact grid
    html += '<div class="grid grid-cols-3 gap-3 mb-3">';
    html += '<div class="bg-white rounded p-2 text-center"><p class="text-xs text-gray-500">Costo devolucion</p><p class="text-sm font-bold text-red-600">$' + (fi.refund_cost || 0).toFixed(2) + '</p></div>';
    html += '<div class="bg-white rounded p-2 text-center"><p class="text-xs text-gray-500">Comision recuperada</p><p class="text-sm font-bold text-green-600">$' + (fi.recovered_commission || 0).toFixed(2) + '</p></div>';
    html += '<div class="bg-white rounded p-2 text-center"><p class="text-xs text-gray-500">Perdida neta</p><p class="text-sm font-bold text-gray-800">$' + (fi.net_loss || 0).toFixed(2) + '</p></div>';
    html += '</div>';

    // Pros / Cons
    var pros = d.pros || [];
    var cons = d.cons || [];
    html += '<div class="grid grid-cols-2 gap-3 mb-3">';
    html += '<div><p class="text-xs font-semibold text-green-700 mb-1">Pros</p><ul class="text-xs text-gray-700 space-y-0.5">';
    for (var i = 0; i < pros.length; i++) html += '<li class="flex items-start gap-1"><span class="text-green-500 mt-0.5">+</span>' + pros[i] + '</li>';
    html += '</ul></div>';
    html += '<div><p class="text-xs font-semibold text-red-700 mb-1">Contras</p><ul class="text-xs text-gray-700 space-y-0.5">';
    for (var j = 0; j < cons.length; j++) html += '<li class="flex items-start gap-1"><span class="text-red-500 mt-0.5">-</span>' + cons[j] + '</li>';
    html += '</ul></div>';
    html += '</div>';

    // Summary + reputation badge
    html += '<div class="border-t border-blue-200 pt-2">';
    html += '<p class="text-sm text-gray-700 mb-2">' + (d.summary || '') + '</p>';
    if (d.affects_reputation) {
        html += '<span class="inline-block px-2 py-0.5 text-xs rounded-full bg-red-100 text-red-700 font-semibold">Afecta reputacion</span>';
    } else {
        html += '<span class="inline-block px-2 py-0.5 text-xs rounded-full bg-green-100 text-green-700 font-semibold">No afecta reputacion</span>';
    }
    html += '</div></div>';
    return html;
}

/* ---------- Claims — Suggest Response ---------- */

window.suggestClaimResponse = function (btn) {
    var id = btn.getAttribute('data-id');
    var panelId = 'ai-panel-claim-' + id;
    var textId = 'ai-text-claim-' + id;
    var spinnerId = 'ai-spin-claim-' + id;
    var actionsId = 'ai-act-claim-' + id;

    _showPanel(panelId, spinnerId);
    var actEl = document.getElementById(actionsId);
    if (actEl) actEl.classList.add('hidden');
    document.getElementById(textId).textContent = '';

    btn.disabled = true;
    btn.innerHTML = '<svg class="animate-spin h-4 w-4 inline mr-1" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg>Generando...';

    var issues = [];
    var suggestions = [];
    try { issues = JSON.parse(btn.getAttribute('data-issues') || '[]'); } catch (e) { }
    try { suggestions = JSON.parse(btn.getAttribute('data-suggestions') || '[]'); } catch (e) { }

    var payload = {
        claim_id: btn.getAttribute('data-id'),
        reason_id: btn.getAttribute('data-reason-id'),
        reason_desc: btn.getAttribute('data-reason-desc'),
        product_title: btn.getAttribute('data-product-title') || '',
        days_open: parseInt(btn.getAttribute('data-days-open')) || 0,
        issues: issues,
        suggestions: suggestions
    };

    streamAiResponse('/api/health-ai/suggest-claim-response', payload, textId, function (fullText) {
        _hideSpinnerShowActions(spinnerId, actionsId);
        btn.disabled = false;
        btn.innerHTML = '<svg class="w-4 h-4 inline mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>Sugerir Respuesta';
    });
};

/* ---------- Messages ---------- */

window.suggestMessageReply = function (btn) {
    var packId = btn.getAttribute('data-pack-id');
    var panelId = 'ai-panel-msg-' + packId;
    var textId = 'ai-text-msg-' + packId;
    var spinnerId = 'ai-spin-msg-' + packId;
    var actionsId = 'ai-act-msg-' + packId;

    _showPanel(panelId, spinnerId);
    var actEl = document.getElementById(actionsId);
    if (actEl) actEl.classList.add('hidden');
    document.getElementById(textId).textContent = '';

    btn.disabled = true;
    btn.innerHTML = '<svg class="animate-spin h-4 w-4 inline mr-1" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg>Generando...';

    // Scrape messages from DOM
    var container = document.getElementById('conv-messages-' + packId);
    var threadMessages = [];
    var lastBuyer = '';
    if (container) {
        var items = container.querySelectorAll('[data-msg-role]');
        items.forEach(function (el) {
            var isSeller = el.getAttribute('data-msg-role') === 'seller';
            var text = el.getAttribute('data-msg-text') || el.textContent.trim();
            threadMessages.push({ is_seller: isSeller, text: text });
            if (!isSeller) lastBuyer = text;
        });
    }

    var payload = {
        thread_messages: threadMessages,
        last_buyer_message: lastBuyer
    };

    streamAiResponse('/api/health-ai/suggest-message', payload, textId, function (fullText) {
        _hideSpinnerShowActions(spinnerId, actionsId);
        btn.disabled = false;
        btn.innerHTML = '<svg class="w-4 h-4 inline mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>Sugerir con IA';
    });
};

/* ---------- Use / Copy suggestion ---------- */

window.useAiSuggestion = function (type, id) {
    var sourceId, targetId;
    if (type === 'question') {
        sourceId = 'ai-text-q-' + id;
        targetId = 'answer-' + id;
    } else if (type === 'claim') {
        sourceId = 'ai-text-claim-' + id;
        targetId = 'claim-text-' + id;
    } else if (type === 'message') {
        sourceId = 'ai-text-msg-' + id;
        targetId = 'msg-input-' + id;
    }
    var source = document.getElementById(sourceId);
    var target = document.getElementById(targetId);
    if (source && target) {
        target.value = source.textContent;
        // Trigger input event to update character counters
        target.dispatchEvent(new Event('input', { bubbles: true }));
        target.focus();
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
};
