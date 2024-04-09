# Getting started with `dh-debputy`

_This is [how-to guide] and is primarily aimed at getting a task done._

<!-- To writers and reviewers: Check the documentation against https://documentation.divio.com/ -->

This document will help you convert a Debian source package using the `dh` sequencer from `debhelper` to
use `dh-debputy`, where `debputy` is integrated with `debhelper`.  Prerequisites for this how-to guide:

 * You have a Debian source package using the `dh` sequencer on `debhelper` compat level 12 or later.
 * It is strongly recommended that your package is bit-for-bit reproducible before starting the conversion
   as that makes it easier to spot bugs introduced by the conversion!  The output of `debputy` will *not*
   be bit-for-bit reproducible with `debhelper` in all cases. However, the differences should be easy to
   review with `diffoscope` if the package was already bit-for-bit reproducible.

Note that during the conversion (particularly Step 2 and Step 3), you may find that `debputy` cannot
support the requirements for your package for now.  Feel free to file an issue for what is holding you
back in the [debputy issue tracker].

Prerequisites
-------------

This guide assumes familiarity with Debian packaging in general.  Notably, you should understand
the different between a (Debian) source package and a (Debian) binary package (e.g., `.deb`) plus
how these concepts relates to `debian/control` (the source control file).

Additionally, since this is about `debputy` integration with debhelper, the reader is expected to
be familiar with `debhelper` (notably the `dh` style `debian/rules`).

## Step 1: Choose the level of migration target

When migrating a package to `debputy`, you have to choose at which level you want `debputy` to manage
your packaging. At the time of writing, your options are:

 1) `dh-sequence-zz-debputy-rrr`: Minimal integration necessary to get support for `Rules-Requires-Root: no`
    in all packages.  It is compatible with most `dh` based packages as it only replaces very few helpers
    and therefore usually also requires very little migration.

 2) `dh-sequence-zz-debputy`: In this integration mode, `debhelper` manages the upstream build system
    (that is, anything up to and including `dh_auto_install`). Everything else is managed by `debputy`.
    This mode is less likely to be compatible with complex packaging at the time of writing. Notably,
    most `debhelper` add-ons will not be compatible with `debputy` in this integration mode.
    - For this integration mode, you are recommended to pick a simple package as many packages cannot be
      converted at this time. Note that this mode does *not* interact well with most third-party
      `dh` addons. You are recommended to start with source packages without third-party `dh` addons.

Since you can always migrate from "less integrated" to "more integrated", you are recommended to start
with `dh-sequence-zz-debputy-rrr` first. If that works, you can re-run the migration with
`dh-sequence-zz-debputy` as the target to see if further integration seems feasible / desirable.

Note: More options may appear in the future.

## Step 2: Have `debputy` convert relevant `debhelper` files

The `debputy` integration with `debhelper` removes (replaces) some debhelper tools, but does **not**
read their relevant config files.  These should instead be converted in to the new format.

You can have `debputy` convert these for you by using the following command:

    # Dry-run conversion, creates `debian/debputy-manifest.new` if anything is migrated and prints a summary
    # of what is done and what you manually need to fix.  Replace `--no-apply-changes` with `--apply-changes`
    # when you are ready to commit the conversion.
    #  - Replace MIGRATION_TARGET in the command with the concrete target from step 1
    $ debputy migrate-from-dh --migration-target MIGRATION_TARGET --no-apply-changes

Note: Running `debputy migrate-from-dh ...` multiple times is supported and often recommended as it can
help you assert that all detectable issues have been fixed.

