# Migrating a `debhelper` plugin to `debputy`

_This is [how-to guide] and is primarily aimed at getting a task done._

<!-- To writers and reviewers: Check the documentation against https://documentation.divio.com/ -->

This document will help you convert a `debhelper` plugin / `debhelper` tool into a `debputy` plugin.
Prerequisites for this how-to guide:

 * You have a `debhelper` tool/plugin that you want to migrate.  Ideally a simple one as not all tools
   can be migrated at this time.
 * Many debhelper tools do not come with test cases, because no one has created a decent test framework
   for them.  Therefore, consider how you intend to validate that the `debputy` plugin does not have any
   (unplanned) regressions compared to `debhelper` tool.
 * Depending on the features needed, you may need to provide a python hook for `debputy` to interact
   with.
   - Note: `debputy` will handle byte-compilation for you per
     [Side note: Python byte-compilation](#side-note-python-byte-compilation)

Note that during the conversion, you may find that `debputy` cannot support the requirements for your
debhelper tool for now.  Feel free to file an issue for what is holding you back in the
[debputy issue tracker].

Prerequisites
-------------

This guide assumes familiarity with Debian packaging and the debhelper tool stack in general.  Notably,
you are expected to be familiar with the `Dh_Lib.pm` API to the point of recognising references to said
API and how to look up document for methods from said API.

If the debhelper tool is not written in `Dh_Lib.pm`, then you will need to understand how to map the
`Dh_Lib.pm` reference into the language/tool equivalent on your own.

## Step 0: The approach taken

The guide will assume you migrate one tool (a `dh_foo` command) at a time. If you have multiple tools
that need to migrate together, you may want to review "Step 1" below for all tools before migrating to
further steps.

## Step 1: Analyze what features are required by the tools and the concept behind the helper

For the purpose of this guide, we can roughly translate debhelper tools into one or more
of the following categories.


### Supported categories

 * Install `debian/pkg.foo` *as-is* into a directory.
    - This category uses a mix of `pkgfile` + `install_dir` + `install_file` / `install_prog`
    - Example: `dh_installtmpfiles`
 * If some file is installed in or beneath a directory, then (maybe) analyze the file, and apply metadata
    (substvars, maintscripts, triggers, etc.). Note this does *not* apply to special-case of services.
    While services follow this pattern, `debputy` will have special support for services.
    - Typically, this category uses a bit of glob matching + (optionally) `open` +
      `addsubstvars` / `autoscript` / `autotrigger`
    - Example: `dh_installtmpfiles`
    - *Counter* examples: `dh_installsystemd` (due to service rule, below).

### Unsupported categories

 * Read `debian/pkg.foo` and do something based on the content of said file.
    - Typically, the category uses a mix of `pkgfile` + `filedoublearray` / `filearray` / `open(...)`.
      The most common case of this is to install a list of files in the `debian/pkg.foo` file.
    - In this scenario, the migration strategy should likely involve replacing `debian/pkg.foo` with
      a section inside the `debian/debputy.manifest` file.
    - Example: `dh_install`
 * Any tool that manages services like `systemd`, `init.d` or `runit`.
    - Typically, this category uses a bit of glob matching + (optionally) `open` +
      `addsubstvars` / `autoscript` / `autotrigger`.
    - This is unsupported because services will be a first-class feature in `debputy`, but the feature
      is not fully developed yet.
    - Example: `dh_installsystemd`
 * Based on a set of rules, modify a set of files if certain criteria are met.
    - Example: `dh_strip`, `dh_compress`, `dh_dwz`, `dh_strip_nondeterminism`, `dh_usrlocal`
 * Run custom build system logic that cannot or has not been fit into the `debhelper` Buildsystem API.
    - Example: `dh_cmake_install`, `dh_raku_build`, etc.
 * "None of the above". There are also tools that have parts not fitting into any of the above
    - Which just means the guide has no good help to offer you for migrating.
    - Example: `dh_quilt_patch`

As mentioned, a tool can have multiple categories at the same time.  As an example:

 * The `dh_installtmpfiles` tool from debhelper is a mix between "installing `debian/pkg.tmpfiles` in to
   `usr/lib/tmpfiles.d`" and "Generate a maintscript based on `<prefix>/tmpfiles.d/*.conf` globs".

 * The `dh_usrlocal` tool from debhelper is a mix between "Generate a maintscript to create dirs in
   `usr/local` as necessary on install and clean up on removal" and "Remove any directory from `usr/local`
   installed into the package".


When migrating a tool (or multiple tools), it is important to assert that all categories are supported by
the `debputy` plugin API.  Otherwise, you will end with a half-finished plugin and realize you cannot
complete the migration because you are missing a critical piece that `debputy` currently do not support.

If your tool does not fit inside those two base categories, you cannot fully migrate the tool. You should
consider whether it makes sense to continue without the missing features.

## Step 2: Setup basic infrastructure

This how-to guide assumes you will be using the debhelper integration via `dh-sequence-installdebputy`. To
do that, add `dh-sequence-installdebputy` in the `Build-Depends` in `debian/control`. With this setup,
any `debputy` plugin should be provided in the directory `debian/<package>.debputy-plugins` (replace `<package>`
with the name of the package that should provide the plugin).

In this directory, for each plugin, you can see the following files:

    debian/package.debputy-plugins/my-plugin.json      # Metadata file (mandatory)
    debian/package.debputy-plugins/my_plugin.py        # Python implementation (optional, note "_" rather than "_")
    debian/package.debputy-plugins/my_plugin_check.py  # tests (optional, run with py.test, note "_" rather than "_")
                                                       # Alternative names such as _test.py or _check_foo.py works too

A basic version of the JSON plugin metadata file could be:


```json
{
  "plugin-initializer": "initialize_my_plugin",
  "api-compat-level": 1,
  "packager-provided-files": [
    {
      "stem": "foo",
      "installed-path": "/usr/share/foo/{name}.conf"
    }
  ]
}
```

This example JSON assumes that you will be providing both python code (`plugin-intializer`, requires a Python
implementation file) and packager provided files (`packager-provided-files`). In some cases, you will *not*
need all of these features. Notably, if you find that you do not need any feature requiring python code,
you are recommended to remove `plugin-initializer` from the plugin JSON file.

A Python-based plugin for `debputy` plugin starts with an initialization function like this:

```python
from debputy.plugin.api import DebputyPluginInitializer

def initialize_my_plugin(api: DebputyPluginInitializer):
    pass
```

Remember to replace the values in the JSON, so they match your plugin. The keys are:

 * `plugin-initializer`: (Python plugin-only) The function `debputy` should call to initialize your plugin. This is
   the function we  just defined in the previous example). The plugin loader requires this initialization function to
   be a top level function of the module (that is, `getattr(module, plugin_initializer)` must return the initializer
   function).
 * `module`: (Python plugin-only, optional) The python module the `plugin-initializer` function is defined in.
   If omitted, `debputy` will derive the module name from the plugin name (replace `-` with `_`). When omitted,
   the Python module can be placed next to the `.json` file.  This is useful single file plugins.
 * `api-compat-level`: This is the API compat level of `debputy` required to load the
   plugin. This defines how `debputy` will load the plugin and is to ensure that
   `debputy`'s plugin API can evolve gracefully.  For now, only one version is supported
   and that is `1`.
 * `packager-provided-files`: Declares packager provided files. This keyword is covered in the section below.

