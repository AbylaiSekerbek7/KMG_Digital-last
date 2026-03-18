from __future__ import annotations

import re
import subprocess
import sys


URL_PATTERN = re.compile(r"https://[a-z0-9.-]+\.trycloudflare\.com", re.IGNORECASE)


def main() -> int:
    result = subprocess.run(
        ["docker", "compose", "logs", "cloudflared", "--no-color"],
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + "\n" + result.stderr
    matches = URL_PATTERN.findall(output)
    if not matches:
        print("Quick tunnel URL not found. Is the share profile running?", file=sys.stderr)
        return 1
    print(matches[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
