"""Offline tests for the decision logic — no Authentik, Traefik, or Docker needed.

    python3 -m unittest discover -s tests -v

The Authentik client is faked with an in-memory model of applications, providers,
and policy bindings, so slug selection, reconciliation, and stale removal can be
exercised exactly as they run against a live server.
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

os.environ.setdefault("TRAEFIK_URL", "http://traefik:8080")
os.environ.setdefault("AUTHENTIK_URL", "http://authentik:9000")
os.environ.setdefault("AUTHENTIK_TOKEN", "test-token")

import main  # noqa: E402
from docker import DockerClient  # noqa: E402


class FakeAK:
    """In-memory stand-in for AuthentikClient."""

    def __init__(self):
        self.applications = {}   # slug -> {"pk": uuid, "provider": pk}
        self.providers = {}      # pk -> {"external_host": url}
        self.bindings = {}       # app_uuid -> {group_pk}
        self.groups = {}         # name -> pk
        self.deleted_applications = []
        self.deleted_providers = []
        self._next_pk = 1

    # -- reads --
    def get_application(self, slug):
        app = self.applications.get(slug)
        return dict(app, slug=slug) if app else None

    def find_application(self, slug):
        app = self.applications.get(slug)
        return app["pk"] if app else None

    def get_provider(self, pk):
        provider = self.providers.get(pk)
        return dict(provider, pk=pk) if provider else None

    def get_provider_application_slug(self, pk):
        for slug, app in self.applications.items():
            if app.get("provider") == pk:
                return slug
        return None

    def provider_index(self):
        return ({p["external_host"]: pk for pk, p in self.providers.items()},
                {p["name"] for p in self.providers.values()})

    def find_provider(self, external_host):
        by_host, _ = self.provider_index()
        return by_host.get(external_host)

    def group_index(self):
        return dict(self.groups), {pk: name for name, pk in self.groups.items()}

    def list_application_bindings(self, app_uuid):
        return [{"pk": f"b-{g}", "group": g} for g in sorted(self.bindings.get(app_uuid, set()))]

    # -- writes --
    def create_provider(self, name, external_host, **kwargs):
        # Authentik enforces provider name uniqueness — the fake must too.
        if any(p["name"] == name for p in self.providers.values()):
            raise RuntimeError("provider with this name already exists.")
        pk = self._next_pk
        self._next_pk += 1
        self.providers[pk] = {"external_host": external_host, "name": name}
        return pk

    def create_application(self, name, slug, provider_pk, launch_url, group=""):
        uuid = f"app-{slug}"
        self.applications[slug] = {"pk": uuid, "provider": provider_pk,
                                   "group": group}
        return uuid

    def find_or_create_group(self, name):
        return self.groups.setdefault(name, f"grp-{name}")

    def bind_group_to_application(self, app_uuid, group_uuid):
        self.bindings.setdefault(app_uuid, set()).add(group_uuid)

    def unbind_group_from_application(self, app_uuid, group_uuid):
        bound = self.bindings.get(app_uuid, set())
        if group_uuid in bound:
            bound.discard(group_uuid)
            return True
        return False

    def add_provider_to_outpost(self, outpost, provider_pk):
        outpost.setdefault("providers", [])
        if provider_pk not in outpost["providers"]:
            outpost["providers"].append(provider_pk)

    def remove_provider_from_outpost(self, outpost, provider_pk):
        outpost["providers"] = [p for p in outpost.get("providers", []) if p != provider_pk]

    def get_outpost(self, name):
        return {"pk": "outpost-1", "name": name, "type": "proxy", "providers": []}

    def delete_application(self, slug):
        self.applications.pop(slug, None)
        self.deleted_applications.append(slug)

    def delete_provider(self, pk):
        self.providers.pop(pk, None)
        self.deleted_providers.append(pk)


def seed(ak, host, slug, groups=()):
    """Register an existing app+provider pair for `host`, as a live install would have."""
    pk = ak.create_provider(f"{slug} Proxy Provider", f"https://{host}")
    uuid = ak.create_application(slug.title(), slug, pk, f"https://{host}")
    for name in groups:
        ak.bind_group_to_application(uuid, ak.find_or_create_group(name))
    return pk, uuid


class TestHostFiltering(unittest.TestCase):
    def test_default_includes_everything(self):
        with mock.patch.object(main, "INCLUDED_HOSTS", main._compile([".*"], "t")), \
             mock.patch.object(main, "EXCLUDED_HOSTS", []):
            self.assertTrue(main._host_allowed("sonarr.example.com"))

    def test_exclude_beats_include(self):
        with mock.patch.object(main, "INCLUDED_HOSTS", main._compile([".*"], "t")), \
             mock.patch.object(main, "EXCLUDED_HOSTS", main._compile([r"^plex\."], "t")):
            self.assertFalse(main._host_allowed("plex.example.com"))
            self.assertTrue(main._host_allowed("sonarr.example.com"))

    def test_include_list_restricts(self):
        with mock.patch.object(main, "INCLUDED_HOSTS", main._compile([r"\.example\.com$"], "t")), \
             mock.patch.object(main, "EXCLUDED_HOSTS", []):
            self.assertTrue(main._host_allowed("sonarr.example.com"))
            self.assertFalse(main._host_allowed("sonarr.example.org"))

    def test_invalid_regex_is_a_startup_error(self):
        with self.assertRaises(RuntimeError):
            main._compile(["(unclosed"], "TRAEFIK_EXCLUDED_HOST")

    def test_indexed_env_vars_are_collected(self):
        with mock.patch.dict(os.environ, {
            "T_HOST": "a", "T_HOST1": "b", "T_HOST2": "c", "T_HOST4": "skipped",
        }, clear=False):
            self.assertEqual(main._env_list("T_HOST"), ["a", "b", "c"])


class TestGroupResolution(unittest.TestCase):
    TIERS = ["guest", "media", "trusted", "admin"]

    def test_hierarchical_includes_higher_tiers(self):
        with mock.patch.object(main, "GROUP_MODE", "hierarchical"), \
             mock.patch.object(main, "_TIER_ORDER", self.TIERS):
            self.assertEqual(main._resolve_groups("media"), ["admin", "media", "trusted"])
            self.assertEqual(main._resolve_groups("admin"), ["admin"])

    def test_flat_binds_only_what_is_listed(self):
        with mock.patch.object(main, "GROUP_MODE", "flat"), \
             mock.patch.object(main, "_TIER_ORDER", self.TIERS):
            self.assertEqual(main._resolve_groups("media"), ["media"])

    def test_unknown_group_passes_through(self):
        with mock.patch.object(main, "GROUP_MODE", "hierarchical"), \
             mock.patch.object(main, "_TIER_ORDER", self.TIERS):
            self.assertEqual(main._resolve_groups("media,custom"),
                             ["admin", "custom", "media", "trusted"])


class TestSlugSelection(unittest.TestCase):
    def test_free_slug_uses_short_form(self):
        ak = FakeAK()
        self.assertEqual(
            main._choose_slug(ak, "sonarr.example.com", "https://sonarr.example.com", None),
            "sonarr")

    def test_existing_provider_link_wins(self):
        ak = FakeAK()
        pk, _ = seed(ak, "qbit.example.com", "qbittorrent")
        self.assertEqual(
            main._choose_slug(ak, "qbit.example.com", "https://qbit.example.com", pk),
            "qbittorrent")

    def test_collision_across_apexes_falls_back_to_fqdn(self):
        """The pre-v5 bug: admin.example.org would have hijacked admin.example.com's app."""
        ak = FakeAK()
        seed(ak, "admin.example.com", "admin")
        slug = main._choose_slug(ak, "admin.example.org", "https://admin.example.org", None)
        self.assertEqual(slug, "admin-example-org")

    def test_reprovisioning_same_host_keeps_short_slug(self):
        ak = FakeAK()
        seed(ak, "admin.example.com", "admin")
        slug = main._choose_slug(ak, "admin.example.com", "https://admin.example.com", None)
        self.assertEqual(slug, "admin")

    def test_both_candidates_taken_skips_host(self):
        ak = FakeAK()
        seed(ak, "admin.example.com", "admin")
        seed(ak, "other.example.net", "admin-example-org")
        self.assertIsNone(
            main._choose_slug(ak, "admin.example.org", "https://admin.example.org", None))

    def test_provider_less_application_is_not_claimed(self):
        ak = FakeAK()
        ak.applications["admin"] = {"pk": "app-admin", "provider": None}
        slug = main._choose_slug(ak, "admin.example.org", "https://admin.example.org", None)
        self.assertEqual(slug, "admin-example-org")


