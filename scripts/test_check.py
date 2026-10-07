#!/usr/bin/env python3
"""Regression tests for the invariants in scripts/check.py that guard SaaS
Fabric's instance partition.

    python3 -m unittest discover -s scripts -p 'test_*.py'

scripts/validate.sh runs exactly that, so CI runs these. Standard-library
unittest, synthetic manifests and policy text written to a temporary
directory, no rendered output, no network and no cluster.

Each case here failed against the implementation it replaced, or pins a
behaviour that implementation got right and the replacement must keep:

  * the deny on the partition is verified inside the `platform-secrets`
    policy that self-init actually writes, so a deny in a comment, or in
    some other policy, no longer satisfies it, while ordinary whitespace and
    comments inside a valid policy no longer break it;
  * both the policy request and the role request must *write*; the role
    binds `platform-secrets` through `token_policies` or its deprecated
    alias `policies`, as a string or a list of literals, and never both at
    once, never through a reference;
  * the reader decodes five string escapes and refuses every other one, so
    no fabricated escape can spell `deny`; a path rule mixing the legacy
    `policy` field with `capabilities`, or a path declared twice, is refused
    as ambiguous rather than merged;
  * the partition is compared segment by segment, so a sibling path such as
    `platform/saas-fabric/instances-public` is allowed, while the root, its
    descendants, its ancestors and the whole store are refused -- for every
    shape ESO offers, and only through the platform store.

What the suite deliberately does not claim: that a pass means the running
External Secrets identity is denied. The checker reads two declarations in
the stanza; it does not evaluate the effective ACL across every policy the
role binds, and it cannot see a running instance. RoleBinding's last test
pins that boundary.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check  # noqa: E402  (after the path insert, deliberately)

REPOSITORY = SCRIPTS.parent
LUCENTROOT_OPENBAO_VALUES = REPOSITORY / "environments" / "lucentroot" / "config" / "openbao.yaml"

PARTITION_DATA = 'secret/data/platform/saas-fabric/instances/*'
PARTITION_METADATA = 'secret/metadata/platform/saas-fabric/instances/*'

PLATFORM_READ = (
    'path "secret/data/platform/*"     { capabilities = ["read"] }\n'
    'path "secret/metadata/platform/*" { capabilities = ["read", "list"] }\n'
)
PARTITION_DENY = (
    f'path "{PARTITION_DATA}"     {{ capabilities = ["deny"] }}\n'
    f'path "{PARTITION_METADATA}" {{ capabilities = ["deny"] }}\n'
)


# ---------------------------------------------------------------------------
# Factories. Every test builds its own configuration; none shares a fixture.
# ---------------------------------------------------------------------------

def openbao_config(
    platform_policy: str,
    *,
    operation: str = "update",
    role_operation: str = "update",
    role_binding: str = 'policies = "platform-secrets"',
    extra_requests: str = "",
) -> str:
    """A server configuration shaped like the one the chart renders.

    Only the `platform-secrets` request, the External Secrets role, and
    whatever `extra_requests` adds vary. `role_binding` is the role's policy
    line (or lines) verbatim, so a test can spell `token_policies`, the
    deprecated `policies`, both, or neither. The rest is what
    `_openbao_config` and the older string checks in
    check_openbao_bootstraps_itself need to see so that the deny
    verification is the only thing under test.
    """
    indented_policy = "".join(f"      {line}\n" for line in platform_policy.splitlines())
    indented_binding = "".join(f"      {line}\n" for line in role_binding.splitlines())
    return (
        'ui = true\n'
        '\n'
        'listener "tcp" {\n'
        '  tls_disable = 1\n'
        '  address = "[::]:8200"\n'
        '  cluster_address = "[::]:8201"\n'
        '}\n'
        '\n'
        'storage "raft" {\n'
        '  path = "/openbao/data"\n'
        '}\n'
        '\n'
        'service_registration "kubernetes" {}\n'
        '\n'
        'seal "static" {\n'
        '  current_key_id = "lucentroot"\n'
        '  current_key    = "file:///openbao/seal/key"\n'
        '}\n'
        '\n'
        'initialize "platform" {\n'
        '  request "kv-mount" {\n'
        '    operation = "update"\n'
        '    path      = "sys/mounts/secret"\n'
        '    data      = {\n'
        '      type    = "kv"\n'
        '      options = { version = "2" }\n'
        '    }\n'
        '  }\n'
        '\n'
        '  # External Secrets reads the platform prefix and nothing beneath\n'
        '  # the instance partition.\n'
        '  request "platform-policy" {\n'
        f'    operation = "{operation}"\n'
        '    path      = "sys/policies/acl/platform-secrets"\n'
        '    data      = {\n'
        '      policy = <<-POLICY\n'
        f'{indented_policy}'
        '      POLICY\n'
        '    }\n'
        '  }\n'
        '\n'
        '  request "external-secrets-role" {\n'
        f'    operation = "{role_operation}"\n'
        '    path      = "auth/kubernetes/role/external-secrets"\n'
        '    data      = {\n'
        '      bound_service_account_names      = "external-secrets"\n'
        '      bound_service_account_namespaces = "secrets"\n'
        f'{indented_binding}'
        '      ttl                              = "1h"\n'
        '    }\n'
        '  }\n'
        f'{extra_requests}'
        '}\n'
    )


def unrelated_policy_request(policy: str) -> str:
    """A second policy request -- the administrative one -- carrying `policy`."""
    indented_policy = "".join(f"      {line}\n" for line in policy.splitlines())
    return (
        '\n'
        '  request "platform-admin-policy" {\n'
        '    operation = "update"\n'
        '    path      = "sys/policies/acl/platform-admin"\n'
        '    data      = {\n'
        '      policy = <<-POLICY\n'
        f'{indented_policy}'
        '      POLICY\n'
        '    }\n'
        '  }\n'
    )


def openbao_problems(config: str) -> list[str]:
    """check_openbao_bootstraps_itself against a render holding `config`."""
    with tempfile.TemporaryDirectory() as temporary:
        render = Path(temporary)
        for environment in check.ENVIRONMENTS:
            (render / environment / "applications").mkdir(parents=True)
        document = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "openbao-config", "namespace": "secrets"},
            "data": {"extraconfig-from-values.hcl": config},
        }
        (render / check.DISPOSABLE_OPENBAO_ENVIRONMENT / "applications" / "openbao.yaml").write_text(
            yaml.safe_dump(document)
        )
        problems: list[str] = []
        check._DOCUMENTS.clear()
        check.check_openbao_bootstraps_itself(render, problems)
        return problems


def deny_problems(problems: list[str]) -> list[str]:
    return [problem for problem in problems if "does not deny" in problem]


def external_secret(
    *,
    data: list[dict] | None = None,
    data_from: list[dict] | None = None,
    store: str | None = "openbao",
    kind: str = "ExternalSecret",
    name: str = "under-test",
) -> dict:
    """An ExternalSecret, or a ClusterExternalSecret with the nested spec."""
    spec: dict = {"target": {"name": name}}
    if store is not None:
        spec["secretStoreRef"] = {"name": store, "kind": "ClusterSecretStore"}
    if data is not None:
        spec["data"] = data
    if data_from is not None:
        spec["dataFrom"] = data_from
    document = {
        "apiVersion": "external-secrets.io/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": "platform-system"},
    }
    if kind == "ClusterExternalSecret":
        document["spec"] = {
            "externalSecretName": name,
            "namespaceSelectors": [{"matchLabels": {"fieldstate.nz/layer": "platform"}}],
            "externalSecretSpec": spec,
        }
    else:
        document["spec"] = spec
    return document


def secret_problems(*documents: dict) -> list[str]:
    """check_platform_secrets_stay_platform against a render of `documents`."""
    with tempfile.TemporaryDirectory() as temporary:
        render = Path(temporary)
        for environment in check.ENVIRONMENTS:
            (render / environment / "applications").mkdir(parents=True)
        (render / "lucentroot" / "applications" / "under-test.yaml").write_text(
            yaml.safe_dump_all(list(documents))
        )
        problems: list[str] = []
        check._DOCUMENTS.clear()
        check.check_platform_secrets_stay_platform(render, problems)
        return problems


def partition_problems(problems: list[str]) -> list[str]:
    return [problem for problem in problems if check.FABRIC_INSTANCE_PREFIX in problem and "can read" in problem]


# ---------------------------------------------------------------------------
# The deny is verified in the active policy, not anywhere in the file.
# ---------------------------------------------------------------------------

class ActivePolicyDeny(unittest.TestCase):
    def test_intended_deny_in_the_active_policy_passes(self) -> None:
        problems = openbao_problems(openbao_config(PLATFORM_READ + PARTITION_DENY))
        self.assertEqual(problems, [])

    def test_lucentroot_source_stanza_passes_as_written(self) -> None:
        """The real environment file, straight from Git, without rendering."""
        values = yaml.safe_load(LUCENTROOT_OPENBAO_VALUES.read_text())
        reasons = check._external_secrets_policy_denies_partition(values["initializeStanza"])
        self.assertEqual(reasons, [])

    def test_missing_deny_fails(self) -> None:
        problems = openbao_problems(openbao_config(PLATFORM_READ))
        self.assertTrue(deny_problems(problems), problems)

    def test_deny_only_on_the_data_path_fails_for_metadata(self) -> None:
        data_only = f'path "{PARTITION_DATA}" {{ capabilities = ["deny"] }}\n'
        problems = deny_problems(openbao_problems(openbao_config(PLATFORM_READ + data_only)))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(PARTITION_METADATA, problems[0])

    def test_commented_out_deny_fails(self) -> None:
        """Failed on the old implementation: a regex over the whole file
        matched the deny inside a comment."""
        commented = "".join(f"# {line}\n" for line in PARTITION_DENY.splitlines())
        problems = openbao_problems(openbao_config(PLATFORM_READ + commented))
        self.assertEqual(len(deny_problems(problems)), 2, problems)

    def test_block_commented_deny_fails(self) -> None:
        commented = f"/*\n{PARTITION_DENY}*/\n"
        problems = openbao_problems(openbao_config(PLATFORM_READ + commented))
        self.assertEqual(len(deny_problems(problems)), 2, problems)

    def test_deny_in_an_unrelated_policy_fails(self) -> None:
        """Failed on the old implementation: the deny was found in the
        administrative policy and credited to External Secrets."""
        config = openbao_config(
            PLATFORM_READ,
            extra_requests=unrelated_policy_request(
                'path "*" { capabilities = ["sudo"] }\n' + PARTITION_DENY
            ),
        )
        problems = openbao_problems(config)
        self.assertEqual(len(deny_problems(problems)), 2, problems)

    def test_ordinary_whitespace_and_comments_inside_a_valid_deny_pass(self) -> None:
        """Failed on the old implementation: the regex demanded one exact
        spelling of `{ capabilities = ["deny"] }`."""
        spaced = (
            f'path   "{PARTITION_DATA}"\n'
            '{\n'
            '  // the partition is the control plane\'s own\n'
            '  capabilities\n'
            '    =\n'
            '      [ "deny" , ]\n'
            '}\n'
            f'path "{PARTITION_METADATA}" {{ # same again\n'
            '  capabilities = [\n'
            '    "deny"\n'
            '  ]\n'
            '}\n'
        )
        problems = openbao_problems(openbao_config(PLATFORM_READ + spaced))
        self.assertEqual(problems, [])

    def test_legacy_policy_deny_spelling_passes(self) -> None:
        legacy = (
            f'path "{PARTITION_DATA}" {{ policy = "deny" }}\n'
            f'path "{PARTITION_METADATA}" {{ policy = "deny" }}\n'
        )
        problems = openbao_problems(openbao_config(PLATFORM_READ + legacy))
        self.assertEqual(problems, [])

    def test_deny_on_a_narrower_path_fails(self) -> None:
        narrower = PARTITION_DENY.replace("instances/*", "instances/master/*")
        problems = openbao_problems(openbao_config(PLATFORM_READ + narrower))
        self.assertEqual(len(deny_problems(problems)), 2, problems)

    def test_policy_request_that_does_not_write_fails(self) -> None:
        for operation in ("read", "delete", "updated"):
            with self.subTest(operation=operation):
                config = openbao_config(PLATFORM_READ + PARTITION_DENY, operation=operation)
                problems = deny_problems(openbao_problems(config))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("does not write anything, so it establishes no policy", problems[0])

    def test_deny_beside_another_capability_is_not_a_deny(self) -> None:
        """The checker verifies the declaration; it does not resolve a
        contradictory one the way OpenBao might."""
        mixed = (
            f'path "{PARTITION_DATA}"     {{ capabilities = ["deny", "read"] }}\n'
            f'path "{PARTITION_METADATA}" {{ capabilities = ["deny"] }}\n'
        )
        problems = deny_problems(openbao_problems(openbao_config(PLATFORM_READ + mixed)))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(PARTITION_DATA, problems[0])

    def test_legacy_policy_mixed_with_capabilities_fails_closed(self) -> None:
        """`policy = "deny"` beside `capabilities = ["read"]` used to pass by
        union. Which half OpenBao honours is not something this checker
        claims to know, so the declaration is refused as ambiguous."""
        mixed = (
            f'path "{PARTITION_DATA}"     {{ policy = "deny" capabilities = ["read"] }}\n'
            f'path "{PARTITION_METADATA}" {{ capabilities = ["deny"] }}\n'
        )
        problems = deny_problems(openbao_problems(openbao_config(PLATFORM_READ + mixed)))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("could not be read", problems[0])
        self.assertIn("both capabilities and the legacy policy", problems[0])

    def test_path_declared_twice_fails_closed(self) -> None:
        """A read rule and a deny rule on the same path is the same ambiguity
        in another shape; it is refused rather than merged."""
        twice = (
            f'path "{PARTITION_DATA}" {{ capabilities = ["read"] }}\n'
            f'path "{PARTITION_DATA}" {{ capabilities = ["deny"] }}\n'
            f'path "{PARTITION_METADATA}" {{ capabilities = ["deny"] }}\n'
        )
        problems = deny_problems(openbao_problems(openbao_config(PLATFORM_READ + twice)))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("declared more than once", problems[0])

    def test_fabricated_escape_cannot_spell_deny(self) -> None:
        """An unbounded reader dropped the backslash, so `"d\\eny"` read as
        `deny`. Each of these is refused outright, and none passes the check."""
        for spelling in ('"d\\eny"', '"\\u0064eny"', '"den\\y"', '"\\x64eny"'):
            with self.subTest(spelling=spelling):
                forged = (
                    f'path "{PARTITION_DATA}"     {{ capabilities = [{spelling}] }}\n'
                    f'path "{PARTITION_METADATA}" {{ capabilities = [{spelling}] }}\n'
                )
                problems = deny_problems(openbao_problems(openbao_config(PLATFORM_READ + forged)))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("unsupported string escape", problems[0])

    def test_fabricated_escape_in_the_path_label_is_refused(self) -> None:
        forged = PARTITION_DENY.replace('"secret/data/', '"secret/d\\ata/')
        problems = deny_problems(openbao_problems(openbao_config(PLATFORM_READ + forged)))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("unsupported string escape", problems[0])

    def test_no_platform_secrets_request_fails(self) -> None:
        config = openbao_config(PLATFORM_READ + PARTITION_DENY).replace(
            'path      = "sys/policies/acl/platform-secrets"',
            'path      = "sys/policies/acl/platform-secrets-draft"',
        )
        problems = deny_problems(openbao_problems(config))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no initialize request writes sys/policies/acl/platform-secrets", problems[0])

    def test_unreadable_configuration_fails_closed(self) -> None:
        broken = openbao_config(PLATFORM_READ + PARTITION_DENY).replace("      POLICY\n", "", 1)
        problems = deny_problems(openbao_problems(broken))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("could not be read", problems[0])

    def test_policy_that_is_not_a_literal_fails_closed(self) -> None:
        config = openbao_config(PLATFORM_READ + PARTITION_DENY)
        start = config.index("      policy = <<-POLICY\n")
        end = config.index("      POLICY\n", start) + len("      POLICY\n")
        config = config[:start] + "      policy = var.platform_policy\n" + config[end:]
        problems = deny_problems(openbao_problems(config))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no literal data.policy", problems[0])


class RoleBinding(unittest.TestCase):
    """The External Secrets role request must write, and must literally bind
    `platform-secrets` through exactly one of the two policy fields."""

    POLICY = PLATFORM_READ + PARTITION_DENY

    def assertBound(self, role_binding: str) -> None:
        problems = openbao_problems(openbao_config(self.POLICY, role_binding=role_binding))
        self.assertEqual(problems, [])

    def assertUnbound(self, role_binding: str, *fragments: str, role_operation: str = "update") -> None:
        config = openbao_config(self.POLICY, role_binding=role_binding, role_operation=role_operation)
        problems = deny_problems(openbao_problems(config))
        self.assertEqual(len(problems), 1, problems)
        for fragment in fragments:
            self.assertIn(fragment, problems[0])

    # -- valid, each alias on its own ----------------------------------------

    def test_token_policies_string_binds(self) -> None:
        """Failed before: the current field was ignored, so a role bound only
        through `token_policies` was reported as unbound."""
        self.assertBound('token_policies = "platform-secrets"')

    def test_token_policies_list_binds(self) -> None:
        self.assertBound('token_policies = ["default", "platform-secrets"]')

    def test_deprecated_policies_string_binds(self) -> None:
        self.assertBound('policies = "platform-secrets"')

    def test_deprecated_policies_comma_separated_binds(self) -> None:
        self.assertBound('policies = "default, platform-secrets"')

    def test_deprecated_policies_list_binds(self) -> None:
        self.assertBound('policies = ["default", "platform-secrets"]')

    # -- refused ---------------------------------------------------------------

    def test_role_bound_to_another_policy_fails(self) -> None:
        self.assertUnbound('policies = "platform-admin"', "not bound to platform-secrets")
        self.assertUnbound('token_policies = ["platform-admin"]', "not bound to platform-secrets")

    def test_no_policy_field_fails(self) -> None:
        self.assertUnbound("", "not bound to platform-secrets")

    def test_both_fields_at_once_fail_closed(self) -> None:
        """Failed before: `policies = "platform-secrets"` beside
        `token_policies = "platform-admin"` passed, because only the
        deprecated field was read. Agreeing or not, both at once is refused."""
        self.assertUnbound(
            'policies       = "platform-secrets"\ntoken_policies = "platform-admin"',
            "could not be read", "both token_policies and policies",
        )
        self.assertUnbound(
            'policies       = "platform-secrets"\ntoken_policies = "platform-secrets"',
            "could not be read", "both token_policies and policies",
        )

    def test_nonliteral_binding_fails_closed(self) -> None:
        """A bare reference is not stringified into a name that might match."""
        self.assertUnbound('token_policies = var.policy', "could not be read", "not a string literal")
        self.assertUnbound('token_policies = [platform-secrets]', "could not be read", "not a string literal")
        self.assertUnbound('token_policies = { name = "platform-secrets" }', "could not be read", "neither a string nor a list")

    def test_role_request_that_does_not_write_fails(self) -> None:
        """Failed before: only the policy request's operation was checked, so
        a role request with `read` or `delete` counted as a binding."""
        for operation in ("read", "delete"):
            with self.subTest(operation=operation):
                self.assertUnbound(
                    'token_policies = "platform-secrets"',
                    f"operation {operation!r}, which does not write anything, so it binds no policy",
                    role_operation=operation,
                )

    # -- a deliberate boundary, recorded rather than claimed -------------------

    def test_other_bound_policies_are_not_evaluated(self) -> None:
        """Not a security pass. The role may bind further policies, and one of
        them could grant an exact path beneath the partition, which OpenBao's
        most-specific-path rule would rank above the glob deny. This checker
        reads two declarations and does not compute the token's effective
        ACL; that is verified against the instance, in the External Secrets
        README's in-place procedure. This test pins the boundary so a change
        that silently starts claiming more is noticed."""
        self.assertBound('token_policies = ["platform-secrets", "some-other-policy"]')


class ConfigurationReader(unittest.TestCase):
    """The reader's own guarantees: what it tolerates and what it refuses."""

    def test_strings_may_hold_braces_quotes_and_comment_markers(self) -> None:
        items = check.parse_hcl(
            'a = "{ not a block } # not a comment \\" still the string"\n'
            'b "label with // slashes" { c = 1 }\n'
        )
        self.assertEqual(items[0], ("attr", "a", '{ not a block } # not a comment " still the string'))
        self.assertEqual(items[1][:3], ("block", "b", ["label with // slashes"]))

    def test_indented_heredoc_strips_common_indent(self) -> None:
        items = check.parse_hcl('x = <<-EOT\n    one\n      two\n    EOT\n')
        self.assertEqual(items, [("attr", "x", "one\n  two\n")])

    def test_supported_escapes_decode(self) -> None:
        items = check.parse_hcl('a = "q\\"q b\\\\b n\\nn t\\tt r\\rr"\n')
        self.assertEqual(items, [("attr", "a", 'q"q b\\b n\nn t\tt r\rr')])

    def test_unsupported_escapes_are_refused_not_unescaped(self) -> None:
        """`\\u` is real HCL this reader does not implement; `\\e` and `\\x`
        are not HCL at all. None of them may become the next character."""
        for text in ('a = "d\\eny"\n', 'a = "\\u0064eny"\n', 'a = "\\x64eny"\n', 'a = "\\U00000064eny"\n'):
            with self.subTest(text=text):
                with self.assertRaises(check.ConfigSyntaxError) as refused:
                    check.parse_hcl(text)
                self.assertIn("unsupported string escape", str(refused.exception))

    def test_unterminated_string_is_refused(self) -> None:
        with self.assertRaises(check.ConfigSyntaxError):
            check.parse_hcl('a = "open\n')
        with self.assertRaises(check.ConfigSyntaxError):
            check.parse_hcl('a = "trailing backslash\\')

    def test_unbalanced_block_is_refused(self) -> None:
        with self.assertRaises(check.ConfigSyntaxError):
            check.parse_hcl('a "b" {\n  c = 1\n')

    def test_repeated_attribute_is_ambiguous(self) -> None:
        with self.assertRaises(check.ConfigSyntaxError):
            check._hcl_attribute(check.parse_hcl('p = "a"\np = "b"\n'), "p")


