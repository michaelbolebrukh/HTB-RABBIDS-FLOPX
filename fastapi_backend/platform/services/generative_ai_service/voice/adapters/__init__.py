"""
Voice adapters — the sanctioned provider seams (Phase 6, D39).

These modules (and ONLY these in the voice package) may import provider SDKs
and speak provider wire formats; everything crossing back out is our
VoiceEvent. Imported lazily by the port so an SDK is only loaded when its
provider actually serves a session.
"""
