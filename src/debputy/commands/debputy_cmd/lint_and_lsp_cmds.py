import random
import textwrap
from argparse import BooleanOptionalAction

from debputy.commands.debputy_cmd.context import ROOT_COMMAND, CommandContext, add_arg
from debputy.lsp.lsp_reference_keyword import ALL_PUBLIC_NAMED_STYLES
from debputy.util import _error


_EDITOR_SNIPPETS = {
    "emacs": "emacs+eglot",
    "emacs+eglot": textwrap.dedent(
        """\
        ;; `deputy lsp server` glue for emacs eglot (eglot is built-in these days)
        ;;
        ;; Add to ~/.emacs or ~/.emacs.d/init.el and then activate via `M-x eglot`.
        ;;
        ;; Requires: apt install elpa-dpkg-dev-el elpa-yaml-mode
        ;; Recommends: apt install elpa-markdown-mode

        ;; Make emacs recognize debian/debputy.manifest as a YAML file
        (add-to-list 'auto-mode-alist '("/debian/debputy.manifest\\'" . yaml-mode))
        ;; Inform eglot about the debputy LSP
        (with-eval-after-load 'eglot
          (add-to-list 'eglot-server-programs
                       '(
                         (
                            ;; Requires elpa-dpkg-dev-el (>= 37.12)
                            (debian-autopkgtest-control-mode :language-id "debian/tests/control")
                            ;; Requires elpa-dpkg-dev-el
                            (debian-control-mode :language-id "debian/control")
                            (debian-changelog-mode :language-id "debian/changelog")
                            (debian-copyright-mode :language-id "debian/copyright")
                            ;; No language id for these atm.
                            makefile-gmake-mode
                            ;; Requires elpa-yaml-mode
                            yaml-mode
                          )
                         . ("debputy" "lsp" "server")
        )))

        ;; Auto-start eglot for the relevant modes.
        (add-hook 'debian-control-mode-hook 'eglot-ensure)
        ;; Requires elpa-dpkg-dev-el (>= 37.12)
        ;;   Technically, the `eglot-ensure` works before then, but it causes a
        ;;   visible and very annoying long delay on opening the first changelog.
        ;;   It still has a minor delay in 37.12, which may still be too long for
        ;;   for your preference. In that case, comment it out.
        (add-hook 'debian-changelog-mode-hook 'eglot-ensure)
        (add-hook 'debian-copyright-mode-hook 'eglot-ensure)
        ;; Requires elpa-dpkg-dev-el (>= 37.12)
        (add-hook 'debian-autopkgtest-control-mode-hook 'eglot-ensure)
        (add-hook 'makefile-gmake-mode-hook 'eglot-ensure)
        (add-hook 'yaml-mode-hook 'eglot-ensure)
    """
    ),
    "vim": "vim+youcompleteme",
    "vim+youcompleteme": textwrap.dedent(
        """\
        # debputy lsp server glue for vim with vim-youcompleteme. Add to ~/.vimrc
        #
        # Requires: apt install vim-youcompleteme

        # Make vim recognize debputy.manifest as YAML file
        au BufNewFile,BufRead debputy.manifest          setf yaml
        # Inform vim/ycm about the debputy LSP
        # - NB: No known support for debian/tests/control that we can hook into.
        #   Feel free to provide one :)
        let g:ycm_language_server = [
          \\   { 'name': 'debputy',
          \\     'filetypes': [ 'debcontrol', 'debcopyright', 'debchangelog', 'make', 'yaml'],
          \\     'cmdline': [ 'debputy', 'lsp', 'server', '--ignore-language-ids' ]
          \\   },
          \\ ]

        packadd! youcompleteme
        # Add relevant ycm keybinding such as:
        # nmap <leader>d <plug>(YCMHover)
    """
    ),
    "vim+vim9lsp": textwrap.dedent(
        """\
        # debputy lsp server glue for vim with vim9 lsp. Add to ~/.vimrc
        #
        # Requires https://github.com/yegappan/lsp to be in your packages path

        vim9script

        # Make vim recognize debputy.manifest as YAML file
        autocmd BufNewFile,BufRead debputy.manifest setfiletype yaml

        packadd! lsp

        final lspServers: list<dict<any>> = []

        if executable('debputy')
            lspServers->add({
                filetype: ['debcontrol', 'debcopyright', 'debchangelog', 'make', 'yaml'],
                path: 'debputy',
                args: ['lsp', 'server', '--ignore-language-ids']
            })
        endif

        autocmd User LspSetup g:LspOptionsSet({semanticHighlight: true})
        autocmd User LspSetup g:LspAddServer(lspServers)
        """
    ),
    "neovim": "neovim+nvim-lspconfig",
    "neovim+nvim-lspconfig": textwrap.dedent(
        """\
        # debputy lsp server glue for neovim with nvim-lspconfig. Add to ~/.config/nvim/init.lua
        #
        # Requires https://github.com/neovim/nvim-lspconfig to be in your packages path

        require("lspconfig").debputy.setup {capabilities = capabilities}

        # Make vim recognize debputy.manifest as YAML file
        vim.filetype.add({filename = {["debputy.manifest"] = "yaml"})
        """
    ),
}


