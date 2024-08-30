# The debputy manifest format

_This is [reference documentation] and is primarily useful if you have an idea of what you are looking for._
_If you are new to `debputy`, maybe you want to read [GETTING-STARTED-WITH-dh-debputy.md](GETTING-STARTED-WITH-dh-debputy.md) first._

<!-- To writers and reviewers: Check the documentation against https://documentation.divio.com/ -->


## Prerequisites

This guide assumes familiarity with Debian packaging in general.  Notably, you should understand
the different between a (Debian) source package and a (Debian) binary package (e.g., `.deb`) plus
how these concepts relates to `debian/control` (the source control file).

Additionally, the reader is expected to have an understanding of globs and substitution variables.

It is probably very useful to have an understanding on how a binary package is
assembled.  While `debputy` handles the assembly for you, this document will not go in details
with this. Prior experience with `debhelper` (notably the `dh`-style `debian/rules`) may be
useful but should not be a strict requirement.


# The basic manifest

The manifest is a YAML document with a dictionary at the top level layer.  As usual with YAML
versions, you can choose to leave it implicit.  All manifests must include a manifest version, which
will enable the format to change over time.  For now, there is only one version (`0.1`) and you have
to include the line:

    manifest-version: "0.1"


On its own, the manifest containing only `manifest-version: "..."` will not do anything.  So if you
end up only having the `manifest-version` key in the manifest, you can just remove the manifest and
rely entirely on the built-in rules.


# Path matching rules

Most of the manifest is about declaring rules for a given path such as "foo must be a symlink"
or "bar must be owned by root:tty and have mode 02755".

The manifest contains the following types of matches:

 1) Exact path matches.  These specify the path inside the Debian package exactly without any
    form of wildcards (e.g., `*` or `?`).  However, they can use substitution variables.
    Examples include:
    * `usr/bin/sudo`
    * `usr/lib/{{DEB_HOST_MULTIARCH}}/libfoo.so`

    Having a leading `/` is optional.  I.e. `/usr/bin/sudo` and `usr/bin/sudo` are considered the
    same path.

 2) Glob based path matches.  These specify a rule that match any path that matches a given
    glob.  These rules must contain a glob character (e.g., `*`) _and_ a `/`. Examples include:

    * `foo/*`
    * `foo/*.txt`
    * `usr/lib/{{DEB_HOST_MULTIARCH}}/lib*.so*`

    Note that if the glob does not contain a `/`, then it falls into the Basename glob rule
    below.

 3) Basename glob matches.  These specify a rule that match any path where the basename matches
    a given glob.  They must contain a glob character (e.g., `*`) but either must not have a
    `/` in them at all or start with `**/` and not have any `/` from there.
    Examples include:

    * `"*.la"`
    * `"**/*.md"`
    * `"**/LICENSE"`

    The examples use explicit quoting because YAML often interprets `*` as an anchor rule in the
    places where you are likely to use this kind of glob.  The use of the `**/`-prefix is
    optional when the basename is a glob.  If you wanted to match all paths with the basename
    of exactly `LICENSE`, then you have to use the prefix (that is, use `**/LICENSE`) as `LICENSE`
    would be interpreted as an exact match for a top-level path.

However, there are also cases where these matching rules can cause conflicts.  This is covered
in the [Conflict resolution](#conflict-resolution) section below.


## Limitations on debputy path rules

Path rules:

 1) Must match relatively to the package root.
 2) Never resolves a symlink.
    * If `bin/ls` is a symlink, then `bin/ls` matches the symlink (and never the target)
    * If `bin` is a symlink, then `bin/ls` (or `bin/*`) would fail to match, because
      matching would require resolving the symlink.

These limitations are in place because of implementation details in `debputy`.


## Conflict resolution

The manifest can contain seemly mutually exclusive rules.  As an example, if you ask for
`foo/symlink` to be a symlink but also state that you want to remove `foo` entirely
from the Debian package then the manifest now has two mutually exclusive requests.

