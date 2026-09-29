"""Connectors: the apps an operations team works in, reached over MCP.

Each connector is a separate thing the person consents to, the first time a run
wants it, and only if the administrator's policy allows it (see
``permissions.PermissionBroker``). Every tool a connector offers is *declared*:
what it reads, what it changes, and which effects that has. Undeclared tools are
not offered to the model, and declared effects gate deterministically. What the
connector returns is untrusted data, fenced and screened like a file or a page.
"""
