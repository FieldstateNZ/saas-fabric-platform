#!/usr/bin/env python3
"""Which ordering the platform's Argo CD wiring actually provides, read from
rendered manifests alone.

    import ordering
    wiring = ordering.read_wiring(platform_documents, bootstrap_documents)
    ordering.guarantee(wiring, "master-instance", "saas-fabric-control-plane")

Two questions are answered separately, because the answers differ:

  * **create** -- when the environment is first brought up, does `after` wait
    for `before` to be Healthy?
  * **update** -- when `after`'s own source changes on an environment that
    already exists, does `after` still wait for `before`?

`may_sync` answers the operational form of the second question for one moment:
given each Application's health and which ones are OutOfSync, would the
controller start a sync of this one now?

This is a model of documented Argo CD semantics, not an observation of a
controller. It encodes three mechanisms and nothing else:

  1. app-of-apps sync waves -- the root Application applies its child
     Application objects wave by wave, and waits on each wave's health only
     because argocd/runtime/application-health.yaml makes a child
     Application's health that of the application it names. A child with
     `automated` sync then reconciles its own source on its own schedule.
  2. ApplicationSet `RollingSync` -- generated Applications sync step by step,
     a step only once every Application in each earlier step is Healthy, and
     only while the applicationset-controller runs with progressive syncs
     enabled. Generated Applications have their own automated sync disabled
     by the controller; one selected by no step is never synced by it.
     Only the step shape that rule describes is modelled: a non-empty
     `matchExpressions` per step and `maxUpdate` absent or at its documented
     default of 100%. Any other capacity -- `0`, which upstream leaves for a
     person to sync by hand, an integer, or any other percentage -- and any
     strategy key this model does not read is reported as unmodelled.
  3. Nothing else orders anything. Two Applications in one wave race, and a
     manually synced Application is ordered by whoever presses sync.

Whatever this model says about the running environment still has to be
established against a real controller before anyone relies on it; see
docs/ordered-updates.md, "What is proven where".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import yaml

WAVE_ANNOTATION = "argocd.argoproj.io/sync-wave"
APPLICATION_HEALTH_KEY = "resource.customizations.health.argoproj.io_Application"
PROGRESSIVE_SYNCS_KEY = "applicationsetcontroller.enable.progressive.syncs"
ROOT_APPLICATION = "platform-root"

HEALTHY = "Healthy"


class NotModelled(LookupError):
    """The model makes no claim about this Application."""


@dataclass(frozen=True)
class Child:
    name: str
    wave: int
    automated: bool
    labels: dict[str, str]
    # The ApplicationSet that generates this Application, or None when the
    # root Application authors it directly.
    generated_by: str | None = None


@dataclass(frozen=True)
class RollingSet:
    name: str
    wave: int
    # Each step is the set of generated Application names its selector matches.
    steps: tuple[frozenset[str], ...]
    generated: frozenset[str]

    def step_of(self, application: str) -> int | None:
        for index, members in enumerate(self.steps):
            if application in members:
                return index
        return None


@dataclass
class Wiring:
    # The custom Application health assessment, in the bootstrap set (active
    # before the first wave-ordered sync) and in the reconciled environment.
    health_in_bootstrap: bool
    health_in_environment: bool
    progressive_syncs: bool
    children: dict[str, Child] = field(default_factory=dict)
    rolling_sets: dict[str, RollingSet] = field(default_factory=dict)
    # ApplicationSets this model does not understand, by name, with the reason.
    unmodelled: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Guarantee:
    create: bool
    update: bool
    create_because: str
    update_because: str


def _wave(document: dict) -> int:
    annotations = (document.get("metadata") or {}).get("annotations") or {}
    return int(annotations.get(WAVE_ANNOTATION, "0"))


def _config_value(documents: list[dict], config_map: str, key: str) -> str | None:
    for document in documents:
        if document.get("kind") == "ConfigMap" and document["metadata"]["name"] == config_map:
            value = (document.get("data") or {}).get(key)
            if value is not None:
                return str(value)
    return None


def _substitute(value, element: dict, go_template: bool):
    if isinstance(value, str):
        for key, replacement in element.items():
            for token in ((f"{{{{.{key}}}}}", f"{{{{ .{key} }}}}") if go_template else (f"{{{{{key}}}}}",)):
                value = value.replace(token, str(replacement))
        return value
    if isinstance(value, dict):
        return {k: _substitute(v, element, go_template) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, element, go_template) for v in value]
    return value


def _matches(labels: dict[str, str], expressions: list[dict]) -> bool:
    for expression in expressions:
        key, operator = expression["key"], expression["operator"]
        values = set(expression.get("values") or [])
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and key in labels and labels[key] in values:
            return False
        if operator not in ("In", "NotIn"):
            raise ValueError(f"matchExpressions operator {operator!r} is not modelled")
    return True


# Upstream's default capacity for a step: every Application it selects.
DEFAULT_MAX_UPDATE = "100%"
STRATEGY_KEYS = {"type", "rollingSync"}
STRATEGY_TYPES = ("AllAtOnce", "RollingSync")
ROLLING_SYNC_KEYS = {"steps"}
STEP_KEYS = {"matchExpressions", "maxUpdate"}
EXPRESSION_KEYS = {"key", "operator", "values"}


def _check_strategy(strategy: dict) -> None:
    """Raise for any strategy shape `may_sync`'s step rule does not describe."""
    unknown = set(strategy) - STRATEGY_KEYS
    if unknown:
        raise ValueError(f"strategy.{sorted(unknown)[0]} is not modelled")
    kind = strategy.get("type", "AllAtOnce")
    if kind not in STRATEGY_TYPES:
        raise ValueError(f"strategy.type {kind!r} is not modelled")
    if kind != "RollingSync":
        if "rollingSync" in strategy:
            raise ValueError("strategy.rollingSync without type RollingSync is not modelled")
        return
    rolling = strategy.get("rollingSync") or {}
    unknown = set(rolling) - ROLLING_SYNC_KEYS
    if unknown:
        raise ValueError(f"rollingSync.{sorted(unknown)[0]} is not modelled")
    steps = rolling.get("steps") or []
    if not steps:
        raise ValueError("a RollingSync strategy with no steps is not modelled")
    for number, step in enumerate(steps, start=1):
        unknown = set(step) - STEP_KEYS
        if unknown:
            raise ValueError(f"step {number}: {sorted(unknown)[0]} is not modelled")
        if "maxUpdate" in step and step["maxUpdate"] != DEFAULT_MAX_UPDATE:
            raise ValueError(
                f"step {number}: maxUpdate {step['maxUpdate']!r} is not modelled;"
                f" only the default {DEFAULT_MAX_UPDATE!r} is"
            )
        expressions = step.get("matchExpressions") or []
        if not expressions:
            raise ValueError(f"step {number}: a step with no matchExpressions is not modelled")
        for expression in expressions:
            unknown = set(expression) - EXPRESSION_KEYS
            if unknown:
                raise ValueError(f"step {number}: matchExpressions.{sorted(unknown)[0]} is not modelled")