If relevant, `debputy` may inform you that:

 1) the package needs to change build dependencies or/and activate `dh` add-ons. Concretely, it will ask you to
    ensure that the relevant `dh` add-on is added for `debputy` sequence matching the migration target. Additionally,
    it may ask you to add build-dependencies for activating `debputy` plugins to match certain `dh` add-ons.
    - Note that if `debputy` asks you to add a `debputy` plugin without removing the `dh` add-on it supposedly
      replaces then keep the `dh` active. Sometimes the add-on is only partly converted and sometimes the `dh`
      sequence is used to pull relevant build-dependencies.

 2) the package is using an unsupported feature.  Unless that functionality can easily be removed (e.g., it is now
    obsolete) or easily replaced, then you probably do not want to convert the package to `debputy` at this time.
    * One common source of unsupported features are dh hook targets (such as override targets), which will be covered
      in slightly more detail in the next section.  If `debputy` detected any hook targets, it is probably worth it to
      check if these can be migrated before continuing or run earlier in a hook target that is not removed.
    * Other cases can be that the package uses a feature, where `debputy` behaves differently than `debhelper` would
      at the given compat level. In this case, `debputy` will recommend that you perform a compat upgrade in
      `debhelper` before migrating to `debputy`.
    * It is worth noting that the migration tool will update an existing manifest when re-run. You can safely "jump"
      around in the order of the steps, when you try to migrate, if that better suits you.

 3) the migration would trigger a conflict.  This can occur for two reasons:
    * The debhelper configuration has the conflict (example [#934499]), where debhelper is being lenient and ignores
       the problem.  In this case, you need to resolve the conflict in the debhelper config and re-run `debputy`.
    * The package has a manifest already with a similar (but not identical) definition of what the migration would
       generate.  In this case, you need to reconcile the definitions manually (and removing one of them).  After that
       you can re-run `debputy`.

As an example, if you had a `debian/foo.links` (with `foo` being defined in `debian/control`) containing the following:

    usr/share/foo/my-first-symlink usr/share/bar/symlink-target
    usr/lib/{{DEB_HOST_MULTIARCH}}/my-second-symlink usr/lib/{{DEB_HOST_MULTIARCH}}/baz/symlink-target

The `debputy migrate-from-dh --migration-target dh-sequence-zz-debputy` tool would generate a manifest looking
something like this:

    manifest-version: "0.1"
    packages:
        foo:
            transformations:
             - create-symlink:
                  path: usr/share/foo/my-first-symlink
                  target: /usr/share/bar/symlink-target
             - create-symlink:
                  path: usr/lib/{{DEB_HOST_MULTIARCH}}/my-second-symlink
                  target: /usr/lib/{{DEB_HOST_MULTIARCH}}/baz/symlink-target


## Step 3: Migrate override/hook-targets in debian/rules

Have a look at the hook targets that `debputy migrate-from-dh` flags as unsupported and migrate them to
`debputy` features or move them to earlier hook targets that are supported.  See also the subsections
below for concrete advice on how to deal with override or hook targets for some of these tools. However,
since `debhelper` hooks are arbitrary code execution hooks, there will be many cases that the guide will
not be able to cover or where `debputy` may not have the feature to support your hook target.

While you could manually force any of the removed `debhelper` tools to be run via a hook target, they are
very likely to feature  interact with `debputy`. Either causing `debputy` to replace their output completely
or by having the tool overwrite what `debputy` did (depending on the exact order).  If you find, you
*really* need to run one of these tools, because `debputy` is not supporting a particular feature they have,
then you are probably better off not migrate to this level of `debputy` integration at this time.


### Affected debhelper command list for `dh-sequence-zz-debputy-rrr` integration mode

The following `debhelper` commands are replaced in the `dh-sequence-zz-debputy-rrr` integration mode. Generally,
`debputy migrate-from-dh` will warn you if there is anything to worry about in relation to these commands.

 * `dh_fixperms`
 * `dh_shlibdeps`
 * `dh_gencontrol`
 * `dh_md5sums`
 * `dh_builddeb`

In case something is flagged, it is most likely a hook target, which either have to be converted to `debputy`'s
features or moved earlier. The most common cases are hook targets for `dh_fixperms` and `dh_gencontrol`, which
have sections below advising how to approach those. The only potential problematic command would be `dh_shlibdeps`.
The `debputy` toolchain replaces `dh_shlibdeps` with a similar behavior to that of debhelper compat 14.  If
you need selective promotion or demotion of parts of a substvar, then that is currently not supported.

### Affected debhelper command list for `dh-sequence-zz-debputy` integration mode

The following `debhelper` commands are replaced in the `dh-sequence-zz-debputy` integration mode. The
`debputy migrate-from-dh` command will warn you where it can when some of these are used. However, some
usage may only become apparent during package build. Therefore, `debputy migrate-from-dh` can be silent
even though `debputy` during the build will flag an unsupported feature.

You are recommended to skim through this list for commands where your package might be using non-trivial
or non-default features, as those are likely to prevent migration at this time. Pay extra attention to
commands marked with **(!)** as `debputy` has zero or almost zero support for features from these
commands. Other tools will have some form of support (often at least a commonly used flow/feature set).

 * `dh_install`
 * `dh_installdocs`
 * `dh_installchangelogs`
 * `dh_installexamples`
 * `dh_installman`
 * `dh_installcatalogs` **(!)**
 * `dh_installcron`
 * `dh_installifupdown`
 * `dh_installdebconf` **(!)**
 * `dh_installemacsen` **(!)**
 * `dh_installinfo`
 * `dh_installinit`
 * `dh_installsysusers`
 * `dh_installtmpfiles`
 * `dh_installsystemd`
 * `dh_installsystemduser` **(!)**
 * `dh_installmenu` **(!)**
 * `dh_installmime`
 * `dh_installmodules`
 * `dh_installlogcheck`
 * `dh_installlogrotate`
 * `dh_installpam`
 * `dh_installppp`
 * `dh_installudev`  **(!)**
 * `dh_installgsettings`
 * `dh_installinitramfs`
 * `dh_installalternatives`
 * `dh_bugfiles`
 * `dh_ucf` **(!)**
 * `dh_lintian`
 * `dh_icons`
 * `dh_perl`
 * `dh_usrlocal` **(!)**
 * `dh_links`
 * `dh_installwm` **(!)**
 * `dh_installxfonts`
 * `dh_strip_nondeterminism`
 * `dh_compress`
 * `dh_fixperms`
 * `dh_dwz`
 * `dh_strip`
 * `dh_makeshlibs`
 * `dh_shlibdeps`
 * `dh_missing`
 * `dh_installdeb`
 * `dh_gencontrol`
 * `dh_md5sums`

As mentioned, `debputy migrate-from-dh --no-act` will detect these completely unsupported tools via existence
of their config files or indirectly debhelper hook targets for these tools where possible.  However, some
tools may only be detected late into the build (which is the case with `dh_usrlocal` as a concrete
example).

### Review and migrate any installation code from `dh_install`, `dh_installdocs`, etc. (if any)

_This is section does **not** apply to the `dh-sequence-zz-debputy-rrr` integration mode._

All code in `debian/rules` that involves copying or moving files into packages or around in packages must
be moved to the manifest's `installations` section.  The migration tool will attempt to auto-migrate
any rules from `debian/install` (etc.). However, be aware of:

 1) The migration tool assumes none of install rules overlap.  Most packages do not have overlapping
    install rules as it tends to lead to file conflicts.  If the install rules overlap, `debputy` will
    detect it at *runtime* (not migration time) and stop with an error. In that case, you will have to
    tweak the migrated rules manually.

 2) Any hook target that copies or moves files around in packages must be moved to `installations`
    (per source) or `transformations` (per package) depending on the case.

    - For source packages installing content via `debian/tmp`, you can use `install` to rename paths as
      you install them and `discard` (under `installations`) to ignore paths that should not be installed.

    - For source packages installing content via `debian/<pkg>`, then everything in there is "auto-installed".
      If you need to tweak that content, you can use `remove` or `move` transformations (under `transformations`)
      for manipulation the content.

