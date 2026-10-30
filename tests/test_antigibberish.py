"""Aggressive keysmash tests with safe normal-language examples."""
import ast
from collections import Counter
import re
import unicodedata
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1].joinpath("messaging_app.py").read_text()
TREE = ast.parse(SOURCE)
FUNCTIONS = [node for node in TREE.body
             if isinstance(node, ast.FunctionDef) and node.name in (
                 'gibberish_reason', 'is_gibberish')]
env = {'re': re, 'Counter': Counter, 'unicodedata': unicodedata}
exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]),
             '<gibberish>', 'exec'), env)


class AntiGibberishTests(unittest.TestCase):
    def test_reject_repeated_keysmashes_and_long_random_strings(self):
        spam = [
            'a' * 50,
            '😆' * 25,
            'asdf' * 20,
            'qwrtqwrtqwrtqwrtqwrtqwrtqwrtqwrtqwrtqwrt',
            'aaaaaaaa-bbbbbbbb-aaaaaaaa-bbbbbbbb-aaaaaaaa-bbbbbbbb',
            'hello ' * 16,
            'qwertyuiopasdfghjklqwertyuiopasdfghjkl',
            'q3jhfggqhrfjgqregfhwjerfghwegrhjfgehrjfbgv',
            'jghfkgjhsfdkjghgfkjsdhfgjkshdgfkjshdfjskhd',
            'bbccddffgghhjjkkllmmnnppqqrrssttvvxxzz',
        ]
        for message in spam:
            with self.subTest(message=message):
                self.assertTrue(env['is_gibberish'](message), message)

    def test_preserve_human_messages(self):
        normal = [
            'hi',
            'lol',
            'brb omg 😂',
            'NOOOOOO',
            'this is sooo funny hahaha',
            'What homework is due tomorrow?',
            'can u help with compound inequalities?',
            'Привет, как дела?',
            'hola buenos días ¿cómo estás?',
            '你好，今天怎么样？',
            'مرحبا كيف حالك',
            '🤷🤷🤷',
            'antidisestablishmentarianism',
            'pneumonoultramicroscopicsilicovolcanoconiosis',
            'https://github.com/example/repository',
            'print("hello world")',
            'RTX5060 32GB DDR4 1TB',
            '5551234567',
            "Can't join because I'm getting a different error.",
            'gggggggggg',
        ]
        for message in normal:
            with self.subTest(message=message):
                self.assertFalse(env['is_gibberish'](message), message)

    def test_explains_rejections(self):
        self.assertIsInstance(env['gibberish_reason']('z'*40), str)
        self.assertIsNone(env['gibberish_reason']('Hey what is going on?'))


if __name__ == '__main__':
    unittest.main()
