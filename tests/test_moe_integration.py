import os
import unittest
import importlib.util


MOE_INTEGRATION = os.environ.get('MOE_RUN_INTEGRATION', '0') in ('1', 'true', 'yes')


def load_moe_module():
    spec = importlib.util.spec_from_file_location('moe_router', 'mcp-ai/moe_router.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(MOE_INTEGRATION, 'Integration tests disabled (set MOE_RUN_INTEGRATION=1)')
class MoeIntegrationTests(unittest.TestCase):
    def test_live_moe_rule(self):
        mod = load_moe_module()
        # Run a simple rule-mode MoE call against the local bridge (OLLAMA_URL)
        out = mod.moe_call('Summarize recent HAL design and goals', debug=True, mode='rule')
        self.assertIsInstance(out, str)
        self.assertTrue(len(out) > 0)

    def test_live_moe_embedding(self):
        mod = load_moe_module()
        out = mod.moe_call('Summarize HAL mission and autosuggest goals', debug=True, mode='embedding')
        self.assertIsInstance(out, str)
        self.assertTrue(len(out) > 0)


if __name__ == '__main__':
    unittest.main()
