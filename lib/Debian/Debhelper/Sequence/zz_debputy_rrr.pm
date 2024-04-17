use Debian::Debhelper::Dh_Lib qw(error);

insert_after('dh_builddeb', 'dh_debputy');
if (exists($INC{"Debian/Debhelper/Sequence/debputy.pm"})) {
    error("The zz-debputy-rrr sequence cannot be used with the (zz-)debputy sequence");
}
add_command_options('dh_debputy', '--integration-mode=rrr');

remove_command('dh_fixperms');
remove_command('dh_shlibdeps');
remove_command('dh_gencontrol');
remove_command('dh_md5sums');
remove_command('dh_builddeb');
1;
