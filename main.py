"""The same CLI entrypoint as python -m spotquant."""
from spotquant.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
