debputy - Integration modes
===========================

_This is [reference documentation] and is primarily useful if you want to know more on integration modes_
_If you want to migrate to one of these integration modes, then [GETTING-STARTED-WITH-dh-debputy.md](GETTING-STARTED-WITH-dh-debputy.md) might be better._

<!-- To writers and reviewers: Check the documentation against https://documentation.divio.com/ -->


The debputy tool is a Debian package builder, and it has multiple levels of
"integration" with the package build. Each integration mode has different
pros and cons, which will be covered in this document.

The integration modes are:

 * `dh-sequence-zz-debputy-rrr`
 * `dh-sequence-zz-debputy` (or `dh-sequence-debputy`)
 * `full`

The integration modes that start with `dh-sequence-` are named such because
they leverage `dh` and its add-on system. The `dh-sequence-zz-` is a trick
to convince `dh` to load the add-ons last when they are activated via
`Build-Depends`, which is part of `debputy`'s strategy for maximizing
compatibility with other `dh` add-ons.

Integration mode - `dh-sequence-zz-debputy-rrr`
-----------------------------------------------

This integration mode is a minimal integration mode aimed exactly at removing the
(implicit) requirement for `fakeroot` in packages that need static ownership in
the `.deb`.

It trades many of `debputy` features for compatibility with `debhelper` and ease
of transitions.

This integration mode is relevant for you when:

 * You want to get rid of the implicit `fakeroot` requirement, and you need static
   ownership, OR
 * You want to transition to `debputy` in the long term, but more involved integration
   modes do not support what you need, OR
 * The mode has a particular feature you want that `debhelper` does not provide.


Pros:

 * You can use `debputy` to assign static ownerships without needing `fakeroot`,
   which is not possible with `debhelper`.
 * You get maximum compatibility with existing `dh` add-ons.
 * Migration is generally possible with minimal or small changes.
 * It is accessible in `bookworm-backports`

Cons:

 * Many of `debputy`'s features cannot be used.
 * Most limitations of `debhelper` still applies (though these limitations are the
   status quo, so the package would have a solution to them if needed).
 * You still longer a turning complete configuration language for your package helper
   (`debian/rules`) with poor introspection.

To migrate, please use:

     debputy migrate-from-dh --migration-target dh-sequence-zz-debputy-rrr

Note: The `debputy migrate-from-dh` command accepts a `--no-act --acceptable-migration-issues=ALL`,
if you want to see how involved the migration will be.

For documentation, please see:
 * [GETTING-STARTED-WITH-dh-debputy.md] for a more detailed migration guide (how-to guide).
 * https://wiki.debian.org/BuildingWithoutFakeroot


Integration mode - `dh-sequence-zz-debputy`
-------------------------------------------

This integration mode is a more involved integration mode of `debputy` that partly leverages
`dh`. In this mode, `debputy` will take over all the logic of installing files into the
respective package staging directories (`debian/<pkg>`). Roughly speaking, the original
`debhelper` runs until `dh_auto_install` and then `debputy` takes over.

This integration mode is relevant when:

 * You want to migrate to the `full` integration mode, but you would like to split the migration
   in two, OR
 * You want to use more of `debputy`'s features and do not use any unsupported `dh` add-ons without
   wanting to migrate the build part.

Pros:

 * You can use the most features of `debputy`. Only the build and environment related ones are
   not accessible.
 * It is accessible in `bookworm-backports`

Cons:

 * Almost all `dh` add-ons will stop working since they rely on being able to see the content
   of `debian/<pkg>`. Since `debputy` will populate *and* assemble the `.deb`, there is never
   a window for the affected add-on to work. Any features provided by these add-ons would have
   to be provided by a `debputy` plugin (or `debputy` itself).
 * Your only `debhelper` limitations is `dh` notorious lack of proper multi-build support.
 * You still longer a turning complete configuration language for your package helper
   (`debian/rules`) with poor introspection.

To migrate, please use:

     debputy migrate-from-dh --migration-target dh-sequence-zz-debputy

Note: The `debputy migrate-from-dh` command accepts a `--no-act --acceptable-migration-issues=ALL`,
if you want to see how involved the migration will be. It will also detect possible incompatible
`dh` add-ons if you are concerned about whether your package can be supported.

For documentation, please see:
 * [GETTING-STARTED-WITH-dh-debputy.md] for a more detailed migration guide (how-to guide).


Integration mode - `full`
-------------------------

This is the integration mode that `debputy` is about. In the `full` integration mode, `debputy`
replaces `dh` as the package helper. It even removes `debian/rules` replacing it with `dpkg`'s
new `Build-Driver` feature.

Pros:

 * You can use all features from `debputy` including its native multi-build support.
 * You can still leverage `debhelper` build systems (anything integrating with the `dh_auto_*`
   tools.) via the `debhelper` build system.
 * You no longer have a turning complete configuration language for your package helper
   (`debian/rules`) with poor introspection.

Cons:

 * (Temporary) Incomplete `debputy migrate-from-dh` support
 * It is a new ecosystem missing a lot of the third-party features you would find for `dh`.
   Only `debhelper` build systems (`dh_auto_*`) can be reused.
 * It requires Debian `trixie` or later due to `Build-Driver` (`dpkg-dev`)
 * You no longer have the flexibility of a turning complete configuration language for your
   package helper, which some people might miss. :)

To migrate, please use:

     debputy migrate-from-dh --migration-target full

Note: The `debputy migrate-from-dh` command accepts a `--no-act --acceptable-migration-issues=ALL`,
if you want to see how involved the migration will be. It will also detect possible incompatible
`dh` add-ons if you are concerned about whether your package can be supported.


[reference documentation]: https://documentation.divio.com/reference/
