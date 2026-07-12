"""Capture domain: pure logic for the capture pipeline (organizer + note writing).

Kept free of I/O and provider SDKs so it is unit-testable without mocks (08 testing policy).
The orchestration that calls these lives in ``app/services/capture_pipeline.py``.
"""
