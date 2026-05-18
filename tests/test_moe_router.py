import importlib.util
import unittest
import time


def load_moe_module():
    spec = importlib.util.spec_from_file_location('moe_router', 'mcp-ai/moe_router.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MoeRouterTests(unittest.TestCase):
    def test_rule_mode_moe_call(self):
        mod = load_moe_module()

        # stub bridge call to return deterministic responses
        def fake_call(messages, model=None, timeout=60):
            return 'MOCK_RESP for ' + (messages[-1]['content'][:30] if messages else 'NO_MSG')

        mod._call_bridge = fake_call
        res = mod.moe_call('Please summarize company revenue and market traction', debug=True, mode='rule')
        self.assertIsInstance(res, str)
        self.assertIn('MOCK_RESP', res)

    def test_embedding_mode_fallback(self):
        mod = load_moe_module()
        # force embedding subsystem to be unavailable
        mod._ensure_embedding_model = lambda: False
        mod._call_bridge = lambda messages, model=None, timeout=60: 'MOCK'
        res = mod.moe_call('Short query for embedding test', debug=False, mode='embedding')
        self.assertIsInstance(res, str)

    def test_expert_concurrency(self):
        mod = load_moe_module()

        def slow_call(messages, model=None, timeout=60):
            time.sleep(0.05)
            return 'OK'

        mod._call_bridge = slow_call
        res = mod.moe_call('Test concurrency behavior', debug=True, mode='rule')
        self.assertIsInstance(res, str)


if __name__ == '__main__':
    unittest.main()
