"""
Media adapters — provider REST seams (Phase 7, D43).

All httpx-only (no provider SDKs — the D39 SDK quarantine stays exactly where
P6 drew it). Each lane is dormant without its key; the port's validation is
what rejects models whose lane is unavailable, loudly, before any call.
"""
