"""Shared rules: private env grammar, secret scope, redaction, VM layout, session identity."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('compute_common', Path(__file__).with_name('compute_common.py'))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


class EnvGrammarTests(unittest.TestCase):
    def test_strict_parse_accepts_literals_comments_and_export(self):
        text = ('# comment\n\nexport INFER_API_KEY="quoted value"\n'
                "RAG_COMPUTE_API_KEY='single'\nEMPTY=\nTRAILING=abc # note\n")
        self.assertEqual(common.parse_env(text), {'INFER_API_KEY': 'quoted value',
                                                  'RAG_COMPUTE_API_KEY': 'single',
                                                  'EMPTY': '', 'TRAILING': 'abc'})

    def test_strict_parse_rejects_anything_but_a_single_literal(self):
        for text in ['NAME=two words\n', 'lowercase=value\n', 'no equals sign\n', '1BAD=x\n']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                common.parse_env(text)

    def test_selected_names_ignore_unrelated_user_lines_but_validate_chosen_ones(self):
        text = 'PATH_EXTRA=$HOME/bin:$PATH extra\nnot an assignment\nINFER_API_KEY=x\n'
        self.assertEqual(common.parse_env(text, names={'INFER_API_KEY'}), {'INFER_API_KEY': 'x'})
        with self.assertRaises(ValueError):
            common.parse_env('INFER_API_KEY=two words\n', names={'INFER_API_KEY'})

    def test_hash_and_comments_read_like_bash_source(self):
        # bash keeps '#' inside a word; only a separate '#...' word starts a comment.
        text = ('INFER_API_KEY=abc#def-0123456789\nA=x # note\nB= # only a comment\n'
                "C='quoted # kept'\nD=a\\bc\nE=#leading\nF='#quoted-leading'\n"
                'G="x # in dquotes"\nH=x\\ #c\n')
        self.assertEqual(common.parse_env(text), {'INFER_API_KEY': 'abc#def-0123456789',
                                                  'A': 'x', 'B': '', 'C': 'quoted # kept',
                                                  'D': 'abc', 'E': '#leading',
                                                  'F': '#quoted-leading', 'G': 'x # in dquotes',
                                                  'H': 'x #c'})
        for bad in ('I=a;#b\n', 'J=a|b\n', 'K=$(x)\n'[:0] or 'K=a&b\n', "L=`x`\n", 'M=a>b\n'):
            with self.assertRaises(ValueError, msg=bad):
                common.parse_env(bad)
        self.assertEqual(common.parse_env("N='a;b'\n"), {'N': 'a;b'})

    def test_format_round_trips_shell_special_characters(self):
        values = {'A_KEY': "a b#c'd\"e$f`g\\h", 'EMPTY': '', 'JSON': '{"method": "mtp"}'}
        self.assertEqual(common.parse_env(common.format_env(values)), values)


class SecretTests(unittest.TestCase):
    def test_secret_scope_is_one_rule(self):
        for name in ['INFER_API_KEY', 'RAG_COMPUTE_API_KEY', 'HF_TOKEN', 'TS_AUTHKEY',
                     'FUTURE_SERVICE_KEY', 'SOME_TOKEN']:
            self.assertTrue(common.is_secret_name(name), name)
        for name in ['INFER_MODEL_REPO', 'HF_HOME', 'INFER_KV_DTYPE', 'KEYBOARD']:
            self.assertFalse(common.is_secret_name(name), name)
        self.assertEqual(common.secret_values({'HF_TOKEN': 'hf_x', 'HF_HOME': '/m', 'A_KEY': ''}),
                         ['hf_x'])

    def test_redaction_covers_values_token_shapes_and_signed_queries(self):
        text = ('value=synthetic-secret-123 tskey-auth-abc_DEF hf_Abc123 Bearer abc.def '
                'https://example.test/file?X-Amz-Signature=zzz end')
        result = common.redact(text, ['synthetic-secret-123'])
        for leaked in ['synthetic-secret-123', 'tskey-auth-abc_DEF', 'hf_Abc123', 'abc.def', 'zzz']:
            self.assertNotIn(leaked, result)
        self.assertIn('https://example.test/file?[REDACTED_QUERY]', result)


class LayoutTests(unittest.TestCase):
    def test_lease_without_layout_is_legacy_and_copies_are_independent(self):
        self.assertEqual(common.layout_of({'session': 'agent-demo-0123456789ab'}),
                         common.LEGACY_LAYOUT)
        layout = common.layout_of({'vm_layout': common.LAYOUT})
        layout['private'] = '/elsewhere'
        self.assertEqual(common.LAYOUT['private'], '/content/agent-compute-private')

    def test_owned_sessions_are_exactly_both_generated_shapes(self):
        for name in ['agent-compute-0123456789ab', 'agent-demo-0123456789ab']:
            self.assertTrue(common.is_owned_session(name))
        for name in ['llm', 'agent-compute-0123', 'agent-other-0123456789ab',
                     'agent-compute-0123456789abX', None, '']:
            self.assertFalse(common.is_owned_session(name))


if __name__ == '__main__':
    unittest.main()
