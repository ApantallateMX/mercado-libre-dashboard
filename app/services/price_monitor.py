"""
BinManager Price Monitor
========================
Monitorea en background el RetailPrice PH (LastRetailPricePurchaseHistory)
de SKUs configurados. Detecta cambios y notifica via SSE en tiempo real.

Configuración (env vars):
  BM_WATCHED_SKUS   = "SNTV007283,SNTV007822,SNTV001864,SNTV001764"
  BM_POLL_INTERVAL  = 300   (segundos, default 5 minutos)
"""
import asyncio
import logging
import os
from asyncio import Queue
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from app.services.binmanager_client import BinManagerClient

logger = logging.getLogger(__name__)

_DEFAULT_SKUS = ["SNTV007283", "SNTV007822", "SNTV001864", "SNTV001764"]
_POLL_INTERVAL = int(os.getenv("BM_POLL_INTERVAL", "300"))  # 5 min default


@dataclass
class PriceRecord:
    sku: str
    price: Optional[float]
    last_checked: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_changed: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "price": self.price,
            "last_checked": self.last_checked.isoformat(),
            "last_changed": self.last_changed.isoformat(),
        }


@dataclass
class PriceChange:
    sku: str
    old_price: Optional[float]
    new_price: Optional[float]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        delta = None
        if self.old_price is not None and self.new_price is not None:
            delta = round(self.new_price - self.old_price, 2)
        return {
            "sku": self.sku,
            "old_price": self.old_price,
            "new_price": self.new_price,
            "delta": delta,
            "timestamp": self.timestamp.isoformat(),
        }


class PriceMonitor:
    """
    Monitor de precios BinManager.

    - Hace poll cada BM_POLL_INTERVAL segundos
    - Compara precio nuevo vs. último registrado
    - Si cambia: guarda en historial y emite SSE a todos los subscribers
    - Si se configura con set_cache(), lee de caché local en vez de BM (cero hits)
    """

    def __init__(self):
        self._client = BinManagerClient()
        self._prices: Dict[str, PriceRecord] = {}
        self._history: List[PriceChange] = []
        self._subscribers: Set[Queue] = set()
        self._watched: List[str] = self._load_skus()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ext_cache: Optional[dict] = None  # {sku: (ts, price_usd)}

    def set_cache(self, cache: dict):
        """
        Conecta el monitor a la caché local de retail prices (dict {sku: (ts, price_usd)}).
        Cuando está configurado, _check_prices lee de memoria — cero hits a BinManager.
        """
        self._ext_cache = cache
        logger.info(f"PriceMonitor: usando caché local ({len(cache)} SKUs) — sin hits a BM")

    def _load_skus(self) -> List[str]:
        env = os.getenv("BM_WATCHED_SKUS", "").strip()
        if env:
            return [s.strip().upper() for s in env.split(",") if s.strip()]
        return list(_DEFAULT_SKUS)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return
        self._running = True
        if self._ext_cache is None:
            # Solo hace login a BM si no hay caché local configurada
            if not await self._client.login():
                logger.warning("PriceMonitor: BinManager login falló — reintento en primer poll")
        # Fetch inicial antes de arrancar el loop
        await self._check_prices(initial=True)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"PriceMonitor iniciado — {len(self._watched)} SKUs, "
            f"intervalo {_POLL_INTERVAL}s, "
            f"fuente={'caché local' if self._ext_cache is not None else 'BinManager live'}"
        )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._ext_cache is None:
            await self._client.close()
        logger.info("PriceMonitor detenido")

    async def _poll_loop(self):
        while self._running:
            try:
                await asyncio.sleep(_POLL_INTERVAL)
                await self._check_prices()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"PriceMonitor poll error: {e}")

    # ── Core logic ───────────────────────────────────────────────────────────

    async def _check_prices(self, initial: bool = False):
        """Detecta cambios de precio. Lee de caché local si está disponible, si no llama a BM."""
        now = datetime.now(timezone.utc)
        for sku in list(self._watched):
            try:
                if self._ext_cache is not None:
                    # Lee de _bm_retail_ph_cache — cero hits a BinManager
                    entry = self._ext_cache.get(sku)
                    price = entry[1] if entry and entry[1] > 0 else None
                else:
                    price = await self._client.get_retail_price_ph(sku)
                existing = self._prices.get(sku)

                if existing is None:
                    # Primera vez que vemos este SKU
                    self._prices[sku] = PriceRecord(sku=sku, price=price,
                                                     last_checked=now, last_changed=now)
                    continue

                existing.last_checked = now

                if not initial and price != existing.price:
                    # Cambio detectado
                    change = PriceChange(
                        sku=sku,
                        old_price=existing.price,
                        new_price=price,
                        timestamp=now,
                    )
                    logger.info(
                        f"Cambio de precio: {sku} "
                        f"${existing.price} -> ${price}"
                    )
                    existing.price = price
                    existing.last_changed = now
                    self._history.append(change)
                    self._history = self._history[-200:]  # máx 200 cambios
                    await self._broadcast(change)
                else:
                    existing.price = price

            except Exception as e:
                logger.error(f"PriceMonitor error SKU {sku}: {e}")

    async def _broadcast(self, change: PriceChange):
        """Envía el cambio a todos los subscribers SSE conectados."""
        dead: Set[Queue] = set()
        for q in self._subscribers:
            try:
                q.put_nowait(change.to_dict())
            except asyncio.QueueFull:
                dead.add(q)
            except Exception:
                dead.add(q)
        self._subscribers -= dead

    # ── SSE subscriptions ────────────────────────────────────────────────────

    def subscribe(self) -> Queue:
        q: Queue = Queue(maxsize=50)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: Queue):
        self._subscribers.discard(q)

    # ── Public API ───────────────────────────────────────────────────────────

    def get_current_prices(self) -> List[dict]:
        """Retorna precios actuales de todos los SKUs watcheados."""
        return [rec.to_dict() for rec in self._prices.values()]

    def get_price_history(self, limit: int = 50) -> List[dict]:
        """Retorna los últimos cambios detectados (más reciente primero)."""
        return [c.to_dict() for c in reversed(self._history[-limit:])]

    def add_sku(self, sku: str) -> bool:
        sku = sku.upper()
        if sku not in self._watched:
            self._watched.append(sku)
            return True
        return False

    def remove_sku(self, sku: str) -> bool:
        sku = sku.upper()
        if sku in self._watched:
            self._watched.remove(sku)
            self._prices.pop(sku, None)
            return True
        return False

    def get_watched_skus(self) -> List[str]:
        return list(self._watched)

    @property
    def poll_interval(self) -> int:
        return _POLL_INTERVAL


# Singleton global — importado por main.py y por el router
price_monitor = PriceMonitor()