Keep in mind that the migrator "blindly" appends new rules to the bottom of `installations` if you have any existing
rules (per "none of the install rules overlap"-logic mentioned above).  If you cannot convert all debhelper config
files in one go, or you manually created some installation rules before running the migrator, you may need to
manually re-order the generated installation rules to avoid conflicts due to inadequate ordering.  You probably
want to do so any way for readability/grouping purposes.

Note: For very complex hook targets that manipulate context in packages, it is possible to keep the logic in
`debian/rules` by moving the logic to `execute_after_dh_auto_install`. This will mainly be useful if you are
using complex rules for pruning or moving around files in a way that are not easily representable via globs.

#### Double-check the `language` settings on all `install-man` rules in `installations`

_This is section does **not** apply to the `dh-sequence-zz-debputy-rrr` integration mode._

The `dh_installman` tool auto-detects the language for man pages via two different methods:

 1) By path name (Does the installation path look like `man/<language>/man<section>/...`?)
 2) By basename (Does the basename look like `foo.<language>.<section>`?)

Both methods are used in order with first match being the method of choice.  Unfortunately, the second
method is prune to false-positives.  Does `foo.pl.1` mean a Polish translation of `foo.1` or is it the
man page for a Perl script called `foo.pl` (similar happens for other languages/file extensions).

To avoid this issue, `debputy` uses 1) by default and only that.  Option 2) can be chosen by setting
`language: derive-from-basename` on the concrete installation rule.  The problem is that the migration tool
has to guess, and it is hard to tell if rules like `man/*.1` would need option 2).

Therefore, take a critical look at the generated `install-man` rules and the generated `language` property
(or lack thereof).

### Convert your overrides or excludes for `dh_fixperms` (if any)

The `debputy` tool will normalize permissions like `dh_fixperms` during package build.  If you have
any special requirements that `dh_fixperms` did not solve for you, you will have to tell `debputy`
about them.

If you had something like:

    override_dh_fixperms:
        dh_fixperms -X bin/sudo

and the goal was to have `dh_fixperms` not touch the mode but the ownership (root:root) was fine, you
would have the manifest `debian/debputy.manifest`:

    manifest-version: "0.1"
    packages:
        foo:
            transformations:
             - path-metadata:
                  path: usr/bin/sudo
                  mode: "04755"

Note you have to spell out the desired mode for this file.

