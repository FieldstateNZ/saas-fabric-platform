# The master realm's own instance resources -- not a client realm. ADR 0025 in
# the application repository: nothing here is made by hand any more. This
# module is run by ../job.yaml, a normal Argo CD-managed Job replaced on every
# sync (`Replace=true,Force=true`, not a hook -- see that file's own comment
# for why), with the credential ../../../keycloak-credentials generated and
# ../../../master-instance-credential generated for it, through the same
# OpenTofu + Keycloak provider mechanism the `hosting/` harness already uses
# for a client realm (hosting/src/SaaSFabric.Aspire.Hosting/Templates/main.tf
# in the application repository) -- no vault provider here, because the
# master realm holds no client secret partition.
terraform {
  required_version = ">= 1.12.0, < 2.0.0"

  required_providers {
    keycloak = {
      source  = "keycloak/keycloak"
      version = "5.9.0"
    }
  }

  # State for exactly this one module. The Job that runs it is replaced on
  # every sync, so state cannot live on the container's own filesystem -- it
  # has to outlive the pod that wrote it. A Kubernetes Secret is the backend
  # with the fewest moving parts available in-cluster: no object storage, no
  # database, nothing beyond the API server this Job already talks to.
  #
  # `namespace = "master-instance-state"`, not this Job's own
  # `operator-system`: `tofu init` calls this backend's `Workspaces()`
  # unconditionally (OpenTofu 1.12.6's `meta_backend.go`, `selectWorkspace`),
  # which issues a label-selector `list` against Secrets in this namespace --
  # even though this module never selects a workspace of its own. `list`
  # cannot be scoped by name and returns whole object bodies, so granting it
  # in `operator-system` would let this identity read every Secret's content
  # there, including `saas-fabric-gateway-oidc` and the mirrored
  # `keycloak-admin`. A namespace holding nothing but this state is what
  # keeps that unavoidable `list` from granting anything beyond itself. See
  # ../overlays/lucentroot/state-namespace.yaml and
  # ../overlays/lucentroot/state-rbac.yaml for the access this requires, and
  # this Application's README ("A Job replaced mid-run") for what happens to
  # this backend's lock if the Job is killed mid-apply.
  backend "kubernetes" {
    secret_suffix     = "master-instance"
    namespace         = "master-instance-state"
    in_cluster_config = true
  }
}

# Provider-native environment variables carry the endpoint and the credential:
# KEYCLOAK_URL, KEYCLOAK_CLIENT_ID (admin-cli), KEYCLOAK_USER, KEYCLOAK_PASSWORD
# -- the same shape the hosting/ harness already uses. Nothing is written here
# because nothing here is committed: the credential is the bootstrap
# administrator ../../../keycloak-credentials generates and never types, and
# it reaches this container as a Secret ../../master-instance-credential's
# sibling `ExternalSecret` mirrors from identity (see ../job.yaml).
provider "keycloak" {}

# The master realm itself: not created (Keycloak creates it the moment the
# cluster exists), but managed from here on, for one reason -- `frontendUrl`.
# Without it, Keycloak rejected a valid operator token on the internal Admin
# API with 401 while accepting the same token on the public origin, because
# the browser and this Job reach Keycloak at different addresses (see the
# control plane's README, "Identity and bootstrap prerequisites", for the
# incident this fixed). That was the platform's last hand-made master-realm
# step; this resource is what removes it.
#
# `import` (OpenTofu's own block, not this provider's `import` argument --
# `keycloak_realm` does not have one) brings the *existing* realm into state
# instead of attempting to create it, unconditionally: master exists in
# every environment before this module ever runs, so there is no "fresh vs
# adopted" branch to make here the way there is for the two clients below.
# The id is the realm's own name, never a UUID, and the block is a no-op
# once the realm is already in this module's state -- safe to leave in place
# on every apply, in every environment.
import {
  to = keycloak_realm.master
  id = "master"
}