class TestProviderNaming(unittest.TestCase):
    """Provider names are unique server-side — a clash returns HTTP 400."""

    def test_free_name_is_used(self):
        self.assertEqual(
            main._choose_provider_name("Sonarr", "sonarr.example.com", set()),
            "Sonarr Proxy Provider")

    def test_clash_falls_back_to_the_full_host(self):
        self.assertEqual(
            main._choose_provider_name("Admin", "admin.example.org",
                                       {"Admin Proxy Provider"}),
            "admin.example.org Proxy Provider")


class TestProvisioning(unittest.TestCase):
    """End-to-end provisioning against the fake, including the batch failure mode."""

    def setUp(self):
        self.ak = FakeAK()
        self.outpost = {"pk": "o1", "name": "embedded", "type": "proxy", "providers": []}
        patches = [
            mock.patch.object(main, "GROUP_MODE", "flat"),
            mock.patch.object(main, "_TIER_ORDER", []),
            mock.patch.object(main, "DRY_RUN", True),  # keeps _save_state off disk
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def _provision_all(self, hosts, labels):
        providers, names = self.ak.provider_index()
        claimed, provisioned, records = set(), set(), {}
        errors = []
        for host in hosts:
            sub, dom = host.split(".", 1)
            try:
                main._provision(self.ak, host, sub, dom, ("f1", "f2", "f3"), labels,
                                providers, names, self.outpost, claimed,
                                provisioned, {}, records)
            except Exception as exc:
                errors.append((host, str(exc)))
        return provisioned, records, errors

    def test_same_subdomain_two_apexes_both_provision(self):
        """The bug the clean room caught: the second host used to 400 and abort the poll."""
        provisioned, records, errors = self._provision_all(
            ["admin.example.com", "admin.example.org"],
            {"admin.example.com": "trusted", "admin.example.org": "media"})
        self.assertEqual(errors, [])
        self.assertEqual(provisioned, {"admin.example.com", "admin.example.org"})
        self.assertEqual(records["admin.example.com"]["slug"], "admin")
        self.assertEqual(records["admin.example.org"]["slug"], "admin-example-org")
        self.assertEqual(
            {p["name"] for p in self.ak.providers.values()},
            {"Admin Proxy Provider", "admin.example.org Proxy Provider"})

    def test_each_host_gets_its_own_groups(self):
        _, records, _ = self._provision_all(
            ["admin.example.com", "admin.example.org"],
            {"admin.example.com": "trusted", "admin.example.org": "media"})
        com = self.ak.bindings[self.ak.applications["admin"]["pk"]]
        org = self.ak.bindings[self.ak.applications["admin-example-org"]["pk"]]
        self.assertEqual(com, {"grp-trusted"})
        self.assertEqual(org, {"grp-media"})

    def test_unlabelled_host_provisions_open(self):
        provisioned, records, errors = self._provision_all(["media.example.com"], {})
        self.assertEqual(errors, [])
        self.assertEqual(records["media.example.com"]["groups"], [])
        self.assertEqual(self.ak.bindings.get("app-media", set()), set())


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.ak = FakeAK()
        self.outpost = {"pk": "o1", "name": "embedded", "type": "proxy", "providers": []}
        self.patches = [
            mock.patch.object(main, "GROUP_MODE", "flat"),
            mock.patch.object(main, "_TIER_ORDER", []),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _reconcile(self, host, labels, records, can_unbind=True):
        provisioned = {host}
        pk_by_name, name_by_pk = self.ak.group_index()
        providers, _ = self.ak.provider_index()
        main._reconcile(self.ak, host, labels, records, provisioned,
                        providers, self.outpost,
                        pk_by_name, name_by_pk, can_unbind)
        return provisioned

    def test_label_change_binds_new_and_unbinds_old(self):
        _, uuid = seed(self.ak, "app.example.com", "app", groups=["media"])
        records = {"app.example.com": {"slug": "app", "groups": ["media"]}}
        self._reconcile("app.example.com", {"app.example.com": "admin"}, records)
        self.assertEqual(self.ak.bindings[uuid], {"grp-admin"})
        self.assertEqual(records["app.example.com"]["groups"], ["admin"])

    def test_missing_unbind_permission_keeps_old_binding(self):
        _, uuid = seed(self.ak, "app.example.com", "app", groups=["media"])
        records = {"app.example.com": {"slug": "app", "groups": ["media"]}}
        self._reconcile("app.example.com", {"app.example.com": "admin"}, records,
                        can_unbind=False)
        self.assertEqual(self.ak.bindings[uuid], {"grp-admin", "grp-media"})

    def test_removed_label_never_prunes(self):
        """A container recreated without its label must not silently open the app up."""
        _, uuid = seed(self.ak, "app.example.com", "app", groups=["media"])
        records = {"app.example.com": {"slug": "app", "groups": ["media"]}}
        self._reconcile("app.example.com", {}, records)
        self.assertEqual(self.ak.bindings[uuid], {"grp-media"})

    def test_first_reconcile_after_upgrade_adopts_without_pruning(self):
        _, uuid = seed(self.ak, "app.example.com", "app", groups=["media", "manual-extra"])
        records = {"app.example.com": {"slug": "app"}}  # v2 state: no "groups" key
        self._reconcile("app.example.com", {"app.example.com": "admin"}, records)
        self.assertEqual(self.ak.bindings[uuid], {"grp-admin", "grp-manual-extra", "grp-media"})
        # Only the labelled group is claimed — pre-existing bindings stay unowned.
        self.assertEqual(records["app.example.com"]["groups"], ["admin"])

    def test_pre_existing_bindings_survive_a_later_narrowing(self):
        """Live stacks carry hand-made bindings ('authentik Admins', 'friends').

        Adopting must not claim them, or the next label change prunes them.
        """
        _, uuid = seed(self.ak, "app.example.com", "app",
                       groups=["homelab-admin", "authentik Admins", "media-admin"])
        records = {"app.example.com": {"slug": "app"}}  # v2 state
        # Pass 1: adopt under the current label.
        self._reconcile("app.example.com", {"app.example.com": "homelab-admin"}, records)
        # Pass 2: the label narrows to something else entirely.
        self._reconcile("app.example.com", {"app.example.com": "homelab-guest"}, records)
        self.assertIn("grp-authentik Admins", self.ak.bindings[uuid])
        self.assertIn("grp-media-admin", self.ak.bindings[uuid])
        self.assertIn("grp-homelab-guest", self.ak.bindings[uuid])
        self.assertNotIn("grp-homelab-admin", self.ak.bindings[uuid])

    def test_manual_binding_survives_a_later_prune(self):
        _, uuid = seed(self.ak, "app.example.com", "app", groups=["media", "manual-extra"])
        records = {"app.example.com": {"slug": "app", "groups": ["media"]}}
        self._reconcile("app.example.com", {"app.example.com": "admin"}, records)
        self.assertIn("grp-manual-extra", self.ak.bindings[uuid])
        self.assertNotIn("grp-media", self.ak.bindings[uuid])

    def test_deleted_application_is_requeued(self):
        records = {"app.example.com": {"slug": "app", "groups": ["media"]}}
        provisioned = self._reconcile("app.example.com", {"app.example.com": "media"}, records)
        self.assertNotIn("app.example.com", provisioned)
        self.assertNotIn("app.example.com", records)

    def test_missing_outpost_membership_is_repaired(self):
        pk, _ = seed(self.ak, "app.example.com", "app", groups=["media"])
        records = {"app.example.com": {"slug": "app", "groups": ["media"]}}
        self._reconcile("app.example.com", {"app.example.com": "media"}, records)
        self.assertIn(pk, self.outpost["providers"])

    def test_slug_backfilled_from_provider_link(self):
        seed(self.ak, "qbit.example.com", "qbittorrent", groups=["admin"])
        records = {}
        self._reconcile("qbit.example.com", {"qbit.example.com": "admin"}, records)
        self.assertEqual(records["qbit.example.com"]["slug"], "qbittorrent")


class TestStaleRemoval(unittest.TestCase):
    def test_refuses_to_delete_another_hosts_application(self):
        ak = FakeAK()
        seed(ak, "admin.example.com", "admin")
        provisioned, stale, records = {"admin.example.org"}, {}, {}
        main._remove_stale_app(ak, "admin.example.org", "admin", provisioned, stale, records)
        self.assertEqual(ak.deleted_applications, [])
        self.assertIn("admin.example.org", provisioned)

    def test_deletes_its_own_application_and_provider(self):
        ak = FakeAK()
        pk, _ = seed(ak, "gone.example.com", "gone")
        provisioned, stale, records = {"gone.example.com"}, {"gone.example.com": "x"}, \
            {"gone.example.com": {"slug": "gone", "groups": []}}
        main._remove_stale_app(ak, "gone.example.com", "gone", provisioned, stale, records)
        self.assertEqual(ak.deleted_applications, ["gone"])
        self.assertEqual(ak.deleted_providers, [pk])
        self.assertEqual(provisioned, set())
        self.assertEqual(records, {})


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "provisioned.json"
        p = mock.patch.object(main, "STATE_FILE", self.path)
        p.start()
        self.addCleanup(p.stop)

    def test_v1_list_migrates(self):
        self.path.write_text(json.dumps(["a.example.com"]))
        provisioned, stale, records = main._load_state()
        self.assertEqual(provisioned, {"a.example.com"})
        self.assertEqual((stale, records), ({}, {}))

    def test_v2_migrates_with_empty_records(self):
        self.path.write_text(json.dumps({
            "version": 2, "provisioned": ["a.example.com"],
            "stale_since": {"b.example.com": "2026-01-01T00:00:00+00:00"},
        }))
        provisioned, stale, records = main._load_state()
        self.assertEqual(provisioned, {"a.example.com"})
        self.assertIn("b.example.com", stale)
        self.assertEqual(records, {})

    def test_round_trip_v3(self):
        with mock.patch.object(main, "DRY_RUN", False):
            main._save_state({"a.example.com"}, {},
                             {"a.example.com": {"slug": "a", "groups": ["admin"]}})
        provisioned, _, records = main._load_state()
        self.assertEqual(provisioned, {"a.example.com"})
        self.assertEqual(records["a.example.com"]["groups"], ["admin"])
        self.assertEqual(json.loads(self.path.read_text())["version"], 3)

    def test_corrupt_state_starts_fresh(self):
        self.path.write_text("{not json")
        self.assertEqual(main._load_state(), (set(), {}, {}))

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(main, "DRY_RUN", True):
            main._save_state({"a.example.com"}, {}, {})
        self.assertFalse(self.path.exists())


class TestReleaseFiltered(unittest.TestCase):
    def test_excluded_host_is_released_not_deleted(self):
        with mock.patch.object(main, "INCLUDED_HOSTS", main._compile([".*"], "t")), \
             mock.patch.object(main, "EXCLUDED_HOSTS", main._compile([r"^plex\."], "t")):
            provisioned = {"plex.example.com", "sonarr.example.com"}
            stale = {"plex.example.com": "2026-01-01T00:00:00+00:00"}
            records = {"plex.example.com": {"slug": "plex"}}
            main._release_filtered(provisioned, stale, records)
            self.assertEqual(provisioned, {"sonarr.example.com"})
            self.assertEqual(stale, {})
            self.assertEqual(records, {})


class TestDockerClient(unittest.TestCase):
    def test_unreachable_api_returns_none_not_empty(self):
        client = DockerClient("tcp://socket-proxy:2375")
        with mock.patch("docker.requests.get", side_effect=OSError("boom")):
            self.assertIsNone(client.get_host_access_groups("authentik.access.group"))

    def test_no_labelled_containers_returns_empty_dict(self):
        client = DockerClient("tcp://socket-proxy:2375")
        response = mock.Mock(status_code=200)
        response.json.return_value = [{"Labels": {"com.example": "x"}}]
        response.raise_for_status.return_value = None
        with mock.patch("docker.requests.get", return_value=response):
            self.assertEqual(client.get_host_access_groups("authentik.access.group"), {})

    def test_label_maps_to_every_host_in_the_rule(self):
        client = DockerClient("tcp://socket-proxy:2375")
        response = mock.Mock(status_code=200)
        response.json.return_value = [{"Labels": {
            "authentik.access.group": "homelab-admin",
            "traefik.http.routers.x.rule": "Host(`a.example.com`) || Host(`b.example.com`)",
        }}]
        response.raise_for_status.return_value = None
        with mock.patch("docker.requests.get", return_value=response):
            self.assertEqual(
                client.get_host_access_groups("authentik.access.group"),
                {"a.example.com": "homelab-admin", "b.example.com": "homelab-admin"})


class TestLogging(unittest.TestCase):
    """LOG_TYPE must never be able to take the service down or silence it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Restore whatever the test session was using afterwards.
        self.addCleanup(main._setup_logging)

    def _handlers(self):
        import logging as _logging
        root = _logging.getLogger()
        return ([h for h in root.handlers if isinstance(h, _logging.StreamHandler)
                 and not hasattr(h, "baseFilename")],
                [h for h in root.handlers if hasattr(h, "baseFilename")])

    def test_console_is_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOG_TYPE", None)
            main._setup_logging()
        console, files = self._handlers()
        self.assertTrue(console)
        self.assertFalse(files)

    def test_both_writes_console_and_file(self):
        with mock.patch.dict(os.environ, {"LOG_TYPE": "BOTH", "LOG_PATH": self.tmp.name,
                                          "LOG_FILE": "t.log"}):
            main._setup_logging()
            logging.getLogger("authentik-companion").info("hello from the test")
        console, files = self._handlers()
        self.assertTrue(console)
        self.assertEqual(len(files), 1)
        self.assertIn("hello from the test", (Path(self.tmp.name) / "t.log").read_text())

    def test_file_only_omits_console(self):
        with mock.patch.dict(os.environ, {"LOG_TYPE": "FILE", "LOG_PATH": self.tmp.name,
                                          "LOG_FILE": "t.log"}):
            main._setup_logging()
        console, files = self._handlers()
        self.assertFalse(console)
        self.assertEqual(len(files), 1)

    def test_unwritable_log_dir_falls_back_to_console(self):
        with mock.patch.dict(os.environ, {"LOG_TYPE": "FILE",
                                          "LOG_PATH": "/proc/nonexistent/nope",
                                          "LOG_FILE": "t.log"}):
            main._setup_logging()  # must not raise
        console, files = self._handlers()
        self.assertTrue(console, "a failed file handler must not leave logging silent")
        self.assertFalse(files)

    def test_unknown_log_type_falls_back_to_console(self):
        with mock.patch.dict(os.environ, {"LOG_TYPE": "SYSLOG"}):
            main._setup_logging()
        console, _ = self._handlers()
        self.assertTrue(console)

    def test_invalid_log_level_does_not_raise(self):
        with mock.patch.dict(os.environ, {"LOG_LEVEL": "CHATTY"}):
            main._setup_logging()
        self.assertEqual(logging.getLogger().level, logging.INFO)


class TestEnvHelpers(unittest.TestCase):
    def test_file_variant_wins(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("  from-file\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        with mock.patch.dict(os.environ, {"X_VAL": "from-env", "X_VAL_FILE": path}):
            self.assertEqual(main._env("X_VAL"), "from-file")

    def test_missing_file_is_a_startup_error(self):
        with mock.patch.dict(os.environ, {"X_VAL_FILE": "/nonexistent/nope"}):
            with self.assertRaises(RuntimeError):
                main._env("X_VAL")

    def test_flag_parsing(self):
        with mock.patch.dict(os.environ, {"F": "TRUE"}):
            self.assertTrue(main._env_flag("F"))
        with mock.patch.dict(os.environ, {"F": "no"}):
            self.assertFalse(main._env_flag("F"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("F", None)
            self.assertTrue(main._env_flag("F", True))


if __name__ == "__main__":
    unittest.main()


class TestStaleSlugResolution(unittest.TestCase):
    """A hand-renamed application must still be found when its host goes stale."""

    def setUp(self):
        self.ak = FakeAK()

    def test_recorded_slug_wins(self):
        records = {"qbit.example.com": {"slug": "qbittorrent", "groups": []}}
        self.assertEqual(
            main._resolve_stale_slug(self.ak, "qbit.example.com", records), "qbittorrent")

    def test_renamed_app_is_found_via_its_provider(self):
        """The gap: derived slug 'qbit' misses, but the provider knows the truth."""
        seed(self.ak, "qbit.example.com", "qbittorrent")
        self.assertEqual(
            main._resolve_stale_slug(self.ak, "qbit.example.com", {}), "qbittorrent")

    def test_plain_host_still_resolves_by_derivation(self):
        seed(self.ak, "sonarr.example.com", "sonarr")
        self.ak.providers.clear()  # force the derivation path
        self.assertEqual(
            main._resolve_stale_slug(self.ak, "sonarr.example.com", {}), "sonarr")

    def test_unresolvable_host_returns_none(self):
        self.assertIsNone(main._resolve_stale_slug(self.ak, "ghost.example.com", {}))

    def test_renamed_app_is_actually_deleted_not_silently_skipped(self):
        pk, _ = seed(self.ak, "qbit.example.com", "qbittorrent")
        provisioned, stale, records = {"qbit.example.com"}, {"qbit.example.com": "x"}, {}
        slug = main._resolve_stale_slug(self.ak, "qbit.example.com", records)
        main._remove_stale_app(self.ak, "qbit.example.com", slug,
                               provisioned, stale, records)
        self.assertEqual(self.ak.deleted_applications, ["qbittorrent"])
        self.assertEqual(self.ak.deleted_providers, [pk])
        self.assertEqual(provisioned, set())

    def test_unresolvable_host_stays_flagged_rather_than_vanishing(self):
        """Never drop a host from state on a guess that missed — keep it visible."""
        provisioned, stale, records = {"ghost.example.com"}, {"ghost.example.com": "x"}, {}
        main._remove_stale_app(self.ak, "ghost.example.com", None,
                               provisioned, stale, records)
        self.assertEqual(self.ak.deleted_applications, [])
        self.assertIn("ghost.example.com", provisioned)
        self.assertIn("ghost.example.com", stale)