This file then has to be installed into the `debputy` plugin directory.

With this you have an empty plugin that `debputy` can load, but it does not provide any features.

## Step 3: Provide packager provided files (Category 1 tools)

*This step only applies if the tool in question automatically installs `debian/pkg.foo` in some predefined path
like `dh_installtmpfiles` does.  If not, please skip this section as it is not relevant to your case.*

You can ask `debputy` to automatically detect `debian/pkg.foo` files and install them into a concrete directory
via the plugin. You have two basic options for providing packager provided files.

 1) A pure-JSON plugin variant.
 2) A Python plugin variant.

This guide will show you both. The pure-JSON variant is recommended assuming it satisfies your needs as it is
the simplest to get started with and have fewer moving parts.  The Python plugin has slightly more features
for the "1% special cases".

### Packager provided files primer on naming convention

This section will break the filename `debian/g++-3.0.name.segment.my.file.type.amd64` down into parts and name
the terms `debputy` uses for them and how they are used. If you already know the terms, you can skip this section.

This example breaks into 4 pieces, in order:

 * An optional package name (`g++-3.0`). Decides which package the file applies to (defaulting to the main package
   if omitted).  It is also used as the default "installed as name".

 * An optional "name segment" (`name.segment`). Named so after the `--name` parameter from `debhelper` that is
   needed for `debhelper` to detect files with the segment and because it also changes the default "installed as
   name" (both in `debhelper` and `debputy`). When omitted, the package name decides the "installed as name".

 * The "stem" (`my.file.type`). This part never had an official name in `debhelper` other than `filename`
   or `basename`.

 * An optional architecture restriction. It is used in special cases like `debian/foo.symbols.amd64` where you
   have architecture specific details in the file.