To resolve these problems, `debputy` relies on the following rules for conflict resolutions:

  1. Requests are loosely-speaking ranked and applied from top to bottom:
     1. `installations` (ordered; "first match wins")
     2. `transformations` (ordered; "all matching rules applies")

     The "first match wins" rule is exactly what it says.

     The "all matching rules applies" rule means that each rule is applied in order.  Often
     this behaves like a simple case of either "first match wins" or "last match wins" (depending
     on the context).

     Note for transformation rules, an early rule can change the file system layout, which will
     affect whether a later rule matches.  This is similar to how shell globs commands work:

          $ rm usr/lib/libfoo.la
          $ chmod 0644 usr/lib/*

     Here the glob used with `chmod` will not match `usr/lib/libfoo.la` because it was removed.
     As noted, a similar logic applies to transformation rules.

  2. The ordered rules (such as every transformation inside `transformations`) are applied in the
     listed order (top to bottom). Due to the explicit ordering, such rules generally do not trigger
     other conflict resolution rules.

     Note keep in mind that all rules must in general apply to at least one path.

Note that `debputy` will in some cases enforce that rules are not redundant.  This feature is currently
only fully implemented for `installations`.

## All definitions must be used

Whenever you define a request or rule in the manifest, `debputy` will insist on it being used
at least once.  The exception to this rule being conditional rules where the condition
evaluates to `false` (in which case the rule never used and does not trigger an error).

This is useful for several reasons:

 1. The definition may have been shadowed by another rule and would have been ignored otherwise
 2. The definition may no longer be useful, but its present might confuse a future reader of
    the manifest.

In all cases, `debputy` will tell you if a definition was unused and where you can find that
definition.


## debputy globs

In general, the following rules applies to globs in `debputy`.

 * The `*` match 0 or more times any characters except `/`.
 * The `?` match exactly one character except `/`.
 * The glob `foo/*` matches _everything_ inside `foo/` including hidden files (i.e., paths starting
   with `.`) unlike `bash`/`sh` globs. However, `foo/*` does _not_ match `foo/` itself (this latter
   part matches `bash`/`sh` behaviour and should be unsurprising).
 * For the special-cases where `**` is supported, then `**` matches zero or more levels of directories.
   This means that `foo/**/*` match any path beneath `foo` (but still not `foo`).  This is mostly relevant
   for built-in path matches as it is currently not possible to define `foo/**/...` patterns in the manifest.

Note that individual rules (such as `clean-after-removal`) may impose special cases to how globs
work. The rules will explicitly list if they divert from the above listed glob rules.

# Rules for substituting manifest variables

The `debputy` tool supports substitution in various places (usually paths) via the following
rules.  That means:

 1) All substitutions must start with `{{` and end with `}}`.  The part between is
    the `MANIFEST_VARIABLE` and must match the regular expression `[A-Za-z0-9][-_:0-9A-Za-z]*`.
    Note that you can use space around the variable name if you feel that increases readability.
    (That is, `{{ FOO }}` can be used as an alternative to `{{FOO}}`).
 2) The `MANIFEST_VARIABLE` will be result from a set of built-in variables and the variables from
    `dpkg-architecture`.
 3) You can use `{{token:DOUBLE_OPEN_CURLY_BRACE}}` and `{{token:DOUBLE_CLOSE_CURLY_BRACE}}` (built-in
    variables)  if you want a literal `{{` or `}}`  would otherwise have triggered an undesired expansion.
 4) All `{{MANIFEST_VARIABLE}}` must refer to a defined variable.
    - You can see the full list of `debputy` and plugin provided manifest variables via:
      `debputy plugin list manifest-variables`. The manifest itself can declare its own variables
      beyond that list. Please refer to the [Manifest Variables](#manifest-variables-variables)
      section manifest variables declared inside the manifest.
 5) There are no expression syntax inside the `{{ ... }}` (unlike jinja2 and other template languages).
    This rule may be changed in the future (with a new manifest version).

Keep in mind that substitution _cannot_ be used everywhere.  There are specific places where
it can be used.  Also, substitution _cannot be used_ to introduce globs into paths.  When a
substitution occurs inside a path all characters inserted are treated as literal characters.

Note: While manifest variables can be substituted into various places in the `debputy` manifest, they
are distinct from `dpkg`'s "substvars" (`man 5 deb-substvars`) that are used in the `debian/control`
file.

## Built-in or common substitution variables

 * `{{token:NEWLINE}}` or `{{token:NL}}` expands to a literal newline (LF) `\n`.
 * `{{token:TAB}}` expands to a literal tab `\t`.
 * `{{token:OPEN_CURLY_BRACE}}`  / `{{token:CLOSE_CURLY_BRACE}}` expand to `{` / `}`
 * `{{token:DOUBLE_OPEN_CURLY_BRACE}}` / `{{token:DOUBLE_CLOSE_CURLY_BRACE}}` expands to `{{` / `}}`.
 * `{{PACKAGE}}` expands to the binary package name of the current binary package. This substitution
   only works/applies when the substitution occurs in the context of a concrete binary package.
 * Plus any of the variables produced by `dpkg-architecture`, such as `{{DEB_HOST_MULTIARCH}}`.

The following variables from `/usr/share/dpkg/pkg-info.mk` (`dpkg`) are also available:

 * DEB_SOURCE (as `{{DEB_SOURCE}}`)
 * DEB_VERSION (as `{{DEB_VERSION}}`)
 * DEB_VERSION_EPOCH_UPSTREAM (as `{{DEB_VERSION_EPOCH_UPSTREAM}}`)
 * DEB_VERSION_UPSTREAM_REVISION (as `{{DEB_VERSION_UPSTREAM_REVISION}}`)
 * DEB_VERSION_UPSTREAM (as `{{DEB_VERSION_UPSTREAM}}`)
 * SOURCE_DATE_EPOCH (as `{{SOURCE_DATE_EPOCH}}`)

These have the same definition as those from the `dpkg` provided makefile.


# Restrictions on defining ownership of paths

In some parts of the manifest, you can specify which user or group should have ownership of
a given path.  As an example, you can define a directory to be owned by `root` with group `tty`
(think `chown root:tty <some/path>`).

Ownership is generally defined via the keys `owner` and `group`.  For each of them, you can use
one of the following formats:

 1) A name (e.g., `owner: root`).
 2) An id (e.g., `owner: 0`). Please avoid using quotes around the ID in YAML as that can
    cause `debputy` to read the number as a name.
 3) A name and an id with a colon in between (e.g., `owner: "root:0"`).  The name must always
    come first here.  You may have to quote the value to prevent the YAML parser from being
    confused.

All three forms are valid and provide the same result.  Unless you have a compelling reason to
pick a particular form, the name-only is recommended for simplicity.  Notably, it does not
require your co-maintainer or future you to remember what the IDs mean.

Regardless of which form you pick:

 1) The provided owner must be defined by Debian `base-passwd` file, which are the only users guaranteed
    to be present on every Debian system.
    * Concretely, `debputy` verifies the input against `/usr/share/base-passwd/passwd.master` and
      `/usr/share/base-passwd/group.master` (except for `root` / `0` as an optimization).

 2) If the `name:id` form is used, then the name and the id values must match.  I.e., `root:2` would
    be invalid as the id for `root` is defined to be `0` in the `base-passwd` data files.

 3) The `debputy` tool maintains a `deny`-list of owners that it refuses even though `base-passwd`
    defines them. As a notable non-exhaustive example, `debputy` considers `nobody` or id `65534`
    (the ID of `nobody` / `nogroup`) to be invalid owners.


# Conditional rules

There are cases, where a given rule should only apply in certain cases - such as only when a given
build profile is active (`DEB_BUILD_PROFILES` / `dpkg-buildpackage -P`).  For rules that
*support being conditional*, the condition is generally defined via the `when:` key and the condition
is then described beneath the `when:`.

As an example:

    packages:
        util-linux:
            transformations:
            - create-symlink
                  path: sbin/agetty
                  target: /sbin/getty
                  when:
                      # On Hurd, the package "hurd" ships "sbin/getty".
                      arch-matches: '!hurd-any'


When the condition under `when:` resolves to `true`, the rule will and must be used.  When the
condition resolves to `false`, the rule will not be applied even if it could have been.  However,
the rule may still be "partially" evaluated.  As an example, for installation rules, the source
patterns will still be evaluated to reserve what it would have matched, so that following rules
behave deterministically regardless of how the condition evaluates.

Note that conditions are *not* used as a conflict resolution and as such two conditional rules
can still cause conflicts even though their conditions are mutually exclusive.  This may be
changed in a later version of `debputy` provided `debputy` can assert the two conditions
are mutually exclusive.

The `when:` key has either a mapping, a list or a string as value depending on the condition.
Each supported condition is described in the following subsections.

## Architecture match condition `arch-matches` (mapping)

Sometimes, a rule needs to be conditional on the architecture.  This can be done by using the
`arch-matches` rule. In 99.99% of the cases, `arch-matches` will be form you are looking for
and practically behaves like a comparison against `dpkg-architecture -qDEB_HOST_ARCH`.

As an example:

    packages:
        util-linux:
            transformations:
            - create-symlink
                  path: sbin/agetty
                  target: /sbin/getty
                  when:
                      # On Hurd, the package "hurd" ships "sbin/getty".
                      arch-matches: '!hurd-any'

The `arch-matches` must be defined as a part of a mapping, where `arch-matches` is the key. The
value must be a string in the form of a space separated list architecture names or architecture
wildcards (same syntax as the  architecture restriction in Build-Depends in debian/control except
there is no enclosing `[]` brackets). The names/wildcards can optionally be prefixed by `!` to
negate them.  However, either *all* names / wildcards must have negation or *none* of them may
have it.

For the cross-compiling specialists or curious people: The `arch-matches` rule behaves like a
`package-context-arch-matches` in the context of a binary package and like
`source-context-arch-matches` otherwise. The details of those are covered in their own section.

## Explicit source or binary package context architecture match condition `source-context-arch-matches`, `package-context-arch-matches` (mapping)

**These are special-case conditions**. Unless you know that you have a very special-case,
you should probably use `arch-matches` instead. These conditions are aimed at people with
corner-case special architecture needs. It also assumes the reader is familiar with the
`arch-matches` condition.

To understand these rules, here is a quick primer on `debputy`'s concept of "source context"
vs "(binary) package context" architecture.  For a native build, these two contexts are the
same except that in the package context an `Architecture: all` package always resolve to
`all` rather than `DEB_HOST_ARCH`. As a consequence, `debputy` forbids `arch-matches` and
`package-context-arch-matches` in the context of an `Architecture: all` package as a warning
to the packager that condition does not make sense.

In the very rare case that you need an architecture condition for an `Architecture: all` package,
you can use `source-context-arch-matches`. However, this means your `Architecture: all` package
is not reproducible between different build hosts (which has known to be relevant for some
very special cases).

Additionally, for the 0.0001% case you are building a cross-compiling compiler (that is,
`DEB_HOST_ARCH != DEB_TARGET_ARCH` and you are working with `gcc` or similar) `debputy` can be
instructed (opt-in) to use `DEB_TARGET_ARCH` rather than `DEB_HOST_ARCH` for certain packages when
evaluating an architecture condition in context of a binary package. This can be useful if the
compiler produces supporting libraries that need to be built for the `DEB_TARGET_ARCH` rather than
the `DEB_HOST_ARCH`.  This is where `arch-matches` or `package-context-arch-matches` can differ
subtly from `source-context-arch-matches` in how they evaluate the condition.  This opt-in currently
relies on setting `X-DH-Build-For-Type: target` for each of the relevant packages in
`debian/control`.  However, unless you are a cross-compiling specialist, you will probably never
need to care about nor use any of this.

Accordingly, the possible conditions are:

 * `arch-matches`: This is the form recommended to laymen and as the default use-case. This
   conditional acts `package-context-arch-matches` if the condition is used in the context
   of a binary package. Otherwise, it acts as `source-context-arch-matches`.

 * `source-context-arch-matches`: With this conditional, the provided architecture constraint is compared
   against the build time provided host architecture (`dpkg-architecture -qDEB_HOST_ARCH`). This can
   be useful when an `Architecture: all` package needs an architecture condition for some reason.

 * `package-context-arch-matches`: With this conditional, the provided architecture constraint is compared
   against the package's resolved architecture. This condition can only be used in the context of a binary
   package (usually, under `packages.<name>.`).  If the package is an `Architecture: all` package, the
   condition will fail with an error as the condition always have the same outcome. For all other
   packages, the package's resolved architecture is the same as the build time provided host architecture
   (`dpkg-architecture -qDEB_HOST_ARCH`).

   - However, as noted above there is a special case for when compiling a cross-compiling compiler, where
     this behaves subtly different from `source-context-arch-matches`.

All conditions are used the same way as `arch-matches`. Simply replace `arch-matches` with the other
condition. See the `arch-matches` description for an example.

## Active build profile match condition `build-profiles-matches` (mapping)

The `build-profiles-matches` condition is used to assert whether the active build profiles
(`DEB_BUILD_PROFILES` / `dpkg-buildpackage -P`) matches a given build profile restriction.

As an example:

    # TODO: Not the best example (`create-symlink` is an unlikely use-case for this condition)
    packages:
        foo:
            transformations:
            - create-symlink
                  path: sbin/agetty
                  target: /sbin/getty
                  when:
                      build-profiles-matches: '<!pkg.foo.mycustomprofile>'

The `build-profiles-matches` must be defined as a part of a mapping, where `build-profiles-matches`
is the key.  The value is a string using the same syntax as the `Build-Profiles` field from `debian/control`
(i.e., a space separated list of `<[!]profile ...>` groups).

## Can run produced binaries `can-execute-compiled-binaries` (string)

The `can-execute-compiled-binaries` condition is used to assert the build can assume
that all compiled binaries can be run as-if they were native binaries. For native
builds, this condition always evaluates to `true`.  For cross builds, the condition
is generally evaluates to `false`.  However, there are special-cases where binaries
can be run during cross-building. Accordingly, this condition is subtly different
from the `cross-compiling` condition.

Note this condition should *not* be used when you know the binary has been built
for the build architecture (`DEB_BUILD_ARCH`) or for determining whether build-time tests
should be run (for build-time tests, please use the `run-build-time-tests` condition instead).
Some upstream build systems are advanced enough to distinguish building a final product vs.
building a helper tool that needs to run during build.  The latter will often be compiled by
a separate compiler (often using `$(CC_FOR_BUILD)`, `cc_for_build` or similar variable names
in upstream build systems for that compiler).

As an example:

    # TODO: Not the best example (`create-symlink` is an unlikely use-case for this condition)
    packages:
        foo:
            transformations:
            - create-symlink
                  path: sbin/agetty
                  target: /sbin/getty
                  # Only for native builds or when we can transparently run a compiled
                  when: can-execute-compiled-binaries

The `can-execute-compiled-binaries` condition is specified as a string.

## Cross-Compiling condition `cross-compiling` (string)

The `cross-compiling` condition is used to determine if the current build is performing a cross
build (i.e., `DEB_BUILD_GNU_TYPE` != `DEB_HOST_GNU_TYPE`). Often this has consequences for what
is possible to do.

Note if you specifically want to know:

 * whether build-time tests should be run, then please use the `run-build-time-tests` condition.
 * whether compiled binaries can be run as if it was a native binary, please use the
   `can-execute-compiled-binaries` condition instead.  That condition accounts for cross-building
   in its evaluation.

As an example:

    # TODO: Not the best example (`create-symlink` is an unlikely use-case for this condition)
    packages:
        foo:
            transformations:
            - create-symlink
                  path: sbin/agetty
                  target: /sbin/getty
                  when: cross-compiling

The `cross-compiling` condition is specified as a string.

## Whether build time tests should be run `run-build-time-tests` (string)

The `run-build-time-tests` condition is used to determine whether (build time) tests should
be run for this build.  This condition roughly translates into whether `nocheck` is present
in `DEB_BUILD_OPTIONS`.

In general, the manifest *should not* prevent build time tests from being run during cross-builds.

As an example:

    # TODO: Not the best example (`create-symlink` is an unlikely use-case for this condition)
    packages:
        foo:
            transformations:
            - create-symlink
                  path: sbin/agetty
                  target: /sbin/getty
                  when: run-build-time-tests

The `run-build-time-tests` condition is specified as a string.

## Negated condition `not` (mapping)

It is possible to negate a condition via the `not` condition.

As an example:

    packages:
        util-linux:
            transformations:
            - create-symlink
                  path: sbin/getty
                  target: /sbin/agetty
                  when:
                      # On Hurd, the package "hurd" ships "sbin/getty".
                      # This example happens to also be an alternative to `arch-marches: '!hurd-any`
                      not:
                          arch-matches: 'hurd-any'

