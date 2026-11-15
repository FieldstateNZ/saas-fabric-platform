variable "public_base_url" {
  type        = string
  description = <<-EOT
    This instance's public origin, e.g. https://fabric-lucentroot.tail5a7546.ts.net
    -- no trailing slash. Derives the gateway's redirect and post-logout URIs,
    the console's own redirect, and the master realm's frontendUrl attribute.
    Read from the environment overlay's own
    overlays/<environment>/master-instance-config.yaml.
  EOT
}

variable "gateway_secret" {
  type        = string
  sensitive   = true
  description = <<-EOT
    The saas-fabric-gateway client's secret, generated in-cluster by
    ../../master-instance-credential and never chosen by a person.
  EOT
}

variable "operators" {
  type        = set(string)
  description = <<-EOT
    Stable Keycloak user ids of the people who administer this environment,
    granted fabric-operator and master-realm admin. Each entry is the user's
    id, never a username. D01-6 option C feeds in the id of the built-in
    administrator Keycloak created at first start; this module does not create
    that account. See main.tf's comment on data.keycloak_user_realm_roles.operator.
  EOT

  validation {
    condition = alltrue([
      for id in var.operators : can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", id))
    ])
    error_message = "operators must be Keycloak user ids (UUIDs). Pass the built-in administrator's id. A reused username is a new account and is not granted master-realm admin."
  }
}

variable "adopt_existing" {
  type        = bool
  default     = false
  description = <<-EOT
    True only where the master realm's fabric-operator role and
    saas-fabric-console client predate this module -- LucentRoot today, whose
    realm was hand-configured before ADR 0025. Wired to this provider's own
    `import` argument on keycloak_role.fabric_operator and
    keycloak_openid_client.console (never on the gateway client, which never
    existed by hand): true adopts the existing object instead of attempting a
    create that Keycloak would refuse with 409. A fresh environment has no
    such history and leaves this false, so both resources are created
    outright.

    MUST NOT CHANGE for an environment once set. `import` is ForceNew on
    this provider: flipping this value later plans a destroy-and-recreate of
    both resources, not a no-op. `prevent_destroy` on both turns that into a
    loud plan failure rather than a silent drop-and-409 -- this is a fact
    about an environment's history, recorded once, not a toggle.
  EOT
}
