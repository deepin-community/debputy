debputy
=======

The debputy tool is a Debian package builder that works with a declarative
manifest format.

The early versions will integrate into the debhelper sequencer dh and will replace
several of debhelper's tools that are covered by debputy.  However, the goal is that
debputy will be a standalone tool capable of packaging work from start to end.

The debputy package builder aims to reduce cognitive load for the packager
and provide better introspection to packagers, linters and the Debian janitor.

For documentation, please see:
 * [GETTING-STARTED-WITH-dh-debputy.md](GETTING-STARTED-WITH-dh-debputy.md) for getting started (how-to guide).
 * [MANIFEST-FORMAT.md](MANIFEST-FORMAT.md) for details of the format (reference documentation).
 * [MIGRATING-A-DH-PLUGIN.md](MIGRATING-A-DH-PLUGIN.md) for details on how to migrate a `dh` add-on
   (how-to guide)


Prerequisites
-------------

On a (modern) Debian system, you can install prerequisites via the following apt command.

     apt build-deps -y .

This includes the minimal development stack used to build the `debputy` package.  Have
a look at `debian/control` if you want the minimal dependencies for running `debputy`
directly.


Running tests
-------------

From the top level directory, run `py.test`.


Running debputy from source
---------------------------

While developing, it is useful to run `debputy` directly from the source tree without having
to build first.  This can be done by invoking the `debputy.sh` command in the source root on
any system that has `#!/bin/sh`.

Otherwise, at the time of writing, you can use the following command:

    PYTHONPATH=src python3 -m debputy.commands.debputy_cmd

Which only relies on the `PYTHONPATH` environment variable being set correctly and having `python3`
in `PATH`.

You can also run the `dh_debputy` command in a similar fashion. It will require you to set the
`DEBPUTY_CMD` to point to full path of `debputy.sh` (or a self-written script of similar
functionality), or have wrapper called `debputy` in `PATH`.


The naming
----------

The name debputy is a play on the word "deputy" (using the "assistant" or
"authorized to act as substitute for another" definition) and the "deb"
from deb packages.  The idea is that you tell debputy what you want
(via the declarative manifest) and debputy handles the details of how to
do it.


# Communication channels:

The following communication channels are available:

 * Using the https://salsa.debian.org/debian/debputy features, such as issues. Generally, these
   will be available to the public. While confidential issues can be filed, please note that
   all Debian Developers can read it.
 * Filing bugs via `reportbug debputy` to Debian's Bug Tracking System. Note all such bugs will have
   public archives.
 * Mail to Debputy Maintainers <debputy@packages.debian.org>. Please note that no public archive
   is available but anyone can subscribe via tracker.debian.org.
 * IRC chat on #debputy-devel on the irc.oftc.net server. No public archive will be kept but any
   individual  in the channel will likely keep a history of the discussions.


## Security issues

For issues that should be embargoed, please report the issue to the Debian Security Team, which
has the relevant skill set, policies, and infrastructure to handle this. While the
salsa.debian.org services provides support for **confidential** issues, all Debian Developers
can read those for the `debputy` project. This makes it unsuitable for security bugs under embargo.

Please review https://www.debian.org/security/faq#contact for how to contact the Debian Security
Team.