The `not` condition is specified as a mapping, where the key is `not` and the
value is a nested condition.

## All or any of a list of conditions `all-of`/`any-of` (list)

It is possible to aggregate conditions using the `all-of` or `any-of` condition. This provide
`X and Y` and `X or Y` semantics (respectively).

As an example:

    packages:
        util-linux:
            transformations:
                - create-symlink
                  path: sbin/getty
                  target: /sbin/agetty
                  when:
                      # Only ship getty on linux except for s390(x)
                      all-of:
                          - arch-matches: 'linux-any'
                          - arch-matches: '!s390 !s390x'

The `all-of` and `any-of` conditions are specified as lists, where each entry is a nested condition.
The lists need at least 2 entries as with fewer entries the `all-of` and `any-of` conditions are
redundant.

# Packager provided definitions

For more complex manifests or packages, it is possible define some common attributes for reuse.

## Manifest Variables (`variables`)

It is possible to provide custom manifest variables via the `variables` attribute.  An example:

    manifest-version: '0.1'
    definitions:
      variables:
        LIBPATH: "/usr/lib/{{DEB_HOST_MULTIARCH}}"
        SONAME: "1"
    installations:
      - install:
           source: build/libfoo.so.{{SONAME}}*
           # The quotes here is for the YAML parser's sake.
           dest-dir: "{{LIBPATH}}"
           into: libfoo{{SONAME}}

The value of the `variables` key must be a mapping, where each key is a new variable name and
the related value is the value of said key. The keys must be valid variable name and not shadow
existing variables (that is, variables such as `PACKAGE` and `DEB_HOST_MULTIARCH` *cannot* be
redefined). The value for each variable *can* refer to *existing* variables as seen in the
example above.

As usual, `debputy` will insist that all declared variables must be used.

Limitations:
 * When declaring variables that depends on another variable declared in the manifest, the
   order is important. The variables are resolved from top to bottom.
 * When a manifest variable depends on another manifest variable, the existing variable is
   currently always resolved in source context. As a consequence, some variables such as
  `{{PACKAGE}}` cannot be used when defining a variable. This restriction may be
   lifted in the future.

# Build environment (`build-environment`)

Define the environment variables used in all build commands.

The environment definition can be used to tweak the environment variables used by the
build commands. An example:

    environment:
      set:
        ENV_VAR: foo
        ANOTHER_ENV_VAR: bar

The environment definition has multiple attributes for setting environment variables
which determines when the definition is applied. The resulting environment is the
result of the following order of operations.

  1. The environment `debputy` received from its parent process.
  2. Apply all the variable definitions from `set` (if the attribute is present)
  3. Apply all computed variables (such as variables from `dpkg-buildflags`).
  4. Apply all the variable definitions from `override` (if the attribute is present)
  5. Remove all variables listed in `unset` (if the attribute is present).

Accordingly, both `override` and `unset` will overrule any computed variables while
`set` will be overruled by any computed variables.

Note that these variables are not available via manifest substitution (they are only
visible to build commands). They are only available to build commands.

The `build-environment` attribute is a mapping and has the following attributes:

 - `set` (optional): Mapping of string

   A mapping of environment variables to be set.

   Note these environment variables are set before computed variables (such
   as `dpkg-buildflags`) are provided. They can affect the content of the
   computed variables, but they cannot overrule them. If you need to overrule
   a computed variable, please use `override` instead.

 - `override` (optional): Mapping of string

   A mapping of environment variables to set.

   Similar to `set`, but it can overrule computed variables like those from
   `dpkg-buildflags`.

 - `unset` (optional): List of string

   A list of environment variables to unset.

   Any environment variable named here will be unset. No warnings or errors
   will be raised if a given variable was not set.


