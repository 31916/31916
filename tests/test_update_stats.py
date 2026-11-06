import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/update_stats.py"
SPEC = importlib.util.spec_from_file_location("update_stats", MODULE_PATH)
stats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stats)


def commit(identifier, timestamp, author="31916", parents=1, author_type="User"):
    return {
        "sha": f"{identifier:040x}",
        "author": {"login": author, "type": author_type},
        "commit": {"author": {"date": timestamp, "email": "do-not-publish@example.invalid"}, "message": "Do not publish this raw message"},
        "parents": [{}] * parents,
    }


def repository(name="demo", **changes):
    result = {"name": name, "owner": {"login": "31916"}, "private": False, "visibility": "public", "fork": False, "default_branch": "main"}
    result.update(changes)
    return result


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.start = stats.parse_time("2026-08-08T00:00:00Z")
        self.end = stats.parse_time("2026-11-06T00:00:00Z")

    def test_language_bytes_are_aggregated_before_percentage(self):
        rows = stats.aggregate_languages([{"Python": 30, "Java": 70}, {"Python": 100, "HTML": 0}])
        self.assertEqual([row["name"] for row in rows], ["Python", "Java"])
        self.assertEqual([row["bytes"] for row in rows], [130, 70])
        self.assertEqual([row["share_percent"] for row in rows], [65.0, 35.0])
        self.assertEqual(stats.aggregate_languages([{}]), [])
        with self.assertRaises(ValueError):
            stats.aggregate_languages([{"Java": -1}])

    def test_only_public_owned_nonfork_repositories(self):
        self.assertTrue(stats.eligible_repository(repository(archived=True), "31916"))
        for repo in [repository(private=True), repository(fork=True), repository("31916"), repository(visibility="private"), repository(owner={"login": "someone-else"})]:
            with self.subTest(repo=repo):
                self.assertFalse(stats.eligible_repository(repo, "31916"))

    def test_deduplication_author_filter_merges_and_boundaries(self):
        valid = commit(1, "2026-10-01T15:30:00Z")
        data = stats.aggregate_activity([
            valid, valid,
            commit(2, "2026-08-08T00:00:00Z"),
            commit(3, "2026-11-06T00:00:00Z"),
            commit(4, "2026-08-07T23:59:59Z"),
            commit(5, "2026-11-06T00:00:01Z"),
            commit(6, "2026-10-01T15:30:00Z", author="another"),
            commit(7, "2026-10-01T15:30:00Z", parents=2),
            commit(8, "2026-10-01T15:30:00Z", author_type="Bot"),
        ], "31916", self.start, self.end)
        self.assertEqual(data["commits"], 3)
        self.assertEqual(data["active_days"], 3)
        self.assertEqual(data["hourly_commits_jst"][0], 1)
        self.assertEqual(data["hourly_commits_jst"][9], 2)
        self.assertEqual(sum(data["weekday_commits_jst"]), 3)

    def test_jst_date_rollover_and_offsets(self):
        data = stats.aggregate_activity([
            commit(1, "2026-10-01T14:59:59Z"),
            commit(2, "2026-10-02T00:00:00+09:00"),
            commit(3, "2026-10-01T17:00:00+02:00"),
        ], "31916", self.start, self.end)
        self.assertEqual(data["active_days"], 2)
        self.assertEqual(data["hourly_commits_jst"][23], 1)
        self.assertEqual(data["hourly_commits_jst"][0], 2)
        self.assertEqual(data["weekday_commits_jst"][3:5], [1, 2])

    def test_full_pagination_including_exact_page(self):
        class FakeApi(stats.GitHubApi):
            def __init__(self):
                self.calls = []
            def get(self, endpoint):
                self.calls.append(endpoint)
                if parse_qs(urlsplit(endpoint).query)["page"] == ["1"]:
                    return list(range(100))
                return [100]
        api = FakeApi()
        self.assertEqual(list(api.pages("/example", {"author": "31916"})), list(range(101)))
        self.assertEqual(len(api.calls), 2)
        self.assertIn("per_page=100", api.calls[0])
        self.assertIn("page=2", api.calls[1])

    def test_collect_sanitizes_raw_fields_and_skips_private_queries(self):
        class FakeApi:
            def pages(self, path, parameters=None):
                if path.startswith("/users/"):
                    return iter([repository(), repository("secret", private=True), repository("31916")])
                self.assert_path(path)
                return iter([commit(1, "2026-10-01T15:00:00Z")])
            def assert_path(self, path):
                if "/demo/" not in path:
                    raise AssertionError(path)
            def get(self, path):
                self.assert_path(path)
                return {"Python": 20, "Java": 80}
        data = stats.collect(FakeApi(), "31916", self.end)
        serialized = json.dumps(data)
        for private_field in ("secret", "do-not-publish", "raw message", "2026-10-01T15:00:00Z", "email"):
            self.assertNotIn(private_field, serialized)
        self.assertEqual(len(data["repositories"]), 1)
        self.assertEqual(data["activity"]["commits"], 1)

    def test_only_explicit_empty_repository_409_is_ignored(self):
        class FakeApi:
            def __init__(self, message):
                self.message = message
            def get(self, path):
                return {}
            def pages(self, path, parameters=None):
                if path.startswith("/users/"):
                    return iter([repository()])
                raise stats.ApiError(409, self.message)
        data = stats.collect(FakeApi("Git Repository is empty."), "31916", self.end)
        self.assertTrue(data["repositories"][0]["empty"])
        with self.assertRaises(stats.ApiError):
            stats.collect(FakeApi("A different conflict"), "31916", self.end)

    def test_svg_is_well_formed_escaped_and_zero_safe(self):
        class EmptyApi:
            def pages(self, *args):
                return iter([])
        data = stats.collect(EmptyApi(), "31916", self.end)
        for locale in ("en", "ja", "de"):
            for render in (stats.render_languages, stats.render_activity):
                value = render(data, locale)
                ET.fromstring(value)
                self.assertNotIn("nan", value.casefold())
                self.assertNotIn("Infinity", value)
        data["languages"] = {"items": [{"name": "A&B <test>", "bytes": 1, "share_percent": 100.0}], "total_bytes": 1}
        rendered = stats.render_languages(data, "en")
        ET.fromstring(rendered)
        self.assertIn("A&amp;B &lt;test&gt;", rendered)

    def test_writes_exactly_expected_generated_files(self):
        class EmptyApi:
            def pages(self, *args):
                return iter([])
        data = stats.collect(EmptyApi(), "31916", self.end)
        files = stats.output_files(data)
        self.assertEqual(len(files), 7)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "stats"
            stats.write_outputs(files, destination)
            self.assertEqual(set(files), {path.name for path in destination.iterdir()})
            self.assertEqual(json.loads((destination / "data.json").read_text(encoding="utf-8")), data)

    def test_failed_fetch_preserves_last_successful_output(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            existing = destination / "data.json"
            existing.write_text("last successful data", encoding="utf-8")
            with patch.object(stats.sys, "argv", ["update_stats.py", "--output-dir", directory]), \
                 patch.object(stats, "collect", side_effect=stats.ApiError(403, "Forbidden")), \
                 patch("builtins.print"):
                self.assertEqual(stats.main(), 1)
            self.assertEqual(existing.read_text(encoding="utf-8"), "last successful data")
            self.assertEqual([path.name for path in destination.iterdir()], ["data.json"])


if __name__ == "__main__":
    unittest.main()
