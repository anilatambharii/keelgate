"""Keelgate quickstart: ALLOW, DENY and REQUIRE_APPROVAL, then a verified audit chain.

    python examples/quickstart.py            # or, after `pip install keelgate`: keelgate quickstart
    python examples/quickstart.py --tamper   # also show tampering being caught

The code lives inside the package, so it runs from an installed wheel too. Read it in
src/keelgate/_quickstart.py: it registers a read tool and a paper-trading tool, issues a signed
grant, and runs proposals through the gateway.
"""

import sys

from keelgate.cli import main

if __name__ == "__main__":
    sys.exit(main(["quickstart", *sys.argv[1:]]))
