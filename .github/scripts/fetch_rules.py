"""Fetch the pipeline rules from the Hub once per workflow run.

Reads HUB_RULES_URL and RULES_OUT (a file path), downloads the rules once,
validates them with the same checks every other script applies, and writes the
fetched JSON to RULES_OUT. The workflow step then exports
PIPELINE_RULES_FILE=$RULES_OUT so every later script reads the file instead of
calling the Hub again. Standard library only, Python 3.8 compatible.
"""
import json
import os
import sys

from pipeline_rules import Rules, RulesError, fetch_rules_json


def main() -> int:
    url = os.environ.get("HUB_RULES_URL", "")
    out = os.environ.get("RULES_OUT", "")
    try:
        if not url:
            raise RulesError("HUB_RULES_URL is not set")
        if not out:
            raise RulesError("RULES_OUT is not set")
        data = fetch_rules_json(url)
        Rules.from_dict(data)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except (RulesError, OSError) as e:
        print("::error::%s" % e)
        return 1
    print("Pipeline rules fetched from %s" % url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
