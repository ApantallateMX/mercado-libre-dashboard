(function () {
    var _prevCounts = null;
    var _pollInterval = 30000;
    var _timerId = null;

    function pollCounts() {
        fetch('/api/health/counts')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.ok) return;

                if (_prevCounts === null) {
                    // Primera carga: guardar sin mostrar toast
                    _prevCounts = {
                        unanswered_questions: data.unanswered_questions,
                        open_claims: data.open_claims,
                        unread_messages: data.unread_messages
                    };
                    updateBadge(data.total);
                    return;
                }

                // Calcular deltas
                var dq = data.unanswered_questions - _prevCounts.unanswered_questions;
                var dc = data.open_claims - _prevCounts.open_claims;
                var dm = data.unread_messages - _prevCounts.unread_messages;

                var items = [];
                if (dq > 0) items.push(dq + (dq === 1 ? ' nueva pregunta' : ' nuevas preguntas'));
                if (dc > 0) items.push(dc + (dc === 1 ? ' nuevo reclamo' : ' nuevos reclamos'));
                if (dm > 0) items.push(dm + (dm === 1 ? ' nuevo mensaje' : ' nuevos mensajes'));

                if (items.length > 0) {
                    showToast(items);
                    tryPlaySound();
                }

                updateBadge(data.total);
                _prevCounts = {
                    unanswered_questions: data.unanswered_questions,
                    open_claims: data.open_claims,
                    unread_messages: data.unread_messages
                };
            })
            .catch(function () { /* silencioso */ });
    }

    function updateBadge(total) {
        var badge = document.getElementById('health-badge');
        if (!badge) return;
        if (total > 0) {
            badge.textContent = total > 99 ? '99+' : total;
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    }

    function showToast(items) {
        var container = document.getElementById('notif-container');
        if (!container) return;

        var toast = document.createElement('div');
        toast.className = 'bg-white rounded-lg shadow-lg border-l-4 border-yellow-400 p-4 flex items-start gap-3 transition-all duration-300 opacity-0 translate-x-4';
        toast.innerHTML =
            '<span class="text-yellow-500 text-lg flex-shrink-0">&#x1F514;</span>' +
            '<div class="flex-1 min-w-0">' +
                '<p class="text-sm font-semibold text-gray-800">Nuevas notificaciones</p>' +
                '<p class="text-xs text-gray-600">' + items.join(', ') + '</p>' +
            '</div>' +
            '<a href="/health" class="text-xs bg-yellow-400 hover:bg-yellow-500 px-3 py-1 rounded font-semibold flex-shrink-0 text-gray-800 no-underline">Ver</a>' +
            '<button class="text-gray-400 hover:text-gray-600 flex-shrink-0 notif-dismiss">&times;</button>';

        container.appendChild(toast);

        // Animar entrada
        requestAnimationFrame(function () {
            toast.classList.remove('opacity-0', 'translate-x-4');
            toast.classList.add('opacity-100', 'translate-x-0');
        });

        // Dismiss button
        toast.querySelector('.notif-dismiss').addEventListener('click', function () {
            dismissToast(toast);
        });

        // Auto-dismiss en 8 segundos
        setTimeout(function () {
            dismissToast(toast);
        }, 8000);
    }

    function dismissToast(toast) {
        if (!toast || !toast.parentNode) return;
        toast.classList.add('opacity-0', 'translate-x-4');
        setTimeout(function () {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 300);
    }

    function tryPlaySound() {
        // Solo si el usuario ya interactuo con la pagina
        try {
            var ctx = new (window.AudioContext || window.webkitAudioContext)();
            var osc = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.frequency.value = 880;
            osc.type = 'sine';
            gain.gain.value = 0.08;
            osc.start();
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
            osc.stop(ctx.currentTime + 0.3);
        } catch (e) { /* sin sonido */ }
    }

    function startPolling() {
        pollCounts();
        _timerId = setInterval(pollCounts, _pollInterval);
    }

    function stopPolling() {
        if (_timerId) {
            clearInterval(_timerId);
            _timerId = null;
        }
    }

    document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
            stopPolling();
        } else {
            pollCounts(); // poll inmediato al volver
            stopPolling(); // limpiar si habia timer
            _timerId = setInterval(pollCounts, _pollInterval);
        }
    });

    // Iniciar al cargar
    startPolling();
})();
