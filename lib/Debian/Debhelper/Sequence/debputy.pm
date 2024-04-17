use Debian::Debhelper::Dh_Lib qw(error);

insert_after('dh_builddeb', 'dh_debputy');
if (exists($INC{"Debian/Debhelper/Sequence/zz_debputy_rrr.pm"})) {
    error("The (zz-)debputy sequence cannot be used with the zz-debputy-rrr sequence");
}
# Prune commands that debputy takes over; align with migrators_impl.py
remove_command('dh_install');
remove_command('dh_installdocs');
remove_command('dh_installchangelogs');
remove_command('dh_installexamples');
remove_command('dh_installman');
remove_command('dh_installcatalogs');
remove_command('dh_installcron');
remove_command('dh_installdebconf');
remove_command('dh_installemacsen');
remove_command('dh_installifupdown');
remove_command('dh_installinfo');
remove_command('dh_installinit');
remove_command('dh_installsysusers');
remove_command('dh_installtmpfiles');
remove_command('dh_installsystemd');
remove_command('dh_installsystemduser');
remove_command('dh_installmenu');
remove_command('dh_installmime');
remove_command('dh_installmodules');
remove_command('dh_installlogcheck');
remove_command('dh_installlogrotate');
remove_command('dh_installpam');
remove_command('dh_installppp');
remove_command('dh_installudev');
remove_command('dh_installgsettings');
remove_command('dh_installinitramfs');
remove_command('dh_installalternatives');
remove_command('dh_bugfiles');
remove_command('dh_ucf');
remove_command('dh_lintian');
remove_command('dh_icons');
remove_command('dh_usrlocal');
remove_command('dh_perl');
remove_command('dh_link');
remove_command('dh_installwm');
remove_command('dh_installxfonts');
remove_command('dh_strip_nondeterminism');
remove_command('dh_compress');
remove_command('dh_fixperms');
remove_command('dh_dwz');
remove_command('dh_strip');
remove_command('dh_makeshlibs');
remove_command('dh_shlibdeps');
remove_command('dh_missing');
remove_command('dh_installdeb');
remove_command('dh_gencontrol');
remove_command('dh_md5sums');
remove_command('dh_builddeb');

# Remove commands from add-ons where partial migration is possible

# sequence: gnome; we remove dh_gnome but not dh_gnome_clean for now.
remove_command('dh_gnome');
# sequence: lua; kept for its dependencies
remove_command('dh_lua');
# sequence: numpy; kept for its dependencies
remove_command('dh_numpy3');
# sequence: perl_openssl; kept for its dependencies
remove_command('dh_perl_openssl');

1;
