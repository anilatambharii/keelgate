# Liveness fixture only — it is NOT an authorisation policy.
#
# OPA starts happily with an empty bundle, but a dev server that answers a real
# query is easier to debug than one that 404s everything. The gating packs
# arrive with the policy engine itself.
package keelgate.health

# METADATA
# description: Always true; proves the bundle loaded and the server evaluates.
ready := true
