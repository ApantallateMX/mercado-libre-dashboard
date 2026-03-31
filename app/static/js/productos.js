/**
 * productos.js — Ciclo de vida unificado de publicaciones MeLi
 */
(function () {
  'use strict';

  // ── Estado global ──────────────────────────────────────────────────────────
  const state = {
    tab:            'all',       // all | active | paused | critico | candidates (candidates = Lanzador Inteligente)
    q:              '',
    offset:         0,
    limit:          50,
    total:          0,
    items:          [],
    loading:        false,
    sort_by:        '',          // '' | score_asc | score_desc | stock_asc | stock_desc | ventas_asc | ventas_desc
    // Panel
    panelItem:      null,
    panelTab:       'editar',
    panelDirty:     {},
    // Video
    currentVideoId: null,
  };

  // ── Init ───────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    loadStats();
    loadItems();
    // Close sort dropdown on outside click
    document.addEventListener('click', function (e) {
      const dd = document.getElementById('sort-dropdown');
      const btn = document.getElementById('sort-btn');
      if (dd && !dd.classList.contains('hidden') && !dd.contains(e.target) && !btn.contains(e.target)) {
        dd.classList.add('hidden');
      }
    });
  });

  // ── Stats ──────────────────────────────────────────────────────────────────
  async function loadStats() {
    try {
      const d = await apiFetch('/api/productos/stats');
      setText('stat-active',     d.active     ?? '—');
      setText('stat-paused',     d.paused     ?? '—');
      setText('stat-criticos',   d.criticos   ?? '—');
      setText('stat-candidates', d.candidates ?? '—');
      setText('cnt-all',         d.total      ?? '—');
      setText('cnt-active',      d.active     ?? '—');
      setText('cnt-paused',      d.paused     ?? '—');
      setText('cnt-critico',     d.criticos   ?? '—');
      setText('cnt-candidates',  d.candidates ?? '—');
    } catch (e) { console.warn('stats error', e); }
  }

  // ── Items list ─────────────────────────────────────────────────────────────
  async function loadItems() {
    if (state.loading) return;
    if (state.tab === 'candidates') return; // handled by Lanzador Inteligente
    state.loading = true;
    show('table-loading'); hide('table-wrap');

    try {
      const st = state.tab === 'all' ? 'all'
               : state.tab === 'critico' ? 'all'
               : state.tab;
      const sc = state.tab === 'critico' ? 'critico' : '';
      const data = await apiFetch(
        `/api/productos?status=${st}&q=${encodeURIComponent(state.q)}&offset=${state.offset}&limit=${state.limit}&score_category=${sc}&sort_by=${encodeURIComponent(state.sort_by)}`
      );
      state.items = data.items || [];
      state.total = data.total || 0;
      renderTable();
    } catch (e) {
      console.error('loadItems error', e);
      show('table-loading');
      document.getElementById('table-loading').innerHTML =
        `<div class="text-red-500 text-sm">Error al cargar: ${e.message}</div>`;
    } finally {
      state.loading = false;
    }
  }

  // ── Tabla ──────────────────────────────────────────────────────────────────
  function renderTable() {
    hide('table-loading');
    show('table-wrap');

    const tbody  = document.getElementById('productos-tbody');
    const mobile = document.getElementById('mobile-cards');
    const empty  = document.getElementById('empty-msg');

    if (!state.items.length) {
      if (tbody)  tbody.innerHTML  = '';
      if (mobile) mobile.innerHTML = '';
      show('empty-msg'); return;
    }
    hide('empty-msg');

    // Desktop rows
    if (tbody) {
      tbody.innerHTML = state.items.map(item => `
        <tr class="hover:bg-yellow-50 cursor-pointer transition" onclick="openPanel('${item.item_id || ''}', ${JSON.stringify(item).replace(/"/g, '&quot;')})">
          <td class="px-4 py-2.5 w-10">
            ${item.thumbnail
              ? `<img src="${item.thumbnail}" class="w-9 h-9 rounded-lg object-cover bg-gray-100" loading="lazy">`
              : `<div class="w-9 h-9 rounded-lg bg-gray-100 flex items-center justify-center text-gray-300 text-xs">—</div>`}
          </td>
          <td class="px-3 py-2.5 max-w-xs">
            <p class="font-medium text-gray-800 text-sm line-clamp-2 leading-snug">${escHtml(item.title)}</p>
            ${item.item_id ? `<p class="text-xs text-gray-400 font-mono mt-0.5">${item.item_id}</p>` : ''}
          </td>
          <td class="px-3 py-2.5 text-xs text-gray-500 font-mono whitespace-nowrap">${escHtml(item.sku || '—')}</td>
          <td class="px-3 py-2.5 text-center">${statusBadge(item.status)}</td>
          <td class="px-3 py-2.5 text-center">${item.status === 'candidate' ? '—' : scoreBadge(item.score)}</td>
          <td class="px-3 py-2.5 text-right text-sm font-mono">
            ${item.status === 'candidate'
              ? bmCell(item)
              : `<span class="${item.bm_total === 0 ? 'text-red-500' : 'text-gray-700'}">${item.bm_total ?? '—'}</span>
                 <span class="text-gray-300 text-xs ml-0.5">(${item.bm_mty}+${item.bm_cdmx})</span>`
            }
          </td>
          <td class="px-3 py-2.5 text-right text-sm font-mono">
            ${item.status === 'candidate' ? '—' : `<span class="${item.stock_ml === 0 ? 'text-orange-500' : 'text-gray-700'}">${item.stock_ml ?? 0}</span>`}
          </td>
          <td class="px-3 py-2.5 text-right text-sm font-semibold text-gray-700 whitespace-nowrap">
            ${item.price ? '$' + fmt(item.price) : '—'}
          </td>
          <td class="px-3 py-2.5 text-center" onclick="event.stopPropagation()">
            ${actionBtns(item)}
          </td>
        </tr>
      `).join('');
    }

    // Mobile cards
    if (mobile) {
      mobile.innerHTML = state.items.map(item => `
        <div class="bg-white rounded-xl border p-3 flex gap-3 cursor-pointer hover:border-yellow-400 transition"
             onclick="openPanel('${item.item_id || ''}', ${JSON.stringify(item).replace(/"/g, '&quot;')})">
          ${item.thumbnail
            ? `<img src="${item.thumbnail}" class="w-14 h-14 rounded-lg object-cover bg-gray-100 shrink-0">`
            : `<div class="w-14 h-14 rounded-lg bg-gray-100 shrink-0"></div>`}
          <div class="flex-1 min-w-0">
            <p class="font-medium text-sm text-gray-800 line-clamp-2 leading-snug">${escHtml(item.title)}</p>
            <div class="flex items-center gap-1.5 mt-1 flex-wrap">
              ${statusBadge(item.status)}
              ${item.status !== 'candidate' ? scoreBadge(item.score) : ''}
              ${item.sku ? `<span class="text-xs text-gray-400 font-mono">${escHtml(item.sku)}</span>` : ''}
            </div>
            <div class="flex items-center gap-3 mt-1 text-xs text-gray-500">
              <span>BM: <b class="${item.bm_total === 0 ? 'text-red-500' : 'text-gray-700'}">${item.bm_total ?? 0}</b></span>
              ${item.status !== 'candidate' ? `<span>ML: <b>${item.stock_ml ?? 0}</b></span>` : ''}
              ${item.price ? `<span class="font-semibold text-gray-700">$${fmt(item.price)}</span>` : ''}
            </div>
          </div>
          <div class="flex flex-col gap-1 shrink-0 items-end" onclick="event.stopPropagation()">
            ${actionBtns(item)}
          </div>
        </div>
      `).join('');
    }

    // Paginación
    const pageInfo = document.getElementById('page-info');
    const btnPrev  = document.getElementById('btn-prev');
    const btnNext  = document.getElementById('btn-next');
    if (pageInfo) pageInfo.textContent = `${state.offset + 1}–${Math.min(state.offset + state.limit, state.total)} de ${state.total}`;
    if (btnPrev)  btnPrev.disabled  = state.offset === 0;
    if (btnNext)  btnNext.disabled  = state.offset + state.limit >= state.total;
  }

  // ── Badge helpers ──────────────────────────────────────────────────────────
  function statusBadge(status) {
    const map = {
      active:    ['bg-green-100 text-green-700', 'Activo'],
      paused:    ['bg-orange-100 text-orange-700', 'Pausado'],
      candidate: ['bg-blue-100 text-blue-700', 'Candidato'],
      inactive:  ['bg-gray-100 text-gray-500', 'Inactivo'],
      closed:    ['bg-red-100 text-red-500', 'Cerrado'],
    };
    const [cls, label] = map[status] || ['bg-gray-100 text-gray-500', status];
    return `<span class="text-xs px-2 py-0.5 rounded-full font-medium ${cls}">${label}</span>`;
  }

  function scoreBadge(score) {
    if (score == null) return '—';
    const cls = score >= 70 ? 'bg-green-100 text-green-700'
              : score >= 40 ? 'bg-yellow-100 text-yellow-700'
              : 'bg-red-100 text-red-600';
    return `<span class="text-xs px-2 py-0.5 rounded-full font-semibold ${cls}">${score}%</span>`;
  }

  function bmCell(item) {
    const total = (item.bm_mty || 0) + (item.bm_cdmx || 0);
    return `<span class="${total === 0 ? 'text-red-500' : 'text-gray-700'}">${total}</span>
            <span class="text-gray-300 text-xs ml-0.5">(${item.bm_mty}+${item.bm_cdmx})</span>`;
  }

  function actionBtns(item) {
    if (item.status === 'candidate') {
      return `<button onclick="openCreateModal('${escHtml(item.sku)}', ${item.bm_total || 0})"
                class="bg-blue-500 hover:bg-blue-600 text-white text-xs px-2.5 py-1.5 rounded-lg font-medium transition">
                🚀 Lanzar</button>`;
    }
    const itemId = item.item_id;
    const videoBtn = item.has_clip_video
      ? `<button onclick="openPanelTab('${itemId}', 'video')"
           class="text-xs px-2 py-1 rounded-lg border text-purple-600 border-purple-200 hover:bg-purple-50 transition" title="Clip subido">
           📹 Clip ${clipStatusIcon(item.clip_status)}</button>`
      : item.video_id
      ? `<button onclick="openPanelTab('${itemId}', 'video')"
           class="text-xs px-2 py-1 rounded-lg border text-blue-600 border-blue-200 hover:bg-blue-50 transition" title="Video listo para subir">
           📹 Subir</button>`
      : '';
    const optimizeBtn = `<button onclick="openOptimizeModal('${itemId}', '${escHtml(item.sku || '')}')"
         class="text-xs px-2 py-1 rounded-lg border text-yellow-700 border-yellow-200 hover:bg-yellow-50 transition">✨ Opt</button>`;
    const toggleBtn = item.status === 'paused'
      ? `<button onclick="toggleStatus('${itemId}', 'active')"
           class="text-xs px-2 py-1 rounded-lg border text-green-600 border-green-200 hover:bg-green-50 transition">▶ Activar</button>`
      : `<button onclick="toggleStatus('${itemId}', 'paused')"
           class="text-xs px-2 py-1 rounded-lg border text-orange-500 border-orange-200 hover:bg-orange-50 transition">⏸ Pausar</button>`;
    return `<div class="flex flex-col gap-1">
      <button onclick="openPanel('${itemId}')"
        class="text-xs px-2 py-1 rounded-lg bg-yellow-100 hover:bg-yellow-200 text-yellow-800 font-medium transition">✏ Editar</button>
      ${optimizeBtn}
      ${toggleBtn}
      ${videoBtn}
    </div>`;
  }

  function clipStatusIcon(status) {
    if (status === 'active')     return '✓';
    if (status === 'processing') return '⏳';
    if (status === 'error')      return '⚠';
    return '';
  }

  // ── Tabs ───────────────────────────────────────────────────────────────────
  window.setTab = function (tab) {
    state.tab    = tab;
    state.offset = 0;
    document.querySelectorAll('.tab-btn').forEach(btn => {
      const active = btn.dataset.tab === tab;
      btn.classList.toggle('active-tab', active);
      btn.classList.toggle('border-yellow-500', active);
      btn.classList.toggle('text-yellow-700', active);
      btn.classList.toggle('bg-yellow-50', active);
      btn.classList.toggle('border-transparent', !active);
      btn.classList.toggle('text-gray-500', !active);
      btn.classList.toggle('bg-white', !active);
    });

    const isCandidates = tab === 'candidates';
    // Show compare-wrap (Lanzador Inteligente) OR products table
    const compareWrap = document.getElementById('compare-wrap');
    if (compareWrap) compareWrap.classList.toggle('hidden', !isCandidates);

    // When switching to candidates tab, hide table; otherwise restore
    if (isCandidates) {
      hide('table-loading');
      hide('table-wrap');
      return; // Lanzador Inteligente handles its own data
    }
    loadItems();
  };

  // ── Sort dropdown ──────────────────────────────────────────────────────────
  window.toggleSortDropdown = function () {
    document.getElementById('sort-dropdown')?.classList.toggle('hidden');
  };

  window.setSort = function (sortBy) {
    state.sort_by  = sortBy;
    state.offset   = 0;
    const labels = {
      '':           'Ordenar',
      'score_desc': 'Score ↓',
      'score_asc':  'Score ↑',
      'stock_desc': 'Stock ↓',
      'stock_asc':  'Stock ↑',
      'ventas_desc':'Ventas ↓',
      'ventas_asc': 'Ventas ↑',
    };
    setText('sort-label', labels[sortBy] || 'Ordenar');
    document.getElementById('sort-dropdown')?.classList.add('hidden');
    loadItems();
  };

  // ── Comparador manual (colapsable dentro de Candidatos) ────────────────────
  window.toggleComparadorManual = function () {
    const body    = document.getElementById('comparador-manual-body');
    const chevron = document.getElementById('comparador-chevron');
    if (!body) return;
    const open = body.classList.toggle('hidden');
    if (chevron) chevron.style.transform = open ? '' : 'rotate(180deg)';
  };

  // ── Search ─────────────────────────────────────────────────────────────────
  let searchTimer;
  window.onSearchInput = function () {
    const v = document.getElementById('search-input').value.trim();
    const clearBtn = document.getElementById('search-clear');
    if (clearBtn) clearBtn.classList.toggle('hidden', !v);
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = v; state.offset = 0; loadItems();
    }, 350);
  };

  window.clearSearch = function () {
    document.getElementById('search-input').value = '';
    document.getElementById('search-clear').classList.add('hidden');
    state.q = ''; state.offset = 0; loadItems();
  };

  // ── Pagination ─────────────────────────────────────────────────────────────
  window.prevPage = function () {
    if (state.offset > 0) { state.offset = Math.max(0, state.offset - state.limit); loadItems(); }
  };
  window.nextPage = function () {
    if (state.offset + state.limit < state.total) { state.offset += state.limit; loadItems(); }
  };

  // ── Panel ──────────────────────────────────────────────────────────────────
  window.openPanel = async function (itemId, preloaded) {
    if (!itemId) return;
    state.panelTab  = 'editar';
    state.panelDirty = {};

    showPanel();
    renderPanelHeader(preloaded || {});
    renderPanelSkeleton();

    try {
      const detail = await apiFetch(`/api/productos/${itemId}`);
      state.panelItem = detail;
      renderPanelHeader(detail);
      renderPanelTab(state.panelTab);
      show('panel-footer');
    } catch (e) {
      document.getElementById('panel-content').innerHTML =
        `<div class="p-4 text-sm text-red-500">Error al cargar: ${e.message}</div>`;
    }
  };

  window.openPanelTab = function (itemId, tab) {
    state.panelTab = tab;
    openPanel(itemId);
  };

  function showPanel() {
    document.getElementById('panel-overlay').classList.remove('hidden');
    const panel = document.getElementById('side-panel');
    panel.classList.remove('hidden');
    setTimeout(() => panel.classList.remove('translate-x-full'), 10);
  }

  window.closePanel = function () {
    const panel = document.getElementById('side-panel');
    panel.classList.add('translate-x-full');
    setTimeout(() => {
      panel.classList.add('hidden');
      document.getElementById('panel-overlay').classList.add('hidden');
      hide('panel-footer');
    }, 310);
    state.panelItem  = null;
    state.panelDirty = {};
  };

  function renderPanelHeader(item) {
    setText('panel-item-id', item.item_id || '');
    setText('panel-title',   item.title   || 'Cargando...');
    const sb = document.getElementById('panel-status-badge');
    if (sb) sb.outerHTML = `<span id="panel-status-badge">${statusBadge(item.status || '')}</span>`;
    const sc = document.getElementById('panel-score-badge');
    if (sc) sc.outerHTML = `<span id="panel-score-badge">${item.score != null ? scoreBadge(item.score) : ''}</span>`;
    const pl = document.getElementById('panel-permalink');
    if (pl && item.permalink) { pl.href = item.permalink; pl.classList.remove('hidden'); }
  }

  function renderPanelSkeleton() {
    document.getElementById('panel-content').innerHTML = `
      <div class="flex items-center justify-center h-32 text-gray-400 text-sm">
        <svg class="w-5 h-5 animate-spin mr-2 text-yellow-400" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"/>
        </svg>
        Cargando detalle...
      </div>`;
  }

  window.setPanelTab = function (tab) {
    state.panelTab = tab;
    document.querySelectorAll('.panel-tab').forEach(btn => {
      const active = btn.dataset.ptab === tab;
      btn.classList.toggle('active-panel-tab', active);
      btn.classList.toggle('border-yellow-500', active);
      btn.classList.toggle('text-yellow-700', active);
      btn.classList.toggle('border-transparent', !active);
      btn.classList.toggle('text-gray-500', !active);
    });
    renderPanelTab(tab);
  };

  function renderPanelTab(tab) {
    const item = state.panelItem;
    if (!item) return;
    const c = document.getElementById('panel-content');

    if (tab === 'editar') {
      c.innerHTML = `
        <div class="p-4 space-y-4">
          <div>
            <label class="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1">Título</label>
            <textarea id="edit-title" rows="2"
              class="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-yellow-300 resize-none"
              oninput="markDirty('title', this.value)">${escHtml(item.title || '')}</textarea>
            <p class="text-xs text-gray-400 mt-0.5"><span id="title-len">${(item.title || '').length}</span>/80 caracteres</p>
          </div>
          <div>
            <label class="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1">Precio (MXN)</label>
            <input id="edit-price" type="number" value="${item.price || ''}"
              class="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-yellow-300"
              oninput="markDirty('price', parseFloat(this.value))">
          </div>
          <div>
            <label class="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1">Descripción</label>
            <textarea id="edit-desc" rows="5"
              class="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-yellow-300 resize-none font-mono text-xs"
              oninput="markDirty('description', this.value)">${escHtml(item.description || '')}</textarea>
          </div>
          ${item.problems && item.problems.length ? `
          <div class="bg-orange-50 border border-orange-200 rounded-lg p-3">
            <p class="text-xs font-semibold text-orange-700 mb-1">Problemas detectados</p>
            <ul class="text-xs text-orange-600 space-y-0.5 list-disc list-inside">
              ${item.problems.map(p => `<li>${escHtml(p)}</li>`).join('')}
            </ul>
          </div>` : ''}
          <div class="pt-2">
            <button onclick="openOptimizeModal('${item.item_id}', '${escHtml(item.sku || '')}')"
              class="w-full border border-yellow-300 bg-yellow-50 hover:bg-yellow-100 text-yellow-800 text-sm font-medium py-2 rounded-lg transition">
              ✨ Optimizar con IA (título, descripción, atributos)
            </button>
          </div>
        </div>`;
      const ta = document.getElementById('edit-title');
      if (ta) ta.addEventListener('input', () => {
        const el = document.getElementById('title-len');
        if (el) el.textContent = ta.value.length;
      });
    }

    else if (tab === 'stock') {
      c.innerHTML = `
        <div class="p-4 space-y-4">
          <div class="grid grid-cols-3 gap-3">
            ${stockCard('MTY', item.bm_mty, 'blue')}
            ${stockCard('CDMX', item.bm_cdmx, 'green')}
            ${stockCard('TJ', item.bm_tj, 'gray')}
          </div>
          <div class="bg-gray-50 rounded-xl p-3 flex items-center justify-between">
            <div>
              <p class="text-xs text-gray-500">Stock disponible BM</p>
              <p class="text-2xl font-bold text-gray-800">${item.bm_avail ?? 0}</p>
            </div>
            <div class="text-right">
              <p class="text-xs text-gray-500">Stock MeLi actual</p>
              <p class="text-2xl font-bold ${item.stock_ml === 0 ? 'text-red-500' : 'text-gray-800'}">${item.stock_ml ?? 0}</p>
            </div>
          </div>
          <div>
            <label class="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1">Actualizar stock ML</label>
            <div class="flex gap-2">
              <input id="new-stock" type="number" min="0" value="${item.stock_ml || 0}"
                class="flex-1 border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-yellow-300">
              <button onclick="syncStock('${item.item_id}')"
                class="bg-green-500 hover:bg-green-600 text-white text-sm px-4 py-2 rounded-lg font-medium transition">
                Sincronizar
              </button>
            </div>
            <p class="text-xs text-gray-400 mt-1">Sugerido: ${Math.floor((item.bm_avail || 0) * 0.6)} (60% del disponible BM)</p>
          </div>
        </div>`;
    }

    else if (tab === 'atributos') {
      c.innerHTML = `
        <div class="p-4 space-y-3">
          <div class="flex items-center justify-between">
            <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide">Atributos del producto</p>
            <div class="flex gap-2">
              <button onclick="panelAiAttrs('${item.item_id}')"
                class="px-2 py-1 bg-purple-100 text-purple-700 text-xs font-medium rounded-full hover:bg-purple-200 transition flex items-center gap-1">
                ✦ Rellenar con IA
              </button>
              <button onclick="panelSaveAttrs('${item.item_id}')"
                class="px-3 py-1 bg-yellow-400 text-gray-800 text-xs font-semibold rounded hover:bg-yellow-500 transition">
                Guardar
              </button>
            </div>
          </div>
          <p id="attrs-msg" class="text-xs hidden"></p>
          <div id="attrs-loading" class="text-xs text-gray-400 text-center py-4">Cargando atributos de categoría...</div>
          <div id="attrs-list" class="space-y-2 hidden"></div>
        </div>`;
      // Load category attributes
      panelLoadAttrs(item);
    }

    else if (tab === 'video') {
      const hasVideo  = item.has_clip_video;
      const clipReady = item.video_id;
      const titleEsc  = escHtml(item.title || '');
      c.innerHTML = `
        <div class="p-4 space-y-4">
          ${hasVideo ? `
          <div class="bg-green-50 border border-green-200 rounded-xl p-3">
            <p class="text-sm font-semibold text-green-700">✓ Video clip ${item.clip_status === 'active' ? 'activo en ML' : 'subido — en moderación'}</p>
            ${item.clip_uuid ? `<p class="text-xs text-gray-400 mt-1 font-mono">UUID: ${item.clip_uuid}</p>` : ''}
          </div>` : ''}

          ${clipReady && !hasVideo ? `
          <div class="bg-blue-50 border border-blue-200 rounded-xl p-3">
            <p class="text-sm font-semibold text-blue-700 mb-2">🎬 Video generado listo para subir</p>
            <p class="text-xs text-gray-500 mb-3">Video ID: <span class="font-mono">${item.video_id}</span></p>
            <button onclick="uploadClipFromPanel('${item.item_id}', '${item.video_id}', '${escHtml(item.sku)}')"
              class="w-full bg-blue-500 hover:bg-blue-600 text-white text-sm py-2 rounded-lg font-medium transition">
              📤 Subir Clip a ML
            </button>
          </div>` : ''}

          <!-- Guion con IA -->
          <div class="bg-purple-50 border border-purple-200 rounded-xl p-3 space-y-2">
            <div class="flex items-center justify-between">
              <p class="text-xs font-semibold text-purple-700 uppercase tracking-wide">1. Guion con IA</p>
              <button id="btn-gen-script" onclick="panelGenScript('${item.item_id}', '${titleEsc}')"
                class="px-3 py-1.5 bg-purple-600 hover:bg-purple-700 text-white text-xs font-semibold rounded-lg transition flex items-center gap-1">
                ✦ Generar Guion
              </button>
            </div>
            <p class="text-xs text-gray-500">La IA genera un guion de 30-45 segundos para tu clip de ML.</p>
            <textarea id="video-script" rows="8" placeholder="El guion aparecerá aquí... puedes editarlo antes de grabar."
              class="w-full border border-purple-200 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:border-purple-400 bg-white resize-none">${item._video_script || ''}</textarea>
            <p id="script-status" class="text-xs text-purple-600 hidden"></p>
          </div>

          <!-- Generar video completo con IA -->
          <div class="bg-blue-50 border border-blue-200 rounded-xl p-3 space-y-2">
            <div class="flex items-center justify-between">
              <p class="text-xs font-semibold text-blue-700 uppercase tracking-wide">2. Generar Video con IA</p>
              <button id="btn-gen-video" onclick="panelGenVideoFull('${item.item_id}')"
                class="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold rounded-lg transition flex items-center gap-1">
                🎬 Generar Video
              </button>
            </div>
            <p class="text-xs text-gray-500">Genera el video con voz en español usando las fotos del producto para revisar antes de subir.</p>
            <div id="video-gen-progress" class="hidden">
              <div class="flex items-center gap-2 py-1">
                <div class="w-3 h-3 rounded-full bg-blue-500 animate-pulse shrink-0"></div>
                <p id="video-gen-step" class="text-xs text-blue-700 font-medium"></p>
              </div>
              <div class="w-full bg-blue-100 rounded-full h-1.5 mt-1">
                <div id="video-gen-bar" class="bg-blue-500 h-1.5 rounded-full transition-all duration-700" style="width:0%"></div>
              </div>
            </div>
            <!-- Preview after generation -->
            <div id="video-preview-wrap" class="hidden space-y-2 pt-1">
              <video id="video-preview-el" controls playsinline
                class="w-full rounded-lg bg-black max-h-48"></video>
              <p id="video-preview-script" class="text-xs text-gray-500 italic hidden"></p>
              <div class="flex gap-2">
                <button id="btn-video-upload" onclick="panelUploadVideo('${item.item_id}', '${escHtml(item.sku || '')}')"
                  class="flex-1 bg-green-600 hover:bg-green-700 text-white text-xs font-semibold py-2 rounded-lg transition">
                  📤 Subir a ML Clips
                </button>
                <button onclick="panelGenVideoFull('${item.item_id}')"
                  class="px-3 py-2 border border-blue-300 text-blue-700 text-xs font-medium rounded-lg hover:bg-blue-100 transition">
                  🔄 Regenerar
                </button>
              </div>
            </div>
            <p id="video-gen-result" class="text-xs hidden"></p>
          </div>

          <!-- Subir clip manualmente -->
          <div class="space-y-2">
            <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide">3. Subir UUID manual</p>
            <div class="flex gap-2">
              <input id="clip-uuid-input" type="text" placeholder="UUID del video (opcional)"
                class="flex-1 border rounded-lg px-3 py-2 text-xs font-mono focus:ring-2 focus:ring-purple-300">
              <button onclick="uploadClipFromInput('${item.item_id}', '${escHtml(item.sku)}')"
                class="bg-purple-600 hover:bg-purple-700 text-white text-xs font-semibold px-3 py-2 rounded-lg transition whitespace-nowrap">
                📤 Subir
              </button>
            </div>
            <p id="clip-upload-msg" class="text-xs hidden"></p>
          </div>

          <div class="bg-gray-50 rounded-xl p-3 text-xs text-gray-500 space-y-1">
            <p class="font-semibold text-gray-600">Requisitos ML Clips</p>
            <p>• Formato: 9:16 vertical (720×1280)</p>
            <p>• Duración: 10 – 60 segundos</p>
            <p>• Máximo: 280 MB  •  Moderación: 24–48 h</p>
          </div>
        </div>`;
    }

    else if (tab === 'imagenes') {
      const pics = item.pictures || [];
      c.innerHTML = `
        <div class="p-4 space-y-4">
          <p class="text-sm text-gray-600">${pics.length} foto${pics.length !== 1 ? 's' : ''}</p>
          <div class="grid grid-cols-3 gap-2">
            ${pics.map((p, i) => `
              <div class="relative group">
                <img src="${p.secure_url || p.url || ''}" class="w-full aspect-square object-cover rounded-lg bg-gray-100">
                <span class="absolute top-1 left-1 bg-black/60 text-white text-xs rounded px-1">${i + 1}</span>
              </div>`).join('')}
          </div>
          ${pics.length === 0 ? '<p class="text-sm text-gray-400 text-center py-4">Sin imágenes</p>' : ''}
          <p class="text-xs text-gray-400">Para gestión avanzada de imágenes usa la sección Inventario.</p>
        </div>`;
    }
  }

  function stockCard(label, qty, color) {
    const colors = {
      blue:  'bg-blue-50 text-blue-700',
      green: 'bg-green-50 text-green-700',
      gray:  'bg-gray-50 text-gray-600',
    };
    return `
      <div class="rounded-xl p-3 text-center ${colors[color] || 'bg-gray-50 text-gray-600'}">
        <p class="text-xs font-semibold uppercase tracking-wide opacity-70">${label}</p>
        <p class="text-2xl font-bold">${qty ?? 0}</p>
      </div>`;
  }

  // ── Panel Save ─────────────────────────────────────────────────────────────
  window.markDirty = function (field, value) {
    state.panelDirty[field] = value;
  };

  window.savePanel = async function () {
    const item = state.panelItem;
    if (!item || !Object.keys(state.panelDirty).length) {
      closePanel(); return;
    }

    const btn = document.getElementById('panel-save-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Guardando...'; }

    const dirty = state.panelDirty;
    const itemId = item.item_id;
    const errs = [];

    try {
      if (dirty.title !== undefined) {
        try {
          await apiFetch(`/api/items/${itemId}/title`, { method: 'PUT', body: { title: dirty.title } });
        } catch (e) { errs.push(`Título: ${e.message}`); }
      }
      if (dirty.price !== undefined) {
        try {
          await apiFetch(`/api/items/${itemId}/price`, { method: 'PUT', body: { price: dirty.price } });
        } catch (e) { errs.push(`Precio: ${e.message}`); }
      }
      if (dirty.description !== undefined) {
        try {
          await apiFetch(`/api/items/${itemId}/description`, { method: 'PUT', body: { description: dirty.description } });
        } catch (e) { errs.push(`Descripción: ${e.message}`); }
      }

      if (errs.length) {
        alert('Errores al guardar:\n' + errs.join('\n'));
      } else {
        toast('Guardado correctamente ✓', 'green');
        closePanel();
        loadItems();
        loadStats();
      }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Guardar'; }
    }
  };

  // ── Stock sync ─────────────────────────────────────────────────────────────
  window.syncStock = async function (itemId) {
    const qty = parseInt(document.getElementById('new-stock')?.value || '0', 10);
    if (isNaN(qty) || qty < 0) { alert('Cantidad inválida'); return; }
    try {
      await apiFetch(`/api/items/${itemId}/stock`, { method: 'PUT', body: { quantity: qty } });
      toast(`Stock actualizado a ${qty} ✓`, 'green');
      const detail = await apiFetch(`/api/productos/${itemId}`);
      state.panelItem = detail;
      renderPanelTab('stock');
      loadItems();
    } catch (e) { alert('Error: ' + e.message); }
  };

  // ── Toggle status ──────────────────────────────────────────────────────────
  window.toggleStatus = async function (itemId, newStatus) {
    if (!confirm(`¿${newStatus === 'paused' ? 'Pausar' : 'Activar'} este producto?`)) return;
    try {
      await apiFetch(`/api/items/${itemId}/status`, { method: 'PUT', body: { status: newStatus } });
      toast(`Producto ${newStatus === 'paused' ? 'pausado' : 'activado'} ✓`, 'green');
      loadItems(); loadStats();
    } catch (e) { alert('Error: ' + e.message); }
  };

  // ── Upload clip from panel ─────────────────────────────────────────────────
  window.uploadClipFromPanel = async function (itemId, videoId, sku) {
    const btn = event.target;
    btn.disabled = true; btn.textContent = '⏳ Subiendo...';
    const result = document.getElementById('clip-upload-result');
    try {
      const res = await apiFetch(`/api/productos/${itemId}/clip`, {
        method: 'POST',
        body: { video_id: videoId, sku: sku }
      });
      if (res.ok) {
        if (result) {
          result.classList.remove('hidden');
          result.innerHTML = `<div class="bg-green-50 border border-green-200 rounded-xl p-3 text-sm text-green-700">
            ✓ Clip subido correctamente. Estado: <strong>${res.status}</strong>
            ${res.clip_uuid ? `<br>UUID: <span class="font-mono text-xs">${res.clip_uuid}</span>` : ''}
          </div>`;
        }
        toast('Clip subido ✓', 'green');
        const detail = await apiFetch(`/api/productos/${itemId}`);
        state.panelItem = detail;
        renderPanelTab('video');
        loadItems();
      } else {
        throw new Error(res.error || 'Error desconocido');
      }
    } catch (e) {
      if (result) {
        result.classList.remove('hidden');
        result.innerHTML = `<div class="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-600">⚠ Error: ${e.message}</div>`;
      }
      btn.disabled = false; btn.textContent = '📤 Subir Clip a ML';
    }
  };

  // ── Upload clip from input field ───────────────────────────────────────────
  window.uploadClipFromInput = async function (itemId, sku) {
    const input = document.getElementById('clip-uuid-input');
    const msgEl = document.getElementById('clip-upload-msg');
    const videoId = input?.value.trim();
    if (!videoId) { if (msgEl) { msgEl.textContent = 'Ingresa el UUID del video'; msgEl.className = 'text-xs text-red-500'; msgEl.classList.remove('hidden'); } return; }
    if (msgEl) { msgEl.textContent = 'Subiendo clip...'; msgEl.className = 'text-xs text-gray-500'; msgEl.classList.remove('hidden'); }
    try {
      const res = await apiFetch(`/api/productos/${itemId}/clip`, { method: 'POST', body: { video_id: videoId, sku: sku } });
      if (res.ok) {
        toast('Clip subido ✓', 'green');
        if (msgEl) { msgEl.textContent = `✓ Clip subido. Estado: ${res.status}`; msgEl.className = 'text-xs text-green-600'; }
        const detail = await apiFetch(`/api/productos/${itemId}`);
        state.panelItem = detail;
        renderPanelTab('video');
        loadItems();
      } else {
        throw new Error(res.error || 'Error');
      }
    } catch (e) {
      if (msgEl) { msgEl.textContent = '⚠ Error: ' + e.message; msgEl.className = 'text-xs text-red-500'; }
    }
  };

  // ── Video script generation ─────────────────────────────────────────────────
  window.panelGenScript = async function (itemId, title) {
    const btn = document.getElementById('btn-gen-script');
    const scriptEl = document.getElementById('video-script');
    const statusEl = document.getElementById('script-status');
    if (!btn || !scriptEl) return;

    const item = state.panelItem;
    const brand = (item?.attributes || []).find(a => a.id === 'BRAND')?.value_name || '';
    const model = (item?.attributes || []).find(a => a.id === 'MODEL')?.value_name || '';

    btn.disabled = true; btn.textContent = 'Generando...';
    if (statusEl) { statusEl.textContent = 'La IA está escribiendo el guion...'; statusEl.classList.remove('hidden'); }
    scriptEl.value = '';

    try {
      const resp = await fetch('/api/sku-inventory/ai-improve', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ field: 'video_script', current_value: title, context: { brand, model, title } })
      });
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '', result = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n'); buffer = lines.pop();
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            if (data === '[DONE]') break;
            if (!data.startsWith('[ERROR]')) { result += data; scriptEl.value = result; }
          }
        }
      }
      if (statusEl) { statusEl.textContent = '✓ Guion generado — puedes editarlo antes de grabar'; statusEl.className = 'text-xs text-green-600'; }
    } catch (e) {
      if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.className = 'text-xs text-red-500'; }
    }
    btn.disabled = false; btn.textContent = '✦ Generar Guion';
  };

  // ── Full video generation pipeline ─────────────────────────────────────────
  // Stores the latest generated vid_id for upload confirmation
  let _pendingVidId = null;

  window.panelGenVideoFull = async function (itemId) {
    const item      = state.panelItem;
    const btn       = document.getElementById('btn-gen-video');
    const progEl    = document.getElementById('video-gen-progress');
    const stepEl    = document.getElementById('video-gen-step');
    const barEl     = document.getElementById('video-gen-bar');
    const resEl     = document.getElementById('video-gen-result');
    const prevWrap  = document.getElementById('video-preview-wrap');
    if (!btn || !item) return;

    _pendingVidId = null;

    const brand  = (item.attributes || []).find(a => a.id === 'BRAND')?.value_name || '';
    const model  = (item.attributes || []).find(a => a.id === 'MODEL')?.value_name || '';
    const size   = (item.attributes || []).find(a => a.id === 'VIDEO_SCREEN_SIZE' || a.id === 'SCREEN_SIZE')?.value_name || '';
    const script = document.getElementById('video-script')?.value?.trim() || '';

    // Always use the item's pictures as slideshow frames.
    // Pad to >= 3 to force the slideshow path (avoids Minimax/Replicate auth issues).
    let rawPics = (item.pictures || []).map(p => p.secure_url || p.url || '').filter(Boolean);
    while (rawPics.length < 3 && rawPics.length > 0) rawPics = [...rawPics, ...rawPics];
    const pics = rawPics.slice(0, 5);

    btn.disabled = true; btn.textContent = '⏳ Generando...';
    if (progEl) progEl.classList.remove('hidden');
    if (prevWrap) prevWrap.classList.add('hidden');
    if (resEl)  { resEl.classList.add('hidden'); resEl.textContent = ''; }

    const setStep = (text, pct) => {
      if (stepEl) stepEl.textContent = text;
      if (barEl)  barEl.style.width  = pct + '%';
    };

    const steps = [
      ['Generando guion y voz con IA...', 15],
      ['Descargando fotos del producto...', 35],
      ['Creando video con las fotos...', 55],
      ['Combinando audio y video...', 75],
      ['Finalizando...', 90],
    ];
    let stepIdx = 0;
    setStep(steps[0][0], steps[0][1]);
    const stepTimer = setInterval(() => {
      stepIdx = Math.min(stepIdx + 1, steps.length - 1);
      setStep(steps[stepIdx][0], steps[stepIdx][1]);
    }, 14000);

    try {
      const resp = await fetch('/api/lanzar/generate-video-commercial', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          brand,
          model,
          title:             item.title || '',
          category:          item.category_id || '',
          size:              size,
          first_frame_image: pics[0] || '',
          ai_image_urls:     pics,
        }),
      });
      clearInterval(stepTimer);
      setStep('Video listo — revisa antes de subir', 100);

      const d = await resp.json();

      if (d.error) {
        if (resEl) { resEl.textContent = '❌ ' + d.error; resEl.className = 'text-xs text-red-500'; resEl.classList.remove('hidden'); }
        if (progEl) progEl.classList.add('hidden');
        btn.disabled = false; btn.textContent = '🎬 Generar Video';
        return;
      }

      // Store vid_id for upload step
      const vidId = d.video_url ? d.video_url.split('/').pop() : null;
      _pendingVidId = vidId;

      // Fill script textarea if empty
      if (d.script) {
        const scriptEl = document.getElementById('video-script');
        if (scriptEl && !scriptEl.value.trim()) scriptEl.value = d.script;
        const prevScript = document.getElementById('video-preview-script');
        if (prevScript) { prevScript.textContent = d.script; prevScript.classList.remove('hidden'); }
      }

      // Show video preview
      const vidEl = document.getElementById('video-preview-el');
      if (vidEl && d.video_url) {
        vidEl.src = d.video_url;
        vidEl.load();
      }
      if (prevWrap) prevWrap.classList.remove('hidden');

      // Update upload button label
      const upBtn = document.getElementById('btn-video-upload');
      if (upBtn) upBtn.textContent = `📤 Subir a ML Clips${d.has_audio ? ' (con voz)' : ''}`;

      btn.disabled = false; btn.textContent = '🎬 Generar Video';
      if (progEl) progEl.classList.add('hidden');

    } catch (e) {
      clearInterval(stepTimer);
      if (resEl) { resEl.textContent = '❌ ' + e.message; resEl.className = 'text-xs text-red-500'; resEl.classList.remove('hidden'); }
      if (progEl) progEl.classList.add('hidden');
      btn.disabled = false; btn.textContent = '🎬 Generar Video';
    }
  };

  // ── Upload confirmed video to ML ────────────────────────────────────────────
  window.panelUploadVideo = async function (itemId, sku) {
    const btn  = document.getElementById('btn-video-upload');
    const resEl = document.getElementById('video-gen-result');
    if (!_pendingVidId) {
      if (resEl) { resEl.textContent = '❌ No hay video generado — genera uno primero'; resEl.className = 'text-xs text-red-500'; resEl.classList.remove('hidden'); }
      return;
    }
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Subiendo...'; }

    try {
      const upRes = await apiFetch(`/api/productos/${itemId}/clip`, {
        method: 'POST',
        body: { video_id: _pendingVidId, sku: sku || state.panelItem?.sku || '' },
      });

      if (upRes.ok || upRes.clip_uuid || upRes.status) {
        toast('Video subido a ML Clips ✓', 'green');
        if (resEl) {
          resEl.textContent = `✓ Video enviado a moderación. ${upRes.status ? 'Estado: ' + upRes.status : ''}`;
          resEl.className = 'text-xs text-green-600';
          resEl.classList.remove('hidden');
        }
        _pendingVidId = null;
        const detail = await apiFetch(`/api/productos/${itemId}`);
        state.panelItem = detail;
        renderPanelTab('video');
        loadItems();
      } else {
        throw new Error(upRes.error || 'Error al subir clip');
      }
    } catch (e) {
      if (resEl) { resEl.textContent = '❌ ' + e.message; resEl.className = 'text-xs text-red-500'; resEl.classList.remove('hidden'); }
      if (btn) { btn.disabled = false; btn.textContent = '📤 Subir a ML Clips'; }
    }
  };

  // ── Atributos: load all category attrs ─────────────────────────────────────
  async function panelLoadAttrs(item) {
    const listEl = document.getElementById('attrs-list');
    const loadEl = document.getElementById('attrs-loading');
    const catId  = item.category_id;
    if (!catId) {
      if (loadEl) loadEl.textContent = 'No se encontró categoria del producto.';
      return;
    }
    // Build a map of existing attr values
    const existingMap = {};
    (item.attributes || []).forEach(a => { if (a.id) existingMap[a.id] = a; });

    try {
      const data = await apiFetch(`/api/sku-inventory/category-attributes/${catId}`);
      const all = [...(data.required || []), ...(data.recommended || []), ...(data.optional || [])];
      if (!all.length) { if (loadEl) loadEl.textContent = 'Sin atributos de categoría.'; return; }

      let html = '';
      let missingCount = 0;

      all.forEach(attr => {
        const existing = existingMap[attr.id];
        const val = existing?.value_name || '';
        const isMissing = !val;
        if (isMissing) missingCount++;
        const labelClass = isMissing ? 'text-red-500 font-medium' : 'text-gray-500';
        const inputClass = isMissing ? 'border-red-200 bg-red-50' : 'border-gray-200';
        const badge = isMissing
          ? (attr.required ? '<span class="text-[9px] bg-red-100 text-red-600 rounded px-1 py-0.5 ml-1">req</span>' : '')
          : '';

        html += `<div class="flex items-center gap-2">
          <label class="text-xs ${labelClass} w-32 shrink-0 truncate" title="${escHtml(attr.name || attr.id)}">${escHtml(attr.name || attr.id)}${badge}</label>`;

        if (attr.values && attr.values.length > 0) {
          html += `<select data-panel-attr-id="${escHtml(attr.id)}" class="panel-attr-input flex-1 border ${inputClass} rounded px-2 py-1.5 text-xs">
            <option value="">Seleccionar...</option>`;
          attr.values.forEach(v => {
            const sel = (existing?.value_id === v.id || val === v.name) ? 'selected' : '';
            html += `<option value="${escHtml(v.id)}" data-name="${escHtml(v.name)}" ${sel}>${escHtml(v.name)}</option>`;
          });
          html += `</select>`;
        } else {
          html += `<input type="text" data-panel-attr-id="${escHtml(attr.id)}" value="${escHtml(val)}"
            placeholder="${isMissing ? 'Vacío' : ''}"
            class="panel-attr-input flex-1 border ${inputClass} rounded px-2 py-1.5 text-xs">`;
        }
        html += `</div>`;
      });

      if (loadEl) loadEl.classList.add('hidden');
      if (listEl) {
        listEl.innerHTML = `<p class="text-xs text-gray-400 mb-2">${all.length} atributos — <span class="text-red-500 font-medium">${missingCount} vacíos</span></p>` + html;
        listEl.classList.remove('hidden');
      }
    } catch (e) {
      if (loadEl) loadEl.textContent = 'Error cargando atributos: ' + e.message;
    }
  }

  // ── Atributos: AI fill ──────────────────────────────────────────────────────
  window.panelAiAttrs = async function (itemId) {
    const item = state.panelItem;
    const msgEl = document.getElementById('attrs-msg');
    const emptyInputs = Array.from(document.querySelectorAll('.panel-attr-input')).filter(el => !el.value.trim());
    const emptyIds = emptyInputs.map(el => el.getAttribute('data-panel-attr-id')).filter(Boolean);
    if (!emptyIds.length) { if (msgEl) { msgEl.textContent = 'Todos los atributos tienen valor'; msgEl.className = 'text-xs text-gray-500'; msgEl.classList.remove('hidden'); } return; }

    const brand = (item?.attributes || []).find(a => a.id === 'BRAND')?.value_name || '';
    const model = (item?.attributes || []).find(a => a.id === 'MODEL')?.value_name || '';
    const title = item?.title || '';

    if (msgEl) { msgEl.textContent = `Consultando IA para ${emptyIds.length} atributos vacíos...`; msgEl.className = 'text-xs text-purple-600'; msgEl.classList.remove('hidden'); }

    try {
      const resp = await apiFetch('/api/sku-inventory/ai-improve', {
        method: 'POST',
        body: { field: 'attributes', current_value: emptyIds.join(', '), context: { brand, model, title } }
      });
      if (resp.result) {
        const match = resp.result.trim().match(/\[[\s\S]*\]/);
        if (match) {
          const suggestions = JSON.parse(match[0]);
          let filled = 0;
          suggestions.forEach(s => {
            const inp = document.querySelector(`.panel-attr-input[data-panel-attr-id="${s.id}"]`);
            if (inp && !inp.value.trim() && s.value_name) {
              inp.value = s.value_name;
              inp.classList.add('border-purple-300', 'bg-purple-50');
              filled++;
            }
          });
          if (msgEl) { msgEl.textContent = `✓ ${filled} atributos rellenados con IA — revisa y guarda`; msgEl.className = 'text-xs text-purple-600'; }
        } else {
          if (msgEl) { msgEl.textContent = 'No se pudieron parsear sugerencias'; msgEl.className = 'text-xs text-red-500'; }
        }
      } else {
        if (msgEl) { msgEl.textContent = resp.error || 'Error'; msgEl.className = 'text-xs text-red-500'; }
      }
    } catch (e) {
      if (msgEl) { msgEl.textContent = 'Error: ' + e.message; msgEl.className = 'text-xs text-red-500'; }
    }
  };

  // ── Atributos: save ─────────────────────────────────────────────────────────
  window.panelSaveAttrs = async function (itemId) {
    const msgEl = document.getElementById('attrs-msg');
    const inputs = document.querySelectorAll('.panel-attr-input');
    const attrs = [];
    inputs.forEach(el => {
      const id = el.getAttribute('data-panel-attr-id');
      const val = el.value.trim();
      if (id && val) {
        if (el.tagName === 'SELECT') {
          const opt = el.options[el.selectedIndex];
          attrs.push({ id, value_id: val, value_name: opt?.getAttribute('data-name') || opt?.textContent?.trim() || val });
        } else {
          attrs.push({ id, value_name: val });
        }
      }
    });
    if (!attrs.length) { if (msgEl) { msgEl.textContent = 'Sin atributos para guardar'; msgEl.className = 'text-xs text-gray-500'; msgEl.classList.remove('hidden'); } return; }
    if (msgEl) { msgEl.textContent = 'Guardando...'; msgEl.className = 'text-xs text-gray-500'; msgEl.classList.remove('hidden'); }
    try {
      await apiFetch(`/api/items/${itemId}/attributes`, { method: 'PUT', body: { attributes: attrs } });
      toast(`${attrs.length} atributos guardados ✓`, 'green');
      if (msgEl) { msgEl.textContent = `✓ ${attrs.length} atributos guardados`; msgEl.className = 'text-xs text-green-600'; }
      const detail = await apiFetch(`/api/productos/${itemId}`);
      state.panelItem = detail;
      loadItems();
    } catch (e) {
      if (msgEl) { msgEl.textContent = '⚠ Error: ' + e.message; msgEl.className = 'text-xs text-red-500'; }
    }
  };

  // ── Utils ──────────────────────────────────────────────────────────────────
  function show(id) { document.getElementById(id)?.classList.remove('hidden'); }
  function hide(id) { document.getElementById(id)?.classList.add('hidden'); }
  function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
  function fmt(n) { return Number(n).toLocaleString('es-MX', { minimumFractionDigits: 0, maximumFractionDigits: 0 }); }
  function escHtml(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  async function apiFetch(url, opts = {}) {
    const { method = 'GET', body } = opts;
    const res = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  function toast(msg, color = 'green') {
    const t = document.createElement('div');
    const cls = color === 'green' ? 'bg-green-600' : 'bg-red-600';
    t.className = `fixed bottom-5 right-5 ${cls} text-white text-sm px-4 py-2.5 rounded-xl shadow-lg z-[9999] transition-opacity`;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 400); }, 3000);
  }

})();
