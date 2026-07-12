"""Provider registry (ADR-004).

The ONLY package allowed to import vendor SDKs / hit vendor HTTP. Everything else depends
on the interfaces in ``base`` and goes through :class:`registry.ProviderRegistry`.
"""