# Installations

For source packages building a single binary, the `dh_auto_install` from debhelper will default to
providing everything from upstream's install in the binary package.  The `debputy` tool matches this
behaviour and accordingly, the `installations` feature is only relevant in this case when you need to
manually specify something upstream's install did not cover.

For sources, that build multiple binaries, where `dh_auto_install` does not detect anything to install,
or when `dh_auto_install --destdir debian/tmp` is used, the `installations` section of the manifest is
used to declare what goes into which binary package. An example:

    installations:
      - install:
            sources: "usr/bin/foo"
            into: foo
      - install:
            sources: "usr/*"
            into: foo-extra


All installation rules are processed in order (top to bottom).  Once a path has been matched, it can
no longer be matched by future rules.  In the above example, then `usr/bin/foo` would be in the `foo`
package while everything in `usr` *except* `usr/bin/foo` would be in `foo-extra`.  If these had been
ordered in reverse, the `usr/bin/foo` rule would not have matched anything and caused `debputy`
to reject the input as an error on that basis.  This behaviour is similar to "DEP-5" copyright files,
except the order is reversed ("DEP-5" uses "last match wins", where here we are doing "first match wins")

In the rare case that some path need to be installed into two packages at the same time, then this is
generally done by changing `into` into a list of packages.

All installations are currently run in *source* package context.  This implies that:

  1) No package specific substitutions are available. Notably `{{PACKAGE}}` cannot be resolved.
  2) All conditions are evaluated in source context.  For 99.9% of users, this makes no difference,
     but there is a cross-build feature that changes the "per package" architecture which is affected.

This is a limitation that should be fixed in `debputy`.

**Attention debhelper users**: Note the difference between `dh_install` (etc.) vs. `debputy` on
overlapping matches for installation.

## Install rule search directories

Most install rules apply their patterns against search directories such as `debian/tmp` by default.

The default search directory order (highest priority first) is:

 1) The upstream install directory (usually, `debian/tmp`)
 2) The source package root directory (`.`)

Each search directory is tried in order.  When a pattern matches an entry in a search directory (even
if that entry is reserved by another package), further search directories will *not* be tried. As an example,
consider the pattern `usr/bin/foo*` and the files:

  `SOURCE_ROOT/debian/tmp/usr/bin/foo.sh`
  `SOURCE_ROOT/usr/bin/foo.pl`

Here the pattern will only match `SOURCE_ROOT/debian/tmp/usr/bin/foo.sh` and not `SOURCE_ROOT/usr/bin/foo.pl`.

## Automatic discard rules

The `debputy` framework provides some built-in discard rules that are applied by default during installation
time.  These are always active and implicit, but can be overwritten by exact path matches for install rules.

The `debputy` tool itself provides the following discard rules:

 * Discard of `.la` files. Their use is rare but not unheard of. You may need to overwrite this.
 * Discard of python byte code (such as `__pycache__` directories).
 * Discard of editor backup files (such as `*~`, `*.bak`, etc.).
 * Discard of Version control files (such as `.gitignore`, etc.).
 * Discard of GNU info's `dir` (`usr/share/info/dir`) as it causes file conflicts with other packages.
 * Discard of `DEBIAN` directory.

Note: Third-party plugins may provide additional automatic discard rules. Please use
`debputy plugin list automatic-discard-rules` to see all known automatic discard rules.

If you find yourself needing a particular path installed that has been discarded by default, you can overrule
the default discard by spelling out the path. As an example, if you needed to install a `libfoo.la` file,
you could do:

    installations:
      - install:
            sources:
            # By-pass automatic discard of `libfoo.la` - globs *cannot* be used!
             - "usr/lib/libfoo.la"
             - "usr/lib/libfoo*.so*"
            into: libfoo1

## Generic install (`install`)

The generic `install` rule can be used to install arbitrary paths into packages and is *similar* to how
`dh_install` from debhelper works.  It is a two "primary" uses.

  1) The classic "install into directory" similar to the standard `dh_install`
  2) The "install as" similar to `dh-exec`'s `foo => bar` feature.

Examples:

    installations:
      - install:
            source: "usr/bin/tool-a"
            dest-dir: "usr/bin"
            into: foo
      - install:
            source: "usr/bin/tool-b"
            # Implicit dest-dir: "usr/bin
            into: foo-extra
      - install:
            source: "usr/bin/tool-c.sh"
            # Rename as a part of installing.
            as: "usr/bin/tool-c"
            into: foo-util


The value for `install` is a string, a list of strings or mapping. When it is a mapping, the
mapping has the following key/value pairs:

 * `source` or `sources` (required): A path match (`source`) or a list of path matches (`sources`) defining
   the source path(s) to be installed. The path match(es) can use globs.  Each match is tried against default
   search directories.
    - When a symlink is matched, then the symlink (not its target) is installed as-is.  When a directory is
     matched, then the directory is installed along with all the contents that have not already been installed
     somewhere.

 * `into` (conditional): Either a package name or a list of package names for which these paths should be
   installed. This key is conditional on whether there are multiple binary packages listed in
   `debian/control`.  When there is only one binary package, then that binary is the default for `into`.
   Otherwise, the key is required.

 * `dest-dir` (optional): A path defining the destination *directory*.  The value *cannot* use globs, but can
   use substitution.  If neither `as` nor `dest-dir` is given, then `dest-dir` defaults to the directory name
   of the `source`.

 * `as` (optional): A path defining the path to install the source as. This is a full path.  This option is
   mutually exclusive with `dest-dir` and `sources` (but not `source`).  When `as` is given, then `source` must
   match exactly one "not yet matched" path.

 * `when` (optional): A condition as defined in [Conditional rules](#conditional-rules).

When the input is a string or a list of string, then that value is used as shorthand for `source`
or `sources` (respectively).  This form can only be used when `into` is not required.


## Install documentation (`install-docs`)

This install rule resemble that of `dh_installdocs`.  It is a shorthand over the generic
`install` rule with the following key features:

 1) The default `dest-dir` is to use the package's documentation directory (usually something
    like `/usr/share/doc/{{PACKAGE}}`, though it respects the "main documentation package"
    recommendation from Debian Policy). The `dest-dir` or `as` can be set in case the
    documentation in question goes into another directory or with a concrete path.  In this
    case, it is still "better" than `install` due to the remaining benefits.
 2) The rule comes with pre-defined conditional logic for skipping the rule under
    `DEB_BUILD_OPTIONS=nodoc`, so you do not have to write that conditional yourself.
 3) The `into` parameter can be omitted as long as there is a exactly one non-`udeb` package
    listed in `debian/control`.

With these two things in mind, it behaves just like the `install` rule.

Note: It is often worth considering to use a more specialized version of the `install-docs`
rule when one such is available. If you are looking to install an example or a man page,
consider whether `install-examples` or `install-man` might be a better fit for your
use-case.

Examples:

    installations:
      - install-docs:
            sources:
              - "docs/README.some-topic.md"
              - "docs/README.another-topic.md"
            into: foo


