# `catalogue` — a deployment grouping

**This directory name is not an architectural classification.** It says where an
application is deployed and how much privilege it gets. It says nothing about
whether the application matters, whether operators depend on it, or whether it
can serve clients.

For what a service *is*, see
[docs/platform-services.md](../../docs/platform-services.md) and each
directory's `platform-service.yaml`.

## What the grouping actually means

| | `core` | `catalogue` |
|---|---|---|
| Namespaces | several, per concern | `catalogue`, only |
| Argo CD project | `saas-fabric-platform` | `saas-fabric-catalogue` |
| Cluster-scoped resources | permitted, within an enumerated list | **none, at all** |

That is the whole distinction: a privilege tier. A chart placed here that
demands cluster scope is a signal to re-examine the placement — not evidence
that the service is unimportant.

## It used to mean something else, and that was wrong

The old test was *"does SaaS Fabric require this to operate?"* — if no, it was
catalogue. That made "catalogue" a synonym for "optional", and optional read as
peripheral.

Perses is the counterexample that retired the model. SaaS Fabric runs without
it, so it lands here; platform operators nonetheless use it daily, Fabric's own
UI is meant to render operational views from its API rather than link out to it,
and its project model is a plausible client partition. Those facts are
independent, and one directory name could not carry them.

Required, operator-facing, client-partitionable and client-selectable are now
four separate declared properties. See
[docs/platform-services.md](../../docs/platform-services.md#the-four-dimensions).

## Not the client capability catalogue

The word is reserved, elsewhere, for a SaaS Fabric product concept: the
capabilities that can be enabled for a client. That catalogue does not live in
this repository. It may be *implemented by* services defined here, but a
platform service and a client capability are different things.

## Current contents

| Application | Platform service | Deployed |
|---|---|---|
| [Perses](perses/) | yes — operator-facing, client projects intended | yes |
| [Superset](superset/) | yes — client partitioning unresolved | assessed, not adopted |
| [Airflow](airflow/) | yes — not a client isolation boundary | assessed, not adopted |
| [perses-provisioning](perses-provisioning/) | component of Perses | yes |

Superset and Airflow are directories holding an assessment and no
`application.yaml`. That is deliberate: the evaluation is worth keeping, and
adding an Application would mean shipping a bundled database this platform does
not intend to own. Both remain platform service candidates — the blocker is an
implementation constraint, not a judgement that they do not belong.

## Enablement

Per environment, and independent of classification. An environment that includes
`../../applications/catalogue` gets every application here; one that omits it
gets none and is still a complete platform.

| Environment | Enabled | Why |
|---|---|---|
| LucentRoot | yes | it dogfoods the services SaaS Fabric expects to manage |
| Production | not yet | |

Perses being enabled in one and not the other does not make it a different kind
of thing in each. See
[docs/platform-services.md](../../docs/platform-services.md#environment-enablement-is-separate-again).