In `debputy`, when you register a packager provided file, you have some freedom in which of these should apply
to your file.  The architecture restriction is rarely used and disabled by default, whereas the "name segment"
is available by default. When the "name segment" is enabled, the packager is able to:

 1) choose a different filename than the package name (by using `debian/package.desired-name.foo` instead of
    `debian/package.foo`)

 2) provide multiple files for the same package (`debian/package.foo` *and* `debian/package.some-name.foo`).

If it is important that a package can provide at most one file, and it must be named after the package itself,
you are advised to disable to name segment.

### JSON-based packager provided files (Category 1 tools)

With the pure JSON based method, the plugin JSON file should contain all the relevant details. A minimal
example is:

```json
{
  "api-compat-level": 1,
  "packager-provided-files": [
    {
      "stem": "foo",
      "installed-path": "/usr/share/foo/{name}.conf",
      "reference-documentation": {
        "description": "Some possibly multi-line description related to foo",
        "format-documentation-uris": ["man:foo.conf(5)"]
      }
    }
  ]
}
```
(This file should be saved as `debian/package.debputy-plugins/my-plugin.json`.)

This plugin snippet would provide one packager provided files and nothing else. When loading the plugin, `debputy`
would detect files such as `debian/package.foo` and install them into `/usr/share/foo/package.conf`.

As shown in the example. the packager provided files are declared as a list in the attribute
`packager-provided-files`. Each element in that list is an object with the following keys:

 * `name` (required): The "stem" of the file. In the example above, `"foo"` is used meaning that `debputy`
   would detect `debian/package.foo`. Note that this value must be unique across all packager provided files known
   by `debputy` and all loaded plugins.

 * `installed-path` (required): A format string describing where the file should be installed. This is
   `"/usr/share/foo/{name}.conf"` from the example above and leads to `debian/package.foo` being installed
   as `/usr/share/foo/package.conf`.

   The following placeholders are supported:

     * `{name}` - The name in the name segment (defaulting the package name if no name segment is given)
     * `{priority}` / `{priority:02}` - The priority of the file. Only provided priorities are used (that
       is, `default-priority` is provided).  The latter variant ensuring that the priority takes at least
       two characters and the `0` character is left-padded for priorities that takes less than two
       characters.
     * `{owning_package}` - The name of the package.  Should only be used when `{name}` alone is insufficient.
       If you do not want the "name" segment in the first place, set `allow-name-segment` to `false` instead.

     The path is always interpreted as relative to the binary package root.

 * `default-mode` (optional): If provided, it must be an octal mode (such as `"0755"`), which defines the mode
   that `debputy` will use by default for this kind of file. Note that the mode must be provided as a string.

 * `default-priority` (optional): If provided, it must be an integer declaring the default priority of the file,
   which will be a part of the filename.  The `installed-path` will be required to have the `{priority}` or
   `{priority:02}` placeholder. This attribute is useful for directories where the files are read in "sorted"
   and there is a convention of naming files like `20-foo.conf` to ensure files are processed in the correct
   order.

 * `allow-name-segment` (optional): If provided, it must be a boolean (defaults to `true`), which determines
   whether `debputy` should allow a name segment for the file.

 * `allow-architecture-segment` (optional): If provided, it must be a boolean (defaults to `false`), which determines
   whether `debputy` should allow an architecture restriction for the file.

 * `reference-documentation` (optional): If provided, the following keys can be used:

    * `description` (optional): If provided, it is used as a description for the file if the user requests
       documentation about the file.

    * `format-documentation-uris` (optional): If provided, it must be a list of URIs that describes the format
       of the file. `http`, `https` and `man` URIs are recommended.


### Python-based packager provided files (Category 1 tools)  [NOT RECOMMENDED]

**This section uses a Python-based API, which is not recommended at this time as the logistics are not finished**

With the Python based method, the plugin JSON file should contain a reference to the python module. A minimal
example is:

```json
{
  "api-compat-level": 1,
  "plugin-initializer": "initialize_my_plugin"
}
```
(This file should be saved as `debian/package.debputy-plugins/my-plugin.json`.)

The python module file should then provide the `initialize_my_plugin` function, which could look something like this:

```python
from debputy.plugin.api import DebputyPluginInitializer

def initialize_my_plugin(api: DebputyPluginInitializer):
    api.packager_provided_file(
        "foo",  # This is the "foo" in "debian/pkg.foo"
        "/usr/share/foo/{name}.conf",  # This is the directory to install them at.
    )
```
(This file would be saved as `debian/package.debputy-plugins/my_plugin.py` assuming `my-plugin.json` was
used for the metadata file)

