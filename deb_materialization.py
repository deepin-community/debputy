#!/usr/bin/python3 -B
import pathlib
import sys

DEBPUTY_ROOT_DIR = pathlib.Path(__file__).parent  # TODO: Subst during install

if __name__ == '__main__':
    # setup PYTHONPATH: add our installation directory.
    sys.path.insert(0, str(DEBPUTY_ROOT_DIR))
    from debputy.commands.deb_materialization import main
    main()
