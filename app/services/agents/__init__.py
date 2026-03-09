# app/services/agents/__init__.py
# Agent system module — exporta todos los agentes especializados.

from app.services.agents.base import BaseAgent, AgentResult
from app.services.agents.sales_agent import SalesAgent
from app.services.agents.inventory_agent import InventoryAgent
from app.services.agents.pricing_agent import PricingAgent
from app.services.agents.health_agent import HealthAgent
from app.services.agents.ads_agent import AdsAgent
from app.services.agents.listing_agent import ListingAgent
from app.services.agents.qa_agent import QAAgent
from app.services.agents.alert_agent import AlertAgent


def build_agent_registry() -> dict:
    """
    Construye y retorna el diccionario de agentes instanciados.
    AlertAgent recibe el resto de agentes para el full_scan orquestado.

    Returns:
        {
            "sales":     SalesAgent(),
            "inventory": InventoryAgent(),
            "pricing":   PricingAgent(),
            "health":    HealthAgent(),
            "ads":       AdsAgent(),
            "listing":   ListingAgent(),
            "qa":        QAAgent(),
            "alert":     AlertAgent(agents={...}),
        }
    """
    sales = SalesAgent()
    inventory = InventoryAgent()
    pricing = PricingAgent()
    health = HealthAgent()
    ads = AdsAgent()
    listing = ListingAgent()
    qa = QAAgent()

    alert = AlertAgent(agents={
        "sales": sales,
        "inventory": inventory,
        "pricing": pricing,
        "health": health,
        "ads": ads,
        "listing": listing,
        "qa": qa,
    })

    return {
        "sales": sales,
        "inventory": inventory,
        "pricing": pricing,
        "health": health,
        "ads": ads,
        "listing": listing,
        "qa": qa,
        "alert": alert,
    }


__all__ = [
    "BaseAgent",
    "AgentResult",
    "SalesAgent",
    "InventoryAgent",
    "PricingAgent",
    "HealthAgent",
    "AdsAgent",
    "ListingAgent",
    "QAAgent",
    "AlertAgent",
    "build_agent_registry",
]
