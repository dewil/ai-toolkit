"""Independent CLI acceptance fixtures for TOOLKIT-FRONTMATTER-20261010.

The fixtures encode the specification, not the producer implementation. Tests use
only stdlib; PyYAML is a dependency of the command under test, not this module.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "tools" / "check-frontmatter.py"
# Frozen original incident: the second colon followed by a space is invalid YAML.
INCIDENT_DESCRIPTION = (
    "SDD-конвейер - как записанная спека ведет изменение от замысла до мержа: "
    "доменная спека"
)


class CanonFrontmatterCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write("manifest.yaml", "universal:\n  - rules/example.md\n")
        self.write("rules/example.md", "---\ndescription: Valid description\n---\nBody\n")

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, expected=0, diagnostic=None, python_options=()):
        self.assertTrue(CLI.is_file(), "Missing public CLI tools/check-frontmatter.py")
        result = subprocess.run(
            [sys.executable, *python_options, str(CLI), "--root", str(self.root)],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, expected, output)
        if diagnostic:
            self.assertIn(diagnostic, output)
            self.assertNotIn(str(self.root / diagnostic), output)
        self.assertNotIn("PRIVATE_BODY_SENTINEL", output)
        return output

    def test_original_unquoted_colon_space_is_metadata_error(self):
        self.write("rules/example.md", "---\ndescription: " + INCIDENT_DESCRIPTION + "\n---\n")
        self.run_cli(1, "rules/example.md")

    def test_quoted_original_description_is_valid(self):
        self.write("rules/example.md", '---\ndescription: "' + INCIDENT_DESCRIPTION + '"\n---\n')
        self.run_cli()

    def test_valid_yaml_scalar_forms_and_bom(self):
        for description in (
            "plain description", "'description: with colon'", '"description: with colon"',
            ">-\n  Folded description:\n  second line", "|\n  Literal description:\n  second line",
        ):
            for prefix in ("", "\ufeff"):
                with self.subTest(description=description, bom=bool(prefix)):
                    self.write("rules/example.md", prefix + "---\ndescription: " + description + "\n---\n")
                    self.run_cli()

    def test_markdown_body_is_ignored(self):
        self.write("rules/example.md", "---\ndescription: Valid\n---\nPRIVATE_BODY_SENTINEL\n"
                   "description: invalid: YAML\n---\n```yaml\n!!python/object:broken\n"
                   "duplicate: first\nduplicate: second\n```\n")
        self.run_cli()

    def test_safe_alias_and_vendor_fields_are_allowed(self):
        self.write("rules/example.md", "---\nvendor: &text 'A valid description'\n"
                   "description: *text\nvendor_mapping:\n  numbers: [1, 2]\n---\n")
        self.run_cli()

    def test_safe_recursive_vendor_alias_is_allowed(self):
        self.write("rules/example.md", "---\ndescription: Valid\nvendor: &v\n"
                   "  self: *v\n---\n")
        self.run_cli()

    def test_valid_merge_sequence_and_explicit_override_are_allowed(self):
        for header in (
            "defaults: &d {description: Default}\n<<: *d\ndescription: Override",
            "first: &a {description: One}\nsecond: &b {description: Two}\n<<: [*a, *b]",
        ):
            with self.subTest(header=header):
                self.write("rules/example.md", "---\n" + header + "\n---\n")
                self.run_cli()

    def test_duplicate_explicit_merge_keys_are_rejected(self):
        self.write("rules/example.md", "---\nfirst: &a {description: One}\n"
                   "second: &b {description: Two}\n<<: *a\n<<: *b\n---\n")
        self.run_cli(1, "rules/example.md")

    def test_missing_or_unclosed_header_is_error(self):
        for content in ("# No header\n", "\n---\ndescription: Valid\n---\n",
                        "---\ndescription: Valid\n", "---\ndescription: Valid\n--- trailing\n"):
            with self.subTest(content=content):
                self.write("rules/example.md", content)
                self.run_cli(1, "rules/example.md")

    def test_header_requires_one_mapping(self):
        for header in ("", "null", "[]", "- description", "just a scalar", "42",
                       "description: Valid\n...\ndescription: Second"):
            with self.subTest(header=header):
                self.write("rules/example.md", "---\n" + header + "\n---\n")
                self.run_cli(1, "rules/example.md")

    def test_description_is_required_nonempty_string(self):
        for header in ("vendor: valid", "description:", "description: null", "description: ''",
                       "description: '   '", "description: []", "description: {}",
                       "description: 7", "description: true"):
            with self.subTest(header=header):
                self.write("rules/example.md", "---\n" + header + "\n---\n")
                self.run_cli(1, "rules/example.md")

    def test_skills_and_agents_require_nonempty_string_name(self):
        for relative in ("skills/demo/SKILL.md", "agents/demo.md"):
            self.write("manifest.yaml", "universal:\n  - " + relative + "\n")
            for name in (None, "", "null", "''", "'   '", "[]", "{}", "42", "true"):
                with self.subTest(relative=relative, name=name):
                    field = "" if name is None else "name: " + name + "\n"
                    self.write(relative, "---\ndescription: Valid\n" + field + "---\n")
                    self.run_cli(1, relative)

    def test_skill_and_agent_names_are_valid_strings(self):
        self.write("manifest.yaml", "universal:\n  - skills/demo/SKILL.md\ncoding:\n  - agents/demo.md\n")
        for relative in ("skills/demo/SKILL.md", "agents/demo.md"):
            self.write(relative, "---\nname: Demo\ndescription: Valid\n---\n")
        self.run_cli()

    def test_duplicate_keys_are_rejected(self):
        for header in ("description: First\ndescription: Second", "description: Valid\n"
                       "vendor: first\nvendor: second", "description: Valid\nvendor:\n"
                       "  nested: first\n  nested: second"):
            with self.subTest(header=header):
                self.write("rules/example.md", "---\n" + header + "\n---\n")
                self.run_cli(1, "rules/example.md")

    def test_unsafe_python_tag_is_rejected_without_execution(self):
        marker = self.root / "must-not-exist"
        self.write("rules/example.md", "---\ndescription: !!python/object/apply:builtins.eval\n"
                   "  - \"__import__('pathlib').Path('" + str(marker) + "').touch()\"\n---\n")
        self.run_cli(1, "rules/example.md")
        self.assertFalse(marker.exists())

    def test_registered_files_in_multiple_manifest_sections_are_checked(self):
        self.write("manifest.yaml", "universal:\n  - rules/example.md\ncoding:\n  - agents/demo.md\n")
        self.write("agents/demo.md", "---\nname: Demo\ndescription: invalid: colon\n---\n")
        self.run_cli(1, "agents/demo.md")

    def test_actual_unregistered_metadata_categories_are_checked(self):
        for relative in ("rules/unregistered.md", "agents/demo.md", "skills/demo/SKILL.md"):
            with self.subTest(relative=relative):
                path = self.write(relative, "---\nname: Demo\ndescription: invalid: colon\n---\n")
                self.run_cli(1, relative)
                path.unlink()

    def test_registered_and_actual_file_is_deduplicated(self):
        self.write("manifest.yaml", "universal:\n  - rules/example.md\ncoding:\n  - rules/example.md\n")
        self.write("rules/example.md", "---\ndescription: invalid: colon\n---\n")
        output = self.run_cli(1, "rules/example.md")
        self.assertEqual(output.count("rules/example.md"), 1, output)

    def test_missing_registered_metadata_is_error(self):
        for relative in ("rules/missing.md", "agents/missing.md", "skills/missing/SKILL.md"):
            with self.subTest(relative=relative):
                self.write("manifest.yaml", "universal:\n  - rules/example.md\ncoding:\n  - " + relative + "\n")
                self.run_cli(1, relative)

    def test_support_files_and_templates_are_ignored_even_if_registered(self):
        support = ("skills/demo/reference.md", "skills/demo/helper.py", "scripts/example.py",
                   "templates/example.md", "docs/support.md")
        self.write("manifest.yaml", "universal:\n  - rules/example.md\n" +
                   "".join("  - " + relative + "\n" for relative in support))
        for relative in support:
            self.write(relative, "PRIVATE_BODY_SENTINEL\nInvalid YAML: :\n")
        self.run_cli()

    def test_missing_or_invalid_manifest_fails_closed(self):
        manifest = self.root / "manifest.yaml"
        manifest.unlink()
        self.run_cli(2, "manifest.yaml")
        for content in ("universal: [unterminated\n", "[]\n", "null\n"):
            with self.subTest(content=content):
                self.write("manifest.yaml", content)
                self.run_cli(2, "manifest.yaml")

    def test_invalid_unrelated_manifest_path_fails_closed(self):
        self.write("manifest.yaml", "universal:\n  - rules/example.md\n"
                   "  - ../../invalid.py\n")
        self.run_cli(2, "manifest.yaml")

    def test_valid_registered_docs_support_remains_ignored(self):
        self.write("manifest.yaml", "universal:\n  - rules/example.md\n"
                   "  - docs/support.md\n")
        self.write("docs/support.md", "PRIVATE_BODY_SENTINEL\nInvalid YAML: :\n")
        self.run_cli()

    def test_empty_catalog_fails_closed(self):
        (self.root / "rules/example.md").unlink()
        self.write("manifest.yaml", "universal:\n")
        self.run_cli(2)

    def test_selected_file_resolving_outside_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "external.md"
            external.write_text("---\ndescription: Valid\n---\n", encoding="utf-8")
            (self.root / "rules/example.md").unlink()
            (self.root / "rules/example.md").symlink_to(external)
            self.run_cli(2, "rules/example.md")

    def test_external_symlink_directory_is_not_traversed(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            (external / "SKILL.md").write_text("PRIVATE_BODY_SENTINEL\n", encoding="utf-8")
            (self.root / "skills").mkdir()
            (self.root / "skills/external").symlink_to(external, target_is_directory=True)
            self.run_cli()

    def test_missing_pyyaml_fails_closed_with_install_instruction(self):
        # -S suppresses third-party site-packages without importing YAML in tests.
        output = self.run_cli(2, python_options=("-S",))
        self.assertIn("pip install -r requirements-dev.txt", output)


if __name__ == "__main__":
    unittest.main()