This example code would make `debputy` install `debian/my-pkg.foo` as `/usr/share/foo/my-pkg.conf` provided the
plugin is  loaded. Please review the API docs for the full details of options.

This can be done via the interactive python shell with:

```python
import sys
sys.path.insert(0, "/usr/share/dh-debputy/")
from debputy.plugin.api import DebputyPluginInitializer
help(DebputyPluginInitializer.packager_provided_file)
```

### Testing your plugin

If you are the type that like to provide tests for your code, the following `py.test` snippet can get you started:

```python
from debputy.plugin.api.test_api import initialize_plugin_under_test


def test_packager_provided_files():
    plugin = initialize_plugin_under_test()
    ppf_by_stem = plugin.packager_provided_files_by_stem()
    assert ppf_by_stem.keys() == {'foo'}
    foo_file = ppf_by_stem['foo']

    assert foo_file.stem == 'foo'

    # Note, the discard part is the installed into directory, and it is skipped because `debputy`
    # normalize the directory as an implementation detail and the test would depend on said detail
    # for no good reason in this case.  If your case have the variable in the directory part, tweak
    # the test as necessary.
    _, basename = foo_file.compute_dest("my-package")
    assert basename == 'my-package.conf'
    # Test other things you might have configured:
    #   assert foo_file.default_mode == 0o755   # ... if the file is to be executable
    #   assert foo_file.default_priority == 20  # ... if the file has priority
    #   ...
```
(This file would be saved as `debian/package.debputy-plugins/my_plugin_check.py` assuming `my-plugin.json` was
used for the metadata file)

This test works the same regardless of whether the JSON-based or Python-based method was chosen.

## Step 4: Migrate metadata detection (Category 3 tools)  [NOT RECOMMENDED]

*This step only applies if the tool in question generates substvars, maintscripts or triggers based on
certain paths being present or having certain content like `dh_installtmpfiles` does.  However,
this section does **NOT** apply to service management tools (such as `dh_installsystemd`). If not, please
skip this section as it is not relevant to your case.*

For dealing with substvars, maintscripts and triggers, the plugin will need to register a function that
can perform the detection. The `debputy` API refers to it as a "detector" and functionally it behaves like
a "callback" or "hook". The "detector" will be run once per package that it applies to with some context and
is expected to register the changes it wants.

A short example is:

```python
from debputy.plugin.api import (
   DebputyPluginInitializer,
   VirtualPath,
   BinaryCtrlAccessor,
   PackageProcessingContext,
)


def initialize_my_plugin(api: DebputyPluginInitializer):
   # ... remember to preserve any existing code here that you may have had from previous steps.
   api.metadata_or_maintscript_detector(
      "foo-detector",  # This is an ID of the detector. It is part of the plugins API and should not change.
      # Packagers see it if it triggers an error and will also be able to disable by this ID.
      detect_foo_files,  # This is the detector (hook) itself.
   )


def detect_foo_files(fs_root: VirtualPath,
                     ctrl: BinaryCtrlAccessor,
                     context: PackageProcessingContext,
                     ) -> None:
   # If for some reason the hook should not apply to all packages, and `metadata_or_maintscript_detector` does not
   # provide a filter for it, then you just do an `if <should not apply>: return`
   if not context.binary_package.is_arch_all:
      # For some reason, our hook only applies to arch:all packages.
      return

   foo_dir = fs_root.lookup("usr/share/foo")
   if not foo_dir:
      return

   conf_files = [path.absolute for path in foo_dir.iterdir if path.is_file and path.name.endswith(".conf")]
   if not conf_files:
      return
   ctrl.substvars.add_dependency("misc:Depends", "foo-utils")
   conf_files_escaped = ctrl.maintscript.escape_shell_words(*conf_files)
   # With multi-line snippets, consider:
   #
   # snippet = textwrap.dedent("""\
   #     ... content here using {var}
   # """).format(var=value)
   #
   # (As that tends to result in more readable snippets, when the dedent happens before formatting)
   snippet = f"foo-analyze --install {conf_files_escaped}"
   ctrl.maintscript.on_configure(snippet)
```
(This file would be saved as `debian/package.debputy-plugins/my_plugin.py`)

This code would register the `detect_foo_files` function as a metadata hook. It would be run for all regular `deb`
packages processed by `debputy` (`udeb` requires opt-in, auto-generated packages such as `-dbgsym` cannot be
targeted).

The hook conditionally generates a dependency (via the `${misc:Depends}` substvar) on `foo-utils` and a `postinst`
snippet to be run when the package is configured.

