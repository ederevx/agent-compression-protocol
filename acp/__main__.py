"""`python -m acp` runs the standalone server; see acp/serve.py."""
from .serve import main

if __name__ == "__main__":
    raise SystemExit(main())
