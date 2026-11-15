# P2-SEC7 plan evidence

A throwaway Keycloak 26.0.7 (`quay.io/keycloak/keycloak:26.0.7 start-dev`) ran on this machine, bound to `127.0.0.1:8080`. No cloud account, no DNS, and no service outside that container. The bootstrap administrator password was generated for the run and was not written here. The provider logged in as that administrator. `TF_VAR_gateway_secret` was the literal `plan-only`; OpenTofu printed the client secret as `(sensitive value)`.

The plan used a copy of `applications/core/master-instance/base/module` with the Kubernetes backend removed, so state stayed in a temporary directory. `adopt_existing` was `false`. Nothing was applied.

| Case | Roster | Result |
|---|---|---|
| (a) [`a-unknown-uuid.txt`](a-unknown-uuid.txt) | `11111111-1111-1111-1111-111111111111` | Exit 1. `GET .../users/<id>/role-mappings` returns 404 `User not found`. `keycloak_user_roles` is absent. |
| (b) [`b-builtin-admin.txt`](b-builtin-admin.txt) | `b30b0bd1-859c-4fd4-9f12-73fdadb1def3`, the built-in `admin` user's id | Exit 0. `data.keycloak_role.admin` reads `75b9c6bb-a113-4a47-a6b8-13b18f28c02f`, the master realm role named `admin`. `keycloak_role.fabric_operator` is planned with `name = "fabric-operator"`. `keycloak_user_roles.operator["b30b0bd1-859c-4fd4-9f12-73fdadb1def3"]` will be created. `role_ids` is `(known after apply)` only because the new `fabric-operator` role's id is created in that same plan. |
| (c) [`c-recreated-username.txt`](c-recreated-username.txt) | old id `ed5fcffd-e54a-4221-babe-202b78b8bb3c` after username `reused` was deleted and created again as `2054e4bd-cda4-4f19-8ede-71b0e4e41a81` | Exit 1. The same 404 on the old id. The new user is not in the plan. |