_TO_JSON = re.compile(r"\{\{-?\s*toJson\s+\.(\w+)\s*-?\}\}")


def _merge(base, patch):
    # A strategic merge patch on Application falls back to JSON merge
    # semantics for these fields: maps merge, lists are replaced whole.
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(base)
        for key, value in patch.items():
            merged[key] = _merge(base.get(key), value) if key in base else value
        return merged
    return patch


def _template_patch(patch: str, element: dict) -> dict:
    rendered = _TO_JSON.sub(lambda m: json.dumps(element[m.group(1)]), patch)
    rendered = _substitute(rendered, element, True)
    if "{{" in rendered:
        raise ValueError("templatePatch uses template actions beyond field and toJson substitution")
    return yaml.safe_load(rendered) or {}


def generate(application_set: dict) -> list[dict]:
    """The Applications a list-generator ApplicationSet renders.

    Only a single `list` generator with literal elements is modelled, with a
    `templatePatch` limited to field and `toJson` substitution: the only shape
    whose output can be known without a cluster. Anything else raises, so it
    is reported as unmodelled rather than guessed at.
    """
    spec = application_set["spec"]
    generators = spec.get("generators") or []
    if len(generators) != 1 or set(generators[0]) != {"list"}:
        raise ValueError("only a single list generator is modelled")
    go_template = bool(spec.get("goTemplate"))
    patch = spec.get("templatePatch")
    if patch is not None and not go_template:
        raise ValueError("templatePatch requires goTemplate")
    applications = []
    for element in generators[0]["list"].get("elements") or []:
        application = _substitute(spec["template"], element, go_template)
        if patch is not None:
            application = _merge(application, _template_patch(patch, element))
        applications.append(application)
    return applications


