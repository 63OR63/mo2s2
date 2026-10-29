"""Lazy Qt import so the mapping core can be tested without a MO2 runtime."""

def __getattr__(name):
    """Import the optional Qt widget lazily when requested by the host."""
    if name == 'Mo2S2VirtualPaksWidget':
        from .widget import Mo2S2VirtualPaksWidget
        return Mo2S2VirtualPaksWidget
    raise AttributeError(name)
