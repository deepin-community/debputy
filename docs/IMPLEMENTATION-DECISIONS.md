# Implementation decisions for debputy

This document logs important decisions taken during the design of `debputy` along with the
rationale and alternatives considered at the time.  This tech note collects decisions, analysis,
and trade-offs made in the implementation of the system that may be of future interest. It also
collects a list of intended future work. The historical background here may be useful for
understanding design and implementation decisions, but would clutter other documents and distract
from the details of the system as implemented.

## Border between "installation" and "transformation"

In `debputy`, a contributor can request certain actions to be performed such as `install foo into pkg`
or `ensure bar in pkg is a symlink to baz`.  While the former is clearly an installation rule, is the
latter an installation rule or a transformation rule?

Answering this was important to ensure that actions were placed where people would expect them or would
find it logical to look for them.  This is complicated by the fact that `install` (the command line tool)
can perform mode and ownership transformation, create directories, whereas `dh_install` deals only with
installing (copying) paths into packages and mode/ownership changes is related to a separate helper.

The considered options were:

### Install upstream bits and then apply packaging modification (chosen)

In this line of thinking, the logic conceptually boils down to the following rule of thumb:

  > If a path does not come from upstream, then it is a transform.

Expanding a bit, anything that would install paths from upstream (usually `debian/tmp/...`) into a
package is considered part of `installation`.  If further mutations are needed (such as, `create an
empty dir at X as integration point`), they are transformations.

All path metadata modifications (owner, group or mode) are considered transformations.  Even in the
case, where the transformation is "disabling" a built-in normalization.  The logic here is that the
packager's transform rule is undoing a built-in transformation rule.

This option was chosen because it fit the perceived idea of how a packager views their own work
per the following 4-step list:

   1. Do any upstream build required.
   2. Install files to build the initial trees for each Debian package.
   3. Transform those trees for any additional fixes required.
   4. Turn those trees into debs.

Note: The `debhelper` stack has all transformations (according to this definition) under its
installation phase as defined by `dh`'s `install` target. Concretely, the `dh install` target covers
`dh_installdirs`, `dh_link` and `dh_fixperms`. However, it is less important what `debhelper` is
doing as long as the definition is simple and not counter-intuitive to packagers.

### Define the structural and then apply non-structural modifications

Another proposal was to see the `file layout` phase as anything that did structural changes to the
content of the package.  By the end of the `file layout` phase, all paths would be present where
they were expected. So any mutation by the packager that changed the deb structurally would be a
part of the `file layout` phase.

Note file compression (and therefore renaming of files) could occur after `file layout` when this
model was discussed.

The primary advantage was that it works without having an upstream build system. However, even
native packages tend to have an "upstream-like" build system, so it is not as much of an advantage
in practice.

Note this definition is not a 1:1 match with debhelper either.  As an example, file mode
modification would be a transformation in this definition, whereas `debhelper` has it under
`dh install`.

## Stateless vs. Stateful installation rules

A key concept in packaging is to "install" paths provided by upstream's build system into one or
more packages.  In source packages producing multiple binary packages, the packager will want to
divide the content across multiple packages and `debputy` should facilitate this in the best
possible fashion.

There were two "schools of thought" considered here, which is easiest to illustrate with the
following example:

  Assume that the upstream build system provides 4 programs in `debian/tmp/usr/bin`.  One of
  these (`foo`) would have to be installed into the package `pkg` and the other would be more
  special purpose and go into `foo-utils`.


For a "stateless" ruleset, the packager would have to specify the request as:

   * install `usr/bin/foo` into `pkg`
   * install `usr/bin/*` except `usr/bin/foo` into `pkg-utils`

Whereas with a "stateful" ruleset, the packager would have to specify the request as:

   1. install `usr/bin/foo` into `pkg`
   2. install `usr/bin/*` into `pkg-utils`
      - Could be read as "install everything remaining in `usr/bin` into `pkg-utils`".


### Stateful installation rules (chosen)

The chosen model ended up being "stateful" patterns.

Pros:

 1. Stateful rules provides a "natural" way to say "install FILE1 in DIR into A,
    FILE2 from DIR into B, and the rest of DIR into C" without having to accumulating
    "excludes" or replacing a glob with "subglobs" to avoid double matching.

 2. There is a "natural" way to define that something should *not* be installed
    via the `discard` rule, which interfaces nicely with the `dh_missing`-like
    behaviour (detecting things that might have been overlooked).

 3. Avoids the complexity of having a glob expansion with a "per-rule" `exclude`,
    where the `exclude` itself contains globs (`usr/lib/*` except `usr/bin/*.la`).

Cons:
 1. Stateful parsing requires `debputy` to track what has already been matched.
 2. Rules cannot be interpreted in isolation nor out of order.
 3. Naming does not (always) imply the "destructiveness" or state of the action.
    - The `install` term is commonly understood to have `copy` semantics rather
      than `move` semantics.
 4. It is a step away from default `debhelper` mechanics and might cause
    surprises for people assuming `debhelper` semantics.


