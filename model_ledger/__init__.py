"""model.ledger: AI spend accounting that finance can audit.

The 0.1 classes (``Ledger``, ``Price``, ``Usage``, ``UnknownModel``) are unchanged. The 0.2 additions (``AuditLedger`` and friends) price
every call in exact decimals against an effective-dated price book, chain the records so any later change is detectable, allocate shared costs
to the cent, forecast budgets, flag spikes robustly, explain changes as a price-volume-mix bridge that adds up exactly, and reconcile the
ledger to a provider invoice with every dollar of difference assigned to a cause."""
from ._version import __version__
from .anomaly import Spike, spikes
from .bridge import Bridge, bridge, bridge_items
from .budget import Budget, BudgetStatus, make_budget, status
from .ledger import AuditLedger, DuplicateEvent, Event, make_event
from .money import MoneyError, allocate, dec, quantize
from .pricing import NoPriceOnDate, PriceBook, Rate, make_rate, parse_book
from .reconcile import InvoiceLine, Reconciliation, parse_invoice, reconcile
from .scenario import analyze, events_from_csv, fingerprint, loads
from .shared import Shared, allocate_shared, fully_loaded, make_shared
from .simple import Ledger, Price, UnknownModel, Usage

__all__ = [
    "AuditLedger", "Bridge", "Budget", "BudgetStatus", "DuplicateEvent", "Event", "InvoiceLine", "Ledger", "MoneyError", "NoPriceOnDate",
    "Price", "PriceBook", "Rate", "Reconciliation", "Shared", "Spike", "UnknownModel", "Usage", "__version__", "allocate", "allocate_shared",
    "analyze", "bridge", "bridge_items", "dec", "events_from_csv", "fingerprint", "fully_loaded", "loads", "make_budget", "make_event",
    "make_rate", "make_shared", "parse_book", "parse_invoice", "quantize", "reconcile", "spikes", "status",
]
