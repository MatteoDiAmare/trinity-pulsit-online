"""Interface for an external secrets provider.

No master keys or secret material are stored in this repository.
"""
class VaultClient:
    def __init__(self, provider):
        self.provider = provider

    def get(self, capability):
        return self.provider.get_secret(capability)