lsp_command = ROOT_COMMAND.add_dispatching_subcommand(
    "lsp",
    dest="lsp_command",
    help_description="Language server related subcommands",
)


@lsp_command.register_subcommand(
    "server",
    log_only_to_stderr=True,
    help_description="Start the language server",
    argparser=[
        add_arg(
            "--tcp",
            action="store_true",
            help="Use TCP server",
        ),
        add_arg(
            "--ws",
            action="store_true",
            help="Use WebSocket server",
        ),
        add_arg(
            "--host",
            default="127.0.0.1",
            help="Bind to this address (Use with --tcp / --ws)",
        ),
        add_arg(
            "--port",
            type=int,
            default=2087,
            help="Bind to this port (Use with --tcp / --ws)",
        ),
        add_arg(
            "--ignore-language-ids",
            dest="trust_language_ids",
            default=True,
            action="store_false",
            help="Disregard language IDs from the editor (rely solely on filename instead)",
        ),
    ],
)
def lsp_server_cmd(context: CommandContext) -> None:
    parsed_args = context.parsed_args

    feature_set = context.load_plugins()

    from debputy.lsp.lsp_self_check import assert_can_start_lsp

    assert_can_start_lsp()

    from debputy.lsp.lsp_features import (
        ensure_lsp_features_are_loaded,
    )
    from debputy.lsp.lsp_dispatch import DEBPUTY_LANGUAGE_SERVER

    ensure_lsp_features_are_loaded()
    debputy_language_server = DEBPUTY_LANGUAGE_SERVER
    debputy_language_server.plugin_feature_set = feature_set
    debputy_language_server.dctrl_parser = context.dctrl_parser
    debputy_language_server.trust_language_ids = parsed_args.trust_language_ids

    debputy_language_server.finish_startup_initialization()

    if parsed_args.tcp and parsed_args.ws:
        _error("Sorry, --tcp and --ws are mutually exclusive")

    if parsed_args.tcp:
        debputy_language_server.start_tcp(parsed_args.host, parsed_args.port)
    elif parsed_args.ws:
        debputy_language_server.start_ws(parsed_args.host, parsed_args.port)
    else:
        debputy_language_server.start_io()


@lsp_command.register_subcommand(
    "editor-config",
    help_description="Provide editor configuration snippets",
    argparser=[
        add_arg(
            "editor_name",
            metavar="editor",
            choices=_EDITOR_SNIPPETS,
            default=None,
            nargs="?",
            help="The editor to provide a snippet for",
        ),
    ],
)
def lsp_editor_glue(context: CommandContext) -> None:
    editor_name = context.parsed_args.editor_name

    if editor_name is None:
        content = []
        for editor_name, payload in _EDITOR_SNIPPETS.items():
            alias_of = ""
            if payload in _EDITOR_SNIPPETS:
                alias_of = f" (short for: {payload})"
            content.append((editor_name, alias_of))
        max_name = max(len(c[0]) for c in content)
        print(
            "This version of debputy has instructions or editor config snippets for the following editors: "
        )
        print()
        for editor_name, alias_of in content:
            print(f" * {editor_name:<{max_name}}{alias_of}")
        print()
        choice = random.Random().choice(list(_EDITOR_SNIPPETS))
        print(
            f"Use `debputy editor-config {choice}` (as an example) to see the instructions for a concrete editor."
        )
        return
    result = _EDITOR_SNIPPETS[editor_name]
    while result in _EDITOR_SNIPPETS:
        result = _EDITOR_SNIPPETS[result]
    print(result)


