import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/memory-vector.py"


class MemoryVector(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        env = patch.dict(os.environ, HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        key = self.home / "key"
        key.write_text("QDRANT__SERVICE__API_KEY=test-only\n")
        key.chmod(0o600)
        self.config = self.home / ".config/ai-toolkit/memory-vector.json"
        self.config.parent.mkdir(parents=True)
        self.values = dict(client_roots=[str(self.home)], native_projects=str(self.home),
                           qdrant_url="http://127.0.0.1:6333", collection="test_v2",
                           ollama_url="http://127.0.0.1:11434/api/embed", model="test-model",
                           dimension=3, key_file=str(key), query_log=str(self.home / "queries.log"))

    def load(self):
        self.config.write_text(json.dumps(self.values))
        self.config.chmod(0o600)
        return runpy.run_path(str(SCRIPT))

    def test_scope_and_configuration(self):
        backend = self.load()
        self.assertEqual(backend["COLLECTION"], "test_v2")
        self.assertEqual(backend["CLIENT_ROOTS"], (self.home,))
        self.assertEqual(backend["MEMORY_SCOPE_VERSION"], 2)

    def test_invalid_configuration(self):
        for key,value in [('client_roots', []), ('client_roots', ['relative']),
                          ('collection', 'claude_memory'), ('dimension', True),
                          ('qdrant_url','https://example.org'), ('ollama_url','http://127.0.0.1:1/x?secret=test')]:
            with self.subTest(key=key,value=value), patch.dict(self.values,{key:value}):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.load()

    def test_missing_config(self):
        with self.assertRaises((ValueError, RuntimeError)):
            runpy.run_path(str(SCRIPT))

    def test_same_uuid_hash_and_no_empty_prune(self):
        b = self.load()
        project=self.home/'sample'/'.AI'
        memory=project/'memory'
        memory.mkdir(parents=True)
        project_id="84a4cb82-b6cb-42af-b0c4-181ee6086140"
        (project/'project.json').write_text(json.dumps({'project_id':project_id}))
        (memory/'fact.md').write_text('---\ndescription: fixture\n---\nSynthetic fact.\n')
        docs, _ = b['_collect_v2']()
        self.assertEqual(len(docs),1)
        self.assertEqual(docs[0]['id'],b['_v2_uuid'](project_id,'fact.md'))
        h=b['source_health']([])
        (memory/'fact.md').unlink()
        self.assertFalse(b['stable_sources'](h,b['source_health']([])))


if __name__ == '__main__':
    unittest.main()
