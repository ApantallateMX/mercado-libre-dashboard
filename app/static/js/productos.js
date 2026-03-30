/**
 * productos.js — Ciclo de vida unificado de publicaciones MeLi
 * ─────────────────────────────────────────────────────────────
 * Gestiona la tabla principal, tabs, búsqueda, panel lateral
 * y todas las acciones (editar, stock, video clip, imágenes).
 */
(function () {
  'use strict';

  // ── Estado global ──────────────────────────────────────────────────────────
  const state = {
    tab:        'all',       // all | active | paused | candidates
    q:          '',
    offset:     0,
    limit:      50,
    total:      0,
    items:      [],
    loading:    false,
    // Panel
    panelItem:  null,        // item detail loaded for panel
    panelTab:   'editar',
    panelDirty: {},          // cambios pendientes {field: value}
    // Video
    currentVideoId: null,
  };

  // ── Init ───────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    loadStats();
    loadItems();
  });

  // ── Stats ──────────────────────────────────────────────────────────────────
  async function loadStats() {
    try {
      const d = await apiFetch('/api/productos/stats');
      setText('stat-active',     d.active     ?? '—');
      setText('stat-paused',     d.paused     ?? '—');
      setText('stat-candidates', d.candidates ?? '—');
      setText('stat-total',      d.total      ?? '—');
      setText('cnt-all',         d.total      ?? '—');
      setText('cnt-active',      d.active     ?? '—');
      setText('cnt-paused',      d.paused     ?? '—');
      setText('cnt-candidates',  d.candidates ?? '—');
    } catch (e) { console.warn('stats error', e); }
  }

  // ── Items list ─────────────────────────────────────────────────────────────
  async function loadItems() {
    if (state.loading) return;
    state.loading = true;
    show('table-loading'); hide('table-wrap');

    try {
      let data;
      if (state.tab === 'candidates') {
        data = await apiFetch(
          `/api/productos/candidates?q=${encodeURIComponent(state.q)}&offset=${state.offset}&limit=${state.limit}`
        );
      } else {
        const st = state.tab === 'all' ? 'all' : state.tab;
        data = await apiFetch(
          `/api/productos?status=${st}&q=${encodeURIComponent(state.q)}&offset=${state.offset}&limit=${state.limit}`
        );
      }
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
      return `<button onclick="launchSku('${escHtml(item.sku)}')"
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
    const toggleBtn = item.status === 'paused'
      ? `<button onclick="toggleStatus('${itemId}', 'active')"
           class="text-xs px-2 py-1 rounded-lg border text-green-600 border-green-200 hover:bg-green-50 transition">▶ Activar</button>`
      : `<button onclick="toggleStatus('${itemId}', 'paused')"
           class="text-xs px-2 py-1 rounded-lg border text-orange-500 border-orange-200 hover:bg-orange-50 transition">⏸ Pausar</button>`;
    return `<div class="flex flex-col gap-1">
      <button onclick="openPanel('${itemId}')"
        class="text-xs px-2 py-1 rounded-lg bg-yellow-100 hover:bg-yellow-200 text-yellow-800 font-medium transition">✏ Editar</button>
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
    loadItems();
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

    // Show panel immediately with preloaded data
    showPanel();
    renderPanelHeader(preloaded || {});
    renderPanelSkeleton();

    try {
      const detail = await apiFetch(`/api/productos/${itemId}`);
      state.panelItem = detail;
      renderPanelHeader(detail);
      renderPanelTab('editar');
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
        </div>`;
      // Title counter
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

    else if (tab === 'video') {
      const hasVideo  = item.has_clip_video;
      const clipReady = item.video_id;
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

          <div>
            <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Generar nuevo video</p>
            <a href="/sku-inventory" class="block text-center bg-yellow-400 hover:bg-yellow-500 text-gray-800 font-semibold text-sm py-2.5 rounded-xl transition">
              🚀 Ir a Lanzar → generar video
            </a>
            <p class="text-xs text-gray-400 mt-2 text-center">Genera el video en Lanzar y luego regresa aquí para subirlo.</p>
          </div>

          <div class="bg-gray-50 rounded-xl p-3 text-xs text-gray-500 space-y-1">
            <p class="font-semibold text-gray-600">Requisitos ML Clips</p>
            <p>• Formato: 9:16 vertical (720×1280)</p>
            <p>• Duración: 10 – 60 segundos</p>
            <p>• Máximo: 280 MB</p>
            <p>• Moderación: 24–48 horas</p>
          </div>

          <div id="clip-upload-result" class="hidden"></div>
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
      // Title
      if (dirty.title !== undefined) {
        try {
          await apiFetch(`/api/items/${itemId}/title`, { method: 'PUT', body: { title: dirty.title } });
        } catch (e) { errs.push(`Título: ${e.message}`); }
      }
      // Price
      if (dirty.price !== undefined) {
        try {
          await apiFetch(`/api/items/${itemId}/price`, { method: 'PUT', body: { price: dirty.price } });
        } catch (e) { errs.push(`Precio: ${e.message}`); }
      }
      // Description
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
      // Reload panel
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
        // Reload panel
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

  // ── Launch SKU ─────────────────────────────────────────────────────────────
  window.launchSku = function (sku) {
    window.location.href = `/sku-inventory?sku=${encodeURIComponent(sku)}`;
  };

  // ── Lanzar wizard modal ────────────────────────────────────────────────────
  window.openLanzarWizard = function () {
    show('lanzar-overlay');
  };
  window.closeLanzarWizard = function () {
    hide('lanzar-overlay');
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
