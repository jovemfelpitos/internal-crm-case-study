"""Check Git-tracked files before publishing this demo."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_DIRS = {
    "instance", "backups", "production-backups", "production-hotfixes",
    ".venv", "venv", "ops", "__pycache__",
}
BLOCKED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".xls", ".xlsx", ".csv",
    ".parquet", ".log", ".zip", ".tar", ".gz",
}
PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"GOCSPX-[A-Za-z0-9_-]{15,}|AKIA[0-9A-Z]{16}"),
    re.compile(r"sk_live_[A-Za-z0-9]{12,}"),
]

def main():
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True,
        stdout=subprocess.PIPE,
    )
    findings = []
    paths = [Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name]
    for path in paths:
        if any(part in BLOCKED_DIRS for part in path.parts):
            findings.append((path, "pasta operacional"))
        if path.suffix.lower() in BLOCKED_SUFFIXES or re.search(r"\.(?:sqlite3?|db)-(?:wal|shm|journal)$", path.name):
            findings.append((path, "arquivo de dados/backup"))
        if path.name.startswith(".env") and path.name != ".env.example":
            findings.append((path, "arquivo de ambiente"))
        raw = (ROOT / path).read_bytes()
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2"}:
            continue
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            findings.append((path, "binario nao previsto"))
            continue
        if any(pattern.search(content) for pattern in PATTERNS):
            findings.append((path, "possivel credencial"))
    if findings:
        for path, reason in findings:
            print(f"{path}: {reason}", file=sys.stderr)
        return 1
    print(f"Publicacao conferida: {len(paths)} arquivos rastreados, sem achados nos padroes verificados.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