# ---------------------------------------------------------------------------
# The partition is a subtree, compared by segment.
# ---------------------------------------------------------------------------

class PartitionBoundary(unittest.TestCase):
    PARTITION_KEY = "platform/saas-fabric/instances/master/git/app-private-key"

    def assertRefused(self, *documents: dict, count: int = 1) -> None:
        problems = partition_problems(secret_problems(*documents))
        self.assertEqual(len(problems), count, problems)

    def assertAllowed(self, *documents: dict) -> None:
        self.assertEqual(partition_problems(secret_problems(*documents)), [])

    # -- refused -------------------------------------------------------------

    def test_remote_ref_key_beneath_the_partition_is_refused(self) -> None:
        self.assertRefused(external_secret(data=[
            {"secretKey": "key", "remoteRef": {"key": self.PARTITION_KEY}},
        ]))

    def test_extract_key_beneath_the_partition_is_refused(self) -> None:
        self.assertRefused(external_secret(data_from=[{"extract": {"key": self.PARTITION_KEY}}]))

    def test_find_beneath_the_partition_is_refused(self) -> None:
        self.assertRefused(external_secret(data_from=[
            {"find": {"path": "platform/saas-fabric/instances/master", "name": {"regexp": ".*"}}},
        ]))

    def test_find_over_an_ancestor_is_refused(self) -> None:
        for ancestor in ("platform", "platform/saas-fabric", "/platform/saas-fabric/"):
            with self.subTest(path=ancestor):
                self.assertRefused(external_secret(data_from=[
                    {"find": {"path": ancestor, "name": {"regexp": ".*"}}},
                ]))

    def test_find_over_the_whole_store_is_refused(self) -> None:
        self.assertRefused(external_secret(data_from=[{"find": {"name": {"regexp": "^platform/"}}}]))

    def test_exact_root_is_refused_by_key_and_by_find(self) -> None:
        """Documented exact-root semantics: stricter than the ACL glob, which
        does not match a secret written at the bare root path."""
        self.assertRefused(external_secret(data_from=[
            {"extract": {"key": "platform/saas-fabric/instances"}},
        ]))
        self.assertRefused(external_secret(data_from=[
            {"find": {"path": "platform/saas-fabric/instances/", "name": {"regexp": ".*"}}},
        ]))

    def test_empty_segments_do_not_disguise_the_partition(self) -> None:
        self.assertRefused(external_secret(data_from=[
            {"extract": {"key": "/platform//saas-fabric/instances/master/git/integration"}},
        ]))

    def test_cluster_external_secret_nested_spec_is_refused(self) -> None:
        self.assertRefused(external_secret(
            kind="ClusterExternalSecret",
            data_from=[{"extract": {"key": self.PARTITION_KEY}}],
        ))

    def test_per_entry_store_override_onto_the_platform_store_is_refused(self) -> None:
        self.assertRefused(external_secret(store="client-acme", data_from=[
            {
                "extract": {"key": self.PARTITION_KEY},
                "sourceRef": {"storeRef": {"name": "openbao", "kind": "ClusterSecretStore"}},
            },
        ]))

    def test_each_offending_entry_is_reported(self) -> None:
        self.assertRefused(external_secret(
            data=[{"secretKey": "a", "remoteRef": {"key": self.PARTITION_KEY}}],
            data_from=[{"find": {"path": "platform/saas-fabric", "name": {"regexp": ".*"}}}],
        ), count=2)

    # -- allowed -------------------------------------------------------------

    def test_sibling_find_path_is_allowed(self) -> None:
        """Failed on the old implementation: `startswith` read
        `instances-public` as beneath `instances`."""
        self.assertAllowed(external_secret(data_from=[
            {"find": {"path": "platform/saas-fabric/instances-public", "name": {"regexp": ".*"}}},
        ]))

    def test_sibling_key_is_allowed(self) -> None:
        self.assertAllowed(external_secret(data_from=[
            {"extract": {"key": "platform/saas-fabric/instances-public/catalogue"}},
        ]))
        self.assertAllowed(external_secret(data=[
            {"secretKey": "k", "remoteRef": {"key": "platform/saas-fabric/instance"}},
        ]))

    def test_ordinary_platform_keys_are_allowed(self) -> None:
        self.assertAllowed(external_secret(
            data=[{"secretKey": "k", "remoteRef": {"key": "platform/my-app", "property": "k"}}],
            data_from=[
                {"extract": {"key": "platform/saas-fabric/runtime"}},
                {"find": {"path": "platform/my-app", "name": {"regexp": ".*"}}},
            ],
        ))

    def test_partition_through_another_store_is_not_this_checks_business(self) -> None:
        self.assertAllowed(external_secret(store="client-acme", data_from=[
            {"extract": {"key": self.PARTITION_KEY}},
        ]))
        self.assertAllowed(external_secret(data_from=[
            {
                "extract": {"key": self.PARTITION_KEY},
                "sourceRef": {"storeRef": {"name": "client-acme", "kind": "SecretStore"}},
            },
        ]))

    def test_generator_backed_secret_without_a_store_is_allowed(self) -> None:
        self.assertAllowed(external_secret(store=None, data_from=[
            {"sourceRef": {"generatorRef": {"apiVersion": "generators.external-secrets.io/v1alpha1",
                                            "kind": "Password", "name": "p"}}},
        ]))

    # -- the client bound beside it still holds ------------------------------

    def test_client_path_through_the_platform_store_is_still_refused(self) -> None:
        problems = secret_problems(external_secret(data_from=[{"extract": {"key": "clients/acme/db"}}]))
        self.assertEqual(len([p for p in problems if "reads a path under" in p]), 1, problems)