The 1st con would have applied anyway, as to avoid accidental RC bugs the
contributor is required to explicitly list multiple packages for any install
rule that would install the same path into two distinct packages or to provide
`dh_missing` functionality.  Therefore, the tracking would have existed in
some form regardless.

The 2nd con can be mitigated by leveraging the tracking to report if the
rules appear to run in opposite order.

The 3rd con is partly mitigated by using `discard` rather than `exclude` (which
was the original name). Additionally, the mitigation for the 2nd con generally
covers the most common cases as well. The only "surprising" case if you have
one tool path you want installed into two packages at the same time, where you
use two matches and the second one is a glob. However, the use-case is rare and
was considered an acceptable risk given its probability.

The 4th con is less of a problem when migrating from `debhelper` to `debputy`.
Any `debhelper` based package will not have (unintentional) overlapping matches
causing file conflicts.  There might be some benign double matching that the
packager will have to clean up post migration, because `debhelper` is more
forgiving. Migration from `debputy` to `debhelper` might be more difficult but
not a goal for `debputy`, so it was not considered relevant.

Prior art: `dh-exec` supports a similar feature via `=> usr/bin/foo`.


### Stateless installation rules

Pros:

  1. It matches the default helper, so it requires less cognitive effort for
     people migrating.

  2. The `install` term would effectively have `copy` semantics.

  3. In theory, `debputy` could do with simpler tracking mechanics.
     - In practice, the tracked used for the error reporting required was 80%
       of the complexity. This severely limits any practical benefit.


Cons:

 1. No obvious way to deliberately ignore content that are not of a glob + exclude.
    - While the `usr/bin/* except <matches>` could work, the default is "new appearances"
      gets installed rather than aborting the built with a "there is a new tool for you
      to consider".  Alternatives such as including a stand-alone `exclude` or `discard`
      rule would imply stateful parsing, but would not actually be stateful for `install`
      and therefore being a potential source of confusion.  Therefore, such a feature
      would have to require a separate configuration next to installations.

 2. Install rules with globs would have to accumulate excludes or degenerate to the "magic
    sub-matching globs" to avoid overlaps. The latter is the pattern supported by debhelper.

# Plugin integration

Looking at `debhelper`, one of its major sources of success is that "anyone" could extend it
to solve their specific need and that it was easy to do so.  When looking at the debhelper
extensions, it seems a vast majority of Debian packages do the debhelper extension "on the
side" (such as a bundle it inside an existing `-dev` package).  Having package dedicated
to the debhelper tooling does happen but seems to be very rare.

With this in mind, using python's `entry_points` API was ruled out. It would require packagers
to do a Python project inside their existing package with double build-systems, which basically
no existing package helper does well (CDBS a possible exception but CDBS is generally frowned
upon by the general Debian contributor population).

Instead, a "drop a .json file here" approach was chosen instead to get a more "light-weight"
integration up and running.  When designing it, the following things were important:

 * It should be possible to extract the metadata of the plugin *without* running any code from it
   as running code could "taint" the process and break "list all plugins" features.
   (This ruled out loading python code directly)

 * Simple features would ideally not require code at all.  Packager provided files as an example
   can basically be done as a configuration rather than code.  This means that `debputy` can provide
   automated plugin upgrades from the current format to a future one if needed be.

 * Being able to reuse the declarative parser to handle the error messages and data normalization
   (this implies `JSON`, `YAML` or similar formats that is easily parsed in to mappings and lists).

 * It is important that there is a plugin API compat level that enables us to change the format or
   API between `debputy` and the plugins if we learn that the current API is inadequate.

At the time of writing, the plugin integration is still in development.  What is important can change
as we get actual users.

# Plugin module import vs. initialization design

When loading the python code for plugins, there were multiple ways to initialize the plugin:

 1. The plugin could have an initialization function that is called by `debputy`

 2. The plugin could use `@some_feature` annotations on types to be registered. This is similar
    in spirit to the `dh` add-on system where module load is considered the initialization event.

The 1. option was chosen because it is more reliable at directing the control flow and enables
the plugin module to be importable by non-`debputy` code. The latter might not seem directly
useful, but code like `py.test` attempts to import everything it can (even in `debian/` by
default) which could cause unwanted breakage for plugin providers by simply adding a
Python-based  plugin.

With the module autoloads mechanism, `debputy` would have to assume everything imported is
part of the plugin. This can cause issues (misattribution of errors) when a plugin loads
code or definitions from another plugin in addition to the `py.test` problem above. Safeguards
could be added, but the solutions found would either break the "py.test can import the module"-
property (see above) or be "safety is opt-in rather than always on/opt-out" by having an explicit
"This is plugin X code coming now" - the absence of a marker then causing misattribution).

If these problems could be solved, the annotation based loading could be considered.

Note: This is not to say that `@feature` cannot be used at all in the plugin code. The annotation
can be used to add metadata to things that simplify the logic required by the initialization
function. The annotation would have to be stateless and cannot make assumptions about which
plugin is being loaded while it is run.
