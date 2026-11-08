"""FLOPX closed vocabularies. Persisted values are the `.value` strings."""

from enum import Enum


class ProvenanceKind(str, Enum):
    LIVE = "live"
    ARCHIVED = "archived"
    SAMPLE = "sample"
    DERIVED = "derived"


class ProviderKind(str, Enum):
    MARKETPLACE = "marketplace"
    NEOCLOUD = "neocloud"
    HYPERSCALER = "hyperscaler"


class Availability(str, Enum):
    AVAILABLE = "available"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"


class Contract(str, Enum):
    ON_DEMAND = "on-demand"
    RESERVED = "reserved"
    SPOT = "spot"


class FacilityStatus(str, Enum):
    OPERATING = "operating"
    UNDER_CONSTRUCTION = "under-construction"
    ANNOUNCED = "announced"
    PLANNED = "planned"
    REPORTED = "reported"


class IntelKind(str, Enum):
    ANNOUNCEMENT = "announcement"
    PRICE_MOVE = "price-move"
    SUPPLY_ANOMALY = "supply-anomaly"
    EARNINGS = "earnings"
    POWER_EVENT = "power-event"
    PREDICTION_REPRICE = "prediction-reprice"
    POLICY = "policy"
    LAUNCH = "launch"
    DIVERGENCE = "divergence"


class Classification(str, Enum):
    VERIFIED = "verified"
    INTERPRETATION = "interpretation"
    PREDICTION = "prediction"


class InterruptionTolerance(str, Enum):
    NONE = "none"
    LOW = "low"
    HIGH = "high"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class StrategyMode(str, Enum):
    BACKTEST = "backtest"
    FORWARD = "forward"


class DecisionAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
