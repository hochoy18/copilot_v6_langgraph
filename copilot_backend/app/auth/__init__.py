"""Auth-domain layer (T07 / #8 onward).

The repository layer is a thin Mongo wrapper. Everything that depends
on *domain knowledge* — opaque-token format, hash scheme, rotation
rules, reuse detection — lives here. Putting the auth rules in one
module keeps the rotation invariant readable in one place: change the
auth contract here, every router that depends on it follows automatically.

Submodules:

* `tokens`  — `RefreshTokenService`: issue / verify / rotate / revoke.
* `errors`  — auth-domain `AppError` subclasses (T07 #8).

Routers (login, refresh, logout, OIDC) land in T08 (#9); they
depend on the service surface and stay thin.
"""
from __future__ import annotations
