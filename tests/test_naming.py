"""Tests for the naming standard and the normalize-names audit."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tools"))

from naming import (  # noqa: E402
    ACRONYMS, canonical_name, canonical_provider_name, is_identifier, load_overrides,
    SECTION_SEPARATOR, canonical_group, family_for, load_domains, load_sections,
    prefix_for,
)

# tools/normalize-names.py is not an importable module name — load it by path.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("normalize_names", ROOT / "tools" / "normalize-names.py")
normalize_names = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(normalize_names)


class TestCanonicalName(unittest.TestCase):
    def test_title_case_is_the_baseline(self):
        self.assertEqual(canonical_name("bazarr", {}), "Bazarr")
        # Without an override, dashes are word breaks and nothing is expanded.
        self.assertEqual(canonical_name("win-control", {}), "Win Control")

    def test_acronyms_are_upper_cased(self):
        self.assertEqual(canonical_name("db", {}), "DB")
        self.assertIn("db", ACRONYMS)

    def test_override_wins(self):
        self.assertEqual(canonical_name("qbittorrent", {"qbittorrent": "qBittorrent"}),
                         "qBittorrent")

    def test_override_lookup_is_case_insensitive(self):
        self.assertEqual(canonical_name("HA", {"ha": "Home Assistant"}), "Home Assistant")

    def test_underscores_are_separators_too(self):
        self.assertEqual(canonical_name("win_console", {}), "Win Console")

    def test_empty_slug_is_returned_unchanged(self):
        self.assertEqual(canonical_name("", {}), "")

    def test_provider_name_tracks_the_app(self):
        self.assertEqual(canonical_provider_name("Sonarr"), "Sonarr Proxy Provider")


class TestIdentifierRule(unittest.TestCase):
    """Half one of the rule: anything a machine matches on is lowercase."""

    def test_valid_identifiers(self):
        for good in ("sonarr", "win-control", "admin-example-org", "qbittorrent-vpn", "db2"):
            self.assertTrue(is_identifier(good), good)

    def test_rejects_uppercase_and_junk(self):
        for bad in ("Sonarr", "win_control", "has space", "-lead", "trail-", "", "a--b"):
            self.assertFalse(is_identifier(bad), bad)


class TestDomainPrefix(unittest.TestCase):
    """Everything on an apex can carry a group label, applied by rule not by hand."""

    PRE = {"actanumeratorum.com": {"prefix": "Acta", "group": "Acta"}}
    OV = {"sys": "Glances", "db": "Database"}

    def test_prefix_applies_to_the_matching_apex(self):
        self.assertEqual(canonical_name("sys", self.OV, "actanumeratorum.com", self.PRE),
                         "Acta Glances")

    def test_other_domains_are_untouched(self):
        self.assertEqual(canonical_name("portainer", {}, "distraktr.com", self.PRE),
                         "Portainer")

    def test_a_brand_new_host_is_prefixed_without_an_override(self):
        """The whole point: nobody has to remember to add an entry."""
        self.assertEqual(canonical_name("newthing", {}, "actanumeratorum.com", self.PRE),
                         "Acta Newthing")

    def test_prefix_is_not_doubled(self):
        self.assertEqual(canonical_name("acta-admin", {}, "actanumeratorum.com", self.PRE),
                         "Acta Admin")

    def test_existing_prefix_match_is_case_insensitive(self):
        self.assertEqual(canonical_name("x", {"x": "acta thing"}, "actanumeratorum.com", self.PRE),
                         "acta thing")

    def test_subdomains_of_the_apex_match_too(self):
        self.assertEqual(prefix_for("deep.actanumeratorum.com", self.PRE), "Acta")

    def test_unrelated_domain_gets_no_prefix(self):
        self.assertEqual(prefix_for("example.com", self.PRE), "")

    def test_no_prefixes_configured_is_a_no_op(self):
        self.assertEqual(canonical_name("sys", self.OV, "actanumeratorum.com", {}), "Glances")

    def test_domains_load_from_the_shipped_example(self):
        loaded = load_domains(ROOT / "tools" / "naming-overrides.example.json")
        self.assertTrue(loaded)
        for apex, cfg in loaded.items():
            self.assertTrue(cfg["group"], apex)

    def test_shipped_config_uses_sections_not_prefixes(self):
        """Decision: the section heading carries the family, names do not repeat it."""
        loaded = load_domains(ROOT / "tools" / "naming-overrides.example.json")
        for apex, cfg in loaded.items():
            self.assertEqual(cfg["prefix"], "", apex)

    def test_shipped_names_never_repeat_their_section(self):
        cfg = ROOT / "tools" / "naming-overrides.example.json"
        ov, dom, sec = load_overrides(cfg), load_domains(cfg), load_sections(cfg)
        for apex, dcfg in dom.items():
            family = dcfg["group"].lower()
            for slug in list(sec) + list(ov):
                name = canonical_name(slug, ov, apex, dom)
                self.assertFalse(name.lower().startswith(f"{family} "),
                                 f"{slug}@{apex} -> {name!r} repeats its section heading")


class TestPortalSection(unittest.TestCase):
    """Authentik has ONE grouping field; two levels are encoded into the string."""

    DOM = {"actanumeratorum.com": {"group": "Acta", "prefix": ""},
           "distraktr.com": {"group": "Plexy", "prefix": ""}}
    SEC = {"sonarr": "Media", "sys": "Monitoring"}

    def test_family_and_function_are_combined(self):
        self.assertEqual(canonical_group("sonarr", "distraktr.com", self.DOM, self.SEC),
                         f"Plexy{SECTION_SEPARATOR}Media")
        self.assertEqual(canonical_group("sys", "actanumeratorum.com", self.DOM, self.SEC),
                         f"Acta{SECTION_SEPARATOR}Monitoring")

    def test_labels_sort_domain_first_then_function(self):
        labels = sorted(
            canonical_group(s, d, self.DOM, {"sonarr": "Media", "sys": "Monitoring",
                                             "qbittorrent": "Downloads"})
            for s, d in [("sonarr", "distraktr.com"), ("qbittorrent", "distraktr.com"),
                         ("sys", "actanumeratorum.com")])
        self.assertEqual(labels, [f"Acta{SECTION_SEPARATOR}Monitoring",
                                  f"Plexy{SECTION_SEPARATOR}Downloads",
                                  f"Plexy{SECTION_SEPARATOR}Media"])

    def test_family_alone_when_the_app_has_no_function(self):
        self.assertEqual(canonical_group("unassigned", "distraktr.com", self.DOM, {}), "Plexy")

    def test_function_alone_when_the_domain_has_no_family(self):
        self.assertEqual(canonical_group("sonarr", "example.com", {}, self.SEC), "Media")

    def test_unmapped_and_unassigned_is_ungrouped(self):
        self.assertEqual(canonical_group("whatever", "example.com", {}, {}), "")

    def test_subdomains_inherit_the_family(self):
        self.assertEqual(canonical_group("sys", "sys.actanumeratorum.com", self.DOM, self.SEC),
                         f"Acta{SECTION_SEPARATOR}Monitoring")

    def test_shipped_sections_resolve_to_two_level_labels(self):
        """Ungrouped apps sort ABOVE named sections; the tool warns on a remainder."""
        cfg = ROOT / "tools" / "naming-overrides.example.json"
        sections, domains = load_sections(cfg), load_domains(cfg)
        self.assertTrue(sections)
        apex = next(iter(domains))
        for slug, func in sections.items():
            self.assertTrue(func, slug)
            self.assertIn(SECTION_SEPARATOR, canonical_group(slug, apex, domains, sections))


class TestOverridesAreVerbatim(unittest.TestCase):
    """Half two: an override carries upstream's spelling and is never re-cased."""

    def test_upstream_spelling_survives_exactly(self):
        for slug, name in [("qbittorrent", "qBittorrent"), ("unifi", "UniFi"),
                           ("wg", "WireGuard"), ("ha", "Home Assistant")]:
            self.assertEqual(canonical_name(slug, {slug: name}), name)

    def test_uppercase_override_key_still_matches_the_slug(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "o.json"
        path.write_text(json.dumps({"QBitTorrent": "qBittorrent"}))
        self.assertEqual(load_overrides(path), {"qbittorrent": "qBittorrent"})

    def test_override_key_that_is_not_a_slug_is_dropped(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "o.json"
        path.write_text(json.dumps({"not a slug!": "Nope", "ok": "Fine"}))
        self.assertEqual(load_overrides(path), {"ok": "Fine"})


class TestLoadOverrides(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "o.json"

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(load_overrides(self.path), {})

    def test_bare_mapping(self):
        self.path.write_text(json.dumps({"ha": "Home Assistant"}))
        self.assertEqual(load_overrides(self.path), {"ha": "Home Assistant"})

    def test_wrapped_mapping_with_comment(self):
        self.path.write_text(json.dumps({"_comment": ["hi"], "overrides": {"wg": "WireGuard"}}))
        self.assertEqual(load_overrides(self.path), {"wg": "WireGuard"})

    def test_corrupt_file_falls_back_to_defaults(self):
        self.path.write_text("{not json")
        self.assertEqual(load_overrides(self.path), {})

    def test_shipped_example_file_is_valid(self):
        loaded = load_overrides(ROOT / "tools" / "naming-overrides.example.json")
        self.assertEqual(loaded.get("qbittorrent"), "qBittorrent")
        self.assertEqual(loaded.get("ha"), "Home Assistant")


def app(slug, name, provider=None):
    return {"slug": slug, "name": name, "provider": provider}


def provider(pk, name, host):
    return {"pk": pk, "name": name, "external_host": f"https://{host}",
            "assigned_application_slug": None}


class TestAudit(unittest.TestCase):
    OV = {"qbittorrent": "qBittorrent", "ha": "Home Assistant"}

    def test_compliant_names_produce_no_changes(self):
        r = normalize_names.audit([app("sonarr", "Sonarr", 1)],
                                  [provider(1, "Sonarr Proxy Provider", "sonarr.example.com")],
                                  self.OV)
        self.assertEqual(r["changes"], [])

    def test_lowercase_name_is_flagged(self):
        r = normalize_names.audit([app("sonarr", "sonarr", 1)],
                                  [provider(1, "Sonarr Proxy Provider", "sonarr.example.com")],
                                  self.OV)
        self.assertEqual(len(r["changes"]), 1)
        self.assertEqual(r["changes"][0]["suggested"], "Sonarr")
        self.assertEqual(r["changes"][0]["kind"], "application")

    def test_override_drives_the_suggestion(self):
        r = normalize_names.audit([app("ha", "Ha", 1)],
                                  [provider(1, "Ha Proxy Provider", "ha.example.com")],
                                  self.OV)
        suggestions = {c["suggested"] for c in r["changes"]}
        self.assertIn("Home Assistant", suggestions)
        self.assertIn("Home Assistant Proxy Provider", suggestions)

    def test_collision_qualified_provider_name_is_left_alone(self):
        """The companion's own cross-apex tie-breaker must not be reported as drift."""
        r = normalize_names.audit(
            [app("admin-example-org", "Admin (example.org)", 1)],
            [provider(1, "admin.example.org Proxy Provider", "admin.example.org")],
            {})
        provider_changes = [c for c in r["changes"] if c["kind"] == "provider"]
        self.assertEqual(provider_changes, [])

    def test_application_without_provider_is_noted_never_changed(self):
        r = normalize_names.audit([app("plex", "plex", None)], [], self.OV)
        self.assertEqual(r["changes"], [])
        self.assertEqual(r["notes"][0]["kind"], "no-provider")

    def test_slug_rename_is_noted_never_changed(self):
        r = normalize_names.audit(
            [app("qbittorrent", "qBittorrent", 1)],
            [provider(1, "qBittorrent Proxy Provider", "qbit.example.com")],
            self.OV)
        self.assertEqual(r["changes"], [])
        kinds = {n["kind"] for n in r["notes"]}
        self.assertIn("slug-differs", kinds)

    def test_two_apps_normalising_to_one_name_are_flagged(self):
        r = normalize_names.audit(
            [app("admin", "admin", 1), app("admin-example-org", "admin", 2)],
            [provider(1, "A Proxy Provider", "admin.example.com"),
             provider(2, "B Proxy Provider", "admin.example.org")],
            {"admin": "Admin", "admin-example-org": "Admin"})
        self.assertIn("name-collision", {n["kind"] for n in r["notes"]})

    def test_patch_paths_target_the_right_objects(self):
        r = normalize_names.audit([app("db", "Db", 7)],
                                  [provider(7, "Db Proxy Provider", "db.example.com")], {})
        paths = {c["kind"]: c["path"] for c in r["changes"]}
        self.assertEqual(paths["application"], "/api/v3/core/applications/db/")
        self.assertEqual(paths["provider"], "/api/v3/providers/proxy/7/")


class TestDerivedSlug(unittest.TestCase):
    def test_leftmost_label_only(self):
        self.assertEqual(normalize_names.derived_slug("sonarr.example.com"), "sonarr")

    def test_non_alphanumerics_become_dashes(self):
        self.assertEqual(normalize_names.derived_slug("win_console.example.com"), "win-console")

    def test_empty_host(self):
        self.assertEqual(normalize_names.derived_slug(""), "")


if __name__ == "__main__":
    unittest.main()


class TestProviderNameQualification(unittest.TestCase):
    """Apps may share a display name; providers may not — Authentik enforces it."""

    DOM = {"actanumeratorum.com": {"group": "Acta", "prefix": ""},
           "distraktr.com": {"group": "Plexy", "prefix": ""}}

    def test_family_is_prepended_for_providers(self):
        self.assertEqual(canonical_provider_name("Portainer", "Acta"),
                         "Acta Portainer Proxy Provider")

    def test_no_family_leaves_the_name_bare(self):
        self.assertEqual(canonical_provider_name("Portainer"), "Portainer Proxy Provider")

    def test_family_is_not_doubled(self):
        self.assertEqual(canonical_provider_name("Acta Admin", "Acta"),
                         "Acta Admin Proxy Provider")

    def test_two_domains_aliasing_one_service_do_not_collide(self):
        """The live failure: docker@acta and portainer@plexy both display 'Portainer'."""
        acta = canonical_provider_name("Portainer", family_for("actanumeratorum.com", self.DOM))
        plexy = canonical_provider_name("Portainer", family_for("distraktr.com", self.DOM))
        self.assertNotEqual(acta, plexy)
        self.assertEqual(acta, "Acta Portainer Proxy Provider")
        self.assertEqual(plexy, "Plexy Portainer Proxy Provider")
