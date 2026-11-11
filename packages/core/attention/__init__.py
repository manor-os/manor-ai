"""Attention items: the watch layer between platform state and telling anyone.

Watchers (``packages.core.attention.watchers``) are deterministic and read
only. Each returns the situations that exist right now; ``reconcile`` turns
that into open / updated / resolved ``AttentionItem`` rows. Watchers send
nothing; ``delivery`` tells people about the kinds it has taken over from the
older senders, behind the ``attention_delivery`` flag.
"""
from packages.core.attention.contracts import DesiredItem, Watcher
from packages.core.attention.query import list_open_attention
from packages.core.attention.reconcile import reconcile_watcher
from packages.core.attention.registry import registered_watchers

__all__ = [
    "DesiredItem",
    "Watcher",
    "list_open_attention",
    "reconcile_watcher",
    "registered_watchers",
]