def _read_application_set(document: dict, wiring: Wiring) -> None:
    name = document["metadata"]["name"]
    strategy = (document["spec"].get("strategy") or {})
    try:
        _check_strategy(strategy)
        generated = generate(document)
    except (ValueError, KeyError) as error:
        wiring.unmodelled[name] = str(error)
        return

    names: list[str] = []
    for application in generated:
        metadata = application.get("metadata") or {}
        sync_policy = (application.get("spec") or {}).get("syncPolicy") or {}
        child = Child(
            name=metadata["name"],
            wave=_wave(document),
            automated="automated" in sync_policy,
            labels=dict(metadata.get("labels") or {}),
            generated_by=name,
        )
        wiring.children[child.name] = child
        names.append(child.name)

    if strategy.get("type") != "RollingSync":
        return
    try:
        steps = tuple(
            frozenset(
                child for child in names
                if _matches(wiring.children[child].labels, step["matchExpressions"])
            )
            for step in (strategy.get("rollingSync") or {}).get("steps") or []
        )
    except (ValueError, KeyError) as error:
        wiring.unmodelled[name] = str(error)
        return
    wiring.rolling_sets[name] = RollingSet(
        name=name, wave=_wave(document), steps=steps, generated=frozenset(names),
    )


def read_wiring(platform: list[dict], bootstrap: list[dict]) -> Wiring:
    """Read one environment's ordering wiring from its two rendered sets.

    `platform` is what the root Application syncs (environments/<env>);
    `bootstrap` is what a person applies before Argo CD takes over
    (environments/<env>/bootstrap).
    """
    platform = [d for d in platform if d]
    bootstrap = [d for d in bootstrap if d]
    progressive = (
        _config_value(platform, "argocd-cmd-params-cm", PROGRESSIVE_SYNCS_KEY) or "false"
    ).strip().lower() == "true"
    wiring = Wiring(
        health_in_bootstrap=_config_value(bootstrap, "argocd-cm", APPLICATION_HEALTH_KEY) is not None,
        health_in_environment=_config_value(platform, "argocd-cm", APPLICATION_HEALTH_KEY) is not None,
        progressive_syncs=progressive,
    )
    for document in platform:
        kind = document.get("kind")
        if kind == "Application" and document["metadata"]["name"] != ROOT_APPLICATION:
            sync_policy = document["spec"].get("syncPolicy") or {}
            wiring.children[document["metadata"]["name"]] = Child(
                name=document["metadata"]["name"],
                wave=_wave(document),
                automated="automated" in sync_policy,
                labels=dict(document["metadata"].get("labels") or {}),
            )
        elif kind == "ApplicationSet":
            _read_application_set(document, wiring)
    return wiring


def _child(wiring: Wiring, application: str) -> Child:
    if application in wiring.children:
        return wiring.children[application]
    suffix = (
        f"; unmodelled ApplicationSets: {', '.join(sorted(wiring.unmodelled))}"
        if wiring.unmodelled else ""
    )
    raise NotModelled(f"{application} is not an Application this model can read{suffix}")


def _rolling_set(wiring: Wiring, child: Child) -> RollingSet | None:
    if child.generated_by is None:
        return None
    return wiring.rolling_sets.get(child.generated_by)


