#!/usr/bin/env python3
"""Generate monitor_app/panda/error_labels.py from the pilot error catalog.

Reads the pilot source's errorcodes.py (pilot/common/errorcodes.py in the
PanDA pilot repository) and emits the pilot code -> message table used to
label error categories. Rerun when the pilot catalog advances:

    python3 scripts/gen-pilot-error-labels.py /path/to/pilot/common/errorcodes.py
"""

import re
import sys
from pathlib import Path

HEADER = '''"""PanDA error-code labels by error component.

PILOT_LABELS is generated from the pilot error catalog by
scripts/gen-pilot-error-labels.py; edit that generator, not this table.
category_label() is the shared renderer for component:code categories.
"""

'''

FOOTER = '''

def category_label(component, code):
    """Human label for an error category, e.g. 'pilot 1099 - Failed to stage-in file'."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return f"{component} {code}"
    message = None
    if component == "pilot":
        message = PILOT_LABELS.get(code)
    base = f"{component} {code}"
    return f"{base} - {message}" if message else base
'''


def main():
    src = Path(sys.argv[1]).read_text()
    consts = dict(re.findall(r"^\s{4}([A-Z][A-Z0-9_]+)\s*=\s*(\d+)\s*$", src, re.M))
    body = re.search(r"_error_messages\s*=\s*\{(.*?)\n\s{4}\}", src, re.S).group(1)
    pairs = re.findall(r"([A-Z][A-Z0-9_]+)\s*:\s*\"(.*?)\"", body)
    table = {int(consts[name]): msg for name, msg in pairs if name in consts}
    out = Path(__file__).resolve().parent.parent / "src/monitor_app/panda/error_labels.py"
    lines = [HEADER, "PILOT_LABELS = {\n"]
    for code in sorted(table):
        msg = table[code].replace('"', '\\"')
        lines.append(f'    {code}: "{msg}",\n')
    lines.append("}\n")
    lines.append(FOOTER)
    out.write_text("".join(lines))
    print(f"wrote {out} with {len(table)} pilot labels")


if __name__ == "__main__":
    main()