On the other hand, if your `debian/rules` had something like:

    execute_after_dh_fixperms:
        chown www-data:www-data debian/foo/var/lib/something-owned-by-www-data

Then the manifest would look something like:

    manifest-version: "0.1"
    packages:
        foo:
            transformations:
             - path-metadata:
                  path: var/lib/something-owned-by-www-data
                  owner: www-data
                  group: www-data

This can be combined with an explicit `mode: "02755"` if you also need a non-default mode.

The paths provided here support substitution variables (`usr/lib/{{DEB_HOST_MULTIARCH}}/...`) and
some _limited_ glob support (`usr/bin/sudo*`).

_Remember to merge your manifest with previous steps rather than replacing it!_  Note that
`debputy migrate-from-dh` will merge its changes into existing manifests and can safely be re-run
after adding/writing this base manifest.

### Convert your overrides for `dh_installsystemd`, `dh_installinit` (if any)

If your package overrides any of the service related helpers, the following use-cases have a trivial
known solution:

 * Use of `--name`
 * Use of `name.service` (etc.) with `dh_installsystemd`
 * Use of `--no-start`, `--no-enable`, or similar options
 * Any combination of the above.

Dealing with `--name` is generally depends on "why" it is used. If it is about having the helper
pick up `debian/pkg.NAME.service` (etc.), then the `--name` can be dropped. This is because `debputy`
automatically resolves the `NAME` without this option.

For uses that involve `--no-start`, `--no-enable`, etc., you will have to add a `services` section
to the package manifest.  As an example:

    override_dh_installinit:
        dh_installinit --name foo --no-start

    override_dh_installsystemd:
        dh_installsystemd foo.service --no-start

Would become:

    manifest-version: "0.1"
    packages:
        foo:
            services:
             - service: foo
               enable-on-install: false

If `sysvinit` and `systemd` should use different options, then you could do something like:


    manifest-version: "0.1"
    packages:
        foo:
            services:
            # In systemd, the service is reloaded, but for sysvinit we use the "stop in preinst, upgrade than start"
            # approach.
            - service: foo
              on-upgrade: reload
              service-manager: systemd
            - service: foo
              on-upgrade: stop-then-start
              service-manager: sysvinit


### Convert your overrides for `dh_gencontrol` (if any)

If the package uses an override to choose a custom version for a binary package, then it is possible in `debputy`
by  using the `binary-version` key under the package.  Here is an example to force the package `foo` to have
epoch `1`:

    manifest-version: "0.1"
    packages:
        foo:
            # The foo package needs a different epoch because we took it over from a different
            # source package with higher epoch version
            binary-version: '1:{{DEB_VERSION_UPSTREAM_REVISION}}'

Useful if the source took over a binary package from a different source and that binary had a higher
epoch version.

Note that only limited manipulation of the version is supported, since your options are generally
limited to expanding one of the following version related variables:

 * `DEB_VERSION` - same definition as the one from `/usr/share/dpkg/pkg-info.mk` (from `dpkg`)
 * `DEB_VERSION_EPOCH_UPSTREAM` - ditto
 * `DEB_VERSION_UPSTREAM_REVISION` - ditto
 * `DEB_VERSION_UPSTREAM` - ditto

If the override is needed for dynamic substitution variables or binary versions that cannot be done with
the above substitutions, then it might be better to hold off on the conversion.

_Remember to merge your manifest with previous steps rather than replacing it!_  Note that
`debputy migrate-from-dh` will merge its changes into existing manifests and can safely be re-run
after adding/writing this base manifest.

## Step 4: Verify the build

At this stage, if there are no errors in your previous steps, you should be able to build your
changed package with `debputy`.  We recommend that you take time to verify this.  For some packages,
there was no conversion to do in the previous steps, and you would not even need a manifest at all
in this case.  However, we still recommend that you verify the build is successful here and now.

The `debputy` supports bit-for-bit reproducibility in its output. However, the output is not bit-for-bit
reproducible with `debhelper`. You are recommended to use `diffoscope` to compare the `debhelper`
built-version with the `debputy` built version to confirm that all the changes are benign.

However, `debputy` is bit-for-bit reproducible in its output with `(fakeroot) dpkg-deb -b`. Should you
spot a difference where `debputy` does not produce bit-for-bit identical results with `dpkg-deb` (tar
format, file ordering, etc.), then please file a bug against `debputy` with a reproducing test case.

Once you have verified your built, the conversion is done. :)  At this point, you can consider
looking at other features that `debputy` supports that might be useful to you.

[how-to guide]: https://documentation.divio.com/how-to-guides/
[#885580]: https://bugs.debian.org/885580
[#934499]: https://bugs.debian.org/934499
[debputy issue tracker]: https://salsa.debian.org/debian/debputy/-/issues
