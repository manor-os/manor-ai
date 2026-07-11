"""Integrations package — bridges to external systems that don't fit
the OAuth + Integration model.

Currently houses:

  sessions/   Browser-session capture (Playwright storage_state) for
              sites with no usable API. Used by the M7 browser adapter.
"""