# ---------------------------------------------------------------------------
# The master-instance convergence's own failure modes (platform #48).
# ---------------------------------------------------------------------------

MASTER_INSTANCE_MODULE = REPOSITORY / check.MASTER_INSTANCE_MODULE


def master_instance_module(
    *,
    user_realm: str = '"master"',
    admin_realm: str = '"master"',
    user_extra: str = "",
    grant_lifecycle: str = "lifecycle {\n    prevent_destroy = true\n  }",
) -> str:
    """The shape of base/module/main.tf, reduced to what the two checks read."""
    return (
        'resource "keycloak_realm" "master" {\n'
        '  realm = "master"\n'
        '  attributes = {\n'
        '    frontendUrl = var.public_base_url\n'
        '  }\n'
        '}\n'
        '\n'
        'data "keycloak_role" "admin" {\n'
        f'  realm_id = {admin_realm}\n'
        '  name     = "admin"\n'
        '}\n'
        '\n'
        'resource "keycloak_role" "fabric_operator" {\n'
        '  realm_id = keycloak_realm.master.id\n'
        '  name     = "fabric-operator"\n'
        '}\n'
        '\n'
        'data "keycloak_user" "operator" {\n'
        '  for_each = var.operators\n'
        f'  realm_id = {user_realm}\n'
        '  username = each.value\n'
        f'{user_extra}'
        '}\n'
        '\n'
        'resource "keycloak_user_roles" "operator" {\n'
        '  for_each = var.operators\n'
        '  realm_id = keycloak_realm.master.id\n'
        '  user_id  = data.keycloak_user.operator[each.key].id\n'
        '  role_ids = [keycloak_role.fabric_operator.id, data.keycloak_role.admin.id]\n'
        '  exhaustive = false\n'
        f'  {grant_lifecycle}\n'
        '}\n'
    )