@lsp_command.register_subcommand(
    "features",
    help_description="Describe language ids and features",
)
def lsp_describe_features(context: CommandContext) -> None:

    from debputy.lsp.lsp_self_check import assert_can_start_lsp

    try:
        from debputy.lsp.lsp_features import describe_lsp_features
    except ImportError:
        assert_can_start_lsp()
        raise AssertionError(
            "Cannot load the language server features but `assert_can_start_lsp` did not fail"
        )

    describe_lsp_features(context)


@ROOT_COMMAND.register_subcommand(
    "lint",
    log_only_to_stderr=True,
    help_description="Provide diagnostics for the packaging (like `lsp server` except no editor is needed)",
    argparser=[
        add_arg(
            "--spellcheck",
            dest="spellcheck",
            action="store_true",
            help="Enable spellchecking",
        ),
        add_arg(
            "--auto-fix",
            dest="auto_fix",
            action="store_true",
            help="Automatically fix problems with trivial or obvious corrections.",
        ),
        add_arg(
            "--linter-exit-code",
            dest="linter_exit_code",
            default=True,
            action=BooleanOptionalAction,
            help='Enable or disable the "linter" convention of exiting with an error if severe issues were found',
        ),
        add_arg(
            "--lint-report-format",
            dest="lint_report_format",
            default="term",
            choices=["term", "junit4-xml"],
            help="The report output format",
        ),
        add_arg(
            "--report-output",
            dest="report_output",
            default=None,
            action="store",
            help="Where to place the report (for report formats that generate files/directory reports)",
        ),
        add_arg(
            "--warn-about-check-manifest",
            dest="warn_about_check_manifest",
            default=True,
            action=BooleanOptionalAction,
            help="Warn about limitations that check-manifest would cover if d/debputy.manifest is present",
        ),
    ],
)
def lint_cmd(context: CommandContext) -> None:
    try:
        import lsprotocol
    except ImportError:
        _error("This feature requires lsprotocol (apt-get install python3-lsprotocol)")

    from debputy.linting.lint_impl import perform_linting

    context.must_be_called_in_source_root()
    perform_linting(context)


@ROOT_COMMAND.register_subcommand(
    "reformat",
    help_description="Reformat the packaging files based on the packaging/maintainer rules",
    argparser=[
        add_arg(
            "--style",
            dest="named_style",
            choices=ALL_PUBLIC_NAMED_STYLES,
            default=None,
            help="The formatting style to use (overrides packaging style).",
        ),
        add_arg(
            "--auto-fix",
            dest="auto_fix",
            default=True,
            action=BooleanOptionalAction,
            help="Whether to automatically apply any style changes.",
        ),
        add_arg(
            "--linter-exit-code",
            dest="linter_exit_code",
            default=True,
            action=BooleanOptionalAction,
            help='Enable or disable the "linter" convention of exiting with an error if issues were found',
        ),
        add_arg(
            "--supported-style-is-required",
            dest="supported_style_required",
            default=True,
            action="store_true",
            help="Fail with an error if a supported style cannot be identified.",
        ),
        add_arg(
            "--unknown-or-unsupported-style-is-ok",
            dest="supported_style_required",
            action="store_false",
            help="Do not exit with an error if no supported style can be identified. Useful for general"
            ' pipelines to implement "reformat if possible"',
        ),
        add_arg(
            "--missing-style-is-ok",
            dest="supported_style_required",
            action="store_false",
            help="[Deprecated] Use --unknown-or-unsupported-style-is-ok instead",
        ),
    ],
)
def reformat_cmd(context: CommandContext) -> None:
    try:
        import lsprotocol
    except ImportError:
        _error("This feature requires lsprotocol (apt-get install python3-lsprotocol)")

    from debputy.linting.lint_impl import perform_reformat

    context.must_be_called_in_source_root()
    perform_reformat(context, named_style=context.parsed_args.named_style)


def ensure_lint_and_lsp_commands_are_loaded() -> None:
    # Loading the module does the heavy lifting
    # However, having this function means that we do not have an "unused" import that some tool
    # gets tempted to remove
    assert ROOT_COMMAND.has_command("lsp")
    assert ROOT_COMMAND.has_command("lint")
    assert ROOT_COMMAND.has_command("reformat")
