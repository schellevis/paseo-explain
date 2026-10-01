import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paseo-explain" / "scripts"))

from explainlib import leaks, secretscan  # noqa: E402

PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"

TOKEN_POSITIVE = [
    "sk-" + "A" * 24,
    "ghp_" + "b" * 20,
    "AKIA" + "A1" * 8,
    PRIVATE_KEY,
    "github_pat_" + "a1_" * 8,
    "xoxb-" + "1234567890",
    "AIza" + "x" * 35,
    "glpat-" + "a-b_" * 6,
]
TOKEN_NEAR_MISS = [
    "sk-" + "A" * 7,
    "ghp_" + "b" * 7,
    "AKIA" + "A" * 15,
    "-----BEGIN " + "CERTIFICATE-----",
    "github_pat_" + "a" * 19,
    "xoxz-" + "1234567890",
    "AIza" + "x" * 34,
    "glpat-" + "a" * 19,
]


class SecretFileTests(unittest.TestCase):
    def test_patterns_match(self):
        names = [
            ".env", ".env.local", "server.pem", "a.key", "a.p12", "a.pfx", "a.keystore",
            "a.jks", "id_rsa", "id_rsa.pub", "id_dsa", "id_ecdsa", "id_ed25519", "db.kdbx",
            "credentials", "credentials.json", "my_secrets.yml", "Secret.txt", ".npmrc",
            ".pypirc", ".netrc", "terraform.tfstate", "terraform.tfstate.backup",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(secretscan.is_secret_file(name))

    def test_near_misses(self):
        for name in ["README.md", "environment.py", "env", "a.keys", "main.py", "idea.md", "npmrc"]:
            with self.subTest(name=name):
                self.assertFalse(secretscan.is_secret_file(name))

    def test_exceptions(self):
        for name in [".env.example", ".env.sample", ".env.template", ".env.dist"]:
            with self.subTest(name=name):
                self.assertIn(name, secretscan.SECRET_FILE_EXCEPTIONS)
                self.assertFalse(secretscan.is_secret_file(name))
                self.assertFalse(secretscan.is_secret_file(name.upper()))

    def test_case_insensitive(self):
        for name in [".ENV", "SERVER.PEM", "ID_RSA", "Credentials.JSON", ".NpmRC", "A.TFSTATE.backup"]:
            with self.subTest(name=name):
                self.assertTrue(secretscan.is_secret_file(name))


class FindSecretsTests(unittest.TestCase):
    def test_token_patterns_count_and_order(self):
        self.assertEqual(len(secretscan.TOKEN_PATTERNS), 8)

    def test_each_token_positive_and_near_miss(self):
        for index, sample in enumerate(TOKEN_POSITIVE):
            with self.subTest(index=index):
                self.assertTrue(secretscan.TOKEN_PATTERNS[index].search("x " + sample + " y"))
                self.assertEqual(secretscan.find_secrets("x " + sample + " y"), ["token"])
        for index, sample in enumerate(TOKEN_NEAR_MISS):
            with self.subTest(index=index):
                self.assertFalse(secretscan.TOKEN_PATTERNS[index].search(sample))
                self.assertEqual(secretscan.find_secrets(sample), [])

    def test_quoted_assignment(self):
        self.assertEqual(secretscan.find_secrets('password = "' + "p" * 8 + '"'), ["assignment"])
        self.assertEqual(secretscan.find_secrets("API_KEY: '" + "k" * 12 + "'"), ["assignment"])
        self.assertEqual(secretscan.find_secrets('password = "' + "p" * 7 + '"'), [])
        self.assertEqual(secretscan.find_secrets('password = "has space here"'), [])
        self.assertEqual(secretscan.find_secrets("password = " + "p" * 12), [])

    def test_unquoted_env_assignment(self):
        self.assertEqual(secretscan.find_secrets("DB_PASSWORD=" + "p" * 8), ["assignment"])
        self.assertEqual(secretscan.find_secrets("x=1\nexport AWS_SECRET_KEY = " + "p" * 10), ["assignment"])
        self.assertEqual(secretscan.find_secrets("DB_PASSWORD=" + "p" * 7), [])
        self.assertEqual(secretscan.find_secrets("db_password=" + "p" * 12), [])
        self.assertEqual(secretscan.find_secrets("  # DB_PASSWORD=" + "p" * 12), [])

    def test_both_classes_deduplicated(self):
        text = "\n".join(["sk-" + "A" * 24, "ghp_" + "b" * 20, 'token = "' + "t" * 10 + '"'])
        self.assertEqual(secretscan.find_secrets(text), ["token", "assignment"])

    def test_never_returns_matched_text(self):
        secret = "sk-" + "Z" * 24
        result = secretscan.find_secrets("a " + secret)
        self.assertTrue(all(item in ("token", "assignment") for item in result))
        self.assertNotIn(secret, repr(result))

    def test_find_secrets_in(self):
        secret = "sk-" + "A" * 24
        self.assertTrue(secretscan.find_secrets_in(secret))
        self.assertTrue(secretscan.find_secrets_in({"a": [1, {"b": ["ok", secret]}]}))
        self.assertTrue(secretscan.find_secrets_in([[["x"], [secret]]]))
        self.assertTrue(secretscan.find_secrets_in({secret: "fine"}))
        self.assertTrue(secretscan.find_secrets_in({"a": {"b": {secret: 1}}}))
        self.assertFalse(secretscan.find_secrets_in({"a": [1, None, True, 2.5, {"b": "plain"}]}))
        self.assertFalse(secretscan.find_secrets_in([]))
        self.assertFalse(secretscan.find_secrets_in(None))


class LeaksUseTokenPatternsTests(unittest.TestCase):
    def test_leaks_patterns_include_token_patterns(self):
        secret_patterns = [p for kind, p in leaks.PATTERNS if kind == "secret"]
        self.assertEqual(secret_patterns, list(secretscan.TOKEN_PATTERNS))

    def test_scan_text_flags_new_shapes(self):
        for sample in TOKEN_POSITIVE[4:]:
            with self.subTest(sample=sample[:6]):
                hits = leaks.scan_text("value " + sample + "\n", [])
                self.assertEqual([h["kind"] for h in hits], ["secret"])


if __name__ == "__main__":
    unittest.main()