class MasterInstanceLookups(unittest.TestCase):
    """Every lookup is read at plan time: a missing operator account must fail
    the plan, never an apply that already changed the realm. OpenTofu treats a
    data source's reference to a managed resource as depends_on and defers
    the read while that resource has changes pending -- which the imported
    realm always has on a first run."""

    def problems(self, text: str) -> list[str]:
        return check.master_instance_lookup_problems(text, "main.tf")

    def test_literal_realm_ids_pass(self) -> None:
        self.assertEqual(self.problems(master_instance_module()), [])

    def test_the_module_in_this_repository_passes(self) -> None:
        self.assertEqual(self.problems(MASTER_INSTANCE_MODULE.read_text()), [])

    def test_variable_realm_id_passes(self) -> None:
        self.assertEqual(self.problems(master_instance_module(user_realm="var.realm")), [])

    def test_user_lookup_referencing_the_realm_resource_fails(self) -> None:
        """Failed before: main at 0ff5d66 declared exactly this."""
        problems = self.problems(master_instance_module(user_realm="keycloak_realm.master.id"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("data.keycloak_user.operator references the managed resource keycloak_realm.master", problems[0])

    def test_role_lookup_referencing_the_realm_resource_fails(self) -> None:
        problems = self.problems(master_instance_module(admin_realm="keycloak_realm.master.id"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("data.keycloak_role.admin", problems[0])

    def test_reference_inside_an_interpolation_fails(self) -> None:
        problems = self.problems(master_instance_module(user_realm='"${keycloak_realm.master.id}"'))
        self.assertEqual(len(problems), 1, problems)

    def test_depends_on_fails(self) -> None:
        problems = self.problems(master_instance_module(user_extra="  depends_on = [keycloak_role.fabric_operator]\n"))
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any("declares depends_on" in p for p in problems), problems)

    def test_reference_in_a_comment_is_not_a_reference(self) -> None:
        self.assertEqual(self.problems(master_instance_module(user_extra="  # was keycloak_realm.master.id\n")), [])

    def test_managed_address_spelled_inside_a_string_literal_passes(self) -> None:
        """Failed before review: the scan read string contents as references."""
        self.assertEqual(self.problems(master_instance_module(user_realm='"keycloak_realm.master"')), [])

    def test_interpolation_beside_literal_text_still_fails(self) -> None:
        problems = self.problems(master_instance_module(
            user_realm='"realm-${keycloak_realm.master.id}-keycloak_role.literal"'))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("keycloak_realm.master", problems[0])

    def test_realm_id_through_a_local_fails(self) -> None:
        """Passed before review: `local.lookup_realm_id` hid the realm
        resource from the direct-reference scan."""
        problems = self.problems(master_instance_module(user_realm="local.lookup_realm_id"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("references local.*", problems[0])

    def test_realm_id_through_a_module_output_fails(self) -> None:
        problems = self.problems(master_instance_module(admin_realm="module.realm.id"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("references module.*", problems[0])

    def test_lookup_of_another_data_source_passes(self) -> None:
        self.assertEqual(self.problems(master_instance_module(user_realm="data.keycloak_realm.master.id")), [])


class MasterInstanceGrants(unittest.TestCase):
    """Removing a name from the roster destroys that grant, and the provider's
    delete revokes fabric-operator and master-realm admin whatever
    `exhaustive` says; prevent_destroy is what keeps a roster edit from
    revoking the bootstrap administrator's own authority."""

    def problems(self, text: str) -> list[str]:
        return check.master_instance_grant_problems(text, "main.tf")

    def test_guarded_grant_passes(self) -> None:
        self.assertEqual(self.problems(master_instance_module()), [])

    def test_the_module_in_this_repository_passes(self) -> None:
        self.assertEqual(self.problems(MASTER_INSTANCE_MODULE.read_text()), [])

    def test_unguarded_grant_fails(self) -> None:
        """Failed before: main at 0ff5d66 had no lifecycle on the grant."""
        problems = self.problems(master_instance_module(grant_lifecycle=""))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("keycloak_user_roles.operator has no lifecycle prevent_destroy", problems[0])

    def test_prevent_destroy_false_fails(self) -> None:
        problems = self.problems(master_instance_module(
            grant_lifecycle="lifecycle {\n    prevent_destroy = false\n  }"))
        self.assertEqual(len(problems), 1, problems)

    def test_prevent_destroy_after_a_nested_block_passes(self) -> None:
        """Failed before review: `[^}]*` stopped at the nested block's brace."""
        self.assertEqual(self.problems(master_instance_module(grant_lifecycle=(
            "lifecycle {\n"
            "    precondition {\n"
            "      condition     = true\n"
            '      error_message = "x"\n'
            "    }\n"
            "    prevent_destroy = true\n"
            "  }"))), [])

    def test_prevent_destroy_in_a_nested_block_only_fails(self) -> None:
        problems = self.problems(master_instance_module(grant_lifecycle=(
            "lifecycle {\n"
            "    precondition {\n"
            "      condition = true\n"
            "    }\n"
            "  }\n"
            "  dynamic \"x\" {\n"
            "    content {\n"
            "      prevent_destroy = true\n"
            "    }\n"
            "  }")))
        self.assertEqual(len(problems), 1, problems)

    def test_prevent_destroy_only_in_a_comment_fails(self) -> None:
        problems = self.problems(master_instance_module(
            grant_lifecycle="# lifecycle { prevent_destroy = true }"))
        self.assertEqual(len(problems), 1, problems)


def runtime_config(config_toml: str, *, mounts: list[dict] | None = None, volumes: list[dict] | None = None) -> list[dict]:
    """A ConfigMap and the Deployment that mounts it, shaped like saas-fabric's."""
    state = [
        ("state-tenants", "/etc/fabric/state/tenants", "fabric-runtime-tenants"),
        ("state-data-sources", "/etc/fabric/state/data-sources", "fabric-runtime-data-sources"),
        ("state-catalog", "/etc/fabric/state/catalog", "fabric-runtime-catalog"),
    ]
    default_mounts = [{"name": "config", "mountPath": "/etc/fabric"}] + [
        {"name": name, "mountPath": path} for name, path, _ in state
    ]
    default_volumes = [{"name": "config", "configMap": {"name": "saas-fabric-config"}}] + [
        {"name": name, "configMap": {"name": config}} for name, _, config in state
    ]
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "saas-fabric-config", "namespace": "platform-system"},
            "data": {"config.toml": config_toml},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "saas-fabric", "namespace": "platform-system"},
            "spec": {"template": {"spec": {
                "containers": [{
                    "name": "saas-fabric",
                    "env": [{"name": "FABRIC_CONFIG", "value": "/etc/fabric/config.toml"}],
                    "volumeMounts": mounts if mounts is not None else default_mounts,
                }],
                "volumes": volumes if volumes is not None else default_volumes,
            }}},
        },
    ]


TOP_LEVEL_PATHS = (
    'tenants_path = "/etc/fabric/state/tenants/tenants.json"\n'
    'data_sources_path = "/etc/fabric/state/data-sources/data-sources.json"\n'
    'catalog_path = "/etc/fabric/state/catalog/catalog.json"\n'
)
TOKEN_TABLE = '[token]\nmode = "trusted_ingress"\n'


def runtime_config_problems(*documents: dict) -> list[str]:
    with tempfile.TemporaryDirectory() as temporary:
        render = Path(temporary)
        for environment in check.ENVIRONMENTS:
            (render / environment / "applications").mkdir(parents=True)
        (render / "lucentroot" / "applications" / "saas-fabric.yaml").write_text(
            yaml.safe_dump_all(list(documents))
        )
        problems: list[str] = []
        check._DOCUMENTS.clear()
        check.check_runtime_config_document_paths(render, problems)
        return problems


class RuntimeConfigDocumentPaths(unittest.TestCase):
    """The three document paths must be top-level keys, as the real TOML parser reads them.

    Written after `[token]` they parse as `token.tenants_path` and the runtime,
    which refuses unknown keys in that table, would not start.
    """

    def test_paths_above_the_token_table_pass(self) -> None:
        config = 'listen = "0.0.0.0:8080"\n' + TOP_LEVEL_PATHS + TOKEN_TABLE
        self.assertEqual(runtime_config_problems(*runtime_config(config)), [])

    def test_paths_after_the_token_table_fail(self) -> None:
        config = 'listen = "0.0.0.0:8080"\n' + TOKEN_TABLE + TOP_LEVEL_PATHS
        problems = runtime_config_problems(*runtime_config(config))
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(all("under [token]" in problem for problem in problems), problems)

    def test_missing_path_fails(self) -> None:
        config = 'catalog_path = "/etc/fabric/state/catalog/catalog.json"\n' + TOKEN_TABLE
        problems = runtime_config_problems(*runtime_config(config))
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("no top-level" in problem for problem in problems), problems)

    def test_path_outside_every_mount_fails(self) -> None:
        config = TOP_LEVEL_PATHS.replace("/etc/fabric/state/catalog/", "/etc/fabric/state/other/") + TOKEN_TABLE
        problems = runtime_config_problems(*runtime_config(config))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("catalog_path", problems[0])

    def test_sub_path_mount_is_not_a_whole_volume(self) -> None:
        mounts = [
            {"name": "config", "mountPath": "/etc/fabric"},
            {"name": "state-tenants", "mountPath": "/etc/fabric/state/tenants", "subPath": "tenants.json"},
            {"name": "state-data-sources", "mountPath": "/etc/fabric/state/data-sources"},
            {"name": "state-catalog", "mountPath": "/etc/fabric/state/catalog"},
        ]
        problems = runtime_config_problems(*runtime_config(TOP_LEVEL_PATHS + TOKEN_TABLE, mounts=mounts))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tenants_path", problems[0])

    def test_invalid_toml_fails(self) -> None:
        problems = runtime_config_problems(*runtime_config("listen = \n"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("not valid TOML", problems[0])

    def test_unmounted_config_fails(self) -> None:
        mounts = [{"name": "state-tenants", "mountPath": "/etc/fabric/state/tenants"}]
        problems = runtime_config_problems(*runtime_config(TOP_LEVEL_PATHS + TOKEN_TABLE, mounts=mounts))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("not supplied by a mounted ConfigMap", problems[0])

    def test_repository_base_config_passes(self) -> None:
        base = REPOSITORY / "applications" / "core" / "saas-fabric" / "base"
        documents = [
            document
            for name in ("configmap.yaml", "deployment.yaml")
            for document in yaml.safe_load_all((base / name).read_text())
        ]
        for document in documents:
            document["metadata"]["namespace"] = "platform-system"
        self.assertEqual(runtime_config_problems(*documents), [])

    def runtime_problems_with_config_mount(self, mount: dict | None = None, volume: dict | None = None) -> list[str]:
        documents = runtime_config(TOP_LEVEL_PATHS + TOKEN_TABLE)
        pod = documents[1]["spec"]["template"]["spec"]
        if mount is not None:
            pod["containers"][0]["volumeMounts"][0] = mount
        if volume is not None:
            pod["volumes"][0] = volume
        return runtime_config_problems(*documents)

    def test_sub_path_expr_config_mount_is_rejected(self) -> None:
        problems = self.runtime_problems_with_config_mount(
            mount={"name": "config", "mountPath": "/etc/fabric", "subPathExpr": "config.toml"})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("not supplied by a mounted ConfigMap", problems[0])

    def test_sub_path_config_mount_is_rejected(self) -> None:
        problems = self.runtime_problems_with_config_mount(
            mount={"name": "config", "mountPath": "/etc/fabric", "subPath": "config.toml"})
        self.assertEqual(len(problems), 1, problems)

    def test_items_renaming_the_config_key_is_rejected(self) -> None:
        volume = {"name": "config", "configMap": {
            "name": "saas-fabric-config", "items": [{"key": "config.toml", "path": "renamed.toml"}]}}
        problems = self.runtime_problems_with_config_mount(volume=volume)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("not supplied by a mounted ConfigMap", problems[0])

    def test_items_omitting_the_config_key_is_rejected(self) -> None:
        volume = {"name": "config", "configMap": {"name": "saas-fabric-config", "items": []}}
        problems = self.runtime_problems_with_config_mount(volume=volume)
        self.assertEqual(len(problems), 1, problems)

    def test_items_projecting_the_config_key_under_its_name_passes(self) -> None:
        volume = {"name": "config", "configMap": {
            "name": "saas-fabric-config", "items": [{"key": "config.toml", "path": "config.toml"}]}}
        self.assertEqual(self.runtime_problems_with_config_mount(volume=volume), [])

    def test_projected_volume_supplying_the_config_passes(self) -> None:
        volume = {"name": "config", "projected": {"sources": [
            {"configMap": {"name": "saas-fabric-config"}}]}}
        self.assertEqual(self.runtime_problems_with_config_mount(volume=volume), [])

    def test_projected_volume_renaming_the_config_key_is_rejected(self) -> None:
        volume = {"name": "config", "projected": {"sources": [
            {"configMap": {"name": "saas-fabric-config",
                           "items": [{"key": "config.toml", "path": "renamed.toml"}]}}]}}
        self.assertEqual(len(self.runtime_problems_with_config_mount(volume=volume)), 1)

    def test_unreadable_default_mode_is_rejected(self) -> None:
        volume = {"name": "config", "configMap": {"name": "saas-fabric-config", "defaultMode": 0}}
        self.assertEqual(len(self.runtime_problems_with_config_mount(volume=volume)), 1)

    def test_unreadable_item_mode_is_rejected(self) -> None:
        volume = {"name": "config", "configMap": {"name": "saas-fabric-config",
                  "items": [{"key": "config.toml", "path": "config.toml", "mode": 0}]}}
        self.assertEqual(len(self.runtime_problems_with_config_mount(volume=volume)), 1)

    def test_readable_default_mode_passes(self) -> None:
        volume = {"name": "config", "configMap": {"name": "saas-fabric-config", "defaultMode": 0o444}}
        self.assertEqual(self.runtime_problems_with_config_mount(volume=volume), [])

    def test_document_directory_mounted_with_sub_path_expr_is_rejected(self) -> None:
        documents = runtime_config(TOP_LEVEL_PATHS + TOKEN_TABLE)
        documents[1]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][3][
            "subPathExpr"] = "catalog.json"
        problems = runtime_config_problems(*documents)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("catalog_path", problems[0])


if __name__ == "__main__":
    unittest.main()
