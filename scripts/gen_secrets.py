"""Fill the locally generated secrets in .env.

Creates .env from .env.example if it does not exist, then sets
N8N_ENCRYPTION_KEY, INTERNAL_TOKEN and ADMIN_TOKEN to `secrets.token_hex(32)`.
A key is only written while it is empty or still holds its placeholder, so
running this again never rotates a real value (rotating N8N_ENCRYPTION_KEY
would make existing n8n credentials unreadable).

Third-party keys (Anthropic, ElevenLabs, Salesforce, Slack) cannot be
generated locally; get them from each provider as described in the README.

Usage (from the repo root): python scripts/gen_secrets.py
"""

import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
GENERATED_KEYS = ("N8N_ENCRYPTION_KEY", "INTERNAL_TOKEN", "ADMIN_TOKEN")


def is_placeholder(value):
    return value == "" or value.startswith("replace-with")


def main():
    if not ENV.exists():
        shutil.copyfile(EXAMPLE, ENV)
        print("Created .env from .env.example")

    with open(ENV, encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(keepends=True)
    seen = set()
    for i, line in enumerate(lines):
        key, sep, value = line.rstrip("\r\n").partition("=")
        key = key.strip()
        if not sep or key not in GENERATED_KEYS:
            continue
        seen.add(key)
        if is_placeholder(value.strip()):
            ending = line[len(line.rstrip("\r\n")):]
            lines[i] = "{}={}{}".format(key, secrets.token_hex(32), ending or "\n")
            print("Generated {}".format(key))
        else:
            print("Kept existing {}".format(key))

    missing = [k for k in GENERATED_KEYS if k not in seen]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for key in missing:
        lines.append("{}={}\n".format(key, secrets.token_hex(32)))
        print("Added {}".format(key))

    with open(ENV, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))
    print("Done. Copy INTERNAL_TOKEN from .env into the n8n credential "
          "'Hotline Ingress Token (header auth)'.")


if __name__ == "__main__":
    sys.exit(main())
