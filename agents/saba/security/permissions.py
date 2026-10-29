"""Default-deny capability policy."""
class PermissionDenied(Exception):
    pass


class Permissions:
    def __init__(self, allowed=()):
        self.allowed = frozenset(allowed)

    def require(self, capability):
        if capability not in self.allowed:
            raise PermissionDenied(f"Capability denied: {capability}")
