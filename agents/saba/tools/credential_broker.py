"""Capability-scoped secret use: never return raw secrets to the agent."""
class CredentialBroker:
    def __init__(self, vault, permissions, audit):
        self.vault = vault
        self.permissions = permissions
        self.audit = audit

    def use(self, capability, operation):
        self.permissions.require(capability)
        secret = self.vault.get(capability)
        try:
            return operation(secret)
        finally:
            self.audit.record("credential_used", capability=capability)
