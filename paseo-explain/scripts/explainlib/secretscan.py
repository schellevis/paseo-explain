"""Secret file names and secret-like text patterns."""

import fnmatch
import re

SECRET_FILE_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.kdbx",
    "credentials*",
    "*secret*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "*.tfstate",
    "*.tfstate.*",
]
SECRET_FILE_EXCEPTIONS = [".env.example", ".env.sample", ".env.template", ".env.dist"]

TOKEN_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
]

ASSIGNMENT_PATTERNS = [
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\b"
        r"\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"
    ),
    re.compile(
        r"(?m)^\s*(export\s+)?[A-Z][A-Z0-9_]*"
        r"(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY)"
        r"[A-Z0-9_]*\s*=\s*[^\s'\"#]{8,}"
    ),
]


def is_secret_file(basename) -> bool:
    name = str(basename).casefold()
    if name in (item.casefold() for item in SECRET_FILE_EXCEPTIONS):
        return False
    return any(fnmatch.fnmatchcase(name, pattern.casefold()) for pattern in SECRET_FILE_PATTERNS)


def find_secrets(text) -> list:
    found = []
    if any(pattern.search(text) for pattern in TOKEN_PATTERNS):
        found.append("token")
    if any(pattern.search(text) for pattern in ASSIGNMENT_PATTERNS):
        found.append("assignment")
    return found


def find_secrets_in(obj) -> bool:
    if isinstance(obj, str):
        return bool(find_secrets(obj))
    if isinstance(obj, dict):
        return any(find_secrets_in(key) or find_secrets_in(value) for key, value in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(find_secrets_in(item) for item in obj)
    return False
