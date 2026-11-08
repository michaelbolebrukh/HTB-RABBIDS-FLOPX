"""This instance's question flows: FLOPX registers NONE.

The platform registry (platform/question_flows.py) is populated at import time
by the product; an empty product module is the whole of "no flows". Not
registering is safe by construction: the registry's readers
(`get_flow`/`all_flows` in the platform routers and the reminder sweep) treat
an unknown key as a 404 and an empty registry as nothing to sweep, and
`start_question_flow_sweep_loop` returns None when no registered flow declares
reminder stages or lead capture. The frontend disables onboarding on its side.

This module is still imported from product/startup/__init__.py so the seam is
exercised: a future flow is added here and nowhere else.
"""

# Deliberately no register_flow() call.