The value for `install-docs` is a string, a list of strings or mapping. When it is
a mapping, the mapping has the following key/value pairs:

 * `source` or `sources` (required): A path match (`source`) or a list of path matches (`sources`) defining
   the source path(s) to be installed. The path match(es) can use globs.  Each match is tried against default
   search directories.
     - When a symlink is matched, then the symlink (not its target) is installed as-is.  When a directory is
       matched, then the directory is installed along with all the contents that have not already been installed
       somewhere.

     - **CAVEAT**: Specifying `source: docs` where `docs` resolves to a directory for `install-docs`
       will give you an `docs/docs` directory in the package, which is rarely what you want. Often, you
       can solve this by using `docs/*` instead.

 * `dest-dir` (optional): A path defining the destination *directory*.  The value *cannot* use globs, but can
   use substitution.  If neither `as` nor `dest-dir` is given, then `dest-dir` defaults to the relevant package
   documentation directory (a la `/usr/share/doc/{{PACKAGE}}`).

 * `into` (conditional): Either a package name or a list of package names for which these paths should be
   installed as docs.  This key is conditional on whether there are multiple (non-`udeb`) binary
   packages  listed in `debian/control`.  When there is only one (non-`udeb`) binary package, then that binary
   is the default for `into`. Otherwise, the key is required.

 * `as` (optional): A path defining the path to install the source as. This is a full path.  This option is
   mutually exclusive with `dest-dir` and `sources` (but not `source`).  When `as` is given, then `source` must
   match exactly one "not yet matched" path.

 * `when` (optional): A condition as defined in [Conditional rules](#conditional-rules).  This condition will
   be combined with the built-in condition provided by these rules (rather than replacing it).

When the input is a string or a list of string, then that value is used as shorthand for `source`
or `sources` (respectively).  This form can only be used when `into` is not required.

Note: While the canonical name for this rule use plural, the `install-doc` variant is accepted as
alternative name.

## Install examples (`install-examples`)

This install rule resemble that of `dh_installexamples`.  It is a shorthand over the generic
`install` rule with the following key features:

 1) It pre-defines the `dest-dir` that respects the "main documentation package" recommendation from
    Debian Policy. The `install-examples` will use the `examples` subdir for the package documentation
    dir.
 2) The rule comes with pre-defined conditional logic for skipping the rule under
    `DEB_BUILD_OPTIONS=nodoc`, so you do not have to write that conditional yourself.
 3) The `into` parameter can be omitted as long as there is a exactly one non-`udeb` package
    listed in `debian/control`.

With these two things in mind, it behaves just like the `install` rule.

Examples:

    installations:
      - install-examples:
            source: "examples/*"
            into: foo


The value for `install-examples` is a string, a list of strings or mapping. When it is
a mapping, the mapping has the following key/value pairs:

* `source` or `sources` (required): A path match (`source`) or a list of path matches (`sources`) defining
  the source path(s) to be installed. The path match(es) can use globs.  Each match is tried against default
  search directories.
    - When a symlink is matched, then the symlink (not its target) is installed as-is.  When a directory is
      matched, then the directory is installed along with all the contents that have not already been installed
      somewhere.

    - **CAVEAT**: Specifying `source: examples` where `examples` resolves to a directory for `install-examples`
      will give you an `examples/examples` directory in the package, which is rarely what you want. Often, you
      can solve this by using `examples/*` instead.

* `into` (conditional): Either a package name or a list of package names for which these paths should be
  installed as examples.  This key is conditional on whether there are multiple (non-`udeb`) binary
  packages  listed in `debian/control`.  When there is only one (non-`udeb`) binary package, then that binary
  is the default for `into`. Otherwise, the key is required.


* `when` (optional): A condition as defined in [Conditional rules](#conditional-rules).  This condition will
  be combined with the built-in condition provided by these rules (rather than replacing it).

When the input is a string or a list of string, then that value is used as shorthand for `source`
or `sources` (respectively).  This form can only be used when `into` is not required.

Note: While the canonical name for this rule use plural, the `install-example` variant is accepted as
alternative name.

## Install man pages (`install-man`)

Install rule for installing man pages similar to `dh_installman`. It is a shorthand over the generic
`install` rule with the following key features:

 1) The rule can only match files (notably, symlinks cannot be matched by this rule).
 2) The `dest-dir` is computed per source file based on the man page's section and language.
 3) The `into` parameter can be omitted as long as there is a exactly one non-`udeb` package
    listed in `debian/control`.
 4) The rule comes with man page specific attributes such as `language` and `section` for when the
    auto-detection is insufficient.
 5) The rule comes with pre-defined conditional logic for skipping the rule under `DEB_BUILD_OPTIONS=nodoc`,
    so you do not have to write that conditional yourself.

With these things in mind, the rule behaves similar to the `install` rule.

Examples:

    installations:
      - install-man:
            source: "man/foo.1"
            into: foo
      - install-man:
            source: "man/foo.de.1"
            language: derive-from-basename
            into: foo


