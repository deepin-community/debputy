package Dpkg::BuildDriver::Debputy;
use strict;
use warnings FATAL => 'all';
use Dpkg::ErrorHandling;

sub _run_cmd {
    my @cmd = @_;
    printcmd(@cmd);
    system @cmd and subprocerr("@cmd");
}

sub new {
    my ($this, %opts) = @_;
    my $class = ref($this) || $this;
    my $self = bless({
        'ctrl' => $opts{ctrl},
        'debputy_cmd' => 'debputy',
    }, $class);
    return $self;
}


sub pre_check {
    my ($self) = @_;
    my $ctrl_src = $self->{'ctrl'}->get_source();
    my $debputy_self_hosting_cmd = './debputy.sh';
    if ($ctrl_src->{"Source"} eq 'debputy' and -f -x $debputy_self_hosting_cmd) {
        $self->{'debputy_cmd'} = $debputy_self_hosting_cmd;
        notice("Detected this is a self-hosting build of debputy. Using \"${debputy_self_hosting_cmd}\" to self-host.");
    }
    return;
}

sub need_build_task {
    return 0;
}

sub run_task {
    my ($self, $task) = @_;
    _run_cmd($self->{'debputy_cmd'}, 'internal-command', 'dpkg-build-driver-run-task', $task);
    return;
}

1;