def _creation(wiring: Wiring, before: Child, after: Child) -> tuple[bool, str]:
    shared = _rolling_set(wiring, before)
    if shared is not None and shared is _rolling_set(wiring, after) and wiring.progressive_syncs:
        first, second = shared.step_of(before.name), shared.step_of(after.name)
        if first is not None and second is not None and first < second:
            return True, f"RollingSync step {first + 1} precedes step {second + 1} in {shared.name}"

    if not wiring.health_in_bootstrap:
        return False, (
            "the bootstrap set does not carry the Application health assessment, so the"
            " root's first sync treats every child as Healthy once it exists"
        )
    if not wiring.health_in_environment:
        return False, (
            "the reconciled environment drops the Application health assessment, so the"
            " root's waves stop waiting on child health after the first sync"
        )
    # A generated Application is created when its ApplicationSet is, and the
    # root waits on that ApplicationSet's wave, not on the Applications it makes.
    if before.generated_by is not None and before.generated_by == after.generated_by:
        return False, (
            f"both are generated by {before.generated_by}, which creates them together"
            " unless a RollingSync step orders them"
        )
    if before.wave < after.wave:
        if before.generated_by is not None:
            return False, (
                f"{before.name} is generated by {before.generated_by}; the root waits on that"
                " ApplicationSet's own health, not on the Applications it generates"
            )
        return True, f"root sync wave {before.wave} precedes wave {after.wave}"
    return False, (
        f"{before.name} is in wave {before.wave} and {after.name} in wave {after.wave};"
        " Argo CD does not order Applications within one wave, nor backwards"
    )


def _update(wiring: Wiring, before: Child, after: Child) -> tuple[bool, str]:
    shared = _rolling_set(wiring, after)
    if shared is not None:
        if not wiring.progressive_syncs:
            return False, (
                f"{shared.name} declares RollingSync but progressive syncs are not enabled"
                f" ({PROGRESSIVE_SYNCS_KEY}); the strategy is ignored"
            )
        if _rolling_set(wiring, before) is not shared:
            return False, (
                f"{before.name} is outside {shared.name}; RollingSync orders only the"
                " Applications one ApplicationSet generates"
            )
        first, second = shared.step_of(before.name), shared.step_of(after.name)
        if second is None:
            return False, f"{after.name} is selected by no step of {shared.name}, so nothing syncs it"
        if first is None:
            return False, f"{before.name} is selected by no step of {shared.name}"
        if first < second:
            return True, (
                f"RollingSync step {second + 1} of {shared.name} waits for step {first + 1}"
                " to be Healthy on every sync"
            )
        return False, f"{before.name} is in step {first + 1}, not before {after.name}'s step {second + 1}"

    if after.automated:
        return False, (
            f"{after.name} carries its own automated sync and reconciles its own source;"
            " a change there changes nothing the root applies, so it waits on no sibling"
        )
    return False, f"{after.name} is synced by hand, so its order is whoever presses sync"


def guarantee(wiring: Wiring, before: str, after: str) -> Guarantee:
    first, second = _child(wiring, before), _child(wiring, after)
    create, create_because = _creation(wiring, first, second)
    update, update_because = _update(wiring, first, second)
    return Guarantee(create, update, create_because, update_because)


def may_sync(wiring: Wiring, application: str, health: dict[str, str], out_of_sync: set[str]) -> bool:
    """Would the controller start a sync of `application` right now?

    `health` maps Application name to its health status (Healthy, Progressing,
    Degraded, Missing ...); `out_of_sync` names the ones whose live state
    differs from Git. Applies only to an environment that already exists --
    the root's waves are not running. Raises NotModelled for an Application
    the model cannot read, including every Application of an unmodelled
    ApplicationSet, rather than answering for it.
    """
    child = _child(wiring, application)
    if application not in out_of_sync:
        return False
    rolling = _rolling_set(wiring, child)
    if rolling is not None and wiring.progressive_syncs:
        step = rolling.step_of(application)
        if step is None:
            return False
        earlier = set().union(*rolling.steps[:step]) if step else set()
        return all(health.get(name) == HEALTHY for name in earlier)
    return child.automated
