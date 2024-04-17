import textwrap
from argparse import BooleanOptionalAction

from debputy.commands.debputy_cmd.context import ROOT_COMMAND, CommandContext, add_arg
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
                    '(debian-control-mode . ("debputy" "lsp" "server")))
            (add-to-list 'eglot-server-programs
                    '(debian-changelog-mode . ("debputy" "lsp" "server")))
            (add-to-list 'eglot-server-programs
                    '(debian-copyright-mode . ("debputy" "lsp" "server")))
        ;; Requires elpa-dpkg-dev-el (>> 37.11)
        ;;    (add-to-list 'eglot-server-programs
        ;;            '(debian-autopkgtest-control-mode . ("debputy" "lsp" "server")))
            ;; The debian/rules file uses the qmake mode.
            (add-to-list 'eglot-server-programs
                    '(makefile-gmake-mode . ("debputy" "lsp" "server")))
            (add-to-list 'eglot-server-programs
                    '(yaml-mode . ("debputy" "lsp" "server")))
        )

        ;; Auto-start eglot for the relevant modes.
        (add-hook 'debian-control-mode-hook 'eglot-ensure)
        ;; NOTE: changelog disabled by default because for some reason it
        ;;       this hook causes perceivable delay (several seconds) when
        ;;       opening the first changelog. It seems to be related to imenu.
        ;; (add-hook 'debian-changelog-mode-hook 'eglot-ensure)
        (add-hook 'debian-copyright-mode-hook 'eglot-ensure)
        ;; Requires elpa-dpkg-dev-el (>> 37.11)
        ;; (add-hook 'debian-autopkgtest-control-mode-hook 'eglot-ensure)
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
          \\     'cmdline': [ 'debputy', 'lsp', 'server' ]
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
                args: ['lsp', 'server']
            })
        endif

        autocmd User LspSetup g:LspOptionsSet({semanticHighlight: true})
        autocmd User LspSetup g:LspAddServer(lspServers)
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
        print("This version of debputy has editor snippets for the following editors: ")
        for editor_name, alias_of in content:
            print(f" * {editor_name:<{max_name}}{alias_of}")
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

    assert_can_start_lsp()

    from debputy.lsp.lsp_features import describe_lsp_features

    describe_lsp_features(context)


@ROOT_COMMAND.register_subcommand(
    "lint",
    log_only_to_stderr=True,
    argparser=[
        add_arg(
            "--spellcheck",
            dest="spellcheck",
            action="store_true",
            shared=True,
            help="Enable spellchecking",
        ),
        add_arg(
            "--auto-fix",
            dest="auto_fix",
            action="store_true",
            shared=True,
            help="Automatically fix problems with trivial or obvious corrections.",
        ),
        add_arg(
            "--linter-exit-code",
            dest="linter_exit_code",
            default=True,
            action=BooleanOptionalAction,
            help='Enable or disable the "linter" convention of exiting with an error if severe issues were found',
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


def ensure_lint_and_lsp_commands_are_loaded():
    # Loading the module does the heavy lifting
    # However, having this function means that we do not have an "unused" import that some tool
    # gets tempted to remove
    assert ROOT_COMMAND.has_command("lsp")
    assert ROOT_COMMAND.has_command("lint")