An important thing to note is that `debputy` have *NOT* materialized the package anywhere. Instead, `debputy`
provides an in-memory view of the file system (`fs_root`) and related path metadata that the plugin should base its
analysis of. The in-memory view of the file system can have virtual paths that are not backed by any real
path on the file system.  This commonly happens for directories and symlinks - and during tests, also for files.


In addition to the python code above, remember that the plugin JSON file should contain a reference to the python
module. A minimal example for this is:

```json
{
  "api-compat-level": 1,
  "plugin-initializer": "initialize_my_plugin"
}
```
(This file should be saved into `debian/package.debputy-plugins/my-plugin.json` assuming `my_plugin.py` was
used for the module file)


If you are the type that like to provide tests for your code, the following `py.test` snippet can get you started:

```python
from debputy.plugin.api.test_api import initialize_plugin_under_test, build_virtual_file_system, \
    package_metadata_context


def test_packager_provided_files():
    plugin = initialize_plugin_under_test()
    detector_id = 'foo-detector'

    fs_root = build_virtual_file_system([
        '/usr/share/foo/foo.conf'  # Creates a virtual (no-content) file.
        # Use virtual_path_def(..., fs_path="/path") if your detector needs to read the file
        # NB: You have to create that "/path" yourself.
    ])

    metadata = plugin.run_metadata_detector(
        detector_id,
        fs_root,
        # Test with an arch:any package. The test framework will supply a minimum number of fields
        # (e.g., "Package") so you do not *have* to provide them if you do not need them.
        # That is also why providing `Architecture` alone works here.
        context=package_metadata_context(package_fields={'Architecture': 'any'})
    )
    # Per definition of our detector, there should be no dependency added (even though the file is there)
    assert 'misc:Depends' not in metadata.substvars
    # Nor should any maintscripts have been added
    assert metadata.maintscripts() == []

    metadata = plugin.run_metadata_detector(
        detector_id,
        fs_root,
        # This time, we test with an arch:all package
        context=package_metadata_context(package_fields={'Architecture': 'all'})
    )

    assert metadata.substvars['misc:Depends'] == 'foo-utils'

    # You could also have added `maintscript='postinst'` to filter by which script it was added to.
    snippets = metadata.maintscripts()
    # There should be exactly one snippet
    assert len(snippets) == 1
    snippet = snippets[0]
    # And we can verify that the snippet is as expected.
    assert snippet.maintscript == 'postinst'
    assert snippet.registration_method == 'on_configure'
    assert 'foo-analyze --install /usr/share/foo/foo.conf' in snippet.plugin_provided_script
```
(This file should be saved into `debian/package.debputy-plugins/my_plugin_check.json` assuming `my_plugin.py` was
used for the module file)

This test works the same regardless of whether the JSON-based or Python-based method was chosen.

## Step 4: Have your package provide `debputy-plugin-X`

All third-party `debputy` plugins are loaded by adding a build dependency on `debputy-plugin-X`,
where `X` is the basename of the plugin JSON file.  Accordingly, any package providing a `debputy` plugin
must either be named `debputy-plugin-X` or provide `debputy-plugin-X` (where `X` is replaced with the concrete
plugin name).

## Step 5: Running the tests

To run the tests, you have two options:

 1) Add `python3-pytest <!nocheck>` to the `Build-Depends` in `debian/control`. This will cause
    `dh_installdebputy` to run the tests when the package is built. The tests will be skipped
    if `DEB_BUILD_OPTIONS` contains `nocheck` per Debian Policy.  You will also need to have
    the `debputy` command in PATH. This generally happens as a side effect of the
    `dh-sequence-installdebputy` build dependency.

 2) Add `autopkgtest-pkg-debputy` to the `Testsuite` field in `debian/control`.  This will cause
    the Debian CI framework (via the `autodep8` command) to generate an autopkgtest that will
    run the plugin tests against the installed plugin.

Using both options where possible is generally preferable.

If your upstream uses a Python test framework that auto-detects tests such as `py.test`, you may
find that it picks up the `debputy` plugin or its tests. If this is causing you issues, please have
a  look at the `dh_installdebputy` man page, which have a section dedicated to how to resolve these
issues.

## Side note: Python byte-compilation

When you install a `debputy` plugin into `/usr/share/debputy/debputy/plugins`, then `debputy` will
manage the Python byte-compilation for you.

## Closing

You should now either have done all the basic steps of migrating the debhelper tool to `debputy`
or discovered some feature that the guide did not cover. In the latter case, please have a look
at the [debputy issue tracker] and consider whether you should file a feature request for it.

[how-to guide]: https://documentation.divio.com/how-to-guides/
[debputy issue tracker]: https://salsa.debian.org/debian/debputy/-/issues