The value for `install-man` is a string, a list of strings or mapping. When it is a mapping, the mapping
has the following key/value pairs:

 * `source` or `sources` (required): A path match (`source`) or a list of path matches (`sources`) defining
   the source path(s) to be installed. The path match(es) can use globs.  Each match is tried against default
   search directories. Only files can be matched.

 * `into` (conditional): Either a package name or a list of package names for which these paths should be
   installed as man pages.  This key is conditional on whether there are multiple (non-`udeb`) binary
   packages  listed in `debian/control`.  When there is only one (non-`udeb`) binary package, then that binary
   is the default for `into`. Otherwise, the key is required.

 * `section` (optional): If provided, it must be an integer between 1 and 9 (both inclusive), defining the
    section the man pages belong overriding any auto-detection that `debputy` would have performed.

 * `language` (optional): If provided, it must be either a 2 letter language code (such as `de`), a 5 letter
   language + dialect code (such as `pt_BR`), or one of the special keywords `C`, `derive-from-path`, or
   `derive-from-basename`.  The default is `derive-from-path`.
   - When `language` is `C`, then the man pages are assumed to be "untranslated".
   - When `language` is a language code (with or without dialect), then all man pages matched will be assumed
     to be translated to that concrete language / dialect.
   - When `language` is `derive-from-path`, then `debputy` attempts to derive the language from the path
     (`man/<language>/man<section>`).  This matches the default of `dh_installman`. When no language can
     be found for a given source, `debputy` behaves like language was `C`.
   - When `language` is `derive-from-basename`, then `debputy` attempts to derive the language from the
     basename (`foo.<language>.1`) similar to `dh_installman` previous default.  When no language can
     be found for a given source, `debputy` behaves like language was `C`.  Note this is prone to
     false positives where `.pl`, `.so` or similar two-letter extensions gets mistaken for a language
     code (`.pl` can both be "Polish" or "Perl Script", `.so` can both be "Somali" and "Shared Object"
     documentation).  In this configuration, such extensions are always assumed to be a language.

 * `when` (optional): A condition as defined in [Conditional rules](#conditional-rules).  This condition will
   be combined with the built-in condition provided by these rules (rather than replacing it).


When the input is a string or a list of string, then that value is used as shorthand for `source`
or `sources` (respectively).  This form can only be used when `into` is not required.

Comparison with debhelper: The `dh_installman` uses `derive-from-path` as default and then falls back
to `derive-from-basename`.  The `debputy` tool does *not* feature the same fallback logic.  If you want
the `derive-from-basename` with all of its false-positives, you have to explicitly request it.


## Discard (or exclude) upstream provided paths (`discard`)

When installing paths from `debian/tmp` into packages, it might be useful to ignore some paths that you never
need installed.  This can be done with the `discard` rule.


The value for `discard` is a string, a list of strings or mapping. When it is a mapping, the mapping
has the following key/value pairs:

 * `path` or `paths` (required): A path match (`path`) or a list of path matches (`paths`) defining the
   source path(s) that should not be installed anywhere. The path match(es) can use globs.
    - When a symlink is matched, then the symlink (not its target) is discarded as-is.  When a directory is
     matched, then the directory is discarded along with all the contents that have not already been installed
     somewhere.

 * `search-dir` or `search-dirs` (optional): A path (`search-dir`) or a list to paths (`search-dirs`) that
    defines which search directories apply to. This attribute is primarily useful for source packages that
    uses [per package search dirs](#custom-installation-time-search-directories-installation-search-dirs),
    and you want to restrict a discard rule to a subset of the relevant search dirs. Note all listed
    search directories must be either an explicit search requested by the packager or a search directory
    that `debputy` provided automatically (such as `debian/tmp`). Listing other paths will make `debputy`
    report an error.

    - Note that the `path` or `paths` must match at least one entry in any of the search directories unless
      *none* of the search directories exist (or the condition in `required-when` evaluates to false). When
      none of the search directories exist, the discard rule is silently skipped. This special-case enables
      you to have discard rules only applicable to certain builds that are only performed conditionally.

 * `required-when` (optional): A condition as defined in [Conditional rules](#conditional-rules). The discard
   rule is always applied. When the conditional is present and evaluates to false, the discard rule can
   silently match nothing. When the condition is absent, *or* it evaluates to true, then each pattern
   provided must match at least one path.

When the input is a string or a list of string, then that value is used as shorthand for `path`
or `paths` (respectively).

Once a path is discarded, it cannot be matched by any other install rules.  A path that is discarded, is
considered handled when `debputy` checks for paths you might have forgotten to install.  The `discard`
feature is therefore *also* replaces the `debian/not-installed` file used by `debhelper` and `cdbs`.

Note: A discard rule applies to *all* relevant search directories at the same time (including the source
root directory) unlike other install rules that only applies to the first search directory with a *match*.
This is to match the intuition that if you discard something, you want it gone no matter which search
directory it happened to be in.

## Multi destination install (`multi-dest-install`)

Special use install rule for installing the same source multiple times into the same package.

It works similar to the `install` rule except:

 1) `dest-dir` is renamed to `dest-dirs` and is conditionally mandatory (either `as` or `dest-dirs`
    must be provided).
 2) Both `as` and `dest-dirs` are now list of paths must have at least two paths when provided.

Please see `debputy plugin show pluggable-manifest-rules multi-dest-install` for the full documentation.

# Binary package rules

Inside the manifest, the `packages` mapping can be used to define requests for the binary packages
you want `debputy` to produce.  Each key inside `packages` must be the name of a binary package
defined in `debian/control`.  The value is a dictionary defining which features that `debputy`
should apply to that binary package.  An example could be:


    packages:
        foo:
            transformations:
                - create-symlink:
                      path: usr/share/foo/my-first-symlink
                      target: /usr/share/bar/symlink-target
                - create-symlink:
                      path: usr/lib/{{DEB_HOST_MULTIARCH}}/my-second-symlink
                      target: /usr/lib/{{DEB_HOST_MULTIARCH}}/baz/symlink-target
        bar:
            transformations:
            - create-directories:
               - some/empty/directory.d
               - another/empty/integration-point.d
            - create-directories:
                 path: a/third-empty/directory.d
                 owner: www-data
                 group: www-data

In this case, `debputy` will create some symlinks inside the `foo` package and some directories for
the `bar` package.  The following subsections define the keys you can use under each binary package.


## Transformations (`transformations`)

You can define a `transformations` under the package definition, which is a list a transformation
rules.  An example:

    packages:
        foo:
            transformations:
              - remove: 'usr/share/doc/{{PACKAGE}}/INSTALL.md'
              - move:
                    source: bar/*
                    target: foo/


Transformations are ordered and are applied in the listed order.  A path can be matched by multiple
transformations; how that plays out depends on which transformations are applied and in which order.
A quick summary:

 - Transformations that modify the file system layout affect how path matches in later transformations.
   As an example, `move` and `remove` transformations affects what globs and path matches expand to in
   later transformation rules.

 - For other transformations generally the latter transformation overrules the earlier one, when they
   overlap or conflict.

### Remove transformation rule (`remove`)

The remove transformation rule is mostly only useful for single binary source packages, where
everything from upstream's build system is installed automatically into the package.  In those case,
you might find yourself with some files that are _not_ relevant for the Debian package (but would be
relevant for other distros or for non-distro local builds).  Common examples include `INSTALL` files
or `LICENSE` files (when they are just a subset of `debian/copyright`).

In the manifest, you can ask `debputy` to remove paths from the Debian package by using the `remove`
transformation rule.  An example being:

    packages:
        foo:
            transformations:
              - remove: 'usr/share/doc/{{PACKAGE}}/INSTALL.md'


The value for `remove` is a string, a list of strings or mapping. When it is a mapping, the mapping
has the following key/value pairs:

 * `path` or `paths` (required): A path match (`path`) or a list of path matches (`paths`) defining the
   path(s) inside the package that should be removed. The path match(es) can use globs.
    - When a symlink is matched, then the symlink (not its target) is removed as-is.  When a directory is
      matched, then the directory is removed along with all the contents.

 * `keep-empty-parent-dirs` (optional): A boolean determining whether to prune parent directories that become
   empty as a consequence of this rule.  When provided and `true`, this rule will leave empty directories
   behind. Otherwise, if this rule causes a directory to become empty that directory will be removed.

 * `when` (optional): A condition as defined in [Conditional rules](#conditional-rules).  This condition will
   be combined with the built-in condition provided by these rules (rather than replacing it).

When the input is a string or a list of string, then that value is used as shorthand for `path`
or `paths` (respectively).

Note that `remove` removes paths from future glob matches and transformation rules.

This rule behaves roughly like the following shell snippet when applied:

```shell
set -e
for p in ${paths}; do
  rm -fr "${p}"
  if [ "${keep_empty_parent_dirs}" != "true" ]; then
    rmdir --ignore-fail-on-non-empty --parents "$(dirname "${p}")"
  fi
done
```

### Move transformation rule (`move`)

The move transformation rule is mostly only useful for single binary source packages, where
everything from upstream's build system is installed automatically into the package.  In those case,
you might find yourself with some files that need to be renamed to match Debian specific requirements.

This can be done with the `move` transformation rule, which is a rough emulation of the `mv` command
line tool.  An example being:

    packages:
        foo:
            transformations:
              - move:
                    source: bar/*
                    target: foo/
              # Note this example leaves bar/ as an empty directory. Real world usage would probably
              # follow this with an remove rule for bar/ to avoid an empty directory

The value for `move` is a mapping with the following key/value pairs:

 * `source` (required): A path match defining the source path(s) to be renamed.  The value can use globs and
   substitutions.

 * `target` (required): A path defining the target path.  The value *cannot* use globs, but can use substitution.
   If the target ends with a literal `/` (prior to substitution), the target will *always* be a directory.

 * `when` (optional): A condition as defined in [Conditional rules](#conditional-rules)

There are two basic cases:
 1. If `source` match exactly one path and `target` is not a directory (including it does not exist),
    then `target` is removed (if it existed) and `source` is renamed to `target`.
    - If the `target` is a directory, and you intentionally want to replace it with a non-directory, please
      add an explicit `remove` transformation for the directory prior to this transformation.

 2. If `source` match more than one path *or* `target` is a directory (or is specified with a trailing slash),
    then all matching sources a moved into `target` retaining their basename.  If `target` already contains
    overlapping basenames with `source`, then the transformation rule with abort with an error if the overlap
    contains a directory. Otherwise, any overlapping paths in `target` will be implicitly removed first.

    - If the replacement of a directory is intentional, please add an explicit `remove` rule for it first.
    - In the case that the `source` glob is exotic enough to match two distinct paths with the same basename,
      then `debputy` will reject the transformation on account of it being ambiguous.

In either case, parent directories of `target` are created as necessary as long as they do not trigger a conflict
(or require traversing symlinks or non-directories).  Additionally, the paths matched by `source` will no longer
match anything (since they are now renamed/relocated), which may affect what path future path matches will apply
to.

Note that like the `mv`-command, while the `source` paths are no longer present, their parent directories
will remain.

### Create symlinks transformation rule (`create-symlink`)

Often, the upstream build system will provide the symlinks for you.  However, in some cases, it is useful for
the packager to define distribution specific symlinks. This can be done via the `create-symlink` transformation
rule.  An example of how to do this is:

    packages:
        foo:
            transformations:
            - create-symlink:
                  path: usr/share/foo/my-first-symlink
                  target: /usr/share/bar/symlink-target
            - create-symlink:
                  path: usr/lib/{{DEB_HOST_MULTIARCH}}/my-second-symlink
                  target: /usr/lib/{{DEB_HOST_MULTIARCH}}/baz/symlink-target

The value for the `create-symlink` key is a mapping, which contains the following keys:

 * `path` (required): The path that should be a symlink.  The path may contain substitution variables
   such as `{{DEB_HOST_MULTIARCH}}` but _cannot_ use globs.  Parent directories are implicitly created
   as necessary.
   * Note that if `path` already exists, the behaviour of this transformation depends on the value of
     `replacement-rule`.

 * `target` (required): Where the symlink should point to. The target may contain substitution variables
   such as `{{DEB_HOST_MULTIARCH}}` but _cannot_ use globs.  The link target is _not_ required to exist inside
   the package.
   * The `debputy` tool will normalize the target according to the rules of the Debian Policy.  Use absolute
     or relative target at your own preference.

 * `replacement-rule` (optional): This attribute defines how to handle if `path` already exists. It can be
   set to one of the following values:
   - `error-if-exists`: When `path` already exists, `debputy` will stop with an error.  This is similar to
     `ln -s` semantics.
   - `error-if-directory`: When `path` already exists, **and** it is a directory, `debputy` will stop with an
     error. Otherwise, remove the `path` first and then create the symlink.  This is similar to `ln -sf`
     semantics.
   - `abort-on-non-empty-directory` (default): When `path` already exists, then it will be removed provided
     it is a non-directory **or** an *empty* directory and the symlink will then be created.  If the path is
     a *non-empty* directory, `debputy` will stop with an error.
   - `discard-existing`: When `path` already exists, it will be removed. If the `path` is a directory, all
     its contents will be removed recursively along with the directory. Finally, the symlink is created.
     This is similar to having an explicit `remove` rule just prior to the `create-symlink` that is conditional
     on `path` existing (plus the condition defined in `when` if any).

   Keep in mind, that `replacement-rule` only applies if `path` exists.  If the symlink cannot be created,
   because a part of `path` exist and is *not* a directory, then `create-symlink` will fail regardless of the
   value in `replacement-rule`.

 * `when` (optional): A condition as defined in [Conditional rules](#conditional-rules)

This rule behaves roughly like the following shell snippet when applied:

```shell
set -e
case "${replacement_rule}" in
  error-if-directory)
    F_FLAG="-f"
  ;;
  abort-on-non-empty-directory)
    if [ -d "${path}" ]; then
      rmdir "${path}"
    fi
    F_FLAG="-f"
  ;;
  discard-existing)
    rm -fr "${path}"
  ;;
esac
install -o "root" -g "root" -m "755" -d "$(dirname "${path}")"
ln -s ${F_FLAG} "${target}" "${path}"
```

### Create directories transformation rule (`create-directories`)

NOTE: This transformation is only really needed if you need to create an empty directory somewhere
in your package as an integration point.  All `debputy` transformations will create directories
as required.

In most cases, upstream build systems and `debputy` will create all the relevant directories.  However, in some
rare cases you may want to explicitly define a path to be a directory.  Maybe to silence a linter that is
warning you about a directory being empty, or maybe you need an empty directory that nothing else is creating
for you. This can be done via the `create-directories` transformation rule. An example being:

    packages:
        bar:
            create-directories:
            - some/empty/directory.d
            - another/empty/integration-point.d
            - path: a/third-empty/directory.d
              owner: www-data
              group: www-data

The value for the `create-directories` key is either a string, a list of string or a mapping. When it is a
mapping, the mapping has the following key/value pairs:

 * `path` or `paths` (required): A path (`path`) or a list of path (`paths`) defining the
   path(s) inside the package that should be created as directories. The path(es) _cannot_ use globs
   but can use substitution variables.  Parent directories are implicitly created (with owner `root:root`
   and mode `0755` - only explicitly listed directories are affected by the owner/mode options)

 * `owner` (optional, default `root`): Denotes the owner of the directory (but _not_ what is inside the directory).

 * `group` (optional, default `root`): Denotes the group of the directory (but _not_ what is inside the directory).

 * `mode` (optional, default `"0755"`): Denotes the mode of the directory (but _not_ what is inside the directory).
   Note that numeric mode must always be given as a string (i.e., with quotes).  Symbolic mode can be used as well.
   If symbolic mode uses a relative definition (e.g., `o-rx`), then it is relative to the directory's current mode
   (if it already exists) or `0755` if the directory is created by this transformation.

 * `when` (optional): A condition as defined in [Conditional rules](#conditional-rules)

When the input is a string or a list of string, then that value is used as shorthand for `path`
or `paths` (respectively).

Unless you have a specific need for the mapping form, you are recommended to use the shorthand form of
just listing the directories you want created.


Note that implicitly created directories (by this or other transformations) always have owner `root:root`
and mode `"0755"`.  If you need a directory tree with special ownership/mode, you will have to list all the
directories in that tree explicitly with the relevant attributes OR use `path-metadata` transformation rule
to change their metadata after creation.


This rule behaves roughly like the following shell snippet when applied:

```shell
set -e
for p in ${paths}; do
  install -o "root" -g "root" -m "755" -d "${p}"
  chown "${owner}:${group}" "${p}"
  chmod "${mode}" "${p}"
done
```

### Change path owner/group or mode (`path-metadata`)

The `debputy` normalizes the path metadata (such as ownership and mode) similar to `dh_fixperms`.
For most packages, the default is what you want.  However, in some cases, the package has a special
case or two that `debputy` does not cover.  In that case, you can tell `debputy` to use the metadata
you want by using the `path-metadata` transformation.  An example being:

    packages:
        foo:
            transformations:
              - path-metadata:
                  path: usr/bin/sudo
                  mode: "0755"
              - path-metadata:
                  path: usr/bin/write:
                  group: tty
             - path-metadata:
                  path: /usr/sbin/spine
                  capabilities: cap_net_raw+ep


The value for the `path-metadata` key is a mapping. The mapping has the following key/value pairs:
Each key defines a path or a glob that may


* `path` or `paths` (required): A path match (`path`) or a list of path matches (`paths`) defining the
  path(s) inside the package that should be affected. The path match(es) can use globs and substitution
  variables. Special-rules for matches:
    - Symlinks are never followed and will never be matched by this rule.
    - Directory handling depends on the `recursive` attribute.

 * `owner` (conditional): Denotes the owner of the paths matched by `path` or `paths`. When omitted, no change of
   owner is done.

 * `group` (conditional): Denotes the group of the paths matched by `path` or `paths`. When omitted, no change of
   group is done.

 * `mode` (conditional): Denotes the mode of the paths matched by `path` or `paths`. When omitted, no change in
   mode is done. Note that numeric mode must always be given as a string (i.e., with quotes).  Symbolic mode can
   be used as well. If symbolic mode uses a relative definition (e.g., `o-rx`), then it is relative to the
   directory's current mode.

 * `capabilities` (conditional): Denotes a Linux capability that should be applied to the path. When provided,
   `debputy` will cause the capability to be applied to all *files* denoted by the `path`/`paths` attribute
   on install (via `postinst configure`) provided that `setcap` is installed on the system when the
   `postinst configure` is run.
   - If any non-file paths are matched, the `capabilities` will *not* be applied to those paths.

 * `capability-mode` (optional): Denotes the mode to apply to the path *if* the Linux capability denoted in
   `capabilities` was successfully applied. If omitted, it defaults to `a-s` as generally capabilities are
   used to avoid "setuid"/"setgid" binaries. The `capability-mode` is relative to the *final* path mode
   (the mode of the path in the produced `.deb`). The `capability-mode` attribute cannot be used if
   `capabilities` is omitted.

 * `recursive` (optional, default `false`): When a directory is matched, then the metadata changes are applied
    to the directory itself. When `recursive` is `true`, then the transformation is *also* applied to all paths
    beneath the directory.

 * `when` (optional): A condition as defined in [Conditional rules](#conditional-rules)

At least one of `owner`, `group`, `mode`, or `capabilities` must be provided.

This rule behaves roughly like the following shell snippet when applied:

```shell
MAYBE_R_FLAG=$(${recursive} && printf "%s" "-R")

for p in ${path}; do
  if [ -n "${owner}" ] || [ -n "${group}" ]; then
    chown $MAYBE_R_FLAG "${owner}:${group}" "${p}"
  fi
  if [ -n "${mode}" ]; then
    chmod $MAYBE_R_FLAG "${mode}" "${p}"
  fi
done
```

(except all symlinks will be ignored)

## Service management (`services`)

If you have non-standard requirements for certain services in the package, you can define those via
the `services` attribute.

    packages:
        foo:
            services:
              - service: "foo"
                enable-on-install: false
              - service: "bar"
                on-upgrade: stop-then-start


The `services` attribute must contain a non-empty list, where each element in that list is a mapping.
Each mapping has the following key/value pairs:

 * `service` (required): Name of the service to match. The name is usually the basename of the service file.
   However, aliases can also be used for relevant system managers. When aliases **and** multiple service
   managers are involved, then the rule will apply to all matches. See alias handling below.

   - Note: For systemd, the `.service` suffix can be omitted from name, but other suffixes such as `.timer`
     cannot.

 * `type-of-service` (optional, defaults to `service`): The type of service this rule applies to. To act on a
   `systemd` timer, you would set this to `timer` (etc.). Each service manager defines its own set of types
   of services.

 * `service-scope` (optional, defaults to `system`): The scope of the service. It must be either `system` and
   `user`.
   - Note: The keyword is defined to support `user`, but `debputy` does not support `user` services at the moment
     (the detection logic is missing).

 * `service-manager` or `service-managers` (optional): Which service managers this rule is for. When omitted, all
   service managers with this service will be affected. This can be used to specify separate rules for the same
   service under different service managers.
   - When this attribute is explicitly given, then all the listed service managers must provide at least one
     service matching the definition. In contract, when it is omitted, then all service manager integrations
     are consulted but as long as at least one service is match from any service manager, the rule is accepted.

 * `enable-on-install` (optional): Whether to automatically enable the service on installation. Note: This does
   **not** affect whether the service will be started nor how restarts during upgrades will happen.
   - If omitted, the plugin detecting the service decides the default.

 * `start-on-install` (optional): Whether to automatically start the service on installation. Whether it is
   enabled or how upgrades are handled have separate attributes.
   - If omitted, the plugin detecting the service decides the default.

 * `on-upgrade` (optional): How `debputy` should handle the service during upgrades. The default depends on the
   plugin detecting the service. Valid values are:

   - `do-nothing`: During an upgrade, the package should not attempt to stop, reload or restart the service.
   - `reload`: During an upgrade, prefer reloading the service rather than restarting if possible. Note that
     the result may become `restart` instead if the service manager integration determines that `reload` is
     not supported.
   - `restart`: During an upgrade, `restart` the service post upgrade. The service will be left running during
     the upgrade process.
   - `stop-then-start`: Stop the service before the upgrade, perform the upgrade and then start the service.

### Service managers and aliases

When defining a service rule, you can use any name that any of the relevant service managers would call the
service. As an example, consider a package that has the following services:

 * A `sysvinit` service called `foo`
 * A `systemd` service called `bar.service` with `Alias=foo.service` in its definition.

Here, depending on which service managers are relevant to the rule, you can use different names to match.
When the rule applies to the `systemd` service manager, then either of the following names can be used:

 * `bar.service` (the "canonical" name in the systemd world)
 * `foo.service` (the defined alias)
 * `bar` + `foo` (automatic aliases based on the above)

Now, if rule *also* applies to the `sysvinit` service manager, then any of those 4 names would cause the
rule to apply to both the `systemd` and the `sysvinit` services.

To show concrete examples:

    ...:
            services:
              # Only applies to systemd. Either of the 4 names would have work.
              - service: "foo.service"
                on-upgrade: stop-then-start
                service-manager: systemd

    ...:
            services:
              # Only applies to sysvinit. Must use `foo` since the 3 other names only applies when systemd
              # is involved.
              - service: "foo"
                on-upgrade: stop-then-start
                service-manager: sysvinit

    ...:
            services:
              # Applies to both systemd and sysvinit; this works because the `systemd` service provides an
              # alias for `foo`. If the systemd service did not have that alias, only the `systemd` service
              # would have been matched.
              - service: bar
                enable-on-install: false

## Custom binary version (`binary-version`)

In the *rare* case that you need a binary package to have a custom version, you can use the `binary-version:`
key to describe the desired package version.  An example being:

    packages:
        foo:
            # The foo package needs a different epoch because we took it over from a different
            # source package with higher epoch version
            binary-version: '1:{{DEB_VERSION_UPSTREAM_REVISION}}'

Use this feature sparingly as it is generally not possible to undo as each version must be monotonously
higher than the previous one.  This feature translates into `-v` option for `dpkg-gencontrol`.

The value for the `binary-version` key is a string that defines the binary version.  Generally, you will
want it to contain one of the versioned related substitution variables such as
`{{DEB_VERSION_UPSTREAM_REVISION}}`.  Otherwise, you will have to remember to bump the version manually
with each upload as versions cannot be reused and the package would not support binNMUs either.


## Remove runtime created paths on purge or post removal (`clean-after-removal`)

For some packages, it is necessary to clean up some run-time created paths. Typical use cases are
deleting log files, cache files, or persistent state. This can be done via the `clean-after-removal`.
An example being:

    packages:
        foo:
            clean-after-removal:
            - /var/log/foo/*.log
            - /var/log/foo/*.log.gz
            - path: /var/log/foo/
              ignore-non-empty-dir: true
            - /etc/non-conffile-configuration.conf
            - path: /var/cache/foo
              recursive: true


The `clean-after-removal` key accepts a list, where each element is either a mapping, a string or a list
of strings. When an element is a mapping, then the following key/value pairs are applicable:

 * `path` or `paths` (required): A path match (`path`) or a list of path matches (`paths`) defining the
   path(s) that should be removed after clean. The path match(es) can use globs and manifest variables.
   Every path matched will by default be removed via `rm -f` or `rmdir` depending on whether the path
   provided ends with a *literal* `/`. Special-rules for matches:
    - Glob is interpreted by the shell, so shell (`/bin/sh`) rules apply to globs rather than
      `debputy`'s glob rules.  As an example, `foo/*` will **not** match `foo/.hidden-file`.
    - `debputy` cannot evaluate whether these paths/globs will match the desired paths (or anything at
      all). Be sure to test the resulting package.
    - When a symlink is matched, it is not followed.
    - Directory handling depends on the `recursive` attribute and whether the pattern ends with a literal
      "/".
    - `debputy` has restrictions on the globs being used to prevent rules that could cause massive damage
      to the system.

 * `recursive` (optional): When `true`, the removal rule will use `rm -fr` rather than `rm -f` or `rmdir`
    meaning any directory matched will be deleted along with all of its contents.

 * `ignore-non-empty-dir` (optional): When `true`, each path must be or match a directory (and as a
   consequence each path must with a literal `/`). The affected directories will be deleted only if they
   are empty. Non-empty directories will be skipped. This option is mutually exclusive with `recursive`.

 * `delete-on` (optional, defaults to `purge`): This attribute defines when the removal happens. It can
   be set to one of the following values:
   - `purge`: The removal happens with the package is being purged. This is the default. At a technical
     level, the removal occurs at `postrm purge`.
   - `removal`: The removal happens immediately after the package has been removed. At a technical level,
     the removal occurs at `postrm remove`.


This feature resembles the concept of `rpm`'s `%ghost` files.

## Custom installation time search directories (`installation-search-dirs`)

For source packages that does multiple build, it can be an advantage to provide a custom list of
installation-time search directories. This can be done via the `installation-search-dirs` key. A common
example is building  the source twice with different optimization and feature settings where the second
build is for the `debian-installer` (in the form of a `udeb` package). A sample manifest snippet could
look something like:

    installations:
    - install:
        # Because of the search order (see below), `foo` installs `debian/tmp/usr/bin/tool`,
        # while `foo-udeb` installs `debian/tmp-udeb/usr/bin/tool` (assuming both paths are
        # available). Note the rule can be split into two with the same effect if that aids
        # readability or understanding.
        source: usr/bin/tool
        into:
          - foo
          - foo-udeb
    packages:
        foo-udeb:
            installation-search-dirs:
            - debian/tmp-udeb


The `installation-search-dirs` key accepts a list, where each element is a path (str) relative from the
source root to the directory that should be used as a search directory (absolute paths are still interpreted
as relative to the source root).  This list should contain all search directories that should be applicable
for this package (except the source root itself, which is always appended after the provided list). If the
key is omitted, then `debputy` will provide a default  search order (In the `dh` integration, the default
is the directory `debian/tmp`).

If a non-existing or non-directory path is listed, then it will be skipped (info-level note). If the path
exists and is a directory, it will also be checked for "not-installed" paths.


[reference documentation]: https://documentation.divio.com/reference/
