"""The in-sandbox ``manor`` CLI and the server side it calls back into.

``manor_cli`` is the stdlib-only executable installed in the sandbox image;
``tokens`` mints and resolves the short-lived token one bash command hands
it; ``operations`` runs each CLI request through the same file tools and ACL
services the agent's own tools use.
"""