# **This resource now owns the realm, and declares almost none of it --
# except where it declares all of it.** Two different guarantees, for two
# different parts of the schema, and they must not be confused:
#
#   every realm ARGUMENT except `attributes` is left alone.
#     `ignore_changes` lists every other argument the provider's schema
#     carries (`tofu providers schema -json`, keycloak/keycloak 5.9.0,
#     `keycloak_realm`), so nothing about session lifetimes, themes,
#     registration policy, or any other realm-level setting an operator
#     changes by hand through the (otherwise unreachable -- see
#     docs/architecture.md#the-administrative-control-plane) admin console
#     is ever reverted by this module.
#
#   every realm ATTRIBUTE except `frontendUrl` is removed on every apply.
#     `attributes` is Optional, not Computed, in this provider's schema, and
#     the provider replaces the realm's *whole* attribute map on apply
#     rather than merging into it (upstream keycloak/terraform-provider-
#     keycloak#1031) -- so `ignore_changes` cannot protect a key it never
#     declared, the way it protects an ignored argument. Whatever this
#     module sends as `attributes` becomes the realm's entire attribute set;
#     `{ frontendUrl = var.public_base_url }` is what it sends. Anything else
#     ever stored as a master-realm attribute -- by hand, through the admin
#     console, or by any other tool -- does not survive this module's next
#     apply unless it is declared here too.
#
# The trade-off stated plainly: managing `frontendUrl` at all is what
# removed the platform's last hand-made master-realm step (see this
# Application's README, "The realm itself"), and the alternative is the hand
# step the product owner's rule forbids -- so this module keeps it, with the
# blast radius above written down rather than left to be discovered.
resource "keycloak_realm" "master" {
  realm = "master"

  attributes = {
    frontendUrl = var.public_base_url
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      access_code_lifespan, access_code_lifespan_login, access_code_lifespan_user_action, access_token_lifespan,
      access_token_lifespan_for_implicit_flow, account_theme, action_token_generated_by_admin_lifespan, action_token_generated_by_user_lifespan,
      admin_permissions_enabled, admin_theme, browser_flow, client_authentication_flow,
      client_session_idle_timeout, client_session_max_lifespan, default_default_client_scopes, default_optional_client_scopes,
      default_signature_algorithm, direct_grant_flow, display_name, display_name_html,
      docker_authentication_flow, duplicate_emails_allowed, edit_username_allowed, email_theme,
      enabled, first_broker_login_flow, id, internal_id,
      internationalization, login_theme, login_with_email_allowed, oauth2_device_code_lifespan,
      oauth2_device_polling_interval, offline_session_idle_timeout, offline_session_max_lifespan, offline_session_max_lifespan_enabled,
      organizations_enabled, otp_policy, password_policy, refresh_token_max_reuse,
      registration_allowed, registration_email_as_username, registration_flow, remember_me,
      reset_credentials_flow, reset_password_allowed, revoke_refresh_token, security_defenses,
      smtp_server, ssl_required, sso_session_idle_timeout, sso_session_idle_timeout_remember_me,
      sso_session_max_lifespan, sso_session_max_lifespan_remember_me, terraform_deletion_protection, user_managed_access,
      verify_email, web_authn_passwordless_policy, web_authn_policy,
    ]
  }
}

# The master realm's own built-in administrator role. Not created -- it
# exists the moment Keycloak creates the realm -- so it is read, not managed.
# This is what ADR 0012's realm-creation-as-the-operator actually needs:
# `create-realm` alone grants the creator's *new* realm's roles into tokens
# minted afterwards, never master's own admin rights, so an operator who
# reconciles a client realm needs this held beforehand (docs/architecture/
# control-plane.md, "Identity and bootstrap prerequisites", application
# repository). It is composite over every client realm's own administration
# -- full Keycloak authority, not a scoped slice of it -- which is exactly
# what ADR 0012 needs and exactly why it is granted to declared operators
# only, below, and never to the control plane's own service identity.
data "keycloak_role" "admin" {
  realm_id = keycloak_realm.master.id
  name     = "admin"
}

# What the control plane's own OIDC posture checks (`required_role` in
# control-plane.toml). This role does not exist until something creates it --
# unlike `admin` above, it is entirely this platform's own, so it is never
# adopted, only created.
resource "keycloak_role" "fabric_operator" {
  realm_id = keycloak_realm.master.id
  name     = "fabric-operator"

  # LucentRoot's master realm predates this module (see the environment
  # config's own `adoptExisting` comment): this role was created by hand
  # before ADR 0025, and `import = true` -- this provider's own convenience
  # argument on create, distinct from OpenTofu's `import` block above --
  # looks it up by realm and name and adopts it instead of attempting a
  # second create that Keycloak would refuse with a 409. A fresh environment
  # has no such history, so `var.adopt_existing` defaults to `false` there
  # and this resource is created outright.
  #
  # **`import` is `ForceNew` on this provider.** Unlike the one-time,
  # history-only framing an earlier draft of this comment gave it,
  # `var.adopt_existing` must never change for an environment once set --
  # doing so plans a destroy-and-recreate of this resource (and of
  # `keycloak_openid_client.console` below), not a no-op. `prevent_destroy`
  # is what turns that into a loud plan failure instead of a silent
  # drop-and-409: without it here, this role would simply be removed from
  # state and re-created, and the second create is exactly the 409 this
  # module exists to avoid.
  import = var.adopt_existing

  lifecycle {
    prevent_destroy = true
  }
}

# The gateway's own client (ADR 0024). Envoy's OIDC filter redeems an
# authorization code with this client and its secret; the control plane pins
# `azp` to it and never redeems a code itself.
#
# Never adopted, unlike the two resources above: nothing hand-made ever
# created `saas-fabric-gateway` anywhere this module has run, because the
# whole point of ADR 0025 was to stop that plan before anyone carried it out
# (see this Application's README, "Why it exists"). `import` is left at its
# default `false` here, always.
resource "keycloak_openid_client" "gateway" {
  realm_id  = keycloak_realm.master.id
  client_id = "saas-fabric-gateway"

  access_type = "CONFIDENTIAL"
  # Generated in-cluster by ../../../master-instance-credential, never chosen
  # or typed. Setting it here is the one-time act that used to be a person
  # pasting a value into Keycloak's admin console by hand.
  client_secret = var.gateway_secret

  standard_flow_enabled        = true
  implicit_flow_enabled        = false
  direct_access_grants_enabled = false
  # This client redeems a code server-side, in Envoy -- it never authenticates
  # as itself, so it holds no service-account identity of its own.
  service_accounts_enabled = false

  valid_redirect_uris             = ["${var.public_base_url}/oauth2/callback"]
  valid_post_logout_redirect_uris = ["${var.public_base_url}/*"]
  # No web origins: this client is driven server-side, by Envoy, never by a
  # script running in a browser on some other origin (the same statement
  # ../../saas-fabric-control-plane/README.md already makes about it).
  web_origins = []

  # Realm identity other objects depend on by name -- this environment's
  # SecurityPolicy pins clientID to it, and its redirect URI is this
  # instance's own origin. Destroying and recreating it under a config typo
  # is a worse failure than refusing the change outright.
  lifecycle {
    prevent_destroy = true
  }
}

# The console's own client: public, PKCE-required, for as long as the
# console's own sign-in flow exists. ADR 0024 slice 2 retires this flow once
# the console short-circuits its own sign-in against the gateway's session
# instead. Removal is two steps, not one: first drop the `lifecycle` block
# below and apply (so `prevent_destroy` does not refuse the second step),
# then remove this resource entirely and apply again. Doing both in the same
# change risks a single `prevent_destroy` failure masking whether the first
# half landed; doing them separately makes each step's own plan legible on
# its own.
resource "keycloak_openid_client" "console" {
  realm_id  = keycloak_realm.master.id
  client_id = "saas-fabric-console"

  access_type = "PUBLIC"

  standard_flow_enabled        = true
  implicit_flow_enabled        = false
  direct_access_grants_enabled = false
  service_accounts_enabled     = false
  # A public client holds no secret, so PKCE is what replaces one.
  pkce_code_challenge_method = "S256"

  valid_redirect_uris = ["${var.public_base_url}/"]

  # LucentRoot's master realm predates this module: this client was created
  # by hand before ADR 0025, the same history `fabric_operator` above
  # carries. See that resource's comment -- the reasoning is identical.
  import = var.adopt_existing

  lifecycle {
    prevent_destroy = true
  }
}

# Each operator the environment declares
# (../../overlays/lucentroot/master-instance-config.yaml). `keycloak_user` is
# a lookup, not a resource -- the account itself is created by Keycloak's own
# bootstrap or by an administrator signing in for the first time, and this
# module only grants roles to whoever already holds the username.
#
# By username, not email: the keycloak/keycloak provider's `keycloak_user`
# data source resolves a realm user through Keycloak's own user-search API
# filtered on `username`, and has no separate email-lookup argument. An
# operator whose username happens to be their email address still resolves
# here -- Keycloak commonly sets `registrationEmailAsUsername` -- but the
# value in `var.operators` is always matched against `username`, never
# `email`, so it must be the exact string each operator authenticates with.
#
# **A name here that does not already exist as a Keycloak user fails the
# whole convergence, at plan time.** `keycloak_user` is a data source, and a
# data source's read is part of `tofu plan`, before anything is applied --
# so a typo, or an operator declared before their account exists, fails this
# Job before it changes anything else, not partway through. On a fresh
# environment the only account that exists is the bootstrap administrator
# this module authenticates as; creating the *account* for a new operator
# (as opposed to granting one that already exists the two roles below) is
# the one thing this module does not do -- see this Application's README,
# "What this does not decide".
data "keycloak_user" "operator" {
  for_each = var.operators
  realm_id = keycloak_realm.master.id
  username = each.value
}

# `exhaustive = false` is the load-bearing argument on this resource. The
# provider's default, `exhaustive = true`, makes this mapping the *sole*
# owner of everything the operator holds and strips any role assigned outside
# it -- including roles Keycloak itself assigns to every user in a realm.
# This module only ever adds the two roles below; it must never remove a role
# it does not know about.
resource "keycloak_user_roles" "operator" {
  for_each = var.operators
  realm_id = keycloak_realm.master.id
  user_id  = data.keycloak_user.operator[each.key].id

  role_ids = [
    keycloak_role.fabric_operator.id,
    # Master-realm admin: what lets the control plane create a client realm
    # *as this operator* (ADR 0012). fabric-operator alone authenticates them
    # to the control plane; it grants nothing inside Keycloak itself. This is
    # the composite, full-authority role `data.keycloak_role.admin` above
    # describes -- there is no narrower master-realm role that ADR 0012's
    # realm creation could use instead.
    data.keycloak_role.admin.id,
  ]

  exhaustive = false
}
